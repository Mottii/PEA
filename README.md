# PEA v2 — Physics Experiments Automation

**PEA (Physics Experiments Automation)** is a universal, **zero-API-cost** deterministic local toolkit exposed via an **MCP server** (`pea-mcp`), companion **Agent Skill**, **Claude Desktop extension (`.mcpb`)**, and lightweight **loopback local viewer** (`pea-viewer`).

PEA makes **zero LLM API calls** and requires **no API keys**. Any connecting host agent (Claude Desktop, Antigravity, Cursor, Codex, etc.) supplies multimodal PDF/diagram interpretation, planning, control calibration, and theory prose, while PEA executes deterministic simulator interaction, ONNX OCR, dimensional physics calculations, statistical fits, vector plots/diagrams, and submission-ready report compilation with SHA-256 provenance tracking.

## Security & Scientific Integrity Defaults

- **Loopback-only by default (`127.0.0.1`):** Local SWF and HTML5 simulators run on loopback with Ruffle `allowScriptAccess: false` and `allowNetworking: "none"`. External simulator origins are blocked unless explicitly listed in `PEA_ALLOWED_ORIGINS`.
- **Path confinement:** All file operations are strictly confined to project/run roots; traversal (`..`) and symlink escapes are rejected.
- **Provenance separation:** Measurements explicitly distinguish `simulator_captured`, `bench_manual`, and `model_computed` sources; partial evidence is preserved on interruption or cancellation.

## Local Setup (Python 3.13 + `uv`)

> **Runtime requirement:** Use Python `3.12` or `3.13` (`requires-python = ">=3.12,<3.14"`). Python `3.14` currently lacks arm64 macOS binary wheels for `onnxruntime`.

```sh
# 1. Create the virtual environment pinned to uv-managed Python 3.13
uv venv .venv --python "$HOME/.local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13"

# 2. Install PEA v2 in editable mode with dev tooling
uv pip install --python .venv/bin/python -e ".[dev]"

# 3. Fetch / verify pinned offline assets (KaTeX, RapidOCR ONNX models, Ruffle 0.7.1, Noto Sans)
.venv/bin/python scripts/fetch_assets.py --fetch
.venv/bin/python scripts/fetch_assets.py --check

# 4. Run tests and static lint
.venv/bin/pytest -v
.venv/bin/ruff check src/ tests/ scripts/
```

## Planning & Architecture Documents

- [`PLAN_V2.md`](./PLAN_V2.md) — Master architecture blueprint, locked design decisions, checklist, and progress log.
- [`PHASE_0_PLAN.md`](./PHASE_0_PLAN.md) — Day-0 non-destructive Ruffle `0.7.1` & RapidOCR ONNX verification findings.
- [`PHASE_1_PLAN.md`](./PHASE_1_PLAN.md) — Clean-slate migration, Python `3.13` environment, offline asset pinning, and frozen core contracts.
