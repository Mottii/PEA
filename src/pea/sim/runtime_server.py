"""Loopback-only threaded HTTP server for auto-sized Ruffle SWFs and local HTML5 simulators."""

from __future__ import annotations

import html
import json
import mimetypes
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

from pea.config import PathConfinementError, is_loopback_host, resolve_confined_path
from pea.schemas import SwfMeta
from pea.sim.swf_meta import inspect_swf

_MIME_OVERRIDES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
    ".swf": "application/x-shockwave-flash",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".map": "application/json; charset=utf-8",
}


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def render_ruffle_index_html(
    swf_filename: str,
    meta: SwfMeta,
    *,
    header_height_px: int = 39,
) -> str:
    """Build auto-sized loopback HTML locking ``<ruffle-player>`` to ``(0, header_h, W, H)``."""
    safe_name = html.escape(swf_filename, quote=True)
    url_name = quote(swf_filename)
    js_sim_url = json.dumps(f"/sim/{url_name}", ensure_ascii=False).replace("</", "<\\/")
    js_ready_text = json.dumps(
        f"{swf_filename} — ready ({meta.stage_width}x{meta.stage_height})",
        ensure_ascii=False,
    ).replace("</", "<\\/")
    w = meta.stage_width
    h = meta.stage_height
    hh = header_height_px

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>PEA Simulator — {safe_name}</title>
  <style>
    html, body {{
      margin: 0;
      padding: 0;
      overflow: hidden;
      background: #f4f6f8;
      font-family: -apple-system, BlinkMacSystemFont, "Noto Sans", sans-serif;
    }}
    #status {{
      height: {hh}px;
      box-sizing: border-box;
      margin: 0;
      padding: 0 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 13px;
      line-height: 19px;
      color: #1f2937;
      background: #e5e7eb;
      border-bottom: 1px solid #cbd5e1;
      user-select: none;
    }}
    #player {{
      width: {w}px;
      height: {h}px;
      margin: 0;
      padding: 0;
      position: relative;
      background: #ffffff;
    }}
    #player ruffle-player {{
      display: block;
      width: {w}px;
      height: {h}px;
    }}
  </style>
</head>
<body>
  <div id="status">
    <span id="status-text">Loading {safe_name} ({w}x{h})...</span>
    <span id="status-meta">Ruffle sandbox (no network / no script access)</span>
  </div>
  <div id="player"></div>
  <script>
    window.__peaReady = false;
    window.__peaError = null;
    window.RufflePlayer = window.RufflePlayer || {{}};
    window.RufflePlayer.config = {{
      publicPath: "/ruffle/",
      autoplay: "on",
      unmuteOverlay: "hidden",
      allowScriptAccess: false,
      allowNetworking: "none",
      letterbox: "off",
      logLevel: "warn",
    }};
  </script>
  <script src="/ruffle/ruffle.js"></script>
  <script>
    (function () {{
      const statusEl = document.getElementById("status-text");
      const container = document.getElementById("player");
      try {{
        const ruffle = window.RufflePlayer.newest();
        const player = ruffle.createPlayer();
        player.style.width = "{w}px";
        player.style.height = "{h}px";
        container.appendChild(player);
        const loadResult = player.load({{
          url: {js_sim_url},
          allowScriptAccess: false,
          allowNetworking: "none",
        }});
        Promise.resolve(loadResult).then(() => {{
          statusEl.textContent = {js_ready_text};
          window.__peaReady = true;
        }}).catch((err) => {{
          window.__peaError = String(err);
          statusEl.textContent = "Load error: " + String(err);
        }});
      }} catch (err) {{
        window.__peaError = String(err);
        statusEl.textContent = "Init error: " + String(err);
      }}
    }})();
  </script>
