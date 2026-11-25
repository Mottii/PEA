# M2C Implementation Plan — `compute_sim` Sandboxed Numerical, ODE & FFT Engine

> **Status:** `Complete (2026-11-24)`
> **Parent Plan:** [`MILESTONE_2_PLAN.md`](./MILESTONE_2_PLAN.md)
> **Prerequisite:** M2A & M2B complete (frozen `ComputeSimSpec` / `ComputeSimResult` contracts in [`src/pea/schemas.py`](../../src/pea/schemas.py); `sim_watch` & HTML5 fingerprinting in [`src/pea/sim/engine.py`](../../src/pea/sim/engine.py); `ruff` clean; `scripts/build_mcpb.py --check` clean).

---

## 1. Scope & Sub-Phase Boundaries

### In Scope for M2C
1. **Sandboxed Numerical / ODE / FFT Engine (`[NEW] src/pea/math_engine/compute_sim.py`):**
   * Implement typed error hierarchy (`ComputeSimError`, `ComputeSimTimeoutError`, `ComputeSimLimitError`).
   * Implement strict zero-`eval`/`exec` AST static validator (`_validate_compute_ast`) and dual scalar/vectorized evaluator (`_eval_scalar` for ODE RHS, `_eval_vectorized` for `algebraic_grid` and inline `fft_spectrum`) with lazy-branch `where(cond, a, b)` semantics.
   * Implement all three computational modes (`ode_ivp`, `algebraic_grid`, `fft_spectrum`), unconditionally tagged `provenance="model_computed"`.
   * Persist results atomically as immutable `model_computed` datasets via `data_ingest(...)` and return compact `ComputeSimResult` payloads (`preview_rows` bounded to at most 10 rows).
2. **Package & Protocol Wiring (`[MODIFY] src/pea/math_engine/__init__.py`, `src/pea/sim/protocols_m2.py`):**
   * Export `run_compute_sim`, `ComputeSimError`, `ComputeSimTimeoutError`, and `ComputeSimLimitError` from `pea.math_engine`.
   * Replace the stub `compute_sim(spec, run_workspace)` in [`src/pea/sim/protocols_m2.py`](../../src/pea/sim/protocols_m2.py) to validate `ComputeSimSpec` and delegate to `run_compute_sim(validated, run_workspace=run_workspace)`.
3. **Offline Unit & Protocol Test Suite (`[NEW] tests/test_compute_sim.py`, `[MODIFY] tests/test_sim_protocols.py`):**
   * Create comprehensive unit tests in [`tests/test_compute_sim.py`](../../tests/test_compute_sim.py) covering analytical 1D/2D ODEs, stiff solvers, algebraic grids, inline & dataset-backed FFTs, provenance immutability, and AST sandbox / resource-cap rejections.
   * Update `test_protocols_m2_sim_watch_delegation_and_compute_sim_stub` in [`tests/test_sim_protocols.py`](../../tests/test_sim_protocols.py) to verify real `compute_sim(spec, run_workspace)` delegation.

### Out of Scope for M2C *(Deferred to M2D)*
* Registering MCP tools `#17–#18` (`sim_watch`, `compute_sim`) in [`src/pea/mcp_server.py`](../../src/pea/mcp_server.py) and `packaging/mcpb/manifest.json`.
* Adding the **Datasets & Model Outputs** UI card in `pea-viewer` (`src/pea/viewer/`).
* Updating [`skills/pea-lab-assistant/SKILL.md`](../../skills/pea-lab-assistant/SKILL.md), [`README.md`](../../README.md), and [`PLAN_V2.md`](./PLAN_V2.md), bumping version `0.2.0` → `0.3.0`, and running the live HTML5 RC/RLC E2E benchmark (`tests/e2e/test_transient_m2_e2e.py`).

---

## 2. Detailed Design by Component

### 2.1 Typed Errors & Resource Constants (`[NEW] src/pea/math_engine/compute_sim.py`)

1. **Error Hierarchy:**
   * `ComputeSimError(ValueError)`: base error for invalid expressions, domain errors, solver convergence failures, or invalid dataset inputs.
   * `ComputeSimTimeoutError(ComputeSimError)`: raised when monotonic wall-clock execution exceeds `spec.timeout_s`.
   * `ComputeSimLimitError(ComputeSimError)`: raised when expression length (`> 1000`), AST depth (`> 48`), exponent magnitude (`> 1000.0`), or ODE RHS evaluation count (`> spec.max_nfev`) exceeds hard caps.

