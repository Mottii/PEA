"""Offline PDF handout intake: render page PNGs via pypdfium2 and extract text via pypdf."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pypdf
import pypdfium2 as pdfium
from pydantic import Field, field_validator

from pea.config import PathConfinementError
from pea.schemas import (
    ProvenanceSource,
    StrictModel,
    _validate_relpath,
    _validate_sha256,
)
from pea.workspace import sha256_file

if TYPE_CHECKING:
    from pea.workspace import RunWorkspace

_SUBDIR_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class PdfPageInfo(StrictModel):
    """Single inspected PDF page with rendered PNG metadata and extracted text."""

    page_number: int = Field(ge=1)
    image_relpath: str = Field(min_length=1)
    image_sha256: str
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    text: str = ""

    @field_validator("image_relpath")
    @classmethod
    def _check_relpath(cls, value: str) -> str:
        return _validate_relpath(value)

    @field_validator("image_sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class PdfInspectionResult(StrictModel):
    """Structured result of ``inspect_pdf`` (``lab_inspect_pdf``)."""

    run_id: str = Field(min_length=1)
    source_filename: str = Field(min_length=1)
    source_sha256: str
    total_pages: int = Field(ge=1)
    rendered_pages: int = Field(ge=1)
    truncated: bool = False
    dpi: int = Field(ge=72, le=600)
    pages: list[PdfPageInfo] = Field(min_length=1)
    full_text: str = ""
    extracted_text_relpath: str = Field(min_length=1)
    extracted_text_sha256: str

    @field_validator("source_sha256", "extracted_text_sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("extracted_text_relpath")
    @classmethod
    def _check_text_relpath(cls, value: str) -> str:
        return _validate_relpath(value)


def _validate_subdir(subdir: str) -> str:
    cleaned = subdir.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or cleaned in (".", ".."):
        raise PathConfinementError(f"Invalid pdf_pages subdir: {subdir!r}")
    if not _SUBDIR_RE.match(cleaned):
        raise PathConfinementError(f"pdf_pages subdir contains disallowed characters: {subdir!r}")
    return cleaned


def _resolve_target_pages(
    total_pages: int,
    *,
    max_pages: int,
    pages: list[int] | None,
) -> tuple[list[int], bool]:
    if total_pages < 1:
        raise ValueError("PDF document contains zero pages")

    if pages is None:
        count = min(total_pages, max_pages)
        return list(range(1, count + 1)), total_pages > max_pages

    if not pages:
        raise ValueError("pages list must not be empty when provided")

    seen: set[int] = set()
    selected: list[int] = []
    for num in pages:
        if isinstance(num, bool) or not isinstance(num, int):
            raise ValueError(f"Page number must be an integer; got {num!r}")
        if num < 1 or num > total_pages:
            raise ValueError(f"Requested page {num} is out of range 1..{total_pages}")
        if num not in seen:
            seen.add(num)
            selected.append(num)

    truncated = False
    if len(selected) > max_pages:
        selected = selected[:max_pages]
        truncated = True
    return selected, truncated


def _assert_provenance_safe_targets(
    workspace: RunWorkspace,
    relpaths: list[str],
    *,
    overwrite: bool,
) -> None:
    if not overwrite:
        for rel in relpaths:
            if workspace.resolve_path(rel).exists():
                raise FileExistsError(
                    f"PDF intake artifact {rel!r} already exists in run {workspace.run_id}; "
                    "pass a unique subdir to preserve provenance immutability"
                )
        return

    recorded = {entry.relpath for entry in workspace.read_provenance()}
    conflicts = [rel for rel in relpaths if rel in recorded]
    if conflicts:
        raise FileExistsError(
            f"Cannot overwrite provenance-recorded artifact(s) {conflicts} in run "
            f"{workspace.run_id}; pass a unique subdir to preserve provenance integrity"
        )


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def inspect_pdf(
    pdf_path: str | Path,
    run_workspace: RunWorkspace,
    *,
    dpi: int = 180,
    max_pages: int = 30,
    pages: list[int] | None = None,
    subdir: str | None = None,
    source: ProvenanceSource = "bench_manual",
    overwrite: bool = False,
) -> PdfInspectionResult:
    """Render PDF pages to PNGs via ``pypdfium2`` and extract text via ``pypdf``.

    Every rendered PNG and the consolidated ``extracted-text.json`` file are written
    atomically under ``<run>/pdf_pages/`` and recorded in ``provenance.jsonl``.
    """
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not (72 <= dpi <= 600):
        raise ValueError(f"dpi must be an integer in 72..600; got {dpi!r}")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError(f"max_pages must be an integer >= 1; got {max_pages!r}")

    pdf_file = Path(pdf_path).expanduser().resolve()
    if not pdf_file.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_file.name}")
    if not pdf_file.is_file():
        raise ValueError(f"PDF path is not a regular file: {pdf_file.name}")

    source_sha256, _ = sha256_file(pdf_file)

    try:
        reader = pypdf.PdfReader(str(pdf_file))
    except Exception as exc:
        raise ValueError(f"Failed to open PDF with pypdf ({pdf_file.name}): {exc}") from exc

    if reader.is_encrypted:
        try:
            decrypt_status = reader.decrypt("")
        except Exception as exc:
            raise ValueError(f"Encrypted PDF cannot be read ({pdf_file.name}): {exc}") from exc
        if decrypt_status == 0:
            raise ValueError(
                f"Password-protected PDF cannot be read without password: {pdf_file.name}"
            )

    try:
        pdf_doc = pdfium.PdfDocument(str(pdf_file))
    except Exception as exc:
        raise ValueError(f"Failed to open PDF with pypdfium2 ({pdf_file.name}): {exc}") from exc

    try:
        total_pages = len(pdf_doc)
        if total_pages != len(reader.pages):
            total_pages = min(total_pages, len(reader.pages))

        selected_pages, truncated = _resolve_target_pages(
            total_pages,
            max_pages=max_pages,
            pages=pages,
        )

        run_workspace.ensure_dirs()
        rel_base = "pdf_pages"
        if subdir is not None:
            safe_sub = _validate_subdir(subdir)
            rel_base = f"pdf_pages/{safe_sub}"
            run_workspace.resolve_path(rel_base).mkdir(parents=True, exist_ok=True)

        planned_relpaths = [f"{rel_base}/page-{num:03d}.png" for num in selected_pages]
        text_relpath = f"{rel_base}/extracted-text.json"
        _assert_provenance_safe_targets(
            run_workspace,
            [*planned_relpaths, text_relpath],
            overwrite=overwrite,
        )

        scale = dpi / 72.0
        page_infos: list[PdfPageInfo] = []
        full_text_parts: list[str] = []

        for page_num in selected_pages:
            idx = page_num - 1
            relpath = f"{rel_base}/page-{page_num:03d}.png"
            out_path = run_workspace.resolve_path(relpath)
            tmp_path = out_path.with_name(f"{out_path.name}.tmp")

            page_obj = pdf_doc[idx]
            bitmap = None
            pil_img = None
            try:
                bitmap = page_obj.render(scale=scale)
                pil_img = bitmap.to_pil()
                width_px, height_px = pil_img.size
                pil_img.save(tmp_path, format="PNG")
                os.replace(tmp_path, out_path)
            except Exception:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
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

            try:
                raw_text = reader.pages[idx].extract_text() or ""
            except Exception:
                raw_text = ""
            cleaned_text = raw_text.strip()

            entry = run_workspace.record_artifact(
                relpath,
                kind="pdf_handout_page",
                source=source,
                metadata={
                    "source_filename": pdf_file.name,
                    "source_sha256": source_sha256,
                    "page_number": page_num,
                    "total_pages": total_pages,
                    "dpi": dpi,
                    "width_px": width_px,
                    "height_px": height_px,
                },
            )

            page_infos.append(
                PdfPageInfo(
                    page_number=page_num,
                    image_relpath=entry.relpath,
                    image_sha256=entry.sha256,
                    width_px=width_px,
                    height_px=height_px,
                    text=cleaned_text,
                )
            )
            full_text_parts.append(f"--- Page {page_num} ---\n{cleaned_text}")

        full_text = "\n\n".join(full_text_parts)
        text_out_path = run_workspace.resolve_path(text_relpath)
        _atomic_write_json(
            text_out_path,
            {
                "run_id": run_workspace.run_id,
                "source_filename": pdf_file.name,
                "source_sha256": source_sha256,
                "total_pages": total_pages,
                "rendered_pages": len(page_infos),
                "truncated": truncated,
                "dpi": dpi,
                "pages": [
                    {
                        "page_number": p.page_number,
                        "image_relpath": p.image_relpath,
                        "image_sha256": p.image_sha256,
                        "width_px": p.width_px,
                        "height_px": p.height_px,
                        "text": p.text,
                    }
                    for p in page_infos
                ],
            },
        )
        text_entry = run_workspace.record_artifact(
            text_relpath,
            kind="pdf_handout_text",
            source=source,
            metadata={
                "source_filename": pdf_file.name,
                "source_sha256": source_sha256,
                "rendered_pages": len(page_infos),
                "total_pages": total_pages,
            },
        )

        return PdfInspectionResult(
            run_id=run_workspace.run_id,
            source_filename=pdf_file.name,
            source_sha256=source_sha256,
            total_pages=total_pages,
            rendered_pages=len(page_infos),
            truncated=truncated,
            dpi=dpi,
            pages=page_infos,
            full_text=full_text,
            extracted_text_relpath=text_entry.relpath,
            extracted_text_sha256=text_entry.sha256,
        )
    finally:
        try:
            pdf_doc.close()
        except Exception:
            pass


__all__ = [
    "PdfInspectionResult",
    "PdfPageInfo",
    "inspect_pdf",
]
