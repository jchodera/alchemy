#!/usr/bin/python

#=============================================================================================
# MODULE DOCSTRING
#=============================================================================================

"""
Tests for alchemical factory in `alchemy.py`.

"""

#=============================================================================================
# GLOBAL IMPORTS
#=============================================================================================

import os
import numpy as np
import time
from functools import partial

from simtk import unit, openmm
from simtk.openmm import app

from nose.plugins.attrib import attr
import pymbar

import logging
logger = logging.getLogger(__name__)

from openmmtools import testsystems

from alchemy import AlchemicalState, AbsoluteAlchemicalFactory

from nose.plugins.skip import Skip, SkipTest

#=============================================================================================
# CONSTANTS
#=============================================================================================

kB = unit.BOLTZMANN_CONSTANT_kB * unit.AVOGADRO_CONSTANT_NA # Boltzmann constant
temperature = 300.0 * unit.kelvin # reference temperature
#MAX_DELTA = 0.01 * kB * temperature # maximum allowable deviation
MAX_DELTA = 1.0 * kB * temperature # maximum allowable deviation

#=============================================================================================
# SUBROUTINES FOR TESTING
#=============================================================================================

def config_root_logger(verbose, log_file_path=None, mpicomm=None):
    """Setup the the root logger's configuration.
     The log messages are printed in the terminal and saved in the file specified
     by log_file_path (if not None) and printed. Note that logging use sys.stdout
     to print logging.INFO messages, and stderr for the others. The root logger's
     configuration is inherited by the loggers created by logging.getLogger(name).
     Different formats are used to display messages on the terminal and on the log
     file. For example, in the log file every entry has a timestamp which does not
     appear in the terminal. Moreover, the log file always shows the module that
     generate the message, while in the terminal this happens only for messages
     of level WARNING and higher.
    Parameters
    ----------
    verbose : bool
        Control the verbosity of the messages printed in the terminal. The logger
        displays messages of level logging.INFO and higher when verbose=False.
        Otherwise those of level logging.DEBUG and higher are printed.
    log_file_path : str, optional, default = None
        If not None, this is the path where all the logger's messages of level
        logging.DEBUG or higher are saved.
    mpicomm : mpi4py.MPI.COMM communicator, optional, default=None
        If specified, this communicator will be used to determine node rank.
    """

    class TerminalFormatter(logging.Formatter):
        """
        Simplified format for INFO and DEBUG level log messages.
        This allows to keep the logging.info() and debug() format separated from
        the other levels where more information may be needed. For example, for
        warning and error messages it is convenient to know also the module that
        generates them.
        """

        # This is the cleanest way I found to make the code compatible with both
        # Python 2 and Python 3
        simple_fmt = logging.Formatter('%(asctime)-15s: %(message)s')
        default_fmt = logging.Formatter('%(asctime)-15s: %(levelname)s - %(name)s - %(message)s')

        def format(self, record):
            if record.levelno <= logging.INFO:
                return self.simple_fmt.format(record)
            else:
                return self.default_fmt.format(record)

    # Check if root logger is already configured
    n_handlers = len(logging.root.handlers)
    if n_handlers > 0:
        root_logger = logging.root
        for i in xrange(n_handlers):
            root_logger.removeHandler(root_logger.handlers[0])

    # If this is a worker node, don't save any log file
    if mpicomm:
        rank = mpicomm.rank
    else:
        rank = 0

    if rank != 0:
        log_file_path = None

    # Add handler for stdout and stderr messages
    terminal_handler = logging.StreamHandler()
    terminal_handler.setFormatter(TerminalFormatter())
    if rank != 0:
        terminal_handler.setLevel(logging.WARNING)
    elif verbose:
        terminal_handler.setLevel(logging.DEBUG)
    else:
        terminal_handler.setLevel(logging.INFO)
    logging.root.addHandler(terminal_handler)

    # Add file handler to root logger
    if log_file_path is not None:
        file_format = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
        file_handler = logging.FileHandler(log_file_path)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(file_format))
        logging.root.addHandler(file_handler)

    # Do not handle logging.DEBUG at all if unnecessary
    if log_file_path is not None:
        logging.root.setLevel(logging.DEBUG)
    else:
        logging.root.setLevel(terminal_handler.level)

