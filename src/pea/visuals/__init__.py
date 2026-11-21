"""Vector visuals subsystem (Matplotlib scientific plots + schemdraw/parametric diagrams)."""

from __future__ import annotations

from pea.visuals.diagrams import DiagramError, render_diagram
from pea.visuals.plots import (
    ACADEMIC_PALETTE,
    academic_rc_params,
    configure_bundled_fonts,
    render_plot,
    validate_figure_id,
)

__all__ = [
    "ACADEMIC_PALETTE",
    "DiagramError",
    "academic_rc_params",
    "configure_bundled_fonts",
    "render_diagram",
    "render_plot",
    "validate_figure_id",
]
