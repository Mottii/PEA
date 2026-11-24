# M2B Implementation Plan — `sim_watch` Timed Transient Capture & HTML5 Fingerprinting

> **Status:** `Complete (2026-11-24)`
> **Parent Plan:** [`MILESTONE_2_PLAN.md`](./MILESTONE_2_PLAN.md)
> **Prerequisite:** M2A complete (frozen `WatchSpec`, `WatchResult`, `ComputeSimSpec`, `ComputeSimResult`, and `MeasurementPoint` timing contracts in [`src/pea/schemas.py`](./src/pea/schemas.py); `ruff` clean; `scripts/build_mcpb.py --check` clean).

---

## 1. Scope & Sub-Phase Boundaries

### In Scope for M2B
1. **Deterministic HTML5 & URL SHA-256 Fingerprinting (`src/pea/sim/engine.py`):**
   * Add `compute_html5_dir_sha256(root_dir: str | Path) -> str` and `compute_url_sha256(url: str) -> str`.
   * Propagate `sim_sha256` on `SimulatorSession`, `SimulatorManager.open()`, and `SimulatorSession.save_profile()` so `CalibrationProfile` auto-loading and live SHA-256 verification work uniformly for SWF, `html5_dir`, and allowlisted `url` sessions without altering `CalibrationProfile.schema_version == 1`.
2. **Timed Transient & Stopwatch Capture Engine (`SimulatorSession.watch`):**
   * Implement `SimulatorSession.watch(spec: WatchSpec) -> WatchResult` with hybrid pacing (`burst_post_ocr` vs. `inline_ocr`), drift-compensated monotonic timing (`time.perf_counter()`), threshold & stability early stops, atomic `O_CREAT | O_EXCL` (`"xb"`) evidence/readings persistence, cooperative & `asyncio.Task` cancellation safety, and `simulator_captured` dataset auto-export via `data_ingest(...)`.
3. **Protocol & Package Wiring (`src/pea/sim/protocols_m2.py`, `src/pea/sim/__init__.py`):**
   * Wire `sim_watch(session: SimulatorSession, spec: WatchSpec | dict[str, Any]) -> WatchResult` to validate `spec` and delegate to `await session.watch(validated)`.
   * Export `compute_html5_dir_sha256` and `compute_url_sha256` from `src/pea/sim/engine.py` and `src/pea/sim/__init__.py`.
4. **Offline Unit & Mocked Protocol Tests (`tests/test_sim_protocols.py`):**
   * Add comprehensive tests covering HTML5/URL fingerprinting, HTML5 `save_profile` SHA-256 checks, `burst_post_ocr`, `inline_ocr` stability & threshold stops, OCR-miss anchor resets, `trigger_actions[]`, `max_frames`, `save_dataset=False`, cooperative/task/error cancellation recovery, `data_ingest` → `data_analyze` handoff, and `protocols_m2.sim_watch` delegation.

### Out of Scope for M2B *(Deferred to M2C / M2D)*
* `compute_sim` numerical/ODE/FFT engine (`src/pea/math_engine/compute_sim.py`) and wiring `protocols_m2.compute_sim` → **M2C** (`protocols_m2.compute_sim` continues to raise `NotImplementedError("compute_sim is scheduled for Milestone 2")` until M2C so existing stub checks stay green).
* MCP tool `#17` (`sim_watch`) & `#18` (`compute_sim`) registration, `sim_open` MCP response `sim_sha256` field, `pea-viewer` Datasets card, docs/skills updates, and `0.2.0` → `0.3.0` version bump → **M2D**.
* Live HTML5 RC/RLC fixture (`tests/fixtures/html5_rc_sim/index.html`) and `@pytest.mark.live_sim` E2E transient benchmark → **M2D / E2E Verification**.

---

## 2. Detailed Design by Component

### 2.1 HTML5 & URL SHA-256 Fingerprinting (`[MODIFY] src/pea/sim/engine.py`)

