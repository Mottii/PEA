# Milestone 2 Implementation Plan — Timed Transients (`sim_watch`) & Sandboxed Numerical/ODE Modeling (`compute_sim`)

> **Status:** `Complete (2026-11-24)`
> **Parent Plan:** [`PLAN_V2.md`](./PLAN_V2.md)
> **Prerequisite:** Phase 3 / Milestone 1 complete (`b8f7bc1` on `main`; `134/134` tests pass; `ruff` clean; `dist/pea-mcp-0.2.0.mcpb` audited; `pea-viewer` verified).

---

## 1. Objective & Locked-In Design Decisions (from `/grill-me`)

Deliver both remaining **Milestone 2** capabilities — **timed transient & stopwatch capture (`sim_watch`)** and **sandboxed NumPy/SciPy numerical, coupled ODE, and FFT modeling (`compute_sim`)** — as deterministic, strictly zero-API-cost tools **`#17–#18`** on `pea-mcp`. Alongside the core engines, this milestone bumps PEA and the `.mcpb` extension bundle to **`v0.3.0`**, adds a lightweight **Datasets & Model Outputs** view to `pea-viewer`, updates companion docs/skills, and verifies the complete pipeline with unit/protocol tests plus a live HTML5 RC-discharge & damped-RLC transient E2E benchmark.

| Axis | Locked-In Decision |
| :--- | :--- |
| **Scope & Sequencing** | Build both **`sim_watch`** and **`compute_sim`** together in one Milestone 2 rollout (schemas, core engines, MCP tools `#17–#18`, minimal viewer update, skill/docs, `.mcpb` `v0.3.0`, unit/protocol tests, and live HTML5 E2E benchmark). |
| **Live E2E Benchmark** | Build a deterministic local HTML5 RC-discharge & damped-RLC transient fixture (`tests/fixtures/html5_rc_sim/index.html`) served via `SimRuntimeServer` HTML5 mode, keeping `sim_watch` 100% generic for any future Lab 3 SWF or external allowlisted HTML5 simulator. |
| **`sim_watch` Pacing & Timing** | **Hybrid pacing:** drift-compensated burst frame capture + post-OCR (`burst_post_ocr`) by default for lowest timing jitter, automatically switching to `inline_ocr` when live early-stop (`stop_when_stable_s` or threshold) is enabled. Always record both monotonic `elapsed_s` (`time.perf_counter()`) and any calibrated in-sim stopwatch readout. |
| **`WatchSpec` Triggers, Stops & Provenance** | Backwards-compatible extensions: `trigger_actions[]` (alongside `trigger_action`), `stability_tol`, optional threshold stop (`stop_readout` / `stop_op` / `stop_value`), `max_frames` safety cap (default `500`, cap `2000`), `blur_before_capture`, `settle_ms`, `save_dataset`, and `dataset_id`. Every captured frame is SHA-256 hashed in `provenance.jsonl`, with optional auto-save to a `simulator_captured` dataset for immediate `data_analyze` / `fft_spectrum` / `plot_render` handoff. |
| **`compute_sim` Capabilities** | Coupled N-D `ode_ivp` via structured `state_variables` + `expressions` (backwards-compatible with single `expression` for 1D ODEs and `algebraic_grid`), selectable SciPy solver (`RK45`, `Radau`, `BDF`), and `fft_spectrum` on **both** existing run datasets (`sim_watch` / `ode_ivp` outputs) and inline expressions (with optional Hann windowing and mean detrending). All outputs are unconditionally tagged `provenance="model_computed"`. |
| **`compute_sim` Sandboxing** | In-process zero-`eval`/`exec` AST validator & evaluator with hard resource caps: `max_nfev`, monotonic `timeout_s` checked inside the ODE RHS, `max_state_dim <= 16`, `grid_points <= 50_000`, AST depth `<= 48`, expression length `<= 1000`, exponent magnitude `<= 1000.0`, and strict non-finite rejection. |
| **Viewer, Docs & Packaging** | Keep `pea-viewer` minimal (enrich `/api/runs/{run_id}` `datasets` metadata + compact **Datasets & Model Outputs** table with provenance pills and JSON download links, zero charting libraries), bump PEA & `.mcpb` to **`v0.3.0`** (`18` tools), and update [`skills/pea-lab-assistant/SKILL.md`](./skills/pea-lab-assistant/SKILL.md), [`README.md`](./README.md), and [`PLAN_V2.md`](./PLAN_V2.md). |

