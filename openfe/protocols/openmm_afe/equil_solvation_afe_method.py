# This code is part of OpenFE and is licensed under the MIT license.
# For details, see https://github.com/OpenFreeEnergy/openfe
"""OpenMM Equilibrium Solvation AFE Protocol --- :mod:`openfe.protocols.openmm_afe.equil_solvation_afe_methods`
===============================================================================================================

This module implements the necessary methodology tooling to run calculate an
absolute solvation free energy using OpenMM tools and one of the following
alchemical sampling methods:

* Hamiltonian Replica Exchange
* Self-adjusted mixture sampling
* Independent window sampling

Current limitations
-------------------
* Disapearing molecules are only allowed in state A. Support for
  appearing molecules will be added in due course.
* Only small molecules are allowed to act as alchemical molecules.
  Alchemically changing protein or solvent components would induce
  perturbations which are too large to be handled by this Protocol.


Acknowledgements
----------------
* Originally based on hydration.py in
  `espaloma <https://github.com/choderalab/espaloma_charge>`_


TODO
----
* Add in all the AlchemicalFactory and AlchemicalRegion kwargs
  as settings.
* Allow for a more flexible setting of Lambda regions.
* Add support for restraints.
* Improve this docstring by adding an example use case.

"""
from __future__ import annotations

import os
import logging

from collections import defaultdict
import gufe
from gufe.components import Component
import numpy as np
import numpy.typing as npt
import openmm
from openff.units import unit
from openff.units.openmm import from_openmm, to_openmm, ensure_quantity
from openmmtools import multistate
from openmmtools.states import (SamplerState,
                                ThermodynamicState,
                                create_thermodynamic_state_protocol,)
from openmmtools.alchemy import (AlchemicalRegion, AbsoluteAlchemicalFactory,
                                 AlchemicalState,)
from typing import Dict, List, Optional
from openmm import app
from openmm import unit as omm_unit
from openmmforcefields.generators import SystemGenerator
import pathlib
from typing import Any, Iterable
import openmmtools
import uuid
import mdtraj as mdt

from gufe import (
    settings, ChemicalSystem, SmallMoleculeComponent,
    ProteinComponent, SolventComponent
)
from openfe.protocols.openmm_utils.omm_settings import (
    SettingsBaseModel,
)
from openfe.protocols.openmm_afe.equil_afe_settings import (
    AbsoluteTransformSettings, SystemSettings,
    SolvationSettings, AlchemicalSettings,
    AlchemicalSamplerSettings, OpenMMEngineSettings,
    IntegratorSettings, SimulationSettings,
)
from openfe.protocols.openmm_rfe._rfe_utils import compute
from ..openmm_utils import (
    system_validation, settings_validation, system_creation
)


logger = logging.getLogger(__name__)


class AbsoluteTransformProtocolResult(gufe.ProtocolResult):
    """Dict-like container for the output of a AbsoluteTransform"""
    def __init__(self, **data):
        super().__init__(**data)
        # TODO: Detect when we have extensions and stitch these together?
        if any(len(files['nc_paths']) > 2 for files in self.data['nc_files']):
            raise NotImplementedError("Can't stitch together results yet")

        self._analyzers = []
        for f in self.data['nc_files']:
            nc = f['nc_paths'][0]
            chk = f['checkpoint_paths'][0]
            reporter = multistate.MultiStateReporter(
                           storage=nc,
                           checkpoint_storage=chk)
            analyzer = multistate.MultiStateSamplerAnalyzer(reporter)
            self._analyzers.append(analyzer)

    def get_estimate(self):
        """Free energy difference of this transformation

        Returns
        -------
        dG : unit.Quantity
          The free energy difference between the first and last states. This is
          a Quantity defined with units.

        TODO
        ----
        * Check this holds up completely for SAMS.
        """
        dGs = []

        for analyzer in self._analyzers:
            # this returns:
            # (matrix of) estimated free energy difference
            # (matrix of) estimated statistical uncertainty (one S.D.)
            dG, _ = analyzer.get_free_energy()
            dG = (dG[0, -1] * analyzer.kT).in_units_of(
                omm_unit.kilocalories_per_mole)

            dGs.append(dG)

        avg_val = np.average([i.value_in_unit(dGs[0].unit) for i in dGs])

        return avg_val * dGs[0].unit

    def get_uncertainty(self):
        """The uncertainty/error in the dG value"""
        dGs = []

        for analyzer in self._analyzers:
            # this returns:
            # (matrix of) estimated free energy difference
            # (matrix of) estimated statistical uncertainty (one S.D.)
            dG, _ = analyzer.get_free_energy()
            dG = (dG[0, -1] * analyzer.kT).in_units_of(
                omm_unit.kilocalories_per_mole)

            dGs.append(dG)

        std_val = np.std([i.value_in_unit(dGs[0].unit) for i in dGs])

        return std_val * dGs[0].unit

    def get_rate_of_convergence(self):  # pragma: no-cover
        raise NotImplementedError


