# M2D Implementation Plan — MCP Tools `#17–#18`, `pea-viewer` Datasets View, `v0.3.0` Packaging & Live HTML5 Transient E2E

> **Status:** `Complete (2026-11-24)`
> **Parent Plan:** [`MILESTONE_2_PLAN.md`](./MILESTONE_2_PLAN.md)
> **Prerequisite:** M2A, M2B, and M2C complete (frozen schemas in [`src/pea/schemas.py`](../../src/pea/schemas.py); `sim_watch` & HTML5/URL fingerprinting in [`src/pea/sim/engine.py`](../../src/pea/sim/engine.py); `compute_sim` sandboxed engine in [`src/pea/math_engine/compute_sim.py`](../../src/pea/math_engine/compute_sim.py) + atomic `os.link` `data_ingest`; 191 non-loopback offline tests, `ruff`, and `scripts/build_mcpb.py --check` passing).

---

## 1. Scope & Version/Tool-Count Parity Checklist

### M2D Workstreams
1. **MCP Server Tools `#17–#18` & `sim_sha256` Exposure (`[MODIFY] src/pea/mcp_server.py`):**
   * Register `sim_watch` (`#17`) and `compute_sim` (`#18`) with strict `extra="forbid"`, `ToolAnnotations`, error sanitization, and orphan-run prevention.
   * Expose `"sim_sha256"` in `sim_open`'s response alongside `"swf_meta"`.
   * Harden `_collect_run_artifacts()` to skip hidden dotfiles (`p.name.startswith(".")`) under `datasets/`.
2. **Minimal `pea-viewer` Datasets & Model Outputs View (`[MODIFY] src/pea/viewer/{app.py,static/{index.html,app.js,style.css}}`):**
   * Enrich `_detail_run().datasets` defensively with `source`, `row_count`, `columns`, and `notes`.
   * Add a compact **Datasets & Model Outputs** table card (`#datasets-card` / `#datasets-tbody`) between Figures and Provenance Ledger with provenance badges and JSON download links (zero external dependencies, responsive at `1280×800` and `390×844`).
3. **Version & 18-Tool Parity Bump (`0.2.0` / `16` → `0.3.0` / `18`) & `.mcpb` Build:**
   * Bump version across [`pyproject.toml`](../../pyproject.toml), [`src/pea/__init__.py`](../../src/pea/__init__.py), and [`packaging/mcpb/manifest.json`](../../packaging/mcpb/manifest.json).
   * Add `sim_watch` and `compute_sim` to `manifest.json` in canonical order and build/audit `dist/pea-mcp-0.3.0.mcpb` via [`scripts/build_mcpb.py`](../../scripts/build_mcpb.py).
4. **Companion Skill & Documentation Updates:**
   * Update [`skills/pea-lab-assistant/SKILL.md`](../../skills/pea-lab-assistant/SKILL.md), [`README.md`](../../README.md), and [`PLAN_V2.md`](./PLAN_V2.md) for the 18-tool surface, Stage 4 `sim_watch`, Stage 5 `compute_sim`, `pea-viewer` Datasets card, and `0.3.0` `.mcpb` bundle.
5. **Offline Tests, HTML5 Transient Fixture & Live E2E Benchmark:**
   * Update [`tests/test_phase1_core.py`](../../tests/test_phase1_core.py) and [`tests/test_mcp_protocol.py`](../../tests/test_mcp_protocol.py).
   * Create deterministic local HTML5 RC/RLC fixture [`tests/fixtures/html5_rc_sim/index.html`](../../tests/fixtures/html5_rc_sim/index.html) and live E2E benchmark [`tests/e2e/test_transient_m2_e2e.py`](../../tests/e2e/test_transient_m2_e2e.py) (`@pytest.mark.live_sim`).
   * Complete Visual QA on rendered transient PDFs, headless Chromium QA on `pea-viewer`, tracked-file home-path audit, and populate Section 6/7 findings across `M2B_PLAN.md`–`M2D_PLAN.md` and `MILESTONE_2_PLAN.md`.

### Version & Tool-Count Parity Matrix (`0.2.0 / 16` → `0.3.0 / 18`)

