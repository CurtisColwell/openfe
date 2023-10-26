# This code is part of OpenFE and is licensed under the MIT license.
# For details, see https://github.com/OpenFreeEnergy/openfe
"""Equilibrium Free Energy Protocols input settings.

This module implements base settings necessary to run
free energy calculations using OpenMM +/- Tools, such
as :mod:`openfe.protocols.openmm_rfe.equil_rfe_methods.py`
and :mod`openfe.protocols.openmm_afe.equil_afe_methods.py`
"""
from __future__ import annotations

from typing import Optional
from openff.units import unit

from gufe.settings import (
    SettingsBaseModel,
)


try:
    from pydantic.v1 import validator
except ImportError:
    from pydantic import validator  # type: ignore[assignment]


class SystemSettings(SettingsBaseModel):
    """Settings describing the simulation system settings."""

    class Config:
        arbitrary_types_allowed = True

    nonbonded_method = 'PME'
    """
    Method for treating nonbonded interactions, currently only PME and
    NoCutoff are allowed. Default PME.
    """
    nonbonded_cutoff = 1.0 * unit.nanometer
    """
    Cutoff value for short range nonbonded interactions.
    Default 1.0 * unit.nanometer.
    """

    @validator('nonbonded_method')
    def allowed_nonbonded(cls, v):
        if v.lower() not in ['pme', 'nocutoff']:
            errmsg = ("Only PME and NoCutoff are allowed nonbonded_methods")
            raise ValueError(errmsg)
        return v

    @validator('nonbonded_cutoff')
    def is_positive_distance(cls, v):
        # these are time units, not simulation steps
        if not v.is_compatible_with(unit.nanometer):
            raise ValueError("nonbonded_cutoff must be in distance units "
                             "(i.e. nanometers)")
        if v < 0:
            errmsg = "nonbonded_cutoff must be a positive value"
            raise ValueError(errmsg)
        return v


class SolvationSettings(SettingsBaseModel):
    """Settings for solvating the system

    Note
    ----
    No solvation will happen if a SolventComponent is not passed.

    """
    class Config:
        arbitrary_types_allowed = True

    solvent_model = 'tip3p'
    """
    Force field water model to use.
    Allowed values are; `tip3p`, `spce`, `tip4pew`, and `tip5p`.
    """

    solvent_padding = 1.2 * unit.nanometer
    """Minimum distance from any solute atoms to the solvent box edge."""

    num_solvent_molecules = 500
    """**Packmol backend only:** number of solvent molecules to add."""

    box_mass_density = 950 * unit.kilogram / unit.meter**3
    """**Packmol backend only:** target mass density of solvated box."""

    packmol_tolerance = 0.5 * unit.angstrom
    """
    **Packmol backend only:** Minimum spacing between molecules during
    packing. A lower value will converge faster but may not be be suitable
    for protein simulations.
    """

    backend = 'openmm'
    """
    The backend for solvating a system. Current allowed backends
    are `openmm` and `packmol`.

    Please note that the `packmol` is currently available in
    the AbsoluteSolvationProtocol.
    """

    @validator('backend')
    def allowed_backends(cls, v):
        backends = ['openmm', 'packmol']
        if v.lower() not in backends:
            errmsg = f"Only {backends} are allowed backend selections"
            raise ValueError(errmsg)
        return v

    @validator('solvent_model')
    def allowed_solvent(cls, v):
        allowed_models = ['tip3p', 'spce', 'tip4pew', 'tip5p']
        if v.lower() not in allowed_models:
            errmsg = (
                f"Only {allowed_models} are allowed solvent_model values"
            )
            raise ValueError(errmsg)
        return v

    @validator('solvent_padding')
    def is_positive_distance(cls, v):
        # these are time units, not simulation steps
        if not v.is_compatible_with(unit.nanometer):
            raise ValueError("solvent_padding must be in distance units "
                             "(i.e. nanometers)")
        if v < 0:
            errmsg = "solvent_padding must be a positive value"
            raise ValueError(errmsg)
        return v


