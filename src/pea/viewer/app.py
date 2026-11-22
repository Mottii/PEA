"""Loopback-only Starlette local dashboard (`pea-viewer`) with Host/Origin security guards."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from pea import __version__
from pea.config import (
    PathConfinementError,
    PeaConfig,
    is_loopback_host,
    normalize_origin,
    resolve_confined_path,
)
from pea.math_engine.calculator import load_calculations
from pea.schemas import ProvenanceEntry
from pea.workspace import RunWorkspace, WorkspaceManager

STATIC_DIR: Path = Path(__file__).resolve().parent / "static"

_ALLOWED_FONT_FILES: frozenset[str] = frozenset({"NotoSans-Regular.ttf", "NotoSans-Bold.ttf"})
_IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_DOWNLOAD_ATTACHMENT_SUFFIXES: frozenset[str] = frozenset({".docx", ".zip"})
_SANDBOXED_ACTIVE_SUFFIXES: frozenset[str] = frozenset({".html", ".htm", ".js", ".mjs", ".xhtml"})

_DEFAULT_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)

_MIME_OVERRIDES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".zip": "application/zip",
    ".ttf": "font/ttf",
    ".woff2": "font/woff2",
}

_IPV6_HOST_RE = re.compile(r"^\[([^\]]+)\](?::(\d+))?$")


def parse_host_header(raw_host: str | None) -> tuple[str, int] | None:
    """Parse an HTTP ``Host`` header into canonical ``(host, port)`` or ``None`` if invalid."""
    if not raw_host:
        return None
    text = raw_host.strip().lower()
    if not text or any(ch.isspace() for ch in text) or "/" in text or "@" in text:
        return None

    host: str
    port_str: str | None
    if text.startswith("["):
        match = _IPV6_HOST_RE.match(text)
        if not match:
            return None
        host = match.group(1)
        port_str = match.group(2)
    else:
        if ":" in text:
            parts = text.split(":")
            if len(parts) != 2 or not parts[0] or not parts[1].isdigit():
                return None
            host, port_str = parts[0], parts[1]
        else:
            host, port_str = text, None

    port = 80 if port_str is None else int(port_str)
    if not (1 <= port <= 65535):
        return None
    return (host, port)


def _validate_origin_header(raw_origin: str, req_host: str, req_port: int) -> bool:
    """Allow only strict same-origin HTTP loopback ``Origin`` headers."""
    text = raw_origin.strip()
    if not text or text.lower() == "null":
        return False
    try:
        canonical = normalize_origin(text)
    except ValueError:
        return False

    parsed = urlsplit(canonical)
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        return False
    origin_host = parsed.hostname.lower()
    origin_port = parsed.port if parsed.port is not None else 80
    if not is_loopback_host(origin_host):
        return False
    return origin_host == req_host and origin_port == req_port


def _has_traversal_segment(raw_path: bytes | str) -> bool:
    """Detect raw or percent-encoded ``..`` / backslash / NUL traversal in request URLs."""
    text = raw_path.decode("latin-1") if isinstance(raw_path, bytes) else raw_path
    decoded = unquote(text)
    if "\x00" in decoded or "\\" in decoded:
        return True
    segments = decoded.split("/")
    return any(seg == ".." for seg in segments)


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _apply_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Content-Security-Policy", _DEFAULT_CSP)
    return response


class _LoopbackSecurityMiddleware(BaseHTTPMiddleware):
    """Enforce read-only methods, loopback ``Host``, same-origin ``Origin``, and safe headers."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.method not in ("GET", "HEAD"):
            resp = JSONResponse(
                {"error": "Method not allowed; pea-viewer is strictly read-only"},
                status_code=405,
                headers={"Allow": "GET, HEAD"},
            )
            return _apply_security_headers(resp)

        parsed_host = parse_host_header(request.headers.get("host"))
        if parsed_host is None:
            resp = JSONResponse({"error": "Missing or malformed Host header"}, status_code=403)
            return _apply_security_headers(resp)

        req_host, req_port = parsed_host
        if not is_loopback_host(req_host):
            resp = JSONResponse(
                {"error": "Non-loopback Host header rejected (anti-DNS-rebinding guard)"},
                status_code=403,
            )
            return _apply_security_headers(resp)

        raw_origin = request.headers.get("origin")
        if raw_origin is not None and not _validate_origin_header(raw_origin, req_host, req_port):
            resp = JSONResponse(
                {"error": "Cross-origin or non-loopback Origin header rejected"},
                status_code=403,
            )
            return _apply_security_headers(resp)

        raw_path = request.scope.get("raw_path") or request.url.path
        if _has_traversal_segment(raw_path):
            resp = JSONResponse(
                {"error": "Path traversal segment rejected"},
                status_code=403,
            )
            return _apply_security_headers(resp)

        response = await call_next(request)
        return _apply_security_headers(response)


