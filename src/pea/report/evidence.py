"""Raw evidence ZIP packager and optional Teacher Evidence Audit Packet PDF builder."""

from __future__ import annotations

import html
import json
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from pydantic import Field, ValidationError, field_validator

from pea.config import PathConfinementError, PeaConfig, resolve_confined_path
from pea.math_engine.calculator import load_calculations
from pea.report.pdf_builder import (
    REPORT_LABELS,
    assert_provenance_safe_report_write,
    count_pdf_pages,
    load_template,
    provenance_badge_html,
    render_html_to_pdf,
    render_template_tokens,
    validate_report_filename,
)
from pea.schemas import (
    ProvenanceEntry,
    ReportLanguage,
    StrictModel,
    _validate_relpath,
    _validate_sha256,
)
from pea.workspace import sha256_file

if TYPE_CHECKING:
    from pea.workspace import RunWorkspace

_EVIDENCE_SUBDIRS: tuple[str, ...] = (
    "evidence",
    "datasets",
    "crops",
    "ocr",
    "profiles",
)
_ROOT_LOG_SUFFIXES: frozenset[str] = frozenset({".json", ".jsonl", ".csv"})
_IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})


class EvidenceZipResult(StrictModel):
    """Structured result of ``build_evidence_zip``."""

    run_id: str = Field(min_length=1)
    zip_relpath: str = Field(min_length=1)
    zip_sha256: str
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    included_relpaths: list[str] = Field(default_factory=list)

    @field_validator("zip_relpath")
    @classmethod
    def _check_relpath(cls, value: str) -> str:
        return _validate_relpath(value)

    @field_validator("zip_sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class TeacherEvidenceResult(StrictModel):
    """Structured result of ``build_teacher_evidence_pdf``."""

    run_id: str = Field(min_length=1)
    pdf_relpath: str = Field(min_length=1)
    pdf_sha256: str
    page_count: int = Field(ge=1)
    screenshot_count: int = Field(ge=0)
    provenance_ok: bool

    @field_validator("pdf_relpath")
    @classmethod
    def _check_relpath(cls, value: str) -> str:
        return _validate_relpath(value)

    @field_validator("pdf_sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


def _is_temp_file(path: Path) -> bool:
    return path.name.endswith(".tmp") or ".tmp." in path.name


def _collect_evidence_files(
    workspace: RunWorkspace,
    *,
    exclude_relpath: str,
) -> list[tuple[str, Path]]:
    """Collect confined raw evidence, provenance, dataset, and OCR log files deterministically."""
    collected: dict[str, Path] = {}

    def _try_add(candidate: Path, rel_override: str | None = None) -> None:
        if _is_temp_file(candidate):
            return
        try:
            resolved = resolve_confined_path(workspace.root, candidate)
        except PathConfinementError:
            return
        if not resolved.is_file():
            return
        rel = rel_override or resolved.relative_to(workspace.root).as_posix()
        if rel == exclude_relpath or rel.startswith("reports/"):
            return
        collected[rel] = resolved

    # 1. Standard run-root metadata, provenance ledger, calculations, and sweep/search logs
    if workspace.root.is_dir():
        for child in sorted(workspace.root.iterdir(), key=lambda p: p.name):
            if child.is_file() and child.suffix.lower() in _ROOT_LOG_SUFFIXES:
                _try_add(child)

    # 2. Evidence, datasets, crops, OCR logs, and run-local profiles
    for subdir in _EVIDENCE_SUBDIRS:
        sub_path = workspace.root / subdir
        if not sub_path.is_dir():
            continue
        for file_path in sorted(sub_path.rglob("*"), key=lambda p: p.as_posix()):
            if file_path.is_file():
                _try_add(file_path)

    # 3. Handout extracted text JSON (if present)
    pdf_pages_dir = workspace.root / "pdf_pages"
    if pdf_pages_dir.is_dir():
        for text_json in sorted(pdf_pages_dir.rglob("extracted-text.json")):
            if text_json.is_file():
                _try_add(text_json)

    return sorted(collected.items(), key=lambda item: item[0])


def build_evidence_zip(
    workspace: RunWorkspace,
    *,
    filename: str = "evidence.zip",
    profile_paths: list[str | Path] | None = None,
    overwrite: bool = False,
) -> EvidenceZipResult:
    """Package raw run evidence, provenance ledger, datasets, OCR logs, and profiles into a ZIP.

    Strictly excludes the output archive itself, ``reports/`` outputs, ``*.tmp`` files,
    and any symlinks escaping ``workspace.root``. Records the resulting archive in
    ``provenance.jsonl`` with ``kind="evidence_zip"``.
    """
    safe_name = validate_report_filename(filename, expected_suffix=".zip")
    workspace.ensure_dirs()

    zip_relpath = f"reports/{safe_name}"
    assert_provenance_safe_report_write(workspace, (zip_relpath,), overwrite=overwrite)

    provenance_check = workspace.verify_provenance()
    entries = _collect_evidence_files(workspace, exclude_relpath=zip_relpath)

    # Optional external calibration profile files (e.g. profiles/local/<sha256>.json)
    extra_entries: list[tuple[str, Path]] = []
    seen_arcnames = {arc for arc, _ in entries}
    seen_resolved = {fp for _, fp in entries}
    if profile_paths:
        for raw_p in profile_paths:
            p = Path(raw_p).expanduser().resolve()
            if not p.is_file() or _is_temp_file(p):
                raise FileNotFoundError(f"Calibration profile not found: {p.name}")
            if p in seen_resolved:
                continue
            arcname = f"profiles/{p.name}"
            if arcname in seen_arcnames:
                suffix = p.suffix
                stem = p.stem
                idx = 2
                while f"profiles/{stem}-{idx}{suffix}" in seen_arcnames:
                    idx += 1
                arcname = f"profiles/{stem}-{idx}{suffix}"
            seen_resolved.add(p)
            seen_arcnames.add(arcname)
            extra_entries.append((arcname, p))

    all_entries = sorted([*entries, *extra_entries], key=lambda item: item[0])

    manifest_files: list[dict[str, Any]] = []
    total_uncompressed = 0
    for arcname, file_path in all_entries:
        digest, size_bytes = sha256_file(file_path)
        total_uncompressed += size_bytes
        manifest_files.append(
            {
                "relpath": arcname,
                "sha256": digest,
                "size_bytes": size_bytes,
            }
        )

    manifest_payload = {
        "schema_version": 1,
        "run_id": workspace.run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "provenance_verified": provenance_check.model_dump(),
        "file_count": len(manifest_files),
        "files": manifest_files,
    }
    manifest_bytes = (json.dumps(manifest_payload, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )

    out_path = workspace.resolve_path(zip_relpath)
    tmp_path = out_path.with_name(f"{out_path.name}.tmp")
    included_arcnames: list[str] = ["manifest.json"]

    try:
        with zipfile.ZipFile(tmp_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", manifest_bytes)
            for arcname, file_path in all_entries:
                zf.write(file_path, arcname=arcname)
                included_arcnames.append(arcname)
        os.replace(tmp_path, out_path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise

    entry = workspace.record_artifact(
        zip_relpath,
        kind="evidence_zip",
        source="simulator_captured",
        metadata={
            "file_count": len(included_arcnames),
            "uncompressed_bytes": total_uncompressed + len(manifest_bytes),
            "provenance_ok": provenance_check.ok,
        },
    )

    return EvidenceZipResult(
        run_id=workspace.run_id,
        zip_relpath=entry.relpath,
        zip_sha256=entry.sha256,
        file_count=len(included_arcnames),
        total_bytes=entry.size_bytes,
        included_relpaths=included_arcnames,
    )


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _encode_relpath_url(relpath: str) -> str:
    return "/".join(quote(part, safe="") for part in relpath.split("/") if part)


def _read_provenance_lenient(workspace: RunWorkspace) -> list[ProvenanceEntry]:
    """Parse all valid lines of ``provenance.jsonl``, skipping malformed lines for audit reports."""
    if not workspace.provenance_path.is_file():
        return []
    entries: list[ProvenanceEntry] = []
    with workspace.provenance_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                entries.append(ProvenanceEntry.model_validate_json(stripped))
            except ValidationError:
                continue
    return entries


def _collect_evidence_screenshots(
    workspace: RunWorkspace,
    provenance_by_rel: dict[str, ProvenanceEntry],
    *,
    max_screenshots: int,
) -> tuple[list[dict[str, Any]], int]:
    shots: list[dict[str, Any]] = []
    total_found = 0
    if not workspace.evidence_dir.is_dir():
        return shots, 0

    for path in sorted(workspace.evidence_dir.rglob("*"), key=lambda p: p.as_posix()):
        if not path.is_file() or _is_temp_file(path):
            continue
        if path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        try:
            resolved = resolve_confined_path(workspace.root, path)
        except PathConfinementError:
            continue
        total_found += 1
        if len(shots) >= max_screenshots:
            continue

        relpath = resolved.relative_to(workspace.root).as_posix()
        actual_digest, actual_size = sha256_file(resolved)
        prov = provenance_by_rel.get(relpath)
        if prov is not None:
            ledger_digest = prov.sha256
            tampered = actual_digest != prov.sha256 or actual_size != prov.size_bytes
            source = prov.source
            timestamp = prov.timestamp.isoformat()
        else:
            ledger_digest = None
            tampered = False
            source = "simulator_captured"
            timestamp = "—"
        shots.append(
            {
                "relpath": relpath,
                "sha256": actual_digest,
                "ledger_sha256": ledger_digest,
                "tampered": tampered,
                "size_bytes": actual_size,
                "source": source,
                "timestamp": timestamp,
            }
        )
    return shots, total_found


def render_teacher_evidence_html(
    workspace: RunWorkspace,
    *,
    title: str | None = None,
    language: ReportLanguage = "en",
    max_screenshots: int = 100,
    asset_prefix: str = "/assets",
    run_prefix: str = "/run",
) -> tuple[str, int, bool]:
    """Render the Teacher Evidence Audit Packet HTML; returns ``(html, shot_count, prov_ok)``."""
    if (
        isinstance(max_screenshots, bool)
        or not isinstance(max_screenshots, int)
        or max_screenshots < 1
    ):
        raise ValueError(f"max_screenshots must be an integer >= 1; got {max_screenshots!r}")

    labels = REPORT_LABELS[language]
    doc_title = title.strip() if title and title.strip() else labels["teacher_default_title"]

    entries = _read_provenance_lenient(workspace)
    prov_check = workspace.verify_provenance()
    provenance_by_rel = {e.relpath: e for e in entries}
    calcs = load_calculations(workspace)

    status_phase = "unknown"
    run_label = ""
    if workspace.status_path.is_file():
        try:
            st = workspace.read_status()
            status_phase = st.phase
            run_label = st.label or ""
        except Exception:
            status_phase = "unreadable"

    shots, total_shots = _collect_evidence_screenshots(
        workspace,
        provenance_by_rel,
        max_screenshots=max_screenshots,
    )

    masthead_parts = [
        f'<div class="masthead-kicker">{_esc(labels["teacher_kicker"])}</div>',
        f'<h1 class="masthead-title">{_esc(doc_title)}</h1>',
    ]
    if run_label:
        masthead_parts.append(f'<div class="masthead-subtitle">{_esc(run_label)}</div>')
    masthead_parts.append(
        '<div class="meta-grid">'
        f'<div class="meta-item"><span class="meta-label">{_esc(labels["teacher_run_id"])}:'
        f'</span> <span class="meta-value mono">{_esc(workspace.run_id)}</span></div>'
        f'<div class="meta-item"><span class="meta-label">{_esc(labels["teacher_status"])}:'
        f'</span> <span class="meta-value">{_esc(status_phase)}</span></div>'
        "</div>"
    )

    ver_badge = (
        f'<span class="badge badge-model">{_esc(labels["teacher_verified"])}</span>'
        if prov_check.ok
        else f'<span class="badge badge-fail">{_esc(labels["teacher_failed"])}</span>'
    )
    shots_card_val = (
        f"{len(shots)} / {total_shots}" if total_shots > len(shots) else str(len(shots))
    )
    summary_parts = [
        '<div class="summary-cards">',
        (
            '<div class="summary-card">'
            f'<div class="summary-card-label">{_esc(labels["teacher_provenance"])}</div>'
            f'<div class="summary-card-value">{ver_badge}</div>'
            "</div>"
        ),
        (
            '<div class="summary-card">'
            f'<div class="summary-card-label">{_esc(labels["teacher_artifacts"])}</div>'
            f'<div class="summary-card-value">{len(entries)}</div>'
            "</div>"
        ),
        (
            '<div class="summary-card">'
            f'<div class="summary-card-label">{_esc(labels["teacher_screenshots"])}</div>'
            f'<div class="summary-card-value">{_esc(shots_card_val)}</div>'
            "</div>"
        ),
        (
            '<div class="summary-card">'
            f'<div class="summary-card-label">{_esc(labels["teacher_calcs"])}</div>'
            f'<div class="summary-card-value">{len(calcs)}</div>'
            "</div>"
        ),
        "</div>",
    ]
    if total_shots > len(shots):
        trunc_msg = labels["teacher_shots_truncated"].format(shown=len(shots), total=total_shots)
        summary_parts.append(f'<div class="empty-notice">{_esc(trunc_msg)}</div>')
    if prov_check.errors:
        err_items = "".join(f"<li>{_esc(e)}</li>" for e in prov_check.errors)
        summary_parts.append(
            f'<div class="empty-notice"><ul class="academic-list">{err_items}</ul></div>'
        )

    # Section 1: SHA-256 index table
    if entries:
        index_rows: list[str] = []
        for e in entries:
            badge = provenance_badge_html(e.source, language)
            index_rows.append(
                f'<tr><td class="mono path-cell">{_esc(e.relpath)}</td>'
                f'<td class="nowrap-cell">{_esc(e.kind)}</td>'
                f'<td class="nowrap-cell">{badge}</td>'
                f'<td class="num-cell">{e.size_bytes}</td>'
                f'<td class="sha-cell">{_esc(e.sha256)}</td></tr>'
            )
        index_body = (
            '<table class="data-table">\n'
            f"  <thead><tr>"
            f"<th>{_esc(labels['artifact'])}</th>"
            f'<th class="nowrap-cell">{_esc(labels["kind"])}</th>'
            f'<th class="nowrap-cell">{_esc(labels["provenance"])}</th>'
            f'<th class="num-cell">{_esc(labels["size_bytes"])}</th>'
            f"<th>{_esc(labels['sha256'])}</th>"
            "</tr></thead>\n"
            f"  <tbody>{''.join(index_rows)}</tbody>\n"
            "</table>"
        )
    else:
        index_body = f'<div class="empty-notice">{_esc(labels["no_provenance"])}</div>'

    index_html = (
        '<section class="report-section">\n'
        f'  <h2 class="section-heading">{_esc(labels["teacher_index_heading"])}</h2>\n'
        f"  {index_body}\n"
        "</section>"
    )

    # Section 2: Full-page screenshots
    if shots:
        shot_pages: list[str] = []
        for idx, shot in enumerate(shots, start=1):
            img_url = f"{run_prefix.rstrip('/')}/{_encode_relpath_url(shot['relpath'])}"
            badge = provenance_badge_html(shot["source"], language)
            if shot["tampered"]:
                badge = (
                    f'{badge} <span class="badge badge-fail">'
                    f'{_esc(labels["teacher_failed"])}</span>'
                )
            heading_line = (
                f'<h2 class="section-heading">{_esc(labels["teacher_shots_heading"])}</h2>\n'
                if idx == 1
                else ""
            )
            if shot["tampered"] and shot["ledger_sha256"]:
                sha_lines = (
                    f'      <div class="sha-mismatch">'
                    f'<strong>{_esc(labels["teacher_actual_sha256"])}:</strong> '
                    f'<span class="sha-cell">{_esc(shot["sha256"])}</span></div>\n'
                    f'      <div><strong>{_esc(labels["teacher_ledger_sha256"])}:</strong> '
                    f'<span class="sha-cell">{_esc(shot["ledger_sha256"])}</span></div>\n'
                )
            else:
                sha_lines = (
                    f'      <div><strong>{_esc(labels["sha256"])}:</strong> '
                    f'<span class="sha-cell">{_esc(shot["sha256"])}</span></div>\n'
                )
            shot_pages.append(
                f'<section class="evidence-page">\n'
                f"  {heading_line}"
                f'  <div class="evidence-shot">\n'
                f'    <div class="evidence-shot-header">\n'
                f'      <strong class="mono">#{idx} · {_esc(shot["relpath"])}</strong>\n'
                f"      <span>{badge}</span>\n"
                f"    </div>\n"
                f'    <img class="evidence-shot-img" src="{_esc(img_url)}" '
                f'alt="{_esc(shot["relpath"])}" />\n'
                f'    <div class="evidence-shot-footer">\n'
                f"{sha_lines}"
                f'      <div><strong>{_esc(labels["size_bytes"])}:</strong> '
                f"{shot['size_bytes']} · "
                f'<strong>{_esc(labels["teacher_captured_at"])}:</strong> '
                f'<span class="mono">{_esc(shot["timestamp"])}</span></div>\n'
                f"    </div>\n"
                f"  </div>\n"
                f"</section>"
            )
        screenshots_html = "\n".join(shot_pages)
    else:
        screenshots_html = (
            '<section class="report-section">\n'
            f'  <h2 class="section-heading">{_esc(labels["teacher_shots_heading"])}</h2>\n'
            f'  <div class="empty-notice">{_esc(labels["teacher_no_shots"])}</div>\n'
            "</section>"
        )

    clean_asset_prefix = asset_prefix.rstrip("/")
    css = render_template_tokens(
        load_template("report.css"),
        {"{{ASSET_PREFIX}}": clean_asset_prefix},
    )
    template = load_template("teacher_evidence.html")
    rendered = render_template_tokens(
        template,
        {
            "{{LANG}}": language,
            "{{DOC_TITLE}}": _esc(doc_title),
            "{{REPORT_CSS}}": css,
            "{{MASTHEAD_HTML}}": "\n".join(masthead_parts),
            "{{SUMMARY_HTML}}": "\n".join(summary_parts),
            "{{INDEX_HTML}}": index_html,
            "{{SCREENSHOTS_HTML}}": screenshots_html,
        },
    )
    return rendered, len(shots), prov_check.ok


async def build_teacher_evidence_pdf(
    workspace: RunWorkspace,
    *,
    config: PeaConfig | None = None,
    title: str | None = None,
    language: ReportLanguage = "en",
    filename: str = "teacher-evidence.pdf",
    max_screenshots: int = 100,
    overwrite: bool = False,
) -> TeacherEvidenceResult:
    """Compile an optional multi-page Teacher Evidence Audit Packet PDF and log provenance."""
    if (
        isinstance(max_screenshots, bool)
        or not isinstance(max_screenshots, int)
        or max_screenshots < 1
    ):
        raise ValueError(f"max_screenshots must be an integer >= 1; got {max_screenshots!r}")

    safe_name = validate_report_filename(filename, expected_suffix=".pdf")
    workspace.ensure_dirs()

    pdf_relpath = f"reports/{safe_name}"
    assert_provenance_safe_report_write(workspace, (pdf_relpath,), overwrite=overwrite)

    html_content, shot_count, prov_ok = render_teacher_evidence_html(
        workspace,
        title=title,
        language=language,
        max_screenshots=max_screenshots,
    )
    pdf_path = workspace.resolve_path(pdf_relpath)
    doc_title = (
        title.strip()
        if title and title.strip()
        else REPORT_LABELS[language]["teacher_default_title"]
    )
    await render_html_to_pdf(
        html_content,
        pdf_path,
        workspace,
        config=config,
        title=doc_title,
        language=language,
        display_header_footer=True,
        wait_for_katex=True,
    )

    page_count = count_pdf_pages(pdf_path)
    entry = workspace.record_artifact(
        pdf_relpath,
        kind="teacher_evidence_pdf",
        source="simulator_captured",
        metadata={
            "language": language,
            "page_count": page_count,
            "screenshot_count": shot_count,
            "provenance_ok": prov_ok,
        },
    )

    return TeacherEvidenceResult(
        run_id=workspace.run_id,
        pdf_relpath=entry.relpath,
        pdf_sha256=entry.sha256,
        page_count=page_count,
        screenshot_count=shot_count,
        provenance_ok=prov_ok,
    )


__all__ = [
    "EvidenceZipResult",
    "TeacherEvidenceResult",
    "build_evidence_zip",
    "build_teacher_evidence_pdf",
    "render_teacher_evidence_html",
]
