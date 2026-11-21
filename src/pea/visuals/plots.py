"""Headless Matplotlib vector SVG and 300 DPI PNG scientific plot renderer.

Configures bundled ``local_assets/fonts/NotoSans-Regular.ttf`` and ``NotoSans-Bold.ttf``
for full English + Cyrillic glyph coverage without any external network calls.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import matplotlib

matplotlib.use("Agg", force=True)

from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_svg import FigureCanvasSVG
from matplotlib.figure import Figure

from pea.config import PathConfinementError, PeaConfig
from pea.schemas import PlotSpec, ProvenanceSource, ReportFigure

if TYPE_CHECKING:
    from pea.workspace import RunWorkspace

_FIGURE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REGISTERED_FONTS: set[Path] = set()

# Colorblind-safe academic publication palette
ACADEMIC_PALETTE: tuple[str, ...] = (
    "#1f4e79",  # deep navy
    "#c0392b",  # crimson
    "#2e7d32",  # forest green
    "#d97706",  # amber
    "#6b4c9a",  # plum
    "#00838f",  # teal
    "#5d4037",  # espresso
)
_MARKER_CYCLE: tuple[str, ...] = ("o", "s", "^", "D", "v", "P", "X")


def validate_figure_id(figure_id: str) -> str:
    """Validate that a figure/plot/diagram ID is safe for confined filename construction."""
    cleaned = figure_id.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or cleaned in (".", ".."):
        raise PathConfinementError(f"Invalid figure_id: {figure_id!r}")
    if not _FIGURE_ID_RE.match(cleaned):
        raise PathConfinementError(f"figure_id contains disallowed characters: {figure_id!r}")
    return cleaned


def configure_bundled_fonts(config: PeaConfig | None = None) -> list[str]:
    """Register bundled Noto Sans TTF fonts with Matplotlib (idempotently) and return font stack."""
    cfg = config or PeaConfig.from_env()
    fonts_dir = cfg.local_assets_dir / "fonts"
    loaded_noto = False

    for fname in ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"):
        font_path = fonts_dir / fname
        if font_path.is_file():
            resolved = font_path.resolve()
            if resolved not in _REGISTERED_FONTS:
                font_manager.fontManager.addfont(str(resolved))
                _REGISTERED_FONTS.add(resolved)
            loaded_noto = True

    if loaded_noto:
        return ["Noto Sans", "DejaVu Sans"]
    return ["DejaVu Sans"]


def academic_rc_params(font_stack: list[str]) -> dict[str, Any]:
    """Return scoped Matplotlib rcParams for crisp academic plots and schematics."""
    return {
        "font.family": font_stack,
        "mathtext.fontset": "dejavusans",
        "axes.unicode_minus": True,
        "axes.edgecolor": "#334155",
        "axes.linewidth": 0.9,
        "axes.labelcolor": "#0f172a",
        "axes.titlecolor": "#0f172a",
        "xtick.color": "#334155",
        "ytick.color": "#334155",
        "figure.facecolor": "#ffffff",
        "axes.facecolor": "#ffffff",
        "savefig.facecolor": "#ffffff",
        "svg.fonttype": "path",
    }


def _validate_finite_sequence(
    values: list[float],
    label: str,
    *,
    non_negative: bool = False,
) -> None:
    for idx, val in enumerate(values):
        if isinstance(val, bool) or not math.isfinite(float(val)):
            raise ValueError(f"Non-finite coordinate in {label}[{idx}]: {val!r}")
        if non_negative and float(val) < 0.0:
            raise ValueError(f"Negative error bar in {label}[{idx}]: {val!r}")


def _assert_provenance_safe_write(
    workspace: RunWorkspace,
    relpaths: tuple[str, ...],
    *,
    entity_label: str,
    overwrite: bool,
) -> None:
    """Enforce overwrite guard and protect existing ``provenance.jsonl`` hashes."""
    if not overwrite:
        for rel in relpaths:
            if workspace.resolve_path(rel).exists():
                raise FileExistsError(
                    f"Artifacts for {entity_label} already exist in run {workspace.run_id}; "
                    "choose a unique ID to preserve provenance immutability"
                )
        return

    recorded = {entry.relpath for entry in workspace.read_provenance()}
    conflicts = [rel for rel in relpaths if rel in recorded]
    if conflicts:
        raise FileExistsError(
            f"Cannot overwrite provenance-recorded artifact(s) {conflicts} in run "
            f"{workspace.run_id}; choose a unique ID to preserve provenance integrity"
        )


def _save_figure_dual(fig: Figure, svg_path: Path, png_path: Path, *, dpi: int) -> None:
    """Atomically export *fig* to vector SVG and high-DPI PNG without global pyplot state."""
    svg_tmp = svg_path.with_suffix(".svg.tmp")
    png_tmp = png_path.with_suffix(".png.tmp")
    try:
        FigureCanvasSVG(fig)
        fig.savefig(svg_tmp, format="svg", bbox_inches="tight", pad_inches=0.12)
        FigureCanvasAgg(fig)
        fig.savefig(png_tmp, format="png", dpi=dpi, bbox_inches="tight", pad_inches=0.12)
        os.replace(svg_tmp, svg_path)
        os.replace(png_tmp, png_path)
    except Exception:
        for tmp in (svg_tmp, png_tmp):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        raise


def render_plot(
    spec: PlotSpec | dict[str, Any],
    workspace: RunWorkspace,
    *,
    config: PeaConfig | None = None,
    source: ProvenanceSource = "simulator_captured",
    overwrite: bool = False,
) -> ReportFigure:
    """Render a scientific ``PlotSpec`` to vector ``.svg`` and 300 DPI ``.png``.

    Both output files are written inside ``<run>/figures/`` and hashed in ``provenance.jsonl``.
    """
    plot_spec = spec if isinstance(spec, PlotSpec) else PlotSpec.model_validate(spec)
    plot_id = validate_figure_id(plot_spec.plot_id)

    for idx, series in enumerate(plot_spec.series):
        _validate_finite_sequence(series.x, f"series[{idx}].x")
        _validate_finite_sequence(series.y, f"series[{idx}].y")
        if series.x_err is not None:
            _validate_finite_sequence(series.x_err, f"series[{idx}].x_err", non_negative=True)
        if series.y_err is not None:
            _validate_finite_sequence(series.y_err, f"series[{idx}].y_err", non_negative=True)

    workspace.ensure_dirs()
    svg_rel = f"figures/{plot_id}.svg"
    png_rel = f"figures/{plot_id}.png"
    svg_path = workspace.resolve_path(svg_rel)
    png_path = workspace.resolve_path(png_rel)

    _assert_provenance_safe_write(
        workspace,
        (svg_rel, png_rel),
        entity_label=f"plot {plot_id!r}",
        overwrite=overwrite,
    )

    font_stack = configure_bundled_fonts(config)
    with matplotlib.rc_context(academic_rc_params(font_stack)):
        fig = Figure(figsize=(plot_spec.width_in, plot_spec.height_in), dpi=plot_spec.dpi)
        try:
            ax = fig.add_subplot(111)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)

            has_labeled_series = False
            for idx, series in enumerate(plot_spec.series):
                color = series.color or ACADEMIC_PALETTE[idx % len(ACADEMIC_PALETTE)]
                label = series.label if series.label else None
                if label:
                    has_labeled_series = True

                if series.kind == "scatter":
                    marker = series.marker or _MARKER_CYCLE[idx % len(_MARKER_CYCLE)]
                    ax.scatter(
                        series.x,
                        series.y,
                        label=label,
                        color=color,
                        marker=marker,
                        s=38,
                        edgecolors="#ffffff",
                        linewidths=0.6,
                        zorder=3,
                    )
                elif series.kind == "line":
                    ax.plot(
                        series.x,
                        series.y,
                        label=label,
                        color=color,
                        linestyle=series.linestyle or "-",
                        marker=series.marker,
                        markersize=4.5 if series.marker else 0,
                        linewidth=1.9,
                        zorder=2,
                    )
                elif series.kind == "fit_line":
                    ax.plot(
                        series.x,
                        series.y,
                        label=label,
                        color=color,
                        linestyle=series.linestyle or "--",
                        linewidth=1.8,
                        zorder=2.5,
                    )
                elif series.kind == "errorbar":
                    marker = series.marker or _MARKER_CYCLE[idx % len(_MARKER_CYCLE)]
                    ax.errorbar(
                        series.x,
                        series.y,
                        xerr=series.x_err,
                        yerr=series.y_err,
                        label=label,
                        color=color,
                        fmt=marker,
                        linestyle=series.linestyle or "none",
                        markersize=5.2,
                        capsize=3.5,
                        elinewidth=1.2,
                        capthick=1.1,
                        zorder=3,
                    )

            ax.set_title(plot_spec.title, fontsize=11.5, fontweight="bold", pad=10)
            ax.set_xlabel(plot_spec.x_label, fontsize=10, labelpad=6)
            ax.set_ylabel(plot_spec.y_label, fontsize=10, labelpad=6)
            ax.tick_params(axis="both", labelsize=9)

            if plot_spec.grid:
                ax.grid(True, linestyle="--", linewidth=0.65, alpha=0.35, color="#94a3b8")
                ax.set_axisbelow(True)

            if plot_spec.equal_aspect:
                ax.set_aspect("equal", adjustable="box")

            legend_obj = None
            if plot_spec.legend and has_labeled_series:
                legend_obj = ax.legend(
                    frameon=True,
                    facecolor="#ffffff",
                    edgecolor="#cbd5e1",
                    framealpha=0.92,
                    fontsize=8.8,
                    loc="best",
                )

            if plot_spec.annotations:
                ann_x, ann_y, ann_ha, ann_va = 0.025, 0.97, "left", "top"
                if legend_obj is not None:
                    # Inspect resolved legend position to avoid overlapping top-left annotations
                    FigureCanvasAgg(fig)
                    fig.canvas.draw()
                    renderer = fig.canvas.get_renderer()  # type: ignore[attr-defined]
                    leg_bbox = legend_obj.get_window_extent(renderer).transformed(
                        ax.transAxes.inverted()
                    )
                    if leg_bbox.x0 < 0.45 and leg_bbox.y1 > 0.65:
                        ann_x, ann_y, ann_ha, ann_va = 0.975, 0.03, "right", "bottom"
                annotation_text = "\n".join(plot_spec.annotations)
                ax.text(
                    ann_x,
                    ann_y,
                    annotation_text,
                    transform=ax.transAxes,
                    va=ann_va,
                    ha=ann_ha,
                    fontsize=8.6,
                    color="#0f172a",
                    bbox={
                        "boxstyle": "round,pad=0.4",
                        "facecolor": "#f8fafc",
                        "edgecolor": "#cbd5e1",
                        "alpha": 0.92,
                    },
                    zorder=5,
                )

            fig.tight_layout()
            _save_figure_dual(fig, svg_path, png_path, dpi=plot_spec.dpi)
        finally:
            fig.clear()

    workspace.record_artifact(
        svg_rel,
        kind="plot_svg",
        metadata={"plot_id": plot_id, "title": plot_spec.title},
        source=source,
    )
    workspace.record_artifact(
        png_rel,
        kind="plot_png",
        metadata={"plot_id": plot_id, "title": plot_spec.title, "dpi": plot_spec.dpi},
        source=source,
    )

    return ReportFigure(
        figure_id=plot_id,
        caption=plot_spec.title.strip() or plot_id,
        svg_relpath=svg_rel,
        png_relpath=png_rel,
        source=source,
    )


__all__ = [
    "ACADEMIC_PALETTE",
    "academic_rc_params",
    "configure_bundled_fonts",
    "render_plot",
    "validate_figure_id",
]