def dump_xml(system=None, integrator=None, state=None):
    """
    Dump system, integrator, and state to XML for debugging.
    """
    from simtk.openmm import XmlSerializer
    def write_file(filename, contents):
        outfile = open(filename, 'w')
        outfile.write(contents)
        outfile.close()
    if system: write_file('system.xml', XmlSerializer.serialize(system))
    if integrator: write_file('integrator.xml', XmlSerializer.serialize(integrator))
    if state: write_file('state.xml', XmlSerializer.serialize(state))
    return

def compareSystemEnergies(positions, systems, descriptions, platform=None, precision=None):
    # Compare energies.
    timestep = 1.0 * unit.femtosecond

    if platform:
        platform_name = platform.getName()
        if precision:
            if platform_name == 'CUDA':
                platform.setDefaultPropertyValue('CudaPrecision', precision)
            elif platform_name == 'OpenCL':
                platform.setDefaultPropertyValue('OpenCLPrecision', precision)

    potentials = list()
    states = list()
    for system in systems:
        dump_xml(system=system)
        print('Creating integrator...')
        integrator = openmm.VerletIntegrator(timestep)
        print('Creating context...')
        dump_xml(integrator=integrator)
        if platform:
            context = openmm.Context(system, integrator, platform)
        else:
            context = openmm.Context(system, integrator)

        # Report which platform is in use.
        print("context platform: %s" % context.getPlatform().getName())

        print('Setting positions...')
        context.setPositions(positions)
        print('Getting energy and positions...')
        state = context.getState(getEnergy=True, getPositions=True)
        print('dumping XML...')
        dump_xml(system=system, integrator=integrator, state=state)
        print('Getting potential...')
        potential = state.getPotentialEnergy()
        potentials.append(potential)
        states.append(state)
        print('Cleaning up..')
        del context, integrator, state

    logger.info("========")
    for i in range(len(systems)):
        logger.info("%32s : %24.8f kcal/mol" % (descriptions[i], potentials[i] / unit.kilocalories_per_mole))
        if (i > 0):
            delta = potentials[i] - potentials[0]
            logger.info("%32s : %24.8f kcal/mol" % ('ERROR', delta / unit.kilocalories_per_mole))
            if (abs(delta) > MAX_DELTA):
                raise Exception("Maximum allowable deviation exceeded (was %.8f kcal/mol; allowed %.8f kcal/mol); test failed." % (delta / unit.kilocalories_per_mole, MAX_DELTA / unit.kilocalories_per_mole))

    return potentials

def alchemical_factory_check(reference_system, positions, receptor_atoms, ligand_atoms, platform_name=None, annihilate_electrostatics=True, annihilate_sterics=False, precision=None):
    """
    Compare energies of reference system and fully-interacting alchemically modified system.

    ARGUMENTS

    reference_system (simtk.openmm.System) - the reference System object to compare with
    positions - the positions to assess energetics for
    receptor_atoms (list of int) - the list of receptor atoms
    ligand_atoms (list of int) - the list of ligand atoms to alchemically modify
    precision : str, optional, default=None
       Precision model, or default if not specified. ('single', 'double', 'mixed')

    """

    # Create a factory to produce alchemical intermediates.
    print('Creating AbsoluteAlchemicalFactory...')
    logger.info("Creating alchemical factory...")
    initial_time = time.time()
    print('Creating AbsoluteAlchemicalFactory...')
    factory = AbsoluteAlchemicalFactory(reference_system, ligand_atoms=ligand_atoms, annihilate_electrostatics=annihilate_electrostatics, annihilate_sterics=annihilate_sterics)
    final_time = time.time()
    elapsed_time = final_time - initial_time
    logger.info("AbsoluteAlchemicalFactory initialization took %.3f s" % elapsed_time)

    print('Selecting platform')
    platform = None
    if platform_name:
        platform = openmm.Platform.getPlatformByName(platform_name)

    print('Creating perturbed system...')
    alchemical_system = factory.createPerturbedSystem()

    print('Comparing energies...')
    compareSystemEnergies(positions, [reference_system, alchemical_system], ['reference', 'alchemical'], platform=platform, precision=precision)

    return

