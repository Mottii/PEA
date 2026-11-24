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
    _assert_provenance_safe_write,
    _save_figure_dual,
    academic_rc_params,
    configure_bundled_fonts,
    validate_figure_id,
)

if TYPE_CHECKING:
    from pea.workspace import RunWorkspace

_DEFAULT_DIAGRAM_DPI = 300
_MIN_DIAGRAM_DPI = 72
_MAX_DIAGRAM_DPI = 600
_MIN_WAVE_POINTS = 16
_MAX_WAVE_POINTS = 10_000

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


def _require_finite(val: Any, context: str) -> float:
    if isinstance(val, bool):
        raise DiagramError(f"Expected numeric value for {context}; got boolean {val!r}")
    try:
        out = float(val)
    except (TypeError, ValueError) as exc:
        raise DiagramError(f"Invalid numeric value for {context}: {val!r}") from exc
    if not math.isfinite(out):
        raise DiagramError(f"Non-finite numeric value for {context}: {val!r}")
    return out


def _validate_point(pt: tuple[float, float], context: str) -> tuple[float, float]:
    x = _require_finite(pt[0], f"{context}[0]")
    y = _require_finite(pt[1], f"{context}[1]")
    return (x, y)


def _apply_circuit_style(element: Any, style: dict[str, Any]) -> Any:
    if "color" in style:
        element = element.color(str(style["color"]))
    lw = style.get("lw", style.get("linewidth"))
    if lw is not None:
        lw_val = _require_finite(lw, "style.linewidth")
        if lw_val <= 0.0:
            raise DiagramError(f"style.linewidth must be positive; got {lw_val!r}")
        element = element.linewidth(lw_val)
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

            start_pt: tuple[float, float] | None = None
            if el.at is not None:
                start_pt = _validate_point(el.at, f"elements[{idx}].at")
                obj = obj.at(start_pt)
            if el.to is not None:
                end_pt = _validate_point(el.to, f"elements[{idx}].to")
                if start_pt is not None and start_pt == end_pt:
                    raise DiagramError(
                        f"Circuit element at index {idx} has identical 'at' and 'to': {start_pt}"
                    )
                # Source/Meter subclasses in schemdraw==0.23 default elmparams['theta']=90,
                # which would override .to() orientation unless cleared first.
                obj.elmparams.pop("theta", None)
                obj.elmparams.pop("d", None)
                obj = obj.to(end_pt)
            else:
                obj = getattr(obj, el.direction)()
                if el.length is not None:
                    obj = obj.length(_require_finite(el.length, f"elements[{idx}].length"))

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
        ax.set_title(spec.title, fontsize=11.5, fontweight="bold", pad=24)
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
        mag = _require_finite(style["magnitude"], f"elements[{idx}].style.magnitude")
        if "angle_rad" in style:
            ang = _require_finite(style["angle_rad"], f"elements[{idx}].style.angle_rad")
        else:
            ang = math.radians(
                _require_finite(style.get("angle_deg", 0.0), f"elements[{idx}].style.angle_deg")
            )
        end = (start[0] + mag * math.cos(ang), start[1] + mag * math.sin(ang))
        return start, end

    length = _require_finite(el.length, f"elements[{idx}].length") if el.length is not None else 1.0
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


_TEXT_HALO: dict[str, Any] = {
    "facecolor": "#ffffff",
    "edgecolor": "none",
    "alpha": 0.85,
    "pad": 1.2,
}