def _read_provenance_lenient(workspace: RunWorkspace) -> list[ProvenanceEntry]:
    if not workspace.provenance_path.is_file():
        return []
    entries: list[ProvenanceEntry] = []
    with workspace.provenance_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                entries.append(ProvenanceEntry.model_validate_json(stripped))
            except ValidationError:
                continue
    return entries


def _artifact_url(run_id: str, relpath: str) -> str:
    quoted_run = quote(run_id, safe="")
    quoted_rel = quote(relpath, safe="/")
    return f"/runs/{quoted_run}/{quoted_rel}"


def _list_screenshots(workspace: RunWorkspace) -> list[str]:
    shots: list[tuple[float, str]] = []
    for subdir in ("evidence", "pdf_pages"):
        base = workspace.root / subdir
        if not base.is_dir():
            continue
        for item in base.rglob("*"):
            if (
                item.is_file()
                and item.suffix.lower() in _IMAGE_SUFFIXES
                and not item.name.endswith(".tmp")
            ):
                rel = item.relative_to(workspace.root).as_posix()
                try:
                    mtime = item.stat().st_mtime
                except OSError:
                    mtime = 0.0
                shots.append((mtime, rel))
    # Sort newest first, breaking ties by reverse relpath (e.g., observe-0002 before observe-0001).
    shots.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    return [rel for _, rel in shots]