---

## 2. Critical Codebase Findings & Compatibility Invariants

> [!IMPORTANT]
> **HTML5 Simulator SHA-256 Fingerprinting (`src/pea/sim/engine.py`):**
> Currently `SimulatorManager.open()` only loads saved `CalibrationProfile`s when `swf_meta is not None`. M2 adds deterministic directory content hashing (`compute_html5_dir_sha256(html5_dir)`, hashing sorted relative POSIX paths and file bytes of confined files) and canonical URL hashing (`compute_url_sha256(url)` for allowlisted `url` sessions), exposed as `sim_sha256` on `SimulatorSession` and `sim_open`'s response. This allows `CalibrationProfile` auto-loading and `save_profile(..., verify_live=True)` SHA-256 verification to work identically for SWF and HTML5 simulators without modifying `CalibrationProfile` `schema_version = 1`.

1. **100% Backwards Compatibility with Phase 1 Schema Tests:**
   `tests/test_phase1_core.py` instantiates `WatchSpec(readouts=..., duration_s=..., interval_ms=..., trigger_action=...)` and `ComputeSimSpec(model_kind="algebraic_grid", expression=..., parameters=..., grid_variable=..., grid_start=..., grid_stop=...)`. Every new field on `WatchSpec`, `ComputeSimSpec`, and `MeasurementPoint` must be optional with safe defaults. `grid_start` and `grid_stop` become optional on `ComputeSimSpec` **only** when `model_kind == "fft_spectrum"` and `input_dataset_id` is supplied (enforced via `@model_validator`).
2. **Atomic Provenance & Concurrent Sequence Safety:**
   Reuse `SimulatorSession`'s Phase 3 `O_CREAT | O_EXCL` (`"xb"`) atomic claim loop for `evidence/watch-NNNN.png` and immutable `readings/watch-NNNN.json` snapshots (keeping `partial-readings.json` as an unhashed live checkpoint during `inline_ocr` mode). `src/pea/report/evidence.py` already archives `evidence/`, `datasets/`, and `readings/` into `reports/evidence.zip`.
3. **Non-Uniform Timestamp Handling in `fft_spectrum`:**
   Live `sim_watch` captures exhibit small monotonic timing jitter between browser screenshot frames. Dataset-backed `fft_spectrum` must validate strictly increasing finite timestamps (`n >= 4`), measure relative spacing jitter `max(|dt_i - dt_mean|) / dt_mean`, and if jitter exceeds `1%` (`0.01`), deterministically resample onto a uniform `n`-point grid via `np.interp` while recording `resampled: true` and `dt_s` in `ComputeSimResult.solver_meta`.
4. **Exact Version & Tool-Count Parity Touchpoints (`0.2.0` / `16` → `0.3.0` / `18`):**
   * [`pyproject.toml`](./pyproject.toml) (`version = "0.3.0"`)
   * [`src/pea/__init__.py`](./src/pea/__init__.py) (`__version__ = "0.3.0"`)
   * [`src/pea/mcp_server.py`](./src/pea/mcp_server.py) (`UNIVERSAL_TOOL_NAMES` 18 tools, docstrings, `pea://policy`)
   * [`packaging/mcpb/manifest.json`](./packaging/mcpb/manifest.json) (`version`, `long_description`, `tools[]` adding `sim_watch` & `compute_sim`)
   * [`scripts/build_mcpb.py`](./scripts/build_mcpb.py) (replace hardcoded `16 tools` output string with `len(_expected_tool_names())`)
   * [`tests/test_phase1_core.py`](./tests/test_phase1_core.py) (`pea.__version__ == "0.3.0"`)
   * [`tests/test_mcp_protocol.py`](./tests/test_mcp_protocol.py) (`len(tools_res.tools) == 18`, `policy["tools_count"] == 18`)
   * [`tests/test_sim_protocols.py`](./tests/test_sim_protocols.py) (replace `test_milestone2_protocol_stubs_raise_not_implemented` with real `sim_watch` & `compute_sim` tests)

