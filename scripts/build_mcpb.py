#!/usr/bin/env python3
"""Build and security-audit the unsigned Claude Desktop Extension (`.mcpb`) bundle for PEA v2.

Produces a minimal, deterministic, zero-binary ZIP archive (`dist/pea-mcp-<version>.mcpb`)
containing `manifest.json`, `launcher.mjs`, and companion documentation, and verifies:
  - Zero path traversal, absolute paths, drive prefixes, backslashes, or symlinks
  - Zero SWFs, PDFs, DOCX, ONNX models, WASM bundles, fonts, binaries, or secrets
  - Zero literal host home paths (macOS, Linux, or Windows user profile prefixes)
  - Exact MCPB v0.3 manifest schema and 16-tool parity with `pea.mcp_server`

Usage:
    .venv/bin/python scripts/build_mcpb.py
    .venv/bin/python scripts/build_mcpb.py --check
    .venv/bin/python scripts/build_mcpb.py --audit dist/pea-mcp-0.2.0.mcpb
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

PACKAGING_DIR = REPO_ROOT / "packaging" / "mcpb"
DEFAULT_DIST_DIR = REPO_ROOT / "dist"

# Fixed DOS timestamp for reproducible builds (2026-01-01 00:00:00)
_REPRODUCIBLE_ZIP_DATE: tuple[int, int, int, int, int, int] = (2026, 1, 1, 0, 0, 0)
_MAX_UNCOMPRESSED_BYTES = 512 * 1024

_FORBIDDEN_SUFFIXES: frozenset[str] = frozenset(
    {
        ".swf",
        ".pdf",
        ".docx",
        ".onnx",
        ".wasm",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".zip",
        ".mcpb",
        ".tar",
        ".gz",
        ".tgz",
        ".env",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".sqlite",
        ".db",
    }
)

_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    ".venv/",
    "local_assets/",
    "runs/",
    "dist/",
    ".git/",
    "__pycache__/",
    "node_modules/",
    "profiles/local/",
)

_MACOS_HOME_PREFIX = "/" + "Users/"
_WIN_HOME_PREFIX = r"\\" + "Users" + r"\\"

_SECRET_AND_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("literal macOS home path", re.compile(_MACOS_HOME_PREFIX + r"[A-Za-z0-9._-]+")),
    ("literal Linux home path", re.compile(r"/home/[A-Za-z0-9._-]+")),
    ("literal Windows home path", re.compile(r"[A-Za-z]:" + _WIN_HOME_PREFIX + r"[A-Za-z0-9._-]+")),
    ("OpenAI/Anthropic-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key PEM block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
)


def _project_version(repo_root: Path = REPO_ROOT) -> str:
    pyproject_path = repo_root / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Missing [project].version in pyproject.toml")
    return version.strip()


def _expected_tool_names() -> tuple[str, ...]:
    from pea.mcp_server import UNIVERSAL_TOOL_NAMES

    return UNIVERSAL_TOOL_NAMES


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65_536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_sources(repo_root: Path = REPO_ROOT) -> list[tuple[str, Path, int]]:
    """Return ``(arcname, source_path, unix_mode)`` entries to package in `.mcpb`."""
    pkg_dir = repo_root / "packaging" / "mcpb"
    sources: list[tuple[str, Path, int]] = [
        ("manifest.json", pkg_dir / "manifest.json", 0o644),
        ("launcher.mjs", pkg_dir / "launcher.mjs", 0o755),
    ]
    skill_path = repo_root / "skills" / "pea-lab-assistant" / "SKILL.md"
    if skill_path.is_file():
        sources.append(("SKILL.md", skill_path, 0o644))
    return sources


def build_mcpb(
    output_path: Path | None = None,
    *,
    repo_root: Path = REPO_ROOT,
) -> Path:
    """Build the unsigned `.mcpb` archive atomically and audit it before finalizing."""
    version = _project_version(repo_root)
    dest = (
        output_path
        if output_path is not None
        else (repo_root / "dist" / f"pea-mcp-{version}.mcpb")
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(f".{dest.name}.tmp")

    sources = _bundle_sources(repo_root)
    for arcname, src_path, _ in sources:
        if not src_path.is_file():
            raise FileNotFoundError(f"Required MCPB source missing for {arcname}: {src_path}")

    try:
        with zipfile.ZipFile(tmp_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for arcname, src_path, mode in sources:
                data = src_path.read_bytes()
                info = zipfile.ZipInfo(filename=arcname, date_time=_REPRODUCIBLE_ZIP_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3  # Unix permissions
                info.external_attr = (stat.S_IFREG | mode) << 16
                zf.writestr(info, data)

        errors = audit_mcpb_archive(tmp_path, expected_version=version)
        if errors:
            joined = "\n  - ".join(errors)
            raise ValueError(f"MCPB archive failed audit before release:\n  - {joined}")

        os.replace(tmp_path, dest)
        return dest
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _check_text_for_leaks(arcname: str, raw: bytes) -> list[str]:
    errors: list[str] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{arcname}: non-UTF-8 binary payload is forbidden in lightweight .mcpb")
        return errors

    for label, pattern in _SECRET_AND_PATH_PATTERNS:
        if pattern.search(text):
            errors.append(f"{arcname}: contains forbidden {label}")
    return errors


def _validate_manifest_dict(
    manifest: dict[str, Any],
    *,
    member_names: set[str],
    expected_version: str | None,
) -> list[str]:
    errors: list[str] = []

    if manifest.get("manifest_version") != "0.3":
        errors.append(
            "manifest.json: expected manifest_version='0.3', "
            f"got {manifest.get('manifest_version')!r}"
        )
    if manifest.get("name") != "pea-mcp":
        errors.append(f"manifest.json: expected name='pea-mcp', got {manifest.get('name')!r}")
    if expected_version is not None and manifest.get("version") != expected_version:
        errors.append(
            f"manifest.json: version {manifest.get('version')!r} != "
            f"project version {expected_version!r}"
        )

    server = manifest.get("server")
    if not isinstance(server, dict):
        errors.append("manifest.json: missing 'server' object")
    else:
        if server.get("type") != "node":
            errors.append(f"manifest.json: expected server.type='node', got {server.get('type')!r}")
        entry_point = server.get("entry_point")
        if not isinstance(entry_point, str) or entry_point not in member_names:
            errors.append(
                f"manifest.json: server.entry_point {entry_point!r} is missing from archive"
            )
        mcp_config = server.get("mcp_config")
        if not isinstance(mcp_config, dict) or mcp_config.get("command") != "node":
            errors.append("manifest.json: server.mcp_config.command must be 'node'")

    user_cfg = manifest.get("user_config")
    if not isinstance(user_cfg, dict) or not isinstance(user_cfg.get("project_folder"), dict):
        errors.append("manifest.json: missing user_config.project_folder definition")
    else:
        pf = user_cfg["project_folder"]
        if pf.get("type") != "directory" or pf.get("required") is not True:
            errors.append(
                "manifest.json: user_config.project_folder must have "
                "type='directory' and required=true"
            )

    tools = manifest.get("tools")
    if not isinstance(tools, list):
        errors.append("manifest.json: missing 'tools' list")
    else:
        manifest_names = tuple(
            str(item.get("name", "")) for item in tools if isinstance(item, dict)
        )
        expected_names = _expected_tool_names()
        if manifest_names != expected_names:
            errors.append(
                f"manifest.json: tools list mismatch (got {len(manifest_names)}, "
                f"expected {len(expected_names)}: {list(expected_names)})"
            )

    return errors


def audit_mcpb_archive(
    archive_path: Path,
    *,
    expected_version: str | None = None,
) -> list[str]:
    """Audit a `.mcpb` archive for traversal, forbidden binaries, secrets, and manifest parity."""
    errors: list[str] = []
    if not archive_path.is_file():
        return [f"Archive does not exist: {archive_path}"]

    try:
        zf = zipfile.ZipFile(archive_path, mode="r")
    except zipfile.BadZipFile as exc:
        return [f"Invalid ZIP archive: {exc}"]

    with zf:
        infos = zf.infolist()
        if not infos:
            return ["Archive is empty"]

        total_uncompressed = 0
        member_names: set[str] = set()

        for info in infos:
            name = info.filename
            if name in member_names:
                errors.append(f"Duplicate archive entry: {name!r}")
            member_names.add(name)

            # Reject path traversal, absolute paths, drive letters, backslashes, NUL bytes
            if (
                "\x00" in name
                or "\\" in name
                or name.startswith("/")
                or re.match(r"^[A-Za-z]:", name)
                or any(part == ".." for part in name.split("/"))
            ):
                errors.append(f"Unsafe archive entry path: {name!r}")
                continue

            # Reject symlinks stored inside ZIP
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == stat.S_IFLNK:
                errors.append(f"Symlink entry forbidden in archive: {name!r}")
                continue

            if info.is_dir():
                continue

            lowered = name.lower()
            for prefix in _FORBIDDEN_PREFIXES:
                if lowered.startswith(prefix):
                    errors.append(f"Forbidden directory prefix in archive: {name!r}")
                    break

            suffix = Path(lowered).suffix
            if suffix in _FORBIDDEN_SUFFIXES or Path(lowered).name == ".env":
                errors.append(f"Forbidden file type in archive: {name!r}")
                continue

            total_uncompressed += info.file_size
            raw = zf.read(info)
            errors.extend(_check_text_for_leaks(name, raw))

        if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
            errors.append(
                f"Archive uncompressed size ({total_uncompressed} bytes) exceeds "
                f"lightweight limit ({_MAX_UNCOMPRESSED_BYTES} bytes)"
            )

        if "manifest.json" not in member_names:
            errors.append("Required archive entry missing: 'manifest.json'")
        else:
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                if not isinstance(manifest, dict):
                    errors.append("manifest.json: top-level JSON value must be an object")
                else:
                    errors.extend(
                        _validate_manifest_dict(
                            manifest,
                            member_names=member_names,
                            expected_version=expected_version,
                        )
                    )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(f"manifest.json: invalid JSON ({exc})")

        if "launcher.mjs" not in member_names:
            errors.append("Required archive entry missing: 'launcher.mjs'")

    return errors


def check_packaging(*, repo_root: Path = REPO_ROOT) -> list[str]:
    """Validate packaging sources by building into a temp archive and auditing it (plus dist/)."""
    version = _project_version(repo_root)
    errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="pea-mcpb-check-") as tmp_dir:
        tmp_archive = Path(tmp_dir) / f"pea-mcp-{version}.mcpb"
        try:
            build_mcpb(tmp_archive, repo_root=repo_root)
        except Exception as exc:
            errors.append(str(exc))
            return errors
        errors.extend(audit_mcpb_archive(tmp_archive, expected_version=version))

    existing_dist = repo_root / "dist" / f"pea-mcp-{version}.mcpb"
    if existing_dist.is_file():
        dist_errors = audit_mcpb_archive(existing_dist, expected_version=version)
        errors.extend(f"dist/{existing_dist.name}: {err}" for err in dist_errors)

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and security-audit the PEA v2 Claude Desktop (.mcpb) extension bundle."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate packaging sources, manifest parity, and archive audit.",
    )
    mode.add_argument(
        "--audit",
        type=Path,
        default=None,
        help="Audit an existing .mcpb archive path.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .mcpb file path (default: dist/pea-mcp-<version>.mcpb).",
    )
    args = parser.parse_args(argv)

    version = _project_version(REPO_ROOT)

    if args.audit is not None:
        errors = audit_mcpb_archive(args.audit, expected_version=version)
        if errors:
            print(f"MCPB audit FAILED ({len(errors)} issue(s)):", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            return 1
        print(f"OK: audited {args.audit.name} (version {version}, zero policy violations).")
        return 0

    if args.check:
        errors = check_packaging(repo_root=REPO_ROOT)
        if errors:
            print(f"MCPB check FAILED ({len(errors)} issue(s)):", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            return 1
        print(
            f"OK: MCPB packaging v{version} verified "
            f"(manifest v0.3, 16 tools, zero binaries/secrets/traversals)."
        )
        return 0

    dest = build_mcpb(args.output, repo_root=REPO_ROOT)
    digest = _sha256_file(dest)
    size_bytes = dest.stat().st_size
    print(f"OK: built & audited {dest.name} ({size_bytes} bytes, sha256={digest}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
