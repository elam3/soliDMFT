#
# This python script allows one to perform DFT+DMFT calculations with VASP
# or with a pre-defined h5 archive (only one-shot) for
# multiband/many-correlated-shells systems using the TRIQS package,
# in combination with the CThyb solver and SumkDFT from DFT-tools.
# triqs version 2.0 or higher is required
#
# Written by Alexander Hampel, Sophie Beck
# Materials Theory, ETH Zurich,

# the future numpy (>1.15) is not fully compatible with triqs 2.0 atm
# suppress warnings
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

# system
import os.path
import os
import sys
import numpy as np
import shutil
from timeit import default_timer as timer
import configparser as cp
import errno
import re
import shutil

# triqs
import pytriqs.utility.mpi as mpi
from pytriqs.operators.util import *
from pytriqs.archive import HDFArchive
try:
    # TRIQS 2.0
    from triqs_cthyb import *
    from triqs_dft_tools.sumk_dft import *
    from triqs_dft_tools.sumk_dft_tools import *
    from triqs_dft_tools.converters.vasp_converter import *
    from triqs_dft_tools.converters.plovasp.vaspio import VaspData
    import triqs_dft_tools.converters.plovasp.converter as plo_converter
    from pytriqs.gf import *
except ImportError:
    # TRIQS 1.4
    from pytriqs.applications.impurity_solvers.cthyb import *
    from pytriqs.applications.dft.sumk_dft import *
    from pytriqs.applications.dft.sumk_dft_tools import *
    from pytriqs.applications.dft.converters.vasp_converter import *
    from pytriqs.applications.dft.converters.plovasp.vaspio import VaspData
    import pytriqs.applications.dft.converters.plovasp.converter as plo_converter
    from pytriqs.gf.local import *

# own modules
from read_config import *
from observables import *
from dmft_cycle import *
from csc_flow import csc_flow_control
import toolset as toolset

# timing information
if mpi.is_master_node(): global_start = timer()

# reading configuration for calculation
general_parameters = {}
solver_parameters = {}
if mpi.is_master_node():
    if len(sys.argv) > 1:
        print 'reading the config file '+str(sys.argv[1])
        general_parameters, solver_parameters = read_config(str(sys.argv[1]))
        general_parameters['config_file'] = str(sys.argv[1])
    else:
        print 'reading the config file dmft_config.ini'
        general_parameters, solver_parameters = read_config('dmft_config.ini')
        general_parameters['config_file'] = 'dmft_config.ini'
    print '-------------------------- \n General parameters:'
    for key, value in general_parameters.iteritems():
        print "{0: <20}".format(key)+"{0: <4}".format(str(value))
    print '-------------------------- \n Solver parameters:'
    for key, value in solver_parameters.iteritems():
        print "{0: <20}".format(key)+"{0: <4}".format(str(value))

solver_parameters = mpi.bcast(solver_parameters)
general_parameters = mpi.bcast(general_parameters)

# start CSC calculation if csc is set to true
if general_parameters['csc']:

    # check if seedname is only one Value
    if len(general_parameters['seedname']) > 1:
        mpi.report('!!! WARNING !!!')
        mpi.report('CSC calculations can only be done for one set of files at a time')

    # some basic setup that needs to be done for CSC calculations
    general_parameters['seedname'] = general_parameters['seedname'][0]
    general_parameters['jobname'] = '.'
    general_parameters['previous_file'] = 'none'

    # run the whole machinery
    csc_flow_control(general_parameters, solver_parameters)

# do a one-shot calculation with given h5 archive
else:
    # extract filenames and do a dmft iteration for every h5 archive given
    number_of_calculations = len(general_parameters['seedname'])
    filenames = general_parameters['seedname']
    foldernames = general_parameters['jobname']
    mpi.report(str(number_of_calculations)+' DMFT calculation will be made for the following files: '+str(filenames))

    # check for h5 file(s)
    if mpi.is_master_node():
        for i, file in enumerate(filenames):
            if not os.path.exists(file+'.h5'):
                mpi.report('*** Input h5 file(s) not found! I was looking for '+file+'.h5 ***')
                mpi.MPI.COMM_WORLD.Abort(1)

    for i, file in enumerate(foldernames):
        general_parameters['seedname'] = filenames[i]
        general_parameters['jobname'] = foldernames[i]
        if i == 0:
            general_parameters['previous_file'] = 'none'
        else:
            previous_file = filenames[i-1]
            previous_folder = foldernames[i-1]
            general_parameters['previous_file'] = previous_folder+'/'+previous_file+'.h5'

        if mpi.is_master_node():
            # create output directory
            print 'calculation is performed in subfolder: '+general_parameters['jobname']
            if not os.path.exists(general_parameters['jobname']):
                os.makedirs(general_parameters['jobname'])

                # copy h5 archive and config file to created folder
                shutil.copyfile(general_parameters['seedname']+'.h5',
                general_parameters['jobname']+'/'+general_parameters['seedname']+'.h5')
                shutil.copyfile(general_parameters['config_file'],
                general_parameters['jobname']+'/'+general_parameters['config_file'])
            else:
                print '#'*80+'\n WARNING! specified job folder already exists continuing previous job! \n'+'#'*80+'\n'

        mpi.report("#"*80)
        mpi.report('starting the DMFT calculation for '+str(general_parameters['seedname']))
        mpi.report("#"*80)

        # basic H5 archive checks and setup
        if mpi.is_master_node():
            h5_archive = HDFArchive(general_parameters['jobname']+'/'+general_parameters['seedname']+'.h5','a')
            if not 'DMFT_results' in h5_archive:
                h5_archive.create_group('DMFT_results')
            if not 'last_iter' in h5_archive['DMFT_results']:
                h5_archive['DMFT_results'].create_group('last_iter')
            if not 'DMFT_input' in h5_archive:
                h5_archive.create_group('DMFT_input')

        # prepare observable dicts and files, which is stored on the master node
        observables = dict()
        if mpi.is_master_node():
            observables = prep_observables(general_parameters, h5_archive)
        observables = mpi.bcast(observables)

        ############################################################
        # run the dmft_cycle
        observables = dmft_cycle(general_parameters, solver_parameters, observables)
        ############################################################

if mpi.is_master_node():
    global_end = timer()
    print '-------------------------------'
    print 'overall elapsed time: %10.4f seconds'%(global_end-global_start)
