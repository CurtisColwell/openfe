# This code is part of OpenFE and is licensed under the MIT license.
# For details, see https://github.com/OpenFreeEnergy/openfe
"""OpenMM Equilibrium Solvation AFE Protocol --- :mod:`openfe.protocols.openmm_afe.equil_solvation_afe_method`
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
  `espaloma_charge <https://github.com/choderalab/espaloma_charge>`_

"""
from __future__ import annotations

import pathlib
import logging
import warnings
from collections import defaultdict
import gufe
from gufe.components import Component
import itertools
import numpy as np
import numpy.typing as npt
from openff.units import unit
from openmmtools import multistate
from openmmtools.state import ThermodynamicState, GlobalParameterState
from typing import Optional, Union
from typing import Any, Iterable
import uuid

from gufe import (
    settings,
    ChemicalSystem, SmallMoleculeComponent,
    ProteinComponent, SolventComponent
)
from openfe.protocols.openmm_afe.equil_afe_settings import (
    AbsoluteSolvationSettings,
    OpenMMSolvationSettings, AlchemicalSettings, LambdaSettings,
    MDSimulationSettings, MDOutputSettings,
    MultiStateSimulationSettings, OpenMMEngineSettings,
    IntegratorSettings, MultiStateOutputSettings,
    OpenFFPartialChargeSettings,
    SettingsBaseModel,
    HarmonicRestraintSettings,
    FlatBottomRestraintSettings,
    BoreschRestraintSettings,
)
from ..openmm_utils import system_validation, settings_validation
from .base import BaseAbsoluteUnit
from openfe.utils import log_system_probe
from openfe.due import due, Doi


due.cite(Doi("10.5281/zenodo.596504"),
         description="Yank",
         path="openfe.protocols.openmm_afe.equil_binding_afe_method",
         cite_module=True)

due.cite(Doi("10.5281/zenodo.596622"),
         description="OpenMMTools",
         path="openfe.protocols.openmm_afe.equil_binding_afe_method",
         cite_module=True)

due.cite(Doi("10.1371/journal.pcbi.1005659"),
         description="OpenMM",
         path="openfe.protocols.openmm_afe.equil_binding_afe_method",
         cite_module=True)


logger = logging.getLogger(__name__)