1. **`compute_html5_dir_sha256(root_dir: str | Path) -> str`:**
   * Resolve `root = Path(root_dir).expanduser().resolve()`; raise `FileNotFoundError` if `root` does not exist or is not a directory.
   * Walk `root` with `os.walk(root, followlinks=False)` using sorted `dirnames` and `filenames` for deterministic traversal order.
   * For every discovered file, compute its relative POSIX path `rel_posix = (Path(dirpath) / filename).relative_to(root).as_posix()` and validate it with `resolve_confined_path(root, rel_posix)` (from `pea.config`):
     * Escaping symlinks fail closed with `PathConfinementError`.
     * Confined internal symlinks resolve safely inside `root`.
   * Require at least 1 file (raise `ValueError` if the directory is empty).
   * Sort `(rel_posix, resolved_path)` pairs lexicographically by `rel_posix` and hash with domain-separated, length-prefixed framing:
     * Header: `b"pea-html5-dir-v1\x00"`
     * Per entry: `f"{len(rel_bytes)}:".encode("ascii") + rel_bytes + b"\x00"` followed by `f"{len(file_bytes)}:".encode("ascii") + file_bytes + b"\x00"`.
   * Return lowercase 64-char hex digest (`hashlib.sha256(...).hexdigest()`).

2. **`compute_url_sha256(url: str) -> str`:**
   * Parse with `urlsplit(url.strip())`; require `scheme in ("http", "https")` and non-empty `hostname` (raise `ValueError` otherwise).
   * Canonicalize the URL:
     * Lowercase `scheme` and `hostname`.
     * Strip default ports (`80` for `http`, `443` for `https`), preserve explicit non-default ports, and bracket IPv6 hostnames properly (`f"[{host}]"` when `":" in host`).
     * Normalize empty path `""` to `"/"`.
     * Preserve `query`; drop `fragment`.
   * Hash `b"pea-url-v1\x00" + canonical_url.encode("utf-8")` with SHA-256 and return `.hexdigest()`.

3. **`SimulatorSession` & `SimulatorManager.open()` Integration:**
   * Add optional `sim_sha256: str | None = None` to `SimulatorSession.__init__`:
     * Default `self.sim_sha256 = sim_sha256 if sim_sha256 is not None else (swf_meta.sha256 if swf_meta is not None else None)` so existing callers/tests constructing `SimulatorSession` directly remain 100% backwards-compatible.
   * Update `SimulatorSession.save_profile()` to validate `profile.swf_sha256 == expected_sha` against `self.sim_sha256` whenever `self.sim_sha256 is not None` (falling back to `self.swf_meta.sha256` if only `swf_meta` is set). This covers SWF, `html5_dir`, and `url` sessions without modifying `CalibrationProfile.schema_version = 1`.
   * Update `SimulatorManager.open()` to compute `sim_sha256` across all three simulator modes:
     * `swf_path`: `swf_meta.sha256`
     * `html5_dir`: `compute_html5_dir_sha256(html5_dir)`
     * `url`: `compute_url_sha256(url)`
   * Auto-load any matching saved profile via `self.profile_store.load(sim_sha256, expected_stage=(stage_w, stage_h))` (already safe for SWF and `html5_dir`/`url`) and pass `sim_sha256=sim_sha256` to `SimulatorSession(...)`.

---

### 2.2 Atomic Frame Helper & `SimulatorSession.watch(spec: WatchSpec) -> WatchResult` (`[MODIFY] src/pea/sim/engine.py`)

1. **Shared Atomic Frame Helper (`_write_and_record_frame`):**
   * Extract `_write_and_record_frame(self, png_bytes: bytes, *, name_prefix: str, metadata: dict[str, Any] | None = None) -> tuple[Image.Image, str, str]` from `capture_frame()`.
   * Reuses the existing `O_CREAT | O_EXCL` (`"xb"`) atomic sequence claim loop under `evidence/{name_prefix}-{seq:04d}.png` and calls `self.workspace.record_artifact(relpath, kind="screenshot", metadata=metadata, source="simulator_captured")`.
   * Both `capture_frame()` and `watch()` share this helper so provenance invariants and concurrent sequence safety remain identical.

2. **Pre-Flight Validation *(before touching the simulator page)*:**
   * If `spec` is not already a `WatchSpec`, validate with `WatchSpec.model_validate(spec)`.
   * Require `profile = self._require_profile()`.
   * Verify every key in `spec.fixed_controls` exists in `profile.controls`.
   * Verify every name in `spec.readouts` exists in `profile.readouts`.
   * Verify every `step.control` referenced in `spec.trigger_action` or `spec.trigger_actions` exists in `profile.controls`.
   * If `spec.save_dataset` and `spec.dataset_id is not None`, check that `self.workspace.resolve_path(f"datasets/{spec.dataset_id}.json")` does **not** already exist; fail fast with `FileExistsError` / `AnalysisError`-equivalent before running a multi-second live capture.