| Touchpoint | Old (`M1`) | New (`M2D`) |
| :--- | :--- | :--- |
| [`pyproject.toml`](../../pyproject.toml) | `version = "0.2.0"` | `version = "0.3.0"` |
| [`src/pea/__init__.py`](../../src/pea/__init__.py) | `__version__ = "0.2.0"` | `__version__ = "0.3.0"` |
| [`src/pea/mcp_server.py`](../../src/pea/mcp_server.py) | 16 tools (`UNIVERSAL_TOOL_NAMES`, docstrings, `pea://policy`) | 18 tools (adding `sim_watch`, `compute_sim`) |
| [`packaging/mcpb/manifest.json`](../../packaging/mcpb/manifest.json) | `"version": "0.2.0"`, 16 `tools[]`, `"16 universal..."` | `"version": "0.3.0"`, 18 `tools[]`, `"18 universal..."` |
| [`scripts/build_mcpb.py`](../../scripts/build_mcpb.py) | Hardcoded `16 tools` print & docstrings | Dynamic `len(_expected_tool_names())` + `0.3.0` / 18-tool docs |
| [`skills/pea-lab-assistant/SKILL.md`](../../skills/pea-lab-assistant/SKILL.md) | 16-tool reference table | 18-tool table + Stage 4 `sim_watch` & Stage 5 `compute_sim` |
| [`tests/test_phase1_core.py`](../../tests/test_phase1_core.py) | `pea.__version__ == "0.2.0"` | `pea.__version__ == "0.3.0"` |
| [`tests/test_mcp_protocol.py`](../../tests/test_mcp_protocol.py) | `len(tools_res.tools) == 16`, `policy["tools_count"] == 16` | `len(tools_res.tools) == 18`, `policy["tools_count"] == 18` |

---

## 2. Detailed Design by Component

### 2.1 MCP Server Tools `#17–#18` (`[MODIFY] src/pea/mcp_server.py`)

1. **Canonical `UNIVERSAL_TOOL_NAMES` (18 Tools):**
   * **Group 1 — Handout & Runs (2):** `lab_inspect_pdf`, `lab_runs`
   * **Group 2 — Universal Simulator (8):** `sim_open`, `sim_observe`, `sim_act`, `sim_save_profile`, `sim_sweep`, `sim_search_extremum`, `sim_watch`, `sim_close`
   * **Group 3 — Math & Data (4):** `data_ingest`, `data_analyze`, `calc_eval`, `compute_sim`
   * **Group 4 — Visuals (2):** `plot_render`, `diagram_render`
   * **Group 5 — Reports (2):** `report_build`, `report_preview`

2. **Imports & Error Translation:**
   * Import `ComputeSimError` and `run_compute_sim as _run_compute_sim` from `pea.math_engine.compute_sim`.
   * Import `ComputeSimSpec` and `WatchSpec` from `pea.schemas`.
   * Add `ComputeSimError` to the sanitized exception tuple in `_raise_tool_error()` (covers `ComputeSimTimeoutError` and `ComputeSimLimitError` subclasses cleanly as sanitized `ToolError`s).

3. **`sim_open` `sim_sha256` Field:**
   * Include `"sim_sha256": session.sim_sha256` in `sim_open`'s return dictionary alongside `"swf_meta"` so HTML5/URL sessions receive their deterministic SHA-256 fingerprint directly for `CalibrationProfile.swf_sha256`.

4. **Tool `#17`: `sim_watch`:**
   ```python
   @server.tool(
       name="sim_watch",
       title="Watch Timed Simulator Transient",
       description=(
           "Execute a drift-compensated timed transient or stopwatch capture (hybrid burst_post_ocr "
           "or inline_ocr with threshold/stability early stops), logging SHA-256 frames, readings, "
           "and optional simulator_captured datasets."
       ),
       annotations=ToolAnnotations(
           title="Watch Timed Simulator Transient",
           read_only_hint=False,
           destructive_hint=False,
           idempotent_hint=False,
           open_world_hint=False,
       ),
   )
   async def sim_watch(
       session_id: str,
       spec: WatchSpec,
   ) -> dict[str, Any]:
   ```
   * Retrieve `session = runtime.sim_manager.get_session(session_id)`, run `result = await session.watch(spec)`.
   * If `result.points` and `session.workspace.status_path.is_file()`, update `latest_screenshot_relpath` to `result.points[-1].screenshot_relpath`.
   * Return `{"session_id": session.session_id, "run_id": session.workspace.run_id, **result.model_dump(mode="json")}`.