def benchmark(reference_system, positions, receptor_atoms, ligand_atoms, platform_name=None, annihilate_electrostatics=True, annihilate_sterics=False, nsteps=500, timestep=1.0*unit.femtoseconds):
    """
    Benchmark performance of alchemically modified system relative to original system.

    Parameters
    ----------
    reference_system : simtk.openmm.System
       The reference System object to compare with
    positions : simtk.unit.Quantity with units compatible with nanometers
       The positions to assess energetics for.
    receptor_atoms : list of int
       The list of receptor atoms.
    ligand_atoms : list of int
       The list of ligand atoms to alchemically modify.
    platform_name : str, optional, default=None
       The name of the platform to use for benchmarking.
    annihilate_electrostatics : bool, optional, default=True
       If True, electrostatics will be annihilated; if False, decoupled.
    annihilate_sterics : bool, optional, default=False
       If True, sterics will be annihilated; if False, decoupled.
    nsteps : int, optional, default=500
       Number of molecular dynamics steps to use for benchmarking.
    timestep : simtk.unit.Quantity with units compatible with femtoseconds, optional, default=1*femtoseconds
       Timestep to use for benchmarking.

    """

    # Create a factory to produce alchemical intermediates.
    logger.info("Creating alchemical factory...")
    initial_time = time.time()
    factory = AbsoluteAlchemicalFactory(reference_system, ligand_atoms=ligand_atoms, annihilate_electrostatics=annihilate_electrostatics, annihilate_sterics=annihilate_sterics)
    final_time = time.time()
    elapsed_time = final_time - initial_time
    logger.info("AbsoluteAlchemicalFactory initialization took %.3f s" % elapsed_time)

    # Create an alchemically-perturbed state corresponding to nearly fully-interacting.
    # NOTE: We use a lambda slightly smaller than 1.0 because the AlchemicalFactory does not use Custom*Force softcore versions if lambda = 1.0 identically.
    lambda_value = 1.0 - 1.0e-6
    alchemical_state = AlchemicalState(lambda_coulomb=lambda_value, lambda_sterics=lambda_value, lambda_torsions=lambda_value)

    platform = None
    if platform_name:
        platform = openmm.Platform.getPlatformByName(platform_name)

    # Create the perturbed system.
    logger.info("Creating alchemically-modified state...")
    initial_time = time.time()
    alchemical_system = factory.createPerturbedSystem(alchemical_state)
    final_time = time.time()
    elapsed_time = final_time - initial_time
    # Compare energies.
    logger.info("Computing reference energies...")
    reference_integrator = openmm.VerletIntegrator(timestep)
    if platform:
        reference_context = openmm.Context(reference_system, reference_integrator, platform)
    else:
        reference_context = openmm.Context(reference_system, reference_integrator)
    reference_context.setPositions(positions)
    reference_state = reference_context.getState(getEnergy=True)
    reference_potential = reference_state.getPotentialEnergy()
    logger.info("Computing alchemical energies...")
    alchemical_integrator = openmm.VerletIntegrator(timestep)
    if platform:
        alchemical_context = openmm.Context(alchemical_system, alchemical_integrator, platform)
    else:
        alchemical_context = openmm.Context(alchemical_system, alchemical_integrator)
    alchemical_context.setPositions(positions)
    alchemical_state = alchemical_context.getState(getEnergy=True)
    alchemical_potential = alchemical_state.getPotentialEnergy()
    delta = alchemical_potential - reference_potential

    # Make sure all kernels are compiled.
    reference_integrator.step(1)
    alchemical_integrator.step(1)

    # Time simulations.
    logger.info("Simulating reference system...")
    initial_time = time.time()
    reference_integrator.step(nsteps)
    reference_state = reference_context.getState(getEnergy=True)
    reference_potential = reference_state.getPotentialEnergy()
    final_time = time.time()
    reference_time = final_time - initial_time
    logger.info("Simulating alchemical system...")
    initial_time = time.time()
    alchemical_integrator.step(nsteps)
    alchemical_state = alchemical_context.getState(getEnergy=True)
    alchemical_potential = alchemical_state.getPotentialEnergy()
    final_time = time.time()
    alchemical_time = final_time - initial_time

    logger.info("TIMINGS")
    logger.info("reference system       : %12.3f s for %8d steps (%12.3f ms/step)" % (reference_time, nsteps, reference_time/nsteps*1000))
    logger.info("alchemical system      : %12.3f s for %8d steps (%12.3f ms/step)" % (alchemical_time, nsteps, alchemical_time/nsteps*1000))
    logger.info("alchemical simulation is %12.3f x slower than unperturbed system" % (alchemical_time / reference_time))

    return delta

