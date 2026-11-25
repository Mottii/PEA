"""Unit tests for SWF binary header inspection (FWS / CWS / ZWS) and error handling."""

from __future__ import annotations

import hashlib
import lzma
import struct
import zlib
from pathlib import Path

import pytest

from pea.sim.swf_meta import SwfFormatError, inspect_swf

REPO_ROOT = Path(__file__).resolve().parents[1]
SIMS_DIR = REPO_ROOT / "local_assets" / "sims"


def _encode_rect_twips(xmin: int, xmax: int, ymin: int, ymax: int) -> bytes:
    """Pack a valid SWF signed TWIPS RECT structure MSB-first."""
    vals = (xmin, xmax, ymin, ymax)
    # Determine minimum signed bit width accommodating all 4 values.
    nbits = 1
    for v in vals:
        while not (-(1 << (nbits - 1)) <= v <= (1 << (nbits - 1)) - 1):
            nbits += 1

    bits = f"{nbits:05b}"
    mask = (1 << nbits) - 1
    for v in vals:
        bits += f"{v & mask:0{nbits}b}"

    pad = (-len(bits)) % 8
    bits += "0" * pad
    return bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits), 8))


def _build_swf_bytes(
    signature: bytes,
    *,
    version: int = 9,
    stage_width: int = 800,
    stage_height: int = 600,
    fps: float = 24.0,
    frame_count: int = 2,
    xmin_px: int = 0,
    ymin_px: int = 0,
) -> bytes:
    rect = _encode_rect_twips(
        xmin_px * 20,
        (xmin_px + stage_width) * 20,
        ymin_px * 20,
        (ymin_px + stage_height) * 20,
    )
    raw_fps = int(round(fps * 256))
    body = rect + struct.pack("<HH", raw_fps, frame_count) + b"\x00\x00"
    file_length = 8 + len(body)
    prefix = signature + bytes([version]) + struct.pack("<I", file_length)

    if signature == b"FWS":
        return prefix + body
    if signature == b"CWS":
        return prefix + zlib.compress(body)
    if signature == b"ZWS":
        alone = lzma.compress(body, format=lzma.FORMAT_ALONE)
        # FORMAT_ALONE header: 5B props + 8B uncompressed size + raw stream.
        props = alone[:5]
        raw_stream = alone[13:]
        return prefix + struct.pack("<I", len(raw_stream)) + props + raw_stream
    raise ValueError(f"Unsupported test signature: {signature!r}")


@pytest.mark.parametrize(
    ("filename", "expected_w", "expected_h", "expected_fps", "expected_sha"),
    [
        ("m96210.swf", 500, 450, 24.0, None),
        ("m96211.swf", 700, 500, 24.0, None),
        (
            "m96220.swf",
            800,
            600,
            24.0,
            "35e4a257134073356c9796965ea3ea2d9362d51b2dc87486dc93e85a0fb96b15",
        ),
    ],
)
def test_inspect_real_staged_swfs(
    filename: str,
    expected_w: int,
    expected_h: int,
    expected_fps: float,
    expected_sha: str | None,
) -> None:
    swf_path = SIMS_DIR / filename
    if not swf_path.is_file():
        pytest.skip(f"Optional staged SWF not present: {swf_path.name}")

    meta = inspect_swf(swf_path)
    assert meta.signature == "CWS"
    assert meta.compressed is True
    assert meta.swf_version == 9
    assert meta.stage_width == expected_w
    assert meta.stage_height == expected_h
    assert meta.fps == pytest.approx(expected_fps)
    assert meta.frame_count >= 1

    actual_sha = hashlib.sha256(swf_path.read_bytes()).hexdigest()
    assert meta.sha256 == actual_sha
    if expected_sha is not None:
        assert meta.sha256 == expected_sha


@pytest.mark.parametrize("sig", [b"FWS", b"CWS", b"ZWS"])
def test_inspect_synthetic_fws_cws_zws_and_signed_offsets(tmp_path: Path, sig: bytes) -> None:
    raw = _build_swf_bytes(
        sig,
        version=15,
        stage_width=640,
        stage_height=480,
        fps=30.5,
        frame_count=12,
        xmin_px=-10,
        ymin_px=-5,
    )
    swf_file = tmp_path / f"sample_{sig.decode().lower()}.swf"
    swf_file.write_bytes(raw)

    meta = inspect_swf(swf_file)
    assert meta.signature == sig.decode()
    assert meta.compressed is (sig != b"FWS")
    assert meta.swf_version == 15
    assert meta.stage_width == 640
    assert meta.stage_height == 480
    assert meta.fps == pytest.approx(30.5)
    assert meta.frame_count == 12
    assert meta.sha256 == hashlib.sha256(raw).hexdigest()


def test_inspect_swf_rejects_missing_and_corrupted_headers(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        inspect_swf(tmp_path / "missing.swf")

    bad_cases: list[tuple[str, bytes]] = [
        ("too_short.swf", b"CWS\x09"),
        ("bad_sig.swf", b"XYZ\x09" + struct.pack("<I", 100) + b"\x00" * 32),
        ("version_zero.swf", b"FWS\x00" + struct.pack("<I", 100) + b"\x00" * 32),
        ("corrupt_zlib.swf", b"CWS\x09" + struct.pack("<I", 100) + b"\xff\xfe\xfd\xfc"),
        ("corrupt_lzma.swf", b"ZWS\x0f" + struct.pack("<II", 100, 10) + b"\x5d\x00\x00\x00\x00bad"),
        (
            "zero_stage.swf",
            b"FWS\x09"
            + struct.pack("<I", 20)
            + _encode_rect_twips(0, 0, 0, 0)
            + struct.pack("<HH", 24 * 256, 1),
        ),
        (
            "zero_fps.swf",
            b"FWS\x09"
            + struct.pack("<I", 30)
            + _encode_rect_twips(0, 800 * 20, 0, 600 * 20)
            + struct.pack("<HH", 0, 1),
        ),
        ("truncated_body.swf", b"FWS\x09" + struct.pack("<I", 20) + b"\x78"),
    ]

    for name, payload in bad_cases:
        p = tmp_path / name
        p.write_bytes(payload)
        with pytest.raises(SwfFormatError):
            inspect_swf(p)