5. **Tool `#18`: `compute_sim`:**
   ```python
   @server.tool(
       name="compute_sim",
       title="Run Sandboxed Numerical / ODE / FFT Model",
       description=(
           "Evaluate a zero-eval/exec sandboxed ODE IVP (RK45/Radau/BDF), algebraic grid, or "
           "one-sided FFT spectrum (inline or dataset-backed with Hann windowing and jitter "
           "resampling), persisting an immutable model_computed dataset."
       ),
       annotations=ToolAnnotations(
           title="Run Sandboxed Numerical / ODE / FFT Model",
           read_only_hint=False,
           destructive_hint=False,
           idempotent_hint=False,
           open_world_hint=False,
       ),
   )
   def compute_sim(
       spec: ComputeSimSpec,
       run_id: str | None = None,
       label: str | None = None,
   ) -> dict[str, Any]:
   ```
   * **Orphan-run prevention:**
     * Validate `spec` upfront (`validated = spec if isinstance(spec, ComputeSimSpec) else ComputeSimSpec.model_validate(spec)`).
     * If `validated.input_dataset_id is not None` and `run_id is None`, raise `ValueError("run_id is required when input_dataset_id is specified")` **before** calling `create_run()`.
     * Resolve `workspace = runtime.workspace_manager.get_run(run_id)` if `run_id is not None` else `runtime.workspace_manager.create_run(label=label, source="model_computed")` — note: if `run_id` is omitted and `_run_compute_sim` fails (e.g. unsafe expression), avoid leaving an empty orphan workspace by validating AST / pre-flighting before `create_run`, or passing an existing `run_id`. Better: only call `create_run` after `validated` passes; and for `run_id is None` (inline `ode_ivp`/`algebraic_grid`/`fft_spectrum`), if `_run_compute_sim` raises before persisting anything, delete the freshly created empty run dir cleanly or validate expressions via AST before `create_run`.
     * Return `{"run_id": workspace.run_id, **result.model_dump(mode="json")}`.

6. **Artifact Listing & Docstring Updates:**
   * In `_collect_run_artifacts()`, ignore hidden dotfiles (`not p.name.startswith(".")`) alongside `not p.name.endswith(".tmp")` so M2C's temporary `.{id}.{pid}.{token}.tmp` files never appear if inspected mid-write.
   * Update module docstring, `create_mcp_server` docstring, and `pea://policy` resource description from `16` to `18` tools.

---

### 2.2 Minimal `pea-viewer` Datasets & Model Outputs View (`[MODIFY] src/pea/viewer/{app.py,static/{index.html,app.js,style.css}}`)

1. **`src/pea/viewer/app.py` (`_detail_run`):**
   * When enumerating `<run>/datasets/*.json`:
     * Skip `item.name.startswith(".")` and `item.name.endswith(".tmp")`.
     * Verify strict workspace confinement via `workspace.resolve_path(rel)` catching `PathConfinementError` (skipping any escaping symlink).
     * Defensively read and parse the JSON (`try/except (OSError, json.JSONDecodeError, ValueError)`) to extract:
       * `dataset_id`: `payload.get("dataset_id") or item.stem` (only if `isinstance(..., str)` and non-empty).
       * `relpath`: `rel`
       * `url`: `_artifact_url(workspace.run_id, rel)`
       * `source`: `payload.get("source")` if valid string else `source_by_relpath.get(rel, "unspecified")`
       * `row_count`: `len(rows)` if `isinstance(rows, list)` else `0`
       * `columns`: ordered list of unique string keys across `rows` (preserving insertion order)
       * `notes`: `str(payload.get("notes") or "")`
     * Fall back cleanly to `{"dataset_id": item.stem, "relpath": rel, "url": ..., "source": source_by_relpath.get(rel, "unspecified"), "row_count": 0, "columns": [], "notes": ""}` on malformed JSON so `_detail_run()` never crashes.

2. **`src/pea/viewer/static/index.html`:**
   * Insert `<article class="card datasets-card" id="datasets-card">` directly between `.figures-card` and `.provenance-card`:
     * Header: `<h3>Datasets &amp; Model Outputs</h3>` and `<span id="datasets-count" class="muted-count">0 datasets</span>`.
     * `<div class="table-scroll"><table class="ledger-table">` with columns:
       `<th>Dataset</th>`, `<th>Source</th>`, `<th class="num-col">Rows</th>`, `<th>Columns</th>`, `<th>Notes</th>`.
     * `<tbody id="datasets-tbody"></tbody>`.