def overlap_check(reference_system, positions, receptor_atoms, ligand_atoms, platform_name=None, annihilate_electrostatics=True, annihilate_sterics=False, precision=None, nsteps=50, nsamples=200):
    """
    Test overlap between reference system and alchemical system by running a short simulation.

    Parameters
    ----------
    reference_system : simtk.openmm.System
       The reference System object to compare with
    positions : simtk.unit.Quantity with units compatible with nanometers
       The positions to assess energetics for.
    receptor_atoms : list of int
       The list of receptor atoms.
    ligand_atoms : list of int
       The list of ligand atoms to alchemically modify.
    platform_name : str, optional, default=None
       The name of the platform to use for benchmarking.
    annihilate_electrostatics : bool, optional, default=True
       If True, electrostatics will be annihilated; if False, decoupled.
    annihilate_sterics : bool, optional, default=False
       If True, sterics will be annihilated; if False, decoupled.
    nsteps : int, optional, default=50
       Number of molecular dynamics steps between samples.
    nsamples : int, optional, default=100
       Number of samples to collect.

    """

    # Create a fully-interacting alchemical state.
    factory = AbsoluteAlchemicalFactory(reference_system, ligand_atoms=ligand_atoms)
    alchemical_state = AlchemicalState()
    alchemical_system = factory.createPerturbedSystem(alchemical_state)

    temperature = 300.0 * unit.kelvin
    collision_rate = 5.0 / unit.picoseconds
    timestep = 2.0 * unit.femtoseconds
    kT = (kB * temperature)

    # Select platform.
    platform = None
    if platform_name:
        platform = openmm.Platform.getPlatformByName(platform_name)

    # Create integrators.
    reference_integrator = openmm.LangevinIntegrator(temperature, collision_rate, timestep)
    alchemical_integrator = openmm.VerletIntegrator(timestep)

    # Create contexts.
    if platform:
        reference_context = openmm.Context(reference_system, reference_integrator, platform)
        alchemical_context = openmm.Context(alchemical_system, alchemical_integrator, platform)
    else:
        reference_context = openmm.Context(reference_system, reference_integrator)
        alchemical_context = openmm.Context(alchemical_system, alchemical_integrator)

    # Report which platform is in use.
    print("reference_context platform: %s" % reference_context.getPlatform().getName())
    print("alchemical_context platform: %s" % alchemical_context.getPlatform().getName())

    # Collect simulation data.
    reference_context.setPositions(positions)
    du_n = np.zeros([nsamples], np.float64) # du_n[n] is the
    for sample in range(nsamples):
        # Run dynamics.
        reference_integrator.step(nsteps)

        # Get reference energies.
        reference_state = reference_context.getState(getEnergy=True, getPositions=True)
        reference_potential = reference_state.getPotentialEnergy()

        # Get alchemical energies.
        alchemical_context.setPositions(reference_state.getPositions())
        alchemical_state = alchemical_context.getState(getEnergy=True)
        alchemical_potential = alchemical_state.getPotentialEnergy()

        du_n[sample] = (alchemical_potential - reference_potential) / kT

    # Clean up.
    del reference_context, alchemical_context

    # Discard data to equilibration and subsample.
    from pymbar import timeseries
    [t0, g, Neff] = timeseries.detectEquilibration(du_n)
    indices = timeseries.subsampleCorrelatedData(du_n, g=g)
    du_n = du_n[indices]

    # Compute statistics.
    from pymbar import EXP
    [DeltaF, dDeltaF] = EXP(du_n)

    # Raise an exception if the error is larger than 3kT.
    MAX_DEVIATION = 3.0 # kT
    if (dDeltaF > MAX_DEVIATION):
        report = "DeltaF = %12.3f +- %12.3f kT (%5d samples, g = %6.1f)" % (DeltaF, dDeltaF, Neff, g)
        raise Exception(report)

    return

