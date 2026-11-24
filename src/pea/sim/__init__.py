"""Universal simulator subsystem (SWF metadata, loopback server, profiles, engine, M2 stubs)."""

from __future__ import annotations

from pea.sim.engine import (
    SimCancelledError,
    SimulatorManager,
    SimulatorSession,
    StepperPlan,
    compute_html5_dir_sha256,
    compute_stepper_plan,
    compute_url_sha256,
    format_control_value,
    render_annotated_overlay,
    select_all_shortcut,
    values_match,
)
from pea.sim.profiles import ProfileStore, validate_profile_geometry
from pea.sim.protocols_m2 import compute_sim, sim_watch
from pea.sim.runtime_server import SimRuntimeServer, render_ruffle_index_html
from pea.sim.swf_meta import SwfFormatError, inspect_swf

__all__ = [
    "ProfileStore",
    "SimCancelledError",
    "SimRuntimeServer",
    "SimulatorManager",
    "SimulatorSession",
    "StepperPlan",
    "SwfFormatError",
    "compute_html5_dir_sha256",
    "compute_sim",
    "compute_stepper_plan",
    "compute_url_sha256",
    "format_control_value",
    "inspect_swf",
    "render_annotated_overlay",
    "render_ruffle_index_html",
    "select_all_shortcut",
    "sim_watch",
    "validate_profile_geometry",
    "values_match",
]