3. **`src/pea/viewer/static/app.js`:**
   * Add `datasetsCount: document.getElementById("datasets-count")` and `datasetsTbody: document.getElementById("datasets-tbody")` to `els`.
   * Implement `renderDatasets(datasets)` using safe DOM APIs (`document.createElement`, `textContent`, zero `innerHTML`):
     * Update `#datasets-count` (`"N dataset"` / `"N datasets"`).
     * Empty state row (`colSpan = 5`): `"No datasets or model outputs recorded yet."`.
     * Per dataset: clickable JSON download link (`ds.url`, `target="_blank" rel="noopener"`, text `ds.dataset_id`), provenance pill via `sourcePillClass(ds.source)` (`simulator_captured` green, `bench_manual` amber, `model_computed` navy), numeric `row_count`, columns summary (`cols.slice(0, 6).join(", ")` + `( +K more)` if `cols.length > 6`, `"—"` if empty), and `notes` (`"—"` if empty).
   * Call `renderDatasets(detail.datasets || [])` inside `renderRunDetail(detail)`.

4. **`src/pea/viewer/static/style.css`:**
   * Add `.cols-col` and `.notes-col` table cell rules (`overflow-wrap: anywhere; min-width: 140px;`) so long column lists or notes wrap cleanly without horizontal overflow at `1280×800` or `390×844`.

---

### 2.3 Version Bump & `.mcpb` `v0.3.0` Packaging

* **`[MODIFY] pyproject.toml`:** `version = "0.3.0"`.
* **`[MODIFY] src/pea/__init__.py`:** `__version__ = "0.3.0"`.
* **`[MODIFY] packaging/mcpb/manifest.json`:**
  * `"version": "0.3.0"`
  * Update `"long_description"` to reference `18 universal laboratory tools`.
  * Add `sim_watch` (right after `sim_search_extremum`) and `compute_sim` (right after `calc_eval`) to `"tools"` so `manifest["tools"]` matches `UNIVERSAL_TOOL_NAMES` in exact order.
* **`[MODIFY] scripts/build_mcpb.py`:**
  * Update docstrings (`18-tool parity`, `dist/pea-mcp-0.3.0.mcpb`).
  * Replace hardcoded `16 tools` in `main()` `--check` output with `{len(_expected_tool_names())} tools`.
* **Build artifact:** Run `.venv/bin/python scripts/build_mcpb.py` to produce and audit `dist/pea-mcp-0.3.0.mcpb` (and remove stale `dist/pea-mcp-0.2.0.mcpb` only if appropriate — check git status first; keep git history clean).

---

### 2.4 Companion Skill & Documentation

* **`[MODIFY] skills/pea-lab-assistant/SKILL.md`:**
  * Update tool count `16` → `18`.
  * **Stage 4 — Verified Data Acquisition:** document **`sim_watch(session_id, spec=WatchSpec(...))`** for time-domain transients and stopwatches (hybrid `burst_post_ocr` vs. `inline_ocr` pacing, `trigger_action` / `trigger_actions`, `stop_when_stable_s` / `stability_tol`, `stop_readout` / `stop_op` / `stop_value` thresholds, `max_frames`, and auto-saved `simulator_captured` datasets).
  * **Stage 5 — Deterministic Data Analysis & Physics Math:** document **`compute_sim(run_id=..., spec=ComputeSimSpec(...))`** for `ode_ivp` (`RK45`/`Radau`/`BDF`), `algebraic_grid`, and `fft_spectrum` (inline or dataset-backed with Hann windowing and jitter resampling), emphasizing unconditional `model_computed` provenance separation.
  * Update the **Tool Reference** table (`pea-mcp` — 18 Universal Tools + 1 Resource) to include `sim_watch` and `compute_sim`.
* **`[MODIFY] README.md`:**
  * Update `16` → `18` tools, `pea-mcp-0.2.0.mcpb` → `pea-mcp-0.3.0.mcpb`, add `sim_watch` & `compute_sim` to the Universal MCP Surface table, and add the **Datasets & Model Outputs** provenance table bullet to `pea-viewer` features.
* **`[MODIFY] PLAN_V2.md`:**
  * Mark `M2.1` (`sim_watch`) and `M2.2` (`compute_sim`) complete (`[x]`), update module comments (`18 universal tools`, `compute_sim.py`, `protocols_m2.py`), and append the Milestone 2 Execution Log row with verified numbers once all tests pass.

