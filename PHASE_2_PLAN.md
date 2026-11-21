# Phase 2 Implementation Plan — Core Subsystems (`2A`, `2B`, `2C`, `2D`)

> **Status:** `In Progress — Phase 2A Complete; Ready for Phase 2B`
> **Parent Plan:** [`PLAN_V2.md`](./PLAN_V2.md)
> **Prerequisite:** Phase 1 complete (`e3097c2` on clean `main`; `32/32` tests pass; `37` offline assets verified).

---

## 1. Goal Description

Build all four lab-agnostic core subsystems of **PEA v2** on top of the frozen Phase 1 contracts ([`src/pea/config.py`](./src/pea/config.py), [`src/pea/schemas.py`](./src/pea/schemas.py), [`src/pea/workspace.py`](./src/pea/workspace.py)):

* **Phase 2A:** Pluggable Offline OCR (`RapidOcrBackend` M1 primary + timeout-guarded macOS Vision) & Universal Playwright/Ruffle Simulator Engine (`sim_open`, `sim_observe`, `sim_act`, `sim_sweep`, `sim_search_extremum`, SHA-256 Calibration Profiles).
* **Phase 2B:** Safe Dimensional-Analysis Physics Calculator (`calc_eval`), Tabular Transforms & OLS Regression (`data_ingest`, `data_analyze`), Matplotlib Vector Plots (`plot_render`), and `schemdraw` / Parametric Vector Schematics (`diagram_render`).
* **Phase 2C:** PDF Handout Intake (`lab_inspect_pdf`) & Submission-Ready Report Compiler (`HTML + offline KaTeX + Noto Sans → Playwright A4 PDF`, editable `.docx`, `report_preview`, raw `evidence.zip`, and optional Teacher Evidence Packet PDF).
* **Phase 2D:** `mcp==2.3.0` stdio Server (`pea-mcp`, 16 universal tools + `pea://policy`), Lightweight Starlette Local Viewer (`pea-viewer`), Companion Agent Skill (`skills/pea-lab-assistant/SKILL.md`), and Claude Desktop `.mcpb` Packaging.

---

## 2. Critical API & Runtime Findings (Verified Against `.venv`)

> [!IMPORTANT]
> **MCP SDK `2.3.0` Breaking Rename (`MCPServer`, NOT `FastMCP`):**
> Probing `.venv` caught that `mcp==2.3.0` removed `mcp.server.fastmcp.FastMCP` and renamed it to **`from mcp.server.mcpserver import MCPServer`** (with built-in `ResourceSecurity` traversal guards, `@server.tool(annotations=ToolAnnotations(...))`, `@server.resource(...)`, and `server.run(transport="stdio")`). Phase 2D uses `MCPServer` natively—zero downgrades needed.

> [!IMPORTANT]
> **100% Offline RapidOCR Pinning & Stateful Bug Guard (`rapidocr==3.10.0`):**
> * `RapidOcrBackend` must pass explicit `Det.model_path` → `local_assets/ocr/ch_PP-OCRv5_det_mobile.onnx` and `Rec.model_path` → `local_assets/ocr/cyrillic_PP-OCRv5_rec_mobile.onnx` with `Global.use_cls = False`, `Det.ocr_version = OCRVersion.PPOCRV5`, `Det.model_type = ModelType.MOBILE`, `Rec.ocr_version = OCRVersion.PPOCRV5`, `Rec.lang_type = LangRec.CYRILLIC`, `Rec.model_type = ModelType.MOBILE`, and `EngineConfig.onnxruntime.intra_op_num_threads = config.ocr_threads` so RapidOCR **never** attempts network downloads.
> * **Stateful `use_det` trap:** Because `rapidocr==3.10.0` mutates `engine.use_det` persistently when `use_det=False` is passed, `RapidOcrBackend` **must pass `use_det=True` or `use_det=False` explicitly on every single call** (covered by an interleaved unit test).

---

## 3. Proposed Changes (Grouped by Sub-Phase)

### Phase 2A — Pluggable OCR Engine & Universal Simulator Engine