---

## 3. Sub-Phase Implementation Breakdown (`M2A`–`M2D`)

### M2A — Frozen Schemas & Contracts (`[MODIFY] src/pea/schemas.py`)

* **`MeasurementPoint` extensions:**
  * Add `elapsed_s: float | None = Field(default=None, ge=0.0)` and `frame_timestamp_ms: float | None = Field(default=None, ge=0.0)`.
* **New Watch type aliases & models:**
  * `WatchStopOp = Literal["le", "lt", "ge", "gt"]`
  * `WatchPacingMode = Literal["burst_post_ocr", "inline_ocr"]`
  * `WatchStatus = Literal["complete", "stopped_stable", "stopped_threshold", "max_frames_reached", "cancelled"]`
  * `WatchSpec` (backwards-compatible):
    * Retain `readouts`, `duration_s`, `interval_ms`, `trigger_action`, `stop_when_stable_s`, `fixed_controls`.
    * Add `trigger_actions: list[ActionStep] = Field(default_factory=list)`, `stability_tol: float = Field(default=0.0, ge=0.0)`, `stop_readout: str | None = None`, `stop_op: WatchStopOp | None = None`, `stop_value: float | None = None`, `max_frames: int = Field(default=500, ge=1, le=2000)`, `blur_before_capture: bool = True`, `settle_ms: int = Field(default=0, ge=0, le=30_000)`, `save_dataset: bool = True`, `dataset_id: str | None = None`.
    * `@model_validator`: forbid specifying both `trigger_action` and non-empty `trigger_actions`; require all-or-none for `(stop_readout, stop_op, stop_value)` with `stop_readout in readouts`; reject non-finite floats in `duration_s`, `stability_tol`, `stop_value`, and `stop_when_stable_s`.
  * `WatchResult`:
    * `status: WatchStatus`, `pacing_mode: WatchPacingMode`, `requested_duration_s: float`, `actual_duration_s: float`, `interval_ms: int`, `captured_frames: int`, `missing_readouts: int`, `stop_reason: str`, `readings_relpath: str`, `dataset_id: str | None = None`, `dataset_relpath: str | None = None`, `points: list[MeasurementPoint]`.
* **New `ComputeSimSpec` & `ComputeSimResult` contracts:**
  * `OdeMethod = Literal["RK45", "Radau", "BDF"]`, `FftWindow = Literal["none", "hann"]`.
  * `ComputeSimSpec`:
    * `model_kind: Literal["ode_ivp", "algebraic_grid", "fft_spectrum"]`
    * `expression: str | None = None`
    * `expressions: list[str] = Field(default_factory=list)`
    * `state_variables: list[str] = Field(default_factory=list)`
    * `parameters: dict[str, float] = Field(default_factory=dict)`
    * `grid_variable: str = Field(default="t", min_length=1)`
    * `grid_start: float | None = None`, `grid_stop: float | None = None`
    * `grid_points: int = Field(default=200, ge=2, le=50_000)`
    * `initial_state: list[float] = Field(default_factory=list)`
    * `ode_method: OdeMethod = "RK45"`, `rtol: float = Field(default=1e-6, gt=0.0, le=1e-1)`, `atol: float = Field(default=1e-9, gt=0.0, le=1e-1)`
    * `max_nfev: int = Field(default=50_000, ge=10, le=250_000)`, `timeout_s: float = Field(default=5.0, gt=0.0, le=30.0)`
    * `input_dataset_id: str | None = None`, `time_column: str | None = None`, `signal_column: str | None = None`
    * `window: FftWindow = "none"`, `detrend_mean: bool = True`
    * `dataset_id: str | None = None`, `units: dict[str, str] = Field(default_factory=dict)`, `notes: str = ""`
    * `provenance: Literal["model_computed"] = "model_computed"`
    * `@model_validator` rules per `model_kind`:
      * `ode_ivp`: require finite `grid_start` & `grid_stop` (`grid_stop > grid_start`); non-empty finite `initial_state` (`len <= 16`); either single `expression` (when `len(initial_state) == 1`, defaulting `state_variables = ["y"]`) or `expressions` matching `len(initial_state)` and `state_variables` matching `len(initial_state)` with valid unique Python identifiers distinct from `grid_variable` and `parameters`.
      * `algebraic_grid`: require finite `grid_start` & `grid_stop` (`grid_stop != grid_start`); either single `expression` (output column `state_variables[0]` if provided else `"y"`) or `expressions` with matching unique valid `state_variables`.
      * `fft_spectrum`: require **either** dataset-backed (`input_dataset_id`, `time_column`, `signal_column`, forbidding `expression` / `expressions`) **or** inline (`expression` + `grid_start` & `grid_stop` with `grid_stop > grid_start`, `grid_points >= 4`).
  * `ComputeSimResult`:
    * `model_kind`, `provenance: Literal["model_computed"] = "model_computed"`, `dataset_id`, `dataset_relpath`, `dataset_sha256`, `row_count`, `columns`, `units`, `solver_meta: dict[str, Any]`, `preview_rows: list[dict[str, Any]]`.
  * Export all new models and type aliases in `src/pea/schemas.py`'s `__all__`.

