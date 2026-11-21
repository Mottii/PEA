"""Tests for offline PDF handout intake (pypdfium2 PNG rendering + pypdf text extraction)."""

from __future__ import annotations

import json
from pathlib import Path

import pypdf
import pytest
from PIL import Image

from pea.config import PathConfinementError, PeaConfig
from pea.pdf_intake import inspect_pdf
from pea.workspace import WorkspaceManager

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAB5_HANDOUT = PROJECT_ROOT / "local_assets" / "benchmarks" / "lab5-rlc-handout.pdf"


def _make_workspace(tmp_path: Path):
    wm = WorkspaceManager(tmp_path / "runs")
    return wm.create_run(label="pdf-intake-test")


@pytest.mark.skipif(not LAB5_HANDOUT.is_file(), reason="Lab 5 benchmark handout PDF not staged")
def test_inspect_pdf_lab5_handout_all_pages(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    result = inspect_pdf(LAB5_HANDOUT, ws, dpi=150, max_pages=30)

    assert result.run_id == ws.run_id
    assert result.source_filename == "lab5-rlc-handout.pdf"
    assert result.total_pages == 8
    assert result.rendered_pages == 8
    assert result.truncated is False
    assert result.dpi == 150
    assert len(result.pages) == 8

    for idx, page in enumerate(result.pages, start=1):
        expected_rel = f"pdf_pages/page-{idx:03d}.png"
        assert page.page_number == idx
        assert page.image_relpath == expected_rel
        assert page.width_px > 500
        assert page.height_px > 500
        img_path = ws.resolve_path(page.image_relpath)
        assert img_path.is_file()
        with Image.open(img_path) as im:
            assert im.size == (page.width_px, page.height_px)
            im.verify()

    # Handout text extraction covers RLC title, Table 1, and Table 2
    assert "RLC" in result.full_text
    assert "Table 1" in result.full_text
    assert "Table 2" in result.full_text

    # Extracted text JSON exists and contains no literal host home paths
    text_json_path = ws.resolve_path(result.extracted_text_relpath)
    assert text_json_path.is_file()
    raw_json = text_json_path.read_text(encoding="utf-8")
    assert str(Path.home()) not in raw_json
    parsed = json.loads(raw_json)
    assert parsed["total_pages"] == 8
    assert len(parsed["pages"]) == 8

    # Provenance verification passes for all 8 PNGs + extracted-text.json
    entries = ws.read_provenance()
    kinds = [e.kind for e in entries]
    assert kinds.count("pdf_handout_page") == 8
    assert kinds.count("pdf_handout_text") == 1
    assert ws.verify_provenance().ok is True


@pytest.mark.skipif(not LAB5_HANDOUT.is_file(), reason="Lab 5 benchmark handout PDF not staged")
def test_inspect_pdf_max_pages_and_page_selection(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)

    capped = inspect_pdf(LAB5_HANDOUT, ws, dpi=100, max_pages=2)
    assert capped.total_pages == 8
    assert capped.rendered_pages == 2
    assert capped.truncated is True
    assert [p.page_number for p in capped.pages] == [1, 2]

    # Secondary inspection into a distinct subdir preserves earlier provenance
    selected = inspect_pdf(
        LAB5_HANDOUT,
        ws,
        dpi=100,
        pages=[2, 8, 2],  # duplicates deduplicated preserving order
        subdir="selected",
    )
    assert selected.rendered_pages == 2
    assert selected.truncated is False
    assert [p.page_number for p in selected.pages] == [2, 8]
    assert selected.pages[0].image_relpath == "pdf_pages/selected/page-002.png"
    assert selected.pages[1].image_relpath == "pdf_pages/selected/page-008.png"
    assert "Table 1" in selected.pages[0].text
    assert ws.verify_provenance().ok is True


@pytest.mark.skipif(not LAB5_HANDOUT.is_file(), reason="Lab 5 benchmark handout PDF not staged")
def test_inspect_pdf_protects_provenance_immutability(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    inspect_pdf(LAB5_HANDOUT, ws, dpi=96, max_pages=1)

    with pytest.raises(FileExistsError):
        inspect_pdf(LAB5_HANDOUT, ws, dpi=96, max_pages=1, overwrite=False)

    # Even overwrite=True refuses to clobber provenance-recorded artifacts
    with pytest.raises(FileExistsError):
        inspect_pdf(LAB5_HANDOUT, ws, dpi=96, max_pages=1, overwrite=True)

    assert ws.verify_provenance().ok is True


def test_inspect_pdf_edge_cases_and_validation(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)

    with pytest.raises(FileNotFoundError):
        inspect_pdf(tmp_path / "does-not-exist.pdf", ws)

    with pytest.raises(ValueError, match="regular file"):
        inspect_pdf(tmp_path, ws)

    bad_pdf = tmp_path / "corrupt.pdf"
    bad_pdf.write_bytes(b"not a real pdf document")
    with pytest.raises(ValueError, match="Failed to open PDF"):
        inspect_pdf(bad_pdf, ws)
    assert list(ws.pdf_pages_dir.glob("*.tmp")) == []

    # Parameter validation
    cfg = PeaConfig.from_env()
    target = LAB5_HANDOUT if LAB5_HANDOUT.is_file() else bad_pdf
    with pytest.raises(ValueError, match="dpi"):
        inspect_pdf(target, ws, dpi=30)
    with pytest.raises(ValueError, match="dpi"):
        inspect_pdf(target, ws, dpi=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_pages"):
        inspect_pdf(target, ws, max_pages=0)

    if LAB5_HANDOUT.is_file():
        with pytest.raises(ValueError, match="out of range"):
            inspect_pdf(LAB5_HANDOUT, ws, pages=[99])
        with pytest.raises(ValueError, match="empty"):
            inspect_pdf(LAB5_HANDOUT, ws, pages=[])
        with pytest.raises(PathConfinementError):
            inspect_pdf(LAB5_HANDOUT, ws, max_pages=1, subdir="../escape")
    assert cfg.local_assets_dir.is_dir()


def test_inspect_pdf_encrypted_zero_page_and_rotated_pdfs(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)

    # 1. Password-protected PDF rejected cleanly
    enc_writer = pypdf.PdfWriter()
    enc_writer.add_blank_page(width=200, height=300)
    enc_writer.encrypt(user_password="secret", owner_password="owner")
    enc_pdf = tmp_path / "encrypted.pdf"
    with enc_pdf.open("wb") as fh:
        enc_writer.write(fh)
    with pytest.raises(ValueError, match="Password-protected PDF"):
        inspect_pdf(enc_pdf, ws, subdir="enc")

    # 2. Owner-only encrypted PDF (empty user password) succeeds
    owner_writer = pypdf.PdfWriter()
    owner_writer.add_blank_page(width=200, height=300)
    owner_writer.encrypt(user_password="", owner_password="owner")
    owner_pdf = tmp_path / "owner-only.pdf"
    with owner_pdf.open("wb") as fh:
        owner_writer.write(fh)
    owner_res = inspect_pdf(owner_pdf, ws, dpi=72, subdir="owner")
    assert owner_res.rendered_pages == 1
    assert owner_res.pages[0].width_px == 200
    assert owner_res.pages[0].height_px == 300

    # 3. Zero-page PDF raises explicit zero-pages ValueError
    zero_writer = pypdf.PdfWriter()
    zero_pdf = tmp_path / "zero.pdf"
    with zero_pdf.open("wb") as fh:
        zero_writer.write(fh)
    with pytest.raises(ValueError, match="zero pages"):
        inspect_pdf(zero_pdf, ws, subdir="zero")

    # 4. Rotated 90-degree page (200x400 -> 400x200 at 72 DPI)
    rot_writer = pypdf.PdfWriter()
    page = rot_writer.add_blank_page(width=200, height=400)
    page.rotate(90)
    rot_pdf = tmp_path / "rotated.pdf"
    with rot_pdf.open("wb") as fh:
        rot_writer.write(fh)
    rot_res = inspect_pdf(rot_pdf, ws, dpi=72, subdir="rot")
    assert rot_res.pages[0].width_px == 400
    assert rot_res.pages[0].height_px == 200
    assert ws.verify_provenance().ok is True