class AlchemicalSamplerSettings(SettingsBaseModel):
    """Settings for the Equilibrium Alchemical sampler, currently supporting
    either MultistateSampler, SAMSSampler or ReplicaExchangeSampler.

    """

    """
    TODO
    ----
    * It'd be great if we could pass in the sampler object rather than using
      strings to define which one we want.
    * Make n_replicas optional such that: If `None` or greater than the number
      of lambda windows set in :class:`AlchemicalSettings`, this will default
      to the number of lambda windows. If less than the number of lambda
      windows, the replica lambda states will be picked at equidistant
      intervals along the lambda schedule.
    """
    class Config:
        arbitrary_types_allowed = True

    sampler_method = "repex"
    """
    Alchemical sampling method, must be one of;
    `repex` (Hamiltonian Replica Exchange),
    `sams` (Self-Adjusted Mixture Sampling),
    or `independent` (independently sampled lambda windows).
    Default `repex`.
    """
    online_analysis_interval: Optional[int] = 250
    """
    MCMC steps (i.e. ``IntegratorSettings.n_steps``) interval at which
    to perform an analysis of the free energies.

    At each interval, real time analysis data (e.g. current free energy
    estimate and timing data) will be written to a yaml file named
    ``<SimulationSettings.output_name>_real_time_analysis.yaml``. The
    current error in the estimate will also be assed and if it drops
    below ``AlchemicalSamplerSettings.online_analysis_target_error``
    the simulation will be terminated.

    If ``None``, no real time analysis will be performed and the yaml
    file will not be written.

    Must be a multiple of ``SimulationSettings.checkpoint_interval``

    Default `250`.
    """
    online_analysis_target_error = 0.0 * unit.boltzmann_constant * unit.kelvin
    """
    Target error for the online analysis measured in kT. Once the free energy
    is at or below this value, the simulation will be considered complete. A
    suggested value of 0.2 * `unit.boltzmann_constant` * `unit.kelvin` has
    shown to be effective in both hydration and binding free energy benchmarks.
    Default 0.0 * `unit.boltzmann_constant` * `unit.kelvin`, i.e. no early
    termination will occur.
    """
    online_analysis_minimum_iterations = 500
    """
    Number of iterations which must pass before online analysis is
    carried out. Default 500.
    """
    n_repeats: int = 3
    """
    Number of independent repeats to run.  Default 3
    """
    flatness_criteria = 'logZ-flatness'
    """
    SAMS only. Method for assessing when to switch to asymptomatically
    optimal scheme.
    One of ['logZ-flatness', 'minimum-visits', 'histogram-flatness'].
    Default 'logZ-flatness'.
    """
    gamma0 = 1.0
    """SAMS only. Initial weight adaptation rate. Default 1.0."""
    n_replicas = 11
    """Number of replicas to use. Default 11."""

    @validator('flatness_criteria')
    def supported_flatness(cls, v):
        supported = [
            'logz-flatness', 'minimum-visits', 'histogram-flatness'
        ]
        if v.lower() not in supported:
            errmsg = ("Only the following flatness_criteria are "
                      f"supported: {supported}")
            raise ValueError(errmsg)
        return v

    @validator('sampler_method')
    def supported_sampler(cls, v):
        supported = ['repex', 'sams', 'independent']
        if v.lower() not in supported:
            errmsg = ("Only the following sampler_method values are "
                      f"supported: {supported}")
            raise ValueError(errmsg)
        return v

    @validator('n_repeats', 'n_replicas')
    def must_be_positive(cls, v):
        if v <= 0:
            errmsg = "n_repeats and n_replicas must be positive values"
            raise ValueError(errmsg)
        return v

    @validator('online_analysis_target_error', 'n_repeats',
               'online_analysis_minimum_iterations', 'gamma0', 'n_replicas')
    def must_be_zero_or_positive(cls, v):
        if v < 0:
            errmsg = ("Online analysis target error, minimum iteration "
                      "and SAMS gamm0 must be 0 or positive values.")
            raise ValueError(errmsg)
        return v


class OpenMMEngineSettings(SettingsBaseModel):
    """OpenMM MD engine settings"""

    """
    TODO
    ----
    * In the future make precision and deterministic forces user defined too.
    """

    compute_platform: Optional[str] = None
    """
    OpenMM compute platform to perform MD integration with. If None, will
    choose fastest available platform. Default None.
    """