---

### M2B — `sim_watch` Timed Transient Capture & HTML5 Fingerprinting (`[MODIFY] src/pea/sim/engine.py`, `src/pea/sim/protocols_m2.py`, `src/pea/sim/__init__.py`)

1. **HTML5 & URL Fingerprinting:**
   * Add `compute_html5_dir_sha256(root_dir: Path) -> str` and `compute_url_sha256(url: str) -> str` in `src/pea/sim/engine.py`.
   * Store `sim_sha256: str | None` on `SimulatorSession` (populated from `swf_meta.sha256`, `compute_html5_dir_sha256(html5_dir)`, or `compute_url_sha256(url)`).
   * Auto-load saved `CalibrationProfile` in `SimulatorManager.open()` for both SWF and `html5_dir` sessions (and validate `profile.swf_sha256 == self.sim_sha256` inside `save_profile()`).
2. **`SimulatorSession.watch(spec: WatchSpec) -> WatchResult`:**
   * **Pre-flight validation:** require `self.profile`; verify every `spec.readouts` key exists in `profile.readouts` and every `spec.fixed_controls` key exists in `profile.controls`.
   * **Setup & trigger:** apply `fixed_controls`, wait `spec.settle_ms`, optionally blur focus, execute trigger action(s) (`[spec.trigger_action]` or `spec.trigger_actions`), and record `t0 = time.perf_counter()` immediately after the trigger completes.
   * **Hybrid pacing mode:**
     * `burst_post_ocr` *(default when neither `stop_when_stable_s` nor `stop_readout` is set)*: run a drift-compensated monotonic deadline loop (`deadline = t0 + frame_idx * (interval_ms / 1000.0)`), sleep any positive delta (`asyncio.sleep`), capture raw PNG bytes (`await self.page.screenshot(type="png")`), and tag `elapsed_s = time.perf_counter() - t0` + `captured_at = datetime.now(UTC)` into memory until `elapsed_s >= duration_s`, `len(buffer) >= max_frames`, or cancellation. Then atomically write each buffered PNG via `O_CREAT | O_EXCL` (`"xb"`) into `evidence/watch-NNNN.png`, record provenance (`kind="screenshot", source="simulator_captured"`), run offline OCR per readout spec, and assemble `MeasurementPoint`s.
     * `inline_ocr` *(automatic when `stop_when_stable_s` or threshold stop is active)*: capture, atomically save/hash each frame, run OCR inline per readout, persist unhashed `partial-readings.json`, update `workspace.update_status(...)`, evaluate threshold stop (`_check_threshold`), and evaluate stability window (`_check_stable`: requires `>= 2` consecutive valid numeric frames where every watched readout stays within `stability_tol` of the stability-window anchor for `>= stop_when_stable_s`; any `None` / OCR miss resets the stability anchor so missed OCR can never falsely trigger a stable stop).
   * **Finalization, safety & dataset export:**
     * Persist immutable `readings/watch-NNNN.json` (`record_provenance=True`) on complete, early-stop, max-frames, cancellation, or error.
     * Update `status.metadata["latest_screenshot_relpath"]` to the final captured frame.
     * When `spec.save_dataset` is `True` and `len(points) > 0`, persist `DatasetSpec(source="simulator_captured")` via `data_ingest(...)` with columns `["index", "elapsed_s", "frame_timestamp_ms", *readout_cols, "screenshot_relpath", "screenshot_sha256"]` (auto-generating `dataset_id = f"watch-{seq:04d}"` when `spec.dataset_id is None`).
