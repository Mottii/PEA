"""Offline unit tests for Phase 2B vector plots (Matplotlib) and diagrams (schemdraw/SVG)."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

from pea.config import PathConfinementError
from pea.schemas import DiagramElement, DiagramSpec, PlotSeries, PlotSpec
from pea.visuals import DiagramError, render_diagram, render_plot
from pea.workspace import WorkspaceManager


def _assert_valid_svg_and_png(svg_path: Path, png_path: Path) -> None:
    assert svg_path.is_file()
    assert png_path.is_file()
    assert svg_path.stat().st_size > 200
    assert png_path.stat().st_size > 500

    # Verify SVG is well-formed XML with <svg> root
    root = ET.fromstring(svg_path.read_text(encoding="utf-8"))
    assert root.tag.endswith("svg")

    # Verify PNG header and decode via Pillow
    raw_png = png_path.read_bytes()
    assert raw_png[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(png_path) as img:
        w, h = img.size
        assert w > 200 and h > 150


class TestRenderPlot:
    def test_cyrillic_and_en_regression_plot_with_provenance(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="plot-test")

        xs = [10.0, 16.7, 25.0, 50.0]
        ys = [20.1, 33.4, 50.2, 100.1]
        fit_y = [2.0 * x for x in xs]

        spec = PlotSpec(
            plot_id="lab4-b-vs-inv-r",
            title="Зависимость магнитной индукции B(1/r) — I = 10 А",
            x_label=r"Обратное расстояние $1/r$, м$^{-1}$",
            y_label=r"Магнитная индукция $B$, мкТл",
            series=[
                PlotSeries(
                    label="Эксперимент (m96210)",
                    x=xs,
                    y=ys,
                    y_err=[0.5, 0.6, 0.8, 1.0],
                    kind="errorbar",
                ),
                PlotSeries(
                    label=r"МНК: $B = 2.00 \cdot (1/r)$",
                    x=xs,
                    y=fit_y,
                    kind="fit_line",
                ),
            ],
            annotations=[
                r"$R^2 = 0.9999$",
                r"$\mu_0 = 1.257 \times 10^{-6}\ \mathrm{Тл\cdot м/А}$",
            ],
            dpi=300,
        )

        fig = render_plot(spec, ws)
        assert fig.figure_id == "lab4-b-vs-inv-r"
        assert fig.svg_relpath == "figures/lab4-b-vs-inv-r.svg"
        assert fig.png_relpath == "figures/lab4-b-vs-inv-r.png"

        _assert_valid_svg_and_png(
            ws.resolve_path(fig.svg_relpath),
            ws.resolve_path(fig.png_relpath),
        )
        ver = ws.verify_provenance()
        assert ver.ok
        assert ver.checked_entries == 2

    def test_equal_aspect_phase_portrait_ellipse(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="ellipse-test")

        angles = [2.0 * math.pi * k / 120 for k in range(121)]
        q_vals = [1.5 * math.cos(t) for t in angles]
        i_vals = [1.5 * math.sin(t) for t in angles]

        spec = PlotSpec(
            plot_id="problem-4c-ellipse",
            title=r"Phase Portrait $I(q)$ (Problem 4C)",
            x_label=r"$q / q_0$",
            y_label=r"$I / I_0$",
            series=[
                PlotSeries(label="Resonant orbit", x=q_vals, y=i_vals, kind="line"),
                PlotSeries(
                    label="Sample points",
                    x=[1.5, 0.0, -1.5, 0.0],
                    y=[0.0, 1.5, 0.0, -1.5],
                    kind="scatter",
                ),
            ],
            equal_aspect=True,
        )

        fig = render_plot(spec, ws, source="model_computed")
        assert fig.source == "model_computed"
        _assert_valid_svg_and_png(
            ws.resolve_path(fig.svg_relpath or ""),
            ws.resolve_path(fig.png_relpath or ""),
        )
        assert ws.verify_provenance().ok

    def test_plot_guards_reject_traversal_overwrite_and_non_finite(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="plot-guards")

        with pytest.raises(PathConfinementError):
            render_plot(
                PlotSpec(
                    plot_id="../evil",
                    title="Bad",
                    x_label="x",
                    y_label="y",
                    series=[PlotSeries(label="s", x=[1.0], y=[2.0])],
                ),
                ws,
            )

        with pytest.raises(ValueError):
            render_plot(
                PlotSpec(
                    plot_id="nan-plot",
                    title="NaN",
                    x_label="x",
                    y_label="y",
                    series=[PlotSeries(label="s", x=[1.0, float("nan")], y=[2.0, 3.0])],
                ),
                ws,
            )

        valid = PlotSpec(
            plot_id="dup-plot",
            title="Once",
            x_label="x",
            y_label="y",
            series=[PlotSeries(label="s", x=[1.0, 2.0], y=[3.0, 4.0])],
        )
        render_plot(valid, ws)
        with pytest.raises(FileExistsError):
            render_plot(valid, ws)


class TestRenderDiagram:
    def test_schemdraw_lab5_rlc_circuit(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="circuit-test")

        spec = DiagramSpec(
            diagram_id="lab5-fig1-rlc",
            kind="circuit",
            title="Последовательный RLC-контур (Рис. 1)",
            elements=[
                DiagramElement(
                    kind="source_sin",
                    direction="up",
                    label=r"$\varepsilon_0 \cos\omega t$",
                ),
                DiagramElement(kind="switch", direction="right", label=r"$K$"),
                DiagramElement(kind="ammeter", direction="right", label=r"$A$"),
                DiagramElement(kind="resistor", direction="right", label=r"$R$"),
                DiagramElement(kind="inductor", direction="right", label=r"$L$"),
                DiagramElement(kind="capacitor", direction="down", label=r"$C$"),
                DiagramElement(kind="line", direction="left", to=(0.0, 0.0)),
                DiagramElement(kind="dot", at=(0.0, 0.0)),
                DiagramElement(kind="ground", at=(0.0, 0.0)),
            ],
            notes=[r"$U_{0C}, U_{0L}, U_{0R}$ — амплитуды напряжений на элементах"],
        )

        fig = render_diagram(spec, ws)
        assert fig.figure_id == "lab5-fig1-rlc"
        assert fig.source == "model_computed"
        _assert_valid_svg_and_png(
            ws.resolve_path(fig.svg_relpath or ""),
            ws.resolve_path(fig.png_relpath or ""),
        )
        assert ws.verify_provenance().ok

    def test_phasor_diagram_and_ac_waveform(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="phasor-test")

        phasor_spec = DiagramSpec(
            diagram_id="rlc-phasor",
            kind="phasor",
            title="Векторная диаграмма напряжений RLC",
            elements=[
                DiagramElement(kind="phasor", at=(0.0, 0.0), to=(3.2, 0.0), label=r"$U_{0R}$"),
                DiagramElement(kind="phasor", at=(0.0, 0.0), to=(0.0, 2.5), label=r"$U_{0L}$"),
                DiagramElement(kind="phasor", at=(0.0, 0.0), to=(0.0, -1.5), label=r"$U_{0C}$"),
                DiagramElement(
                    kind="phasor",
                    at=(0.0, 0.0),
                    to=(3.2, 1.0),
                    label=r"$\varepsilon_0$",
                ),
                DiagramElement(kind="projection", at=(3.2, 0.0), to=(3.2, 1.0)),
                DiagramElement(
                    kind="arc",
                    at=(0.0, 0.0),
                    length=0.9,
                    label=r"$\varphi$",
                    style={"start_deg": 0.0, "end_deg": 17.35},
                ),
            ],
        )
        fig_phasor = render_diagram(phasor_spec, ws)
        _assert_valid_svg_and_png(
            ws.resolve_path(fig_phasor.svg_relpath or ""),
            ws.resolve_path(fig_phasor.png_relpath or ""),
        )

        wave_spec = DiagramSpec(
            diagram_id="ac-waves",
            kind="phasor",
            title="AC Voltage and Current Waveforms",
            elements=[
                DiagramElement(
                    kind="cosine",
                    label=r"$u(t) = U_0 \cos(\omega t)$",
                    style={"amplitude": 1.0, "omega": 2.0 * math.pi, "t_min": 0.0, "t_max": 2.0},
                ),
                DiagramElement(
                    kind="cosine",
                    label=r"$i(t) = I_0 \cos(\omega t - \varphi)$",
                    style={
                        "amplitude": 0.7,
                        "omega": 2.0 * math.pi,
                        "phase_deg": -35.0,
                        "t_min": 0.0,
                        "t_max": 2.0,
                    },
                ),
            ],
        )
        fig_wave = render_diagram(wave_spec, ws)
        _assert_valid_svg_and_png(
            ws.resolve_path(fig_wave.svg_relpath or ""),
            ws.resolve_path(fig_wave.png_relpath or ""),
        )
        assert ws.verify_provenance().ok

    def test_magnetic_field_diagram(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="field-test")

        field_spec = DiagramSpec(
            diagram_id="wire-magnetic-field",
            kind="field",
            title="Магнитное поле прямого проводника с током",
            elements=[
                DiagramElement(kind="wire_out", at=(0.0, 0.0), length=0.25, label=r"$I$"),
                DiagramElement(
                    kind="field_circle",
                    at=(0.0, 0.0),
                    length=1.0,
                    style={"sense": "ccw"},
                ),
                DiagramElement(
                    kind="field_circle",
                    at=(0.0, 0.0),
                    length=1.8,
                    style={"sense": "ccw"},
                ),
                DiagramElement(
                    kind="field_arrow",
                    at=(1.0, 0.0),
                    to=(1.0, 0.75),
                    label=r"$\vec{B}$",
                ),
                DiagramElement(kind="point", at=(1.0, 0.0), label=r"$r$"),
            ],
            notes=[r"$B = \mu_0 I / (2\pi r)$"],
        )
        fig = render_diagram(field_spec, ws)
        _assert_valid_svg_and_png(
            ws.resolve_path(fig.svg_relpath or ""),
            ws.resolve_path(fig.png_relpath or ""),
        )
        assert ws.verify_provenance().ok

    def test_diagram_rejects_unknown_element_and_traversal(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="diag-guards")

        with pytest.raises(DiagramError):
            render_diagram(
                DiagramSpec(
                    diagram_id="bad-el",
                    kind="circuit",
                    elements=[DiagramElement(kind="flux_capacitor")],
                ),
                ws,
            )

        with pytest.raises(PathConfinementError):
            render_diagram(
                DiagramSpec(
                    diagram_id="../../escape",
                    kind="field",
                    elements=[DiagramElement(kind="point", at=(0.0, 0.0))],
                ),
                ws,
            )