class AbsoluteSolvationProtocol(gufe.Protocol):
    result_cls = AbsoluteTransformProtocolResult
    _settings: AbsoluteTransformSettings

    @classmethod
    def _default_settings(cls):
        """A dictionary of initial settings for this creating this Protocol

        These settings are intended as a suitable starting point for creating
        an instance of this protocol.  It is recommended, however that care is
        taken to inspect and customize these before performing a Protocol.

        Returns
        -------
        Settings
          a set of default settings
        """
        return AbsoluteTransformSettings(
            forcefield_settings=settings.OpenMMSystemGeneratorFFSettings(),
            thermo_settings=settings.ThermoSettings(
                temperature=298.15 * unit.kelvin,
                pressure=1 * unit.bar,
            ),
            solvent_system_settings=SystemSettings(),
            vacuum_system_settings=SystemSettings(nonbonded_method='nocutoff'),
            alchemical_settings=AlchemicalSettings(),
            alchemsampler_settings=AlchemicalSamplerSettings(),
            solvation_settings=SolvationSettings(),
            engine_settings=OpenMMEngineSettings(),
            integrator_settings=IntegratorSettings(),
            solvent_simulation_settings=SimulationSettings(
                equilibration_length=1.0 * unit.nanosecond,
                production_length=10.0 * unit.nanosecond,
                output_filename='solvent.nc',
                checkpoint_storage='solvent_checkpoint.nc',
            ),
            vacuum_simulation_settings=SimulationSettings(
                equilibration_length=0.5 * unit.nanosecond,
                production_length=2.0 * unit.nanosecond,
                output_filename='vacuum.nc',
                checkpoint_storage='vacuum_checkpoint.nc'
            ),
        )

    @staticmethod
    def _validate_solvent_endstates(
        stateA: ChemicalSystem, stateB: ChemicalSystem,
    ) -> None:
        """
        A solvent transformation is defined (in terms of gufe components)
        as starting from a ligand in solvent and ending up just in solvent.

        Parameters
        ----------
        stateA : ChemicalSystem
          The chemical system of end state A
        stateB : ChemicalSystem
          The chemical system of end state B

        Raises
        ------
        ValueError
          If stateB contains anything else but a SolventComponent.
          If stateA contains a ProteinComponent
        """
        if ((len(stateB) != 1) or
           (not isinstance(stateB.values()[0], SolventComponent))):
            errmsg = "Only a single SolventComponent is allowed in stateB"
            raise ValueError(errmsg)

        for comp in stateA.values():
            if isinstance(comp, ProteinComponent):
                errmsg = ("Protein components are not allow for "
                          "absolute solvation free energies")
                raise ValueError(errmsg)

    @staticmethod
    def _validate_alchemical_components(
        alchemical_components: dict[str, list[Component]]
    ) -> None:
        """
        Checks that the ChemicalSystem alchemical components are correct.

        Parameters
        ----------
        alchemical_components : Dict[str, list[Component]]
          Dictionary containing the alchemical components for
          stateA and stateB.

        Raises
        ------
        ValueError
          If there are alchemical components in state B.
          If there are non SmallMoleculeComponent alchemical species.
          If there are more than one alchemical species.

        Notes
        -----
        * Currently doesn't support alchemical components in state B.
        * Currently doesn't support alchemical components which are not
          SmallMoleculeComponents.
        * Currently doesn't support more than one alchemical component
          being desolvated.
        """

        # Crash out if there are any alchemical components in state B for now
        if len(alchemical_components['stateB']) > 0:
            errmsg = ("Components appearing in state B are not "
                      "currently supported")
            raise ValueError(errmsg)

        if len(alchemical_components['stateA']) > 1:
            errmsg = ("More than one alchemical components is not supported "
                      "for absolute solvation free energies")

        # Crash out if any of the alchemical components are not
        # SmallMoleculeComponent
        for comp in alchemical_components['stateA']:
            if not isinstance(comp, SmallMoleculeComponent):
                errmsg = ("Non SmallMoleculeComponent alchemical species "
                          "are not currently supported")
                raise ValueError(errmsg)

    def _create(
        self,
        stateA: ChemicalSystem,
        stateB: ChemicalSystem,
        mapping: Optional[Dict[str, gufe.ComponentMapping]] = None,
        extends: Optional[gufe.ProtocolDAGResult] = None,
    ) -> list[gufe.ProtocolUnit]:
        # TODO: extensions
        if extends:  # pragma: no-cover
            raise NotImplementedError("Can't extend simulations yet")

        # Validate components and get alchemical components
        self._validate_solvation_endstates(stateA, stateB)
        alchem_comps = system_validation.get_alchemical_components(
            stateA, stateB,
        )
        self._validate_alchemical_components(alchem_comps)

        # Check nonbond & solvent compatibility
        solv_nonbonded_method = self.settings.solvent_system_settings.nonbonded_method
        vac_nonbonded_method = self.settings.vacuum_system_settings.nonbonded_method
        # Use the more complete system validation solvent checks
        system_validation.validate_solvent(stateA, solv_nonbonded_method)
        # Gas phase is always gas phase
        assert vac_nonbonded_method.lower() != 'pme'

        # Get the name of the alchemical species
        alchname = alchem_comps['stateA'][0].name

        # Create list units for vacuum and solvent transforms

        solvent_units = [
            AbsoluteSolventTransformUnit(
                stateA=stateA, stateB=stateB,
                settings=self.settings,
                alchemical_components=alchem_comps,
                generation=0, repeat_id=i,
                name=(f"Absolute Solvation, {alchname} solvent leg: "
                      f"repeat {i} generation 0"),
            )
            for i in range(self.settings.alchemsampler_settings.n_repeats)
        ]

        vacuum_units = [
            AbsoluteVacuumTransformUnit(
                # These don't really reflect the actual transform
                # Should these be overriden to be ChemicalSystem{smc} -> ChemicalSystem{} ?
                stateA=stateA, stateB=stateB,
                settings=self.settings,
                alchemical_components=alchem_comps,
                generation=0, repeat_id=i,
                name=(f"Absolute Solvation, {alchname} solvent leg: "
                      f"repeat {i} generation 0"),
            )
            for i in range(self.settings.alchemsampler_settings.n_repeats)
        ]

        return solvent_units + vacuum_units

    # TODO: update to match new unit list
    def _gather(
        self, protocol_dag_results: Iterable[gufe.ProtocolDAGResult]
    ) -> Dict[str, Any]:
        # result units will have a repeat_id and generation
        # first group according to repeat_id
        repeats = defaultdict(list)
        for d in protocol_dag_results:
            pu: gufe.ProtocolUnitResult
            for pu in d.protocol_unit_results:
                if not pu.ok():
                    continue
                rep = pu.outputs['repeat_id']
                gen = pu.outputs['generation']

                repeats[rep].append((
                    gen, pu.outputs['nc'],
                    pu.outputs['last_checkpoint']))

        data = []
        for rep_id, rep_data in sorted(repeats.items()):
            # then sort within a repeat according to generation
            nc_paths = [
                ncpath for gen, ncpath, nc_check in sorted(rep_data)
            ]
            chk_files = [
                nc_check for gen, ncpath, nc_check in sorted(rep_data)
            ]
            data.append({'nc_paths': nc_paths,
                         'checkpoint_paths': chk_files})

        return {
            'nc_files': data,
        }


