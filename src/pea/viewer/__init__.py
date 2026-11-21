"""Lightweight loopback-only Starlette local viewer for PEA v2 runs and live captures."""

from __future__ import annotations

from pea.viewer.app import create_viewer_app, main, parse_host_header

__all__ = [
    "create_viewer_app",
    "main",
    "parse_host_header",
]