#### [NEW] `src/pea/ocr/__init__.py`, `src/pea/ocr/base.py`
* `OcrBackend` Protocol (`backend_name`, `recognize(image, *, mode="det_rec") -> OcrResult`, `read_spec(image, spec: ReadoutSpec) -> tuple[OcrResult, float | int | str | None, str | None]`).
* Shared pure-Python helpers:
  * `normalize_homoglyphs(text)` mapping Latin↔Cyrillic visual lookalikes (`m→м`, `k→к`, `t→т`, `l→л`, `c→с`, `o→о`, `h→н`, `p→р`, `x→х`, `a→а`, `e→е`) for unit parsing.
  * `parse_numeric_and_unit(text, parse_mode, unit_pattern, normalize_homoglyphs)` handling optional sign (`[-−]?\d+`), Russian decimal comma (`,` → `.`), and unit-scale extraction (`мкТл` vs `мТл`, `мГн`, `мкФ` vs `пФ`, `Ом`, `с⁻¹`).
  * `prepare_crop(image, roi_px, pad_px, scale)` using Pillow (white padding + Lanczos upscale).

#### [NEW] `src/pea/ocr/rapid_ocr.py`
* `RapidOcrBackend` loading pinned local ONNX files from `config.local_assets_dir / "ocr"`, always passing explicit `use_det=True/False` per call, returning normalized `OcrResult` with both pixel and `[0, 1]` bounding boxes.

#### [NEW] `native/macos_vision_ocr.swift`, `src/pea/ocr/macos_vision.py`
* Optional persistent JSON-lines stdin/stdout Swift helper (`usesCPUOnly = true`, compiled once via `-module-cache-path local_assets/swift-cache`).
* `MacOsVisionOcrBackend` enforces a hard `config.ocr_timeout_s` per-call deadline; on timeout/crash, kills the child cleanly and automatically falls back to `RapidOcrBackend`.

#### [NEW] `src/pea/sim/__init__.py`, `src/pea/sim/swf_meta.py`
* `inspect_swf(path: Path) -> SwfMeta`: parses `FWS` (uncompressed), `CWS` (zlib), and `ZWS` (lzma) headers, bit-unpacks signed TWIPS stage `RECT` into exact `(stage_width, stage_height)`, `fps`, `frame_count`, `swf_version`, and streaming `sha256`.

#### [NEW] `src/pea/sim/runtime_server.py`
* `SimRuntimeServer`: Loopback-only (`127.0.0.1`, ephemeral port `0`) threaded HTTP server with strict `Host` header check (anti-DNS-rebinding), `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`.
* Serves `local_assets/ruffle/`, the SHA-256-verified `.swf`, and an auto-sized `index.html` (`39 px` fixed header bar + `<ruffle-player>` locked to exact native `stage_width × stage_height`, `allowScriptAccess: false`, `allowNetworking: 'none'`). Also supports confined local HTML5 simulator folders.

#### [NEW] `src/pea/sim/profiles.py`
* `ProfileStore`: Loads/saves SHA-256-keyed `CalibrationProfile` JSON under `profiles/local/<sha256>.json` (with read-only fallback to shared `profiles/<sha256>.json`), atomic writes, and stage-dimension verification.