class BaseAbsoluteTransformUnit(gufe.ProtocolUnit):
    """
    Base class for ligand absolute free energy transformations.
    """
    def __init__(self, *,
                 stateA: ChemicalSystem,
                 stateB: ChemicalSystem,
                 settings: settings.Settings,
                 alchemical_components: Dict[str, List[str]],
                 generation: int = 0,
                 repeat_id: int = 0,
                 name: Optional[str] = None,):
        """
        Parameters
        ----------
        stateA : ChemicalSystem
          ChemicalSystem containing the components defining the state at
          lambda 0.
        stateB : ChemicalSystem
          ChemicalSystem containing the components defining the state at
          lambda 1.
        settings : gufe.settings.Setings
          Settings for the Absolute Tranformation Protocol. This can be
          constructed by calling the
          :class:`AbsoluteTransformProtocol.get_default_settings` method
          to get a default set of settings.
        name : str, optional
          Human-readable identifier for this Unit
        repeat_id : int, optional
          Identifier for which repeat (aka replica/clone) this Unit is,
          default 0
        generation : int, optional
          Generation counter which keeps track of how many times this repeat
          has been extended, default 0.
        """
        super().__init__(
            name=name,
            stateA=stateA,
            stateB=stateB,
            settings=settings,
            alchemical_components=alchemical_components,
            repeat_id=repeat_id,
            generation=generation,
        )

    @staticmethod
    def _get_alchemical_indices(omm_top: openmm.Topology,
                                comp_resids: Dict[str, npt.NDArray],
                                alchem_comps: Dict[str, List[Component]]
                                ) -> List[int]:
        """
        Get a list of atom indices for all the alchemical species

        Parameters
        ----------
        omm_top : openmm.Topology
          Topology of OpenMM System.
        comp_resids : Dict[str, npt.NDArray]
          A dictionary of residues for each component in the System.
        alchem_comps : Dict[str, List[Component]]
          A dictionary of alchemical components for each end state.

        Return
        ------
        atom_ids : List[int]
          A list of atom indices for the alchemical species
        """

        # concatenate a list of residue indexes for all alchemical components
        residxs = np.concatenate(
            [comp_resids[key] for key in alchem_comps['stateA']]
        )

        # get the alchemicical residues from the topology
        alchres = [
            r for r in omm_top.residues() if r.index in residxs
        ]

        atom_ids = []

        for res in alchres:
            atom_ids.extend([at.index for at in res.atoms()])

        return atom_ids

    @staticmethod
    def _pre_minimize(system: openmm.System,
                      positions: omm_unit.Quantity) -> npt.NDArray:
        """
        Short CPU minization of System to avoid GPU NaNs

        Parameters
        ----------
        system : openmm.System
          An OpenMM System to minimize.
        positionns : openmm.unit.Quantity
          Initial positions for the system.

        Returns
        -------
        minimized_positions : npt.NDArray
          Minimized positions
        """
        integrator = openmm.VerletIntegrator(0.001)
        context = openmm.Context(
                system, integrator,
                openmm.Platform.getPlatformByName('CPU'),
        )
        context.setPositions(positions)
        # Do a quick 100 steps minimization, usually avoids NaNs
        openmm.LocalEnergyMinimizer.minimize(
                context, maxIterations=100
        )
        state = context.getState(getPositions=True)
        minimized_positions = state.getPositions(asNumpy=True)
        return minimized_positions

    def _prepare(
        self, verbose: bool,
        scratch_basepath: Optional[pathlib.Path],
        shared_basepath: Optional[pathlib.Path],
    ):
        """
        Set basepaths and do some initial logging.

        Parameters
        ----------
        verbose : bool
          Verbose output of the simulation progress. Output is provided via
          INFO level logging.
        basepath : Optional[pathlib.Path]
          Optional base path to write files to.
        """
        self.verbose = verbose

        if self.verbose:
            self.logger.info("setting up alchemical system")

        # set basepaths
        def _set_optional_path(basepath):
            if basepath is None:
                return pathlib.Path('.')
            return basepath

        self.scratch_basepath = _set_optional_path(scratch_basepath)
        self.shared_basepath = _set_optional_path(shared_basepath)

    def _get_components(self):
        """
        Get the relevant components to create the alchemical system with.

        Note
        ----
        Must be implemented in child class.

        To move:
        stateA = self._inputs['stateA']
        alchem_comps = self._inputs['alchemical_components']
        # Get the relevant solvent & protein components & openff molecules
        solvent_comp, protein_comp, off_mols = self._parse_components(stateA)
        """
        raise NotImplementedError

    def _handle_settings(self):
        """
        Get a dictionary with the following entries:
          * forcefield_settings : OpenMMSystemGeneratorFFSettings
          * thermo_settings : ThermoSettings
          * system_settings : SystemSettings
          * solvation_settings : SolvationSettings
          * alchemical_settings : AlchemicalSettings
          * sampler_settings : AlchemicalSamplerSettings
          * engine_settings : OpenMMEngineSettings
          * integrator_settings : IntegratorSettings
          * simulation_settings : SimulationSettings

        Settings may change depending on what type of simulation you are
        running. Cherry pick them and return them to be available later on.

        This method should also add various validation checks as necessary.

        # a. Validation checks
        settings_validation.validate_timestep(
            settings.forcefield_settings.hydrogen_mass,
            settings.integrator_settings.timestep
        )
        """
        raise NotImplementedError

    def _get_system_generator(
        self, settings: dict[str, SettingsBaseModel],
        solvent_comp: Optional[SolventComponent]
    ) -> SystemGenerator:
        """
        Get a system generator through the system creation
        utilities

        Parameters
        ----------
        settings : dict[str, SettingsBaseModel]
          A dictionary of settings object for the unit.
        solvent_comp : Optional[SolventComponent]
          The solvent component of this system, if there is one.

        Returns
        -------
        system_generator : openmmforcefields.generator.SystemGenerator
          System Generator to parameterise this unit.
        """
        ffcache = settings['simulation_settings'].forcefield_cache
        if ffcache is not None:
            ffcache = self.shared_basepath / ffcache

        system_generator = system_creation.get_system_generator(
            forcefield_settings=settings['forcefield_settings'],
            thermo_settings=settings['thermo_settings'],
            cache=ffcache,
            has_solvent=solvent_comp is not None,
        )
        return system_generator

    def _get_modeller(
        self,
        protein_component: Optional[ProteinComponent],
        solvent_component: Optional[SolventComponent],
        smc_components: list[SmallMoleculeComponent],
        system_generator: SystemGenerator,
        solvation_settings: SolvationSettings
    ) -> tuple[app.Modeller, dict[Component, npt.NDArray]]:
        """
        Get an OpenMM Modeller object and a list of residue indices
        for each component in the system.

        Parameters
        ----------
        protein_component : Optional[ProteinComponent]
          Protein Component, if it exists.
        solvent_component : Optional[ProteinCompoinent]
          Solvent Component, if it exists.
        smc_components : list[SmallMoleculeComponents]
          List of SmallMoleculeComponents to add.
        system_generator : openmmforcefields.generator.SystemGenerator
          System Generator to parameterise this unit.
        solvation_settings : SolvationSettings
          Settings detailing how to solvate the system.

        Returns
        -------
        system_modeller : app.Modeller
          OpenMM Modeller object generated from ProteinComponent and
          OpenFF Molecules.
        comp_resids : dict[Component, npt.NDArray]
          Dictionary of residue indices for each component in system.
        """
        if self.verbose:
            self.logger.info("Parameterizing molecules")

        # force the creation of parameters for the small molecules
        # this is necessary because we need to have the FF generated ahead
        # of solvating the system.
        # Note by default this is cached to ctx.shared/db.json which should
        # reduce some of the costs.
        for comp in smc_components:
            offmol = comp.to_openff()
            system_generator.create_system(
                offmol.to_topology().to_openmm(), molecules=[offmol]
            )

        # get OpenMM modeller + dictionary of resids for each component
        system_modeller, comp_resids = system_creation.get_omm_modeller(
            protein_comp=protein_component,
            solvent_comp=solvent_component,
            small_mols=smc_components,
            omm_forcefield=system_generator.forcefield,
            solvent_settings=solvation_settings,
        )

        return system_modeller, comp_resids

    def _get_omm_objects(
        self,
        system_modeller: app.Modeller,
        system_generator: SystemGenerator,
        smc_components: list[SmallMoleculeComponent],
    ) -> tuple[app.Topology, openmm.unit.Quantity, openmm.System]:
        """
        Get the OpenMM Topology, Positions and System of the
        parameterised system.

        Parameters
        ----------
        system_modeller : app.Modeller
          OpenMM Modeller object representing the system to be
          parametrized.
        system_generator : SystemGenerator
          SystemGenerator object to create a System with.
        smc_components : list[SmallMoleculeComponent]
          A list of SmallMoleculeComponents to add to the system.

        Returns
        -------
        topology : app.Topology
          Topology object describing the parameterized system
        positionns : openmm.unit.Quantity
          Positions of the system.
        system : openmm.System
          An OpenMM System of the alchemical system.
        """
        topology = system_modeller.getTopology()
        # roundtrip positions to remove vec3 issues
        positions = to_openmm(from_openmm(system_modeller.getPositions()))
        system = system_generator.create_system(
            system_modeller.topology,
            molecules=[s.to_openff() for s in smc_components]
        )
        return topology, positions, system

    def _get_lambda_schedule(
        self, settings: dict[str, SettingsBaseModel]
    ) -> dict[str, npt.NDArray]:
        """
        Create the lambda schedule

        Parameters
        ----------
        settings : dict[str, SettingsBaseModel]
          Settings for the unit.

        Returns
        -------
        lambdas : dict[str, npt.NDArray]

        TODO
        ----
        * Augment this by using something akin to the RFE protocol's
          LambdaProtocol
        """
        lambdas = dict()
        n_elec = settings['alchemical_settings'].lambda_elec_windows
        n_vdw = settings['alchemical_settings'].lambda_vdw_windows + 1
        lambdas['lambda_electrostatics'] = np.concatenate(
                [np.linspace(1, 0, n_elec), np.linspace(0, 0, n_vdw)[1:]]
        )
        lambdas['lambda_sterics'] = np.concatenate(
                [np.linspace(1, 1, n_elec), np.linspace(1, 0, n_vdw)[1:]]
        )

        n_replicas = settings['sampler_settings'].n_replicas

        if n_replicas != (len(lambdas['lambda_sterics'])):
            errmsg = (f"Number of replicas {n_replicas} "
                      "does not equal the number of lambda windows ")
            raise ValueError(errmsg)

        return lambdas

    def _add_restraints(self, system, topology, settings):
        """
        Placeholder method to add restraints if necessary
        """
        return

    def _get_alchemical_system(
        self,
        topology: app.Topology,
        system: openmm.System,
        comp_resids: dict[Component, npt.NDArray],
        alchem_comps: dict[str, list[Component]]
    ) -> tuple[AbsoluteAlchemicalFactory, openmm.System, list[int]]:
        """
        Get an alchemically modified system and its associated factory

        Parameters
        ----------
        topology : openmm.Topology
          Topology of OpenMM System.
        system : openmm.System
          System to alchemically modify.
        comp_resids : dict[str, npt.NDArray]
          A dictionary of residues for each component in the System.
        alchem_comps : dict[str, list[Component]]
          A dictionary of alchemical components for each end state.


        Returns
        -------
        alchemical_factory : AbsoluteAlchemicalFactory
          Factory for creating an alchemically modified system.
        alchemical_system : openmm.System
          Alchemically modified system
        alchemical_indices : list[int]
          A list of atom indices for the alchemically modified
          species in the system.

        TODO
        ----
        * Add support for all alchemical factory options
        """
        alchemical_indices = self._get_alchemical_indices(
            topology, comp_resids, alchem_comps
        )

        alchemical_region = AlchemicalRegion(
            alchemical_atoms=alchemical_indices,
        )

        alchemical_factory = AbsoluteAlchemicalFactory()
        alchemical_system = alchemical_factory.create_alchemical_system(
            system, alchemical_region
        )

        return alchemical_factory, alchemical_system, alchemical_indices

    def _get_states(
        self,
        alchemical_system: openmm.System,
        positions: openmm.unit.Quantity,
        settings: dict[str, SettingsBaseModel],
        lambdas: dict[str, npt.NDArray],
        solvent_comp: Optional[SolventComponent],
    ) -> tuple[list[SamplerState], list[ThermodynamicState]]:
        """
        Get a list of sampler and thermodynmic states from an
        input alchemical system.

        Parameters
        ----------
        alchemical_system : openmm.System
          Alchemical system to get states for.
        positions : openmm.unit.Quantity
          Positions of the alchemical system.
        settings : dict[str, SettingsBaseModel]
          A dictionary of settings for the protocol unit.
        lambdas : dict[str, npt.NDArray]
          A dictionary of lambda scales.
        solvent_comp : Optional[SolventComponent]
          The solvent component of the system, if there is one.

        Returns
        -------
        sampler_states : list[SamplerState]
          A list of SamplerStates for each replica in the system.
        cmp_states : list[ThermodynamicState]
          A list of ThermodynamicState for each replica in the system.
        """
        alchemical_state = AlchemicalState.from_system(alchemical_system)
        # Set up the system constants
        temperature = settings.thermo_settings.temperature
        pressure = settings.thermo_settings.pressure
        constants = dict()
        constants['temperature'] = ensure_quantity(temperature, 'openmm')
        if solvent_comp is not None:
            constants['pressure'] = ensure_quantity(pressure, 'openmm')

        cmp_states = create_thermodynamic_state_protocol(
            alchemical_system, protocol=lambdas,
            consatnts=constants, composable_states=[alchemical_state],
        )

        sampler_state = SamplerState(positions=positions)
        if alchemical_system.usesPeriodicBoundaryConditions():
            box = alchemical_system.getDefaultPeriodicBoxVectors()
            sampler_state.box_vectors = box

        sampler_states = [sampler_state for _ in cmp_states]

        return sampler_states, cmp_states

    def _get_reporter(
        self,
        topology: app.Topology,
        simulation_settings: SimulationSettings,
    ) -> multistate.MultiStateReporter:
        """
        Get a MultistateReporter for the simulation you are running.

        Parameters
        ----------
        topology : app.Topology
          A Topology of the system being created.
        simulation_settings : SimulationSettings
          Settings for the simulation.

        Returns
        -------
        reporter : multistate.MultiStateReporter
          The reporter for the simulation.
        """
        mdt_top = mdt.Topology.from_openmm(topology)

        selection_indices = mdt_top.select(
                simulation_settings.output_indices
        )

        nc = self.shared_basepath / simulation_settings.output_filename
        chk = self.shared_basepath / simulation_settings.checkpoint_storage

        reporter = multistate.MultiStateReporter(
            storage=nc,
            analysis_particle_indices=selection_indices,
            checkpoint_interval=simulation_settings.checkpoint_interval.m,
            checkpoint_storage=chk,
        )

        return reporter

    def _get_ctx_caches(
        self,
        engine_settings: OpenMMEngineSettings
    ) -> tuple[openmmtools.cache.ContextCache, openmmtools.cache.ContextCache]:
        """
        Set the context caches based on the chosen platform

        Parameters
        ----------
        engine_settings : OpenMMEngineSettings,

        Returns
        -------
        energy_context_cache : openmmtools.cache.ContextCache
          The energy state context cache.
        sampler_context_cache : openmmtools.cache.ContextCache
          The sampler state context cache.
        """
        platform = compute.get_openmm_platform(
            engine_settings.compute_platform,
        )

        energy_context_cache = openmmtools.cache.ContextCache(
            capacity=None, time_to_line=None, platform=platform,
        )

        sampler_context_cache = openmmtools.cache.ContextCache(
            capacity=None, time_to_line=None, platform=platform,
        )

        return energy_context_cache, sampler_context_cache

    def _get_integrator(
        self,
        integrator_settings: IntegratorSettings
    ) -> openmmtools.mcmc.LangevinDynamicsMove:
        """
        Return a LangevinDynamicsMove integrator

        Parameters
        ----------
        integrator_settings : IntegratorSettings

        Returns
        -------
        integrator : openmmtools.mcmc.LangevinDynamicsMove
          A configured integrator object.
        """
        integrator = openmmtools.mcmc.LangevinDynamicsMove(
            timestep=to_openmm(integrator_settings.timestep),
            collision_rate=to_openmm(integrator_settings.collision_rate),
            n_steps=integrator_settings.n_steps.m,
            reassign_velocities=integrator_settings.reassign_velocities,
            n_restart_attempts=integrator_settings.n_restart_attempts,
            constraint_tolerance=integrator_settings.constraint_tolerance,
        )

        return integrator

    def _get_sampler(
        self,
        integrator: openmmtools.mcmc.LangevinDynamicsMove,
        reporter: openmmtools.multistate.MultiStateReporter,
        sampler_settings: AlchemicalSamplerSettings,
        cmp_states: list[ThermodynamicState],
        sampler_states: list[SamplerState],
        energy_context_cache: openmmtools.cache.ContextCache,
        sampler_context_cache: openmmtools.cache.ContextCache
    ) -> multistate.MultiStateSampler:
        """
        Get a sampler based on the equilibrium sampling method requested.

        Parameters
        ----------
        integrator : openmmtools.mcmc.LangevinDynamicsMove
          The simulation integrator.
        reporter : openmmtools.multistate.MultiStateReporter
          The reporter to hook up to the sampler.
        sampler_settings : AlchemicalSamplerSettings
          Settings for the alchemical sampler.
        cmp_states : list[ThermodynamicState]
          A list of thermodynamic states to sample.
        sampler_states : list[SamplerState]
          A list of sampler states.
        energy_context_cache : openmmtools.cache.ContextCache
          Context cache for the energy states.
        sampler_context_cache : openmmtool.cache.ContextCache
          Context cache for the sampler states.

        Returns
        -------
        sampler : multistate.MultistateSampler
          A sampler configured for the chosen sampling method.
        """

        # Select the right sampler
        # Note: doesn't need else, settings already validates choices
        if sampler_settings.sampler_method.lower() == "repex":
            sampler = multistate.ReplicaExchangeSampler(
                mcmc_moves=integrator,
                online_analysis_interval=sampler_settings.online_analysis_interval,
                online_analysis_target_error=sampler_settings.online_analysis_target_error.m,
                online_analysis_minimum_iterations=sampler_settings.online_analysis_minimum_iterations
            )
        elif sampler_settings.sampler_method.lower() == "sams":
            sampler = multistate.SAMSSampler(
                mcmc_moves=integrator,
                online_analysis_interval=sampler_settings.online_analysis_interval,
                online_analysis_minimum_iterations=sampler_settings.online_analysis_minimum_iterations,
                flatness_criteria=sampler_settings.flatness_criteria,
                gamma0=sampler_settings.gamma0,
            )
        elif sampler_settings.sampler_method.lower() == 'independent':
            sampler = multistate.MultiStateSampler(
                mcmc_moves=integrator,
                online_analysis_interval=sampler_settings.online_analysis_interval,
                online_analysis_target_error=sampler_settings.online_analysis_target_error.m,
                online_analysis_minimum_iterations=sampler_settings.online_analysis_minimum_iterations
            )

        sampler.create(
            thermodynamic_states=cmp_states,
            sampler_states=sampler_states,
            storage=reporter
        )

        sampler.energy_context_cache = energy_context_cache
        sampler.sampler_context_cache = sampler_context_cache

        return sampler

    def _run_simulation(
        self,
        sampler: multistate.MultiStateSampler,
        reporter: multistate.MultiStateReporter,
        settings: dict[str, SettingsBaseModel],
        dry: bool
    ):
        """
        Run the simulation.

        Parameters
        ----------
        sampler : multistate.MultiStateSampler
          The sampler associated with the simulation to run.
        reporter : multistate.MultiStateReporter
          The reporter associated with the sampler.
        settings : dict[str, SettingsBaseModel]
          The dictionary of settings for the protocol.
        dry : bool
          Whether or not to dry run the simulation
        """
        # Get the relevant simulation steps
        mc_steps = settings['integrator_settings'].n_steps.m

        equil_steps, prod_steps = settings_validation.get_simsteps(
            equil_length=settings['simulation_settings'].equilibration_length,
            prod_length=settings['simulation_settings'].production_length,
            timestep=settings['integrator_settings'].timestep,
            mc_steps=mc_steps,
        )

        if not dry:  # pragma: no-cover
            # minimize
            if self.verbose:
                self.logger.info("minimizing systems")

            sampler.minimize(
                max_iterations=settings['sim_settings'].minimization_steps
            )

            # equilibrate
            if self.verbose:
                self.logger.info("equilibrating systems")

            sampler.equilibrate(int(equil_steps / mc_steps))  # type: ignore

            # production
            if self.verbose:
                self.logger.info("running production phase")

            sampler.extend(int(prod_steps / mc_steps))  # type: ignore

            # close reporter when you're done
            reporter.close()

            nc = self.shared_basepath / settings['simulation_settings'].output_filename
            chk = self.shared_basepath / settings['simulation_settings'].checkpoint_storage
            return {
                'nc': nc,
                'last_checkpoint': chk,
            }
        else:
            # close reporter when you're done, prevent file handle clashes
            reporter.close()

            # clean up the reporter file
            fns = [self.shared_basepath / settings['simulation_settings'].output_filename,
                   self.shared_basepath / settings['simulation_settings'].checkpoint_storage]
            for fn in fns:
                os.remove(fn)
            return {'debug': {'sampler': sampler}}

    def run(self, dry=False, verbose=True, basepath=None) -> Dict[str, Any]:
        """Run the absolute free energy calculation.

        Parameters
        ----------
        dry : bool
          Do a dry run of the calculation, creating all necessary alchemical
          system components (topology, system, sampler, etc...) but without
          running the simulation.
        verbose : bool
          Verbose output of the simulation progress. Output is provided via
          INFO level logging.
        basepath : Pathlike, optional
          Where to run the calculation, defaults to current working directory

        Returns
        -------
        dict
          Outputs created in the basepath directory or the debug objects
          (i.e. sampler) if ``dry==True``.

        Attributes
        ----------
        solvent : Optional[SolventComponent]
          SolventComponent to be applied to the system
        protein : Optional[ProteinComponent]
          ProteinComponent for the system
        openff_mols : List[openff.Molecule]
          List of OpenFF Molecule objects for each SmallMoleculeComponent in
          the stateA ChemicalSystem
        """
        # 0. Generaly preparation tasks
        self._prepare(verbose, basepath)

        # 1. Get components
        alchem_comps, solv_comp, prot_comp, smc_comps = self._get_components()

        # 2. Get settings
        settings = self._handle_settings()

        # 3. Get system generator
        system_generator = self._get_system_generator(settings, solv_comp)

        # 4. Get modeller
        system_modeller, comp_resids = self._get_modeller(
            prot_comp, solv_comp, smc_comps, system_generator,
            settings['solvation_settings']
        )

        # 5. Get OpenMM topology, positions and system
        omm_topology, omm_system, positions = self._get_omm_objects(
            system_generator, system_modeller, smc_comps
        )

        # 6. Pre-minimize System (Test + Avoid NaNs)
        positions = self._pre_minimize(omm_system, positions)

        # 7. Get lambdas
        lambdas = self._get_lambda_schedule(settings)

        # 8. Add restraints
        self._add_restraints(omm_system, omm_topology, settings)

        # 9. Get alchemical system
        alchem_system, alchem_factory = self._get_alchemical_system(
            omm_topology, omm_system, comp_resids, alchem_comps
        )

        # 10. Get compound and sampler states
        cmp_states, sampler_states = self._get_states(
            alchem_system, positions, settings,
            lambdas, solv_comp
        )

        # 11. Create the multistate reporter & create PDB
        reporter = self._get_reporter(
            omm_topology, settings['simulation_setttings'],
        )

        # Wrap in try/finally to avoid memory leak issues
        try:
            # 12. Get context caches
            energy_ctx_cache, sampler_ctx_cache = self._get_ctx_caches(
                    settings['engine_settings']
            )

            # 13. Get integrator
            integrator = self._get_integrator(settings['integrator_settings'])

            # 14. Get sampler
            sampler = self._get_sampler(
                integrator, reporter, settings['sampler_settings'],
                cmp_states, sampler_states,
                energy_ctx_cache, sampler_ctx_cache
            )

            # 15. Run simulation
            self._run_simulation(
                sampler, reporter, settings, dry
            )
        finally:
            # close reporter when you're done to prevent file handle clashes
            reporter.close()

            # clear GPU context
            # Note: use cache.empty() when openmmtools #690 is resolved
            for context in list(energy_ctx_cache._lru._data.keys()):
                del energy_ctx_cache._lru._data[context]
            for context in list(sampler_ctx_cache._lru._data.keys()):
                del sampler_ctx_cache._lru._data[context]
            # cautiously clear out the global context cache too
            for context in list(
                    openmmtools.cache.global_context_cache._lru._data.keys()):
                del openmmtools.cache.global_context_cache._lru._data[context]

            del sampler_ctx_cache, energy_ctx_cache

            # Keep these around in a dry run so we can inspect things
            if not dry:
                del integrator, sampler

    def _execute(
        self, ctx: gufe.Context, **kwargs,
    ) -> Dict[str, Any]:
        # create directory for *this* unit within the context of the *DAG*
        # stops output files mashing into each other within a DAG
        myid = uuid.uuid4()
        mypath = pathlib.Path(os.path.join(ctx.shared, str(myid)))
        mypath.mkdir(parents=True, exist_ok=False)

        outputs = self.run(basepath=mypath)

        return {
            'repeat_id': self._inputs['repeat_id'],
            'generation': self._inputs['generation'],
            **outputs
        }
