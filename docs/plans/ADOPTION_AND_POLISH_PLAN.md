# Adoption, Onboarding & Release Polish Plan — PEA v2 (`v0.3.1`)

> **Status:** `Complete`
> **Baseline:** Post-`v0.3.0` on `main` (`aca1453`, including cross-platform ZWS SWF `FORMAT_RAW` fix, live HTML5 E2E hardening, and CI workflow fixes; `18` MCP tools; `pea://policy` resource).

---

## 1. External Review Feedback Addressed

This release addresses concrete onboarding, framing, and packaging gaps surfaced during an external review of the repository landing page and `README.md`:

1. **Plain-English Pitch & Flash/HTML5 Hook:** Open `README.md` with what a user gets in one sentence—pointing an AI agent at a legacy Flash (`.swf`) or HTML5 physics lab simulator to obtain a SHA-256-verified dataset, deterministic math, vector plots, and an auditable report draft—before introducing MCP architecture internals.
2. **Animated Demo GIF (`docs/images/demo-run.gif`):** Add a compact, self-contained animated walkthrough generated from the open-source HTML5 RC/RLC benchmark right below the pitch.
3. **One-Command Demo (`pea demo`):** Replace the `pytest` invocation in the 60-second demo with `pea demo`, which runs the self-contained RC/RLC pipeline over real MCP stdio and launches `pea-viewer`.
4. **Simplified Setup & PyPI / `uvx` Readiness (`pea setup` + User Cache Decoupling):** Decouple `PeaConfig` and `pea.assets` from requiring a git checkout (`pyproject.toml`), bundle the open-source HTML5 demo fixture inside `pea.demo_assets`, and add a unified `pea` CLI (`pea setup`, `pea demo`, `pea viewer`, `pea mcp`) so `uvx pea-mcp setup` and `uvx pea-mcp` work cleanly once published to PyPI.
5. **Accurate SHA-256 Integrity Framing:** Describe `provenance.jsonl` accurately as a **SHA-256 artifact integrity manifest** for detecting accidental corruption, stale artifacts, and drift—not cryptographic tamper-proofing against a local disk owner.
6. **Academic Integrity & Intended-Use Framing:** Explicitly frame PEA as reproducible data acquisition, auditable calculations, and instructor-verifiable evidence—not homework automation—and clarify the role of the Instructor Verification Packet (`teacher-evidence.pdf` + `evidence.zip`).
7. **Release Hygiene (`v0.3.1`) & Remote GitHub Checklist:** Bump version to `0.3.1` across package, `.mcpb`, and tests; document manual GitHub steps to fix the `labratory` topic typo and publish `v0.3.1` on GitHub Releases + PyPI.

---

## 2. Workstreams

### Workstream 1 — Decouple Assets & Config for PyPI / `uvx` Readiness
1. Move asset bootstrapper logic from `scripts/fetch_assets.py` into `src/pea/assets.py` and keep `scripts/fetch_assets.py` as a backward-compatible wrapper (updating `.github/workflows/ci.yml` cache keys to hash both files).
2. Add `default_cache_dir()` to `src/pea/config.py`, restrict `_discover_project_root()` to actual PEA source checkouts (`pyproject.toml` + `src/pea/`), and fall back to `default_cache_dir() / "local_assets"` when outside a repo checkout.
3. Package the open-source HTML5 RC/RLC fixture inside `src/pea/demo_assets/html5_rc_sim/index.html`.

### Workstream 2 — Unified `pea` CLI (`pea demo`, `pea setup`, `pea viewer`, `pea mcp`) & `v0.3.1` Bump
1. Add `--runs-dir` CLI option to `src/pea/viewer/app.py`.
2. Create `src/pea/demo.py` executing the self-contained HTML5 RC/RLC pipeline via `pea.mcp_server` over stdio with friendly preflight checks and optional `pea-viewer` launch.
3. Create `src/pea/cli.py` unifying `pea setup`, `pea demo`, `pea viewer`, and `pea mcp`.
4. Bump version `0.3.0` → `0.3.1` in `pyproject.toml`, `src/pea/__init__.py`, `packaging/mcpb/manifest.json`, and `tests/test_phase1_core.py`, and register `pea = "pea.cli:main"`.

### Workstream 3 — Animated Demo GIF (`docs/images/demo-run.gif`)
1. Run `pea demo --out runs/demo --no-viewer` to generate fresh open-source artifacts.
2. Render a lightweight (`< 1.5 MB`) quantized Pillow GIF (`960×560`) showing `sim_observe` grid calibration, live `sim_watch` RC discharge frames, `pea-viewer` Datasets provenance badges, and compiled ODE/FFT plots.