3. **Pacing Mode Selection:**
   * Select `"inline_ocr"` whenever `spec.stop_when_stable_s is not None` or `spec.stop_readout is not None`.
   * Otherwise select `"burst_post_ocr"` for minimum frame-to-frame timing jitter.

4. **Setup & Trigger Sequence:**
   * Call `self.reset_cancel()` and `self.workspace.update_status(phase="running", source="simulator_captured", ...)`.
   * Apply `spec.fixed_controls` in order via `await self._apply_single_control(ctrl_name, val)`.
   * If `spec.settle_ms > 0`, wait `await self._sleep_ms(spec.settle_ms)`.
   * If `spec.blur_before_capture` is `True`, call `await self._blur_focus(blur_target)` **before** executing the trigger so `t0` reflects zero post-trigger blur latency.
   * Execute triggers (`[spec.trigger_action]` if `spec.trigger_action is not None` else `spec.trigger_actions`) via `await self.act(triggers)` when non-empty.
   * Record monotonic reference `t0 = time.perf_counter()` immediately after the trigger completes.

5. **Drift-Compensated Capture Loops:**
   * Both pacing loops schedule `deadline = t0 + frame_idx * (spec.interval_ms / 1000.0)` and perform cancel-aware waits using `asyncio.wait_for(self._cancel_event.wait(), timeout=sleep_s)` so `request_cancel()` wakes the loop immediately without waiting a full `interval_ms`.
   * Frame `0` captures immediately (`elapsed_s` near `0.0`); subsequent iterations stop cleanly when:
     * `self._cancel_event.is_set()` → `status = "cancelled"`,
     * `len(captured) >= spec.max_frames` → `status = "max_frames_reached"`, or
     * `(time.perf_counter() - t0) >= spec.duration_s` → `status = "complete"`.
   * **`burst_post_ocr` mode:**
     * Captures raw PNG bytes (`await self.page.screenshot(type="png")`) and tags `(png_bytes, elapsed_s, captured_at)` into an in-memory buffer bounded by `spec.max_frames` (`<= 2000`).
     * Records `actual_duration_s = max(0.0, time.perf_counter() - t0)` right when capture finishes (so post-OCR time does not inflate `actual_duration_s`).
     * Then iterates over buffered frames to atomically write/hash `evidence/watch-NNNN.png`, run `self.ocr.read_spec(...)` for each readout in `spec.readouts`, and construct `MeasurementPoint`s with `elapsed_s=elapsed_s` and `frame_timestamp_ms=elapsed_s * 1000.0`.
   * **`inline_ocr` mode:**
     * Captures raw PNG bytes, immediately atomically writes/hashes `evidence/watch-NNNN.png`, runs OCR per readout, builds `MeasurementPoint`, appends to `points`, persists unhashed `partial-readings.json` (`record_provenance=False`), updates `self.workspace.update_status(..., metadata_updates={"latest_screenshot_relpath": relpath})`, checks threshold stop, and updates the stability tracker.
     * Records `actual_duration_s = max(0.0, time.perf_counter() - t0)` on loop exit.

6. **Early-Stop Semantics & Edge Cases:**
   * `_as_finite_float(value: Any) -> float | None`: rejects `bool`, `None`, non-numeric strings, and non-finite floats; converts `int`/`float` (or numeric string if encountered) to finite `float`.
   * `_check_threshold(point: MeasurementPoint, spec: WatchSpec) -> bool`:
     * Extracts `val = _as_finite_float(point.readouts.get(spec.stop_readout))`.
     * Returns `False` immediately if `val is None` (OCR misses never trigger a threshold stop).
     * Evaluates `le` (`<=`), `lt` (`<`), `ge` (`>=`), `gt` (`>`) against `spec.stop_value`.
   * `_StabilityTracker`:
     * Requires `>= 2` consecutive valid numeric frames where **every** readout in `spec.readouts` stays within `spec.stability_tol + 1e-9` of the stability-window anchor value for `(elapsed_s - anchor_elapsed_s) >= spec.stop_when_stable_s - 1e-9`.
     * Any `None` or non-numeric OCR miss on **any** watched readout resets the stability window (`anchor_values = None`, `anchor_elapsed_s = None`, `valid_count = 0`) so missed OCR can never falsely trigger `"stopped_stable"`.
     * *Usage note:* Because a ticking stopwatch readout (`t`) continuously increases, callers using `stop_when_stable_s` should either watch only the settling channel (e.g., `readouts=["Vc"]`, relying on monotonic `elapsed_s`) or use threshold stop (`stop_readout="Vc"`, `stop_op="le"`, `stop_value=...`).
   * `missing_readouts`: exact count of `None` readout slots across all `(point, ro_name)` pairs (`sum(1 for pt in points for ro in spec.readouts if pt.readouts.get(ro) is None)`).