def rstyle(ax):
    '''Styles x,y axes to appear like ggplot2
    Must be called after all plot and axis manipulation operations have been
    carried out (needs to know final tick spacing)

    From:
    http://nbviewer.ipython.org/github/wrobstory/climatic/blob/master/examples/ggplot_styling_for_matplotlib.ipynb
    '''
    import pylab
    import matplotlib
    import matplotlib.pyplot as plt

    #Set the style of the major and minor grid lines, filled blocks
    ax.grid(True, 'major', color='w', linestyle='-', linewidth=1.4)
    ax.grid(True, 'minor', color='0.99', linestyle='-', linewidth=0.7)
    ax.patch.set_facecolor('0.90')
    ax.set_axisbelow(True)

    #Set minor tick spacing to 1/2 of the major ticks
    ax.xaxis.set_minor_locator((pylab.MultipleLocator((plt.xticks()[0][1]
                                -plt.xticks()[0][0]) / 2.0 )))
    ax.yaxis.set_minor_locator((pylab.MultipleLocator((plt.yticks()[0][1]
                                -plt.yticks()[0][0]) / 2.0 )))

    #Remove axis border
    for child in ax.get_children():
        if isinstance(child, matplotlib.spines.Spine):
            child.set_alpha(0)

    #Restyle the tick lines
    for line in ax.get_xticklines() + ax.get_yticklines():
        line.set_markersize(5)
        line.set_color("gray")
        line.set_markeredgewidth(1.4)

    #Remove the minor tick lines
    for line in (ax.xaxis.get_ticklines(minor=True) +
                 ax.yaxis.get_ticklines(minor=True)):
        line.set_markersize(0)

    #Only show bottom left ticks, pointing out of axis
    plt.rcParams['xtick.direction'] = 'out'
    plt.rcParams['ytick.direction'] = 'out'
    ax.xaxis.set_ticks_position('bottom')
    ax.yaxis.set_ticks_position('left')

def lambda_trace(reference_system, positions, receptor_atoms, ligand_atoms, platform_name=None, precision=None, annihilate_electrostatics=True, annihilate_sterics=False, nsteps=100):
    """
    Compute potential energy as a function of lambda.

    """
    # Create a factory to produce alchemical intermediates.
    factory = AbsoluteAlchemicalFactory(reference_system, ligand_atoms=ligand_atoms, annihilate_electrostatics=annihilate_electrostatics, annihilate_sterics=annihilate_sterics)

    platform = None
    if platform_name:
        # Get platform.
        platform = openmm.Platform.getPlatformByName(platform_name)

    if precision:
        if platform_name == 'CUDA':
            platform.setDefaultPropertyValue('CudaPrecision', precision)
        elif platform_name == 'OpenCL':
            platform.setDefaultPropertyValue('OpenCLPrecision', precision)

    # Take equally-sized steps.
    delta = 1.0 / nsteps

    def compute_potential(system, positions, platform=None):
        timestep = 1.0 * unit.femtoseconds
        integrator = openmm.VerletIntegrator(timestep)
        if platform:
            context = openmm.Context(system, integrator, platform)
        else:
            context = openmm.Context(system, integrator)
        context.setPositions(positions)
        state = context.getState(getEnergy=True)
        potential = state.getPotentialEnergy()
        del integrator, context
        return potential

    # Compute unmodified energy.
    u_original = compute_potential(reference_system, positions, platform)

    # Scan through lambda values.
    lambda_i = np.zeros([nsteps+1], np.float64) # lambda values for u_i
    u_i = unit.Quantity(np.zeros([nsteps+1], np.float64), unit.kilocalories_per_mole) # u_i[i] is the potential energy for lambda_i[i]
    for i in range(nsteps+1):
        lambda_value = 1.0-i*delta # compute lambda value for this step
        alchemical_system = factory.createPerturbedSystem(AlchemicalState(lambda_coulomb=lambda_value, lambda_sterics=lambda_value, lambda_torsions=lambda_value))
        lambda_i[i] = lambda_value
        u_i[i] = compute_potential(alchemical_system, positions, platform)
        logger.info("%12.9f %24.8f kcal/mol" % (lambda_i[i], u_i[i] / unit.kilocalories_per_mole))

    # Write figure as PDF.
    import pylab
    from matplotlib.backends.backend_pdf import PdfPages
    import matplotlib.pyplot as plt
    with PdfPages('lambda-trace.pdf') as pdf:
        fig = plt.figure(figsize=(10, 5))
        ax = fig.add_subplot(111)
        plt.plot(1, u_original / unit.kilocalories_per_mole, 'ro', label='unmodified')
        plt.plot(lambda_i, u_i / unit.kilocalories_per_mole, 'k.', label='alchemical')
        plt.title('T4 lysozyme L99A + p-xylene : AMBER96 + OBC GBSA')
        plt.ylabel('potential (kcal/mol)')
        plt.xlabel('lambda')
        ax.legend()
        rstyle(ax)
        pdf.savefig()  # saves the current figure into a pdf page
        plt.close()

    return

