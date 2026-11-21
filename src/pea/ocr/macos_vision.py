"""Optional timeout-guarded macOS Vision Swift OCR backend with automatic RapidOCR fallback."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from pea.config import PeaConfig
from pea.ocr.base import (
    ImageInput,
    OcrBackend,
    load_pil_image,
    read_spec_with_backend,
    sort_boxes_reading_order,
)
from pea.ocr.rapid_ocr import RapidOcrBackend
from pea.schemas import OcrBackendKind, OcrBox, OcrMode, OcrResult, ReadoutSpec


def compile_macos_vision_helper(
    source_path: Path,
    binary_path: Path,
    module_cache_dir: Path,
    *,
    compile_timeout_s: float = 60.0,
) -> Path:
    """Compile ``native/macos_vision_ocr.swift`` once with an explicit module cache path."""
    if sys.platform != "darwin":
        raise RuntimeError("macOS Vision OCR helper can only be compiled on macOS (darwin).")
    if not source_path.is_file():
        raise FileNotFoundError(f"Swift Vision source not found: {source_path}")

    binary_path.parent.mkdir(parents=True, exist_ok=True)
    module_cache_dir.mkdir(parents=True, exist_ok=True)

    if binary_path.is_file() and binary_path.stat().st_mtime >= source_path.stat().st_mtime:
        return binary_path

    tmp_bin = binary_path.with_suffix(".tmp")
    cmd = [
        "swiftc",
        "-O",
        "-module-cache-path",
        str(module_cache_dir),
        str(source_path),
        "-o",
        str(tmp_bin),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=compile_timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        if tmp_bin.exists():
            tmp_bin.unlink(missing_ok=True)
        raise RuntimeError(
            f"swiftc failed compiling {source_path.name} (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    tmp_bin.replace(binary_path)
    return binary_path


class MacOsVisionOcrBackend:
    """Persistent JSON-lines macOS Vision helper with hard per-call timeout & RapidOCR fallback."""

    def __init__(
        self,
        config: PeaConfig | None = None,
        *,
        fallback: OcrBackend | None = None,
        helper_command: list[str] | None = None,
        timeout_s: float | None = None,
        languages: tuple[str, ...] = ("ru-RU", "en-US"),
    ) -> None:
        self._config = config or PeaConfig.from_env()
        self._fallback: OcrBackend | None = fallback
        self._helper_command = list(helper_command) if helper_command is not None else None
        self._timeout_s = float(timeout_s if timeout_s is not None else self._config.ocr_timeout_s)
        if self._timeout_s <= 0:
            raise ValueError(f"timeout_s must be > 0; got {self._timeout_s}")
        self._languages = list(languages)

        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[str | None] | None = None
        self._reader_thread: threading.Thread | None = None

        self.fallback_count: int = 0
        self.last_fallback_reason: str | None = None

    @property
    def backend_name(self) -> OcrBackendKind:
        return "macos_vision"

    def _get_fallback(self) -> OcrBackend:
        if self._fallback is None:
            self._fallback = RapidOcrBackend(self._config)
        return self._fallback

    def _resolve_command(self) -> list[str]:
        if self._helper_command is not None:
            return self._helper_command
        source = self._config.project_root / "native" / "macos_vision_ocr.swift"
        cache_dir = self._config.local_assets_dir / "swift-cache"
        binary = self._config.local_assets_dir / "bin" / "macos_vision_ocr"
        compiled = compile_macos_vision_helper(source, binary, cache_dir)
        return [str(compiled)]

    def _kill_child(self) -> None:
        proc = self._proc
        self._proc = None
        self._stdout_queue = None
        self._reader_thread = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2.0)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def _ensure_child(self) -> tuple[subprocess.Popen[str], queue.Queue[str | None]]:
        if self._proc is not None and self._proc.poll() is None and self._stdout_queue is not None:
            return (self._proc, self._stdout_queue)

        self._kill_child()
        cmd = self._resolve_command()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        out_q: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    out_q.put(line)
            finally:
                out_q.put(None)

        thread = threading.Thread(target=_reader, name="pea-vision-ocr-reader", daemon=True)
        thread.start()
        self._proc = proc
        self._stdout_queue = out_q
        self._reader_thread = thread
        return (proc, out_q)

    def _fallback_recognize(
        self,
        image: ImageInput,
        *,
        mode: OcrMode,
        reason: str,
    ) -> OcrResult:
        with self._lock:
            self.fallback_count += 1
            self.last_fallback_reason = reason
            fallback = self._get_fallback()
        return fallback.recognize(image, mode=mode)

    def _send_and_receive(self, payload: dict[str, Any]) -> dict[str, Any]:
        proc, out_q = self._ensure_child()
        if proc.stdin is None:
            raise RuntimeError("Vision OCR child stdin is unavailable")
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

        try:
            raw_line = out_q.get(timeout=self._timeout_s)
        except queue.Empty as exc:
            self._kill_child()
            raise TimeoutError(
                f"macOS Vision OCR timed out after {self._timeout_s:.2f}s"
            ) from exc

        if raw_line is None:
            self._kill_child()
            raise RuntimeError("macOS Vision OCR child exited unexpectedly before replying")

        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            self._kill_child()
            raise RuntimeError(f"Malformed JSON from macOS Vision OCR child: {raw_line!r}") from exc

        if not isinstance(parsed, dict):
            self._kill_child()
            raise RuntimeError("Unexpected non-object JSON payload from macOS Vision OCR child")
        return parsed

    def recognize(self, image: ImageInput, *, mode: OcrMode = "det_rec") -> OcrResult:
        """Run timeout-guarded Vision OCR; kill child and fall back to RapidOCR on any failure."""
        if mode not in ("det_rec", "rec_only"):
            raise ValueError(f"Unsupported OCR mode: {mode!r}")

        pil_img = load_pil_image(image)
        if self._helper_command is None and sys.platform != "darwin":
            return self._fallback_recognize(
                pil_img, mode=mode, reason=f"Unsupported platform {sys.platform!r}"
            )

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            pil_img.save(tmp_path, format="PNG")

            start = time.perf_counter()
            with self._lock:
                response = self._send_and_receive(
                    {
                        "image_path": str(tmp_path),
                        "mode": mode,
                        "languages": self._languages,
                        "level": "accurate",
                    }
                )
            elapsed_ms = (time.perf_counter() - start) * 1000.0

            if not response.get("ok"):
                err_msg = str(response.get("error") or "Unknown Vision OCR error")
                return self._fallback_recognize(pil_img, mode=mode, reason=err_msg)

            boxes: list[OcrBox] = []
            for item in response.get("boxes", []):
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                px = item.get("bbox_px", [0, 0, pil_img.width, pil_img.height])
                norm = item.get("bbox_norm", [0.0, 0.0, 1.0, 1.0])
                conf = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
                boxes.append(
                    OcrBox(
                        text=text,
                        confidence=conf,
                        bbox_px=(int(px[0]), int(px[1]), int(px[2]), int(px[3])),
                        bbox_norm=(float(norm[0]), float(norm[1]), float(norm[2]), float(norm[3])),
                    )
                )

            boxes = sort_boxes_reading_order(boxes)
            reported_ms = float(response.get("elapsed_ms", elapsed_ms))
            return OcrResult(
                backend="macos_vision",
                mode=mode,
                elapsed_ms=max(0.0, reported_ms),
                boxes=boxes,
            )
        except Exception as exc:
            with self._lock:
                self._kill_child()
            return self._fallback_recognize(pil_img, mode=mode, reason=str(exc))
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def read_spec(
        self,
        image: ImageInput,
        spec: ReadoutSpec,
    ) -> tuple[OcrResult, float | int | str | None, str | None]:
        return read_spec_with_backend(self, image, spec)

    def close(self) -> None:
        with self._lock:
            self._kill_child()

    def __enter__(self) -> MacOsVisionOcrBackend:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self._kill_child()
        except Exception:
            pass


__all__ = [
    "MacOsVisionOcrBackend",
    "compile_macos_vision_helper",
]