---

## 3. Deterministic HTML5 Transient Fixture & Live E2E Benchmark

### 3.1 HTML5 RC & Damped RLC Fixture (`[NEW] tests/fixtures/html5_rc_sim/index.html`)

* Self-contained, **zero external network requests** (inline CSS + JS, system sans/monospace fonts), designed for exact fixed coordinates at `viewport_width=640, viewport_height=420` (`margin: 0; overflow: hidden;` on `body`):
  * **Top inert bar (`height: 36px`):** title `PEA RC / RLC Transient Fixture`, safe `blur_xy=(320, 18)`.
  * **Parameter inputs (`commit_mode="enter"`, `decimal_comma=False`):**
    * `R` (kΩ, default `10.0`), `C` (µF, default `100.0`), `V0` (V, default `10.0`).
    * Explicit `<button>` controls: `mode-rc` (`RC`), `mode-rlc` (`RLC`), `btn-reset` (`Reset`), `btn-start` (`Start`).
  * **High-contrast RapidOCR-friendly readouts** (dark `#0f172a` text on `#ffffff` boxed panels with generous padding, updated via `requestAnimationFrame` + `performance.now()`):
    * Stopwatch readout `t`: `0.00 s`
    * Voltage readout `Vc`: `10.00 V` (preserving `-` sign in RLC mode, e.g. `-8.42 V`)
  * **Physics equations:**
    * **RC discharge mode:** $V_C(t) = V_0 \, e^{-t / \tau}$ where $\tau = (R \times 10^3)(C \times 10^{-6})$ (at default `R=10 kΩ, C=100 µF → τ = 1.00 s`).
    * **Damped RLC mode:** $V_C(t) = V_0 \, e^{-\beta t} \cos(2\pi f_0 t)$ with $f_0 = 2.0\text{ Hz}$ and $\beta = 0.25\text{ s}^{-1}$ so a `3.0 s` capture spans `6` complete cycles for live FFT peak recovery.

### 3.2 Live E2E Transient Benchmark (`[NEW] tests/e2e/test_transient_m2_e2e.py`, `@pytest.mark.live_sim`)

Supports `PEA_E2E_KEEP_DIR` (e.g., `runs/m2-qa`) for preserving run artifacts for Visual QA and `pea-viewer` verification, and drives `pea-mcp` over real `mcp.client.stdio.stdio_client`:

1. **Open, Calibrate, Save & Auto-Load Profile:**
   * `sim_open(html5_dir="tests/fixtures/html5_rc_sim", viewport_width=640, viewport_height=420)` → assert non-null 64-char `sim_sha256` and `aborted_requests == []`.
   * `sim_observe(annotate=True)` → `sim_save_profile(verify_live=True)` → `sim_close`.
   * Re-open via `sim_open(html5_dir=..., run_id=run_id)` and assert `profile_loaded is True`.
2. **RC Discharge `sim_watch` (`inline_ocr` threshold stop) + `data_analyze`:**
   * Call `sim_watch` with `readouts=["t", "Vc"]`, `duration_s=4.0`, `interval_ms=120`, `trigger_actions=[Reset, Start]`, `stop_readout="Vc"`, `stop_op="le"`, `stop_value=1.5`, `save_dataset=True`, `dataset_id="rc_watch"`.
   * Assert `pacing_mode == "inline_ocr"`, `status == "stopped_threshold"`, `captured_frames >= 5`, and `dataset_relpath == "datasets/rc_watch.json"`.
   * Call `data_analyze` on `rc_watch` computing `ln_Vc = log(Vc)` and free-intercept OLS of `ln_Vc` vs `elapsed_s`, verifying recovered $\tau = -1 / \text{slope}$ matches $\tau_{\text{ref}} = 1.0\text{ s}$ within `< 5%` (`r_squared > 0.99`).
3. **`compute_sim` (`ode_ivp`) Analytical Comparison:**
   * Call `compute_sim` on `run_id` with `model_kind="ode_ivp"`, `expression="-Vc / (R * C)"`, `state_variables=["Vc"]`, `parameters={"R": 10_000.0, "C": 100e-6}`, `grid_start=0.0`, `grid_stop=..., grid_points=100`, `initial_state=[10.0]`, `dataset_id="rc_ode"`.
   * Assert `provenance == "model_computed"` and compare `rc_ode` values against live `rc_watch` points at matched times.