2. **Sandbox & Naming Constants:**
   * `_MAX_EXPRESSION_LEN = 1000`
   * `_MAX_AST_DEPTH = 48`
   * `_MAX_EXPONENT_ABS = 1000.0`
   * `_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e, "euler": math.e}`
   * `_COMPUTE_DATASET_RE_TEMPLATE = r"^compute-{model_kind}-(\d+)\.json$"` (scoped strictly per `model_kind` so unrelated datasets in `datasets/` never skew auto-increment numbering).

---

### 2.2 Zero-`eval`/`exec` AST Sandbox & Dual Evaluator (`[NEW] src/pea/math_engine/compute_sim.py`)

1. **Whitelisted Functions & Exact Arities:**
   * **1-arg:** `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `sinh`, `cosh`, `tanh`, `exp`, `log`, `log10`, `sqrt`, `abs`, `sign`, `floor`, `ceil`.
   * **1-or-2-arg:** `heaviside(x)` (default midpoint value `h0 = 0.5`) and `heaviside(x, h0)`.
   * **2-arg:** `atan2(y, x)`.
   * **2+-arg (pairwise reduction):** `min`, `max`.
   * **3-arg:** `clip(x, lo, hi)` (rejects `lo > hi`), `where(cond, a, b)`.

2. **Static AST Validation (`_validate_compute_ast(expr: str, allowed_names: set[str]) -> ast.Expression`):**
   * Enforce `len(expr) <= _MAX_EXPRESSION_LEN` (raise `ComputeSimLimitError` if exceeded).
   * Parse with `ast.parse(expr, mode="eval")` (raise `ComputeSimError` on `SyntaxError`).
   * Walk AST enforcing `depth <= _MAX_AST_DEPTH` (raise `ComputeSimLimitError` if exceeded).
   * Whitelist only:
     * `ast.Expression`
     * `ast.Constant` (finite `int` or `float`, non-`bool`)
     * `ast.Name` (no dunders, must exist in `allowed_names | _CONSTANTS.keys()`)
     * `ast.UnaryOp` (`ast.UAdd`, `ast.USub`)
     * `ast.BinOp` (`ast.Add`, `ast.Sub`, `ast.Mult`, `ast.Div`, `ast.Pow`)
     * `ast.Call` with direct `ast.Name` callee (`node.func`), **zero** keyword arguments (`not node.keywords`), **zero** starred arguments (`not isinstance(arg, ast.Starred)`), and validated arity per whitelisted function.
     * `ast.Compare` with a **single** comparator (`len(node.ops) == 1` and `len(node.comparators) == 1`) using `<`, `<=`, `>`, or `>=` — permitted **only** as the first argument (`args[0]`) to `where(cond, a, b)`. Any bare comparison elsewhere or chained comparison raises `ComputeSimError`.

3. **Dual Evaluator with Lazy-Branch `where(cond, a, b)`:**
   * **Variable Resolution Precedence:** check runtime variables (`grid_variable`, `state_variables`, `parameters`) before `_CONSTANTS` (`pi`, `e`, `euler`), matching `calculator.py`.
   * **Scalar `math` Evaluator (`_eval_scalar` for `ode_ivp` RHS):**
     * Evaluates `cond` first and evaluates **only** the taken branch (`a` when `cond` is true, `b` when false) so guarded expressions like `where(t > 0, 1 / t, 0)` never fault on inactive points.
   * **Vectorized `numpy` Evaluator (`_eval_vectorized` for `algebraic_grid` and inline `fft_spectrum`):**
     * Evaluates `cond_mask` as a 1D boolean `np.ndarray` of length `n`, then evaluates `a` only over the sliced environment where `cond_mask` is `True` and `b` only over the sliced environment where `~cond_mask` is `True` (matching scalar lazy-branch semantics so inactive branches never raise division-by-zero or domain errors).
     * Broadcasts scalar/constant expressions cleanly into 1D `np.float64` arrays of length `n`.

4. **Runtime Arithmetic & Domain Guards (Both Evaluators):**
   * Wrap `numpy` vectorized ops in `with np.errstate(all="ignore"):` so hardware warnings stay silent and explicit guards report deterministic errors:
     * Division by zero → `ComputeSimError`.
     * Exponent magnitude `abs(exp) > _MAX_EXPONENT_ABS` (`1000.0`) → `ComputeSimLimitError`.
     * Zero base raised to negative power (`0 ** -k`) → `ComputeSimError`.
     * Negative base raised to non-integer exponent → `ComputeSimError`.
     * Domain/overflow failures (`log(-1)`, `sqrt(-1)`, `asin(2)`, `exp(1000)`) or any non-finite (`NaN`/`inf`) intermediate or final value → `ComputeSimError`.

---

### 2.3 Computational Modes (All Strictly `provenance="model_computed"`)

1. **`ode_ivp` Mode:**
   * Construct uniform output grid `t_eval = np.linspace(spec.grid_start, spec.grid_stop, spec.grid_points, dtype=np.float64)`.
   * Validate all expression ASTs upfront against `{spec.grid_variable, *state_vars, *spec.parameters.keys()}`.
   * Build closure `rhs(t: float, y: np.ndarray) -> list[float]` with monotonic deadline `deadline = start_perf + spec.timeout_s` and counter `rhs_calls`:
     * Increment `rhs_calls`; if `time.perf_counter() > deadline`, raise `ComputeSimTimeoutError`.
     * If `rhs_calls > spec.max_nfev`, raise `ComputeSimLimitError`.
     * Verify `t` and every component of `y` are finite floats (raise `ComputeSimError` on solver divergence/non-finite state).
     * Evaluate each state expression via `_eval_scalar` and return `list[float]`.
   * Invoke `scipy.integrate.solve_ivp(rhs, (spec.grid_start, spec.grid_stop), np.asarray(spec.initial_state, dtype=np.float64), method=spec.ode_method, t_eval=t_eval, rtol=spec.rtol, atol=spec.atol)`.
   * Verify `sol.success`, `sol.y.shape == (len(state_vars), len(t_eval))`, and `np.all(np.isfinite(sol.y))` (raise `ComputeSimError` otherwise).
   * Build rows with columns `[spec.grid_variable, *state_vars]` and `solver_meta = {"method": spec.ode_method, "nfev": int(sol.nfev), "njev": int(sol.njev), "nlu": int(sol.nlu), "rhs_calls": rhs_calls, "status": int(sol.status), "message": str(sol.message), "rtol": spec.rtol, "atol": spec.atol, "elapsed_ms": elapsed_ms}`.

2. **`algebraic_grid` Mode:**
   * Construct `grid = np.linspace(spec.grid_start, spec.grid_stop, spec.grid_points, dtype=np.float64)`.
   * Determine output expressions/columns:
     * Single `spec.expression` → column `spec.state_variables[0]` if provided else `"y"`.
     * Multi `spec.expressions` → columns `spec.state_variables`.
   * Validate all ASTs upfront against `{spec.grid_variable, *spec.parameters.keys()}`.
   * Evaluate each expression via `_eval_vectorized`, checking monotonic `deadline` between expressions and enforcing finite length-`grid_points` outputs.
   * Build rows with columns `[spec.grid_variable, *out_cols]` and `solver_meta = {"grid_points": spec.grid_points, "elapsed_ms": elapsed_ms}`.

3. **`fft_spectrum` Mode:**
   * **Dataset-backed path (`spec.input_dataset_id is not None`):**
     * Load input dataset via `load_dataset(spec.input_dataset_id, run_workspace)`.
     * Verify both `spec.time_column` and `spec.signal_column` are present across dataset columns (raise `ComputeSimError` if missing).
     * Extract finite `(t, y)` pairs:
       * Skip rows where `row.get(signal_column) is None` (e.g., OCR misses from `sim_watch`), incrementing `dropped_missing_rows`.
       * Reject `None` time values, `bool` values, non-numeric strings, or non-finite floats (`NaN`/`inf`) with `ComputeSimError`.
     * Require `len(t) >= 4` and strictly increasing timestamps (`np.all(np.diff(t) > 0)`).
     * Compute `dts = np.diff(t)`, `dt_mean = float(np.mean(dts))`, `jitter_ratio = float(np.max(np.abs(dts - dt_mean)) / dt_mean)`.
     * If `jitter_ratio > 0.01`, resample deterministically onto `t_uniform = np.linspace(t[0], t[-1], n, dtype=np.float64)` via `y = np.interp(t_uniform, t, y)`, set `resampled = True`, and set `dt_s = float((t[-1] - t[0]) / (n - 1))`; otherwise set `resampled = False` and `dt_s = dt_mean`.
   * **Inline path (`spec.expression is not None`):**
     * Evaluate `spec.expression` over `np.linspace(spec.grid_start, spec.grid_stop, spec.grid_points, dtype=np.float64)` (`n >= 4`), setting `dt_s = float((spec.grid_stop - spec.grid_start) / (n - 1))`, `resampled = False`, `jitter_ratio = 0.0`, `dropped_missing_rows = 0`.
   * **Conditioning & One-Sided `rfft` Normalization:**
     * Optional mean detrend: `y_work = y - np.mean(y)` if `spec.detrend_mean` else `y.copy()`.
     * Optional Hann window: if `spec.window == "hann"`, `win = np.hanning(n)` with coherent amplitude gain `gain = 1.0 / float(np.mean(win))` applied (`y_work = y_work * win * gain`) so sinusoidal peak amplitudes recover physical amplitudes.
     * Compute `freqs = scipy.fft.rfftfreq(n, d=dt_s)` and `spec_c = scipy.fft.rfft(y_work)`.
     * Normalize one-sided amplitude spectrum:
       * `amplitude = np.abs(spec_c) * (2.0 / n)`
       * `amplitude[0] *= 0.5` (DC bin `1/N`)
       * If `n % 2 == 0`: `amplitude[-1] *= 0.5` (Nyquist bin `1/N` for even `n`; odd `n` has no Nyquist bin so last bin stays `2/N`).
     * Compute `power = amplitude ** 2`, `phase_rad = np.angle(spec_c)`, and `omega_rad_s = 2.0 * math.pi * freqs`.
     * Dominant peak (`dominant_frequency_hz`, `dominant_omega_rad_s`, `dominant_amplitude`): argmax over `amplitude[1:]` when `len(freqs) > 1` (excluding DC bin 0).
     * Output columns `["frequency_hz", "omega_rad_s", "amplitude", "power", "phase_rad"]`, default units `{"frequency_hz": "Hz", "omega_rad_s": "rad/s", "phase_rad": "rad"}` (plus `"amplitude"` unit inherited from input dataset `signal_column` when available, and `"^2"` for `"power"`, overridden by `spec.units`), and full `solver_meta` (`dominant_frequency_hz`, `dominant_omega_rad_s`, `dominant_amplitude`, `nyquist_hz`, `dt_s`, `n_samples`, `window`, `detrend_mean`, `resampled`, `jitter_ratio`, `dropped_missing_rows`, `input_dataset_id`, `elapsed_ms`).

---

### 2.4 Atomic Dataset Persistence, Provenance & Compact `preview_rows`

1. **Pre-Flight Dataset Collision Check:**
   * If `spec.dataset_id is not None`, check `run_workspace.resolve_path(f"datasets/{spec.dataset_id}.json").exists()` **before** running any computation and raise `FileExistsError` immediately.
2. **Scoped Auto-ID Generation (`^compute-{model_kind}-(\d+)\.json$`):**
   * Scan `run_workspace.resolve_path("datasets")` strictly for files matching `^compute-{model_kind}-(\d+)\.json$` (so unrelated files like `watch-0001.json` or `lab5-2026.json` never skew sequence numbers).
   * Loop `seq += 1` with `data_ingest(..., source="model_computed")` catching `FileExistsError` on concurrent calls until persisted atomically.
3. **SHA-256 & Bounded Preview:**
   * Compute `dataset_sha256, _ = sha256_file(run_workspace.resolve_path(dataset_relpath))` directly from the written JSON artifact.
   * Populate `preview_rows = rows` if `len(rows) <= 10` else `rows[:5] + rows[-5:]`, keeping MCP stdio responses compact even when `grid_points == 50_000`.
   * Return `ComputeSimResult(model_kind=spec.model_kind, provenance="model_computed", dataset_id=..., dataset_relpath=..., dataset_sha256=..., row_count=len(rows), columns=columns, units=units, solver_meta=solver_meta, preview_rows=preview_rows)`.

---

### 2.5 Package & Protocol Wiring (`[MODIFY] src/pea/math_engine/__init__.py`, `src/pea/sim/protocols_m2.py`)

* **`src/pea/math_engine/__init__.py`:**
  * Import and export `ComputeSimError`, `ComputeSimLimitError`, `ComputeSimTimeoutError`, and `run_compute_sim` in `__all__`.
* **`src/pea/sim/protocols_m2.py`:**
  * Replace the `compute_sim` stub with:
    ```python
    def compute_sim(
        spec: ComputeSimSpec | dict[str, Any],
        run_workspace: RunWorkspace,
    ) -> ComputeSimResult:
        """Run sandboxed numerical, ODE, or FFT modeling via ``run_compute_sim``."""
        validated = (
            spec if isinstance(spec, ComputeSimSpec) else ComputeSimSpec.model_validate(spec)
        )
        return run_compute_sim(validated, run_workspace=run_workspace)
    ```

---

## 3. Test Plan (`[NEW] tests/test_compute_sim.py`, `[MODIFY] tests/test_sim_protocols.py`)

### 3.1 `tests/test_compute_sim.py` *(8 Test Groups)*
1. **1D RC Discharge ODE (`ode_ivp`):** Solve $dV_C/dt = -V_C/(RC)$ via shorthand `expression` (`state_variables == ["y"]`) and custom `state_variables=["Vc"]`; verify analytical match $V_0 e^{-t/(RC)}$ within tight tolerance (`< 1e-5`) and verify `provenance == "model_computed"`.
2. **2D Coupled Damped/Driven RLC ODE (`ode_ivp`):** Solve $dq/dt = i$, $di/dt = -(R/L)i - q/(LC) + (V_0/L)\cos(\omega t)$ and verify exact agreement with closed-form analytical underdamped / steady-state solution.
3. **Stiff ODE Solvers (`Radau`, `BDF`):** Solve stiff Robertson / rapid-decay system (`k1 = 1e4`) with `ode_method="Radau"` and `"BDF"`, verifying `sol.success` (`status == 0`), accuracy, and `solver_meta["method"]`.
4. **`algebraic_grid` Mode:** Test single expression, descending grid (`grid_start > grid_stop`), multi-expression parametric curves, constant/scalar expressions (e.g., `"5.0"`), and lazy `where(t > 0, 1.0 / t, 0.0)` with `t[0] == 0.0` (verifying inactive branch never raises division-by-zero), plus `heaviside`, `clip`, `sign`, `floor`, `ceil`, `min`, `max`, `atan2`, and `euler`.
5. **Inline `fft_spectrum` Mode:** Test multi-tone signal with both `window="none"` and `window="hann"` across even and odd `grid_points`; verify `dominant_frequency_hz`, `dominant_omega_rad_s`, coherent Hann peak amplitude recovery (`< 2%`), DC/Nyquist bin scaling, `detrend_mean=True` vs `False`.
6. **Dataset-Backed `fft_spectrum` Mode:** Test uniform dataset, irregular/jittered `sim_watch`-like timestamps (`jitter > 1%` → `resampled=True` recovering exact $f_0$), and dataset rows containing `None` OCR misses (`dropped_missing_rows` tracked), plus rejections (`< 4` valid samples, non-monotonic timestamps, missing columns, non-numeric values).
7. **Provenance, Immutability, Auto-ID & Bounded `preview_rows`:** Verify `verify_provenance().ok == True`, `dataset_sha256` matches written file, `model_computed` source tag in `provenance.jsonl`, pre-flight `FileExistsError` on duplicate explicit `dataset_id`, `compute-{model_kind}-NNNN` auto-increment isolated from unrelated datasets, and `len(preview_rows) == 10` (`first 5 + last 5`) when `row_count == 100`.
8. **AST Sandbox, Resource Caps & Runtime Rejection Suite:** Verify rejection of `__import__`, attribute access (`x.real`), subscript (`x[0]`), lambda, list comprehension, keyword args, starred args, bare comparison outside `where()`, chained comparison `0 < t < 1`, length `> 1000`, depth `> 48`, exponent `> 1000`, `0 ** -1`, `(-1) ** 0.5`, `clip(t, 5, 1)`, `div/0`, `log(-1)`, non-finite blowup, `max_nfev` exceeded (`ComputeSimLimitError`), and `timeout_s` exceeded (`ComputeSimTimeoutError`).

### 3.2 `tests/test_sim_protocols.py` *(Update)*
* Update `test_protocols_m2_sim_watch_delegation_and_compute_sim_stub` → `test_protocols_m2_sim_watch_and_compute_sim_delegation`, verifying `protocols_m2.compute_sim(compute_spec, run_workspace)` returns a verified `ComputeSimResult` tagged `"model_computed"` and updates `provenance.jsonl`.

---

## 4. Verification Commands & Acceptance Gates

```sh
.venv/bin/pytest tests/test_compute_sim.py tests/test_sim_protocols.py tests/test_phase1_core.py -k "not legacy_backup and not runtime_server" -v
.venv/bin/pytest tests/ -m "not live_sim" -v
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/python scripts/build_mcpb.py --check
```

Acceptance gates:
* All new `tests/test_compute_sim.py` and updated `tests/test_sim_protocols.py` tests pass.
* `ruff check src/ tests/ scripts/` exits with `0` errors.
* `scripts/build_mcpb.py --check` passes cleanly (`0.2.0` / 16 tools until M2D).

---

## 5. Known Pitfalls & Guardrails

1. **Lazy-Branch Evaluation in Vectorized `where(cond, a, b)`:** Evaluating both `a` and `b` over the full array before `np.where` causes spurious division-by-zero or domain errors on masked-out elements (e.g., `where(t > 0, sin(t) / t, 1.0)` at `t = 0`). Slice the evaluation environment by `cond_mask` and `~cond_mask` and only evaluate each branch when its mask has at least one `True` element.
2. **Even vs. Odd `N` Nyquist Bin in One-Sided `rfft`:** `rfft` of length `N` returns `N // 2 + 1` bins; the last bin is the Nyquist frequency (`1/N` scaling) **only** when `N` is even. When `N` is odd, the last bin is an interior frequency (`2/N` scaling). Scaling `amplitude[-1] *= 0.5` unconditionally would halve the highest frequency bin on odd-length inputs.
3. **Tracking `rhs_calls` Across `Radau`/`BDF` Jacobians:** `scipy.integrate.solve_ivp` counts `sol.nfev` internally and returns it only after finishing; checking both `time.perf_counter() > deadline` and `rhs_calls > spec.max_nfev` inside the RHS closure guarantees fast termination even during stiff finite-difference Jacobian evaluations.
4. **Scoped `compute-{model_kind}-NNNN` Auto-Increment:** Scan `datasets/` strictly with `^compute-{re.escape(model_kind)}-(\d+)\.json$` so `watch-0001.json` or `compute-algebraic_grid-0050.json` never skews `compute-ode_ivp-0001`.
5. **Scalar Constant Broadcasting & Pre-Flight Dataset Check:** Expressions with no `grid_variable` reference (e.g. `"5.0"` or `"2 * pi"`) must broadcast to a 1D `np.float64` array of length `grid_points` in `_eval_vectorized`, and explicit `spec.dataset_id` collisions must fail before running expensive ODE integrations.

---

## 6. Observed M2C Findings

* **Sandboxed Zero-`eval`/`exec` Engine (`src/pea/math_engine/compute_sim.py`):** Verified analytical accuracy across 1D RC ODE, 2D coupled damped/driven RLC ODE, stiff `Radau`/`BDF` systems, vectorized `algebraic_grid` with lazy-branch `where(cond, a, b)`, and inline + dataset-backed one-sided `fft_spectrum` (including Hann coherent amplitude gain, even/odd Nyquist bin scaling, missing-row dropping, and automatic uniform resampling when spacing jitter `> 1%`).
* **Atomic Provenance & Sandbox Caps:** Hardened `data_ingest` with atomic `os.link` publication (preserving provenance immutability under concurrent dataset creation) and verified all AST sandbox, depth (`<= 48`), length (`<= 1000`), exponent (`<= 1000.0`), `max_nfev`, and monotonic `timeout_s` guards in `tests/test_compute_sim.py`. Live E2E confirmed max RK45 ODE residual `0.029 V` against live RC OCR and `1.974 Hz` vs `2.000 Hz` (`1.32%` dev, `resampled=True`) on live damped RLC capture.