7. **Finalization, Dataset Export & Cancellation/Error Recovery:**
   * **Normal exit (`complete`, `stopped_stable`, `stopped_threshold`, `max_frames_reached`, or cooperative `cancelled`):**
     * Persist immutable `readings/watch-NNNN.json` via `self._persist_partial_readings(kind="watch", status=status, points=points, extra={...}, record_provenance=True)`.
     * When `points` is non-empty, update `status.metadata["latest_screenshot_relpath"]` to `points[-1].screenshot_relpath`.
     * When `spec.save_dataset` is `True` and `len(points) > 0`:
       * Build rows with columns `["index", "elapsed_s", "frame_timestamp_ms", *spec.readouts, "screenshot_relpath", "screenshot_sha256"]`.
       * Build `units = {"elapsed_s": "s", "frame_timestamp_ms": "ms", **{ro: unit for ... if non-empty}}`.
       * Choose `dataset_id = spec.dataset_id` if provided; otherwise scan `self.workspace.datasets_dir` for the next non-colliding `f"watch-{seq:04d}"`.
       * Persist via `data_ingest(DatasetSpec(dataset_id=..., source="simulator_captured", rows=rows, units=units, notes=...), self.workspace)` so `provenance.jsonl` logs the dataset SHA-256 and `dataset_relpath = f"datasets/{dataset_id}.json"`.
     * Update `self.workspace.update_status(phase="cancelled" if status == "cancelled" else "complete", ...)`.
     * Return `WatchResult(...)` satisfying `captured_frames == len(points)` and paired `(dataset_id, dataset_relpath)`.
   * **External `asyncio.CancelledError` or unexpected `Exception`:**
     * In `burst_post_ocr`, flush any already-captured buffered PNGs through `_write_and_record_frame` + OCR into `points` before persisting, so zero captured frames are lost or left unhashed.
     * Persist `readings/watch-NNNN.json` (`status="cancelled"` or `"failed"`, `record_provenance=True`).
     * Update `self.workspace.update_status(phase="cancelled" / "failed", ...)` and re-raise.

---

### 2.3 Protocol & Package Wiring (`[MODIFY] src/pea/sim/protocols_m2.py`, `src/pea/sim/__init__.py`)

* **`src/pea/sim/protocols_m2.py`:**
  * Implement `async def sim_watch(session: SimulatorSession, spec: WatchSpec | dict[str, Any]) -> WatchResult`:
    * Validate `spec` if not already `WatchSpec`, then return `await session.watch(validated)`.
  * Keep `def compute_sim(spec: ComputeSimSpec, *args: Any, **kwargs: Any) -> NoReturn` raising `NotImplementedError("compute_sim is scheduled for Milestone 2")` until **M2C**.
* **`src/pea/sim/engine.py` & `src/pea/sim/__init__.py`:**
  * Export `compute_html5_dir_sha256` and `compute_url_sha256` in both `__all__` lists.

---

## 3. Test Plan (`[MODIFY] tests/test_sim_protocols.py`)

Add offline unit and mocked-session protocol tests covering every new behavior and edge case:

1. **`compute_html5_dir_sha256` tests:** determinism across equivalent copies, file-content and filename/rename sensitivity, confined internal symlink support, escaping symlink rejection (`PathConfinementError`), and empty/missing directory errors.
2. **`compute_url_sha256` tests:** canonicalization invariance (uppercase host/scheme, explicit `:80`/`:443` default port stripping, empty path vs. `/`, fragment removal), query/path sensitivity, and non-HTTP(S) scheme rejection.
3. **HTML5/URL `save_profile` SHA-256 verification:** `SimulatorSession` with `sim_sha256` (no `swf_meta`) validates `profile.swf_sha256 == sim_sha256` on `save_profile()` and rejects mismatched SHA-256 digests.
4. **`sim_watch` `burst_post_ocr` happy path & `data_analyze` handoff:** verifies `pacing_mode == "burst_post_ocr"`, `status == "complete"`, monotonic `elapsed_s` & `frame_timestamp_ms`, unique hashed `evidence/watch-NNNN.png`, `readings/watch-NNNN.json`, `latest_screenshot_relpath`, auto-saved `simulator_captured` dataset ingested cleanly by `data_analyze(...)`, and `verify_provenance().ok == True`.
5. **`sim_watch` `inline_ocr` stability stop + OCR-miss anchor reset:** verifies `pacing_mode == "inline_ocr"`, injected OCR miss (`None`) resets the stability window and increments `missing_readouts` without fabricating values, and subsequent stable frames trigger `status == "stopped_stable"`.
6. **`sim_watch` `inline_ocr` threshold stop (`le`/`lt`/`ge`/`gt`) + OCR-miss guard:** verifies `None` readout never falsely triggers threshold stop and crossing `stop_value` triggers `status == "stopped_threshold"`.
7. **Setup, `trigger_actions[]`, `settle_ms`, and `blur_before_capture` ordering:** asserts `fixed_controls` → `settle_ms` → `blur` → `trigger_actions[]` execution order prior to `t0`.
8. **`max_frames` cap & `save_dataset=False`:** verifies `status == "max_frames_reached"` at cap and `dataset_id is None` / `dataset_relpath is None` when `save_dataset=False`.
9. **Cooperative `request_cancel()`, external `asyncio.Task.cancel()`, and mid-burst exception recovery:** verifies immediate wakeup on cancel, flushing of buffered `burst_post_ocr` frames to `evidence/` and `readings/`, workspace `phase` (`"cancelled"` / `"failed"`), and `verify_provenance().ok == True`.
10. **`protocols_m2.sim_watch(session, spec)` delegation:** verifies `sim_watch(session, spec)` returns `WatchResult` while keeping `compute_sim` stub verification passing until M2C.

---

## 4. Verification Commands & Acceptance Gates

Run after M2B implementation completes:

```sh
.venv/bin/pytest tests/ -m "not live_sim" -v
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/python scripts/build_mcpb.py --check
```

Acceptance gates:
* All offline tests pass with zero regressions.
* `ruff check src/ tests/ scripts/` exits with `0` errors.
* `scripts/build_mcpb.py --check` reports clean parity (`0.2.0` / 16 tools until M2D).

---

## 5. Known Pitfalls & Guardrails

1. **`actual_duration_s` Timing Accuracy:** Measure `actual_duration_s = time.perf_counter() - t0` immediately when frame capture ends in `burst_post_ocr`, before offline OCR runs, so OCR latency never inflates the reported capture duration.
2. **Cancel-Aware Inter-Frame Sleep:** Wait between deadlines via `asyncio.wait_for(self._cancel_event.wait(), timeout=sleep_s)` rather than bare `asyncio.sleep(sleep_s)` so `request_cancel()` aborts immediately.
3. **Burst Buffer Flush on Interrupt/Failure:** If `asyncio.CancelledError` or an unexpected error hits mid-burst, flush all buffered PNG bytes via `_write_and_record_frame` + OCR before writing `readings/watch-NNNN.json` so zero captured evidence is lost or unhashed.
4. **Pre-Flight Dataset Collision Check & Auto-ID Uniqueness:** Validate explicit `spec.dataset_id` availability before capture starts, and scan existing `datasets/watch-*.json` files when auto-generating `watch-NNNN` IDs so consecutive `sim_watch` calls in the same run workspace never collide.
5. **`WatchResult` Contract Compliance:** Always ensure `captured_frames == len(points)` and `(dataset_id is None) == (dataset_relpath is None)`, including when `captured_frames == 0` (no dataset written).

---

## 6. Observed M2B Findings

* **HTML5 & URL SHA-256 Fingerprinting (`compute_html5_dir_sha256`, `compute_url_sha256`):** Verified deterministic content/path hashing with strict `resolve_confined_path` escaping-symlink rejection, canonical URL normalization, and seamless `CalibrationProfile` auto-reload on HTML5 sessions without changing `schema_version == 1`.
* **Hybrid `sim_watch` Pacing & Safety Hardening:** Both `burst_post_ocr` and `inline_ocr` modes, stability anchor resets on `None` OCR misses, threshold operators (`le`/`lt`/`ge`/`gt`), reserved column collision checks, cancel-aware inter-frame waits, and mid-burst exception/cancellation flush passed all mocked protocol tests in `tests/test_sim_protocols.py` and live HTML5 E2E verification (`17` inline RC frames + `76` burst RLC frames at `40 ms` interval, `100%` SHA-256 verified provenance).