def generate_trace(test_system):
    lambda_trace(test_system['test'].system, test_system['test'].positions, test_system['receptor_atoms'], test_system['ligand_atoms'])
    return

#=============================================================================================
# TEST SYSTEM DEFINITIONS
#=============================================================================================

test_systems = dict()
test_systems['Lennard-Jones cluster'] = {
    'test' : testsystems.LennardJonesCluster(),
    'ligand_atoms' : range(0,1), 'receptor_atoms' : range(1,2) }
test_systems['Lennard-Jones fluid without dispersion correction'] = {
    'test' : testsystems.LennardJonesFluid(dispersion_correction=False),
    'ligand_atoms' : range(0,1), 'receptor_atoms' : range(1,2) }
test_systems['Lennard-Jones fluid with dispersion correction'] = {
    'test' : testsystems.LennardJonesFluid(dispersion_correction=True),
    'ligand_atoms' : range(0,1), 'receptor_atoms' : range(1,2) }
test_systems['TIP3P with reaction field, no charges, no switch, no dispersion correction'] = {
    'test' : testsystems.DischargedWaterBox(dispersion_correction=False, switch=False, nonbondedMethod=app.CutoffPeriodic),
    'ligand_atoms' : range(0,3), 'receptor_atoms' : range(3,6) }
test_systems['TIP3P with reaction field, switch, no dispersion correction'] = {
    'test' : testsystems.WaterBox(dispersion_correction=False, switch=True, nonbondedMethod=app.CutoffPeriodic),
    'ligand_atoms' : range(0,3), 'receptor_atoms' : range(3,6) }
test_systems['TIP3P with reaction field, no switch, dispersion correction'] = {
    'test' : testsystems.WaterBox(dispersion_correction=True, switch=False, nonbondedMethod=app.CutoffPeriodic),
    'ligand_atoms' : range(0,3), 'receptor_atoms' : range(3,6) }
test_systems['TIP3P with reaction field, switch, dispersion correction'] = {
    'test' : testsystems.WaterBox(dispersion_correction=True, switch=True, nonbondedMethod=app.CutoffPeriodic),
    'ligand_atoms' : range(0,3), 'receptor_atoms' : range(3,6) }
test_systems['alanine dipeptide in vacuum'] = {
    'test' : testsystems.AlanineDipeptideVacuum(),
    'ligand_atoms' : range(0,22), 'receptor_atoms' : range(22,22) }
test_systems['alanine dipeptide in vacuum with annihilated sterics'] = {
    'test' : testsystems.AlanineDipeptideVacuum(),
    'ligand_atoms' : range(0,22), 'receptor_atoms' : range(22,22),
    'annihilate_sterics' : True, 'annihilate_electrostatics' : True }
test_systems['alanine dipeptide in OBC GBSA'] = {
    'test' : testsystems.AlanineDipeptideImplicit(),
    'ligand_atoms' : range(0,22), 'receptor_atoms' : range(22,22) }
test_systems['alanine dipeptide in OBC GBSA, with sterics annihilated'] = {
    'test' : testsystems.AlanineDipeptideImplicit(),
    'ligand_atoms' : range(0,22), 'receptor_atoms' : range(22,22),
    'annihilate_sterics' : True, 'annihilate_electrostatics' : True }
test_systems['alanine dipeptide in TIP3P with reaction field'] = {
    'test' : testsystems.AlanineDipeptideExplicit(nonbondedMethod=app.CutoffPeriodic),
    'ligand_atoms' : range(0,22), 'receptor_atoms' : range(22,22) }
test_systems['T4 lysozyme L99A with p-xylene in OBC GBSA'] = {
    'test' : testsystems.LysozymeImplicit(),
    'ligand_atoms' : range(2603,2621), 'receptor_atoms' : range(0,2603) }
test_systems['DHFR in explicit solvent with reaction field, annihilated'] = {
    'test' : testsystems.DHFRExplicit(nonbondedMethod=app.CutoffPeriodic),
    'ligand_atoms' : range(0,2849), 'receptor_atoms' : [],
    'annihilate_sterics' : True, 'annihilate_electrostatics' : True }
test_systems['Src in TIP3P with reaction field, with Src sterics annihilated'] = {
    'test' : testsystems.SrcExplicit(nonbondedMethod=app.CutoffPeriodic),
    'ligand_atoms' : range(0,4428), 'receptor_atoms' : [],
    'annihilate_sterics' : True, 'annihilate_electrostatics' : True }
