"""Milestone 2 typed simulator extensions (``sim_watch`` and ``compute_sim``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pea.math_engine.compute_sim import run_compute_sim
from pea.schemas import ComputeSimResult, ComputeSimSpec, WatchResult, WatchSpec

if TYPE_CHECKING:
    from pea.sim.engine import SimulatorSession
    from pea.workspace import RunWorkspace


async def sim_watch(
    session: SimulatorSession,
    spec: WatchSpec | dict[str, Any],
) -> WatchResult:
    """Run timed transient / stopwatch capture via ``session.watch(spec)``."""
    validated = spec if isinstance(spec, WatchSpec) else WatchSpec.model_validate(spec)
    return await session.watch(validated)


def compute_sim(
    spec: ComputeSimSpec | dict[str, Any],
    run_workspace: RunWorkspace,
) -> ComputeSimResult:
    """Run sandboxed numerical, ODE, or FFT modeling via ``run_compute_sim``."""
    validated = (
        spec if isinstance(spec, ComputeSimSpec) else ComputeSimSpec.model_validate(spec)
    )
    return run_compute_sim(validated, run_workspace=run_workspace)


__all__ = [
    "compute_sim",
    "sim_watch",
]
