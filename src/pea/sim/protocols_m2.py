"""Milestone 2 typed simulator extension stubs (``sim_watch`` and ``compute_sim``)."""

from __future__ import annotations

from typing import Any, NoReturn

from pea.schemas import ComputeSimSpec, WatchSpec


async def sim_watch(spec: WatchSpec, *args: Any, **kwargs: Any) -> NoReturn:
    """Timed transient / stopwatch capture stub scheduled for Milestone 2."""
    if not isinstance(spec, WatchSpec):
        WatchSpec.model_validate(spec)
    raise NotImplementedError("sim_watch is scheduled for Milestone 2")


def compute_sim(spec: ComputeSimSpec, *args: Any, **kwargs: Any) -> NoReturn:
    """Sandboxed numerical/ODE modeling stub scheduled for Milestone 2."""
    if not isinstance(spec, ComputeSimSpec):
        ComputeSimSpec.model_validate(spec)
    raise NotImplementedError("compute_sim is scheduled for Milestone 2")


__all__ = [
    "compute_sim",
    "sim_watch",
]