3. **Wire `src/pea/sim/protocols_m2.py` & `src/pea/sim/__init__.py`:**
   * Implement `sim_watch(session: SimulatorSession, spec: WatchSpec) -> WatchResult` delegating to `await session.watch(spec)`.
   * Implement `compute_sim(spec: ComputeSimSpec, run_workspace: RunWorkspace) -> ComputeSimResult` delegating to `run_compute_sim(spec, run_workspace=run_workspace)`.

---

### M2C — `compute_sim` Sandboxed Numerical / ODE / FFT Engine (`[NEW] src/pea/math_engine/compute_sim.py`, `[MODIFY] src/pea/math_engine/__init__.py`)

1. **Typed errors:**
   * `ComputeSimError(ValueError)`, `ComputeSimTimeoutError(ComputeSimError)`, `ComputeSimLimitError(ComputeSimError)`.
2. **Zero-`eval`/`exec` AST validator & dual evaluator (scalar `math` for ODE RHS, vectorized `numpy` for grids/inline FFT):**
   * Static validation (`_validate_compute_ast`): enforce expression length `<= 1000`, AST depth `<= 48`; whitelist `Expression`, `Constant` (finite int/float, non-bool), `Name` (no dunders), `UnaryOp` (`+`, `-`), `BinOp` (`+`, `-`, `*`, `/`, `**`), `Call` (direct `Name` only, no keyword arguments), and `Compare` (`<`, `<=`, `>`, `>=` single comparator permitted **only** as the first argument to `where(cond, a, b)`).
   * Whitelisted functions: `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `sinh`, `cosh`, `tanh`, `exp`, `log`, `log10`, `sqrt`, `abs`, `min`, `max`, `clip`, `heaviside`, `sign`, `floor`, `ceil`, `where`. Constants: `pi`, `e`, `euler`.
   * Runtime guards: exponent magnitude `<= 1000.0`, zero/negative-base fractional power guard, division-by-zero guard, and finite-value enforcement on all intermediate and output values.
3. **Three computational modes (all strictly `provenance="model_computed"`):**
   * **`ode_ivp`:** Builds an RHS closure checking monotonic `deadline = time.perf_counter() + spec.timeout_s` and evaluation counter `nfev <= spec.max_nfev`; executes `scipy.integrate.solve_ivp(..., method=spec.ode_method, t_eval=t_eval, rtol=spec.rtol, atol=spec.atol)`; verifies `sol.success` and finite arrays; returns rows `{grid_variable, *state_variables}` + `solver_meta = {method, nfev, njev, status, message, elapsed_ms}`.
   * **`algebraic_grid`:** Evaluates `np.linspace(grid_start, grid_stop, grid_points)` over `{grid_variable: grid, **parameters}`; verifies finite outputs; returns rows `{grid_variable, *output_cols}` + `solver_meta = {grid_points, elapsed_ms}`.
   * **`fft_spectrum`:**
     * *Dataset-backed path:* loads `spec.input_dataset_id` via `load_dataset()`, extracts finite `(t, y)` pairs (`n >= 4`, strictly increasing `t`), measures relative spacing jitter `max(|dt_i - dt_mean|) / dt_mean`; if `> 0.01`, resamples onto uniform `np.linspace(t[0], t[-1], n)` via `np.interp` and sets `resampled = True`.
     * *Inline path:* evaluates `spec.expression` on uniform `np.linspace(grid_start, grid_stop, grid_points)`.
     * *Conditioning & transform:* optional mean detrend (`detrend_mean`), optional Hann window (`np.hanning(n)` scaled by coherent amplitude gain `1.0 / np.mean(win)` so sinusoidal peak amplitudes match physical amplitude), `scipy.fft.rfft` + `scipy.fft.rfftfreq(n, d=dt)` with one-sided amplitude normalization (`2/N` interior bins, `1/N` DC/Nyquist); outputs rows `{frequency_hz, omega_rad_s, amplitude, power, phase_rad}` and populates `solver_meta` (`dominant_frequency_hz`, `dominant_omega_rad_s`, `dominant_amplitude`, `nyquist_hz`, `dt_s`, `n_samples`, `window`, `detrend_mean`, `resampled`, `input_dataset_id`).
4. **Atomic dataset persistence & compact response:**
   * Persist output via `data_ingest(..., source="model_computed")` in `run_workspace` (auto-generating unique `dataset_id = f"compute-{model_kind}-{seq:04d}"` if omitted), compute `dataset_sha256` from the written JSON file, and return `ComputeSimResult` with bounded `preview_rows` (first 5 + last 5 rows when `row_count > 10`) so MCP stdio payloads stay compact even for large grids.

---

### M2D — MCP Tools `#17–#18`, Minimal `pea-viewer` Update, Docs & `.mcpb` `v0.3.0`