def _load_ocr_for_screenshot(
    workspace: RunWorkspace,
    screenshot_relpath: str,
    status_meta: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    candidates: list[str] = []
    stem = Path(screenshot_relpath).stem
    candidates.append(f"ocr/{stem}.json")
    latest_ocr_rel = status_meta.get("latest_ocr_relpath")
    if isinstance(latest_ocr_rel, str) and latest_ocr_rel not in candidates:
        candidates.append(latest_ocr_rel)

    for rel in candidates:
        try:
            resolved = workspace.resolve_path(rel)
        except PathConfinementError:
            continue
        if not resolved.is_file():
            continue
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        recorded_shot = payload.get("screenshot_relpath")
        if recorded_shot and recorded_shot != screenshot_relpath:
            continue
        ocr_obj = payload.get("ocr") if isinstance(payload.get("ocr"), dict) else payload
        boxes = ocr_obj.get("boxes") if isinstance(ocr_obj, dict) else None
        if isinstance(boxes, list):
            backend = ocr_obj.get("backend") if isinstance(ocr_obj, dict) else None
            return boxes, (str(backend) if backend else None), payload
    return [], None, {}


def _resolve_confined_image(workspace: RunWorkspace, relpath: Any) -> str | None:
    """Resolve *relpath* strictly inside *workspace* and verify it is an existing image file."""
    if not isinstance(relpath, str):
        return None
    try:
        resolved = workspace.resolve_path(relpath)
    except PathConfinementError:
        return None
    if not resolved.is_file() or resolved.suffix.lower() not in _IMAGE_SUFFIXES:
        return None
    return resolved.relative_to(workspace.root).as_posix()


def _find_annotated_for_screenshot(
    workspace: RunWorkspace,
    screenshot_relpath: str,
    status_meta: dict[str, Any],
    ocr_meta: dict[str, Any],
) -> str | None:
    candidates: list[str] = []
    if isinstance(ocr_meta.get("annotated_relpath"), str):
        candidates.append(ocr_meta["annotated_relpath"])
    if isinstance(status_meta.get("latest_annotated_relpath"), str):
        if status_meta.get("latest_screenshot_relpath") in (None, screenshot_relpath):
            candidates.append(status_meta["latest_annotated_relpath"])

    stem = Path(screenshot_relpath).stem
    suffix_match = re.search(r"(\d+)$", stem)
    if suffix_match:
        candidates.append(f"crops/annotated-{suffix_match.group(1)}.png")

    for rel in candidates:
        safe_rel = _resolve_confined_image(workspace, rel)
        if safe_rel is not None:
            return safe_rel
    return None


def _collect_downloads(workspace: RunWorkspace) -> dict[str, Any]:
    def _existing(rel: str) -> str | None:
        try:
            p = workspace.resolve_path(rel)
        except PathConfinementError:
            return None
        return rel if p.is_file() else None

    pdf_rel = _existing("reports/report.pdf")
    docx_rel = _existing("reports/report.docx")
    zip_rel = _existing("reports/evidence.zip")
    teacher_rel = _existing("reports/teacher-evidence.pdf")

    extra: list[dict[str, str]] = []
    standard = {pdf_rel, docx_rel, zip_rel, teacher_rel}
    if workspace.reports_dir.is_dir():
        for item in sorted(workspace.reports_dir.iterdir(), key=lambda p: p.name):
            if not item.is_file() or item.name.endswith(".tmp"):
                continue
            if item.suffix.lower() not in (".pdf", ".docx", ".zip"):
                continue
            rel = item.relative_to(workspace.root).as_posix()
            if rel in standard:
                continue
            extra.append(
                {
                    "name": item.name,
                    "relpath": rel,
                    "url": _artifact_url(workspace.run_id, rel),
                }
            )

    return {
        "pdf_relpath": pdf_rel,
        "pdf_url": _artifact_url(workspace.run_id, pdf_rel) if pdf_rel else None,
        "docx_relpath": docx_rel,
        "docx_url": _artifact_url(workspace.run_id, docx_rel) if docx_rel else None,
        "evidence_zip_relpath": zip_rel,
        "evidence_zip_url": _artifact_url(workspace.run_id, zip_rel) if zip_rel else None,
        "teacher_packet_relpath": teacher_rel,
        "teacher_packet_url": _artifact_url(workspace.run_id, teacher_rel) if teacher_rel else None,
        "extra_reports": extra,
    }


def _collect_figures(workspace: RunWorkspace) -> list[dict[str, Any]]:
    if not workspace.figures_dir.is_dir():
        return []
    grouped: dict[str, dict[str, Any]] = {}
    for item in sorted(workspace.figures_dir.iterdir(), key=lambda p: p.name):
        if not item.is_file() or item.name.endswith(".tmp"):
            continue
        ext = item.suffix.lower()
        if ext not in (".svg", ".png"):
            continue
        fig_id = item.stem
        rel = item.relative_to(workspace.root).as_posix()
        entry = grouped.setdefault(
            fig_id,
            {
                "figure_id": fig_id,
                "svg_relpath": None,
                "svg_url": None,
                "png_relpath": None,
                "png_url": None,
            },
        )
        if ext == ".svg":
            entry["svg_relpath"] = rel
            entry["svg_url"] = _artifact_url(workspace.run_id, rel)
        elif ext == ".png":
            entry["png_relpath"] = rel
            entry["png_url"] = _artifact_url(workspace.run_id, rel)
    return list(grouped.values())


def _summarize_run(workspace: RunWorkspace) -> dict[str, Any]:
    status = workspace.read_status()
    entries = _read_provenance_lenient(workspace)
    downloads = _collect_downloads(workspace)
    shots = _list_screenshots(workspace)
    latest_shot = _resolve_confined_image(
        workspace, status.metadata.get("latest_screenshot_relpath")
    )
    if latest_shot is None and shots:
        latest_shot = shots[0]

    return {
        "run_id": status.run_id,
        "phase": status.phase,
        "label": status.label,
        "source": status.source,
        "created_at": status.created_at.isoformat(),
        "updated_at": status.updated_at.isoformat(),
        "message": status.message,
        "error": status.error,
        "progress_current": status.progress_current,
        "progress_total": status.progress_total,
        "provenance_entries": len(entries),
        "has_pdf": downloads["pdf_relpath"] is not None,
        "has_docx": downloads["docx_relpath"] is not None,
        "has_evidence_zip": downloads["evidence_zip_relpath"] is not None,
        "has_teacher_packet": downloads["teacher_packet_relpath"] is not None,
        "latest_screenshot_relpath": latest_shot,
    }


def _detail_run(workspace: RunWorkspace) -> dict[str, Any]:
    status = workspace.read_status()
    entries = _read_provenance_lenient(workspace)
    verification = workspace.verify_provenance()

    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    source_by_relpath: dict[str, str] = {}
    for entry in entries:
        by_source[entry.source] = by_source.get(entry.source, 0) + 1
        by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
        source_by_relpath[entry.relpath] = entry.source

    try:
        calcs_count = len(load_calculations(workspace))
    except ValueError:
        calcs_count = 0

    datasets: list[dict[str, str]] = []
    ds_dir = workspace.root / "datasets"
    if ds_dir.is_dir():
        for item in sorted(ds_dir.glob("*.json"), key=lambda p: p.name):
            if item.is_file() and not item.name.endswith(".tmp"):
                rel = item.relative_to(workspace.root).as_posix()
                datasets.append(
                    {
                        "dataset_id": item.stem,
                        "relpath": rel,
                        "url": _artifact_url(workspace.run_id, rel),
                    }
                )

    shots = _list_screenshots(workspace)
    latest_shot = _resolve_confined_image(
        workspace, status.metadata.get("latest_screenshot_relpath")
    )
    if latest_shot is None and shots:
        latest_shot = shots[0]

    latest_capture: dict[str, Any] | None = None
    if latest_shot is not None:
        ocr_boxes, ocr_backend, ocr_meta = _load_ocr_for_screenshot(
            workspace, latest_shot, status.metadata
        )
        annotated_rel = _find_annotated_for_screenshot(
            workspace, latest_shot, status.metadata, ocr_meta
        )
        latest_capture = {
            "screenshot_relpath": latest_shot,
            "screenshot_url": _artifact_url(workspace.run_id, latest_shot),
            "annotated_relpath": annotated_rel,
            "annotated_url": (
                _artifact_url(workspace.run_id, annotated_rel) if annotated_rel else None
            ),
            "ocr_boxes": ocr_boxes,
            "ocr_backend": ocr_backend,
            "stage_width": ocr_meta.get("stage_width"),
            "stage_height": ocr_meta.get("stage_height"),
            "header_height_px": ocr_meta.get("header_height_px"),
            "source": source_by_relpath.get(latest_shot, status.source or "simulator_captured"),
        }

    screenshot_items: list[dict[str, Any]] = []
    for rel in shots[:50]:
        ocr_boxes_item, _, ocr_meta_item = _load_ocr_for_screenshot(
            workspace, rel, status.metadata
        )
        ann_rel_item = _find_annotated_for_screenshot(
            workspace, rel, status.metadata, ocr_meta_item
        )
        screenshot_items.append(
            {
                "relpath": rel,
                "url": _artifact_url(workspace.run_id, rel),
                "annotated_url": (
                    _artifact_url(workspace.run_id, ann_rel_item) if ann_rel_item else None
                ),
                "ocr_boxes": ocr_boxes_item,
                "source": source_by_relpath.get(rel, status.source or "simulator_captured"),
            }
        )

    return {
        "run_id": status.run_id,
        "status": status.model_dump(mode="json"),
        "provenance_verification": verification.model_dump(mode="json"),
        "provenance_summary": {
            "total": len(entries),
            "by_source": by_source,
            "by_kind": by_kind,
        },
        "provenance": [entry.model_dump(mode="json") for entry in entries],
        "calculations_count": calcs_count,
        "datasets": datasets,
        "figures": _collect_figures(workspace),
        "downloads": _collect_downloads(workspace),
        "latest_capture": latest_capture,
        "screenshots": screenshot_items,
    }


def create_viewer_app(
    config: PeaConfig | None = None,
    *,
    workspace_manager: WorkspaceManager | None = None,
) -> Starlette:
    """Create the loopback-hardened Starlette ``pea-viewer`` ASGI application."""
    cfg = config or PeaConfig.from_env()
    wm = workspace_manager or WorkspaceManager.from_config(cfg)

    async def index(_request: Request) -> Response:
        index_path = STATIC_DIR / "index.html"
        if not index_path.is_file():
            return JSONResponse({"error": "Viewer UI index.html not found"}, status_code=404)
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    async def serve_static(request: Request) -> Response:
        asset_path = str(request.path_params.get("asset_path", ""))
        try:
            resolved = resolve_confined_path(STATIC_DIR, asset_path)
        except PathConfinementError:
            return JSONResponse({"error": "Static path escapes allowed directory"}, status_code=403)
        if not resolved.is_file():
            return JSONResponse({"error": "Static asset not found"}, status_code=404)
        return FileResponse(resolved, media_type=_guess_mime(resolved))

    async def serve_font(request: Request) -> Response:
        filename = str(request.path_params.get("filename", ""))
        if filename not in _ALLOWED_FONT_FILES:
            return JSONResponse({"error": "Font not allowed"}, status_code=404)
        fonts_dir = cfg.local_assets_dir / "fonts"
        try:
            resolved = resolve_confined_path(fonts_dir, filename)
        except PathConfinementError:
            return JSONResponse({"error": "Font path escapes allowed directory"}, status_code=403)
        if not resolved.is_file():
            return JSONResponse({"error": "Bundled font not found"}, status_code=404)
        return FileResponse(resolved, media_type="font/ttf")

    async def api_health(_request: Request) -> Response:
        runs = wm.list_runs()
        return JSONResponse(
            {
                "ok": True,
                "service": "pea-viewer",
                "version": __version__,
                "loopback_host": cfg.loopback_host,
                "viewer_port": cfg.viewer_port,
                "runs_count": len(runs),
                "zero_api_cost": True,
            }
        )

    async def api_runs(_request: Request) -> Response:
        summaries: list[dict[str, Any]] = []
        for status in wm.list_runs():
            try:
                ws = wm.get_run(status.run_id)
                summaries.append(_summarize_run(ws))
            except (FileNotFoundError, PathConfinementError, ValidationError, OSError):
                continue
        return JSONResponse({"runs": summaries, "total": len(summaries)})

    async def api_run_detail(request: Request) -> Response:
        run_id = str(request.path_params.get("run_id", ""))
        try:
            ws = wm.get_run(run_id)
        except PathConfinementError:
            return JSONResponse({"error": "Invalid run_id"}, status_code=403)
        except FileNotFoundError:
            return JSONResponse({"error": f"Run not found: {run_id}"}, status_code=404)

        if not ws.status_path.is_file():
            return JSONResponse({"error": f"Run status missing: {run_id}"}, status_code=404)
        try:
            detail = _detail_run(ws)
        except ValidationError:
            return JSONResponse({"error": f"Corrupt run status: {run_id}"}, status_code=500)
        return JSONResponse(detail)

    async def serve_run_artifact(request: Request) -> Response:
        run_id = str(request.path_params.get("run_id", ""))
        artifact = str(request.path_params.get("artifact", ""))

        try:
            ws = wm.get_run(run_id)
        except PathConfinementError:
            return JSONResponse({"error": "Invalid run_id"}, status_code=403)
        except FileNotFoundError:
            return JSONResponse({"error": f"Run not found: {run_id}"}, status_code=404)

        try:
            resolved = ws.resolve_path(artifact)
        except PathConfinementError:
            return JSONResponse(
                {"error": "Artifact path escapes confined run directory"},
                status_code=403,
            )

        if resolved.name.endswith(".tmp") or ".tmp." in resolved.name:
            return JSONResponse({"error": "Temporary files are not served"}, status_code=404)
        if not resolved.exists() or not resolved.is_file():
            return JSONResponse({"error": "Artifact not found"}, status_code=404)

        ext = resolved.suffix.lower()
        force_download = request.query_params.get("download") in ("1", "true", "yes")
        safe_disp_name = resolved.name.replace('"', "_").replace("\r", "").replace("\n", "")
        headers: dict[str, str] = {}

        if ext in _SANDBOXED_ACTIVE_SUFFIXES:
            headers["Content-Security-Policy"] = "sandbox"
            headers["Content-Disposition"] = f'attachment; filename="{safe_disp_name}"'
        elif ext == ".svg":
            headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
            if force_download:
                headers["Content-Disposition"] = f'attachment; filename="{safe_disp_name}"'
        elif force_download or ext in _DOWNLOAD_ATTACHMENT_SUFFIXES:
            headers["Content-Disposition"] = f'attachment; filename="{safe_disp_name}"'

        return FileResponse(resolved, media_type=_guess_mime(resolved), headers=headers)

    routes = [
        Route("/", index, methods=["GET", "HEAD"]),
        Route("/static/{asset_path:path}", serve_static, methods=["GET", "HEAD"]),
        Route("/assets/fonts/{filename:str}", serve_font, methods=["GET", "HEAD"]),
        Route("/api/health", api_health, methods=["GET", "HEAD"]),
        Route("/api/runs", api_runs, methods=["GET", "HEAD"]),
        Route("/api/runs/{run_id:str}", api_run_detail, methods=["GET", "HEAD"]),
        Route("/runs/{run_id:str}/{artifact:path}", serve_run_artifact, methods=["GET", "HEAD"]),
    ]

    app = Starlette(debug=False, routes=routes)
    app.add_middleware(_LoopbackSecurityMiddleware)
    return app


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``pea-viewer`` (strictly loopback-bound Uvicorn server)."""
    cfg = PeaConfig.from_env()
    parser = argparse.ArgumentParser(
        prog="pea-viewer",
        description="PEA v2 lightweight loopback local run & live-capture viewer.",
    )
    parser.add_argument(
        "--host",
        default=cfg.loopback_host,
        help=f"Loopback interface to bind (default: {cfg.loopback_host}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=cfg.viewer_port,
        help=f"Local TCP port to bind (default: {cfg.viewer_port}).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"pea-viewer {__version__}",
    )
    args = parser.parse_args(argv)

    if not is_loopback_host(args.host):
        parser.error(
            f"--host must be a loopback address (127.0.0.1, localhost, ::1); got {args.host!r}"
        )
    if not (1 <= args.port <= 65535):
        parser.error(f"--port must be in 1..65535; got {args.port}")

    import uvicorn

    effective_cfg = PeaConfig(
        project_root=cfg.project_root,
        local_assets_dir=cfg.local_assets_dir,
        runs_dir=cfg.runs_dir,
        profiles_dir=cfg.profiles_dir,
        loopback_host=args.host,
        viewer_port=args.port,
        allowed_origins=cfg.allowed_origins,
        ocr_backend=cfg.ocr_backend,
        ocr_threads=cfg.ocr_threads,
        ocr_timeout_s=cfg.ocr_timeout_s,
        browser_executable=cfg.browser_executable,
    )
    app = create_viewer_app(effective_cfg)
    uvicorn.run(app, host=effective_cfg.loopback_host, port=effective_cfg.viewer_port)
    return 0


__all__ = [
    "STATIC_DIR",
    "create_viewer_app",
    "main",
    "parse_host_header",
]


if __name__ == "__main__":
    raise SystemExit(main())