def _resolve_label_offset(style: dict[str, Any], ctx: str) -> tuple[float, float] | None:
    raw = style.get("label_offset")
    if raw is None:
        return None
    return _validate_point(raw, f"{ctx}.style.label_offset")


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
                # Offset label cleanly past tip or perpendicular to midpoint, with optional override
                custom_off = _resolve_label_offset(style, f"elements[{idx}]")
                if custom_off is not None:
                    base_x, base_y = (
                        (0.5 * (x0 + x1), 0.5 * (y0 + y1))
                        if style.get("label_pos") == "mid"
                        else (x1, y1)
                    )
                    lx, ly = base_x + custom_off[0], base_y + custom_off[1]
                elif style.get("label_pos") == "mid":
                    lx = 0.5 * (x0 + x1) - 0.22 * dy / norm
                    ly = 0.5 * (y0 + y1) + 0.22 * dx / norm
                else:
                    offset = max(0.22, 0.09 * norm)
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
                    ha=str(style.get("ha", "center")),
                    va=str(style.get("va", "center")),
                    bbox=_TEXT_HALO,
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
            radius = (
                _require_finite(el.length, f"elements[{idx}].length")
                if el.length is not None
                else 0.35
            )
            start_deg = _require_finite(
                style.get("start_deg", 0.0), f"elements[{idx}].style.start_deg"
            )
            end_deg = _require_finite(
                style.get("end_deg", 45.0), f"elements[{idx}].style.end_deg"
            )
            theta1, theta2 = (start_deg, end_deg) if end_deg >= start_deg else (end_deg, start_deg)
            arc = Arc(
                (cx, cy),
                2.0 * radius,
                2.0 * radius,
                angle=0.0,
                theta1=theta1,
                theta2=theta2,
                color=color,
                linewidth=_require_finite(style.get("lw", 1.4), f"elements[{idx}].style.lw"),
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
            radius = (
                _require_finite(el.length, f"elements[{idx}].length")
                if el.length is not None
                else 1.0
            )
            circle = Circle(
                (cx, cy),
                radius,
                fill=False,
                edgecolor=color,
                linestyle=str(style.get("ls", ":")),
                linewidth=_require_finite(style.get("lw", 1.1), f"elements[{idx}].style.lw"),
                zorder=2,
            )
            ax.add_patch(circle)
            _track(cx - radius, cy - radius)
            _track(cx + radius, cy + radius)

        elif kind in ("wave", "sine", "cosine"):
            amp = _require_finite(
                style.get("amplitude", 1.0), f"elements[{idx}].style.amplitude"
            )
            omega = _require_finite(
                style.get("omega", 2.0 * math.pi), f"elements[{idx}].style.omega"
            )
            phase = (
                _require_finite(style["phase_rad"], f"elements[{idx}].style.phase_rad")
                if "phase_rad" in style
                else math.radians(
                    _require_finite(
                        style.get("phase_deg", 0.0), f"elements[{idx}].style.phase_deg"
                    )
                )
            )
            t_min = _require_finite(style.get("t_min", 0.0), f"elements[{idx}].style.t_min")
            t_max = _require_finite(style.get("t_max", 2.0), f"elements[{idx}].style.t_max")
            if t_max <= t_min:
                raise DiagramError(
                    f"Wave element at index {idx} requires t_max > t_min; got [{t_min}, {t_max}]"
                )
            raw_pts = style.get("points", 400)
            if isinstance(raw_pts, bool) or not isinstance(raw_pts, int):
                raise DiagramError(f"Wave points must be an integer; got {raw_pts!r}")
            if not (_MIN_WAVE_POINTS <= raw_pts <= _MAX_WAVE_POINTS):
                raise DiagramError(
                    f"Wave points must lie in [{_MIN_WAVE_POINTS}, {_MAX_WAVE_POINTS}]; "
                    f"got {raw_pts}"
                )
            t = np.linspace(t_min, t_max, raw_pts)
            fn_name = str(style.get("func", "sin" if kind == "sine" else "cos")).lower()
            y = amp * (np.sin(omega * t + phase) if fn_name == "sin" else np.cos(omega * t + phase))
            ls = str(style.get("ls", style.get("linestyle", "-")))
            lw = _require_finite(
                style.get("lw", style.get("linewidth", 1.9)), f"elements[{idx}].style.lw"
            )
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
                custom_off = _resolve_label_offset(style, f"elements[{idx}]")
                if custom_off is not None:
                    base_x, base_y = (
                        (0.5 * (x0 + x1), 0.5 * (y0 + y1))
                        if style.get("label_pos") == "mid"
                        else (x1, y1)
                    )
                    lx, ly = base_x + custom_off[0], base_y + custom_off[1]
                elif style.get("label_pos") == "mid":
                    lx = 0.5 * (x0 + x1) - 0.24 * dy / norm
                    ly = 0.5 * (y0 + y1) + 0.24 * dx / norm
                else:
                    offset = max(0.28, 0.14 * norm)
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
                    ha=str(style.get("ha", "center")),
                    va=str(style.get("va", "center")),
                    bbox=_TEXT_HALO,
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
            w = (
                _require_finite(el.length, f"elements[{idx}].length")
                if el.length is not None
                else 1.6
            )
            h = _require_finite(style.get("height", 0.45), f"elements[{idx}].style.height")
            if h <= 0.0:
                raise DiagramError(f"Dipole height must be positive; got {h!r}")
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
                custom_off = _resolve_label_offset(style, f"elements[{idx}]")
                if custom_off is not None:
                    lx, ly = px + custom_off[0], py + custom_off[1]
                elif kind in ("point", "dot"):
                    lx, ly = px + 0.10, py + 0.12
                else:
                    lx, ly = px, py
                _track(lx, ly)
                ax.text(
                    lx,
                    ly,
                    el.label,
                    color=color,
                    fontsize=9.2,
                    ha=str(style.get("ha", "left")),
                    va=str(style.get("va", "bottom")),
                    bbox=_TEXT_HALO,
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
    if isinstance(dpi, bool) or not (_MIN_DIAGRAM_DPI <= dpi <= _MAX_DIAGRAM_DPI):
        raise DiagramError(
            f"Diagram dpi must lie in [{_MIN_DIAGRAM_DPI}, {_MAX_DIAGRAM_DPI}]; got {dpi!r}"
        )

    workspace.ensure_dirs()
    svg_rel = f"figures/{diagram_id}.svg"
    png_rel = f"figures/{diagram_id}.png"
    svg_path = workspace.resolve_path(svg_rel)
    png_path = workspace.resolve_path(png_rel)

    _assert_provenance_safe_write(
        workspace,
        (svg_rel, png_rel),
        entity_label=f"diagram {diagram_id!r}",
        overwrite=overwrite,
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
        caption=diag_spec.title.strip() or diagram_id,
        svg_relpath=svg_rel,
        png_relpath=png_rel,
        source=source,
    )


__all__ = [
    "DiagramError",
    "render_diagram",
]
