"""SWF binary header parser (FWS / CWS / ZWS) extracting exact native stage dimensions & SHA-256."""

from __future__ import annotations

import lzma
import struct
import zlib
from pathlib import Path
from typing import Literal

from pea.schemas import SwfMeta
from pea.workspace import sha256_file

_TWIPS_PER_PIXEL = 20
_HEADER_PREFIX_BYTES = 8
_MAX_DECOMPRESSED_HEADER_BYTES = 128


class SwfFormatError(ValueError):
    """Raised when a file is not a valid FWS, CWS, or ZWS Shockwave Flash binary."""


class _BitReader:
    """MSB-first bit reader for SWF packed structures (e.g. ``RECT``)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._bit_pos = 0
        self._total_bits = len(data) * 8

    @property
    def bit_pos(self) -> int:
        return self._bit_pos

    def read_ub(self, nbits: int) -> int:
        if nbits < 0:
            raise SwfFormatError(f"Negative bit count: {nbits}")
        if nbits == 0:
            return 0
        if self._bit_pos + nbits > self._total_bits:
            raise SwfFormatError("Truncated SWF header while reading packed RECT bits")

        value = 0
        for _ in range(nbits):
            byte_idx = self._bit_pos >> 3
            shift = 7 - (self._bit_pos & 7)
            bit = (self._data[byte_idx] >> shift) & 1
            value = (value << 1) | bit
            self._bit_pos += 1
        return value

    def read_sb(self, nbits: int) -> int:
        if nbits == 0:
            return 0
        unsigned = self.read_ub(nbits)
        sign_bit = 1 << (nbits - 1)
        if unsigned & sign_bit:
            return unsigned - (1 << nbits)
        return unsigned


def _decompress_header_body(
    signature: Literal["FWS", "CWS", "ZWS"],
    file_length: int,
    raw_tail: bytes,
) -> bytes:
    if signature == "FWS":
        return raw_tail[:_MAX_DECOMPRESSED_HEADER_BYTES]

    if signature == "CWS":
        try:
            decompressor = zlib.decompressobj()
            body = decompressor.decompress(raw_tail, _MAX_DECOMPRESSED_HEADER_BYTES)
        except zlib.error as exc:
            raise SwfFormatError(f"Corrupted CWS zlib stream: {exc}") from exc
        if not body:
            raise SwfFormatError("Empty decompressed body in CWS SWF header")
        return body

    # ZWS layout after byte 8: uint32 compressed_len (4B) + LZMA props (5B) + raw LZMA stream.
    if len(raw_tail) < 10:
        raise SwfFormatError("Truncated ZWS LZMA header")
    lzma_props = raw_tail[4:9]
    lzma_stream = raw_tail[9:]
    uncompressed_body_len = max(0, file_length - _HEADER_PREFIX_BYTES)
    alone_payload = lzma_props + struct.pack("<Q", uncompressed_body_len) + lzma_stream
    try:
        decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
        body = decompressor.decompress(alone_payload, max_length=_MAX_DECOMPRESSED_HEADER_BYTES)
    except lzma.LZMAError as exc:
        raise SwfFormatError(f"Corrupted ZWS LZMA stream: {exc}") from exc
    if not body:
        raise SwfFormatError("Empty decompressed body in ZWS SWF header")
    return body


def inspect_swf(path: str | Path) -> SwfMeta:
    """Parse an SWF file header and return validated ``SwfMeta`` with streaming SHA-256."""
    swf_path = Path(path).expanduser()
    if not swf_path.is_file():
        raise FileNotFoundError(f"SWF file not found: {swf_path}")

    with swf_path.open("rb") as handle:
        prefix = handle.read(_HEADER_PREFIX_BYTES)
        if len(prefix) < _HEADER_PREFIX_BYTES:
            raise SwfFormatError(
                f"File is too short to be an SWF ({len(prefix)} bytes): {swf_path}"
            )
        # Read enough compressed bytes to decode the header RECT + fps + frame_count.
        raw_tail = handle.read(65_536)

    sig_bytes = prefix[:3]
    if sig_bytes not in (b"FWS", b"CWS", b"ZWS"):
        raise SwfFormatError(f"Invalid SWF signature {sig_bytes!r}; expected FWS, CWS, or ZWS")
    signature: Literal["FWS", "CWS", "ZWS"] = sig_bytes.decode("ascii")  # type: ignore[assignment]

    swf_version = prefix[3]
    if swf_version < 1:
        raise SwfFormatError(f"Invalid SWF version {swf_version}; expected >= 1")

    file_length = struct.unpack_from("<I", prefix, 4)[0]
    if file_length < 12:
        raise SwfFormatError(f"Invalid SWF uncompressed file_length {file_length}")

    body = _decompress_header_body(signature, file_length, raw_tail)
    reader = _BitReader(body)
    nbits = reader.read_ub(5)
    xmin = reader.read_sb(nbits)
    xmax = reader.read_sb(nbits)
    ymin = reader.read_sb(nbits)
    ymax = reader.read_sb(nbits)

    rect_bytes = (reader.bit_pos + 7) // 8
    if len(body) < rect_bytes + 4:
        raise SwfFormatError("Truncated SWF header: missing frame rate or frame count")

    width_twips = xmax - xmin
    height_twips = ymax - ymin
    if width_twips <= 0 or height_twips <= 0:
        raise SwfFormatError(
            f"Non-positive SWF stage bounds in TWIPS: x=[{xmin}, {xmax}], y=[{ymin}, {ymax}]"
        )

    stage_width = round(width_twips / _TWIPS_PER_PIXEL)
    stage_height = round(height_twips / _TWIPS_PER_PIXEL)
    if stage_width <= 0 or stage_height <= 0:
        raise SwfFormatError(
            f"Non-positive SWF stage dimensions in px: ({stage_width}, {stage_height})"
        )

    raw_fps, frame_count = struct.unpack_from("<HH", body, rect_bytes)
    fps = raw_fps / 256.0
    if fps <= 0.0:
        raise SwfFormatError(f"Invalid SWF frame rate: {fps}")

    digest, _ = sha256_file(swf_path)
    return SwfMeta(
        path=str(swf_path),
        sha256=digest,
        swf_version=swf_version,
        compressed=(signature != "FWS"),
        signature=signature,
        stage_width=stage_width,
        stage_height=stage_height,
        fps=fps,
        frame_count=frame_count,
    )


__all__ = [
    "SwfFormatError",
    "inspect_swf",
]