class AbsoluteBindingProtocolResult(gufe.ProtocolResult):
    """Dict-like container for the output of a AbsoluteBindingProtocol
    """
    def __init__(self, **data):
        super().__init__(**data)
        # TODO: Detect when we have extensions and stitch these together?
        if any(len(pur_list) > 2 for pur_list
               in itertools.chain(self.data['solvent'].values(), self.data['vacuum'].values())):
            raise NotImplementedError("Can't stitch together results yet")

    def get_individual_estimates(self) -> dict[str, list[tuple[unit.Quantity, unit.Quantity]]]:
        """
        Get the individual estimate of the free energies.

        Returns
        -------
        dGs : dict[str, list[tuple[unit.Quantity, unit.Quantity]]]
          A dictionary, keyed `solvent`, `complex`, and 'standard_state'
          representing each portion of the thermodynamic cycle,
          with lists of tuples containing the individual free energy
          estimates and, for 'solvent' and 'complex', the associated MBAR
          uncertainties for each repeat of that simulation type.

        TODO
        ----
        * Work out poperly what to do with the standard state correction.
        """
        complex_dGs = []
        correction_dGs = []
        solv_dGs = []

        for pus in self.data['complex'].values():
            complex_dGs.append((
                pus[0].outputs['unit_estimate'],
                pus[0].outputs['unit_estimate_error']
            ))
            correction_dGs.append((
                pus[0].outputs['standard_state_correction']
            ))

        for pus in self.data['solvent'].values():
            solv_dGs.append((
                pus[0].outputs['unit_estimate'],
                pus[0].outputs['unit_estimate_error']
            ))

        return {'solvent': solv_dGs, 'complex': complex_dGs, 'standard_state': correction_dGs}

    def get_estimate(self):
        """Get the binding free energy estimate for this calculation.

        Returns
        -------
        dG : unit.Quantity
          The binding free energy. This is a Quantity defined with units.
        """
        def _get_average(estimates):
            # Get the unit value of the first value in the estimates
            u = estimates[0][0].u
            # Loop through estimates and get the free energy values
            # in the unit of the first estimate
            dGs = [i[0].to(u).m for i in estimates]

            return np.average(dGs) * u

        individual_estimates = self.get_individual_estimates()
        complex_dG = _get_average(individual_estimates['complex'])
        solv_dG = _get_average(individual_estimates['solvent'])
        standard_state_dG = _get_average(
            individual_estimates['standard_state']
        )

        return - complex_dG + solv_dG + standard_state_dG

    def get_uncertainty(self):
        """Get the binding free energy error for this calculation.

        Returns
        -------
        err : unit.Quantity
          The standard deviation between estimates of the binding free
          energy. This is a Quantity defined with units.
        """
        def _get_stdev(estimates):
            # Get the unit value of the first value in the estimates
            u = estimates[0][0].u
            # Loop through estimates and get the free energy values
            # in the unit of the first estimate
            dGs = [i[0].to(u).m for i in estimates]

            return np.std(dGs) * u

        individual_estimates = self.get_individual_estimates()
        complex_err = _get_stdev(individual_estimates['complex'])
        solv_err = _get_stdev(individual_estimates['solvent'])
        standard_state_err = _get_stdev(individual_estimates['standard_state'])

        # return the combined error
        return np.sqrt(complex_err**2 + solv_err**2 + standard_state_err**2)

    def get_forward_and_reverse_energy_analysis(self) -> dict[str, list[Optional[dict[str, Union[npt.NDArray, unit.Quantity]]]]]:
        """
        Get the reverse and forward analysis of the free energies.

        Returns
        -------
        forward_reverse : dict[str, list[Optional[dict[str, Union[npt.NDArray, unit.Quantity]]]]]
            A dictionary, keyed `solvent` and `complex` for each leg of the
            thermodynamic cycle which each contain a list of dictionaries
            containing the forward and reverse analysis of each repeat
            of that simulation type.

            The forward and reverse analysis dictionaries contain:
              - `fractions`: npt.NDArray
                  The fractions of data used for the estimates
              - `forward_DGs`, `reverse_DGs`: unit.Quantity
                  The forward and reverse estimates for each fraction of data
              - `forward_dDGs`, `reverse_dDGs`: unit.Quantity
                  The forward and reverse estimate uncertainty for each
                  fraction of data.

            If one of the cycle leg list entries is ``None``, this indicates
            that the analysis could not be carried out for that repeat. This
            is most likely caused by MBAR convergence issues when attempting to
            calculate free energies from too few samples.

        Raises
        ------
        UserWarning
          * If any of the forward and reverse dictionaries are ``None`` in a
            given thermodynamic cycle leg.
        """

        forward_reverse: dict[str, list[Optional[dict[str, Union[npt.NDArray, unit.Quantity]]]]] = {}

        for key in ['solvent', 'complex']:
            forward_reverse[key] = [
                pus[0].outputs['forward_and_reverse_energies']
                for pus in self.data[key].values()
            ]

            if None in forward_reverse[key]:
                wmsg = (
                    "One or more ``None`` entries were found in the forward "
                    f"and reverse dictionaries of the repeats of the {key} "
                    "calculations. This is likely caused by an MBAR convergence "
                    "failure caused by too few independent samples when "
                    "calculating the free energies of the 10% timeseries slice."
                )
                warnings.warn(wmsg)

        return forward_reverse

    def get_overlap_matrices(self) -> dict[str, list[dict[str, npt.NDArray]]]:
        """
        Get a the MBAR overlap estimates for all legs of the simulation.

        Returns
        -------
        overlap_stats : dict[str, list[dict[str, npt.NDArray]]]
          A dictionary with keys `solvent` and `complex` for each
          leg of the thermodynamic cycle, which each containing a
          list of dictionaries with the MBAR overlap estimates of
          each repeat of that simulation type.

          The underlying MBAR dictionaries contain the following keys:
            * ``scalar``: One minus the largest nontrivial eigenvalue
            * ``eigenvalues``: The sorted (descending) eigenvalues of the
              overlap matrix
            * ``matrix``: Estimated overlap matrix of observing a sample from
              state i in state j
        """
        # Loop through and get the repeats and get the matrices
        overlap_stats: dict[str, list[dict[str, npt.NDArray]]] = {}

        for key in ['solvent', 'complex']:
            overlap_stats[key] = [
                pus[0].outputs['unit_mbar_overlap']
                for pus in self.data[key].values()
            ]

        return overlap_stats

    def get_replica_transition_statistics(self) -> dict[str, list[dict[str, npt.NDArray]]]:
        """
        Get the replica exchange transition statistics for all
        legs of the simulation.

        Note
        ----
        This is currently only available in cases where a replica exchange
        simulation was run.

        Returns
        -------
        repex_stats : dict[str, list[dict[str, npt.NDArray]]]
          A dictionary with keys `solvent` and `complex` for each
          leg of the thermodynamic cycle, which each containing
          a list of dictionaries containing the replica transition
          statistics for each repeat of that simulation type.

          The replica transition statistics dictionaries contain the following:
            * ``eigenvalues``: The sorted (descending) eigenvalues of the
              lambda state transition matrix
            * ``matrix``: The transition matrix estimate of a replica switching
              from state i to state j.
        """
        repex_stats: dict[str, list[dict[str, npt.NDArray]]] = {}
        try:
            for key in ['solvent', 'complex']:
                repex_stats[key] = [
                    pus[0].outputs['replica_exchange_statistics']
                    for pus in self.data[key].values()
                ]
        except KeyError:
            errmsg = ("Replica exchange statistics were not found, "
                      "did you run a repex calculation?")
            raise ValueError(errmsg)

        return repex_stats

    def get_replica_states(self) -> dict[str, list[npt.NDArray]]:
        """
        Get the timeseries of replica states for all simulation legs.

        Returns
        -------
        replica_states : dict[str, list[npt.NDArray]]
          Dictionary keyed `solvent` and `complex` for each leg of
          the thermodynamic cycle, with lists of replica states
          timeseries for each repeat of that simulation type.
        """
        replica_states: dict[str, list[npt.NDArray]] = {
            'solvent': [], 'complex': []
        }

        def is_file(filename: str):
            p = pathlib.Path(filename)

            if not p.exists():
                errmsg = f"File could not be found {p}"
                raise ValueError(errmsg)

            return p

        def get_replica_state(nc, chk):
            nc = is_file(nc)
            dir_path = nc.parents[0]
            chk = is_file(dir_path / chk).name

            reporter = multistate.MultiStateReporter(
                storage=nc, checkpoint_storage=chk, open_mode='r'
            )

            retval = np.asarray(reporter.read_replica_thermodynamic_states())
            reporter.close()

            return retval

        for key in ['solvent', 'complex']:
            for pus in self.data[key].values():
                states = get_replica_state(
                    pus[0].outputs['nc'],
                    pus[0].outputs['last_checkpoint'],
                )
                replica_states[key].append(states)

        return replica_states

    def equilibration_iterations(self) -> dict[str, list[float]]:
        """
        Get the number of equilibration iterations for each simulation.

        Returns
        -------
        equilibration_lengths : dict[str, list[float]]
          Dictionary keyed `solvent` and `complex` for each leg
          of the thermodynamic cycle, with lists containing the
          number of equilibration iterations for each repeat
          of that simulation type.
        """
        equilibration_lengths: dict[str, list[float]] = {}

        for key in ['solvent', 'complex']:
            equilibration_lengths[key] = [
                pus[0].outputs['equilibration_iterations']
                for pus in self.data[key].values()
            ]

        return equilibration_lengths

    def production_iterations(self) -> dict[str, list[float]]:
        """
        Get the number of production iterations for each simulation.
        Returns the number of uncorrelated production samples for each
        repeat of the calculation.

        Returns
        -------
        production_lengths : dict[str, list[float]]
          Dictionary keyed `solvent` and `complex` for each leg of the
          thermodynamic cycle, with lists with the number
          of production iterations for each repeat of that simulation
          type.
        """
        production_lengths: dict[str, list[float]] = {}

        for key in ['solvent', 'complex']:
            production_lengths[key] = [
                pus[0].outputs['production_iterations']
                for pus in self.data[key].values()
            ]

        return production_lengths


