"""Offline HTML + KaTeX + Noto Sans -> Playwright Chromium A4 PDF report builder."""

from __future__ import annotations

import html
import mimetypes
import os
import re
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

import pypdfium2 as pdfium

from pea.config import (
    PathConfinementError,
    PeaConfig,
    is_loopback_host,
    is_origin_allowed,
    resolve_browser_executable,
    resolve_confined_path,
)
from pea.math_engine.calculator import load_calculations
from pea.schemas import (
    CalcResult,
    ProvenanceSource,
    ReportBuildResult,
    ReportFigure,
    ReportLanguage,
    ReportSpec,
    ReportTable,
)

if TYPE_CHECKING:
    from pea.workspace import RunWorkspace

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".json": "application/json; charset=utf-8",
}

REPORT_LABELS: dict[ReportLanguage, dict[str, Any]] = {
    "en": {
        "lab_prefix": "Laboratory Work",
        "author": "Author",
        "team": "Team / Variant",
        "course": "Course",
        "date": "Date",
        "lab_number": "Lab",
        "objective": "Objective",
        "apparatus": "Apparatus & Setup",
        "theory": "Theoretical Background",
        "procedure": "Experimental Procedure",
        "results": "Experimental Data & Results",
        "analysis": "Data Analysis & Discussion",
        "conclusions": "Conclusions",
        "calculations": "Verified Calculations Ledger",
        "self_control": "Self-Control Questions & Answers",
        "problems": "Additional Worked Problems",
        "evidence_appendix": "Appendix: Provenance & Artifact Checksums",
        "figure_label": "Figure",
        "table_label": "Table",
        "question_prefix": "Question",
        "answer_prefix": "Answer",
        "problem_prefix": "Problem",
        "solution_prefix": "Solution",
        "verified_calcs": "Linked Verified Calculations",
        "calc_id": "Calc ID",
        "expression": "Expression",
        "result": "Result",
        "uncertainty_si": "Uncertainty (SI)",
        "provenance": "Provenance",
        "artifact": "Artifact",
        "kind": "Kind",
        "size_bytes": "Bytes",
        "sha256": "SHA-256",
        "ledger_missing": "not found in run ledger",
        "no_provenance": "No artifacts recorded in provenance.jsonl.",
        "page_word": "Page",
        "of_word": "of",
        "provenance_labels": {
            "simulator_captured": "Simulator Captured",
            "bench_manual": "Bench / Manual",
            "model_computed": "Model Computed",
        },
        "teacher_kicker": "PEA v2 — Instructor Audit Packet",
        "teacher_title_suffix": "Evidence Packet",
        "teacher_default_title": "Experimental Evidence & Provenance Packet",
        "teacher_run_id": "Run ID",
        "teacher_status": "Run Status",
        "teacher_provenance": "Provenance Check",
        "teacher_verified": "VERIFIED",
        "teacher_failed": "INTEGRITY ERROR",
        "teacher_artifacts": "Hashed Artifacts",
        "teacher_screenshots": "Evidence Captures",
        "teacher_calcs": "Calculations",
        "teacher_index_heading": "1. Artifact Provenance Index (SHA-256)",
        "teacher_shots_heading": "2. Raw Evidence Screenshots",
        "teacher_no_shots": "No raw evidence screenshots were found in this run.",
        "teacher_captured_at": "Timestamp (UTC)",
        "teacher_actual_sha256": "Actual Disk SHA-256",
        "teacher_ledger_sha256": "Recorded Ledger SHA-256",
        "teacher_shots_truncated": "Showing first {shown} of {total} evidence screenshots.",
    },
    "ru": {
        "lab_prefix": "Лабораторная работа",
        "author": "Выполнил(а)",
        "team": "Бригада / Вариант",
        "course": "Дисциплина",
        "date": "Дата",
        "lab_number": "Работа",
        "objective": "Цель работы",
        "apparatus": "Приборы и оборудование",
        "theory": "Теоретические сведения",
        "procedure": "Порядок выполнения работы",
        "results": "Результаты измерений",
        "analysis": "Обработка данных и анализ результатов",
        "conclusions": "Выводы",
        "calculations": "Реестр верифицированных расчётов",
        "self_control": "Контрольные вопросы и ответы",
        "problems": "Дополнительные задачи",
        "evidence_appendix": "Приложение: реестр артефактов и контрольных сумм SHA-256",
        "figure_label": "Рисунок",
        "table_label": "Таблица",
        "question_prefix": "Вопрос",
        "answer_prefix": "Ответ",
        "problem_prefix": "Задача",
        "solution_prefix": "Решение",
        "verified_calcs": "Связанные верифицированные расчёты",
        "calc_id": "ID расчёта",
        "expression": "Выражение",
        "result": "Результат",
        "uncertainty_si": "Погрешность (СИ)",
        "provenance": "Источник",
        "artifact": "Артефакт",
        "kind": "Тип",
        "size_bytes": "Байт",
        "sha256": "SHA-256",
        "ledger_missing": "не найден в реестре расчётов",
        "no_provenance": "В provenance.jsonl артефакты не записаны.",
        "page_word": "Стр.",
        "of_word": "из",
        "provenance_labels": {
            "simulator_captured": "Измерено в симуляторе",
            "bench_manual": "Лабораторные данные",
            "model_computed": "Расчётная модель",
        },
        "teacher_kicker": "PEA v2 — Пакет подтверждающих материалов",
        "teacher_title_suffix": "Пакет подтверждающих материалов",
        "teacher_default_title": "Пакет первичных данных и контрольных сумм",
        "teacher_run_id": "ID запуска",
        "teacher_status": "Статус запуска",
        "teacher_provenance": "Проверка SHA-256",
        "teacher_verified": "ПОДТВЕРЖДЕНО",
        "teacher_failed": "ОШИБКА ЦЕЛОСТНОСТИ",
        "teacher_artifacts": "Артефактов",
        "teacher_screenshots": "Скриншотов",
        "teacher_calcs": "Расчётов",
        "teacher_index_heading": "1. Реестр артефактов и контрольных сумм SHA-256",
        "teacher_shots_heading": "2. Первичные скриншоты измерений",
        "teacher_no_shots": "В данном запуске скриншоты в каталоге evidence/ отсутствуют.",
        "teacher_captured_at": "Метка времени (UTC)",
        "teacher_actual_sha256": "Фактический SHA-256 на диске",
        "teacher_ledger_sha256": "SHA-256 в реестре",
        "teacher_shots_truncated": "Показаны первые {shown} из {total} скриншотов.",
    },
}


