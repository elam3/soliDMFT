# contains all the helper functions:
# is_vasp_lock_present
# is_vasp_running
# store_dft_eigvals
# get_dft_energy
# get_dft_mu
# check_convergence
# determine_block_structure
# load_sigma_from_h5

import errno
import numpy as np

# triqs
import pytriqs.utility.mpi as mpi
try:
    # TRIQS 2.0
    from triqs_dft_tools.sumk_dft import *
    from triqs_dft_tools.sumk_dft_tools import *
except ImportError:
    # TRIQS 1.4
    from pytriqs.applications.dft.sumk_dft import *
    from pytriqs.applications.dft.sumk_dft_tools import *

from observables import *

def is_vasp_lock_present():
    """
    small function to check if vasp is still running
    """
    res_bool = False
    if mpi.is_master_node():
        res_bool = os.path.isfile('./vasp.lock')
    res_bool = mpi.bcast(res_bool)
    return res_bool

def store_dft_eigvals(config_file, path_to_h5, iteration ):
    """
    save the eigenvalues from LOCPROJ file to calc directory
    """
    ar = HDFArchive(path_to_h5,'a')
    if not 'dft_eigvals' in ar: ar.create_group('dft_eigvals')

    vasp_data = VaspData('./')
    eigenvals = vasp_data.plocar.eigs[:,:,0]

    for ik in range(0, vasp_data.plocar.eigs[:,0,0].shape[0]):
        eigenvals[ik,:] = eigenvals[ik,:]-vasp_data.plocar.efermi

    ar['dft_eigvals']['it_'+str(iteration)] = eigenvals

    del ar

    return

def get_dft_energy():
    """
    Reads energy from the last line of OSZICAR.
    """
    with open('OSZICAR', 'r') as f:
        nextline = f.readline()
        while nextline.strip():
            line = nextline
            nextline = f.readline()
    try:
        dft_energy = float(line.split()[2])
    except ValueError:
        print "Cannot read energy from OSZICAR, setting it to zero"
        dft_energy = 0.0
    return dft_energy

def get_dft_mu():
    """
    Reads fermi energy from the first line of LOCPROJ.
    """
    with open('LOCPROJ', 'r') as f:
        line = f.readline()
    try:
        fermi_energy = float(line.split()[4])
    except ValueError:
        print "Cannot read energy from OSZICAR, setting it to zero"
        fermi_energy = 0.0
    return fermi_energy

def check_convergence(SK,general_parameters,observables):
    """
    check last x iterations for convergence and stop if criteria is reached

    Parameters
    ----------
    SK : SumK Object instances

    general_parameters : dict
        general parameters as a dict

    observables : list of dicts
        observable arrays

    __Returns:__
    converged : bool
        true if desired accuracy is reached

    std_dev : list of floats
        list of std_dev from the last #iterations

    """
    converged = False
    iterations = general_parameters['occ_conv_it']

    print "="*60
    print 'checking covergence of the last '+str(iterations)+' iterations:'
    #loading the observables file
    avg_occ = []
    std_dev = []

    for icrsh in range(SK.n_inequiv_shells):

        mean = (np.mean(observables['imp_occ'][icrsh]['up'][-iterations:])+
                np.mean(observables['imp_occ'][icrsh]['down'][-iterations:]))

        std = (np.std(observables['imp_occ'][icrsh]['up'][-iterations:])+
               np.std(observables['imp_occ'][icrsh]['down'][-iterations:]))

        avg_occ.append(mean)
        std_dev.append(std)
        print 'Average occupation of impurity '+str(icrsh)+': '+"{:10.5f}".format(avg_occ[icrsh])
        print 'Standard deviation of impurity '+str(icrsh)+': '+"{:10.5f}".format(std_dev[icrsh])

    if all(i < general_parameters['occ_conv_crit'] for i in std_dev) == True:
        converged = True

    print "="*60
    print

    return converged, std_dev

