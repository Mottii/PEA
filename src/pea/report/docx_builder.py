"""Editable academic ``.docx`` report builder mirroring ``ReportSpec`` via ``python-docx``."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Mm, Pt, RGBColor
from PIL import Image

from pea.math_engine.calculator import load_calculations
from pea.report.pdf_builder import (
    REPORT_LABELS,
    assert_provenance_safe_report_write,
    format_calc_value,
    index_and_validate_figures,
    validate_report_filename,
)
from pea.schemas import (
    CalcResult,
    ReportBuildResult,
    ReportFigure,
    ReportLanguage,
    ReportSpec,
    ReportTable,
)

if TYPE_CHECKING:
    from pea.workspace import RunWorkspace

_NAVY_RGB = RGBColor(0x1F, 0x4E, 0x79)
_INK_RGB = RGBColor(0x0F, 0x17, 0x2A)
_BODY_RGB = RGBColor(0x1E, 0x29, 0x3B)
_MUTED_RGB = RGBColor(0x47, 0x55, 0x69)
_GREEN_RGB = RGBColor(0x2E, 0x7D, 0x32)

_LATEX_SYMBOL_MAP: tuple[tuple[str, str], ...] = (
    (r"\varepsilon", "ε"),
    (r"\varphi", "φ"),
    (r"\omega", "ω"),
    (r"\Omega", "Ω"),
    (r"\Delta", "Δ"),
    (r"\delta", "δ"),
    (r"\alpha", "α"),
    (r"\beta", "β"),
    (r"\gamma", "γ"),
    (r"\theta", "θ"),
    (r"\lambda", "λ"),
    (r"\sigma", "σ"),
    (r"\Sigma", "Σ"),
    (r"\tau", "τ"),
    (r"\phi", "φ"),
    (r"\Phi", "Φ"),
    (r"\psi", "ψ"),
    (r"\mu", "μ"),
    (r"\pi", "π"),
    (r"\cdot", "·"),
    (r"\times", "×"),
    (r"\pm", "±"),
    (r"\mp", "∓"),
    (r"\approx", "≈"),
    (r"\neq", "≠"),
    (r"\leq", "≤"),
    (r"\geq", "≥"),
    (r"\le", "≤"),
    (r"\ge", "≥"),
    (r"\infty", "∞"),
    (r"\to", "→"),
    (r"\rightarrow", "→"),
    (r"\partial", "∂"),
    (r"\,", " "),
    (r"\;", " "),
    (r"\:", " "),
    (r"\!", ""),
    (r"\left", ""),
    (r"\right", ""),
)

_FRAC_RE = re.compile(r"\\frac\{([^{}]+)\}\{([^{}]+)\}")
_SQRT_RE = re.compile(r"\\sqrt\{([^{}]+)\}")
_TEXT_WRAP_RE = re.compile(r"\\(?:text|mathrm|mathbf|operatorname)\{([^{}]+)\}")


def latex_to_readable_text(raw: str) -> str:
    """Convert inline/display LaTeX expressions into clean Unicode text for editable DOCX."""
    text = raw.strip()
    if text.startswith("$$") and text.endswith("$$") and len(text) >= 4:
        text = text[2:-2].strip()
    elif text.startswith(r"\[") and text.endswith(r"\]") and len(text) >= 4:
        text = text[2:-2].strip()
    else:
        text = text.replace("$$", "").replace("$", "")
        text = text.replace(r"\(", "").replace(r"\)", "")
        text = text.replace(r"\[", "").replace(r"\]", "")

    for _ in range(4):
        updated = _TEXT_WRAP_RE.sub(r"\1", text)
        updated = _FRAC_RE.sub(r"(\1) / (\2)", updated)
        updated = _SQRT_RE.sub(r"√(\1)", updated)
        if updated == text:
            break
        text = updated

    for token, repl in _LATEX_SYMBOL_MAP:
        text = text.replace(token, repl)

    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _set_cell_shading(cell: Any, fill_hex: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    tc_pr.append(shd)


def _set_cell_margins(
    cell: Any,
    *,
    top: int = 75,
    bottom: int = 75,
    left: int = 110,
    right: int = 110,
) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = OxmlElement("w:tcMar")
    for side, val in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        node = OxmlElement(f"w:{side}")
        node.set(qn("w:w"), str(val))
        node.set(qn("w:type"), "dxa")
        tc_mar.append(node)
    tc_pr.append(tc_mar)


def _set_table_academic_borders(table: Any) -> None:
    tbl_pr = table._tbl.tblPr
    borders = parse_xml(
        f'<w:tblBorders {nsdecls("w")}>'
        '<w:top w:val="single" w:sz="12" w:space="0" w:color="1F4E79"/>'
        '<w:bottom w:val="single" w:sz="12" w:space="0" w:color="1F4E79"/>'
        '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>'
        '<w:left w:val="none"/>'
        '<w:right w:val="none"/>'
        '<w:insideV w:val="none"/>'
        "</w:tblBorders>"
    )
    tbl_pr.append(borders)


def _mark_row_header_and_nosplit(row: Any, *, is_header: bool) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tr_pr.append(OxmlElement("w:cantSplit"))
    if is_header:
        tr_pr.append(OxmlElement("w:tblHeader"))


def _add_bottom_border_to_paragraph(
    paragraph: Any,
    color_hex: str = "CBD5E1",
    sz: str = "6",
) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = parse_xml(
        f'<w:pBdr {nsdecls("w")}>'
        f'<w:bottom w:val="single" w:sz="{sz}" w:space="3" w:color="{color_hex}"/>'
        "</w:pBdr>"
    )
    p_pr.append(p_bdr)


def _add_section_heading(doc: Document, num: int, title: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12)
    p.paragraph_format.space_after = Pt(5)
    p.paragraph_format.keep_with_next = True
    run = p.add_run(f"{num}. {title}")
    run.font.name = "Noto Sans"
    run.font.size = Pt(12)
    run.font.bold = True
    run.font.color.rgb = _NAVY_RGB
    _add_bottom_border_to_paragraph(p, color_hex="CBD5E1", sz="6")


def _add_subsection_heading(doc: Document, title: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.keep_with_next = True
    run = p.add_run(title)
    run.font.name = "Noto Sans"
    run.font.size = Pt(10.5)
    run.font.bold = True
    run.font.color.rgb = _INK_RGB


def _add_body_paragraphs(doc: Document, text: str) -> None:
    cleaned = text.strip()
    if not cleaned:
        return
    for block in re.split(r"\n\s*\n", cleaned):
        chunk = " ".join(line.strip() for line in block.splitlines() if line.strip())
        if not chunk:
            continue
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(5)
        p.paragraph_format.line_spacing = 1.25
        run = p.add_run(latex_to_readable_text(chunk))
        run.font.name = "Noto Sans"
        run.font.size = Pt(10)
        run.font.color.rgb = _BODY_RGB


def _add_equation_paragraph(doc: Document, equation: str) -> None:
    readable = latex_to_readable_text(equation)
    if not readable:
        return
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(readable)
    run.font.name = "Noto Sans"
    run.font.size = Pt(10)
    run.font.italic = True
    run.font.color.rgb = _INK_RGB


def _add_styled_table(
    doc: Document,
    headers: list[str],
    rows: list[list[str]],
) -> None:
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _set_table_academic_borders(table)

    hdr_row = table.rows[0]
    _mark_row_header_and_nosplit(hdr_row, is_header=True)
    for col_idx, header_text in enumerate(headers):
        cell = hdr_row.cells[col_idx]
        _set_cell_shading(cell, "EEF4FA")
        _set_cell_margins(cell)
        p = cell.paragraphs[0]
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run(latex_to_readable_text(header_text))
        run.font.name = "Noto Sans"
        run.font.size = Pt(8.8)
        run.font.bold = True
        run.font.color.rgb = _INK_RGB

    for row_idx, row_vals in enumerate(rows):
        row = table.rows[row_idx + 1]
        _mark_row_header_and_nosplit(row, is_header=False)
        shade = "F8FAFC" if row_idx % 2 == 1 else "FFFFFF"
        for col_idx, val in enumerate(row_vals):
            cell = row.cells[col_idx]
            if shade != "FFFFFF":
                _set_cell_shading(cell, shade)
            _set_cell_margins(cell)
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            run = p.add_run(latex_to_readable_text(val))
            run.font.name = "Noto Sans"
            run.font.size = Pt(8.8)
            run.font.color.rgb = _BODY_RGB


def _add_report_table(
    doc: Document,
    table_spec: ReportTable,
    table_num: int,
    *,
    language: ReportLanguage,
) -> None:
    labels = REPORT_LABELS[language]
    prov_label = labels["provenance_labels"].get(table_spec.source, table_spec.source)

    cap = doc.add_paragraph()
    cap.paragraph_format.space_before = Pt(8)
    cap.paragraph_format.space_after = Pt(3)
    cap.paragraph_format.keep_with_next = True
    r_num = cap.add_run(f"{labels['table_label']} {table_num}. ")
    r_num.font.name = "Noto Sans"
    r_num.font.size = Pt(9.2)
    r_num.font.bold = True
    r_num.font.color.rgb = _NAVY_RGB
    r_txt = cap.add_run(f"{latex_to_readable_text(table_spec.caption)} [{prov_label}]")
    r_txt.font.name = "Noto Sans"
    r_txt.font.size = Pt(9.2)
    r_txt.font.bold = True
    r_txt.font.color.rgb = _INK_RGB

    _add_styled_table(doc, table_spec.headers, table_spec.rows)

    if table_spec.notes and table_spec.notes.strip():
        note_p = doc.add_paragraph()
        note_p.paragraph_format.space_before = Pt(3)
        note_p.paragraph_format.space_after = Pt(6)
        r_note = note_p.add_run(latex_to_readable_text(table_spec.notes))
        r_note.font.name = "Noto Sans"
        r_note.font.size = Pt(8.2)
        r_note.font.italic = True
        r_note.font.color.rgb = _MUTED_RGB


def _compute_fit_dimensions_mm(
    image_path: Path,
    *,
    max_width_mm: float = 160.0,
    max_height_mm: float = 112.0,
) -> tuple[float, float]:
    with Image.open(image_path) as img:
        w_px, h_px = img.size
    if w_px <= 0 or h_px <= 0:
        return max_width_mm, max_height_mm
    aspect = w_px / h_px
    width = max_width_mm
    height = width / aspect
    if height > max_height_mm:
        height = max_height_mm
        width = height * aspect
    return width, height


def _add_report_figure(
    doc: Document,
    fig: ReportFigure,
    fig_num: int,
    workspace: RunWorkspace,
    *,
    language: ReportLanguage,
) -> None:
    labels = REPORT_LABELS[language]
    prov_label = labels["provenance_labels"].get(fig.source, fig.source)

    png_path: Path | None = None
    if fig.png_relpath:
        candidate = workspace.resolve_path(fig.png_relpath)
        if candidate.is_file():
            png_path = candidate

    if png_path is not None:
        width_mm, _ = _compute_fit_dimensions_mm(png_path)
        img_p = doc.add_paragraph()
        img_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        img_p.paragraph_format.space_before = Pt(8)
        img_p.paragraph_format.space_after = Pt(3)
        img_p.paragraph_format.keep_with_next = True
        img_p.add_run().add_picture(str(png_path), width=Mm(width_mm))
    elif fig.svg_relpath:
        svg_path = workspace.resolve_path(fig.svg_relpath)
        if not svg_path.is_file():
            raise FileNotFoundError(
                f"Figure {fig.figure_id!r} asset {fig.svg_relpath!r} not found inside run "
                f"{workspace.run_id}"
            )
        ph = doc.add_paragraph()
        ph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        ph.paragraph_format.space_before = Pt(8)
        ph.paragraph_format.space_after = Pt(3)
        ph.paragraph_format.keep_with_next = True
        r_ph = ph.add_run(f"[Vector SVG artifact: {fig.svg_relpath}]")
        r_ph.font.name = "Noto Sans"
        r_ph.font.size = Pt(8.5)
        r_ph.font.italic = True
        r_ph.font.color.rgb = _MUTED_RGB
    else:
        raise FileNotFoundError(f"Figure {fig.figure_id!r} has no readable asset in run")

    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.paragraph_format.space_before = Pt(2)
    cap.paragraph_format.space_after = Pt(8)
    r_num = cap.add_run(f"{labels['figure_label']} {fig_num}. ")
    r_num.font.name = "Noto Sans"
    r_num.font.size = Pt(8.8)
    r_num.font.bold = True
    r_num.font.color.rgb = _NAVY_RGB
    r_txt = cap.add_run(f"{latex_to_readable_text(fig.caption)} [{prov_label}]")
    r_txt.font.name = "Noto Sans"
    r_txt.font.size = Pt(8.8)
    r_txt.font.color.rgb = _MUTED_RGB


def _add_calc_lines(
    doc: Document,
    calc_ids: list[str],
    calc_ledger: dict[str, CalcResult],
    *,
    language: ReportLanguage,
) -> None:
    if not calc_ids:
        return
    labels = REPORT_LABELS[language]
    hdr = doc.add_paragraph()
    hdr.paragraph_format.space_before = Pt(4)
    hdr.paragraph_format.space_after = Pt(2)
    r_hdr = hdr.add_run(f"{labels['verified_calcs']}:")
    r_hdr.font.name = "Noto Sans"
    r_hdr.font.size = Pt(8.8)
    r_hdr.font.bold = True
    r_hdr.font.color.rgb = _NAVY_RGB

    for cid in calc_ids:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Mm(6)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(2)
        rec = calc_ledger.get(cid)
        if rec is None:
            r = p.add_run(f"{cid} ({labels['ledger_missing']})")
        else:
            unc = f" ± {rec.uncertainty_si:.6g} (SI)" if rec.uncertainty_si is not None else ""
            r = p.add_run(f"{rec.calc_id}: {rec.expression} = {format_calc_value(rec)}{unc}")
        r.font.name = "Noto Sans"
        r.font.size = Pt(8.6)
        r.font.color.rgb = _BODY_RGB


def build_report_docx(
    spec: ReportSpec | dict[str, Any],
    workspace: RunWorkspace,
    *,
    filename: str = "report.docx",
    overwrite: bool = False,
) -> ReportBuildResult:
    """Compile a ``ReportSpec`` into an editable A4 ``.docx`` file and log provenance."""
    report = spec if isinstance(spec, ReportSpec) else ReportSpec.model_validate(spec)
    safe_name = validate_report_filename(filename, expected_suffix=".docx")
    workspace.ensure_dirs()

    docx_relpath = f"reports/{safe_name}"
    assert_provenance_safe_report_write(workspace, (docx_relpath,), overwrite=overwrite)

    lang: ReportLanguage = report.language
    labels = REPORT_LABELS[lang]
    figure_map, main_figures, _ = index_and_validate_figures(report)
    calc_ledger = load_calculations(workspace)

    doc = Document()
    for section in doc.sections:
        section.page_width = Mm(210)
        section.page_height = Mm(297)
        section.top_margin = Mm(18)
        section.bottom_margin = Mm(18)
        section.left_margin = Mm(16)
        section.right_margin = Mm(16)

    normal_style = doc.styles["Normal"]
    normal_style.font.name = "Noto Sans"
    normal_style.font.size = Pt(10)
    normal_style.font.color.rgb = _BODY_RGB

    # Masthead
    kicker_parts: list[str] = []
    if report.institution:
        kicker_parts.append(report.institution.strip())
    if report.lab_number:
        kicker_parts.append(f"{labels['lab_prefix']} {report.lab_number.strip()}")
    if kicker_parts:
        kp = doc.add_paragraph()
        kp.paragraph_format.space_before = Pt(0)
        kp.paragraph_format.space_after = Pt(2)
        rk = kp.add_run(" · ".join(kicker_parts).upper())
        rk.font.name = "Noto Sans"
        rk.font.size = Pt(8.5)
        rk.font.bold = True
        rk.font.color.rgb = _NAVY_RGB

    tp = doc.add_paragraph()
    tp.paragraph_format.space_before = Pt(0)
    tp.paragraph_format.space_after = Pt(3)
    rt = tp.add_run(report.title)
    rt.font.name = "Noto Sans"
    rt.font.size = Pt(16.5)
    rt.font.bold = True
    rt.font.color.rgb = _INK_RGB

    if report.subtitle:
        sp = doc.add_paragraph()
        sp.paragraph_format.space_before = Pt(0)
        sp.paragraph_format.space_after = Pt(6)
        rs = sp.add_run(report.subtitle)
        rs.font.name = "Noto Sans"
        rs.font.size = Pt(10.5)
        rs.font.color.rgb = _MUTED_RGB

    meta_pairs: list[tuple[str, str]] = []
    if report.author:
        meta_pairs.append((labels["author"], report.author))
    if report.team:
        meta_pairs.append((labels["team"], report.team))
    if report.course:
        meta_pairs.append((labels["course"], report.course))
    if report.date_str:
        meta_pairs.append((labels["date"], report.date_str))
    if meta_pairs:
        mp = doc.add_paragraph()
        mp.paragraph_format.space_before = Pt(2)
        mp.paragraph_format.space_after = Pt(8)
        for idx, (k, v) in enumerate(meta_pairs):
            if idx > 0:
                sep = mp.add_run("   |   ")
                sep.font.name = "Noto Sans"
                sep.font.size = Pt(8.8)
                sep.font.color.rgb = _MUTED_RGB
            rk = mp.add_run(f"{k}: ")
            rk.font.name = "Noto Sans"
            rk.font.size = Pt(8.8)
            rk.font.bold = True
            rk.font.color.rgb = _MUTED_RGB
            rv = mp.add_run(v)
            rv.font.name = "Noto Sans"
            rv.font.size = Pt(8.8)
            rv.font.color.rgb = _INK_RGB
        _add_bottom_border_to_paragraph(mp, color_hex="1F4E79", sz="10")

    sec_num = 0
    fig_counter = 0
    tbl_counter = 0

    def _next_sec(title: str) -> None:
        nonlocal sec_num
        sec_num += 1
        _add_section_heading(doc, sec_num, title)

    if report.objective.strip():
        _next_sec(labels["objective"])
        _add_body_paragraphs(doc, report.objective)

    if report.apparatus:
        items = [item for item in report.apparatus if item.strip()]
        if items:
            _next_sec(labels["apparatus"])
            for item in items:
                p = doc.add_paragraph(style="List Bullet")
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.space_after = Pt(2.5)
                r = p.add_run(latex_to_readable_text(item))
                r.font.name = "Noto Sans"
                r.font.size = Pt(9.8)
                r.font.color.rgb = _BODY_RGB

    if report.theory_sections:
        valid_theory = [
            e
            for e in report.theory_sections
            if any(
                (e.get(k) or "").strip()
                for k in ("heading", "title", "body", "content", "text", "equation", "equations")
            )
        ]
        if valid_theory:
            _next_sec(labels["theory"])
            for entry in valid_theory:
                heading = (entry.get("heading") or entry.get("title") or "").strip()
                body = (
                    entry.get("body")
                    or entry.get("content")
                    or entry.get("text")
                    or ""
                ).strip()
                eq = (entry.get("equation") or entry.get("equations") or "").strip()
                if heading:
                    _add_subsection_heading(doc, heading)
                if body:
                    _add_body_paragraphs(doc, body)
                if eq:
                    for eq_line in eq.splitlines():
                        if eq_line.strip():
                            _add_equation_paragraph(doc, eq_line)

    if report.procedure:
        steps = [s for s in report.procedure if s.strip()]
        if steps:
            _next_sec(labels["procedure"])
            for step in steps:
                p = doc.add_paragraph(style="List Number")
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.space_after = Pt(2.5)
                r = p.add_run(latex_to_readable_text(step))
                r.font.name = "Noto Sans"
                r.font.size = Pt(9.8)
                r.font.color.rgb = _BODY_RGB

    if report.tables or main_figures:
        _next_sec(labels["results"])
        for tbl in report.tables:
            tbl_counter += 1
            _add_report_table(doc, tbl, tbl_counter, language=lang)
        for fig in main_figures:
            fig_counter += 1
            _add_report_figure(doc, fig, fig_counter, workspace, language=lang)

    if report.analysis_paragraphs:
        paras = [p for p in report.analysis_paragraphs if p.strip()]
        if paras:
            _next_sec(labels["analysis"])
            for para in paras:
                _add_body_paragraphs(doc, para)

    if report.calc_ids:
        _next_sec(labels["calculations"])
        calc_rows: list[list[str]] = []
        for cid in report.calc_ids:
            rec = calc_ledger.get(cid)
            if rec is None:
                calc_rows.append([cid, labels["ledger_missing"], "—", "—", "—"])
            else:
                unc_str = f"{rec.uncertainty_si:.6g}" if rec.uncertainty_si is not None else "—"
                prov_str = labels["provenance_labels"].get(rec.provenance, rec.provenance)
                calc_rows.append(
                    [rec.calc_id, rec.expression, format_calc_value(rec), unc_str, prov_str]
                )
        _add_styled_table(
            doc,
            [
                labels["calc_id"],
                labels["expression"],
                labels["result"],
                labels["uncertainty_si"],
                labels["provenance"],
            ],
            calc_rows,
        )

    if report.conclusions:
        concs = [c for c in report.conclusions if c.strip()]
        if concs:
            _next_sec(labels["conclusions"])
            for conc in concs:
                p = doc.add_paragraph(style="List Number")
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.space_after = Pt(3)
                r = p.add_run(latex_to_readable_text(conc))
                r.font.name = "Noto Sans"
                r.font.size = Pt(9.8)
                r.font.color.rgb = _BODY_RGB

    # Strict opt-in: Self-Control Q&A
    if report.options.include_self_control and report.self_control:
        _next_sec(labels["self_control"])
        for item in report.self_control:
            qp = doc.add_paragraph()
            qp.paragraph_format.space_before = Pt(7)
            qp.paragraph_format.space_after = Pt(3)
            qp.paragraph_format.keep_with_next = True
            r_num = qp.add_run(f"{labels['question_prefix']} {item.number}. ")
            r_num.font.name = "Noto Sans"
            r_num.font.size = Pt(9.8)
            r_num.font.bold = True
            r_num.font.color.rgb = _NAVY_RGB
            r_q = qp.add_run(latex_to_readable_text(item.question))
            r_q.font.name = "Noto Sans"
            r_q.font.size = Pt(9.8)
            r_q.font.bold = True
            r_q.font.color.rgb = _INK_RGB

            ap = doc.add_paragraph()
            ap.paragraph_format.space_before = Pt(0)
            ap.paragraph_format.space_after = Pt(4)
            r_al = ap.add_run(f"{labels['answer_prefix']}: ")
            r_al.font.name = "Noto Sans"
            r_al.font.size = Pt(9.6)
            r_al.font.bold = True
            r_al.font.color.rgb = _MUTED_RGB
            r_a = ap.add_run(latex_to_readable_text(item.answer))
            r_a.font.name = "Noto Sans"
            r_a.font.size = Pt(9.6)
            r_a.font.color.rgb = _BODY_RGB

            for eq in item.equations:
                if eq.strip():
                    _add_equation_paragraph(doc, eq)
            for fid in item.figure_ids:
                fig_counter += 1
                _add_report_figure(doc, figure_map[fid], fig_counter, workspace, language=lang)

    # Strict opt-in: Additional Problems
    if report.options.include_problems and report.problems:
        _next_sec(labels["problems"])
        for item in report.problems:
            pp = doc.add_paragraph()
            pp.paragraph_format.space_before = Pt(7)
            pp.paragraph_format.space_after = Pt(3)
            pp.paragraph_format.keep_with_next = True
            r_num = pp.add_run(f"{labels['problem_prefix']} {item.number}. ")
            r_num.font.name = "Noto Sans"
            r_num.font.size = Pt(9.8)
            r_num.font.bold = True
            r_num.font.color.rgb = _GREEN_RGB
            r_p = pp.add_run(latex_to_readable_text(item.prompt))
            r_p.font.name = "Noto Sans"
            r_p.font.size = Pt(9.8)
            r_p.font.bold = True
            r_p.font.color.rgb = _INK_RGB

            sp = doc.add_paragraph()
            sp.paragraph_format.space_before = Pt(0)
            sp.paragraph_format.space_after = Pt(4)
            r_sl = sp.add_run(f"{labels['solution_prefix']}: ")
            r_sl.font.name = "Noto Sans"
            r_sl.font.size = Pt(9.6)
            r_sl.font.bold = True
            r_sl.font.color.rgb = _MUTED_RGB
            r_s = sp.add_run(latex_to_readable_text(item.solution))
            r_s.font.name = "Noto Sans"
            r_s.font.size = Pt(9.6)
            r_s.font.color.rgb = _BODY_RGB

            _add_calc_lines(doc, item.calc_ids, calc_ledger, language=lang)
            for fid in item.figure_ids:
                fig_counter += 1
                _add_report_figure(doc, figure_map[fid], fig_counter, workspace, language=lang)

    # Strict opt-in: Provenance Appendix
    if report.options.include_evidence_appendix:
        _next_sec(labels["evidence_appendix"])
        entries = workspace.read_provenance()
        if not entries:
            _add_body_paragraphs(doc, labels["no_provenance"])
        else:
            prov_rows = [
                [
                    entry.relpath,
                    entry.kind,
                    labels["provenance_labels"].get(entry.source, entry.source),
                    str(entry.size_bytes),
                    entry.sha256,
                ]
                for entry in entries
            ]
            _add_styled_table(
                doc,
                [
                    labels["artifact"],
                    labels["kind"],
                    labels["provenance"],
                    labels["size_bytes"],
                    labels["sha256"],
                ],
                prov_rows,
            )

    out_path = workspace.resolve_path(docx_relpath)
    tmp_path = out_path.with_name(f"{out_path.name}.tmp")
    try:
        doc.save(str(tmp_path))
        os.replace(tmp_path, out_path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise

    entry = workspace.record_artifact(
        docx_relpath,
        kind="report_docx",
        source="model_computed",
        metadata={
            "title": report.title,
            "language": report.language,
            "include_self_control": report.options.include_self_control,
            "include_problems": report.options.include_problems,
            "include_evidence_appendix": report.options.include_evidence_appendix,
        },
    )

    return ReportBuildResult(
        run_id=workspace.run_id,
        docx_relpath=entry.relpath,
        docx_sha256=entry.sha256,
    )


__all__ = [
    "build_report_docx",
    "latex_to_readable_text",
]