class AbsoluteBindingProtocol(gufe.Protocol):
    """
    Absolute binding free energy calculations using OpenMM and OpenMMTools.

    See Also
    --------
    :mod:`openfe.protocols`
    :class:`openfe.protocols.openmm_afe.AbsoluteBindingSettings`
    :class:`openfe.protocols.openmm_afe.AbsoluteBindingProtocolResult`
    :class:`openfe.protocols.openmm_afe.AbsoluteBindingSolventUnit`
    :class:`openfe.protocols.openmm_afe.AbsoluteBindingComplexUnit`
    """
    result_cls = AbsoluteBindingProtocolResult
    _settings: AbsoluteBindingSettings

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
        return AbsoluteBindingSettings(
            protocol_repeats=3,
            forcefield_settings=settings.OpenMMSystemGeneratorFFSettings(),
            thermo_settings=settings.ThermoSettings(
                temperature=298.15 * unit.kelvin,
                pressure=1 * unit.bar,
            ),
            alchemical_settings=AlchemicalSettings(),
            solvent_lambda_settings=LambdaSettings(
                lambda_elec=[
                    0.0, 0.25, 0.5, 0.75, 1.0, 1.0, 1.0,
                    1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
                lambda_vdw=[
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.12, 0.24,
                    0.36, 0.48, 0.6, 0.7, 0.77, 0.85, 1.0],
                lambda_restraints=[
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            ),
            complex_lambda_settings=LambdaSettings(
                lambda_elec=[
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.1,
                    0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
                    1.0, 1.0, 1.0, 1.0, 1.0, 1.00, 1.0, 1.00, 1.0, 1.00, 1.0, 1.00, 1.0],
                lambda_vdw=[
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1,
                    0.2, 0.3, 0.4, 0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0],
                lambda_restraints=[
                    0.0, 0.2, 0.4, 0.6, 0.8, 1.0,
                    1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
                    1.0, 1.0, 1.0, 1.0, 1.0, 1.00, 1.0, 1.00, 1.0, 1.00, 1.0, 1.00, 1.0],
            ),
            partial_charge_settings=OpenFFPartialChargeSettings(),
            solvation_settings=OpenMMSolvationSettings(),
            engine_settings=OpenMMEngineSettings(),
            integrator_settings=IntegratorSettings(),
            restraint_settings=BoreschRestraintSettings(),
            solvent_equil_simulation_settings=MDSimulationSettings(
                equilibration_length_nvt=0.1 * unit.nanosecond,
                equilibration_length=0.2 * unit.nanosecond,
                production_length=0.5 * unit.nanosecond,
            ),
            solvent_equil_output_settings=MDOutputSettings(
                equil_nvt_structure='equil_nvt_structure.pdb',
                equil_npt_structure='equil_npt_structure.pdb',
                production_trajectory_filename='production_equil.xtc',
                log_output='equil_simulation.log',
            ),
            solvent_simulation_settings=MultiStateSimulationSettings(
                n_replicas=14,
                equilibration_length=1.0 * unit.nanosecond,
                production_length=10.0 * unit.nanosecond,
            ),
            solvent_output_settings=MultiStateOutputSettings(
                output_filename='solvent.nc',
                checkpoint_storage_filename='solvent_checkpoint.nc',
            ),
            complex_equil_simulation_settings=MDSimulationSettings(
                equilibration_length_nvt=0.25 * unit.nanosecond,
                equilibration_length=0.5 * unit.nanosecond,
                production_length=5.0 * unit.nanosecond,
            ),
            complex_equil_output_settings=MDOutputSettings(
                equil_nvt_structure='equil_nvt_structure.pdb',
                equil_npt_structure='equil_npt_structure.pdb',
                production_trajectory_filename='production_equil.xtc',
                log_output='equil_simulation.log',
            ),
            complex_simulation_settings=MultiStateSimulationSettings(
                n_replicas=28,
                equilibration_length=1 * unit.nanosecond,
                production_length=10.0 * unit.nanosecond,
            ),
            complex_output_settings=MultiStateOutputSettings(
                output_filename='complex.nc',
                checkpoint_storage_filename='complex_checkpoint.nc'
            ),
        )

    @staticmethod
    def _validate_endstates(
        stateA: ChemicalSystem, stateB: ChemicalSystem,
    ) -> None:
        """
        A binding transformation is defined (in terms of gufe components)
        as starting from one or more ligands with one protein and solvent,
        that then ends up in a state with one less ligand.

        Parameters
        ----------
        stateA : ChemicalSystem
          The chemical system of end state A
        stateB : ChemicalSystem
          The chemical system of end state B

        Raises
        ------
        ValueError
          If stateA does not contain a ProteinComponent
          If stateA does not contain a SolventComponent
          If stateA has more than one unique Component
          If the stateA unique Component is not a SmallMoleculeComponent
          If stateB contains any unique Components
        """
        if not any(
            isinstance(comp, ProteinComponent) for comp in stateA.values()
        ):
            errmsg = "No ProteinComponent found"
            raise ValueError(errmsg)

        if not any(
            isinstance(comp, SolventComponent) for comp in stateA.values()
        ):
            errmsg = "No SolventComponent found"
            raise ValueError(errmsg)

        # Needs gufe 1.3
        diff = stateA.component_diff(stateB)
        if len(diff[0]) > 1:
            errmsg = ("More than unique components found in stateA, "
                      "only one alchemical species is supported")
            raise ValueError(errmsg)

        if not isinstance(diff[0][0], SmallMoleculeComponent):
            errmsg = ("Only dissapearing smalll molecule components "
                      "are supported by this protocol. "
                      f"Found a {type(diff[0][0])}")
            raise ValueError(errmsg)

        if len(diff[1]) > 0:
            errmsg = ("Unique components are found in stateB, "
                      "this should not happen")
            raise ValueError(errmsg)

    @staticmethod
    def _validate_lambda_schedule(
            lambda_settings: LambdaSettings,
            simulation_settings: MultiStateSimulationSettings,
    ) -> None:
        """
        Checks that the lambda schedule is set up correctly.

        Parameters
        ----------
        lambda_settings : LambdaSettings
          the lambda schedule Settings
        simulation_settings : MultiStateSimulationSettings
          the settings for either the complex or solvent phase

        Raises
        ------
        ValueError
          If the number of lambda windows differs for electrostatics, sterics,
          and restraints.
          If the number of replicas does not match the number of lambda windows.
          If there are states with naked charges.
        """

        lambda_elec = lambda_settings.lambda_elec
        lambda_vdw = lambda_settings.lambda_vdw
        lambda_restraints = lambda_settings.lambda_restraints
        n_replicas = simulation_settings.n_replicas

        # Ensure that all lambda components have equal amount of windows
        lambda_components = [lambda_vdw, lambda_elec, lambda_restraints]
        it = iter(lambda_components)
        the_len = len(next(it))
        if not all(len(l) == the_len for l in it):
            errmsg = (
                "Components elec, vdw, and restraints must have equal amount"
                f" of lambda windows. Got {len(lambda_elec)} elec lambda"
                f" windows, {len(lambda_vdw)} vdw lambda windows, and"
                f"{len(lambda_restraints)} restraints lambda windows.")
            raise ValueError(errmsg)

        # Ensure that number of overall lambda windows matches number of lambda
        # windows for individual components
        if n_replicas != len(lambda_vdw):
            errmsg = (f"Number of replicas {n_replicas} does not equal the"
                      f" number of lambda windows {len(lambda_vdw)}")
            raise ValueError(errmsg)

        # Check if there are no lambda windows with naked charges
        for inx, lam in enumerate(lambda_elec):
            if lam < 1 and lambda_vdw[inx] == 1:
                errmsg = (
                    "There are states along this lambda schedule "
                    "where there are atoms with charges but no LJ "
                    f"interactions: lambda {inx}: "
                    f"elec {lam} vdW {lambda_vdw[inx]}")
                raise ValueError(errmsg)

    def _create(
        self,
        stateA: ChemicalSystem,
        stateB: ChemicalSystem,
        mapping: Optional[Union[gufe.ComponentMapping, list[gufe.ComponentMapping]]] = None,
        extends: Optional[gufe.ProtocolDAGResult] = None,
    ) -> list[gufe.ProtocolUnit]:
        # TODO: extensions
        if extends:  # pragma: no-cover
            raise NotImplementedError("Can't extend simulations yet")

        # Validate components and get alchemical components
        self._validate_endstates(stateA, stateB)
        alchem_comps = system_validation.get_alchemical_components(
            stateA, stateB,
        )

        # Validate the lambda schedule
        self._validate_lambda_schedule(self.settings.solvent_lambda_settings,
                                       self.settings.solvent_simulation_settings)
        self._validate_lambda_schedule(self.settings.complex_lambda_settings,
                                       self.settings.complex_simulation_settings)

        # Check nonbond & solvent compatibility
        nonbonded_method = self.settings.forcefield_settings.nonbonded_method
        # Use the more complete system validation solvent checks
        system_validation.validate_solvent(stateA, nonbonded_method)

        # Validate solvation settings
        settings_validation.validate_openmm_solvation_settings(
            self.settings.solvation_settings
        )
    
        # Get the name of the alchemical species
        alchname = alchem_comps['stateA'][0].name

        # Create list units for complex and solvent transforms

        solvent_units = [
            AbsoluteBindingSolventUnit(
                protocol=self,
                stateA=stateA,
                stateB=stateB,
                alchemical_components=alchem_comps,
                generation=0, repeat_id=int(uuid.uuid4()),
                name=(f"Absolute Binding, {alchname} solvent leg: "
                      f"repeat {i} generation 0"),
            )
            for i in range(self.settings.protocol_repeats)
        ]

        complex_units = [
            AbsoluteBindingComplexUnit(
                protocol=self,
                stateA=stateA,
                stateB=stateB,
                alchemical_components=alchem_comps,
                generation=0, repeat_id=int(uuid.uuid4()),
                name=(f"Absolute Binding, {alchname} complex leg: "
                      f"repeat {i} generation 0"),
            )
            for i in range(self.settings.protocol_repeats)
        ]

        return solvent_units + complex_units

    def _gather(
        self, protocol_dag_results: Iterable[gufe.ProtocolDAGResult]
    ) -> dict[str, dict[str, Any]]:
        # result units will have a repeat_id and generation
        # first group according to repeat_id
        unsorted_solvent_repeats = defaultdict(list)
        unsorted_complex_repeats = defaultdict(list)
        for d in protocol_dag_results:
            pu: gufe.ProtocolUnitResult
            for pu in d.protocol_unit_results:
                if not pu.ok():
                    continue
                if pu.outputs['simtype'] == 'solvent':
                    unsorted_solvent_repeats[pu.outputs['repeat_id']].append(pu)
                else:
                    unsorted_complex_repeats[pu.outputs['repeat_id']].append(pu)

        repeats: dict[str, dict[str, list[gufe.ProtocolUnitResult]]] = {
            'solvent': {}, 'complex': {},
        }
        for k, v in unsorted_solvent_repeats.items():
            repeats['solvent'][str(k)] = sorted(v, key=lambda x: x.outputs['generation'])

        for k, v in unsorted_complex_repeats.items():
            repeats['complex'][str(k)] = sorted(v, key=lambda x: x.outputs['generation'])
        return repeats


class AbsoluteBindingComplexUnit(BaseAbsoluteUnit):
    """
    Protocol Unit for the complex phase of an absolute binding free energy
    """
    def _get_components(self):
        """
        Get the relevant components for a complex transformation.

        Returns
        -------
        alchem_comps : dict[str, Component]
          A list of alchemical components
        solv_comp : SolventComponent
          The SolventComponent of the system
        prot_comp : Optional[ProteinComponent]
          The protein component of the system, if it exists.
        small_mols : dict[SmallMoleculeComponent: OFFMolecule]
          SmallMoleculeComponents to add to the system.
        """
        stateA = self._inputs['stateA']
        alchem_comps = self._inputs['alchemical_components']

        solv_comp, prot_comp, small_mols = system_validation.get_components(stateA)
        off_comps = {m: m.to_openff() for m in small_mols}

        # We don't need to check that solv_comp is not None, otherwise
        # an error will have been raised when calling `validate_solvent`
        # in the Protocol's `_create`.
        # Similarly we don't need to check prot_comp
        return alchem_comps, solv_comp, prot_comp, off_comps

    def _handle_settings(self) -> dict[str, SettingsBaseModel]:
        """
        Extract the relevant settings for a complex transformation.

        Returns
        -------
        settings : dict[str, SettingsBaseModel]
          A dictionary with the following entries:
            * forcefield_settings : OpenMMSystemGeneratorFFSettings
            * thermo_settings : ThermoSettings
            * charge_settings : OpenFFPartialChargeSettings
            * solvation_settings : OpenMMSolvationSettings
            * alchemical_settings : AlchemicalSettings
            * lambda_settings : LambdaSettings
            * engine_settings : OpenMMEngineSettings
            * integrator_settings : IntegratorSettings
            * equil_simulation_settings : MDSimulationSettings
            * equil_output_settings : MDOutputSettings
            * simulation_settings : SimulationSettings
            * output_settings: MultiStateOutputSettings
            * restraint_settings: BaseRestraintSettings
        """
        prot_settings = self._inputs['protocol'].settings

        settings = {}
        settings['forcefield_settings'] = prot_settings.forcefield_settings
        settings['thermo_settings'] = prot_settings.thermo_settings
        settings['charge_settings'] = prot_settings.partial_charge_settings
        settings['solvation_settings'] = prot_settings.solvation_settings
        settings['alchemical_settings'] = prot_settings.alchemical_settings
        settings['lambda_settings'] = prot_settings.complex_lambda_settings
        settings['engine_settings'] = prot_settings.engine_settings
        settings['integrator_settings'] = prot_settings.integrator_settings
        settings['equil_simulation_settings'] = prot_settings.complex_equil_simulation_settings
        settings['equil_output_settings'] = prot_settings.complex_equil_output_settings
        settings['simulation_settings'] = prot_settings.complex_simulation_settings
        settings['output_settings'] = prot_settings.complex_output_settings
        settings['restraint_settings'] = prot_settings.restraint_settings

        settings_validation.validate_timestep(
            settings['forcefield_settings'].hydrogen_mass,
            settings['integrator_settings'].timestep
        )

        return settings

    def _add_restraints(
        self,
        system: openmm.System,
        topology: openmm.app.Topology,
        settings: dict[str, SettingsBaseModel]
    ) -> [GlobalParameterState, unit.Quantity, openmm.System]:
        """
        Find and add restraints to the OpenMM System.

        Parameters
        ----------
        system : openmm.System
          The System to add the restraint to.
        topology : openmm.app.Topology
          An OpenMM Topology that defines the System.
        settings : dict[str, SettingsBaseModel]
          A dictionary of settings that defines how to find and set
          the restraint.

        Returns
        -------
        restraint_parameter_state : RestraintParameterState
          A RestraintParameterState object that defines the control
          parameter for the restraint.
        correction : unit.Quantity
          The standard state correction for the restraint.
        system : openmm.System
          A copy of the System with the restraint added.
        """
        from openfe.protocols.openmm_utils import (
            omm_restraints, geometry, search
        )

        if isinstance(settings['restraints_settings'], BoreschRestraintSettings):
            geom = search.get_boresch_restraint(
                topology,
                self.shared_basepath / settings['equil_output_settings'].production_trajectory_filename
            )

            restraint = omm_restraints.BoreschRestraint(
                settings['restraints_settings'],
                geom,
                controlling_parameter_name='lambda_restraints'
            )
        else:
            # TODO turn this into a direction for different restraint types supported?
            raise NotImplementedError()

        # We need a temporary thermodynamic state to add the restraint
        # & get the correction
        thermodynamic_state = ThermodynamicState(
            system,
            temperature=to_openmm(settings['thermo_settings'].temperature),
            pressure=to_openmm(settings['thermo_settings'].pressure),
        )

        # Add the force to the thermodynamic state
        restraint.add_force(thermodynamic_state)
        # Get the standard state correction as a unit.Quantity
        correction = restraint.get_standard_state_correction(thermodynamic_state)

        # Get the GlobalParameterState for the restraint
        retraint_parameter_state = omm_restraints.RestraintParameterState(
            lambda_restraints=1.0
        )
        return restraint_parameter_state, correction, thermodynamic_state.system

    def _execute(
        self, ctx: gufe.Context, **kwargs,
    ) -> dict[str, Any]:
        log_system_probe(logging.INFO, paths=[ctx.scratch])

        outputs = self.run(scratch_basepath=ctx.scratch,
                           shared_basepath=ctx.shared)

        return {
            'repeat_id': self._inputs['repeat_id'],
            'generation': self._inputs['generation'],
            'simtype': 'complex',
            **outputs
        }


class AbsoluteBindingSolventUnit(BaseAbsoluteUnit):
    """
    Protocol Unit for the solvent phase of an absolute binding free energy
    """
    def _get_components(self):
        """
        Get the relevant components for a solvent transformation.

        Returns
        -------
        alchem_comps : dict[str, Component]
          A list of alchemical components
        solv_comp : SolventComponent
          The SolventComponent of the system
        prot_comp : Optional[ProteinComponent]
          The protein component of the system, if it exists.
        small_mols : dict[SmallMoleculeComponent: OFFMolecule]
          SmallMoleculeComponents to add to the system.
        """
        stateA = self._inputs['stateA']
        alchem_comps = self._inputs['alchemical_components']

        solv_comp, prot_comp, small_mols = system_validation.get_components(stateA)
        off_comps = {m: m.to_openff() for m in small_mols}

        # We don't need to check that solv_comp is not None, otherwise
        # an error will have been raised when calling `validate_solvent`
        # in the Protocol's `_create`.
        # Similarly we don't need to check prot_comp
        return alchem_comps, solv_comp, prot_comp, off_comps

    def _handle_settings(self) -> dict[str, SettingsBaseModel]:
        """
        Extract the relevant settings for a solvent transformation.

        Returns
        -------
        settings : dict[str, SettingsBaseModel]
          A dictionary with the following entries:
            * forcefield_settings : OpenMMSystemGeneratorFFSettings
            * thermo_settings : ThermoSettings
            * charge_settings : OpenFFPartialChargeSettings
            * solvation_settings : OpenMMSolvationSettings
            * alchemical_settings : AlchemicalSettings
            * lambda_settings : LambdaSettings
            * engine_settings : OpenMMEngineSettings
            * integrator_settings : IntegratorSettings
            * equil_simulation_settings : MDSimulationSettings
            * equil_output_settings : MDOutputSettings
            * simulation_settings : MultiStateSimulationSettings
            * output_settings: MultiStateOutputSettings
        """
        prot_settings = self._inputs['protocol'].settings

        settings = {}
        settings['forcefield_settings'] = prot_settings.forcefield_settings
        settings['thermo_settings'] = prot_settings.thermo_settings
        settings['charge_settings'] = prot_settings.partial_charge_settings
        settings['solvation_settings'] = prot_settings.solvation_settings
        settings['alchemical_settings'] = prot_settings.alchemical_settings
        settings['lambda_settings'] = prot_settings.solvent_lambda_settings
        settings['engine_settings'] = prot_settings.engine_settings
        settings['integrator_settings'] = prot_settings.integrator_settings
        settings['equil_simulation_settings'] = prot_settings.solvent_equil_simulation_settings
        settings['equil_output_settings'] = prot_settings.solvent_equil_output_settings
        settings['simulation_settings'] = prot_settings.solvent_simulation_settings
        settings['output_settings'] = prot_settings.solvent_output_settings

        settings_validation.validate_timestep(
            settings['forcefield_settings'].hydrogen_mass,
            settings['integrator_settings'].timestep
        )

        return settings

    def _execute(
        self, ctx: gufe.Context, **kwargs,
    ) -> dict[str, Any]:
        log_system_probe(logging.INFO, paths=[ctx.scratch])

        outputs = self.run(scratch_basepath=ctx.scratch,
                           shared_basepath=ctx.shared)

        return {
            'repeat_id': self._inputs['repeat_id'],
            'generation': self._inputs['generation'],
            'simtype': 'solvent',
            **outputs
        }