def determine_block_structure(SK, general_parameters):
    """
    determines block structrure and degenerate deg_shells
    computes first DFT density matrix to determine block structure and changes
    the density matrix according to needs i.e. magnetic calculations, or keep
    off-diag elements

    Parameters
    ----------
    SK : SumK Object instances

    __Returns:__
    SK : SumK Object instances
        updated SK Object
    shell_multiplicity : list of int
        list that contains the shell_multiplicity of each ineq impurity
    """
    mpi.report('\n *** determination of block structure ***')

    # this returns a list of dicts (one entry for each corr shell)
    # the dict contains one entry for up and one for down
    # each entry is a square complex numpy matrix with dim=corr_shell['dim']
    dens_mat = SK.density_matrix(method = 'using_gf', beta = general_parameters['beta'])

    # if we want to do a magnetic calculation we need to lift up/down degeneracy
    if general_parameters['magnetic']:
        mpi.report('magnetic calculation: removing the spin degeneracy from the block structure')
        for i, elem in enumerate(dens_mat):
            for key, value in elem.iteritems():
                if key == 'up':
                    for a in range(0,len(value[:,0])):
                        for b in range(0,len(value[0,:])):
                            if a==b:
                                dens_mat[i][key][a,b] = value[a,b]*1.1
                elif key == 'down':
                    for a in range(0,len(value[:,0])):
                        for b in range(0,len(value[0,:])):
                            if a==b:
                                dens_mat[i][key][a,b] = value[a,b]*0.9
                else:
                    mpi.report('warning spin channels not found! Doing a PM calculation')

    # for certain systems it is needed to keep off diag elements
    # this enforces to use the full corr subspace matrix
    if general_parameters['enforce_off_diag']:
        mpi.report('enforcing off-diagonal elements in block structure finder')
        for i, elem in enumerate(dens_mat):
            for key, value in elem.iteritems():
                for a in range(0,len(value[:,0])):
                    for b in range(0,len(value[0,:])):
                        if a!=b:
                            dens_mat[i][key][a,b] += 0.05

    SK.analyse_block_structure(dm=dens_mat,threshold=general_parameters['block_threshold'])

    # Summary of block structure finder and determination of shell_multiplicity
    shell_multiplicity = [0 for icrsh in range(SK.n_inequiv_shells)]
    if mpi.is_master_node():
        print "\n number of ineq. correlated shells: %d"%(SK.n_inequiv_shells)
        # correlated shells and their structure
        print "\n block structure summary"
        for icrsh in range(SK.n_inequiv_shells):
            shlst = []
            for ish in range(SK.n_corr_shells):
                if SK.corr_to_inequiv[ish] == icrsh: shlst.append(ish)
            shell_multiplicity[icrsh] = len(shlst)
            print " -- Shell type #%3d : "%icrsh + format(shlst)
            print "  | shell multiplicity "+str(shell_multiplicity[icrsh])
            print "  | block struct. : " + format(SK.gf_struct_solver[icrsh])
            print "  | deg. orbitals : " + format(SK.deg_shells[icrsh])

        print "\n rotation matrices "
        # rotation matrices
        for icrsh in range(SK.n_corr_shells):
            n_orb = SK.corr_shells[icrsh]['dim']
            print 'rot_mat[%2d] '%(icrsh)+'real part'.center(9*n_orb)+'  '+'imaginary part'.center(9*n_orb)
            rot = np.matrix( SK.rot_mat[icrsh] )
            for irow in range(n_orb):
                fmt = '{:9.5f}' * n_orb
                row = np.real(rot[irow,:]).tolist()[0] + np.imag(rot[irow,:]).tolist()[0]
                print ('           '+fmt+'  '+fmt).format(*row)

        print '\n'

    shell_multiplicity = mpi.bcast(shell_multiplicity)

    return SK, shell_multiplicity

def print_block_sym(SK, shell_multiplicity):
    # Summary of block structure finder and determination of shell_multiplicity
    shell_multiplicity = [0 for icrsh in range(SK.n_inequiv_shells)]
    if mpi.is_master_node():
        print "\n number of ineq. correlated shells: %d"%(SK.n_inequiv_shells)
        # correlated shells and their structure
        print "\n block structure summary"
        for icrsh in range(SK.n_inequiv_shells):
            shlst = []
            for ish in range(SK.n_corr_shells):
                if SK.corr_to_inequiv[ish] == icrsh: shlst.append(ish)
            shell_multiplicity[icrsh] = len(shlst)
            print " -- Shell type #%3d : "%icrsh + format(shlst)
            print "  | shell multiplicity "+str(shell_multiplicity[icrsh])
            print "  | block struct. : " + format(SK.gf_struct_solver[icrsh])
            print "  | deg. orbitals : " + format(SK.deg_shells[icrsh])

        print "\n rotation matrices "
        # rotation matrices
        for icrsh in range(SK.n_corr_shells):
            n_orb = SK.corr_shells[icrsh]['dim']
            print 'rot_mat[%2d] '%(icrsh)+'real part'.center(9*n_orb)+'  '+'imaginary part'.center(9*n_orb)
            rot = np.matrix( SK.rot_mat[icrsh] )
            for irow in range(n_orb):
                fmt = '{:9.5f}' * n_orb
                row = np.real(rot[irow,:]).tolist()[0] + np.imag(rot[irow,:]).tolist()[0]
                print ('           '+fmt+'  '+fmt).format(*row)

        print '\n'

def load_sigma_from_h5(path_to_h5, iteration):
    """
    Reads impurity self-energy for all impurities from file and returns them as a list

    Parameters
    ----------
    path_to_h5 : string
        path to h5 archive
    iteration : int
        at which iteration will sigma be loaded

    __Returns:__
    self_energies : list of green functions

    dc_imp : numpy array
        DC potentials
    dc_energ : numpy array
        DC energies per impurity
    """
    self_energies = []

    old_calc = HDFArchive(path_to_h5,'r')
    if iteration == -1:
        for icrsh in range(old_calc['dft_input']['n_inequiv_shells']):
            print 'loading Sigma_imp'+str(icrsh)+' at last iteration from '+path_to_h5
            self_energies.append(old_calc['DMFT_results']['last_iter']['Sigma_iw_'+str(icrsh)])

        # loading DC from this iteration as well!
        dc_imp = old_calc['DMFT_results']['last_iter']['DC_pot']
        dc_energ = old_calc['DMFT_results']['last_iter']['DC_energ']
    else:
        for icrsh in range(old_calc['dft_input']['n_inequiv_shells']):
            print 'loading Sigma_imp'+str(icrsh)+' at it '+str(iteration)+' from '+path_to_h5
            self_energies.append(old_calc['DMFT_results']['it_'+str(iteration)]['Sigma_iw_'+str(icrsh)])

        # loading DC from this iteration as well!
        dc_imp = old_calc['DMFT_results']['it_'+str(iteration)]['DC_pot']
        dc_energ = old_calc['DMFT_results']['it_'+str(iteration)]['DC_energ']

    del old_calc

    return self_energies, dc_imp, dc_energ
