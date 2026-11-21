"""Tests for offline HTML+KaTeX PDF compiler, editable DOCX builder, previews, and evidence."""

from __future__ import annotations

import hashlib
import json
import unicodedata
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import docx
import pypdf
import pytest
from PIL import Image

from pea.config import PathConfinementError, PeaConfig
from pea.math_engine.calculator import calc_eval
from pea.report import (
    build_evidence_zip,
    build_report,
    build_report_docx,
    build_report_pdf,
    build_teacher_evidence_pdf,
    latex_to_readable_text,
    preview_report_pages,
    render_html_to_pdf,
    render_report_html,
    render_teacher_evidence_html,
)
from pea.report.pdf_builder import _OfflineRenderServer
from pea.schemas import (
    CalcRequest,
    CalcVariable,
    DiagramElement,
    DiagramSpec,
    PlotSeries,
    PlotSpec,
    ProblemItem,
    ReportFigure,
    ReportSectionOptions,
    ReportSpec,
    ReportTable,
    SelfControlItem,
)
from pea.visuals import render_diagram, render_plot
from pea.workspace import RunWorkspace, WorkspaceManager, sha256_file


def _make_config(tmp_path: Path) -> PeaConfig:
    base = PeaConfig.from_env()
    return PeaConfig(
        project_root=base.project_root,
        local_assets_dir=base.local_assets_dir,
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
        loopback_host="127.0.0.1",
        viewer_port=5190,
        allowed_origins=(),
        ocr_backend=base.ocr_backend,
        ocr_threads=base.ocr_threads,
        ocr_timeout_s=base.ocr_timeout_s,
        browser_executable=base.browser_executable,
    )


def _create_sample_visuals_and_calcs(
    ws: RunWorkspace,
    cfg: PeaConfig,
    *,
    language: str = "en",
) -> dict[str, object]:
    """Generate real Matplotlib plot, schemdraw circuit, calc ledger entries, and evidence PNGs."""
    # 1. Verified calculations in calculations.jsonl
    c_w0 = calc_eval(
        CalcRequest(
            expression="1 / sqrt(L * C)",
            variables={
                "L": CalcVariable(value=5.0, unit="mH", uncertainty=0.05),
                "C": CalcVariable(value=32.0, unit="uF", uncertainty=0.2),
            },
            output_unit="rad/s",
        ),
        workspace=ws,
    )
    c_q = calc_eval(
        CalcRequest(
            expression="sqrt(L / C) / R",
            variables={
                "L": CalcVariable(value=5.0, unit="mH"),
                "C": CalcVariable(value=32.0, unit="uF"),
                "R": CalcVariable(value=3.2, unit="Ohm"),
            },
            output_unit="1",
        ),
        workspace=ws,
    )

    # 2. Real Matplotlib plot (resonance / linearized fit)
    if language == "ru":
        plot_title = "Зависимость ω₀ / Δω от Q² (Бригада 2)"
        x_label = "Квадрат добротности Q²"
        y_label = "Отношение ω₀ / Δω"
        series_label = "Эксперимент (m96220)"
        fit_label = "Линейная аппроксимация"
    else:
        plot_title = "Resonance Sharpness ω₀ / Δω vs Q² (Team 2)"
        x_label = "Quality Factor Squared Q²"
        y_label = "Ratio ω₀ / Δω"
        series_label = "Measured (m96220)"
        fit_label = "Linear Fit (4Q²)"

    fig_plot = render_plot(
        PlotSpec(
            plot_id="fig_q2_fit",
            title=plot_title,
            x_label=x_label,
            y_label=y_label,
            series=[
                PlotSeries(
                    label=series_label,
                    x=[4.34, 6.25, 9.77, 15.26],
                    y=[17.3, 25.1, 38.9, 61.2],
                    kind="scatter",
                ),
                PlotSeries(
                    label=fit_label,
                    x=[4.0, 16.0],
                    y=[16.0, 64.0],
                    kind="fit_line",
                ),
            ],
            annotations=["Slope = 4.01 ± 0.03 (R² = 0.9999)"],
        ),
        ws,
        config=cfg,
        source="simulator_captured",
    )

    # 3. Real schemdraw RLC circuit schematic
    fig_circuit = render_diagram(
        DiagramSpec(
            diagram_id="fig_rlc_circuit",
            kind="circuit",
            title="Series RLC Circuit" if language == "en" else "Последовательный RLC-контур",
            elements=[
                DiagramElement(
                    kind="source_sin",
                    label=r"$\varepsilon_0 \cos\omega t$",
                    direction="up",
                ),
                DiagramElement(kind="resistor", label=r"$R$", direction="right"),
                DiagramElement(kind="inductor", label=r"$L$", direction="right"),
                DiagramElement(kind="capacitor", label=r"$C$", direction="down"),
                DiagramElement(kind="line", direction="left"),
                DiagramElement(kind="line", direction="left"),
            ],
        ),
        ws,
        config=cfg,
        source="model_computed",
    )

    # 4. Real equal-aspect q-I ellipse figure for opt-in Problem / Q&A
    import math

    thetas = [2.0 * math.pi * i / 60 for i in range(61)]
    fig_ellipse = render_plot(
        PlotSpec(
            plot_id="fig_qa_ellipse",
            title=(
                "Phase Portrait q(t) – I(t)"
                if language == "en"
                else "Фазовый портрет q(t) – I(t)"
            ),
            x_label="q / q_max",
            y_label="I / I_max",
            series=[
                PlotSeries(
                    label="Harmonic trajectory",
                    x=[math.cos(t) for t in thetas],
                    y=[-math.sin(t) for t in thetas],
                    kind="line",
                )
            ],
            equal_aspect=True,
        ),
        ws,
        config=cfg,
        source="model_computed",
    )

    # 5. Raw simulator evidence PNGs + dataset + scratch .tmp file
    for idx, color in enumerate(((31, 78, 121), (46, 125, 50)), start=1):
        shot_rel = f"evidence/capture-{idx:03d}.png"
        shot_path = ws.resolve_path(shot_rel)
        img = Image.new("RGB", (640, 360), color=color)
        img.save(shot_path, format="PNG")
        img.close()
        ws.record_artifact(
            shot_rel,
            kind="sim_screenshot",
            source="simulator_captured",
            metadata={"step": idx},
        )

    datasets_dir = ws.resolve_path("datasets")
    datasets_dir.mkdir(parents=True, exist_ok=True)
    ds_file = datasets_dir / "rlc-sweep.json"
    ds_file.write_text(json.dumps({"rows": [{"R": 3.2, "omega0": 2458.5}]}), encoding="utf-8")
    ws.record_artifact("datasets/rlc-sweep.json", kind="dataset", source="simulator_captured")

    # Scratch .tmp file that MUST be excluded from evidence.zip
    (ws.evidence_dir / "scratch.png.tmp").write_bytes(b"temporary partial bytes")

    return {
        "c_w0": c_w0,
        "c_q": c_q,
        "fig_plot": fig_plot,
        "fig_circuit": fig_circuit,
        "fig_ellipse": fig_ellipse,
    }


