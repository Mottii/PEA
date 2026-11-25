"""Configuration, path confinement, browser discovery, and origin security guards."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

OcrBackendName = Literal["rapidocr", "macos_vision"]

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}
_CHROMIUM_RELATIVE_CANDIDATES: tuple[str, ...] = (
    "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "chrome-linux64/chrome",
    "chrome-linux/chrome",
)


class PathConfinementError(ValueError):
    """Raised when a requested path escapes its confined base directory."""


def is_loopback_host(host: str) -> bool:
    """Return True if *host* refers strictly to a local loopback interface."""
    normalized = host.strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized in LOOPBACK_HOSTS


def _parse_canonical_origin(raw: str, *, require_origin_only: bool) -> tuple[str, str, int]:
    """Parse an HTTP(S) URL or origin into canonical ``(scheme, host, port)``."""
    text = raw.strip()
    if not text or any(ch.isspace() for ch in text) or "*" in text:
        raise ValueError(f"Invalid origin/URL: {raw!r}")

    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(f"Unsupported URL scheme in {raw!r}; only http/https allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Credentials/userinfo are forbidden in {raw!r}")
    if not parsed.hostname:
        raise ValueError(f"Missing hostname in {raw!r}")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid port in {raw!r}") from exc

    if require_origin_only:
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError(
                f"Allowlisted origin must not contain path, query, or fragment: {raw!r}"
            )

    effective_port = port if port is not None else _DEFAULT_PORTS[scheme]
    return (scheme, parsed.hostname.lower(), effective_port)


def normalize_origin(origin: str) -> str:
    """Validate and canonicalize a single HTTP(S) origin string (``scheme://host[:port]``)."""
    scheme, host, port = _parse_canonical_origin(origin, require_origin_only=True)
    host_display = f"[{host}]" if ":" in host else host
    if port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host_display}"
    return f"{scheme}://{host_display}:{port}"