4. **Damped RLC `sim_watch` (`burst_post_ocr`) + `compute_sim` (`fft_spectrum`):**
   * Switch fixture to `RLC` mode (`trigger_actions=[click RLC, click Reset, click Start]`), run `sim_watch` (`readouts=["Vc"]`, `duration_s=3.0`, `interval_ms=40`, `save_dataset=True`, `dataset_id="rlc_watch"`).
   * Assert `pacing_mode == "burst_post_ocr"` and `status == "complete"`.
   * Call `compute_sim` with `model_kind="fft_spectrum"`, `input_dataset_id="rlc_watch"`, `time_column="elapsed_s"`, `signal_column="Vc"`, `window="hann"`, `detrend_mean=True`, `dataset_id="rlc_fft"`.
   * Assert `solver_meta["dominant_frequency_hz"]` recovers $f_0 = 2.0\text{ Hz}$ within FFT bin resolution (`|f_peak - 2.0| <= 1 / actual_duration_s + 0.1`).
5. **Close, Plots, Report, Preview & Verify:**
   * `sim_close(session_id)`.
   * `plot_render` for RC transient overlay (`simulator_captured` scatter points + `model_computed` ODE curve) and RLC FFT amplitude spectrum.
   * `report_build` compiling A4 PDF + `.docx` + `evidence.zip` + Teacher Evidence Packet PDF.
   * `report_preview` + `lab_runs(action="verify").verification.ok == True`.

---

## 4. Offline Protocol & Viewer Test Updates

1. **`tests/test_phase1_core.py`:**
   * Update `test_python_runtime_and_version`: assert `pea.__version__ == "0.3.0"`.
2. **`tests/test_mcp_protocol.py`:**
   * Update 16 → 18 tool assertions (`len(tools_res.tools) == 18`, `policy["tools_count"] == 18`, docstrings).
   * In `test_mcp_stdio_tools_policy_math_analysis_and_visuals`:
     * Call `compute_sim` (`ode_ivp` + dataset-backed `fft_spectrum`) over real `stdio_client`, verify `provenance == "model_computed"`, verify missing `run_id` with `input_dataset_id` fails cleanly without creating an orphan run, and verify `lab_runs(action="verify").verification.ok == True`.
   * In `test_mcp_simulator_tools_mocked_and_lifespan_cleanup`:
     * Assert `sim_open` exposes `"sim_sha256"` and exercise `sim_watch` via `create_mcp_server` + `call_tool`, verifying `WatchResult` fields, `latest_screenshot_relpath` update, and error sanitization.
   * In `test_viewer_endpoints_and_artifact_streaming` & `test_viewer_security_guards_*`:
     * Seed both `simulator_captured` and `model_computed` datasets; verify `/api/runs/{run_id}` returns enriched `datasets[]` (`dataset_id`, `source`, `row_count`, `columns`, `notes`, `url`) and safely skips escaping symlink dataset files.
   * In `test_viewer_responsive_desktop_and_mobile_zero_overflow_and_ocr_toggle`:
     * Seed a dataset and verify `#datasets-card` / `#datasets-tbody` render `simulator_captured` / `model_computed` provenance pills at `1280×800` and `390×844` with zero horizontal overflow, zero console/page errors, and zero external requests.

---

## 5. Verification Commands & Acceptance Gates *(Outside Socket Sandbox for Full Suite)*

```sh
.venv/bin/pytest tests/ -m "not live_sim" -v
.venv/bin/pytest tests/e2e/ -m live_sim -v
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/python scripts/build_mcpb.py
.venv/bin/python scripts/build_mcpb.py --check
```

Acceptance checklist:
1. All offline unit & protocol tests pass (`pytest tests/ -m "not live_sim" -v` — zero failures, zero skipped loopback tests).
2. All 3 live E2E benchmarks (`test_lab5_e2e.py`, `test_lab4_e2e.py`, `test_transient_m2_e2e.py`) pass (`pytest tests/e2e/ -m live_sim -v`).
3. Visual QA via `read_file` on every rendered transient PDF preview PNG under the preserved `runs/m2-qa` run (clean typography, KaTeX equations, figure overlays, tables, pagination).
4. Live headless Chromium verification of `pea-viewer` at `1280×800` and `390×844` on the preserved transient run (zero overflow/errors/external requests, Datasets card showing both `simulator_captured` and `model_computed` pills).
5. `ruff check src/ tests/ scripts/` clean (`0` errors) and `scripts/build_mcpb.py --check` passing for `v0.3.0` (`18` tools).
6. Tracked-file home-path hygiene audit (`0` matches in `git ls-files`) and Section 6/7 findings populated across `M2B_PLAN.md`, `M2C_PLAN.md`, `M2D_PLAN.md`, and `MILESTONE_2_PLAN.md`.