def _build_en_spec(assets: dict[str, object], *, opt_in: bool = True) -> ReportSpec:
    fig_plot = assets["fig_plot"]
    fig_circuit = assets["fig_circuit"]
    fig_ellipse = assets["fig_ellipse"]
    c_w0 = assets["c_w0"]
    c_q = assets["c_q"]
    assert isinstance(fig_plot, ReportFigure)
    assert isinstance(fig_circuit, ReportFigure)
    assert isinstance(fig_ellipse, ReportFigure)

    return ReportSpec(
        title="Forced Oscillations and Resonance in a Series RLC Circuit",
        subtitle="Experimental Verification of Voltage Resonance and Quality Factor Scaling",
        lab_number="5",
        language="en",
        author="Student Researcher",
        team="Team 2 (L = 5.0 mH, C = 32 µF)",
        course="General Physics II — Electromagnetism",
        institution="Department of Physics",
        date_str="2026-11-21",
        objective=(
            "Investigate forced harmonic oscillations in a series $RLC$ circuit, measure "
            "the resonant angular frequency $\\omega_0$ and quality factor $Q$ across "
            "damping resistances $R \\in \\{3.2, 4.0, 5.0, 6.0\\}\\,\\Omega$, and verify "
            "the scaling law $\\omega_0 / \\Delta\\omega = 4Q^2$."
        ),
        apparatus=[
            "Virtual AC sinusoidal generator $\\varepsilon(t) = \\varepsilon_0 \\cos(\\omega t)$",
            "Adjustable inductance $L = 5.0\\text{ mH}$, capacitance $C = 32\\,\\mu\\text{F}$, "
            "and resistance $R$",
            "Capacitor voltage amplitude indicator $U_{0C} / \\varepsilon_0$",
        ],
        theory_sections=[
            {
                "heading": "Impedance and Capacitor Voltage Resonance",
                "body": (
                    "For a series $RLC$ loop driven at angular frequency $\\omega$, the "
                    "complex impedance is $Z(\\omega) = R + j(\\omega L - 1/(\\omega C))$. "
                    "Voltage across the capacitor peaks slightly below $\\omega_0$ when $R > 0$."
                ),
                "equation": (
                    "\\omega_0 = \\frac{1}{\\sqrt{LC}}, \\qquad "
                    "\\omega_{C,\\max} = \\sqrt{\\omega_0^2 - \\frac{R^2}{2L^2}}"
                ),
            }
        ],
        procedure=[
            "Set $L = 5.0\\text{ mH}$ and $C = 32\\,\\mu\\text{F}$ on the simulator.",
            (
                "Locate the plateau bounds $[\\omega_1, \\omega_2]$ of maximum "
                "$U_{0C}/\\varepsilon_0$."
            ),
            "Compute $\\omega_0 / \\Delta\\omega$ and regress linearly against $Q^2$.",
        ],
        tables=[
            ReportTable(
                table_id="tbl_resonance",
                caption="Measured Capacitor Resonance Plateaus for Team 2",
                headers=["R (Ω)", "ω₁ (rad/s)", "ω₂ (rad/s)", "ω_mid (rad/s)", "Q²"],
                rows=[
                    ["3.2", "2445", "2472", "2458.5", "15.26"],
                    ["4.0", "2427", "2444", "2435.5", "9.77"],
                    ["5.0", "2361", "2434", "2397.5", "6.25"],
                    ["6.0", "2333", "2370", "2351.5", "4.34"],
                ],
                notes="All plateau endpoints captured via automated simulator search.",
                source="simulator_captured",
            )
        ],
        figures=[fig_circuit, fig_plot, fig_ellipse],
        analysis_paragraphs=[
            "Linear OLS regression of $\\omega_0 / \\Delta\\omega$ against $Q^2$ yields a slope "
            "consistent with the theoretical factor $4$ within experimental uncertainty."
        ],
        conclusions=[
            "Measured resonance midpoints agree closely with $\\omega_{C,\\max} < \\omega_0$.",
            "Quality factor decreases monotonically with increasing resistance $R$.",
        ],
        calc_ids=[c_w0.calc_id, c_q.calc_id],  # type: ignore[attr-defined]
        options=ReportSectionOptions(
            include_self_control=opt_in,
            include_problems=opt_in,
            include_evidence_appendix=opt_in,
        ),
        self_control=[
            SelfControlItem(
                number="1",
                question="Why is $\\omega_{C,\\max} < \\omega_0$ in a damped series RLC circuit?",
                answer=(
                    "Because $U_{0C}(\\omega) = I_0(\\omega)/(\\omega C)$ carries an extra "
                    "$1/\\omega$ factor that shifts the maximum downward by $R^2/(2L^2)$."
                ),
                equations=[r"\omega_{C,\max} = \sqrt{\omega_0^2 - \frac{R^2}{2L^2}}"],
                figure_ids=[],
            )
        ],
        problems=[
            ProblemItem(
                number="4C",
                prompt=(
                    "Sketch the normalized $q(t)$–$I(t)$ phase portrait for an undamped "
                    "LC circuit."
                ),
                solution=(
                    "Eliminating time from $q(t) = q_{\\max}\\cos(\\omega_0 t)$ and "
                    "$I(t) = -\\omega_0 q_{\\max}\\sin(\\omega_0 t)$ yields a closed ellipse "
                    "$(q/q_{\\max})^2 + (I/I_{\\max})^2 = 1$."
                ),
                calc_ids=[c_w0.calc_id],  # type: ignore[attr-defined]
                figure_ids=[fig_ellipse.figure_id],
            )
        ],
    )