test_systems['Src in GBSA'] = {
    'test' : testsystems.SrcImplicit(),
    'ligand_atoms' : range(0,4427), 'receptor_atoms' : [],
    'annihilate_sterics' : False, 'annihilate_electrostatics' : False }
test_systems['Src in GBSA, with Src sterics annihilated'] = {
    'test' : testsystems.SrcImplicit(),
    'ligand_atoms' : range(0,4427), 'receptor_atoms' : [],
    'annihilate_sterics' : True, 'annihilate_electrostatics' : True }

# Problematic tests: PME is not fully implemented yet
test_systems['TIP3P with PME, no switch, no dispersion correction'] = {
    'test' : testsystems.WaterBox(dispersion_correction=False, switch=False, nonbondedMethod=app.PME),
    'ligand_atoms' : range(0,3), 'receptor_atoms' : range(3,6) }

# Slow tests
#test_systems['Src in OBC GBSA'] = {
#    'test' : testsystems.SrcImplicit(),
#    'ligand_atoms' : range(0,21), 'receptor_atoms' : range(21,7208) }
#test_systems['Src in TIP3P with reaction field'] = {
#    'test' : testsystems.SrcExplicit(nonbondedMethod=app.CutoffPeriodic),
#    'ligand_atoms' : range(0,21), 'receptor_atoms' : range(21,4091) }

fast_testsystem_names = [
    'Lennard-Jones cluster',
    'Lennard-Jones fluid without dispersion correction',
    'Lennard-Jones fluid with dispersion correction',
    'TIP3P with reaction field, no charges, no switch, no dispersion correction',
    'TIP3P with reaction field, switch, no dispersion correction',
    'TIP3P with reaction field, switch, dispersion correction',
    'alanine dipeptide in vacuum with annihilated sterics',
    'TIP3P with PME, no switch, no dispersion correction' # PME still problematic
    ]


#=============================================================================================
# NOSETEST GENERATORS
#=============================================================================================

@attr('slow')
def test_overlap():
    """
    Generate nose tests for overlap for all alchemical test systems.
    """
    for name in fast_testsystem_names:
        test_system = test_systems[name]
        reference_system = test_system['test'].system
        positions = test_system['test'].positions
        ligand_atoms = test_system['ligand_atoms']
        receptor_atoms = test_system['receptor_atoms']
        annihilate_sterics = False if 'annihilate_sterics' not in test_system else test_system['annihilate_sterics']
        f = partial(overlap_check, reference_system, positions, receptor_atoms, ligand_atoms, annihilate_sterics=annihilate_sterics)
        f.description = "Testing reference/alchemical overlap for %s..." % name
        yield f

    return

def test_alchemical_accuracy():
    """
    Generate nose tests for overlap for all alchemical test systems.
    """
    for name in test_systems.keys():
        test_system = test_systems[name]
        reference_system = test_system['test'].system
        positions = test_system['test'].positions
        ligand_atoms = test_system['ligand_atoms']
        receptor_atoms = test_system['receptor_atoms']
        annihilate_sterics = False if 'annihilate_sterics' not in test_system else test_system['annihilate_sterics']
        f = partial(alchemical_factory_check, reference_system, positions, receptor_atoms, ligand_atoms, annihilate_sterics=annihilate_sterics)
        f.description = "Testing alchemical fidelity of %s..." % name
        yield f

    return

#=============================================================================================
# MAIN FOR MANUAL DEBUGGING
#=============================================================================================

if __name__ == "__main__":
    #generate_trace(test_systems['TIP3P with reaction field, switch, dispersion correction'])
    config_root_logger(True)

    #name = 'Lennard-Jones fluid with dispersion correction'
    #name = 'Src in GBSA, with Src sterics annihilated'
    name = 'Src in GBSA'
    #name = 'alanine dipeptide in OBC GBSA, with sterics annihilated'
    #name = 'alanine dipeptide in OBC GBSA'
    test_system = test_systems[name]
    reference_system = test_system['test'].system
    positions = test_system['test'].positions
    ligand_atoms = test_system['ligand_atoms']
    receptor_atoms = test_system['receptor_atoms']
    alchemical_factory_check(reference_system, positions, receptor_atoms, ligand_atoms)