</body>
</html>
"""


class _PeaThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _PeaThreadingHTTPServerV6(_PeaThreadingHTTPServer):
    address_family = socket.AF_INET6


class SimRuntimeServer:
    """Loopback HTTP server with anti-DNS-rebinding ``Host`` checks and path confinement."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        header_height_px: int = 39,
    ) -> None:
        if not is_loopback_host(host):
            raise ValueError(f"SimRuntimeServer host must be loopback; got {host!r}")
        if not (0 <= port <= 65535):
            raise ValueError(f"port must be in 0..65535; got {port}")
        if header_height_px < 0:
            raise ValueError(f"header_height_px must be >= 0; got {header_height_px}")

        normalized_host = host.strip().lower()
        if normalized_host.startswith("[") and normalized_host.endswith("]"):
            normalized_host = normalized_host[1:-1]

        self._host = normalized_host
        self._requested_port = port
        self._header_height_px = header_height_px

        self._mode: Literal["swf", "html5"] | None = None
        self._swf_path: Path | None = None
        self._swf_meta: SwfMeta | None = None
        self._ruffle_dir: Path | None = None
        self._index_html_bytes: bytes = b""
        self._html5_root: Path | None = None
        self._html5_entry: str = "index.html"

        self._httpd: _PeaThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bound_port: int | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._bound_port is None:
            raise RuntimeError("SimRuntimeServer has not been started")
        return self._bound_port

    @property
    def origin(self) -> str:
        host_fmt = f"[{self._host}]" if ":" in self._host else self._host
        return f"http://{host_fmt}:{self.port}"

    @property
    def swf_meta(self) -> SwfMeta | None:
        return self._swf_meta

    @property
    def header_height_px(self) -> int:
        return self._header_height_px

    def _allowed_hosts(self) -> set[str]:
        p = self.port
        if ":" in self._host:
            return {f"[{self._host}]:{p}"}
        return {f"{self._host}:{p}"}

    def configure_swf(
        self,
        swf_path: str | Path,
        ruffle_dir: str | Path,
        *,
        expected_sha256: str | None = None,
    ) -> SwfMeta:
        """Configure server to host an auto-sized Ruffle harness for *swf_path*."""
        resolved_swf = Path(swf_path).expanduser().resolve()
        resolved_ruffle = Path(ruffle_dir).expanduser().resolve()
        if not resolved_swf.is_file():
            raise FileNotFoundError(f"SWF file not found: {resolved_swf}")
        if not (resolved_ruffle / "ruffle.js").is_file():
            raise FileNotFoundError(
                f"Ruffle bundle not found (missing ruffle.js) in: {resolved_ruffle}"
            )

        meta = inspect_swf(resolved_swf)
        if expected_sha256 is not None and meta.sha256 != expected_sha256.strip().lower():
            raise ValueError(
                f"SWF SHA-256 mismatch for {resolved_swf.name}: "
                f"expected {expected_sha256.strip().lower()}, actual {meta.sha256}"
            )
        html_str = render_ruffle_index_html(
            resolved_swf.name,
            meta,
            header_height_px=self._header_height_px,
        )
        self._mode = "swf"
        self._swf_path = resolved_swf
        self._swf_meta = meta
        self._ruffle_dir = resolved_ruffle
        self._index_html_bytes = html_str.encode("utf-8")
        self._html5_root = None
        return meta

    def configure_html5_dir(self, root_dir: str | Path, *, entry_file: str = "index.html") -> Path:
        """Configure server to host a strictly confined local HTML5 simulator directory."""
        resolved_root = Path(root_dir).expanduser().resolve()
        if not resolved_root.is_dir():
            raise FileNotFoundError(f"HTML5 simulator directory not found: {resolved_root}")
        entry_path = resolve_confined_path(resolved_root, entry_file)
        if not entry_path.is_file():
            raise FileNotFoundError(f"HTML5 entry file not found: {entry_path}")

        self._mode = "html5"
        self._html5_root = resolved_root
        self._html5_entry = entry_path.relative_to(resolved_root).as_posix()
        self._swf_path = None
        self._swf_meta = None
        self._ruffle_dir = None
        self._index_html_bytes = b""
        return entry_path

    def start(self) -> str:
        """Start the loopback HTTP server in a background daemon thread and return its origin."""
        if self._mode is None:
            raise RuntimeError("Configure SWF or HTML5 directory before starting SimRuntimeServer")
        if self._httpd is not None:
            return self.origin

        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_security_headers(self) -> None:
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")

            def _send_bytes(
                self,
                status: HTTPStatus,
                body: bytes,
                content_type: str,
                *,
                head_only: bool = False,
            ) -> None:
                self.send_response(status)
                self._send_security_headers()
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head_only and body:
                    self.wfile.write(body)

            def _send_text_error(
                self, status: HTTPStatus, message: str, *, head_only: bool
            ) -> None:
                self._send_bytes(
                    status,
                    (message + "\n").encode("utf-8"),
                    "text/plain; charset=utf-8",
                    head_only=head_only,
                )

            def _check_host(self, *, head_only: bool) -> bool:
                host_header = (self.headers.get("Host") or "").strip().lower()
                if host_header not in parent._allowed_hosts():
                    self._send_text_error(
                        HTTPStatus.FORBIDDEN,
                        "Forbidden: invalid Host header",
                        head_only=head_only,
                    )
                    return False
                return True

            def _serve_file(self, file_path: Path, *, head_only: bool) -> None:
                if not file_path.is_file():
                    self._send_text_error(HTTPStatus.NOT_FOUND, "Not Found", head_only=head_only)
                    return
                data = file_path.read_bytes()
                self._send_bytes(
                    HTTPStatus.OK,
                    data,
                    _guess_mime(file_path),
                    head_only=head_only,
                )

            def _handle(self, *, head_only: bool) -> None:
                if not self._check_host(head_only=head_only):
                    return

                raw_path = urlsplit(self.path).path
                decoded_path = unquote(raw_path)

                if parent._mode == "swf":
                    if decoded_path in ("/", "/index.html"):
                        self._send_bytes(
                            HTTPStatus.OK,
                            parent._index_html_bytes,
                            "text/html; charset=utf-8",
                            head_only=head_only,
                        )
                        return

                    if decoded_path.startswith("/ruffle/"):
                        rel = decoded_path.removeprefix("/ruffle/")
                        assert parent._ruffle_dir is not None
                        try:
                            target = resolve_confined_path(parent._ruffle_dir, rel)
                        except PathConfinementError:
                            self._send_text_error(
                                HTTPStatus.FORBIDDEN,
                                "Forbidden: path escape",
                                head_only=head_only,
                            )
                            return
                        self._serve_file(target, head_only=head_only)
                        return

                    if decoded_path.startswith("/sim/"):
                        rel = decoded_path.removeprefix("/sim/")
                        assert parent._swf_path is not None
                        if rel != parent._swf_path.name:
                            self._send_text_error(
                                HTTPStatus.NOT_FOUND,
                                "Not Found",
                                head_only=head_only,
                            )
                            return
                        self._serve_file(parent._swf_path, head_only=head_only)
                        return

                    self._send_text_error(HTTPStatus.NOT_FOUND, "Not Found", head_only=head_only)
                    return

                # HTML5 confined directory mode
                assert parent._html5_root is not None
                rel = parent._html5_entry if decoded_path == "/" else decoded_path.lstrip("/")
                try:
                    target = resolve_confined_path(parent._html5_root, rel)
                except PathConfinementError:
                    self._send_text_error(
                        HTTPStatus.FORBIDDEN,
                        "Forbidden: path escape",
                        head_only=head_only,
                    )
                    return
                self._serve_file(target, head_only=head_only)

            def do_GET(self) -> None:  # noqa: N802
                self._handle(head_only=False)

            def do_HEAD(self) -> None:  # noqa: N802
                self._handle(head_only=True)

            def do_POST(self) -> None:  # noqa: N802
                self.close_connection = True
                self._send_text_error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method Not Allowed",
                    head_only=False,
                )

        server_cls = (
            _PeaThreadingHTTPServerV6 if ":" in self._host else _PeaThreadingHTTPServer
        )
        httpd = server_cls((self._host, self._requested_port), _Handler)
        self._httpd = httpd
        self._bound_port = int(httpd.server_address[1])
        thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.1},
            name=f"pea-sim-http-{self._bound_port}",
            daemon=True,
        )
        thread.start()
        self._thread = thread
        return self.origin

    def stop(self) -> None:
        """Shut down the loopback HTTP server cleanly."""
        httpd = self._httpd
        thread = self._thread
        self._httpd = None
        self._thread = None
        self._bound_port = None
        if httpd is not None:
            try:
                httpd.shutdown()
            finally:
                httpd.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    def __enter__(self) -> SimRuntimeServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


__all__ = [
    "SimRuntimeServer",
    "render_ruffle_index_html",
]