* **`[MODIFY] src/pea/mcp_server.py`:**
  * Extend `UNIVERSAL_TOOL_NAMES` to **18 tools** (`sim_watch`, `compute_sim`).
  * Register `sim_watch(session_id: str, spec: WatchSpec)` and `compute_sim(spec: ComputeSimSpec, run_id: str | None = None)` with `ToolAnnotations`, strict `extra="forbid"`, path/error sanitization, and `latest_screenshot_relpath` status update after `sim_watch`.
  * Include `sim_sha256` in `sim_open`'s response (alongside `swf_sha256` for SWFs).
* **`[MODIFY] src/pea/viewer/{app.py,static/{index.html,app.js,style.css}}`:**
  * Enrich `_detail_run().datasets` entries with `source`, `row_count`, `columns`, and `notes` (reading each confined `datasets/*.json` defensively).
  * Add a compact **Datasets & Model Outputs** card (`id="datasets-card"`, `id="datasets-tbody"`) between Figures and Provenance Ledger displaying dataset ID (download link), provenance pill (`simulator_captured` / `bench_manual` / `model_computed`), row count, columns summary, and notes — zero external dependencies, responsive at `1280×800` and `390×844`.
* **`[MODIFY] pyproject.toml`, `src/pea/__init__.py`, `packaging/mcpb/manifest.json`, `scripts/build_mcpb.py`:**
  * Bump version `0.2.0` → `0.3.0`; add `sim_watch` and `compute_sim` to `manifest.json`; use `len(_expected_tool_names())` in `scripts/build_mcpb.py`.
* **`[MODIFY] skills/pea-lab-assistant/SKILL.md`, `README.md`, `PLAN_V2.md`:**
  * Document `sim_watch` (Stage 4) and `compute_sim` (Stage 5) workflows, hybrid pacing, and `model_computed` vs. `simulator_captured` provenance separation across the 18-tool reference tables and Master Plan tracker.

---

## 4. HTML5 Transient Test Fixture & Comprehensive Verification Plan

### 4.1 Deterministic HTML5 Transient Fixture (`[NEW] tests/fixtures/html5_rc_sim/index.html`)