#### [NEW] `src/pea/sim/engine.py`, `src/pea/sim/protocols_m2.py`
* `SimulatorSession` & `SimulatorManager` (async Playwright Chromium using `config.browser_executable`):
  * Network isolation via `page.route("**/*", ...)` checking `is_origin_allowed()`.
  * **`sim_open`**: Opens local `.swf`, local HTML5 dir, or allowlisted URL; asserts exact `<ruffle-player>` bounds `(0, 39, W, H)`; loads any saved SHA-256 `CalibrationProfile`.
  * **`sim_observe`**: Captures unmodified raw PNG (hashed in `provenance.jsonl`), runs OCR, and optionally writes an annotated coordinate-grid + numbered-box PNG for agent calibration.
  * **`sim_act`**: Executes `click`, `double_click`, `drag`, `type`, `key` (OS-aware `Meta+a`/`Control+a`), `wait`, `blur`, and profile-aware **`set_stepper`**:
    * `commit_mode="enter"` (Lab 5 `m96220`): click → select-all → type → `Enter` → blur.
    * `commit_mode="step_interior"` (Lab 4 `m96210`/`m96211`): boundary-safe direction (`target + step` + `down_xy` when strictly below `max_val`; `target - step` + `up_xy` at/near `max_val` so `+20 A` never clamps to `19 A`) → blur.
    * `commit_mode="blur"`: click → select-all → type → click inert `blur_xy` (dismisses blinking `|` caret before OCR).
  * **`sim_save_profile`**: Validates live control/readout visibility before persisting profile.
  * **`sim_sweep`**: N-dimensional grid sweep with per-point OCR control verification (`verify_controls=True`), incremental `partial-readings.json`, hashed evidence PNGs, zero equation substitution on failure, and clean cancellation preserving partial evidence.
  * **`sim_search_extremum`**: Closed-loop stepping (`max`, `min`, `plateau_max`, `threshold`) supporting wide plateaus (`max_steps=120+`, covering Lab 5 $R=5.0\,\Omega$'s 74-step plateau), stopping after `stop_after_drop` consecutive drops below peak, returning `PlateauResult` with full `trace` and hashed `boundary_screenshots`.
  * **`protocols_m2.py`**: Typed stubs for `sim_watch(WatchSpec)` and `compute_sim(ComputeSimSpec)` raising `NotImplementedError("Scheduled for Milestone 2")`.

---

### Phase 2B — Physics Math, Data Analysis & Vector Visuals

#### [NEW] `src/pea/math_engine/__init__.py`, `src/pea/math_engine/calculator.py`
* `PhysicsCalculator` (zero `eval()`/`exec()`, pure Python `ast.parse` safe AST evaluator):
  * 5-dim SI vector `(kg, m, s, A, K)`.
  * Full unit table: mechanics (`kg, g, m, cm, mm, km, s, ms, us, ns, min, Hz, kHz, MHz, rad, deg, N, J, W, eV, keV, Pa, K`) + electromagnetism/AC (`A, mA, uA, kA, C, mC, uC, nC, pC, V, mV, kV, Ohm, kOhm, MOhm, F, mF, uF, nF, pF, H, mH, uH, T, mT, uT, Wb`, plus compound expressions like `rad/s`, `T*m/A`, `m^-1`, `m^-3`, `kA/m`, `µT`, `Ω`).
  * Built-in constants: `pi, e, me, mp, c, mu0, eps0, kB, g0`.
  * Supports scalars, 3D vectors (`dot, cross, norm`), complex AC impedance (`j, abs, arg, re, im, conj`), math functions (`sqrt, sin, cos, tan, asin, acos, atan, exp, log, log10`), conventional unary precedence (`-2**2 == -4`), linear Gaussian uncertainty propagation, and persistent `calculations.jsonl` ledger with immutable `calc_id`.

#### [NEW] `src/pea/math_engine/analysis.py`
* `data_ingest(spec_or_path, run_workspace)` — imports CSV/JSON/inline rows into `<run>/datasets/<id>.json` preserving explicit `ProvenanceSource`.
* `data_analyze(dataset_id, derived_columns, regressions, comparisons, run_workspace)`:
  * Evaluates safe AST column transforms (`1/r`, `(R**2 + r**2)**(-1.5)`, `(w1+w2)/2`, `w0/dw`, `Q**2`).
  * Runs grouped/ungrouped OLS regressions with explicit `intercept="free"` or `intercept="zero"` (returning slope, `slope_se`, intercept, `intercept_se`, $R^2$, `n_points`).
  * Computes `ComparisonOutput` (`abs_diff`, `percent_deviation`) and column summary stats.

#### [NEW] `src/pea/visuals/__init__.py`, `src/pea/visuals/plots.py`, `src/pea/visuals/diagrams.py`
* **`plots.py`** (`render_plot`): Headless Matplotlib (`Agg`) configured with bundled `local_assets/fonts/NotoSans-Regular.ttf` / `Bold.ttf` (full English + Cyrillic); renders `scatter`, `line`, `fit_line`, `errorbar`, `equal_aspect` (for Problem 4C $q$–$I$ ellipses), annotations, and exports both `.svg` and 300 DPI `.png` logged in `provenance.jsonl`.
* **`diagrams.py`** (`render_diagram`):
  * `kind="circuit"`: `schemdraw==0.23` declarative builder (`resistor`, `capacitor`, `inductor`, `source_sin`, `source_dc`, `ammeter`, `voltmeter`, `switch`, `line`, `ground`, `dot`) with full LaTeX math labels (e.g., $\varepsilon_0 \cos\omega t$, $U_{0C}, U_{0L}, U_{0R}$ for Lab 5 Fig. 1).
  * `kind="phasor"` & `kind="field"`: Parametric SVG/PNG renderer for phasor diagrams, impedance/power triangles, AC waveforms, and magnetic field diagrams.

---

### Phase 2C — PDF Intake & Submission-Ready Report Compiler

#### [NEW] `src/pea/pdf_intake.py`
* `inspect_pdf(pdf_path, run_workspace, dpi=180, max_pages=30)`: Renders every page via `pypdfium2` (`page.render(scale=dpi/72).to_pil()`) to `pdf_pages/page-NNN.png` and extracts page-indexed text via `pypdf`, recording hashes in `provenance.jsonl`.

#### [NEW] `src/pea/report/__init__.py`, `templates/{report.html,report.css,teacher_evidence.html}`, `pdf_builder.py`, `docx_builder.py`, `evidence.py`
* **Templates:** Clean university academic layout (localized `en`/`ru` headings), offline KaTeX (`local_assets/katex/`), bundled Noto Sans `@font-face`, print CSS (`@page` A4 margins, `break-inside: avoid` on tables/figures/cards, `break-after: avoid` on headings), and modular opt-in **Self-Control Q&A** (rendering `equations` + linked `figure_ids`) and **Additional Problems** (linking `calc_ids` + `figure_ids`).
* **`pdf_builder.py`:** Renders HTML via local loopback/file assets (zero external network calls) in headless Chromium (`wait_for_function("window.__katexDone === true")`), exports A4 PDF (`page.pdf`), and provides `preview_report_pages()` via `pypdfium2`.
* **`docx_builder.py`:** Mirrors `ReportSpec` into an editable `.docx` (`python-docx`) with styled tables, 300 DPI PNG figures, equations, and opt-in Q&A/Problems.
* **`evidence.py`:**
  * `build_evidence_zip`: Archives raw `evidence/`, `provenance.jsonl`, datasets, OCR logs, and calibration profiles into `reports/evidence.zip` (excluding itself and `*.tmp`) and logs SHA-256.
  * `build_teacher_evidence_pdf`: Optional multi-page audit PDF (summary + SHA-256 index + full-page screenshots).

---

### Phase 2D — `MCPServer`, Local Viewer, Agent Skill & `.mcpb` Packaging

#### [NEW] `src/pea/mcp_server.py`
* Built on **`mcp.server.mcpserver.MCPServer`** (`mcp==2.3.0`), registering **16 universal tools** with `ToolAnnotations` + 1 read-only resource `pea://policy`:
  * Handout & Runs: `lab_inspect_pdf`, `lab_runs`
  * Simulator: `sim_open`, `sim_observe`, `sim_act`, `sim_save_profile`, `sim_sweep`, `sim_search_extremum`, `sim_close`
  * Math & Data: `data_ingest`, `data_analyze`, `calc_eval`
  * Visuals & Reports: `plot_render`, `diagram_render`, `report_build`, `report_preview`
* Lifespan hook cleanly closes active Playwright sessions & loopback servers on exit; zero arbitrary shell/filesystem tools.

#### [NEW] `src/pea/viewer/__init__.py`, `app.py`, `static/{index.html,app.js,style.css}`
* Lightweight Starlette app (`pea-viewer`, bound strictly to `127.0.0.1:5190`) with `Host` and `Origin` validation middleware and `X-Content-Type-Options: nosniff`.
* Endpoints: `/`, `/api/health`, `/api/runs`, `/api/runs/{run_id}`, `/runs/{run_id}/{artifact:path}` (confined via `resolve_confined_path`).
* Minimal responsive dashboard (desktop `1280px` & mobile `390px` zero horizontal overflow): latest capture preview, optional OCR box overlay toggle, provenance badge, run history, and PDF/DOCX/Evidence downloads.

#### [NEW] `skills/pea-lab-assistant/SKILL.md`
* Universal Agent Skill usable in Antigravity, Claude Desktop/Code, Cursor, and Codex encoding the strict workflow: inspect PDF → confirm team/variant & opt-in sections → reuse SHA-256 profile or calibrate visually → run `sim_sweep`/`sim_search_extremum` (never fabricate readings) → run all math via `calc_eval`/`data_analyze` → generate plots & schematics → compile PDF/DOCX → visually inspect rendered pages via `report_preview`.

#### [NEW] `packaging/mcpb/{manifest.json,launcher.mjs}`, `scripts/build_mcpb.py`, `[MODIFY] README.md`
* Claude Desktop Extension v0.3 manifest + tiny zero-dep Node stdio shim spawning `<user_config.project_folder>/.venv/bin/pea-mcp` (plus Windows `.venv\Scripts\pea-mcp.exe` fallback).
* `scripts/build_mcpb.py` builds unsigned `dist/pea-mcp-0.2.0.mcpb` and audits the ZIP for zero traversal entries, SWFs, PDFs, ONNX models, or secrets.

---

## 4. Verification Plan (Offline Unit & Protocol Suite for Phase 2)

```sh
.venv/bin/pytest tests/ -m "not live_sim" -v
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/python scripts/build_mcpb.py --check
```

| Test Module | What It Verifies |
| :--- | :--- |
| `tests/test_swf_meta.py` | Header parsing (`stage_width`, `stage_height`, `fps`, `sha256`) on `m96210` (`500×450`), `m96211` (`700×500`), `m96220` (`800×600`) + corrupted header rejection. |
| `tests/test_ocr_and_profiles.py` | Offline `RapidOcrBackend` on staged Lab 4 crops (`4/4` exact value+sign+unit), **interleaved `use_det=False` / `use_det=True` stateful-bug regression test**, `мкТл` vs `мТл` homoglyph parsing, `MacOsVisionOcrBackend` timeout fallback mock, `ProfileStore` atomic save/load/validation. |
| `tests/test_sim_protocols.py` | `SimRuntimeServer` `Host` guard + auto-sized HTML, boundary-safe `set_stepper` direction logic (including `+20 A` max bound), `sim_sweep` & `sim_search_extremum` (`plateau_max` across 74-step synthetic plateau, cancel partial-evidence retention) using mocked page/OCR drivers. |
| `tests/test_calculator.py` | 5-dim SI dimensional checks, RLC/AC units (`mH, uF, pF, Ohm, rad/s`), complex impedance (`R + j*w*L`), 3D vectors (`dot, cross, norm`), unary precedence (`-2**2 == -4`), uncertainty propagation, and security rejection (`__import__`, `eval`, attribute access, non-finite outputs). |
| `tests/test_analysis.py` | CSV/JSON ingest, derived transforms (`1/r`, `(R**2+r**2)**(-1.5)`, `w0/dw`, `Q**2`), free vs. zero-intercept OLS against exact Lab 4 `mu0` and Lab 5 `4Q^2` benchmarks. |
| `tests/test_visuals.py` | `render_plot` (EN + Cyrillic labels, fit line, `equal_aspect` ellipse) and `render_diagram` (`schemdraw` RLC circuit + phasor SVG) produce valid non-empty `.svg` + `.png` files registered in `provenance.jsonl`. |
| `tests/test_pdf_intake.py` | `inspect_pdf` on `local_assets/benchmarks/lab5-rlc-handout.pdf` renders all 8 PNGs (`page-001.png..008.png`) and extracts Table 1/Table 2 text. |
| `tests/test_report_builders.py` | Offline HTML + KaTeX → Playwright A4 PDF (both EN & RU with Cyrillic, equations, SVG figure, opt-in Q&A with diagram), `report_preview` PNG render, `.docx` parseback via `python-docx`, `evidence.zip` SHA-256 verification. |
| `tests/test_mcp_protocol.py` | Real `mcp.client.stdio.stdio_client` over stdio: initializes `pea-mcp`, verifies all 16 tools + annotations + `pea://policy`, tests `calc_eval` & `data_analyze` end-to-end over stdio, rejects path traversal and extra schema fields; tests Starlette `pea-viewer` `Host`/`Origin` guards & artifact endpoints via `httpx.AsyncClient`. |

*(Full live Ruffle dual-lab E2E benchmarks on `m96220.swf` and `m96210/m96211.swf` follow in **Phase 3**.)*

---

## 5. Recommended Execution Cadence

Execute sub-phase by sub-phase with a clean commit & test run after each:
1. **Phase 2A** (`ocr/` + `sim/`) → test + commit
2. **Phase 2B** (`math_engine/` + `visuals/`) → test + commit
3. **Phase 2C** (`pdf_intake.py` + `report/`) → test + commit
4. **Phase 2D** (`mcp_server.py` + `viewer/` + `SKILL.md` + `.mcpb`) → test + commit

---

## 6. Observed Phase 2 Findings *(Populated After Execution)*

* **Phase 2A (`ocr/` + `sim/`):** `Complete (2026-11-21)`
  * Implemented pluggable offline OCR (`src/pea/ocr/base.py`, `src/pea/ocr/rapid_ocr.py`, `src/pea/ocr/macos_vision.py`, `native/macos_vision_ocr.swift`) and universal simulator engine (`src/pea/sim/swf_meta.py`, `runtime_server.py`, `profiles.py`, `engine.py`, `protocols_m2.py`).
  * **Swift Vision Helper Note:** Fixed `VNRequestTextRecognitionLevel` (instead of `VNRequestRecognitionLevel`) and verified sandbox compilation via `swiftc -O -module-cache-path local_assets/swift-cache` (`local_assets/bin/macos_vision_ocr`, JSON-lines protocol smoke-tested; hard timeout kills hung child cleanly and falls back to `RapidOcrBackend`).
  * **RapidOCR Findings:** Passing `PIL.Image` directly to `RapidOCR` lets its loader convert RGB→BGR cleanly; `Global.log_level="error"` + stderr logger redirection keeps stdout clean for MCP stdio. On 3× upscaled legacy Lab 4 crops (`1560×495` / `3120×432`), DB text detection requires sufficient white border padding (`pad_px=80` at 3× scale) to avoid splitting `мкТл` / decimal commas; with `pad_px=80`, all `4/4` staged Lab 4 crops extract exact value, sign, and unit (`50.0`, `200.0`, `-60.32`, `-241.30 мкТл`). Interleaved `rec_only` (`use_det=False`) ↔ `det_rec` (`use_det=True`) calls pass cleanly under explicit per-call `use_det` + lock guard.
  * **Verification:** `60/60` tests pass (`.venv/bin/pytest tests/ -m "not live_sim" -v`), `ruff check src/ tests/ scripts/` clean (`0` errors), real SWF header inspection verified on `m96210` (`500×450`), `m96211` (`700×500`), and `m96220` (`800×600`), plus live headless Chromium + Ruffle `0.7.1` `SimulatorManager.open()` + `observe()` verified on `m96220.swf` (`800×600`) and `m96210.swf` (`500×450`) with zero `page_errors`, zero `aborted_requests`, exact `<ruffle-player>` bounds, and verified provenance.
* **Phase 2B (`math_engine/` + `visuals/`):** `Pending`
* **Phase 2C (`pdf_intake.py` + `report/`):** `Pending`
* **Phase 2D (`mcp_server.py` + `viewer/` + `SKILL.md` + `.mcpb`):** `Pending`