def parse_allowed_origins(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated ``PEA_ALLOWED_ORIGINS`` value into unique canonical origins."""
    if not raw or not raw.strip():
        return ()
    seen: set[str] = set()
    origins: list[str] = []
    for item in raw.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        canonical = normalize_origin(cleaned)
        if canonical not in seen:
            seen.add(canonical)
            origins.append(canonical)
    return tuple(origins)


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the user-level PEA cache directory across macOS, Linux, and Windows."""
    source_env = os.environ if env is None else env
    xdg = source_env.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "pea"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "pea"
    if sys.platform == "win32":
        local_app = source_env.get("LOCALAPPDATA", "").strip()
        base = Path(local_app).expanduser() if local_app else (Path.home() / "AppData" / "Local")
        return base / "pea" / "Cache"
    return Path.home() / ".cache" / "pea"


def _discover_project_root() -> Path:
    """Locate the PEA source checkout root, or fall back to the current working directory."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "pea").is_dir():
            return parent
    return Path.cwd().resolve()


def _chromium_revision_key(dir_path: Path) -> tuple[int, str]:
    """Sort key preferring higher numeric ``chromium-<rev>`` build numbers."""
    suffix = dir_path.name.removeprefix("chromium-")
    try:
        return (int(suffix), dir_path.name)
    except ValueError:
        return (-1, dir_path.name)


def _playwright_cache_dirs(env: Mapping[str, str]) -> list[Path]:
    """Return candidate Playwright browser cache directories in priority order."""
    dirs: list[Path] = []
    custom_cache = env.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if custom_cache and custom_cache != "0":
        dirs.append(Path(custom_cache).expanduser())
    home = Path.home()
    dirs.append(home / "Library" / "Caches" / "ms-playwright")
    dirs.append(home / ".cache" / "ms-playwright")
    return dirs


def _playwright_expected_chromium_revision() -> str | None:
    """Read Playwright's expected Chromium revision from its bundled ``browsers.json``."""
    try:
        import json

        import playwright

        browsers_json = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
        if not browsers_json.is_file():
            return None
        payload = json.loads(browsers_json.read_text(encoding="utf-8"))
        for browser in payload.get("browsers", []):
            if browser.get("name") == "chromium" and browser.get("revision"):
                return str(browser["revision"])
    except Exception:
        return None
    return None


def _find_chromium_in_dir(chrom_dir: Path) -> Path | None:
    for rel in _CHROMIUM_RELATIVE_CANDIDATES:
        candidate = chrom_dir / rel
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def _find_cached_chromium(cache_dirs: list[Path]) -> Path | None:
    """Resolve Playwright's exact bundled Chromium revision first, then highest cached build."""
    expected_rev = _playwright_expected_chromium_revision()
    if expected_rev:
        for cache_dir in cache_dirs:
            exact_dir = cache_dir / f"chromium-{expected_rev}"
            if exact_dir.is_dir():
                found = _find_chromium_in_dir(exact_dir)
                if found is not None:
                    return found

    for cache_dir in cache_dirs:
        if not cache_dir.is_dir():
            continue
        chromium_dirs = sorted(
            (p for p in cache_dir.glob("chromium-*") if p.is_dir()),
            key=_chromium_revision_key,
            reverse=True,
        )
        for chrom_dir in chromium_dirs:
            found = _find_chromium_in_dir(chrom_dir)
            if found is not None:
                return found
    return None


def resolve_browser_executable(
    env: Mapping[str, str] | None = None,
    *,
    strict_env: bool = True,
) -> Path | None:
    """Resolve Chromium executable path: ``PEA_BROWSER`` -> cached Playwright Chromium.

    When ``PEA_BROWSER`` is explicitly configured and ``strict_env=True``, raises
    ``FileNotFoundError`` if the configured binary does not exist or is not executable.
    """
    source_env = os.environ if env is None else env
    explicit = source_env.get("PEA_BROWSER", "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return path
        if strict_env:
            raise FileNotFoundError(f"PEA_BROWSER executable not found or not executable: {path}")
        return None

    return _find_cached_chromium(_playwright_cache_dirs(source_env))


@dataclass(frozen=True)
class PeaConfig:
    """Immutable runtime configuration for PEA v2."""

    project_root: Path
    local_assets_dir: Path
    runs_dir: Path
    profiles_dir: Path
    loopback_host: str = "127.0.0.1"
    viewer_port: int = 5190
    allowed_origins: tuple[str, ...] = ()
    ocr_backend: OcrBackendName = "rapidocr"
    ocr_threads: int = 4
    ocr_timeout_s: float = 10.0
    browser_executable: Path | None = None

    def __post_init__(self) -> None:
        if not is_loopback_host(self.loopback_host):
            raise ValueError(
                f"loopback_host must be a loopback address (127.0.0.1, localhost, ::1); "
                f"got {self.loopback_host!r}"
            )
        if not (1 <= self.viewer_port <= 65535):
            raise ValueError(f"viewer_port must be in 1..65535; got {self.viewer_port}")
        if self.ocr_backend not in ("rapidocr", "macos_vision"):
            raise ValueError(f"Unsupported ocr_backend: {self.ocr_backend!r}")
        if self.ocr_threads < 1:
            raise ValueError(f"ocr_threads must be >= 1; got {self.ocr_threads}")
        if self.ocr_timeout_s <= 0:
            raise ValueError(f"ocr_timeout_s must be > 0; got {self.ocr_timeout_s}")

        normalized = tuple(normalize_origin(origin) for origin in self.allowed_origins)
        object.__setattr__(self, "allowed_origins", normalized)

    @property
    def viewer_origin(self) -> str:
        """Canonical loopback origin URL for the local Starlette viewer."""
        host = f"[{self.loopback_host}]" if ":" in self.loopback_host else self.loopback_host
        return f"http://{host}:{self.viewer_port}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PeaConfig:
        """Build a ``PeaConfig`` from environment variables with safe defaults."""
        source_env = os.environ if env is None else env

        root_override = source_env.get("PEA_PROJECT_ROOT", "").strip()
        if root_override:
            project_root = Path(root_override).expanduser().resolve()
        else:
            project_root = _discover_project_root()

        def _path_from_env(var_name: str, default: Path) -> Path:
            raw = source_env.get(var_name, "").strip()
            if not raw:
                return default
            return Path(raw).expanduser().resolve()

        raw_assets_override = source_env.get("PEA_LOCAL_ASSETS_DIR", "").strip()
        if raw_assets_override:
            local_assets_dir = Path(raw_assets_override).expanduser().resolve()
        elif (project_root / "local_assets").is_dir():
            local_assets_dir = project_root / "local_assets"
        elif root_override or (project_root / "pyproject.toml").is_file():
            local_assets_dir = project_root / "local_assets"
        else:
            local_assets_dir = default_cache_dir(source_env) / "local_assets"
        runs_dir = _path_from_env("PEA_RUNS_DIR", project_root / "runs")
        profiles_dir = _path_from_env("PEA_PROFILES_DIR", project_root / "profiles")

        loopback_host = source_env.get("PEA_LOOPBACK_HOST", "127.0.0.1").strip() or "127.0.0.1"
        viewer_port = int(source_env.get("PEA_VIEWER_PORT", "5190").strip() or "5190")
        allowed_origins = parse_allowed_origins(source_env.get("PEA_ALLOWED_ORIGINS"))

        ocr_backend_raw = source_env.get("PEA_OCR_BACKEND", "rapidocr").strip() or "rapidocr"
        if ocr_backend_raw not in ("rapidocr", "macos_vision"):
            raise ValueError(f"Invalid PEA_OCR_BACKEND: {ocr_backend_raw!r}")
        ocr_backend: OcrBackendName = ocr_backend_raw  # type: ignore[assignment]

        ocr_threads = int(source_env.get("PEA_OCR_THREADS", "4").strip() or "4")
        ocr_timeout_s = float(source_env.get("PEA_OCR_TIMEOUT_S", "10.0").strip() or "10.0")
        browser_executable = resolve_browser_executable(source_env)

        return cls(
            project_root=project_root,
            local_assets_dir=local_assets_dir,
            runs_dir=runs_dir,
            profiles_dir=profiles_dir,
            loopback_host=loopback_host,
            viewer_port=viewer_port,
            allowed_origins=allowed_origins,
            ocr_backend=ocr_backend,
            ocr_threads=ocr_threads,
            ocr_timeout_s=ocr_timeout_s,
            browser_executable=browser_executable,
        )


def resolve_confined_path(base: Path, target: str | Path) -> Path:
    """Resolve *target* strictly within *base*, blocking ``..`` traversal and symlink escapes.

    Raises ``PathConfinementError`` (a ``ValueError`` subclass) if *target* is empty,
    contains null bytes, or resolves outside *base*.
    """
    target_str = os.fspath(target)
    if not target_str or "\x00" in target_str:
        raise PathConfinementError(f"Invalid target path: {target!r}")

    base_resolved = Path(base).expanduser().resolve(strict=False)
    target_path = Path(target_str)

    candidate = target_path if target_path.is_absolute() else (base_resolved / target_path)
    resolved = candidate.resolve(strict=False)

    if not resolved.is_relative_to(base_resolved):
        raise PathConfinementError(
            f"Path {target!r} resolves to {resolved}, which escapes confined base {base_resolved}"
        )

    return resolved


def is_origin_allowed(url: str, loopback_origin: str, config: PeaConfig) -> bool:
    """Return True iff *url* matches *loopback_origin* or ``config.allowed_origins``.

    Rejects non-HTTP(S) schemes, userinfo credentials, malformed URLs, non-loopback
    ``loopback_origin`` arguments, and subdomain/port/scheme spoofing.
    """
    try:
        target_origin = _parse_canonical_origin(url, require_origin_only=False)
    except ValueError:
        return False

    try:
        lb_scheme, lb_host, lb_port = _parse_canonical_origin(
            loopback_origin, require_origin_only=True
        )
    except ValueError:
        return False

    if not is_loopback_host(lb_host):
        return False

    if target_origin == (lb_scheme, lb_host, lb_port):
        return True

    for allowed in config.allowed_origins:
        try:
            allowed_tuple = _parse_canonical_origin(allowed, require_origin_only=True)
        except ValueError:
            continue
        if target_origin == allowed_tuple:
            return True

    return False


__all__ = [
    "LOOPBACK_HOSTS",
    "OcrBackendName",
    "PathConfinementError",
    "PeaConfig",
    "default_cache_dir",
    "is_loopback_host",
    "is_origin_allowed",
    "normalize_origin",
    "parse_allowed_origins",
    "resolve_browser_executable",
    "resolve_confined_path",
]