* Self-contained, zero-network `640×420` HTML5 transient simulator served over loopback via `SimRuntimeServer` HTML5 mode:
  * **Controls:** `R` (kΩ), `C` (µF), `V0` (V), mode toggle (`RC` / `RLC`), `Reset` button, and `Start` trigger button.
  * **Crisp RapidOCR-friendly readouts** updated via `requestAnimationFrame` + `performance.now()`: stopwatch `t` (`0.00 s`) and capacitor voltage `Vc` (`10.00 V`).
  * **RC discharge mode:** $V_C(t) = V_0 e^{-t / \tau}$ (e.g., `R = 10 kΩ, C = 100 µF → τ = 1.0 s`, plus configurable steady-state floor for stability/threshold testing).
  * **Damped RLC mode:** $V(t) = V_0 e^{-\beta t}\cos(2\pi f_0 t)$ (e.g., `f0 = 2.0 Hz`, `beta = 0.25 s⁻¹`) so a ~3 s capture spans ~6 oscillation cycles for live `fft_spectrum` frequency recovery.

### 4.2 Offline Unit & Protocol Test Suite (`pytest tests/ -m "not live_sim" -v`)

| Test File | Scope Covered |
| :--- | :--- |
| **`tests/test_compute_sim.py`** *(new)* | Analytical verification for 1D RC ODE, 2D damped/driven RLC ODE, stiff `Radau`/`BDF` methods, `algebraic_grid`, inline & dataset-backed `fft_spectrum` (including jittered-timestamp resampling and Hann window coherent amplitude recovery), `model_computed` provenance & dataset immutability, and AST sandbox / `max_nfev` / `timeout_s` / non-finite rejection tests. |
| **`tests/test_sim_protocols.py`** *(update)* | Replace M2 stub test with mocked `sim_watch` tests covering `burst_post_ocr`, `inline_ocr` stability stop + OCR-miss anchor reset, threshold stop, `trigger_actions[]`, `max_frames`, cooperative cancel preserving partial evidence, auto-dataset ingest → `data_analyze` / `fft_spectrum`, and deterministic `compute_html5_dir_sha256`. |
| **`tests/test_mcp_protocol.py`** *(update)* | Assert 18-tool parity across `pea-mcp` stdio, `pea://policy`, `manifest.json`, and `SKILL.md`; verify `compute_sim` end-to-end over real `stdio_client`; test enriched `pea-viewer` `datasets` payload and path confinement. |
| **`tests/test_phase1_core.py`** *(update)* | Assert `pea.__version__ == "0.3.0"` plus backwards compatibility and `@model_validator` rejection rules for extended `WatchSpec` and `ComputeSimSpec`. |

### 4.3 Live E2E Transient Benchmark (`[NEW] tests/e2e/test_transient_m2_e2e.py`, `@pytest.mark.live_sim`)

Full workflow executed over real `mcp.client.stdio.stdio_client` against `tests/fixtures/html5_rc_sim`:

1. `sim_open(html5_dir=...)` → `sim_observe(annotate=True)` → `sim_save_profile(verify_live=True)` → confirm profile auto-loads on a subsequent `sim_open`.
2. `sim_watch` RC discharge with `trigger_action`, threshold/stable stop, and `save_dataset=True` → `data_analyze` (`ln(V)` vs `t` OLS) recovering time constant $\tau = RC$ within `< 5%`.
3. `compute_sim(model_kind="ode_ivp")` solving $dV/dt = -V/(RC)$ (`provenance="model_computed"`) and comparing against live `sim_watch` readings.
4. `sim_watch` (`burst_post_ocr`) in damped RLC mode → `compute_sim(model_kind="fft_spectrum")` recovering dominant oscillation frequency $f_0$ within FFT bin resolution.
5. `plot_render` (overlaying `simulator_captured` points with `model_computed` ODE curve, plus FFT spectrum plot) → `report_build` (A4 PDF + `.docx` + `evidence.zip` + Teacher Evidence PDF) → `report_preview` → `lab_runs(action="verify").verification.ok == True`.

### 4.4 Verification Commands & Acceptance Gates

```sh
.venv/bin/pytest tests/ -m "not live_sim" -v
.venv/bin/pytest tests/e2e/ -m live_sim -v
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/python scripts/build_mcpb.py --check
```

* Visually inspect every rendered transient report PDF preview PNG via `read_file`.
* Verify `pea-viewer` in headless Chromium at `1280×800` and `390×844` on the preserved transient run (zero horizontal overflow, zero console/page errors, zero external requests, Datasets card showing both `simulator_captured` and `model_computed` badges).
* Run tracked-file home-path hygiene audit (`0` literal host home paths in `git ls-files`).

