"""Submission-ready report compiler: offline HTML+KaTeX PDF, editable DOCX, and evidence packets."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pea.report.docx_builder import build_report_docx, latex_to_readable_text
from pea.report.evidence import (
    EvidenceZipResult,
    TeacherEvidenceResult,
    build_evidence_zip,
    build_teacher_evidence_pdf,
    render_teacher_evidence_html,
)
from pea.report.pdf_builder import (
    REPORT_LABELS,
    build_report_pdf,
    count_pdf_pages,
    preview_report_pages,
    render_html_to_pdf,
    render_report_html,
)
from pea.schemas import ReportBuildResult, ReportSpec

if TYPE_CHECKING:
    from pea.config import PeaConfig
    from pea.workspace import RunWorkspace

_ALLOWED_FORMATS: frozenset[str] = frozenset({"pdf", "docx", "evidence_zip"})


async def build_report(
    spec: ReportSpec | dict[str, Any],
    workspace: RunWorkspace,
    *,
    config: PeaConfig | None = None,
    formats: Sequence[str] = ("pdf", "docx", "evidence_zip"),
    include_teacher_packet: bool = False,
    render_previews: bool = True,
    preview_dpi: int = 150,
    profile_paths: list[str | Path] | None = None,
    overwrite: bool = False,
) -> ReportBuildResult:
    """Build requested report outputs (PDF, DOCX, Teacher Evidence PDF, Evidence ZIP) atomically."""
    report = spec if isinstance(spec, ReportSpec) else ReportSpec.model_validate(spec)
    requested = tuple(dict.fromkeys(fmt.strip().lower() for fmt in formats))
    invalid = [fmt for fmt in requested if fmt not in _ALLOWED_FORMATS]
    if invalid:
        raise ValueError(
            f"Unsupported report format(s) {invalid}; expected subset of {sorted(_ALLOWED_FORMATS)}"
        )
    if not requested and not include_teacher_packet:
        raise ValueError("At least one format or include_teacher_packet=True is required")

    pdf_relpath: str | None = None
    pdf_sha256: str | None = None
    docx_relpath: str | None = None
    docx_sha256: str | None = None
    evidence_zip_relpath: str | None = None
    evidence_zip_sha256: str | None = None
    teacher_packet_relpath: str | None = None
    teacher_packet_sha256: str | None = None
    preview_png_relpaths: list[str] = []
    page_count = 0

    if "pdf" in requested:
        pdf_res = await build_report_pdf(
            report,
            workspace,
            config=config,
            render_previews=render_previews,
            preview_dpi=preview_dpi,
            overwrite=overwrite,
        )
        pdf_relpath = pdf_res.pdf_relpath
        pdf_sha256 = pdf_res.pdf_sha256
        preview_png_relpaths = pdf_res.preview_png_relpaths
        page_count = pdf_res.page_count

    if "docx" in requested:
        docx_res = build_report_docx(
            report,
            workspace,
            overwrite=overwrite,
        )
        docx_relpath = docx_res.docx_relpath
        docx_sha256 = docx_res.docx_sha256

    if include_teacher_packet:
        teacher_res = await build_teacher_evidence_pdf(
            workspace,
            config=config,
            title=f"{report.title} — Evidence Packet",
            language=report.language,
            overwrite=overwrite,
        )
        teacher_packet_relpath = teacher_res.pdf_relpath
        teacher_packet_sha256 = teacher_res.pdf_sha256

    if "evidence_zip" in requested:
        zip_res = build_evidence_zip(
            workspace,
            profile_paths=profile_paths,
            overwrite=overwrite,
        )
        evidence_zip_relpath = zip_res.zip_relpath
        evidence_zip_sha256 = zip_res.zip_sha256

    return ReportBuildResult(
        run_id=workspace.run_id,
        pdf_relpath=pdf_relpath,
        pdf_sha256=pdf_sha256,
        docx_relpath=docx_relpath,
        docx_sha256=docx_sha256,
        evidence_zip_relpath=evidence_zip_relpath,
        evidence_zip_sha256=evidence_zip_sha256,
        teacher_packet_relpath=teacher_packet_relpath,
        teacher_packet_sha256=teacher_packet_sha256,
        preview_png_relpaths=preview_png_relpaths,
        page_count=page_count,
    )


__all__ = [
    "EvidenceZipResult",
    "REPORT_LABELS",
    "TeacherEvidenceResult",
    "build_evidence_zip",
    "build_report",
    "build_report_docx",
    "build_report_pdf",
    "build_teacher_evidence_pdf",
    "count_pdf_pages",
    "latex_to_readable_text",
    "preview_report_pages",
    "render_html_to_pdf",
    "render_report_html",
    "render_teacher_evidence_html",
]