### Workstream 4 — Documentation, Viewer & Skill Polish
1. Rewrite `README.md` top pitch, embed `docs/images/demo-run.gif`, streamline Quick Start with `pea setup`, replace `pytest` demo with `pea demo`, add **Academic Integrity & Intended Use**, and refine SHA-256 integrity wording.
2. Update `SECURITY.md`, `skills/pea-lab-assistant/SKILL.md`, `src/pea/viewer/static/index.html` (`SHA-256 Artifact Integrity Manifest`), `src/pea/viewer/static/app.js` (`Instructor Audit PDF`), `CONTRIBUTING.md`, and `CHANGELOG.md` (`0.3.1`).

### Workstream 5 — Unit Tests & Full Verification
1. Add `tests/test_cli_and_packaging.py` covering `pea --version`, `pea setup --check`, `PeaConfig` checkout vs user-cache resolution, `demo_assets` fixture parity, `pea demo` preflight errors, and `pea-viewer --runs-dir`.
2. Run full verification gates (`ruff`, `fetch_assets.py --check`, `build_mcpb.py`, `node --check`, offline `pytest`, `pea demo`, live HTML5 E2E, and CI hygiene check).

---

## 3. Verification Gates

- [x] `.venv/bin/ruff check src/ tests/ scripts/` — zero errors
- [x] `.venv/bin/python scripts/fetch_assets.py --check` — all 37 pinned offline assets verified (`katex, ocr, ruffle, fonts`)
- [x] `.venv/bin/python scripts/build_mcpb.py --check && .venv/bin/python scripts/build_mcpb.py` — audited `dist/pea-mcp-0.3.1.mcpb` (`10222` bytes)
- [x] `node --check packaging/mcpb/launcher.mjs` — clean
- [x] `.venv/bin/pytest tests/ -m "not live_sim" -v` — `219 passed, 3 deselected` (`65.59s`)
- [x] `.venv/bin/pea demo --out runs/demo --no-viewer` — live HTML5 demo succeeded & passed SHA-256 verification (`113` verified artifacts)
- [x] `.venv/bin/pytest tests/e2e/test_transient_m2_e2e.py -m live_sim -v` — `1 passed` (`22.60s`)
- [x] Repository hygiene check — `91` tracked + new files audited (`0` violations)

---

## 4. Manual Remote Checklist (After Local Execution)

These actions require remote GitHub / PyPI credentials and are intentionally left for the maintainer:

1. **Fix GitHub Topic Typo:** In GitHub repo **About → Topics**, replace `labratory` with `laboratory`.
2. **Push & Tag `v0.3.1` to Publish GitHub Release:**
   ```sh
   git push origin main
   git tag v0.3.1
   git push origin v0.3.1
   ```
   Confirm `.github/workflows/release.yml` publishes `pea-mcp-0.3.1.mcpb` and `SHA256SUMS.txt` on the Releases page.
3. **Optional PyPI Publish & Registry Listings:**
   - Build & upload wheel/sdist (`uv build && uv publish`), test `uvx pea-mcp setup` + `uvx pea-mcp`, and submit to MCP registries / `awesome-mcp` lists.

---

## 5. Observed Findings

- **CLI & Packaging (`v0.3.1`):**
  - Reinstalled editable package offline (`uv pip install --python .venv/bin/python --offline --no-deps -e .`); `.venv/bin/pea --version` prints `pea 0.3.1`.
  - `scripts/build_mcpb.py` produced and audited `dist/pea-mcp-0.3.1.mcpb` (`10222` bytes, `sha256=22e1eec4b96003f076832da8cdc835a2db5c4d2b1289f5e8c840fccc535fd9c4`, manifest v0.3, `18` tools, zero binaries/secrets/traversals).
- **Live `pea demo` Run (`2026-11-25T12-06-08-103Z-aa0efa54`):**
  - RC discharge `sim_watch(inline_ocr)` + OLS recovered `tau = 1.001 s` (ref `1.000 s`, dev `0.05%`, `R^2 = 1.0000`).
  - Damped RLC `sim_watch(burst_post_ocr)` + Hann FFT recovered `f_peak = 1.973 Hz` (ref `2.000 Hz`).
  - SHA-256 artifact integrity verification: `OK` across `113` verified artifacts; generated `report.pdf`, `report.docx`, `evidence.zip`, and `teacher-evidence.pdf`.
- **Animated Demo GIF (`docs/images/demo-run.gif`):**
  - Generated `960x560`, `11` frames, `338.0 KiB` purely from the self-contained HTML5 RC/RLC demo run and `viewer-datasets-desktop.png`.
- **Test & Hygiene Gates:**
  - `ruff check src/ tests/ scripts/`: `All checks passed!`.
  - `scripts/fetch_assets.py --check`: `OK: all 37 pinned offline assets verified across groups katex, ocr, ruffle, fonts.`
  - `pytest tests/ -m "not live_sim" -v` (with loopback/Chromium allowed outside sandbox): `219 passed, 3 deselected in 65.59s` (including `7` new tests in `tests/test_cli_and_packaging.py`).
  - `pytest tests/e2e/test_transient_m2_e2e.py -m live_sim -v`: `1 passed in 22.60s`.
  - Repository hygiene audit across `91` tracked + new files: `0` violations.
