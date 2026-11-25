# Contributing to PEA

Thank you for your interest in contributing to **PEA (Physics Experiments Automation)**!
PEA is a universal, lab-agnostic, zero-API-cost physics laboratory automation toolkit.

---

## Development Setup

PEA supports **Python `3.12` and `3.13`** (`requires-python = ">=3.12,<3.14"`) on macOS, Linux, and Windows.

### macOS / Linux

```sh
# 1. Create a virtual environment
uv venv .venv --python 3.13
# (or: python3 -m venv .venv)

# 2. Install PEA in editable mode with dev dependencies
uv pip install --python .venv/bin/python -e ".[dev]"

# 3. Install Playwright Chromium
.venv/bin/playwright install chromium

# 4. Fetch & SHA-256-verify pinned offline assets (KaTeX, RapidOCR ONNX, Ruffle 0.7.1, Noto Sans)
.venv/bin/python scripts/fetch_assets.py --fetch
.venv/bin/python scripts/fetch_assets.py --check
```

### Windows (PowerShell)

```powershell
uv venv .venv --python 3.13
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.venv\Scripts\playwright.exe install chromium
.venv\Scripts\python.exe scripts\fetch_assets.py --fetch
.venv\Scripts\python.exe scripts\fetch_assets.py --check
```

---

## Running Checks & Tests

Run all required checks before opening a pull request:

```sh
# Lint (zero errors required)
.venv/bin/ruff check src/ tests/ scripts/

# Offline test suite (runs without private SWFs or university handouts)
.venv/bin/pytest tests/ -m "not live_sim" -v

# Self-contained live HTML5 RC/RLC transient E2E benchmark
.venv/bin/pytest tests/e2e/test_transient_m2_e2e.py -m live_sim -v

# Claude Desktop .mcpb manifest parity & security audit
.venv/bin/python scripts/build_mcpb.py --check
```

---

## Contributing Calibration Profiles or Simulator Fixtures

### New `CalibrationProfile`s

- Calibration profiles are keyed by the **64-character SHA-256** of the simulator (`.swf` file bytes, local HTML5 directory content/path hash, or canonical URL hash).
- Never commit third-party copyrighted `.swf` binaries, `.pdf` handouts, or student-identifying screenshots into the repository.
- Shared calibration profiles (if contributed) must contain only geometry, control specs, readout ROIs, and unit patterns — zero literal home paths or private filenames.

### Self-Contained HTML5 Simulator Fixtures

- Reference [`tests/fixtures/html5_rc_sim/index.html`](./tests/fixtures/html5_rc_sim/index.html) as a template for deterministic offline HTML5 physics fixtures.
- Fixtures must:
  - Make **zero external network requests** (no CDN fonts/scripts).
  - Render high-contrast dark text on clean light panels with explicit SI units so offline RapidOCR reads values and negative signs accurately.
  - Keep fixed viewport geometry (`margin: 0; overflow: hidden`) so ROI coordinates remain deterministic across platforms.

---

## Design Principles & PR Checklist

Before submitting a PR, verify:

- [ ] **Zero API Cost:** No external LLM SDKs or API keys introduced.
- [ ] **Lab-Agnostic Core:** No hardcoded lab numbers, course names, or simulator-specific magic constants inside `src/pea/`.
- [ ] **Strict Provenance Separation:** `simulator_captured`, `bench_manual`, and `model_computed` sources remain strictly separated; missing simulator readings are never back-filled from formulas.
- [ ] **Security Boundaries Preserved:** Loopback-only defaults, path confinement, AST sandbox (zero `eval`/`exec`), and `extra="forbid"` Pydantic schemas remain intact.
- [ ] **Hygiene:** Zero forbidden binaries (`.swf`, `.pdf`, `.docx`, `.zip`, `.onnx`, `.wasm`, `.mcpb`), zero secrets, and zero literal host home paths in tracked files.
- [ ] **Checks Green:** `ruff check src/ tests/ scripts/`, `pytest tests/ -m "not live_sim" -v`, `pytest tests/e2e/test_transient_m2_e2e.py -m live_sim -v`, and `python scripts/build_mcpb.py --check` all pass.