def test_opt_in_sections_strictly_hidden_when_disabled_and_html_escaped(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    wm = WorkspaceManager.from_config(cfg)
    ws = wm.create_run(label="opt-in-guard")
    assets = _create_sample_visuals_and_calcs(ws, cfg, language="en")

    # Populate self_control & problems, but keep opt-in flags False
    spec = _build_en_spec(assets, opt_in=False)
    spec = spec.model_copy(
        update={
            "title": 'Lab <script>alert("xss")</script> & Resonance',
            "objective": "Verify $x < y$ and block <img src=x onerror=alert(1)>.",
        }
    )

    html_doc = render_report_html(spec, ws)
    # XSS payloads escaped
    assert "<script>alert(" not in html_doc
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in html_doc
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_doc
    # Math inequality preserved as escaped entity inside text node for KaTeX DOM decoding
    assert "$x &lt; y$" in html_doc

    # Opt-in sections strictly excluded
    assert "Self-Control Questions" not in html_doc
    assert "Additional Worked Problems" not in html_doc
    assert "Provenance &amp; Artifact Checksums" not in html_doc
    # Problem-linked figure does not leak into main results when opt-in is disabled
    assert "fig_qa_ellipse" not in html_doc

    # DOCX also excludes opt-in sections when disabled
    docx_res = build_report_docx(spec, ws, filename="no-optin.docx")
    assert docx_res.docx_relpath == "reports/no-optin.docx"
    parsed_doc = docx.Document(str(ws.resolve_path(docx_res.docx_relpath)))
    full_docx_text = "\n".join(p.text for p in parsed_doc.paragraphs)
    assert "Self-Control Questions" not in full_docx_text
    assert "Additional Worked Problems" not in full_docx_text
    assert len(parsed_doc.inline_shapes) == 2  # circuit + plot only, no ellipse


def test_validation_guards_and_latex_helper(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    wm = WorkspaceManager.from_config(cfg)
    ws = wm.create_run(label="validation-guards")
    assets = _create_sample_visuals_and_calcs(ws, cfg)
    spec = _build_en_spec(assets, opt_in=True)

    # Unknown figure_id in enabled SelfControlItem
    bad_spec = spec.model_copy(
        update={
            "self_control": [
                SelfControlItem(
                    number="1",
                    question="Q?",
                    answer="A.",
                    figure_ids=["nonexistent_fig"],
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="unknown figure_id"):
        render_report_html(bad_spec, ws)

    # Duplicate figure_id rejected
    fig_plot = assets["fig_plot"]
    assert isinstance(fig_plot, ReportFigure)
    dup_spec = spec.model_copy(update={"figures": [fig_plot, fig_plot]})
    with pytest.raises(ValueError, match="Duplicate figure_id"):
        render_report_html(dup_spec, ws)

    # Path traversal in output filenames rejected
    with pytest.raises(PathConfinementError):
        build_report_docx(spec, ws, filename="../escape.docx")
    with pytest.raises(PathConfinementError):
        build_evidence_zip(ws, filename="../escape.zip")

    # LaTeX readable conversion for DOCX
    readable = latex_to_readable_text(r"$$\omega_0 = \frac{1}{\sqrt{LC}}, \quad R = 5\,\Omega$$")
    assert "ω_0" in readable
    assert "√(LC)" in readable
    assert "Ω" in readable


@pytest.mark.asyncio
async def test_full_english_report_pdf_docx_teacher_and_evidence_zip(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    wm = WorkspaceManager.from_config(cfg)
    ws = wm.create_run(label="en-report-e2e")
    assets = _create_sample_visuals_and_calcs(ws, cfg, language="en")
    spec = _build_en_spec(assets, opt_in=True)

    # External calibration profile JSON to bundle in evidence.zip
    prof_file = tmp_path / "m96220-profile.json"
    prof_file.write_text(json.dumps({"schema_version": 1, "notes": "test"}), encoding="utf-8")

    result = await build_report(
        spec,
        ws,
        config=cfg,
        formats=("pdf", "docx", "evidence_zip"),
        include_teacher_packet=True,
        render_previews=True,
        preview_dpi=130,
        profile_paths=[prof_file],
    )

    assert result.run_id == ws.run_id
    assert result.pdf_relpath == "reports/report.pdf"
    assert result.pdf_sha256 is not None
    assert result.docx_relpath == "reports/report.docx"
    assert result.docx_sha256 is not None
    assert result.teacher_packet_relpath == "reports/teacher-evidence.pdf"
    assert result.teacher_packet_sha256 is not None
    assert result.evidence_zip_relpath == "reports/evidence.zip"
    assert result.evidence_zip_sha256 is not None
    assert result.page_count >= 2
    assert len(result.preview_png_relpaths) == result.page_count

    # Verify PDF text parseback via pypdf
    pdf_path = ws.resolve_path(result.pdf_relpath)
    pdf_reader = pypdf.PdfReader(str(pdf_path))
    assert len(pdf_reader.pages) == result.page_count
    pdf_text = "\n".join(p.extract_text() or "" for p in pdf_reader.pages)
    assert "Forced Oscillations and Resonance" in pdf_text
    assert "Self-Control Questions & Answers" in pdf_text
    assert "Additional Worked Problems" in pdf_text
    assert "2458.5" in pdf_text
    assert assets["c_w0"].calc_id in pdf_text  # type: ignore[attr-defined]

    # Verify preview PNGs are real non-empty images
    for prev_rel in result.preview_png_relpaths:
        prev_path = ws.resolve_path(prev_rel)
        assert prev_path.is_file()
        with Image.open(prev_path) as im:
            assert im.width > 800
            assert im.height > 1000

    # Verify standalone preview_report_pages subset selection
    subset = preview_report_pages(
        result.pdf_relpath,
        ws,
        dpi=100,
        pages=[1],
        output_subdir="reports/previews/subset",
        prefix="chk",
    )
    assert subset == ["reports/previews/subset/chk-001.png"]
    assert ws.resolve_path(subset[0]).is_file()

    # Verify DOCX parseback via python-docx
    docx_path = ws.resolve_path(result.docx_relpath)
    parsed_doc = docx.Document(str(docx_path))
    docx_text = "\n".join(p.text for p in parsed_doc.paragraphs)
    assert "Forced Oscillations and Resonance" in docx_text
    assert "Self-Control Questions & Answers" in docx_text
    assert "Additional Worked Problems" in docx_text
    assert len(parsed_doc.tables) >= 3  # results + calc ledger + provenance appendix
    assert parsed_doc.tables[0].rows[1].cells[3].text == "2458.5"
    assert len(parsed_doc.inline_shapes) == 3  # circuit + plot + problem 4C ellipse

    # Verify Teacher Evidence Packet PDF
    teacher_path = ws.resolve_path(result.teacher_packet_relpath)
    teacher_reader = pypdf.PdfReader(str(teacher_path))
    assert len(teacher_reader.pages) >= 3  # summary/index + 2 full-page screenshots
    teacher_text = "\n".join(p.extract_text() or "" for p in teacher_reader.pages)
    assert ws.run_id in teacher_text
    assert "VERIFIED" in teacher_text
    assert "evidence/capture-001.png" in teacher_text

    # Verify evidence.zip contents, exclusions, and SHA-256 manifest
    zip_path = ws.resolve_path(result.evidence_zip_relpath)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "provenance.jsonl" in names
        assert "calculations.jsonl" in names
        assert "evidence/capture-001.png" in names
        assert "evidence/capture-002.png" in names
        assert "datasets/rlc-sweep.json" in names
        assert "profiles/m96220-profile.json" in names
        # Strictly excluded: itself, reports/*, *.tmp, traversal paths
        assert "reports/evidence.zip" not in names
        assert not any(n.startswith("reports/") for n in names)
        assert not any(n.endswith(".tmp") for n in names)
        assert not any(n.startswith("/") or ".." in n.split("/") for n in names)

        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["run_id"] == ws.run_id
        assert manifest["provenance_verified"]["ok"] is True
        for file_rec in manifest["files"]:
            raw_bytes = zf.read(file_rec["relpath"])
            assert hashlib.sha256(raw_bytes).hexdigest() == file_rec["sha256"]
            assert len(raw_bytes) == file_rec["size_bytes"]

    # Provenance verification across the entire run passes 100%
    assert ws.verify_provenance().ok is True

    # Provenance overwrite protection refuses clobbering already-hashed outputs
    with pytest.raises(FileExistsError):
        await build_report_pdf(spec, ws, config=cfg, overwrite=True)
    with pytest.raises(FileExistsError):
        build_report_docx(spec, ws, overwrite=True)
    with pytest.raises(FileExistsError):
        build_evidence_zip(ws, overwrite=True)


@pytest.mark.asyncio
async def test_russian_cyrillic_report_pdf_and_docx_offline(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    wm = WorkspaceManager.from_config(cfg)
    ws = wm.create_run(label="ru-report-e2e")
    assets = _create_sample_visuals_and_calcs(ws, cfg, language="ru")
    fig_plot = assets["fig_plot"]
    fig_circuit = assets["fig_circuit"]
    fig_ellipse = assets["fig_ellipse"]
    assert isinstance(fig_plot, ReportFigure)
    assert isinstance(fig_circuit, ReportFigure)
    assert isinstance(fig_ellipse, ReportFigure)

    ru_spec = ReportSpec(
        title="Вынужденные колебания в последовательном RLC-контуре",
        subtitle="Экспериментальное определение резонансной частоты и добротности",
        lab_number="5",
        language="ru",
        author="Иванов И. И.",
        team="Бригада 2 (L = 5,0 мГн; C = 32 мкФ)",
        course="Общая физика — Электричество и магнетизм",
        institution="Кафедра общей физики",
        date_str="21.11.2026",
        objective=(
            "Изучить явление резонанса напряжений в цепи с $R$, $L$, $C$ и проверить "
            "соотношение $\\omega_0 / \\Delta\\omega = 4Q^2$."
        ),
        apparatus=[
            "Виртуальный генератор гармонического напряжения $\\varepsilon_0 \\cos\\omega t$",
            "Катушка индуктивности $L = 5{,}0\\text{ мГн}$ и конденсатор $C = 32\\text{ мкФ}$",
        ],
        theory_sections=[
            {
                "heading": "Резонанс напряжений и добротность контура",
                "body": "Собственная частота контура определяется формулой Томсона:",
                "equation": (
                    r"\omega_0 = \frac{1}{\sqrt{LC}}, "
                    r"\qquad Q = \frac{1}{R}\sqrt{\frac{L}{C}}"
                ),
            }
        ],
        tables=[
            ReportTable(
                table_id="tbl_ru_1",
                caption="Результаты измерений резонансного плато",
                headers=["R, Ом", "ω₁, с⁻¹", "ω₂, с⁻¹", "ω_ср, с⁻¹"],
                rows=[
                    ["3,2", "2445", "2472", "2458,5"],
                    ["5,0", "2361", "2434", "2397,5"],
                ],
                source="simulator_captured",
            )
        ],
        figures=[fig_circuit, fig_plot, fig_ellipse],
        conclusions=[
            (
                "Экспериментальная зависимость подтверждает линейный рост "
                "$\\omega_0/\\Delta\\omega$ от $Q^2$."
            )
        ],
        options=ReportSectionOptions(include_self_control=True, include_problems=False),
        self_control=[
            SelfControlItem(
                number="1",
                question=(
                    "Как меняется ширина резонансной кривой при увеличении "
                    "сопротивления $R$?"
                ),
                answer=(
                    "При увеличении активного сопротивления $R$ добротность $Q$ падает, "
                    "а полоса пропускания $\\Delta\\omega$ расширяется."
                ),
                equations=[r"\Delta\omega = \frac{R}{L}"],
                figure_ids=[fig_ellipse.figure_id],
            )
        ],
    )

    pdf_res = await build_report_pdf(ru_spec, ws, config=cfg, render_previews=True, preview_dpi=120)
    docx_res = build_report_docx(ru_spec, ws)
    teacher_res = await build_teacher_evidence_pdf(ws, config=cfg, language="ru")

    assert pdf_res.pdf_relpath == "reports/report.pdf"
    assert pdf_res.page_count >= 2
    assert len(pdf_res.preview_png_relpaths) == pdf_res.page_count

    pdf_reader = pypdf.PdfReader(str(ws.resolve_path(pdf_res.pdf_relpath)))
    ru_pdf_text = "\n".join(p.extract_text() or "" for p in pdf_reader.pages)
    assert "Вынужденные колебания" in ru_pdf_text
    assert "Контрольные вопросы и ответы" in ru_pdf_text
    assert "Бригада 2" in ru_pdf_text

    parsed_docx = docx.Document(str(ws.resolve_path(docx_res.docx_relpath or "")))
    ru_docx_text = "\n".join(p.text for p in parsed_docx.paragraphs)
    assert "Вынужденные колебания" in ru_docx_text
    assert "Контрольные вопросы и ответы" in ru_docx_text

    assert teacher_res.provenance_ok is True
    assert teacher_res.screenshot_count == 2
    assert ws.verify_provenance().ok is True


@pytest.mark.asyncio
async def test_offline_render_blocks_external_network_requests_and_host_spoofing(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    wm = WorkspaceManager.from_config(cfg)
    ws = wm.create_run(label="offline-guard")

    # 1. Host header & path traversal guard on _OfflineRenderServer
    srv = _OfflineRenderServer(
        html_content="<html><body>ok</body></html>",
        assets_dir=cfg.local_assets_dir,
        run_root=ws.root,
        host="127.0.0.1",
    )
    origin = srv.start()
    try:
        req = urllib.request.Request(f"{origin}/document.html", headers={"Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=3)
        assert exc_info.value.code == 403

        trav_req = urllib.request.Request(f"{origin}/run/..%2F..%2Fetc%2Fpasswd")
        with pytest.raises(urllib.error.HTTPError) as exc_info2:
            urllib.request.urlopen(trav_req, timeout=3)
        assert exc_info2.value.code in (403, 404)
    finally:
        srv.stop()

    # 2. Playwright route guard aborts any external resource request
    external_html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        "<body><script src='https://cdn.example.invalid/evil.js'></script>"
        "<script>window.__katexDone = true;</script></body></html>"
    )
    out_pdf = ws.resolve_path("reports/should-not-exist.pdf")
    with pytest.raises(RuntimeError, match="Blocked disallowed external network request"):
        await render_html_to_pdf(
            external_html,
            out_pdf,
            ws,
            config=cfg,
            wait_for_katex=True,
        )
    assert not out_pdf.exists()
    assert not out_pdf.with_name(f"{out_pdf.name}.tmp").exists()


@pytest.mark.asyncio
async def test_edge_cases_symlink_exclusion_svg_only_docx_and_tampered_teacher_packet(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    wm = WorkspaceManager.from_config(cfg)
    ws = wm.create_run(label="edge-cases")

    # 1. Invalid format arguments to build_report
    minimal_spec = ReportSpec(title="Minimal Lab Report", objective="Test edge cases.")
    with pytest.raises(ValueError, match="Unsupported report format"):
        await build_report(minimal_spec, ws, config=cfg, formats=("html",))
    with pytest.raises(ValueError, match="At least one format"):
        await build_report(
            minimal_spec, ws, config=cfg, formats=(), include_teacher_packet=False
        )

    # 2. SVG-only figure fallback in DOCX (no PNG present)
    svg_rel = "figures/vector_only.svg"
    ws.resolve_path(svg_rel).write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"></svg>',
        encoding="utf-8",
    )
    ws.record_artifact(svg_rel, kind="diagram_svg", source="model_computed")
    svg_spec = ReportSpec(
        title="SVG-Only Figure Test",
        figures=[
            ReportFigure(
                figure_id="vec_only",
                caption="Vector schematic without raster PNG",
                svg_relpath=svg_rel,
                png_relpath=None,
                source="model_computed",
            )
        ],
    )
    docx_res = build_report_docx(svg_spec, ws, filename="svg-only.docx")
    parsed = docx.Document(str(ws.resolve_path(docx_res.docx_relpath or "")))
    assert any("Vector SVG artifact" in p.text for p in parsed.paragraphs)

    # 3. Symlink inside evidence/ escaping workspace MUST be ignored by evidence.zip
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("do-not-archive", encoding="utf-8")
    escape_link = ws.evidence_dir / "escape.png"
    escape_link.symlink_to(outside_secret)

    zip_res = build_evidence_zip(ws, filename="confined.zip")
    with zipfile.ZipFile(ws.resolve_path(zip_res.zip_relpath), "r") as zf:
        assert "evidence/escape.png" not in zf.namelist()

    # 4. Tampered artifact surfaces provenance_ok=False in Teacher Evidence Packet
    ws.resolve_path(svg_rel).write_text("<svg>tampered</svg>", encoding="utf-8")
    teacher_res = await build_teacher_evidence_pdf(
        ws, config=cfg, filename="tampered-audit.pdf", language="en"
    )
    assert teacher_res.provenance_ok is False
    assert teacher_res.screenshot_count == 0
    t_reader = pypdf.PdfReader(str(ws.resolve_path(teacher_res.pdf_relpath)))
    t_text = "\n".join(p.extract_text() or "" for p in t_reader.pages)
    assert "INTEGRITY ERROR" in t_text
    assert "No raw evidence screenshots" in t_text


@pytest.mark.asyncio
async def test_template_injection_multiline_latex_and_docx_fidelity(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    wm = WorkspaceManager.from_config(cfg)
    ws = wm.create_run(label="run-{{INDEX_HTML}}")

    # 1. Single-pass template substitution prevents {{TOKEN}} double-expansion
    inj_spec = ReportSpec(
        title="Title {{BODY_HTML}} and {{REPORT_CSS}}",
        objective="Check $a < b < c$ and Cyrillic $\\text{привет}$:\n$$\n\\begin{aligned}\n"
        "a &= b + c \\\\\nd &= e + f\n\\end{aligned}\n$$",
        theory_sections=[
            {
                "heading": "Resonance at $\\omega_0$",
                "body": "Coupled system:",
                "equation": (
                    "\\begin{aligned}\n"
                    "\\omega_0 &= \\frac{1}{\\sqrt{LC}} \\\\\n"
                    "Q &= \\frac{1}{R}\\sqrt{\\frac{L}{C}}\n"
                    "\\end{aligned}"
                ),
            }
        ],
        options=ReportSectionOptions(include_self_control=True, include_evidence_appendix=True),
        self_control=[
            SelfControlItem(
                number="1",
                question="Explain $R \\in \\{3.2, 4.0\\}\\,\\Omega$ and $L = 5{,}0\\text{ мГн}$.",
                answer="First paragraph with $\\varepsilon_0 \\cos(\\omega t)$.\n\n"
                "Second paragraph preserved separately.",
                equations=[r"\begin{aligned} x &= 1 \\ y &= 2 \end{aligned}"],
            )
        ],
    )
    # Record a file containing '$' in its name to verify provenance table isn't mangled in DOCX
    dollar_rel = "evidence/cost$1.png"
    im = Image.new("RGB", (60, 40), (20, 40, 60))
    im.save(ws.resolve_path(dollar_rel), format="PNG")
    im.close()
    ws.record_artifact(dollar_rel, kind="sim_screenshot", source="simulator_captured")

    html_doc = render_report_html(inj_spec, ws)
    title_tag = html_doc.split("<title>")[1].split("</title>")[0]
    assert title_tag == "Title {{BODY_HTML}} and {{REPORT_CSS}}"
    assert html_doc.count('class="equation-block"') == 3  # objective + theory + self_control

    teacher_html, _, _ = render_teacher_evidence_html(ws, title="Audit {{SCREENSHOTS_HTML}}")
    t_title = teacher_html.split("<title>")[1].split("</title>")[0]
    assert t_title == "Audit {{SCREENSHOTS_HTML}}"

    # 2. Multi-line \begin{aligned} renders cleanly through Chromium KaTeX with zero errors
    pdf_res = await build_report_pdf(inj_spec, ws, config=cfg, render_previews=False)
    pdf_prov = [e for e in ws.read_provenance() if e.kind == "report_pdf"][-1]
    assert pdf_prov.details["katex_errors"] == []
    pdf_txt = "\n".join(
        p.extract_text() or ""
        for p in pypdf.PdfReader(str(ws.resolve_path(pdf_res.pdf_relpath or ""))).pages
    )
    assert "\\begin{aligned}" not in pdf_txt
    assert "a < b < c" in pdf_txt
    assert "привет" in pdf_txt

    # 3. DOCX preserves multi-paragraph Q&A, strips LaTeX tokens, keeps literal relpath intact
    docx_res = build_report_docx(inj_spec, ws)
    parsed = docx.Document(str(ws.resolve_path(docx_res.docx_relpath or "")))
    para_texts = [p.text for p in parsed.paragraphs]
    assert any("Resonance at ω_0" in t for t in para_texts)
    assert any("First paragraph" in t and "Second paragraph" not in t for t in para_texts)
    assert any("Second paragraph preserved separately." in t for t in para_texts)
    assert not any("\\quad" in t or "\\in" in t or "\\cos" in t or "{,}" in t for t in para_texts)
    # Provenance table retains exact '$' in relpath
    prov_table = parsed.tables[-1]
    assert any(cell.text == dollar_rel for row in prov_table.rows for cell in row.cells)

    # 4. Extended latex_to_readable_text coverage
    assert "\\" not in latex_to_readable_text(
        r"$$\omega_0 = \frac{1}{\sqrt{LC}}, \quad R \in \{3.2, 4.0\}\,\Omega, "
        r"\qquad L = 5{,}0\text{ мГн}, \varepsilon_0 \cos(\omega t)$$"
    )
    assert "\\" not in latex_to_readable_text(r"\begin{aligned} a &= b \\ c &= d \end{aligned}")


@pytest.mark.asyncio
async def test_missing_local_assets_orphan_cleanup_ipv6_and_audit_hardening(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    wm = WorkspaceManager.from_config(cfg)

    # 1. Missing KaTeX / font assets fail hard AND leave zero orphan reports/report.html
    empty_assets = tmp_path / "empty_assets"
    (empty_assets / "katex").mkdir(parents=True)
    (empty_assets / "fonts").mkdir(parents=True)
    bad_assets_cfg = PeaConfig(
        project_root=cfg.project_root,
        local_assets_dir=empty_assets,
        runs_dir=cfg.runs_dir,
        profiles_dir=cfg.profiles_dir,
        loopback_host="127.0.0.1",
        browser_executable=cfg.browser_executable,
    )
    ws_missing = wm.create_run(label="missing-katex")
    with pytest.raises(RuntimeError, match="offline report asset|renderMathInElement"):
        await build_report_pdf(
            ReportSpec(title="Missing Assets", objective="Test $\\omega_0$."),
            ws_missing,
            config=bad_assets_cfg,
            render_previews=False,
        )
    assert not ws_missing.resolve_path("reports/report.pdf").exists()
    assert not ws_missing.resolve_path("reports/report.html").exists()

    # 2. IPv6 loopback host (both '::1' and bracketed '[::1]') renders cleanly
    for host_val in ("::1", "[::1]"):
        cfg_v6 = PeaConfig(
            project_root=cfg.project_root,
            local_assets_dir=cfg.local_assets_dir,
            runs_dir=cfg.runs_dir,
            profiles_dir=cfg.profiles_dir,
            loopback_host=host_val,
            browser_executable=cfg.browser_executable,
        )
        ws_v6 = wm.create_run(label="ipv6-render")
        res_v6 = await build_report_pdf(
            ReportSpec(title=f"IPv6 Render {host_val}", objective="Verify IPv6 loopback."),
            ws_v6,
            config=cfg_v6,
            render_previews=False,
        )
        assert res_v6.page_count == 1

    # 3. preview_report_pages rejects '..' prefix
    ws_prev = wm.create_run(label="prefix-guard")
    res_prev = await build_report_pdf(
        ReportSpec(title="Prefix Guard"), ws_prev, config=cfg, render_previews=False
    )
    with pytest.raises(PathConfinementError):
        preview_report_pages(res_prev.pdf_relpath or "reports/report.pdf", ws_prev, prefix="..")

    # 4. Duplicate basenames in profile_paths are disambiguated (not dropped)
    ws_zip = wm.create_run(label="dup-profiles")
    d1, d2 = tmp_path / "dir_a", tmp_path / "dir_b"
    d1.mkdir()
    d2.mkdir()
    (d1 / "prof.json").write_text('{"variant": "A"}', encoding="utf-8")
    (d2 / "prof.json").write_text('{"variant": "B"}', encoding="utf-8")
    zres = build_evidence_zip(
        ws_zip,
        profile_paths=[d1 / "prof.json", d2 / "prof.json", d1 / "prof.json"],
    )
    with zipfile.ZipFile(ws_zip.resolve_path(zres.zip_relpath), "r") as zf:
        prof_names = sorted(n for n in zf.namelist() if n.startswith("profiles/"))
        assert set(prof_names) == {"profiles/prof.json", "profiles/prof-2.json"}
        assert json.loads(zf.read("profiles/prof.json"))["variant"] == "A"
        assert json.loads(zf.read("profiles/prof-2.json"))["variant"] == "B"

    # 5. Russian build_report localizes Teacher Evidence Packet title
    ws_ru = wm.create_run(label="ru-teacher-localized")
    ru_res = await build_report(
        ReportSpec(title="Лабораторная работа №5", language="ru", objective="Тест."),
        ws_ru,
        config=cfg,
        formats=("pdf",),
        include_teacher_packet=True,
        render_previews=False,
    )
    ru_t_txt = (
        pypdf.PdfReader(str(ws_ru.resolve_path(ru_res.teacher_packet_relpath or "")))
        .pages[0]
        .extract_text()
        or ""
    )
    assert "Evidence Packet" not in ru_t_txt
    assert "Пакет подтверждающих материалов" in ru_t_txt

    # 6. Tampered screenshot shows actual disk SHA-256 + ledger SHA-256; corrupt provenance survives
    ws_tamper = wm.create_run(label="tampered-audit")
    for idx, color in enumerate(((10, 50, 90), (20, 80, 40)), start=1):
        shot_p = ws_tamper.evidence_dir / f"shot-{idx}.png"
        im = Image.new("RGB", (180, 100), color)
        im.save(shot_p, format="PNG")
        im.close()
        ws_tamper.record_artifact(f"evidence/shot-{idx}.png", kind="sim_screenshot")

    orig_sha = ws_tamper.read_provenance()[0].sha256
    # Overwrite shot-1 with different valid PNG bytes
    im_bad = Image.new("RGB", (180, 100), (220, 10, 10))
    im_bad.save(ws_tamper.evidence_dir / "shot-1.png", format="PNG")
    im_bad.close()
    actual_sha, _ = sha256_file(ws_tamper.evidence_dir / "shot-1.png")
    assert actual_sha != orig_sha

    # Also append a malformed JSON line to provenance.jsonl
    with ws_tamper.provenance_path.open("a", encoding="utf-8") as fh:
        fh.write("{corrupted json line\n")

    with pytest.raises(ValueError, match="max_screenshots"):
        render_teacher_evidence_html(ws_tamper, max_screenshots=0)

    t_res = await build_teacher_evidence_pdf(
        ws_tamper,
        config=cfg,
        max_screenshots=1,
        filename="corrupt-audit.pdf",
    )
    assert t_res.provenance_ok is False
    assert t_res.screenshot_count == 1
    t_pages = pypdf.PdfReader(str(ws_tamper.resolve_path(t_res.pdf_relpath))).pages
    full_t_txt = unicodedata.normalize(
        "NFKC", "\n".join(p.extract_text() or "" for p in t_pages)
    )
    assert "INTEGRITY ERROR" in full_t_txt
    assert "invalid provenance JSON" in full_t_txt
    assert "Showing first 1 of 2" in full_t_txt
    assert actual_sha[:16] in full_t_txt
    assert orig_sha[:16] in full_t_txt


@pytest.mark.asyncio
async def test_multipage_table_and_tall_problem_card_pagination(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    wm = WorkspaceManager.from_config(cfg)
    ws = wm.create_run(label="pagination-stress")

    fig_rel = "figures/shared.png"
    im = Image.new("RGB", (600, 360), (31, 78, 121))
    im.save(ws.resolve_path(fig_rel), format="PNG")
    im.close()
    ws.record_artifact(fig_rel, kind="plot_png", source="model_computed")
    shared_fig = ReportFigure(
        figure_id="shared_fig",
        caption="Shared Phase Portrait",
        png_relpath=fig_rel,
        source="model_computed",
    )

    rows = [[str(i), f"{1000 + i * 10}", f"{2.5 + i * 0.03:.3f}"] for i in range(1, 36)]
    long_sol = "\n\n".join(
        f"Step {k}: Harmonic steady-state equation "
        f"$L\\ddot{{q}} + R\\dot{{q}} + q/C = \\varepsilon_0\\cos(\\omega t)$."
        for k in range(1, 16)
    )
    spec = ReportSpec(
        title="Long Table & Oversized Card Pagination",
        objective="Stress-test multi-page table header repetition and tall problem cards.",
        tables=[
            ReportTable(
                table_id="tbl_35",
                caption="35-Row Sweep Table",
                headers=["#", "ω (rad/s)", "U₀C/ε₀"],
                rows=rows,
            )
        ],
        figures=[shared_fig],
        options=ReportSectionOptions(include_self_control=True, include_problems=True),
        self_control=[
            SelfControlItem(
                number="1",
                question="Compact Q?",
                answer="Short answer.",
                figure_ids=[],
            ),
            SelfControlItem(
                number="2",
                question="Q referencing shared figure?",
                answer="See figure below.",
                figure_ids=["shared_fig"],
            ),
        ],
        problems=[
            ProblemItem(
                number="1",
                prompt="Long multi-paragraph derivation with shared figure.",
                solution=long_sol,
                figure_ids=["shared_fig"],
            )
        ],
    )

    html_doc = render_report_html(spec, ws)
    # Compact Q&A card gets card-compact; tall/figure-bearing cards do not
    assert 'class="qa-card card-compact"' in html_doc
    assert 'class="problem-card"' in html_doc
    # Shared figure referenced twice gets unique DOM IDs
    assert 'id="fig-shared_fig"' in html_doc
    assert 'id="fig-shared_fig-2"' in html_doc

    res = await build_report_pdf(spec, ws, config=cfg, render_previews=False)
    assert res.page_count >= 3
    pages_txt = [
        (p.extract_text() or "")
        for p in pypdf.PdfReader(str(ws.resolve_path(res.pdf_relpath or ""))).pages
    ]
    # Header repeats on Page 2, and Self-Control starts on Page 2 without stranding Page 2 blank
    assert "ω (rad/s)" in pages_txt[1]
    assert "Self-Control Questions & Answers" in pages_txt[1]