---

## 5. Known Risks & Guardrails

1. **Burst Capture Memory & Frame Cap:** Unbounded PNG buffering at tight `interval_ms` can inflate memory; enforce `max_frames` (`default=500`, `le=2000`) cleanly and return `"max_frames_reached"` with all captured frames hashed and persisted.
2. **OCR Misses During Stability Watch:** Missing or unreadable OCR (`None`) on a transient frame must reset the stability-window anchor and never count toward `stop_when_stable_s` or be back-filled from equations.
3. **Hann Window Amplitude & Non-Uniform Sampling:** FFT spectral peaks must apply coherent Hann gain (`1 / mean(win)`) so sinusoidal amplitudes remain physically accurate, and dataset-backed FFT must deterministically resample irregular `sim_watch` timestamps (`jitter > 1%`) rather than silently distorting frequency bins.
4. **In-Process ODE Resource Protection:** Check both monotonic `timeout_s` and `nfev <= max_nfev` inside the ODE RHS closure so stiff or divergent user expressions fail fast and cleanly without hanging the MCP event loop.
5. **Strict Provenance Separation:** `sim_watch` frames and datasets are strictly `"simulator_captured"`; `compute_sim` outputs are strictly `"model_computed"`. HTML5 test fixtures must make zero external network requests.

---

## 6. Observed Milestone 2 Findings

* **Complete Delivery (`v0.3.0`, `18` Universal Tools):** Delivered all four sub-phases (`M2A` frozen schemas, `M2B` `sim_watch` + HTML5/URL SHA-256 fingerprinting, `M2C` sandboxed `compute_sim` ODE/algebraic/FFT engine + atomic `os.link` `data_ingest`, `M2D` MCP tools `#17–#18`, `pea-viewer` Datasets & Model Outputs table, `SKILL.md`/`README.md`/`PLAN_V2.md`, `dist/pea-mcp-0.3.0.mcpb`, and live HTML5 transient benchmark).
* **Empirical Live Benchmark Results (`runs/m2-qa`, `113` SHA-256 Verified Artifacts):**
  * **HTML5 Profile Fingerprinting:** `sim_open` exposed deterministic 64-char `sim_sha256`, `sim_save_profile(verify_live=True)` passed, and session reopen auto-loaded the profile (`profile_loaded=True`) with `0` aborted external requests.
  * **RC Discharge (`inline_ocr` threshold stop):** `17` frames (`120 ms` interval, `stopped_threshold` at `Vc <= 1.5 V`), OLS `ln(Vc)` vs `elapsed_s` recovered $\tau = 1.0003\text{ s}$ vs $\tau_{\text{ref}} = 1.0000\text{ s}$ (`0.03%` deviation, $R^2 = 1.0000$), and pointwise comparison against `compute_sim` RK45 ODE (`model_computed`) stayed within `0.029 V`.
  * **Damped RLC (`burst_post_ocr` + dataset-backed `fft_spectrum`):** `76` frames over `3.021 s` (`40 ms` interval) with signed positive/negative RapidOCR extraction; Hann-windowed FFT with automatic jitter resampling (`resampled=True`) recovered $f_{\text{peak}} = 1.974\text{ Hz}$ vs $2.000\text{ Hz}$ (`1.32%` deviation, within bin resolution).
* **Calibration Pitfall Confirmed:** RapidOCR `rec_only` crops must tightly match single-line text height (`~44 px` for `36 px` text) rather than including wide vertical whitespace margins (`80 px`), which can shrink and drop decimal dots (`4.80` → `480`).
* **Acceptance Gate Summary:** `206/206` offline tests + `3/3` live E2E benchmarks pass (`209/209`, `0` skipped); all `4` transient PDF preview pages visually inspected; `pea-viewer` verified in headless Chromium at `1280×800` and `390×844` (`0` overflow/errors/external requests); `ruff check src/ tests/ scripts/` clean (`0` errors); `scripts/build_mcpb.py --check` passed for `v0.3.0` (`18` tools); tracked-file home-path hygiene audit returned `0` matches.
