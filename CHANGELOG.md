# Changelog

All notable changes to **PEA (Physics Experiments Automation)** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.3.0] — 2026-11-24

### Added

- **Timed Transient & Stopwatch Capture (`sim_watch` — MCP tool `#17`):**
  - Drift-compensated monotonic-deadline frame capture supporting `burst_post_ocr` (high-rate PNG capture with post-run offline OCR) and `inline_ocr` (live threshold and stability stopping).
  - Supports `stop_threshold` (`le`, `lt`, `ge`, `gt`), `stop_when_stable_s` (resetting stability anchor on missing OCR frames), cooperative cancellation, mid-burst buffer flush on interrupt/failure, and automatic `simulator_captured` dataset export with SHA-256 provenance.
- **Sandboxed Numerical / ODE / FFT Engine (`compute_sim` — MCP tool `#18`):**
  - Strictly zero-`eval`/`exec` AST-sandboxed numerical solver with depth, length, exponent, `max_nfev`, and monotonic `timeout_s` resource guards.
  - Supports 1D/N-D ODE IVP integration (`RK45`, `Radau`, `BDF`), vectorized `algebraic_grid` evaluation with lazy-branch `where(cond, a, b)`, and one-sided Hann-windowed `fft_spectrum` with coherent gain compensation, exact even/odd Nyquist bin scaling, and automatic uniform resampling when sampling jitter exceeds `1%`.
  - All outputs are explicitly tagged `model_computed` and persisted immutably under `datasets/`.
- **HTML5 & URL SHA-256 Fingerprinting (`compute_html5_dir_sha256`, `compute_url_sha256`):**
  - Deterministic content/path SHA-256 hashing for local HTML5 directories (with symlink confinement checks) and canonical URLs, enabling automatic `CalibrationProfile` save/reload across non-SWF simulators.
- **Local Viewer (`pea-viewer`) Datasets & Model Outputs View:**
  - Enriched `/api/runs/{run_id}` with confined dataset metadata and added a compact **Datasets & Model Outputs** table displaying `simulator_captured`, `bench_manual`, and `model_computed` provenance pills, row counts, columns, notes, and JSON download links.
- **Self-Contained Open-Source Live HTML5 Benchmark:**
  - Added deterministic offline HTML5 RC discharge + damped RLC oscillator fixture (`tests/fixtures/html5_rc_sim/`) and live E2E benchmark (`tests/e2e/test_transient_m2_e2e.py`).
- **Open-Source Publication & CI/CD Polish:**
  - Added `LICENSE` (MIT), `THIRD_PARTY_NOTICES.md`, `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`, GitHub issue/PR templates, and GitHub Actions CI & release workflows (`.github/workflows/ci.yml`, `.github/workflows/release.yml`).
  - Added fresh-clone portable synthetic SWF, OCR, and PDF intake tests supporting Python `3.12` and `3.13`.

### Changed

- Expanded the universal MCP tool surface to **18 tools** + `pea://policy` resource and bumped `.mcpb` Claude Desktop extension packaging to `v0.3.0`.
- Hardened `data_ingest` with atomic `os.link` publication to preserve provenance immutability under concurrent dataset creation.
- Hardened `SimulatorManager.open` with orphan-run workspace cleanup when an auto-created run fails before establishing a live session.
- Organized historical phase/milestone planning documents under `docs/plans/`.

---

## [0.2.0] — 2026-11-22

### Added

- **Universal MCP Server (`pea-mcp`):** 16 typed domain tools + read-only `pea://policy` resource with strict `extra="forbid"` (`additionalProperties: false`) schemas and zero arbitrary shell/filesystem tools.
- **Pluggable Offline OCR & Universal Simulator Engine (`src/pea/ocr/`, `src/pea/sim/`):**
  - Pinned offline RapidOCR PP-OCRv5 Mobile ONNX models (`det_rec` + `rec_only` modes with stateful `use_det` mutation guard), EN/RU homoglyph normalization, and optional timeout-guarded macOS Vision helper.
  - Loopback-only `SimRuntimeServer` with anti-DNS-rebinding `Host` guard, Ruffle `0.7.1` sandbox (`allowScriptAccess: false`, `allowNetworking: "none"`), SHA-256 `CalibrationProfile` store, boundary-safe `step_interior`/`enter`/`blur` stepper planning, N-D `sim_sweep`, and closed-loop `sim_search_extremum` (`max`, `min`, wide `plateau_max`, `threshold`).
- **Dimensional Physics Math & Vector Visuals (`src/pea/math_engine/`, `src/pea/visuals/`):**
  - Safe 5-dim SI dimensional calculator (`calc_eval`) supporting complex AC phasors, 3D vectors, and uncertainty propagation.
  - Safe AST dataset transforms and free/zero-intercept OLS regressions (`data_ingest`, `data_analyze`).
  - Headless Matplotlib vector `.svg` + 300 DPI `.png` plots (`plot_render`) and `schemdraw` circuit schematics / parametric phasor/waveform/field SVGs (`diagram_render`) using bundled Noto Sans fonts.
- **Offline PDF Intake & Academic Report Compiler (`src/pea/pdf_intake.py`, `src/pea/report/`):**
  - Offline PDF page rendering (`pypdfium2`) + text extraction (`pypdf`), HTML+KaTeX `0.16.21` → A4 PDF (Playwright Chromium), editable `.docx` (`python-docx`), `evidence.zip`, and Teacher Evidence PDF with mandatory visual page QA (`report_preview`).
- **Read-Only Local Viewer (`pea-viewer`), Companion Skill & `.mcpb` Packaging:**
  - Loopback Starlette viewer with responsive desktop/mobile layout, interactive OCR box overlay toggle, and live SHA-256 provenance verification.
  - Companion 8-stage agent skill (`skills/pea-lab-assistant/SKILL.md`) and audited zero-binary Claude Desktop `.mcpb` builder (`scripts/build_mcpb.py`).
  - Verified live dual-lab E2E benchmarks (Lab 4 magnetic field wire/loop + Lab 5 forced RLC resonance plateau).

---

## [0.1.0] — 2026-11-21

### Added

- Clean-slate PEA v2 repository architecture (`src/pea/`), `pyproject.toml` (`hatchling`, Python `3.12–3.13`), and strict `.gitignore` excluding local assets, run artifacts, and third-party binaries.
- Frozen core contracts: `src/pea/config.py` (path confinement & loopback validation), `src/pea/schemas.py` (frozen Pydantic v2 schemas), and `src/pea/workspace.py` (run workspace lifecycle & atomic SHA-256 provenance ledger).
- Idempotent SHA-256-pinned offline asset bootstrapper (`scripts/fetch_assets.py`) for KaTeX `0.16.21`, Ruffle `0.7.1`, Noto Sans, and RapidOCR PP-OCRv5 Mobile ONNX models.