def validate_report_filename(filename: str, *, expected_suffix: str) -> str:
    """Validate a report output filename strictly (no path separators or traversal)."""
    cleaned = filename.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or cleaned in (".", ".."):
        raise PathConfinementError(f"Invalid report filename: {filename!r}")
    if not _FILENAME_RE.match(cleaned):
        raise PathConfinementError(f"Report filename contains disallowed characters: {filename!r}")
    if not cleaned.lower().endswith(expected_suffix.lower()):
        raise ValueError(f"Expected filename ending with {expected_suffix!r}; got {filename!r}")
    return cleaned


def assert_provenance_safe_report_write(
    workspace: RunWorkspace,
    relpaths: tuple[str, ...],
    *,
    overwrite: bool,
) -> None:
    """Prevent silent overwrites and protect immutable ``provenance.jsonl`` hashes."""
    if not overwrite:
        for rel in relpaths:
            if workspace.resolve_path(rel).exists():
                raise FileExistsError(
                    f"Report artifact {rel!r} already exists in run {workspace.run_id}; "
                    "choose a unique filename to preserve provenance immutability"
                )
        return

    recorded = {entry.relpath for entry in workspace.read_provenance()}
    conflicts = [rel for rel in relpaths if rel in recorded]
    if conflicts:
        raise FileExistsError(
            f"Cannot overwrite provenance-recorded report artifact(s) {conflicts} in run "
            f"{workspace.run_id}; choose a unique filename to preserve provenance integrity"
        )


_TEMPLATE_TOKEN_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")
_DISPLAY_MATH_RE = re.compile(r"(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\])")
_LATEX_BEGIN_RE = re.compile(r"\\begin\{[^{}]+\}")
_LATEX_END_RE = re.compile(r"\\end\{[^{}]+\}")


def load_template(name: str) -> str:
    """Read a template file from ``src/pea/report/templates/``."""
    target = resolve_confined_path(_TEMPLATES_DIR, name)
    return target.read_text(encoding="utf-8")


def render_template_tokens(template: str, replacements: dict[str, str]) -> str:
    """Perform single-pass ``{{TOKEN}}`` substitution without re-expanding user content."""
    return _TEMPLATE_TOKEN_RE.sub(
        lambda match: replacements.get(match.group(0), match.group(0)),
        template,
    )


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _encode_relpath_url(relpath: str) -> str:
    parts = [quote(part, safe="") for part in relpath.split("/") if part]
    return "/".join(parts)


def _format_scalar(val: float) -> str:
    if val == 0.0:
        return "0"
    return f"{val:.6g}"


def format_calc_value(record: CalcResult) -> str:
    """Format a ``CalcResult`` value, imaginary part, and unit cleanly."""
    if isinstance(record.output_value, list):
        vec_str = ", ".join(_format_scalar(float(v)) for v in record.output_value)
        val_text = f"[{vec_str}]"
    elif record.output_imag is not None:
        re_str = _format_scalar(float(record.output_value))
        im_val = float(record.output_imag)
        sign = "+" if im_val >= 0 else "-"
        im_str = _format_scalar(abs(im_val))
        val_text = f"{re_str} {sign} {im_str}j"
    else:
        val_text = _format_scalar(float(record.output_value))

    unit = record.output_unit.strip()
    if unit and unit != "1":
        return f"{val_text} {unit}"
    return val_text


def provenance_badge_html(source: ProvenanceSource, language: ReportLanguage) -> str:
    """Render a localized provenance badge ``<span>``."""
    labels = REPORT_LABELS[language]["provenance_labels"]
    text = labels.get(source, source)
    cls_map = {
        "simulator_captured": "badge badge-sim",
        "bench_manual": "badge badge-bench",
        "model_computed": "badge badge-model",
    }
    cls = cls_map.get(source, "badge badge-sim")
    return f'<span class="{cls}">{_esc(text)}</span>'


def _strip_outer_math_delimiters(equation: str) -> str:
    s = equation.strip()
    if s.startswith("$$") and s.endswith("$$") and len(s) >= 4:
        return s[2:-2].strip()
    if s.startswith(r"\[") and s.endswith(r"\]") and len(s) >= 4:
        return s[2:-2].strip()
    if s.startswith("$") and s.endswith("$") and len(s) >= 2:
        return s[1:-1].strip()
    return s


def split_equation_blocks(raw: str) -> list[str]:
    """Split an equation field into blocks without shattering ``\\begin{...}`` environments."""
    cleaned = raw.strip()
    if not cleaned:
        return []

    if _LATEX_BEGIN_RE.search(cleaned) or r"\\" in cleaned:
        blocks: list[str] = []
        current: list[str] = []
        env_depth = 0
        for line in cleaned.splitlines():
            stripped = line.strip()
            if not stripped:
                if env_depth == 0 and current:
                    blocks.append("\n".join(current))
                    current = []
                elif current:
                    current.append("")
                continue
            env_depth += len(_LATEX_BEGIN_RE.findall(stripped))
            current.append(stripped)
            env_depth = max(0, env_depth - len(_LATEX_END_RE.findall(stripped)))
        if current:
            blocks.append("\n".join(current))
        return [b for b in blocks if b.strip()]

    return [line.strip() for line in cleaned.splitlines() if line.strip()]


