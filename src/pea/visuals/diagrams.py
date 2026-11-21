"""Vector schematic and parametric physics diagram renderer (SVG + 300 DPI PNG).

Supports three declarative diagram families:
- ``circuit``: ``schemdraw==0.23`` circuit schematics with LaTeX/Cyrillic labels
- ``phasor``: parametric phasor diagrams, impedance/power triangles, and AC waveforms
- ``field``: parametric magnetic/electric field diagrams (wires, loops, vectors, field lines)
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import matplotlib

matplotlib.use("Agg", force=True)

import numpy as np
import schemdraw
import schemdraw.elements as elm
from matplotlib.figure import Figure
from matplotlib.patches import Arc, Circle, FancyArrowPatch, Rectangle

from pea.config import PeaConfig
from pea.schemas import DiagramElement, DiagramSpec, ProvenanceSource, ReportFigure
from pea.visuals.plots import (
    ACADEMIC_PALETTE,
    _save_figure_dual,
    academic_rc_params,
    configure_bundled_fonts,
    validate_figure_id,
)

if TYPE_CHECKING:
    from pea.workspace import RunWorkspace

_DEFAULT_DIAGRAM_DPI = 300

# Two-terminal circuit elements (accept direction, length, at, to)
_CIRCUIT_TWO_TERMINAL: dict[str, type[Any]] = {
    "resistor": elm.Resistor,
    "resistor_iec": elm.ResistorIEC,
    "resistor_var": elm.ResistorVar,
    "potentiometer": elm.ResistorVar,
    "capacitor": elm.Capacitor,
    "inductor": elm.Inductor2,
    "inductor_simple": elm.Inductor,
    "source_sin": elm.SourceSin,
    "ac_source": elm.SourceSin,
    "source_dc": elm.SourceV,
    "source_v": elm.SourceV,
    "battery": elm.SourceV,
    "source_i": elm.SourceI,
    "ammeter": elm.MeterA,
    "voltmeter": elm.MeterV,
    "ohmmeter": elm.MeterOhm,
    "switch": elm.Switch,
    "line": elm.Line,
    "wire": elm.Line,
}

# Single-point circuit elements
_CIRCUIT_ONE_TERMINAL: dict[str, type[Any]] = {
    "ground": elm.Ground,
    "dot": elm.Dot,
    "node": elm.Dot,
    "label": elm.Label,
    "text": elm.Label,
}


class DiagramError(ValueError):
    """Raised when a diagram specification contains unsupported or invalid elements."""


def _validate_point(pt: tuple[float, float], context: str) -> tuple[float, float]:
    x, y = float(pt[0]), float(pt[1])
    if not (math.isfinite(x) and math.isfinite(y)):
        raise DiagramError(f"Non-finite coordinates in {context}: {pt!r}")
    return (x, y)


def _apply_circuit_style(element: Any, style: dict[str, Any]) -> Any:
    if "color" in style:
        element = element.color(str(style["color"]))
    lw = style.get("lw", style.get("linewidth"))
    if lw is not None:
        element = element.linewidth(float(lw))
    ls = style.get("ls", style.get("linestyle"))
    if ls is not None:
        element = element.linestyle(str(ls))
    if style.get("reverse"):
        element = element.reverse()
    if style.get("flip"):
        element = element.flip()
    return element


def _render_circuit(spec: DiagramSpec, fig: Figure) -> None:
    ax = fig.add_subplot(111)
    drawing = schemdraw.Drawing(canvas=ax, show=False)

    for idx, el in enumerate(spec.elements):
        kind = el.kind.strip().lower()
        style = dict(el.style)

        if kind in _CIRCUIT_ONE_TERMINAL:
            cls = _CIRCUIT_ONE_TERMINAL[kind]
            kwargs: dict[str, Any] = {}
            if kind in ("dot", "node") and "open" in style:
                kwargs["open"] = bool(style["open"])
            obj = cls(**kwargs)
            if el.at is not None:
                obj = obj.at(_validate_point(el.at, f"elements[{idx}].at"))
            obj = _apply_circuit_style(obj, style)
            if el.label:
                label_kwargs: dict[str, Any] = {}
                if "label_loc" in style:
                    label_kwargs["loc"] = str(style["label_loc"])
                if "ofst" in style:
                    label_kwargs["ofst"] = style["ofst"]
                obj = obj.label(el.label, **label_kwargs)
            drawing.add(obj)
            continue

        if kind in _CIRCUIT_TWO_TERMINAL:
            cls = _CIRCUIT_TWO_TERMINAL[kind]
            kwargs = {}
            if kind == "inductor" and "loops" in style:
                kwargs["loops"] = int(style["loops"])
            if kind == "resistor" and style.get("iec"):
                cls = elm.ResistorIEC
            obj = cls(**kwargs)

            if el.at is not None:
                obj = obj.at(_validate_point(el.at, f"elements[{idx}].at"))
            if el.to is not None:
                obj = obj.to(_validate_point(el.to, f"elements[{idx}].to"))
            else:
                obj = getattr(obj, el.direction)()
                if el.length is not None:
                    obj = obj.length(float(el.length))

            obj = _apply_circuit_style(obj, style)
            if el.label:
                label_kwargs = {}
                if "label_loc" in style:
                    label_kwargs["loc"] = str(style["label_loc"])
                if "ofst" in style:
                    label_kwargs["ofst"] = style["ofst"]
                obj = obj.label(el.label, **label_kwargs)
            drawing.add(obj)
            continue

        supported = sorted({*_CIRCUIT_TWO_TERMINAL.keys(), *_CIRCUIT_ONE_TERMINAL.keys()})
        raise DiagramError(
            f"Unsupported circuit element kind {el.kind!r} at index {idx}; "
            f"supported kinds: {', '.join(supported)}"
        )

    drawing.draw(show=False)
    ax.set_aspect("equal", adjustable="box")
    ax.set_axis_off()

    if spec.title:
        ax.set_title(spec.title, fontsize=11.5, fontweight="bold", pad=12)
    if spec.notes:
        fig.text(
            0.5,
            0.01,
            "  |  ".join(spec.notes),
            ha="center",
            va="bottom",
            fontsize=8.5,
            color="#334155",
        )


def _resolve_vector_endpoints(
    el: DiagramElement,
    idx: int,
) -> tuple[tuple[float, float], tuple[float, float]]:
    start = _validate_point(el.at, f"elements[{idx}].at") if el.at is not None else (0.0, 0.0)
    if el.to is not None:
        end = _validate_point(el.to, f"elements[{idx}].to")
        return start, end

    style = el.style
    if "magnitude" in style:
        mag = float(style["magnitude"])
        if not math.isfinite(mag):
            raise DiagramError(f"Non-finite magnitude at elements[{idx}]")
        if "angle_rad" in style:
            ang = float(style["angle_rad"])
        else:
            ang = math.radians(float(style.get("angle_deg", 0.0)))
        end = (start[0] + mag * math.cos(ang), start[1] + mag * math.sin(ang))
        return start, end

    length = float(el.length) if el.length is not None else 1.0
    dx, dy = {
        "right": (length, 0.0),
        "left": (-length, 0.0),
        "up": (0.0, length),
        "down": (0.0, -length),
    }[el.direction]
    return start, (start[0] + dx, start[1] + dy)


def _update_bounds(bounds: list[float], x: float, y: float) -> None:
    bounds[0] = min(bounds[0], x)
    bounds[1] = max(bounds[1], x)
    bounds[2] = min(bounds[2], y)
    bounds[3] = max(bounds[3], y)


def _render_phasor(spec: DiagramSpec, fig: Figure) -> None:
    ax = fig.add_subplot(111)
    has_wave = any(el.kind.strip().lower() in ("wave", "sine", "cosine") for el in spec.elements)
    bounds = [0.0, 0.0, 0.0, 0.0]  # xmin, xmax, ymin, ymax
    has_bounds = False

    def _track(x: float, y: float) -> None:
        nonlocal has_bounds
        if not has_bounds:
            bounds[0] = bounds[1] = x
            bounds[2] = bounds[3] = y
            has_bounds = True
        else:
            _update_bounds(bounds, x, y)

    has_legend = False
    for idx, el in enumerate(spec.elements):
        kind = el.kind.strip().lower()
        style = dict(el.style)
        color = str(style.get("color") or ACADEMIC_PALETTE[idx % len(ACADEMIC_PALETTE)])

        if kind in ("vector", "phasor", "arrow"):
            (x0, y0), (x1, y1) = _resolve_vector_endpoints(el, idx)
            _track(x0, y0)
            _track(x1, y1)
            lw = float(style.get("lw", style.get("linewidth", 2.1)))
            arrow = FancyArrowPatch(
                (x0, y0),
                (x1, y1),
                arrowstyle="-|>,head_length=8,head_width=4.5",
                color=color,
                linewidth=lw,
                zorder=4,
            )
            ax.add_patch(arrow)
            if el.label:
                dx, dy = x1 - x0, y1 - y0
                norm = math.hypot(dx, dy) or 1.0
                # Offset label slightly past tip or perpendicular to midpoint
                if style.get("label_pos") == "mid":
                    lx = 0.5 * (x0 + x1) - 0.16 * dy / norm
                    ly = 0.5 * (y0 + y1) + 0.16 * dx / norm
                else:
                    offset = max(0.18, 0.08 * norm)
                    lx = x1 + offset * dx / norm
                    ly = y1 + offset * dy / norm
                _track(lx, ly)
                ax.text(
                    lx,
                    ly,
                    el.label,
                    color=color,
                    fontsize=9.5,
                    fontweight="bold",
                    ha="center",
                    va="center",
                    zorder=5,
                )

        elif kind in ("line", "projection", "dashed"):
            (x0, y0), (x1, y1) = _resolve_vector_endpoints(el, idx)
            _track(x0, y0)
            _track(x1, y1)
            ls = str(style.get("ls", style.get("linestyle", "--")))
            lw = float(style.get("lw", style.get("linewidth", 1.2)))
            ax.plot([x0, x1], [y0, y1], color=color, linestyle=ls, linewidth=lw, zorder=2)
            if el.label:
                mx, my = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
                ax.text(mx, my, el.label, color=color, fontsize=9, ha="center", va="bottom")

        elif kind in ("arc", "angle"):
            cx, cy = _validate_point(el.at, f"elements[{idx}].at") if el.at else (0.0, 0.0)
            radius = float(el.length) if el.length is not None else 0.35
            start_deg = float(style.get("start_deg", 0.0))
            end_deg = float(style.get("end_deg", 45.0))
            theta1, theta2 = (start_deg, end_deg) if end_deg >= start_deg else (end_deg, start_deg)
            arc = Arc(
                (cx, cy),
                2.0 * radius,
                2.0 * radius,
                angle=0.0,
                theta1=theta1,
                theta2=theta2,
                color=color,
                linewidth=float(style.get("lw", 1.4)),
                zorder=3,
            )
            ax.add_patch(arc)
            mid_rad = math.radians(0.5 * (theta1 + theta2))
            _track(cx + radius * math.cos(mid_rad), cy + radius * math.sin(mid_rad))
            if el.label:
                lx = cx + 1.32 * radius * math.cos(mid_rad)
                ly = cy + 1.32 * radius * math.sin(mid_rad)
                _track(lx, ly)
                ax.text(lx, ly, el.label, color=color, fontsize=9.5, ha="center", va="center")

        elif kind == "circle":
            cx, cy = _validate_point(el.at, f"elements[{idx}].at") if el.at else (0.0, 0.0)
            radius = float(el.length) if el.length is not None else 1.0
            circle = Circle(
                (cx, cy),
                radius,
                fill=False,
                edgecolor=color,
                linestyle=str(style.get("ls", ":")),
                linewidth=float(style.get("lw", 1.1)),
                zorder=2,
            )
            ax.add_patch(circle)
            _track(cx - radius, cy - radius)
            _track(cx + radius, cy + radius)

        elif kind in ("wave", "sine", "cosine"):
            amp = float(style.get("amplitude", 1.0))
            omega = float(style.get("omega", 2.0 * math.pi))
            phase = (
                float(style["phase_rad"])
                if "phase_rad" in style
                else math.radians(float(style.get("phase_deg", 0.0)))
            )
            t_min = float(style.get("t_min", 0.0))
            t_max = float(style.get("t_max", 2.0))
            n_pts = int(style.get("points", 400))
            t = np.linspace(t_min, t_max, max(32, n_pts))
            fn_name = str(style.get("func", "sin" if kind == "sine" else "cos")).lower()
            y = amp * (np.sin(omega * t + phase) if fn_name == "sin" else np.cos(omega * t + phase))
            ls = str(style.get("ls", style.get("linestyle", "-")))
            lw = float(style.get("lw", style.get("linewidth", 1.9)))
            ax.plot(t, y, color=color, linestyle=ls, linewidth=lw, label=el.label or None, zorder=3)
            if el.label:
                has_legend = True
            _track(t_min, -abs(amp))
            _track(t_max, abs(amp))

        elif kind in ("point", "dot", "label", "text"):
            px, py = _validate_point(el.at, f"elements[{idx}].at") if el.at else (0.0, 0.0)
            _track(px, py)
            if kind in ("point", "dot"):
                ax.scatter([px], [py], color=color, s=36, zorder=5)
            if el.label:
                ax.text(
                    px,
                    py,
                    el.label,
                    color=color,
                    fontsize=9.2,
                    ha=str(style.get("ha", "left")),
                    va=str(style.get("va", "bottom")),
                    zorder=6,
                )
        else:
            raise DiagramError(
                f"Unsupported phasor element kind {el.kind!r} at index {idx}; "
                "supported: vector, phasor, arrow, line, projection, arc, circle, wave, sine, "
                "cosine, point, dot, label"
            )

    ax.axhline(0.0, color="#64748b", linewidth=0.9, zorder=1)
    ax.axvline(0.0, color="#64748b", linewidth=0.9, zorder=1)
    ax.grid(True, linestyle="--", linewidth=0.6, alpha=0.32, color="#94a3b8")
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    if not has_wave:
        if has_bounds:
            span_x = max(bounds[1] - bounds[0], 0.5)
            span_y = max(bounds[3] - bounds[2], 0.5)
            pad_x = 0.22 * span_x
            pad_y = 0.22 * span_y
            ax.set_xlim(bounds[0] - pad_x, bounds[1] + pad_x)
            ax.set_ylim(bounds[2] - pad_y, bounds[3] + pad_y)
        ax.set_aspect("equal", adjustable="box")
    elif has_legend:
        ax.legend(frameon=True, facecolor="#ffffff", edgecolor="#cbd5e1", fontsize=8.8)

    if spec.title:
        ax.set_title(spec.title, fontsize=11.5, fontweight="bold", pad=10)
    if spec.notes:
        fig.text(
            0.5,
            0.01,
            "  |  ".join(spec.notes),
            ha="center",
            va="bottom",
            fontsize=8.5,
            color="#334155",
        )


def _render_field(spec: DiagramSpec, fig: Figure) -> None:
    ax = fig.add_subplot(111)
    bounds = [0.0, 0.0, 0.0, 0.0]
    has_bounds = False

    def _track(x: float, y: float) -> None:
        nonlocal has_bounds
        if not has_bounds:
            bounds[0] = bounds[1] = x
            bounds[2] = bounds[3] = y
            has_bounds = True
        else:
            _update_bounds(bounds, x, y)

    for idx, el in enumerate(spec.elements):
        kind = el.kind.strip().lower()
        style = dict(el.style)
        color = str(style.get("color") or ACADEMIC_PALETTE[idx % len(ACADEMIC_PALETTE)])

        if kind in ("vector", "arrow", "field_arrow"):
            (x0, y0), (x1, y1) = _resolve_vector_endpoints(el, idx)
            _track(x0, y0)
            _track(x1, y1)
            arrow = FancyArrowPatch(
                (x0, y0),
                (x1, y1),
                arrowstyle="-|>,head_length=8,head_width=4.5",
                color=color,
                linewidth=float(style.get("lw", 2.0)),
                zorder=4,
            )
            ax.add_patch(arrow)
            if el.label:
                dx, dy = x1 - x0, y1 - y0
                norm = math.hypot(dx, dy) or 1.0
                lx, ly = x1 + 0.1 * dx / norm, y1 + 0.1 * dy / norm
                _track(lx, ly)
                ax.text(
                    lx,
                    ly,
                    el.label,
                    color=color,
                    fontsize=9.5,
                    fontweight="bold",
                    ha="center",
                    va="center",
                    zorder=5,
                )

        elif kind in ("wire_in", "wire_out"):
            cx, cy = _validate_point(el.at, f"elements[{idx}].at") if el.at else (0.0, 0.0)
            radius = float(el.length) if el.length is not None else 0.22
            circ = Circle(
                (cx, cy),
                radius,
                facecolor="#ffffff",
                edgecolor=color,
                linewidth=1.8,
                zorder=5,
            )
            ax.add_patch(circ)
            if kind == "wire_out":
                ax.scatter([cx], [cy], color=color, s=28, zorder=6)
            else:
                d = radius * 0.62
                ax.plot([cx - d, cx + d], [cy - d, cy + d], color=color, linewidth=1.6, zorder=6)
                ax.plot([cx - d, cx + d], [cy + d, cy - d], color=color, linewidth=1.6, zorder=6)
            _track(cx - radius, cy - radius)
            _track(cx + radius, cy + radius)
            if el.label:
                ax.text(
                    cx,
                    cy - radius * 1.45,
                    el.label,
                    color=color,
                    fontsize=9.5,
                    fontweight="bold",
                    ha="center",
                    va="top",
                    zorder=6,
                )
                _track(cx, cy - radius * 1.9)

        elif kind in ("loop", "coil", "field_circle"):
            cx, cy = _validate_point(el.at, f"elements[{idx}].at") if el.at else (0.0, 0.0)
            radius = float(el.length) if el.length is not None else 1.0
            ls = str(style.get("ls", "--" if kind == "field_circle" else "-"))
            circ = Circle(
                (cx, cy),
                radius,
                fill=False,
                edgecolor=color,
                linestyle=ls,
                linewidth=float(style.get("lw", 1.5)),
                zorder=3,
            )
            ax.add_patch(circ)
            sense = str(style.get("sense", "ccw")).lower()
            sign = 1.0 if sense == "ccw" else -1.0
            for deg in style.get("arrow_angles_deg", (90, 270)):
                rad = math.radians(float(deg))
                px = cx + radius * math.cos(rad)
                py = cy + radius * math.sin(rad)
                tx = -sign * math.sin(rad)
                ty = sign * math.cos(rad)
                step = 0.08 * radius
                arr = FancyArrowPatch(
                    (px - step * tx, py - step * ty),
                    (px + step * tx, py + step * ty),
                    arrowstyle="-|>,head_length=7,head_width=4",
                    color=color,
                    linewidth=1.4,
                    zorder=4,
                )
                ax.add_patch(arr)
            _track(cx - radius, cy - radius)
            _track(cx + radius, cy + radius)
            if el.label:
                ax.text(
                    cx + 0.78 * radius,
                    cy + 0.78 * radius,
                    el.label,
                    color=color,
                    fontsize=9.2,
                    ha="left",
                    va="bottom",
                )

        elif kind in ("wire", "line", "conductor", "field_line"):
            (x0, y0), (x1, y1) = _resolve_vector_endpoints(el, idx)
            _track(x0, y0)
            _track(x1, y1)
            ls = str(style.get("ls", "--" if kind == "field_line" else "-"))
            lw = float(style.get("lw", 1.6 if kind == "field_line" else 2.4))
            ax.plot([x0, x1], [y0, y1], color=color, linestyle=ls, linewidth=lw, zorder=3)
            if kind == "field_line" or style.get("show_arrow"):
                mx, my = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
                dx, dy = x1 - x0, y1 - y0
                norm = math.hypot(dx, dy) or 1.0
                step = min(0.18, 0.15 * norm)
                arr = FancyArrowPatch(
                    (mx - step * dx / norm, my - step * dy / norm),
                    (mx + step * dx / norm, my + step * dy / norm),
                    arrowstyle="-|>,head_length=7,head_width=4",
                    color=color,
                    linewidth=1.5,
                    zorder=4,
                )
                ax.add_patch(arr)
            if el.label:
                mx, my = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
                ax.text(mx, my + 0.12, el.label, color=color, fontsize=9.2, ha="center")

        elif kind in ("dipole", "magnet"):
            cx, cy = _validate_point(el.at, f"elements[{idx}].at") if el.at else (0.0, 0.0)
            w = float(el.length) if el.length is not None else 1.6
            h = float(style.get("height", 0.45))
            ax.add_patch(
                Rectangle(
                    (cx - w / 2, cy - h / 2),
                    w / 2,
                    h,
                    facecolor="#c0392b",
                    edgecolor="#1e293b",
                    linewidth=1.2,
                    zorder=4,
                )
            )
            ax.add_patch(
                Rectangle(
                    (cx, cy - h / 2),
                    w / 2,
                    h,
                    facecolor="#1f4e79",
                    edgecolor="#1e293b",
                    linewidth=1.2,
                    zorder=4,
                )
            )
            ax.text(
                cx - w / 4,
                cy,
                "N",
                color="#ffffff",
                fontsize=9.5,
                fontweight="bold",
                ha="center",
                va="center",
                zorder=5,
            )
            ax.text(
                cx + w / 4,
                cy,
                "S",
                color="#ffffff",
                fontsize=9.5,
                fontweight="bold",
                ha="center",
                va="center",
                zorder=5,
            )
            _track(cx - w / 2, cy - h / 2)
            _track(cx + w / 2, cy + h / 2)

        elif kind in ("point", "dot", "label", "text"):
            px, py = _validate_point(el.at, f"elements[{idx}].at") if el.at else (0.0, 0.0)
            _track(px, py)
            if kind in ("point", "dot"):
                ax.scatter([px], [py], color=color, s=36, zorder=5)
            if el.label:
                ax.text(
                    px + 0.08,
                    py + 0.08,
                    el.label,
                    color=color,
                    fontsize=9.2,
                    ha="left",
                    va="bottom",
                    zorder=6,
                )
        else:
            raise DiagramError(
                f"Unsupported field element kind {el.kind!r} at index {idx}; "
                "supported: vector, arrow, field_arrow, wire_in, wire_out, loop, coil, "
                "field_circle, wire, line, conductor, field_line, dipole, magnet, point, dot, label"
            )

    if has_bounds:
        span_x = max(bounds[1] - bounds[0], 0.8)
        span_y = max(bounds[3] - bounds[2], 0.8)
        pad_x = 0.25 * span_x
        pad_y = 0.25 * span_y
        ax.set_xlim(bounds[0] - pad_x, bounds[1] + pad_x)
        ax.set_ylim(bounds[2] - pad_y, bounds[3] + pad_y)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, linestyle="--", linewidth=0.55, alpha=0.25, color="#94a3b8")
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    if spec.title:
        ax.set_title(spec.title, fontsize=11.5, fontweight="bold", pad=10)
    if spec.notes:
        fig.text(
            0.5,
            0.01,
            "  |  ".join(spec.notes),
            ha="center",
            va="bottom",
            fontsize=8.5,
            color="#334155",
        )


def render_diagram(
    spec: DiagramSpec | dict[str, Any],
    workspace: RunWorkspace,
    *,
    config: PeaConfig | None = None,
    source: ProvenanceSource = "model_computed",
    dpi: int = _DEFAULT_DIAGRAM_DPI,
    overwrite: bool = False,
) -> ReportFigure:
    """Render a ``DiagramSpec`` (``circuit``, ``phasor``, or ``field``) to ``.svg`` and ``.png``.

    Both output files are written inside ``<run>/figures/`` and hashed in ``provenance.jsonl``.
    """
    diag_spec = spec if isinstance(spec, DiagramSpec) else DiagramSpec.model_validate(spec)
    diagram_id = validate_figure_id(diag_spec.diagram_id)

    workspace.ensure_dirs()
    svg_rel = f"figures/{diagram_id}.svg"
    png_rel = f"figures/{diagram_id}.png"
    svg_path = workspace.resolve_path(svg_rel)
    png_path = workspace.resolve_path(png_rel)

    if not overwrite and (svg_path.exists() or png_path.exists()):
        raise FileExistsError(
            f"Diagram artifacts for {diagram_id!r} already exist in run {workspace.run_id}; "
            "choose a unique diagram_id to preserve provenance immutability"
        )

    font_stack = configure_bundled_fonts(config)
    with matplotlib.rc_context(academic_rc_params(font_stack)):
        figsize = (6.4, 4.2) if diag_spec.kind == "circuit" else (5.8, 4.8)
        fig = Figure(figsize=figsize, dpi=dpi)
        try:
            if diag_spec.kind == "circuit":
                _render_circuit(diag_spec, fig)
            elif diag_spec.kind == "phasor":
                _render_phasor(diag_spec, fig)
            elif diag_spec.kind == "field":
                _render_field(diag_spec, fig)
            else:
                raise DiagramError(f"Unsupported diagram kind: {diag_spec.kind!r}")

            _save_figure_dual(fig, svg_path, png_path, dpi=dpi)
        finally:
            fig.clear()

    workspace.record_artifact(
        svg_rel,
        kind="diagram_svg",
        metadata={"diagram_id": diagram_id, "kind": diag_spec.kind, "title": diag_spec.title},
        source=source,
    )
    workspace.record_artifact(
        png_rel,
        kind="diagram_png",
        metadata={
            "diagram_id": diagram_id,
            "kind": diag_spec.kind,
            "title": diag_spec.title,
            "dpi": dpi,
        },
        source=source,
    )

    return ReportFigure(
        figure_id=diagram_id,
        caption=diag_spec.title or diagram_id,
        svg_relpath=svg_rel,
        png_relpath=png_rel,
        source=source,
    )


__all__ = [
    "DiagramError",
    "render_diagram",
]
