"""Pluggable OCR subsystem (RapidOCR ONNX primary + timeout-guarded macOS Vision)."""

from __future__ import annotations

from pea.config import PeaConfig
from pea.ocr.base import (
    ImageInput,
    OcrBackend,
    join_ocr_text,
    load_pil_image,
    normalize_homoglyphs,
    parse_numeric_and_unit,
    prepare_crop,
    read_spec_with_backend,
    sort_boxes_reading_order,
)
from pea.ocr.macos_vision import MacOsVisionOcrBackend, compile_macos_vision_helper
from pea.ocr.rapid_ocr import RapidOcrBackend


def create_ocr_backend(config: PeaConfig | None = None) -> OcrBackend:
    """Instantiate the configured ``OcrBackend`` (default: offline ``RapidOcrBackend``)."""
    cfg = config or PeaConfig.from_env()
    if cfg.ocr_backend == "macos_vision":
        return MacOsVisionOcrBackend(cfg, fallback=RapidOcrBackend(cfg))
    return RapidOcrBackend(cfg)


__all__ = [
    "ImageInput",
    "MacOsVisionOcrBackend",
    "OcrBackend",
    "RapidOcrBackend",
    "compile_macos_vision_helper",
    "create_ocr_backend",
    "join_ocr_text",
    "load_pil_image",
    "normalize_homoglyphs",
    "parse_numeric_and_unit",
    "prepare_crop",
    "read_spec_with_backend",
    "sort_boxes_reading_order",
]
