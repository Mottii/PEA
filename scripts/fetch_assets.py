#!/usr/bin/env python3
"""Compatibility CLI wrapper around ``pea.assets`` for repository scripts and CI.

Usage:
    .venv/bin/python scripts/fetch_assets.py --fetch
    .venv/bin/python scripts/fetch_assets.py --check
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pea.assets import (  # noqa: E402
    ASSET_GROUPS,
    FONT_ASSETS,
    FONTS_VERSION,
    KATEX_SPEC,
    OCR_ASSETS,
    OCR_VERSION,
    RUFFLE_SPEC,
    DirectAsset,
    TarballSpec,
    build_manifest,
    check_assets,
    expected_files_for_group,
    fetch_assets,
    sha256_bytes,
    sha256_file,
    write_bytes_atomic,
)
from pea.assets import main as _assets_main  # noqa: E402


def _default_assets_dir() -> Path:
    return REPO_ROOT / "local_assets"


def main(argv: list[str] | None = None) -> int:
    return _assets_main(argv, default_dir=_default_assets_dir())


__all__ = [
    "ASSET_GROUPS",
    "FONTS_VERSION",
    "FONT_ASSETS",
    "KATEX_SPEC",
    "OCR_ASSETS",
    "OCR_VERSION",
    "RUFFLE_SPEC",
    "DirectAsset",
    "TarballSpec",
    "build_manifest",
    "check_assets",
    "expected_files_for_group",
    "fetch_assets",
    "main",
    "sha256_bytes",
    "sha256_file",
    "write_bytes_atomic",
]


if __name__ == "__main__":
    raise SystemExit(main())