---

## 6. Known Pitfalls & Guardrails

1. **Exact Ordered Tool Parity:** `scripts/build_mcpb.py` checks `manifest_names == UNIVERSAL_TOOL_NAMES` as an ordered tuple — `sim_watch` and `compute_sim` must appear at exact matching positions in both `src/pea/mcp_server.py` and `packaging/mcpb/manifest.json`.
2. **Orphan-Run Prevention in `compute_sim`:** When `run_id=None`, never create a run workspace before validating `spec` or when `input_dataset_id` is supplied without `run_id`, and clean up if a pre-persistence error occurs on a newly created workspace.
3. **Defensive Dataset Reading in `pea-viewer`:** Never trust arbitrary JSON or symlinks inside `<run>/datasets/`; enforce `workspace.resolve_path()`, skip dotfiles/temp files, and tolerate malformed files gracefully without breaking `/api/runs/{run_id}`.
4. **RapidOCR Contrast & Negative-Sign Readability on HTML5 Fixture:** Use high-contrast dark text on white panels with generous padding, explicit units (`s`, `V`), and fixed `640×420` viewport sizing (`margin: 0; overflow: hidden`) so ROI crops never clip `-` signs during damped RLC oscillations.
5. **Non-Sandbox Execution for Final Acceptance:** macOS standard sandbox blocks loopback TCP binding and Chromium launch; run the full offline + live E2E + viewer verification outside the sandbox before closing Milestone 2.

---

## 7. Observed M2D Findings

* **18-Tool Parity & `v0.3.0` Packaging:** Registered `sim_watch` (`#17`) and `compute_sim` (`#18`) in exact `UNIVERSAL_TOOL_NAMES` order with `ToolAnnotations`, `additionalProperties: false`, `ComputeSimError` sanitization, `sim_open` `sim_sha256` exposure, hidden dotfile filtering in `_collect_run_artifacts()`, and orphan-run cleanup on pre-persistence `compute_sim` errors. Built and audited `dist/pea-mcp-0.3.0.mcpb` (`10077 bytes`, `scripts/build_mcpb.py --check` passing).
* **Live HTML5 Transient Fixture OCR Discovery:** During the first live run of `tests/e2e/test_transient_m2_e2e.py`, a wide vertical `rec_only` ROI (`80 px` tall for `36 px` text) caused RapidOCR to drop a single decimal dot (`4.80 V` → `480 V`) because PaddleOCR's recognition head shrinks tall whitespace-padded crops to `48 px`. Tightening the single-line `rec_only` ROIs to `44 px` height (`[65, 252, 280, 296]` and `[365, 252, 580, 296]`) produced `17/17` exact RC readings (`tau = 1.0003 s` vs `1.0000 s`, `0.03%` dev, `R^2 = 1.0000`, max RK45 ODE residual `0.029 V`) and `76` signed damped RLC readings (`f_peak = 1.974 Hz` vs `2.000 Hz`, `1.32%` dev, `resampled=True`).
* **Visual & Responsive QA:** Visually inspected all `4` rendered transient PDF preview PNGs (clean KaTeX equations, provenance-badged tables/figures, accurate RC overlay and FFT plots, verified calculations ledger). Verified `pea-viewer` in headless Chromium at `1280×800` and `390×844` on the preserved `runs/m2-qa` run (`0` overflow, `0` console/page errors, `0` external requests, Datasets & Model Outputs table rendering all `4` datasets with `simulator_captured` and `model_computed` pills and download links).
* **Full Verification Gate:** `206/206` offline tests + `3/3` live E2E benchmarks (`test_lab4_e2e.py`, `test_lab5_e2e.py`, `test_transient_m2_e2e.py`) pass (`209/209` total, `0` skipped), `ruff check src/ tests/ scripts/` clean (`0` errors), `scripts/build_mcpb.py --check` passed (`v0.3.0`, `18` tools), and tracked-file home-path hygiene audit clean (`0` matches).