def _render_paragraphs_html(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""
    segments = _DISPLAY_MATH_RE.split(cleaned)
    parts: list[str] = []
    for idx, segment in enumerate(segments):
        if idx % 2 == 1:
            eq_html = _render_equation_block_html(segment)
            if eq_html:
                parts.append(eq_html)
            continue
        seg_clean = segment.strip()
        if not seg_clean:
            continue
        for block in re.split(r"\n\s*\n", seg_clean):
            lines = [_esc(line.strip()) for line in block.splitlines() if line.strip()]
            if lines:
                parts.append(f"<p>{'<br />'.join(lines)}</p>")
    return "\n".join(parts)


def _render_equation_block_html(equation: str) -> str:
    inner = _strip_outer_math_delimiters(equation)
    if not inner:
        return ""
    return f'<div class="equation-block">$${_esc(inner)}$$</div>'


def _resolve_figure_relpath(fig: ReportFigure, workspace: RunWorkspace) -> str:
    """Prefer SVG over PNG for crisp PDF vector output; validate confinement and existence."""
    candidates = [p for p in (fig.svg_relpath, fig.png_relpath) if p]
    for rel in candidates:
        resolved = workspace.resolve_path(rel)
        if resolved.is_file():
            return resolved.relative_to(workspace.root).as_posix()
    raise FileNotFoundError(
        f"Figure {fig.figure_id!r} asset(s) {candidates} not found inside run {workspace.run_id}"
    )


def _render_figure_html(
    fig: ReportFigure,
    fig_num: int,
    workspace: RunWorkspace,
    *,
    run_prefix: str,
    language: ReportLanguage,
    seen_fig_ids: dict[str, int] | None = None,
) -> str:
    labels = REPORT_LABELS[language]
    relpath = _resolve_figure_relpath(fig, workspace)
    img_url = f"{run_prefix.rstrip('/')}/{_encode_relpath_url(relpath)}"
    badge = provenance_badge_html(fig.source, language)
    fig_word = labels["figure_label"]
    dom_id = f"fig-{fig.figure_id}"
    if seen_fig_ids is not None:
        count = seen_fig_ids.get(fig.figure_id, 0) + 1
        seen_fig_ids[fig.figure_id] = count
        if count > 1:
            dom_id = f"fig-{fig.figure_id}-{count}"
    return (
        f'<figure class="figure-block" id="{_esc(dom_id)}">\n'
        f'  <img class="figure-img" src="{_esc(img_url)}" alt="{_esc(fig.caption)}" />\n'
        f'  <figcaption class="figure-caption-bar">\n'
        f'    <span class="figure-caption"><strong>{_esc(fig_word)} {fig_num}.</strong> '
        f"{_esc(fig.caption)}</span>\n"
        f"    {badge}\n"
        f"  </figcaption>\n"
        f"</figure>"
    )


def _render_table_html(
    table: ReportTable,
    table_num: int,
    *,
    language: ReportLanguage,
) -> str:
    labels = REPORT_LABELS[language]
    badge = provenance_badge_html(table.source, language)
    tbl_word = labels["table_label"]
    th_cells = "".join(f"<th>{_esc(h)}</th>" for h in table.headers)
    body_rows: list[str] = []
    for row in table.rows:
        td_cells = "".join(f"<td>{_esc(cell)}</td>" for cell in row)
        body_rows.append(f"      <tr>{td_cells}</tr>")
    notes_html = (
        f'  <div class="table-notes">{_esc(table.notes)}</div>\n'
        if table.notes and table.notes.strip()
        else ""
    )
    compact_cls = " table-compact" if len(table.rows) <= 12 else ""
    return (
        f'<div class="table-block{compact_cls}" id="tbl-{_esc(table.table_id)}">\n'
        f'  <div class="table-header-row">\n'
        f'    <div class="table-caption">{_esc(tbl_word)} {table_num}. '
        f"{_esc(table.caption)}</div>\n"
        f"    {badge}\n"
        f"  </div>\n"
        f'  <table class="data-table">\n'
        f"    <thead><tr>{th_cells}</tr></thead>\n"
        f"    <tbody>\n"
        f"{chr(10).join(body_rows)}\n"
        f"    </tbody>\n"
        f"  </table>\n"
        f"{notes_html}"
        f"</div>"
    )


def _render_calc_strip_html(
    calc_ids: list[str],
    calc_ledger: dict[str, CalcResult],
    *,
    language: ReportLanguage,
) -> str:
    if not calc_ids:
        return ""
    labels = REPORT_LABELS[language]
    rows: list[str] = []
    for cid in calc_ids:
        rec = calc_ledger.get(cid)
        if rec is None:
            rows.append(
                f'<div class="calc-row"><span class="mono">{_esc(cid)}</span> '
                f"<span>({_esc(labels['ledger_missing'])})</span></div>"
            )
            continue
        val_str = format_calc_value(rec)
        unc_part = (
            f" ± {_format_scalar(rec.uncertainty_si)} (SI)"
            if rec.uncertainty_si is not None
            else ""
        )
        rows.append(
            f'<div class="calc-row">'
            f'<span class="mono">{_esc(rec.calc_id)}</span>'
            f'<span class="mono">{_esc(rec.expression)}</span>'
            f"<span>= <strong>{_esc(val_str)}{_esc(unc_part)}</strong></span>"
            f"</div>"
        )
    return (
        f'<div class="calc-strip">\n'
        f'  <div class="calc-strip-title">{_esc(labels["verified_calcs"])}</div>\n'
        f"  {''.join(rows)}\n"
        f"</div>"
    )


def _render_calc_ledger_table_html(
    calc_ids: list[str],
    calc_ledger: dict[str, CalcResult],
    *,
    language: ReportLanguage,
) -> str:
    labels = REPORT_LABELS[language]
    rows_html: list[str] = []
    for cid in calc_ids:
        rec = calc_ledger.get(cid)
        if rec is None:
            rows_html.append(
                f'<tr><td class="mono">{_esc(cid)}</td>'
                f'<td colspan="4">{_esc(labels["ledger_missing"])}</td></tr>'
            )
            continue
        val_str = format_calc_value(rec)
        unc_str = (
            _format_scalar(rec.uncertainty_si) if rec.uncertainty_si is not None else "—"
        )
        badge = provenance_badge_html(rec.provenance, language)
        rows_html.append(
            f'<tr><td class="mono">{_esc(rec.calc_id)}</td>'
            f'<td class="mono">{_esc(rec.expression)}</td>'
            f"<td><strong>{_esc(val_str)}</strong></td>"
            f"<td>{_esc(unc_str)}</td>"
            f"<td>{badge}</td></tr>"
        )
    return (
        f'<table class="data-table">\n'
        f"  <thead><tr>"
        f"<th>{_esc(labels['calc_id'])}</th>"
        f"<th>{_esc(labels['expression'])}</th>"
        f"<th>{_esc(labels['result'])}</th>"
        f"<th>{_esc(labels['uncertainty_si'])}</th>"
        f"<th>{_esc(labels['provenance'])}</th>"
        f"</tr></thead>\n"
        f"  <tbody>{''.join(rows_html)}</tbody>\n"
        f"</table>"
    )


def _render_provenance_appendix_html(
    workspace: RunWorkspace,
    *,
    language: ReportLanguage,
) -> str:
    labels = REPORT_LABELS[language]
    entries = workspace.read_provenance()
    if not entries:
        return f'<div class="empty-notice">{_esc(labels["no_provenance"])}</div>'

    rows_html: list[str] = []
    for entry in entries:
        badge = provenance_badge_html(entry.source, language)
        rows_html.append(
            f'<tr><td class="mono path-cell">{_esc(entry.relpath)}</td>'
            f'<td class="nowrap-cell">{_esc(entry.kind)}</td>'
            f'<td class="nowrap-cell">{badge}</td>'
            f'<td class="num-cell">{entry.size_bytes}</td>'
            f'<td class="sha-cell">{_esc(entry.sha256)}</td></tr>'
        )
    return (
        f'<table class="data-table">\n'
        f"  <thead><tr>"
        f"<th>{_esc(labels['artifact'])}</th>"
        f'<th class="nowrap-cell">{_esc(labels["kind"])}</th>'
        f'<th class="nowrap-cell">{_esc(labels["provenance"])}</th>'
        f'<th class="num-cell">{_esc(labels["size_bytes"])}</th>'
        f"<th>{_esc(labels['sha256'])}</th>"
        f"</tr></thead>\n"
        f"  <tbody>{''.join(rows_html)}</tbody>\n"
        f"</table>"
    )


def index_and_validate_figures(
    spec: ReportSpec,
) -> tuple[dict[str, ReportFigure], list[ReportFigure], set[str]]:
    """Validate figure uniqueness and linked references across Q&A and Problem items.

    Returns ``(figure_map, main_figures, linked_figure_ids)``.
    """
    figure_map: dict[str, ReportFigure] = {}
    for fig in spec.figures:
        if fig.figure_id in figure_map:
            raise ValueError(f"Duplicate figure_id in ReportSpec: {fig.figure_id!r}")
        figure_map[fig.figure_id] = fig

    linked_ids: set[str] = set()
    for item in spec.self_control:
        linked_ids.update(item.figure_ids)
    for item in spec.problems:
        linked_ids.update(item.figure_ids)

    if spec.options.include_self_control:
        for item in spec.self_control:
            for fid in item.figure_ids:
                if fid not in figure_map:
                    raise ValueError(
                        f"SelfControlItem {item.number!r} references unknown figure_id {fid!r}"
                    )

    if spec.options.include_problems:
        for item in spec.problems:
            for fid in item.figure_ids:
                if fid not in figure_map:
                    raise ValueError(
                        f"ProblemItem {item.number!r} references unknown figure_id {fid!r}"
                    )

    main_figures = [fig for fig in spec.figures if fig.figure_id not in linked_ids]
    return figure_map, main_figures, linked_ids


def render_report_html(
    spec: ReportSpec | dict[str, Any],
    workspace: RunWorkspace,
    *,
    asset_prefix: str = "/assets",
    run_prefix: str = "/run",
) -> str:
    """Compile a validated ``ReportSpec`` into self-contained offline academic HTML."""
    report = spec if isinstance(spec, ReportSpec) else ReportSpec.model_validate(spec)
    lang: ReportLanguage = report.language
    labels = REPORT_LABELS[lang]

    figure_map, main_figures, _ = index_and_validate_figures(report)
    calc_ledger = load_calculations(workspace)

    # Masthead
    kicker_parts: list[str] = []
    if report.institution:
        kicker_parts.append(report.institution.strip())
    if report.lab_number:
        kicker_parts.append(f"{labels['lab_prefix']} {report.lab_number.strip()}")
    masthead_parts: list[str] = []
    if kicker_parts:
        masthead_parts.append(
            f'<div class="masthead-kicker">{_esc(" · ".join(kicker_parts))}</div>'
        )
    masthead_parts.append(f'<h1 class="masthead-title">{_esc(report.title)}</h1>')
    if report.subtitle:
        masthead_parts.append(f'<div class="masthead-subtitle">{_esc(report.subtitle)}</div>')

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
        items_html = "".join(
            f'<div class="meta-item"><span class="meta-label">{_esc(k)}:</span> '
            f'<span class="meta-value">{_esc(v)}</span></div>'
            for k, v in meta_pairs
        )
        masthead_parts.append(f'<div class="meta-grid">{items_html}</div>')

    # Dynamic numbered body sections
    body_sections: list[str] = []
    sec_num = 0
    fig_counter = 0
    tbl_counter = 0
    seen_fig_ids: dict[str, int] = {}

    def _section(title: str, inner_html: str) -> None:
        nonlocal sec_num
        sec_num += 1
        body_sections.append(
            f'<section class="report-section">\n'
            f'  <h2 class="section-heading">{sec_num}. {_esc(title)}</h2>\n'
            f"  {inner_html}\n"
            f"</section>"
        )

    if report.objective.strip():
        _section(labels["objective"], _render_paragraphs_html(report.objective))

    if report.apparatus:
        items = "".join(f"<li>{_esc(item)}</li>" for item in report.apparatus if item.strip())
        if items:
            _section(labels["apparatus"], f'<ul class="academic-list">{items}</ul>')

    if report.theory_sections:
        theory_blocks: list[str] = []
        for entry in report.theory_sections:
            heading = (entry.get("heading") or entry.get("title") or "").strip()
            body = (
                entry.get("body")
                or entry.get("content")
                or entry.get("text")
                or ""
            ).strip()
            eq = (entry.get("equation") or entry.get("equations") or "").strip()
            block_parts: list[str] = []
            if heading:
                block_parts.append(f'<h3 class="subsection-heading">{_esc(heading)}</h3>')
            if body:
                block_parts.append(_render_paragraphs_html(body))
            if eq:
                for eq_block in split_equation_blocks(eq):
                    block_parts.append(_render_equation_block_html(eq_block))
            if block_parts:
                theory_blocks.append("\n".join(block_parts))
        if theory_blocks:
            _section(labels["theory"], "\n".join(theory_blocks))

    if report.procedure:
        steps = "".join(f"<li>{_esc(step)}</li>" for step in report.procedure if step.strip())
        if steps:
            _section(labels["procedure"], f'<ol class="academic-list">{steps}</ol>')

    if report.tables or main_figures:
        res_blocks: list[str] = []
        for tbl in report.tables:
            tbl_counter += 1
            res_blocks.append(_render_table_html(tbl, tbl_counter, language=lang))
        for fig in main_figures:
            fig_counter += 1
            res_blocks.append(
                _render_figure_html(
                    fig,
                    fig_counter,
                    workspace,
                    run_prefix=run_prefix,
                    language=lang,
                    seen_fig_ids=seen_fig_ids,
                )
            )
        _section(labels["results"], "\n".join(res_blocks))

    if report.analysis_paragraphs:
        analysis_html = "\n".join(
            _render_paragraphs_html(p) for p in report.analysis_paragraphs if p.strip()
        )
        if analysis_html:
            _section(labels["analysis"], analysis_html)

    if report.calc_ids:
        _section(
            labels["calculations"],
            _render_calc_ledger_table_html(report.calc_ids, calc_ledger, language=lang),
        )

    if report.conclusions:
        conc_items = "".join(
            f"<li>{_esc(item)}</li>" for item in report.conclusions if item.strip()
        )
        if conc_items:
            _section(labels["conclusions"], f'<ol class="academic-list">{conc_items}</ol>')

    # Strict opt-in: Self-Control Questions & Answers
    if report.options.include_self_control and report.self_control:
        qa_cards: list[str] = []
        for item in report.self_control:
            eq_html = "\n".join(
                _render_equation_block_html(eq) for eq in item.equations if eq.strip()
            )
            figs_html_parts: list[str] = []
            for fid in item.figure_ids:
                fig_counter += 1
                figs_html_parts.append(
                    _render_figure_html(
                        figure_map[fid],
                        fig_counter,
                        workspace,
                        run_prefix=run_prefix,
                        language=lang,
                        seen_fig_ids=seen_fig_ids,
                    )
                )
            qa_compact = (
                not item.figure_ids
                and len(item.equations) <= 2
                and len(item.question) + len(item.answer) <= 900
                and len(re.split(r"\n\s*\n", item.answer.strip())) <= 3
            )
            compact_cls = " card-compact" if qa_compact else ""
            qa_cards.append(
                f'<article class="qa-card{compact_cls}">\n'
                f'  <div class="qa-card-header">'
                f'<span class="qa-number">{_esc(labels["question_prefix"])} '
                f"{_esc(item.number)}.</span> {_esc(item.question)}</div>\n"
                f'  <div class="qa-answer"><span class="qa-answer-label">'
                f'{_esc(labels["answer_prefix"])}:</span> '
                f"{_render_paragraphs_html(item.answer)}</div>\n"
                f"  {eq_html}\n"
                f"  {chr(10).join(figs_html_parts)}\n"
                f"</article>"
            )
        _section(labels["self_control"], "\n".join(qa_cards))

    # Strict opt-in: Additional Worked Problems
    if report.options.include_problems and report.problems:
        prob_cards: list[str] = []
        for item in report.problems:
            calcs_html = _render_calc_strip_html(item.calc_ids, calc_ledger, language=lang)
            figs_html_parts = []
            for fid in item.figure_ids:
                fig_counter += 1
                figs_html_parts.append(
                    _render_figure_html(
                        figure_map[fid],
                        fig_counter,
                        workspace,
                        run_prefix=run_prefix,
                        language=lang,
                        seen_fig_ids=seen_fig_ids,
                    )
                )
            prob_compact = (
                not item.figure_ids
                and len(item.calc_ids) <= 4
                and len(item.prompt) + len(item.solution) <= 900
                and len(re.split(r"\n\s*\n", item.solution.strip())) <= 3
            )
            compact_cls = " card-compact" if prob_compact else ""
            prob_cards.append(
                f'<article class="problem-card{compact_cls}">\n'
                f'  <div class="problem-card-header">'
                f'<span class="problem-number">{_esc(labels["problem_prefix"])} '
                f"{_esc(item.number)}.</span> {_esc(item.prompt)}</div>\n"
                f'  <div class="problem-solution"><span class="problem-solution-label">'
                f'{_esc(labels["solution_prefix"])}:</span> '
                f"{_render_paragraphs_html(item.solution)}</div>\n"
                f"  {calcs_html}\n"
                f"  {chr(10).join(figs_html_parts)}\n"
                f"</article>"
            )
        _section(labels["problems"], "\n".join(prob_cards))

    # Strict opt-in: Evidence & Provenance Appendix
    if report.options.include_evidence_appendix:
        _section(
            labels["evidence_appendix"],
            _render_provenance_appendix_html(workspace, language=lang),
        )

    clean_asset_prefix = asset_prefix.rstrip("/")
    css = render_template_tokens(
        load_template("report.css"),
        {"{{ASSET_PREFIX}}": clean_asset_prefix},
    )
    template = load_template("report.html")
    return render_template_tokens(
        template,
        {
            "{{LANG}}": lang,
            "{{DOC_TITLE}}": _esc(report.title),
            "{{ASSET_PREFIX}}": clean_asset_prefix,
            "{{REPORT_CSS}}": css,
            "{{MASTHEAD_HTML}}": "\n".join(masthead_parts),
            "{{BODY_HTML}}": "\n".join(body_sections),
        },
    )


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _MIME_TYPES:
        return _MIME_TYPES[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


class _OfflineRenderServer:
    """Ephemeral loopback-only HTTP server serving HTML + local KaTeX/fonts + run artifacts."""

    def __init__(
        self,
        *,
        html_content: str,
        assets_dir: Path,
        run_root: Path,
        host: str = "127.0.0.1",
    ) -> None:
        if not is_loopback_host(host):
            raise ValueError(f"Offline render server requires a loopback host; got {host!r}")
        normalized_host = host.strip()
        if normalized_host.startswith("[") and normalized_host.endswith("]"):
            normalized_host = normalized_host[1:-1]
        self._html_bytes = html_content.encode("utf-8")
        self._katex_dir = (assets_dir / "katex").resolve()
        self._fonts_dir = (assets_dir / "fonts").resolve()
        self._run_root = run_root.resolve()
        self._host = normalized_host
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.origin: str = ""

    def start(self) -> str:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _check_host(self) -> bool:
                raw_host = self.headers.get("Host", "").strip()
                if not raw_host:
                    return False
                if raw_host.startswith("["):
                    end = raw_host.find("]")
                    host_only = raw_host[1:end] if end != -1 else raw_host
                else:
                    host_only = raw_host.split(":", 1)[0]
                return is_loopback_host(host_only)

            def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if not self._check_host():
                    self._send_bytes(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
                    return

                path = unquote(urlsplit(self.path).path)
                if path in ("/", "/document.html"):
                    self._send_bytes(
                        HTTPStatus.OK,
                        parent._html_bytes,
                        "text/html; charset=utf-8",
                    )
                    return

                base_dir: Path | None = None
                rel: str = ""
                if path.startswith("/assets/katex/"):
                    base_dir = parent._katex_dir
                    rel = path.removeprefix("/assets/katex/")
                elif path.startswith("/assets/fonts/"):
                    base_dir = parent._fonts_dir
                    rel = path.removeprefix("/assets/fonts/")
                elif path.startswith("/run/"):
                    base_dir = parent._run_root
                    rel = path.removeprefix("/run/")

                if base_dir is None or not rel:
                    self._send_bytes(HTTPStatus.NOT_FOUND, b"Not Found", "text/plain")
                    return

                try:
                    resolved = resolve_confined_path(base_dir, rel)
                except PathConfinementError:
                    self._send_bytes(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
                    return

                if not resolved.is_file():
                    self._send_bytes(HTTPStatus.NOT_FOUND, b"Not Found", "text/plain")
                    return

                data = resolved.read_bytes()
                self._send_bytes(HTTPStatus.OK, data, _guess_mime(resolved))

        class _LoopbackHTTPServer(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True
            address_family = socket.AF_INET6 if ":" in self._host else socket.AF_INET

        self._httpd = _LoopbackHTTPServer((self._host, 0), _Handler)
        bound_port = self._httpd.server_address[1]
        host_disp = f"[{self._host}]" if ":" in self._host else self._host
        self.origin = f"http://{host_disp}:{bound_port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.origin

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            finally:
                self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def _build_running_footer_template(title: str, language: ReportLanguage) -> str:
    labels = REPORT_LABELS[language]
    safe_title = _esc(title[:90])
    page_word = _esc(labels["page_word"])
    of_word = _esc(labels["of_word"])
    return (
        '<div style="width:100%;font-family:\'Noto Sans\',sans-serif;font-size:8px;'
        "color:#64748b;padding:0 16mm;display:flex;justify-content:space-between;"
        'align-items:center;border-top:0.5px solid #e2e8f0;padding-top:4px;">'
        f"<span>{safe_title}</span>"
        f'<span>{page_word} <span class="pageNumber"></span> '
        f'{of_word} <span class="totalPages"></span></span>'
        "</div>"
    )


async def render_html_to_pdf(
    html_content: str,
    output_pdf_path: Path,
    workspace: RunWorkspace,
    *,
    config: PeaConfig | None = None,
    title: str = "",
    language: ReportLanguage = "en",
    display_header_footer: bool = True,
    wait_for_katex: bool = True,
    timeout_ms: int = 30_000,
) -> dict[str, Any]:
    """Render offline HTML to an A4 PDF via headless Chromium with zero external network calls."""
    from playwright.async_api import async_playwright

    cfg = config or PeaConfig.from_env()
    browser_exec = cfg.browser_executable or resolve_browser_executable()
    strict_offline_cfg = PeaConfig(
        project_root=cfg.project_root,
        local_assets_dir=cfg.local_assets_dir,
        runs_dir=cfg.runs_dir,
        profiles_dir=cfg.profiles_dir,
        loopback_host=cfg.loopback_host,
        viewer_port=cfg.viewer_port,
        allowed_origins=(),
        ocr_backend=cfg.ocr_backend,
        ocr_threads=cfg.ocr_threads,
        ocr_timeout_s=cfg.ocr_timeout_s,
        browser_executable=browser_exec,
    )

    server = _OfflineRenderServer(
        html_content=html_content,
        assets_dir=cfg.local_assets_dir,
        run_root=workspace.root,
        host=cfg.loopback_host,
    )
    origin = server.start()

    blocked_requests: list[str] = []
    failed_local_assets: list[str] = []
    page_errors: list[str] = []
    katex_errors: list[str] = []
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_pdf = output_pdf_path.with_name(f"{output_pdf_path.name}.tmp")

    playwright = None
    browser = None
    context = None
    page = None
    try:
        playwright = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": True}
        if browser_exec is not None:
            launch_kwargs["executable_path"] = str(browser_exec)
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1240, "height": 1754},
            device_scale_factor=1,
        )
        page = await context.new_page()
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        def _on_response(response: Any) -> None:
            url = response.url
            if url.startswith(f"{origin}/assets/") and response.status >= 400:
                failed_local_assets.append(f"{response.status} {url.removeprefix(origin)}")

        page.on("response", _on_response)

        async def _route_guard(route: Any) -> None:
            url = route.request.url
            if url.startswith("data:") or is_origin_allowed(url, origin, strict_offline_cfg):
                await route.continue_()
            else:
                blocked_requests.append(url)
                await route.abort("blockedbyclient")

        await page.route("**/*", _route_guard)
        await page.goto(f"{origin}/document.html", wait_until="load", timeout=timeout_ms)

        if wait_for_katex:
            await page.wait_for_function("window.__katexDone === true", timeout=timeout_ms)
            raw_katex_errs = await page.evaluate("window.__katexErrors || []")
            if isinstance(raw_katex_errs, list):
                katex_errors = [str(e) for e in raw_katex_errs]

        # Ensure every embedded SVG/PNG image has finished decoding before printing
        broken_images = await page.evaluate(
            """async () => {
                const imgs = Array.from(document.querySelectorAll('img'));
                const broken = [];
                for (const img of imgs) {
                    if (!img.complete) {
                        await new Promise((resolve) => {
                            img.addEventListener('load', resolve, { once: true });
                            img.addEventListener('error', resolve, { once: true });
                        });
                    }
                    if (img.naturalWidth === 0) {
                        broken.push(img.getAttribute('src') || 'unknown');
                    }
                }
                return broken;
            }"""
        )
        if broken_images:
            raise RuntimeError(f"Failed to load report figure(s) in Chromium: {broken_images}")
        if blocked_requests:
            raise RuntimeError(
                f"Blocked disallowed external network request(s) during offline PDF render: "
                f"{blocked_requests}"
            )
        if failed_local_assets:
            raise RuntimeError(
                f"Failed to load required offline report asset(s) from local_assets: "
                f"{failed_local_assets}"
            )
        if wait_for_katex and any("renderMathInElement not available" in e for e in katex_errors):
            raise RuntimeError(
                "Offline KaTeX auto-render script failed to initialize "
                "(renderMathInElement not available)"
            )

        pdf_kwargs: dict[str, Any] = {
            "path": str(tmp_pdf),
            "format": "A4",
            "print_background": True,
            "prefer_css_page_size": True,
            "display_header_footer": display_header_footer,
        }
        if display_header_footer:
            pdf_kwargs["header_template"] = "<div></div>"
            pdf_kwargs["footer_template"] = _build_running_footer_template(title, language)

        await page.pdf(**pdf_kwargs)
        os.replace(tmp_pdf, output_pdf_path)

        return {
            "blocked_requests": blocked_requests,
            "page_errors": page_errors,
            "katex_errors": katex_errors,
        }
    except Exception:
        if tmp_pdf.exists():
            try:
                tmp_pdf.unlink()
            except OSError:
                pass
        raise
    finally:
        for obj in (page, context, browser):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        server.stop()


def count_pdf_pages(pdf_path: Path) -> int:
    """Return the exact page count of a PDF file using ``pypdfium2``."""
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(doc)
    finally:
        doc.close()


def preview_report_pages(
    pdf_path: str | Path,
    workspace: RunWorkspace,
    *,
    dpi: int = 150,
    max_pages: int = 30,
    pages: list[int] | None = None,
    output_subdir: str = "reports/previews",
    prefix: str = "page",
) -> list[str]:
    """Render compiled report PDF pages to PNG images under ``workspace`` for visual QA."""
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not (72 <= dpi <= 600):
        raise ValueError(f"dpi must be an integer in 72..600; got {dpi!r}")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError(f"max_pages must be an integer >= 1; got {max_pages!r}")
    if prefix in (".", "..") or not _FILENAME_RE.match(prefix):
        raise PathConfinementError(f"Invalid preview filename prefix: {prefix!r}")

    resolved_pdf = workspace.resolve_path(pdf_path)
    if not resolved_pdf.is_file():
        raise FileNotFoundError(f"PDF not found inside run {workspace.run_id}: {pdf_path}")

    out_dir = workspace.resolve_path(output_subdir)
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = pdfium.PdfDocument(str(resolved_pdf))
    try:
        total = len(doc)
        if total < 1:
            return []

        if pages is None:
            target_pages = list(range(1, min(total, max_pages) + 1))
        else:
            if not pages:
                raise ValueError("pages list must not be empty when provided")
            seen: set[int] = set()
            target_pages = []
            for p in pages:
                if isinstance(p, bool) or not isinstance(p, int):
                    raise ValueError(f"Page number must be an integer; got {p!r}")
                if p < 1 or p > total:
                    raise ValueError(f"Requested preview page {p} is out of range 1..{total}")
                if p not in seen:
                    seen.add(p)
                    target_pages.append(p)
            target_pages = target_pages[:max_pages]

        scale = dpi / 72.0
        relpaths: list[str] = []
        for page_num in target_pages:
            out_png = out_dir / f"{prefix}-{page_num:03d}.png"
            tmp_png = out_png.with_name(f"{out_png.name}.tmp")
            page_obj = doc[page_num - 1]
            bitmap = None
            pil_img = None
            try:
                bitmap = page_obj.render(scale=scale)
                pil_img = bitmap.to_pil()
                pil_img.save(tmp_png, format="PNG")
                os.replace(tmp_png, out_png)
            except Exception:
                if tmp_png.exists():
                    try:
                        tmp_png.unlink()
                    except OSError:
                        pass
                raise
            finally:
                if pil_img is not None:
                    try:
                        pil_img.close()
                    except Exception:
                        pass
                if bitmap is not None:
                    try:
                        bitmap.close()
                    except Exception:
                        pass
                try:
                    page_obj.close()
                except Exception:
                    pass

            relpaths.append(out_png.relative_to(workspace.root).as_posix())
        return relpaths
    finally:
        doc.close()


async def build_report_pdf(
    spec: ReportSpec | dict[str, Any],
    workspace: RunWorkspace,
    *,
    config: PeaConfig | None = None,
    filename: str = "report.pdf",
    render_previews: bool = True,
    preview_dpi: int = 150,
    preview_max_pages: int = 30,
    save_html: bool = True,
    overwrite: bool = False,
) -> ReportBuildResult:
    """Compile a ``ReportSpec`` into an A4 PDF + optional page PNG previews and log provenance."""
    report = spec if isinstance(spec, ReportSpec) else ReportSpec.model_validate(spec)
    safe_name = validate_report_filename(filename, expected_suffix=".pdf")
    workspace.ensure_dirs()

    pdf_relpath = f"reports/{safe_name}"
    stem = Path(safe_name).stem
    html_relpath = f"reports/{stem}.html"
    check_targets = (pdf_relpath, html_relpath) if save_html else (pdf_relpath,)
    assert_provenance_safe_report_write(workspace, check_targets, overwrite=overwrite)

    html_content = render_report_html(report, workspace)

    pdf_path = workspace.resolve_path(pdf_relpath)
    render_meta = await render_html_to_pdf(
        html_content,
        pdf_path,
        workspace,
        config=config,
        title=report.title,
        language=report.language,
        display_header_footer=True,
        wait_for_katex=True,
    )

    if save_html:
        html_path = workspace.resolve_path(html_relpath)
        tmp_html = html_path.with_name(f"{html_path.name}.tmp")
        try:
            tmp_html.write_text(html_content, encoding="utf-8")
            os.replace(tmp_html, html_path)
        except Exception:
            if tmp_html.exists():
                try:
                    tmp_html.unlink()
                except OSError:
                    pass
            raise

    page_count = count_pdf_pages(pdf_path)
    entry = workspace.record_artifact(
        pdf_relpath,
        kind="report_pdf",
        source="model_computed",
        metadata={
            "title": report.title,
            "language": report.language,
            "page_count": page_count,
            "include_self_control": report.options.include_self_control,
            "include_problems": report.options.include_problems,
            "include_evidence_appendix": report.options.include_evidence_appendix,
            "katex_errors": render_meta["katex_errors"],
        },
    )

    preview_relpaths: list[str] = []
    if render_previews and page_count > 0:
        preview_relpaths = preview_report_pages(
            pdf_relpath,
            workspace,
            dpi=preview_dpi,
            max_pages=preview_max_pages,
            output_subdir=f"reports/previews/{stem}",
            prefix="page",
        )

    return ReportBuildResult(
        run_id=workspace.run_id,
        pdf_relpath=entry.relpath,
        pdf_sha256=entry.sha256,
        preview_png_relpaths=preview_relpaths,
        page_count=page_count,
    )


__all__ = [
    "REPORT_LABELS",
    "assert_provenance_safe_report_write",
    "build_report_pdf",
    "count_pdf_pages",
    "format_calc_value",
    "index_and_validate_figures",
    "load_template",
    "preview_report_pages",
    "provenance_badge_html",
    "render_html_to_pdf",
    "render_report_html",
    "render_template_tokens",
    "split_equation_blocks",
    "validate_report_filename",
]
