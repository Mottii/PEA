"""Unit tests for offline RapidOCR, homoglyph parsing, Vision timeout fallback, and ProfileStore."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from pea.config import PeaConfig
from pea.ocr import (
    MacOsVisionOcrBackend,
    OcrBackend,
    RapidOcrBackend,
    normalize_homoglyphs,
    parse_numeric_and_unit,
    prepare_crop,
)
from pea.schemas import CalibrationProfile, ControlSpec, OcrBox, OcrMode, OcrResult, ReadoutSpec
from pea.sim.profiles import ProfileStore

REPO_ROOT = Path(__file__).resolve().parents[1]
LAB4_CROPS_DIR = REPO_ROOT / "local_assets" / "benchmarks" / "lab4-crops"
NOTO_BOLD_FONT = REPO_ROOT / "local_assets" / "fonts" / "NotoSans-Bold.ttf"
SAMPLE_SHA256 = "35e4a257134073356c9796965ea3ea2d9362d51b2dc87486dc93e85a0fb96b15"


def _repo_config(tmp_path: Path) -> PeaConfig:
    return PeaConfig(
        project_root=REPO_ROOT,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
        ocr_threads=4,
    )


# ---------------------------------------------------------------------------
# 1. Pure-Python Homoglyph, Numeric+Unit Parsing & Crop Helpers
# ---------------------------------------------------------------------------


def test_normalize_homoglyphs_and_unit_parsing() -> None:
    assert normalize_homoglyphs("mkTl") == "мкТл"
    assert normalize_homoglyphs("mTl") == "мТл"
    assert normalize_homoglyphs("mГн") == "мГн"
    assert normalize_homoglyphs("mkФ") == "мкФ"
    assert normalize_homoglyphs("Om") == "Ом"
    assert normalize_homoglyphs("−241,30") == "-241,30"

    # Distinguishes microtesla vs millitesla under Latin homoglyph corruption
    val_u, unit_u = parse_numeric_and_unit(
        "B = -241,30 mkTl",
        parse_mode="float",
        unit_pattern=r"мкТл|мТл",
        normalize_homoglyphs=True,
    )
    assert val_u == pytest.approx(-241.30)
    assert unit_u == "мкТл"

    val_m, unit_m = parse_numeric_and_unit(
        "B = 1,25 mTl",
        parse_mode="float",
        unit_pattern=r"мкТл|мТл",
        normalize_homoglyphs=True,
    )
    assert val_m == pytest.approx(1.25)
    assert unit_m == "мТл"

    # Superscript '-1' in 'с-1' must not be parsed ahead of the primary integer value
    val_w, unit_w = parse_numeric_and_unit(
        "2500 c-1",
        parse_mode="int",
        unit_pattern=r"с⁻¹|с-1",
        normalize_homoglyphs=True,
    )
    assert val_w == 2500
    assert unit_w == "с-1"

    # Non-integer value rejected cleanly in int mode
    bad_int, _ = parse_numeric_and_unit("3,94", parse_mode="int")
    assert bad_int is None

    # Text mode & empty input
    txt_val, txt_unit = parse_numeric_and_unit("R = 3,2 Om", parse_mode="text", unit_pattern=r"Ом")
    assert txt_val == "R = 3,2 Ом"
    assert txt_unit == "Ом"
    assert parse_numeric_and_unit("   ", parse_mode="float") == (None, None)

    # English unit pattern still matches cleanly even with default normalize_homoglyphs=True
    eng_val, eng_unit = parse_numeric_and_unit("5.0 mH", parse_mode="float", unit_pattern=r"mH|uH")
    assert eng_val == pytest.approx(5.0)
    assert eng_unit == "mH"

    # Default unit extraction (unit_pattern=None) must never grab letters from labels/prefixes
    labeled_cases: list[tuple[str, float | None, str | None]] = [
        ("B = 50,0 мкТл", 50.0, "мкТл"),
        ("В = -60,32 мкТл", -60.32, "мкТл"),
        ("Магнитная индукция 50,0 мкТл", 50.0, "мкТл"),
        ("Сила тока I = 5,0 мА", 5.0, "мА"),
        ("Частота 2450 с⁻¹", 2450.0, "с⁻¹"),
        ("f, с-1", None, "с-1"),
        ("1,25e-3 мТл", 0.00125, "мТл"),
        ("1,25е-3 мТл", 0.00125, "мТл"),
        ("T = 250 мс", 250.0, "мс"),
        ("B = 50,0", 50.0, None),
        ("B X = -67 ,86 мкТл", -67.86, "мкТл"),
        ("B = 142 .42 мкТл", 142.42, "мкТл"),
        ("10\n.5", 10.0, None),
        ("10    ,5", 10.0, None),
        ("1, 2", 1.0, None),
    ]
    for raw_text, exp_val, exp_unit in labeled_cases:
        got_val, got_unit = parse_numeric_and_unit(raw_text, parse_mode="float")
        if exp_val is None:
            assert got_val is None, f"{raw_text!r}: expected None, got {got_val!r}"
        else:
            assert got_val == pytest.approx(exp_val), (
                f"{raw_text!r}: expected {exp_val}, got {got_val!r}"
            )
        assert got_unit == exp_unit, f"{raw_text!r}: expected unit {exp_unit!r}, got {got_unit!r}"


def test_prepare_crop_pad_scale_and_validation() -> None:
    img = Image.new("RGB", (200, 100), color=(20, 40, 60))
    cropped = prepare_crop(img, roi_px=(10, 20, 60, 40), pad_px=8, scale=2.0)
    # 50x20 -> scaled 2x = 100x40 -> +8px border on each side = 116x56
    assert cropped.size == (116, 56)
    # Border pixel must be pure white
    assert cropped.getpixel((0, 0)) == (255, 255, 255)
    assert cropped.getpixel((20, 20)) == (20, 40, 60)

    with pytest.raises(ValueError):
        prepare_crop(img, roi_px=(200, 10, 250, 50))
    with pytest.raises(ValueError):
        prepare_crop(img, pad_px=-1)
    with pytest.raises(ValueError):
        prepare_crop(img, scale=0.0)


# ---------------------------------------------------------------------------
# 2. Offline RapidOcrBackend (Lab 4 4/4 Crops + Stateful use_det Regression)
# ---------------------------------------------------------------------------


def test_rapid_ocr_fails_fast_when_local_models_missing(tmp_path: Path) -> None:
    empty_models = tmp_path / "empty_ocr"
    empty_models.mkdir()
    with pytest.raises(FileNotFoundError, match="fetch_assets.py"):
        RapidOcrBackend(model_dir=empty_models)


@pytest.fixture(scope="module")
def rapid_ocr() -> RapidOcrBackend:
    return RapidOcrBackend(model_dir=REPO_ROOT / "local_assets" / "ocr", intra_op_num_threads=4)


@pytest.mark.skipif(
    not LAB4_CROPS_DIR.is_dir(),
    reason="Optional Lab 4 benchmark crops not staged",
)
def test_rapid_ocr_lab4_crops_exact_value_sign_unit(rapid_ocr: RapidOcrBackend) -> None:
    assert isinstance(rapid_ocr, OcrBackend)
    assert rapid_ocr.backend_name == "rapidocr"

    cases = [
        (
            "wire-I-5-r2.png",
            ReadoutSpec(
                name="wire_B",
                roi_px=(800, 260, 1380, 410),
                ocr_mode="det_rec",
                pad_px=80,
                parse_mode="float",
                unit_pattern=r"мкТл|мТл",
            ),
            50.0,
            "мкТл",
        ),
        (
            "wire-I-20-r2.png",
            ReadoutSpec(
                name="wire_B",
                roi_px=(800, 260, 1380, 410),
                ocr_mode="det_rec",
                pad_px=80,
                parse_mode="float",
                unit_pattern=r"мкТл|мТл",
            ),
            200.0,
            "мкТл",
        ),
        (
            "loop-I-5-r2.png",
            ReadoutSpec(
                name="loop_Bx",
                roi_px=(2400, 40, 3080, 160),
                ocr_mode="det_rec",
                pad_px=80,
                parse_mode="float",
                unit_pattern=r"мкТл|мТл",
            ),
            -60.32,
            "мкТл",
        ),
        (
            "loop-I-20-r2.png",
            ReadoutSpec(
                name="loop_Bx",
                roi_px=(2400, 40, 3080, 160),
                ocr_mode="det_rec",
                pad_px=80,
                parse_mode="float",
                unit_pattern=r"мкТл|мТл",
            ),
            -241.30,
            "мкТл",
        ),
    ]

    for filename, spec, expected_val, expected_unit in cases:
        crop_path = LAB4_CROPS_DIR / filename
        assert crop_path.is_file(), f"Missing staged Lab 4 crop: {crop_path}"
        result, val, unit = rapid_ocr.read_spec(crop_path, spec)
        assert result.backend == "rapidocr"
        assert result.mode == "det_rec"
        assert len(result.boxes) >= 1
        assert val == pytest.approx(expected_val), f"{filename}: got {val} from {result.boxes}"
        assert unit == expected_unit, f"{filename}: got unit {unit!r}"


def _render_text_card(
    lines: list[tuple[tuple[int, int], str]],
    *,
    size: tuple[int, int] = (520, 140),
    font_size: int = 38,
) -> Image.Image:
    """Render high-contrast dark text on white background using bundled Noto Sans Bold."""
    assert NOTO_BOLD_FONT.is_file(), f"Missing bundled font: {NOTO_BOLD_FONT}"
    font = ImageFont.truetype(str(NOTO_BOLD_FONT), size=font_size)
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    for xy, text in lines:
        draw.text(xy, text, fill=(15, 23, 42), font=font)
    return img


def test_rapid_ocr_synthetic_rendered_readouts(
    rapid_ocr: RapidOcrBackend,
    tmp_path: Path,
) -> None:
    """Verify real ONNX RapidOCR extracts positive/negative values and EN/RU units offline."""
    assert isinstance(rapid_ocr, OcrBackend)
    assert rapid_ocr.backend_name == "rapidocr"

    voltage_img = _render_text_card([((40, 42), "10.00 V")], size=(360, 130), font_size=40)
    voltage_path = tmp_path / "voltage.png"
    voltage_img.save(voltage_path)

    v_spec = ReadoutSpec(
        name="voltage",
        roi_px=(10, 15, 340, 115),
        ocr_mode="det_rec",
        pad_px=24,
        parse_mode="float",
        unit_pattern=r"mV|V",
    )
    v_res, v_val, v_unit = rapid_ocr.read_spec(voltage_path, v_spec)
    assert v_res.backend == "rapidocr"
    assert v_res.mode == "det_rec"
    assert len(v_res.boxes) >= 1
    assert v_val == pytest.approx(10.0)
    assert v_unit == "V"

    mag_img = _render_text_card([((30, 42), "-60,32 мкТл")], size=(440, 130), font_size=40)
    mag_path = tmp_path / "magnetic.png"
    mag_img.save(mag_path)

    b_spec = ReadoutSpec(
        name="magnetic_field",
        roi_px=(10, 15, 420, 115),
        ocr_mode="det_rec",
        pad_px=24,
        parse_mode="float",
        unit_pattern=r"мкТл|мТл",
    )
    b_res, b_val, b_unit = rapid_ocr.read_spec(mag_path, b_spec)
    assert b_res.backend == "rapidocr"
    assert b_res.mode == "det_rec"
    assert len(b_res.boxes) >= 1
    assert b_val == pytest.approx(-60.32)
    assert b_unit == "мкТл"


def test_rapid_ocr_interleaved_use_det_stateful_bug_regression(
    rapid_ocr: RapidOcrBackend,
    tmp_path: Path,
) -> None:
    """Calling rec_only (use_det=False) must NEVER corrupt subsequent det_rec (use_det=True)."""
    panel_a = _render_text_card(
        [
            ((30, 24), "50,0 мкТл"),
            ((30, 94), "10.00 V"),
            ((30, 164), "2450 c-1"),
            ((30, 234), "3,20 Ом"),
            ((320, 24), "1,25 мТл"),
            ((320, 94), "5,0 мГн"),
            ((320, 164), "32,0 мкФ"),
            ((320, 234), "0.50 s"),
        ],
        size=(640, 310),
        font_size=34,
    )
    panel_a_path = tmp_path / "panel_a.png"
    panel_a.save(panel_a_path)

    panel_b = _render_text_card(
        [
            ((30, 24), "-241,30 мкТл"),
            ((30, 94), "-60,32 мкТл"),
            ((30, 164), "200,0 мкТл"),
            ((30, 234), "1.995 Hz"),
            ((340, 24), "R = 5,0 Ом"),
            ((340, 94), "I = 20 А"),
            ((340, 164), "U = 10,0 В"),
            ((340, 234), "tau = 1,00 с"),
        ],
        size=(680, 310),
        font_size=34,
    )
    panel_b_path = tmp_path / "panel_b.png"
    panel_b.save(panel_b_path)

    tight_num_crop = prepare_crop(panel_a_path, roi_px=(20, 22, 130, 74), pad_px=8)

    # 1. Start with rec_only (use_det=False) to exercise rapidocr==3.10.0 stateful mutation guard
    rec1 = rapid_ocr.recognize(tight_num_crop, mode="rec_only")
    assert rec1.mode == "rec_only"
    assert any("50" in b.text for b in rec1.boxes), f"Unexpected rec_only output: {rec1.boxes}"

    # 2. Immediately call det_rec (use_det=True) on full multi-box panel
    det1 = rapid_ocr.recognize(panel_a_path, mode="det_rec")
    assert det1.mode == "det_rec"
    assert len(det1.boxes) >= 6, f"det_rec degraded after rec_only: only {len(det1.boxes)} boxes"
    assert any("50,0" in b.text for b in det1.boxes)
    assert any("мкТл" in b.text for b in det1.boxes)

    # 3. Interleave back to rec_only and again to det_rec
    rec2 = rapid_ocr.recognize(tight_num_crop, mode="rec_only")
    assert rec2.mode == "rec_only"
    assert any("50" in b.text for b in rec2.boxes)

    det2 = rapid_ocr.recognize(panel_b_path, mode="det_rec")
    assert det2.mode == "det_rec"
    assert len(det2.boxes) >= 6
    assert any("-241,30" in b.text for b in det2.boxes)

    # 4. Blank white image handled cleanly in both modes
    blank = Image.new("RGB", (200, 60), color=(255, 255, 255))
    blank_det = rapid_ocr.recognize(blank, mode="det_rec")
    blank_rec = rapid_ocr.recognize(blank, mode="rec_only")
    assert blank_det.boxes == []
    assert blank_rec.boxes == []


# ---------------------------------------------------------------------------
# 3. MacOsVisionOcrBackend Timeout & Crash Fallback
# ---------------------------------------------------------------------------


class _StubFallbackBackend:
    @property
    def backend_name(self) -> str:
        return "rapidocr"

    def __init__(self) -> None:
        self.calls: list[OcrMode] = []

    def recognize(self, image: object, *, mode: OcrMode = "det_rec") -> OcrResult:
        self.calls.append(mode)
        return OcrResult(
            backend="rapidocr",
            mode=mode,
            elapsed_ms=1.5,
            boxes=[
                OcrBox(
                    text="fallback_ok",
                    confidence=0.99,
                    bbox_px=(0, 0, 50, 20),
                    bbox_norm=(0.0, 0.0, 1.0, 1.0),
                )
            ],
        )

    def read_spec(self, image: object, spec: ReadoutSpec) -> tuple[OcrResult, None, None]:
        return (self.recognize(image, mode=spec.ocr_mode), None, None)


def test_macos_vision_timeout_kills_child_and_falls_back(tmp_path: Path) -> None:
    # Write a fake helper script that hangs forever (mimicking macOS 27.2 Vision .accurate hang)
    hanging_helper = tmp_path / "hang_helper.py"
    hanging_helper.write_text(
        "import sys, time\n"
        "for _ in sys.stdin:\n"
        "    time.sleep(60)\n",
        encoding="utf-8",
    )

    fallback = _StubFallbackBackend()
    cfg = _repo_config(tmp_path)
    img = Image.new("RGB", (100, 40), color=(255, 255, 255))

    with MacOsVisionOcrBackend(
        cfg,
        fallback=fallback,  # type: ignore[arg-type]
        helper_command=[sys.executable, "-u", str(hanging_helper)],
        timeout_s=0.25,
    ) as backend:
        result = backend.recognize(img, mode="det_rec")
        assert result.backend == "rapidocr"
        assert result.boxes[0].text == "fallback_ok"
        assert backend.fallback_count == 1
        assert backend.last_fallback_reason is not None
        assert "timed out" in backend.last_fallback_reason.lower()
        assert backend._proc is None
        assert fallback.calls == ["det_rec"]


def test_macos_vision_success_and_error_fallback(tmp_path: Path) -> None:
    good_helper = tmp_path / "good_helper.py"
    good_helper.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    if req.get('cmd') == 'quit': break\n"
        "    print(json.dumps({\n"
        "        'ok': True, 'elapsed_ms': 4.2,\n"
        "        'boxes': [{'text': '3,94', 'confidence': 0.98,\n"
        "                   'bbox_px': [10, 5, 60, 25], 'bbox_norm': [0.1, 0.125, 0.6, 0.625]}],\n"
        "        'error': None,\n"
        "    }), flush=True)\n",
        encoding="utf-8",
    )
    crash_helper = tmp_path / "crash_helper.py"
    crash_helper.write_text("import sys; sys.exit(2)\n", encoding="utf-8")

    fallback = _StubFallbackBackend()
    cfg = _repo_config(tmp_path)
    img = Image.new("RGB", (100, 40), color=(255, 255, 255))

    with MacOsVisionOcrBackend(
        cfg,
        fallback=fallback,  # type: ignore[arg-type]
        helper_command=[sys.executable, "-u", str(good_helper)],
        timeout_s=2.0,
    ) as backend:
        res = backend.recognize(img, mode="det_rec")
        assert res.backend == "macos_vision"
        assert len(res.boxes) == 1
        assert res.boxes[0].text == "3,94"
        assert backend.fallback_count == 0

    with MacOsVisionOcrBackend(
        cfg,
        fallback=fallback,  # type: ignore[arg-type]
        helper_command=[sys.executable, "-u", str(crash_helper)],
        timeout_s=2.0,
    ) as backend:
        res = backend.recognize(img, mode="rec_only")
        assert res.backend == "rapidocr"
        assert backend.fallback_count == 1
        assert fallback.calls == ["rec_only"]

    # Verbose stderr (>100 KB) must not deadlock the helper pipe
    chatty_helper = tmp_path / "chatty_helper.py"
    chatty_helper.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    if req.get('cmd') == 'quit': break\n"
        "    sys.stderr.write('W' * 120000 + '\\n')\n"
        "    sys.stderr.flush()\n"
        "    print(json.dumps({\n"
        "        'ok': True, 'elapsed_ms': 2.0,\n"
        "        'boxes': [{'text': '42,5', 'confidence': 0.99,\n"
        "                   'bbox_px': [0, 0, 40, 20], 'bbox_norm': [0.0, 0.0, 1.0, 1.0]}],\n"
        "        'error': None,\n"
        "    }), flush=True)\n",
        encoding="utf-8",
    )
    with MacOsVisionOcrBackend(
        cfg,
        fallback=fallback,  # type: ignore[arg-type]
        helper_command=[sys.executable, "-u", str(chatty_helper)],
        timeout_s=2.0,
    ) as backend:
        res = backend.recognize(img, mode="det_rec")
        assert res.backend == "macos_vision"
        assert res.boxes[0].text == "42,5"
        assert backend.fallback_count == 0


def test_macos_vision_swift_source_exists() -> None:
    swift_src = REPO_ROOT / "native" / "macos_vision_ocr.swift"
    assert swift_src.is_file()
    text = swift_src.read_text(encoding="utf-8")
    assert "usesCPUOnly = true" in text
    assert "VNRecognizeTextRequest" in text


# ---------------------------------------------------------------------------
# 4. ProfileStore Atomic Save, Local/Shared Fallback & Geometry Validation
# ---------------------------------------------------------------------------


def _sample_profile(sha256: str = SAMPLE_SHA256, *, notes: str = "local") -> CalibrationProfile:
    return CalibrationProfile(
        swf_sha256=sha256,
        stage_width=800,
        stage_height=600,
        header_height_px=39,
        controls={
            "R": ControlSpec(
                name="R",
                kind="stepper",
                input_xy=(644, 289),
                up_xy=(678, 282),
                down_xy=(678, 296),
                readout_roi_px=(620, 275, 667, 302),
                unit="Ом",
                step=0.1,
                min_val=3.0,
                max_val=10.0,
                decimal_comma=True,
                commit_mode="enter",
            )
        },
        readouts={
            "uc_over_u": ReadoutSpec(
                name="uc_over_u",
                roi_px=(625, 474, 710, 502),
                ocr_mode="rec_only",
                parse_mode="float",
            )
        },
        blur_xy=(450, 18),
        verified_at=datetime.now(UTC),
        notes=notes,
    )


def test_profile_store_atomic_save_override_fallback_and_validation(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")

    # Missing profile returns None
    assert store.load(SAMPLE_SHA256) is None

    # Save shared read-only baseline
    shared_prof = _sample_profile(notes="shared-baseline")
    shared_path = store.save(shared_prof, shared=True)
    assert shared_path == store.shared_path(SAMPLE_SHA256)
    assert not shared_path.with_suffix(".json.tmp").exists()

    loaded_shared = store.load(SAMPLE_SHA256, expected_stage=(800, 600))
    assert loaded_shared is not None
    assert loaded_shared.notes == "shared-baseline"

    # Save local override -> takes precedence over shared
    local_prof = _sample_profile(notes="local-override")
    local_path = store.save(local_prof)
    assert local_path == store.local_path(SAMPLE_SHA256)
    loaded_local = store.load(SAMPLE_SHA256, expected_stage=(800, 600))
    assert loaded_local is not None
    assert loaded_local.notes == "local-override"
    assert len(store.list_profiles()) == 1
    assert store.list_profiles()[0].notes == "local-override"

    # Deleting local override falls back cleanly to untouched shared baseline
    assert store.delete_local(SAMPLE_SHA256) is True
    assert store.delete_local(SAMPLE_SHA256) is False
    loaded_after_delete = store.load(SAMPLE_SHA256)
    assert loaded_after_delete is not None
    assert loaded_after_delete.notes == "shared-baseline"

    # Stage dimension mismatch must raise ValueError
    with pytest.raises(ValueError, match="does not match expected stage"):
        store.load(SAMPLE_SHA256, expected_stage=(700, 500))

    # Invalid / traversal SHA-256 key rejected
    with pytest.raises(ValueError):
        store.load("../escape")

    # Out-of-bounds ROI rejected on save
    bad_roi_prof = _sample_profile()
    bad_roi_prof.readouts["oob"] = ReadoutSpec(name="oob", roi_px=(750, 620, 850, 650))
    with pytest.raises(ValueError, match="outside stage"):
        store.save(bad_roi_prof)

    # Mismatched internal swf_sha256 vs filename rejected on load
    other_sha = "a" * 64
    tampered_path = store.local_path(other_sha)
    tampered_path.parent.mkdir(parents=True, exist_ok=True)
    tampered_path.write_text(shared_prof.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="declares swf_sha256"):
        store.load(other_sha)
