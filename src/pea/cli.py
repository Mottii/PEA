"""Unified command-line interface for PEA (`pea setup`, `pea demo`, `pea viewer`, `pea mcp`)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from pea import __version__
from pea.assets import ASSET_GROUPS, check_assets, fetch_assets
from pea.config import PeaConfig, resolve_browser_executable


def _run_setup(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="pea setup",
        description="Fetch and verify pinned offline runtime assets and Playwright Chromium.",
    )
    parser.add_argument(
        "--assets-dir",
        type=Path,
        default=None,
        help="Target directory for offline assets (default: PEA_LOCAL_ASSETS_DIR or user cache).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify offline assets and Chromium without downloading anything.",
    )
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="Skip Playwright Chromium check/installation.",
    )
    parser.add_argument(
        "--with-deps",
        action="store_true",
        help="Pass --with-deps to `playwright install chromium` (useful on Linux).",
    )
    args = parser.parse_args(argv)

    cfg = PeaConfig.from_env()
    assets_dir = (
        args.assets_dir.expanduser().resolve()
        if args.assets_dir is not None
        else cfg.local_assets_dir
    )

    if not args.check:
        fetch_assets(assets_dir, ASSET_GROUPS)

    errors = check_assets(assets_dir, ASSET_GROUPS, require_manifest=True)
    if errors:
        print(f"Asset verification FAILED ({len(errors)} issue(s)):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"[ok] Offline assets verified at {assets_dir}")

    if not args.skip_browser:
        browser_exe = resolve_browser_executable()
        if browser_exe is None:
            if args.check:
                print(
                    "Browser check FAILED: Playwright Chromium not found. "
                    "Run `pea setup` (or `playwright install chromium`).",
                    file=sys.stderr,
                )
                return 1
            cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
            if args.with_deps:
                cmd.append("--with-deps")
            print("[fetch] Installing Playwright Chromium...")
            proc = subprocess.run(cmd, check=False)
            if proc.returncode != 0:
                print(
                    f"Playwright Chromium install failed with exit code {proc.returncode}.",
                    file=sys.stderr,
                )
                return proc.returncode
            browser_exe = resolve_browser_executable()
        if browser_exe is not None:
            print(f"[ok] Chromium ready: {browser_exe}")

    if not args.check:
        print("\nSetup complete. Next steps:")
        print("  pea demo     # Run the 60s self-contained RC/RLC demo and open pea-viewer")
        print("  pea mcp      # Start the MCP stdio server")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the unified ``pea`` CLI."""
    parser = argparse.ArgumentParser(
        prog="pea",
        description=(
            "PEA (Physics Experiments Automation) — zero-API-cost deterministic physics lab "
            "automation over Flash/Ruffle and HTML5 simulators."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"pea {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.add_parser(
        "setup",
        add_help=False,
        help="Download and verify pinned offline assets and Playwright Chromium.",
    )
    subparsers.add_parser(
        "demo",
        add_help=False,
        help="Run the self-contained HTML5 RC/RLC transient demo and open pea-viewer.",
    )
    subparsers.add_parser(
        "viewer",
        add_help=False,
        help="Start the read-only loopback local run viewer (pea-viewer).",
    )
    subparsers.add_parser(
        "mcp",
        add_help=False,
        help="Start the MCP stdio server (pea-mcp).",
    )

    args, remaining = parser.parse_known_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "setup":
        return _run_setup(remaining)
    if args.command == "demo":
        from pea.demo import main as demo_main

        return demo_main(remaining)
    if args.command == "viewer":
        from pea.viewer.app import main as viewer_main

        return viewer_main(remaining)
    if args.command == "mcp":
        from pea.mcp_server import main as mcp_main

        return mcp_main(remaining)
    parser.print_help()
    return 2


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
