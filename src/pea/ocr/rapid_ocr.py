"""100% offline RapidOCR ONNX Runtime backend (M1 primary OCR engine)."""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

from pea.config import PeaConfig
from pea.ocr.base import (
    ImageInput,
    load_pil_image,
    read_spec_with_backend,
    sort_boxes_reading_order,
)
from pea.schemas import OcrBackendKind, OcrBox, OcrMode, OcrResult, ReadoutSpec

DET_MODEL_FILENAME = "ch_PP-OCRv5_det_mobile.onnx"
REC_MODEL_FILENAME = "cyrillic_PP-OCRv5_rec_mobile.onnx"


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _redirect_rapidocr_logger_to_stderr() -> None:
    """Keep RapidOCR logging off stdout so MCP stdio framing is never corrupted."""
    logger = logging.getLogger("RapidOCR")
    logger.setLevel(logging.ERROR)
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            handler.stream = sys.stderr


class RapidOcrBackend:
    """Offline ONNX Runtime OCR using pinned PP-OCRv5 Mobile Det + Cyrillic PP-OCRv5 Mobile Rec.

    Guards against the ``rapidocr==3.10.0`` stateful ``use_det`` mutation bug by passing
    ``use_det=True`` or ``use_det=False`` explicitly on every single call under a lock.
    """

    def __init__(
        self,
        config: PeaConfig | None = None,
        *,
        model_dir: Path | None = None,
        intra_op_num_threads: int | None = None,
    ) -> None:
        if model_dir is not None:
            resolved_dir = Path(model_dir).expanduser().resolve()
        elif config is not None:
            resolved_dir = (config.local_assets_dir / "ocr").resolve()
        else:
            resolved_dir = (PeaConfig.from_env().local_assets_dir / "ocr").resolve()

        self._det_model_path = resolved_dir / DET_MODEL_FILENAME
        self._rec_model_path = resolved_dir / REC_MODEL_FILENAME
        self._threads = (
            intra_op_num_threads
            if intra_op_num_threads is not None
            else (config.ocr_threads if config is not None else 4)
        )
        if self._threads < 1:
            raise ValueError(f"intra_op_num_threads must be >= 1; got {self._threads}")

        missing = [
            str(p)
            for p in (self._det_model_path, self._rec_model_path)
            if not p.is_file()
        ]
        if missing:
            raise FileNotFoundError(
                "Pinned offline RapidOCR ONNX model(s) missing: "
                + ", ".join(missing)
                + ". Run scripts/fetch_assets.py to populate local_assets/ocr/."
            )

        self._lock = threading.Lock()
        self._engine: Any = None

    @property
    def backend_name(self) -> OcrBackendKind:
        return "rapidocr"

    @property
    def det_model_path(self) -> Path:
        return self._det_model_path

    @property
    def rec_model_path(self) -> Path:
        return self._rec_model_path

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine

        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

        _redirect_rapidocr_logger_to_stderr()
        engine = RapidOCR(
            params={
                "Global.use_cls": False,
                "Global.log_level": "error",
                "Det.ocr_version": OCRVersion.PPOCRV5,
                "Det.model_type": ModelType.MOBILE,
                "Det.model_path": str(self._det_model_path),
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.lang_type": LangRec.CYRILLIC,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.model_path": str(self._rec_model_path),
                "EngineConfig.onnxruntime.intra_op_num_threads": self._threads,
            }
        )
        _redirect_rapidocr_logger_to_stderr()
        self._engine = engine
        return engine

    def recognize(self, image: ImageInput, *, mode: OcrMode = "det_rec") -> OcrResult:
        """Run offline OCR in ``det_rec`` or tight single-line ``rec_only`` mode."""
        if mode not in ("det_rec", "rec_only"):
            raise ValueError(f"Unsupported OCR mode: {mode!r}")

        pil_img = load_pil_image(image)
        width, height = pil_img.size
        use_det = mode == "det_rec"

        start = time.perf_counter()
        with self._lock:
            engine = self._get_engine()
            # ALWAYS pass use_det explicitly on every call to guard against the
            # rapidocr==3.10.0 bug where use_det=False persistently mutates engine.use_det.
            raw = engine(pil_img, use_det=use_det, use_cls=False, use_rec=True)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        boxes: list[OcrBox] = []
        if not use_det:
            txts = getattr(raw, "txts", None) or ()
            scores = getattr(raw, "scores", None) or ()
            for idx, txt in enumerate(txts):
                cleaned = str(txt).strip()
                if not cleaned:
                    continue
                score = scores[idx] if idx < len(scores) else 0.0
                boxes.append(
                    OcrBox(
                        text=cleaned,
                        confidence=_clamp01(score),
                        bbox_px=(0, 0, width, height),
                        bbox_norm=(0.0, 0.0, 1.0, 1.0),
                    )
                )
        else:
            raw_boxes = getattr(raw, "boxes", None)
            raw_txts = getattr(raw, "txts", None)
            raw_scores = getattr(raw, "scores", None)
            if raw_boxes is not None and raw_txts is not None:
                score_seq = raw_scores if raw_scores is not None else [1.0] * len(raw_txts)
                for poly, txt, score in zip(raw_boxes, raw_txts, score_seq, strict=False):
                    cleaned = str(txt).strip()
                    if not cleaned:
                        continue
                    xs = [float(pt[0]) for pt in poly]
                    ys = [float(pt[1]) for pt in poly]
                    x1 = max(0, min(width, int(round(min(xs)))))
                    y1 = max(0, min(height, int(round(min(ys)))))
                    x2 = max(x1, min(width, int(round(max(xs)))))
                    y2 = max(y1, min(height, int(round(max(ys)))))
                    nx1 = _clamp01(x1 / width) if width > 0 else 0.0
                    ny1 = _clamp01(y1 / height) if height > 0 else 0.0
                    nx2 = max(nx1, _clamp01(x2 / width) if width > 0 else 0.0)
                    ny2 = max(ny1, _clamp01(y2 / height) if height > 0 else 0.0)
                    boxes.append(
                        OcrBox(
                            text=cleaned,
                            confidence=_clamp01(score),
                            bbox_px=(x1, y1, x2, y2),
                            bbox_norm=(nx1, ny1, nx2, ny2),
                        )
                    )
                boxes = sort_boxes_reading_order(boxes)

        return OcrResult(
            backend="rapidocr",
            mode=mode,
            elapsed_ms=elapsed_ms,
            boxes=boxes,
        )

    def read_spec(
        self,
        image: ImageInput,
        spec: ReadoutSpec,
    ) -> tuple[OcrResult, float | int | str | None, str | None]:
        return read_spec_with_backend(self, image, spec)


__all__ = [
    "DET_MODEL_FILENAME",
    "REC_MODEL_FILENAME",
    "RapidOcrBackend",
]