class IntegratorSettings(SettingsBaseModel):
    """Settings for the LangevinSplittingDynamicsMove integrator"""

    class Config:
        arbitrary_types_allowed = True

    timestep = 4 * unit.femtosecond
    """Size of the simulation timestep. Default 4 * unit.femtosecond."""
    collision_rate = 1.0 / unit.picosecond
    """Collision frequency. Default 1.0 / unit.pisecond."""
    n_steps = 250 * unit.timestep
    """
    Number of integration timesteps between each time the MCMC move
    is applied. Default 250 * unit.timestep.
    """
    reassign_velocities = False
    """
    If True, velocities are reassigned from the Maxwell-Boltzmann
    distribution at the beginning of move. Default False.
    """
    n_restart_attempts = 20
    """
    Number of attempts to restart from Context if there are NaNs in the
    energies after integration. Default 20.
    """
    constraint_tolerance = 1e-06
    """Tolerance for the constraint solver. Default 1e-6."""
    barostat_frequency = 25 * unit.timestep
    """
    Frequency at which volume scaling changes should be attempted.
    Default 25 * unit.timestep.
    """

    @validator('collision_rate', 'n_restart_attempts')
    def must_be_positive_or_zero(cls, v):
        if v < 0:
            errmsg = ("collision_rate, and n_restart_attempts must be "
                      "zero or positive values")
            raise ValueError(errmsg)
        return v

    @validator('timestep', 'n_steps', 'constraint_tolerance')
    def must_be_positive(cls, v):
        if v <= 0:
            errmsg = ("timestep, n_steps, constraint_tolerance "
                      "must be positive values")
            raise ValueError(errmsg)
        return v

    @validator('timestep')
    def is_time(cls, v):
        # these are time units, not simulation steps
        if not v.is_compatible_with(unit.picosecond):
            raise ValueError("timestep must be in time units "
                             "(i.e. picoseconds)")
        return v

    @validator('collision_rate')
    def must_be_inverse_time(cls, v):
        if not v.is_compatible_with(1 / unit.picosecond):
            raise ValueError("collision_rate must be in inverse time "
                             "(i.e. 1/picoseconds)")
        return v


class SimulationSettings(SettingsBaseModel):
    """
    Settings for simulation control, including lengths,
    writing to disk, etc...
    """
    class Config:
        arbitrary_types_allowed = True

    minimization_steps = 5000
    """Number of minimization steps to perform. Default 5000."""
    equilibration_length: unit.Quantity
    """
    Length of the equilibration phase in units of time. The total number of
    steps from this equilibration length
    (i.e. ``equilibration_length`` / :class:`IntegratorSettings.timestep`)
    must be a multiple of the value defined for
    :class:`IntegratorSettings.n_steps`.
    """
    production_length: unit.Quantity
    """
    Length of the production phase in units of time. The total number of
    steps from this production length (i.e.
    ``production_length`` / :class:`IntegratorSettings.timestep`) must be
    a multiple of the value defined for :class:`IntegratorSettings.nsteps`.
    """

    # reporter settings
    output_filename = 'simulation.nc'
    """Path to the trajectory storage file. Default 'simulation.nc'."""
    output_structure = 'hybrid_system.pdb'
    """
    Path of the output hybrid topology structure file. This is used
    to visualise and further manipulate the system.
    Default 'hybrid_system.pdb'.
    """
    output_indices = 'not water'
    """
    Selection string for which part of the system to write coordinates for.
    Default 'not water'.
    """
    checkpoint_interval = 250 * unit.timestep
    """
    Frequency to write the checkpoint file. Default 250 * unit.timestep.
    """
    checkpoint_storage = 'checkpoint.nc'
    """
    Separate filename for the checkpoint file. Note, this should
    not be a full path, just a filename. Default 'checkpoint.nc'.
    """
    forcefield_cache: Optional[str] = 'db.json'
    """
    Filename for caching small molecule residue templates so they can be
    later reused.
    """

    @validator('equilibration_length', 'production_length')
    def is_time(cls, v):
        # these are time units, not simulation steps
        if not v.is_compatible_with(unit.picosecond):
            raise ValueError("Durations must be in time units")
        return v

    @validator('minimization_steps', 'equilibration_length',
               'production_length', 'checkpoint_interval')
    def must_be_positive(cls, v):
        if v <= 0:
            errmsg = ("Minimization steps, MD lengths, and checkpoint "
                      "intervals must be positive")
            raise ValueError(errmsg)
        return v
