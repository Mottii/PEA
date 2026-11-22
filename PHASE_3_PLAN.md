# Phase 3 Implementation Plan — Automated Verification & Live Dual-Lab Benchmarks

> **Status:** `Complete — Dual-Lab Live E2E Verified (Lab 5 + Lab 4), Visual QA Passed, .mcpb Audited & Viewer Verified`
> **Parent Plan:** [`PLAN_V2.md`](./PLAN_V2.md)
> **Prerequisite:** Phase 2 complete (`ffa509d` on clean `main`; `132/132` offline tests pass; `ruff` clean; `scripts/build_mcpb.py --check` passing).

---

## 1. Objective

Prove end-to-end that the **exact same zero-hardcoded, zero-API-cost `pea-mcp` stdio server** executes two completely distinct university physics laboratories live inside sandboxed Ruffle `0.7.1` + headless Chromium + offline RapidOCR ONNX, compiles submission-ready A4 PDFs, editable `.docx` reports, raw `evidence.zip` archives, and Teacher Evidence Packet PDFs, passes visual QA on every rendered PDF page, audits cleanly as a `.mcpb` bundle, and renders cleanly in `pea-viewer` on desktop and mobile viewports.

---

## 2. Pre-Benchmark Hardening Fixes

1. **`src/pea/sim/engine.py`:**
   * Fall back to `resolve_browser_executable()` in `SimulatorManager._ensure_browser()` whenever `self.config.browser_executable is None`.
   * Load bundled `local_assets/fonts/NotoSans-Regular.ttf` (with graceful `ImageFont.load_default()` fallback) in `render_annotated_overlay()` and pass it from `SimulatorSession.observe()` so Cyrillic OCR tags render crisply instead of bitmap boxes.
2. **`src/pea/report/pdf_builder.py`:**
   * Fall back to `resolve_browser_executable()` in `render_html_to_pdf()` whenever `cfg.browser_executable is None`.
3. **`src/pea/viewer/app.py` + `tests/test_mcp_protocol.py`:**
   * Confine `latest_screenshot_relpath` inside `_summarize_run()` (and normalize in `_detail_run()`) via `workspace.resolve_path(...)` catching `PathConfinementError`, matching `_detail_run()`.
   * Add regression test coverage in `tests/test_mcp_protocol.py` asserting traversal/symlink/outside `latest_screenshot_relpath` metadata values never leak through `/api/runs` or `/api/runs/{run_id}`.

---

## 3. Live Dual-Lab E2E Benchmarks (`tests/e2e/`, `@pytest.mark.live_sim`)

Both benchmarks connect to `pea-mcp` over real `mcp.client.stdio.stdio_client` (zero hardcoded lab tools inside `src/pea/`), support optional `PEA_E2E_KEEP_DIR` to preserve outputs under gitignored `runs/` for visual QA and `pea-viewer` inspection, and verify `lab_runs(action="verify").verification.ok == True`.

### 3.1 Lab 5 Live E2E Benchmark (`tests/e2e/test_lab5_e2e.py`)
* **Simulator & Variant:** `local_assets/sims/m96220.swf` (`800×600`), Team 2 ($L = 5.0\text{ mH}, C = 32\text{ µF}, \omega_0 = 2500\text{ rad/s}$, $R \in \{3.2, 4.0, 5.0, 6.0\}\,\Omega$).
* **Workflow Over `pea-mcp` stdio:**
  1. `lab_inspect_pdf` on `local_assets/benchmarks/lab5-rlc-handout.pdf` (8 pages, verifying Team 2 parameters & Table 2 structure).
  2. `sim_open` + `sim_observe(annotate=True)` + `sim_save_profile(verify_live=True)` for `m96220.swf`.
  3. Coarse-to-fine `sim_search_extremum(mode="plateau_max")` starting from $\omega_0 = 2500\text{ s}^{-1}$ across all 4 resistors:
     * Verify exact discrete plateaus: $R=3.2 \to [2445, 2472]$ (`3.94`), $R=4.0 \to [2427, 2444]$ (`3.17`), $R=5.0 \to [2361, 2434]$ (`2.55`, 74-step wide plateau), $R=6.0 \to [2333, 2370]$ (`2.15`).
  4. `calc_eval` for $\omega_0$, ideal $Q$ and $\omega_{\text{res}}$ benchmarks, plus worked Problems 3, 4A–C, and 5 from the handout.
  5. `data_ingest` + `data_analyze` on the 4-resistor summary (computing $\omega_{\text{res}}, \Delta\omega, \omega_0/\Delta\omega, Q^2$, free/zero-intercept OLS of $\omega_0/\Delta\omega$ vs $Q^2$, and comparison against theoretical slope $4$).
  6. `diagram_render` & `plot_render`:
     * Redrawn Fig. 1 RLC circuit schematic (`schemdraw`) using handout notation ($\varepsilon_0, U_{0C}, U_{0L}, U_{0R}$).
     * $\omega_0/\Delta\omega$ vs $Q^2$ plot with OLS fit & $4Q^2$ comparison.
     * Live resonance trace plot ($U_{0C}/\varepsilon_0$ vs $\omega$).
     * Series RLC phasor diagram at resonance.
     * Problem 4C $q$–$I$ phase-portrait ellipse (`equal_aspect=True`).
  7. `report_build` compiling A4 PDF + `.docx` + `evidence.zip` + Teacher Evidence Packet PDF (with Table 1, Table 2, opt-in Self-Control Q&As `#1, #3, #36`, and worked Problems `3, 4A–C, 5`), followed by `report_preview` and `lab_runs(action="verify")`.

### 3.2 Lab 4 Live E2E Benchmark (`tests/e2e/test_lab4_e2e.py`)
* **Simulators & Variant:** `local_assets/sims/m96210.swf` (`500×450`, straight wire) + `local_assets/sims/m96211.swf` (`700×500`, circular loop, $R = 3\text{ cm}$), Team 2 ($I \in \{-5, -10, -15, -20\}\text{ A}$, $r \in \{2, 3, 4, 5, 6, 7, 8, 9, 10\}\text{ cm}$; 72 total readings).
* **Workflow Over `pea-mcp` stdio:**
  1. `sim_open` + `sim_observe(annotate=True)` + `sim_save_profile(verify_live=True)` for both `m96210.swf` and `m96211.swf` (`decimal_comma=False`, `commit_mode="step_interior"`).
  2. `sim_sweep` for 36 wire readings (`m96210`) and 36 loop readings (`m96211`, reading unclipped `Bx` and `By` with `By == 0`).
  3. Assert **72/72 exact match** against `local_assets/benchmarks/lab4-measurements.json`, and derive `B = sqrt(Bx**2 + By**2)` via `data_analyze`.
  4. Compute linearized transforms (`inv_r = 1 / r_m` and `geom_factor = (R_m**2 + r_m**2)**(-1.5)`), grouped zero-intercept OLS slopes per current, $\mu_0$ recovery via `calc_eval` / `data_analyze`, and assert `< 0.5%` deviation from $\mu_0 = 4\pi \times 10^{-7}\text{ T}\cdot\text{m/A}$.
  5. Render linearized plots ($B$ vs $1/r$ and $B$ vs $(R^2+r^2)^{-3/2}$) and magnetic field diagram (`diagram_render(kind="field")`).
  6. `report_build` compiling A4 PDF + `.docx` + `evidence.zip` + Teacher Evidence Packet PDF with handout-compliant Tables 2 & 3 (including transformed rows) and an explicit scientific-honesty note (repeatability check against prior automated pilot; independent manual reference remains pending), followed by `report_preview` and `lab_runs(action="verify")`.

---

## 4. Visual QA, `.mcpb` Audit & Viewer Verification

1. Visually inspect **every** rendered PDF preview page PNG for both Lab 5 and Lab 4 via `read_file`; fix any typography, KaTeX, figure, or pagination defects found.
2. Run `.venv/bin/python scripts/build_mcpb.py --check` to rebuild and audit `dist/pea-mcp-0.2.0.mcpb`.
3. Launch `pea-viewer` against the preserved Phase 3 benchmark runs and verify desktop (`1280×800`) and mobile (`390×844`) viewports in headless Chromium (zero horizontal overflow, zero console/page errors, zero external requests, working OCR box & annotated overlay toggles).
4. Run full verification commands:
   * `.venv/bin/pytest tests/ -m "not live_sim" -v`
   * `.venv/bin/pytest tests/e2e/ -m live_sim -v`
   * `.venv/bin/ruff check src/ tests/ scripts/`
   * Tracked-file home-path hygiene audit (zero literal host home paths in `git ls-files`)

---

## 5. Observed Phase 3 Findings *(Populated After Execution)*

* **Date Executed:** `2026-11-22`
* **Pre-Benchmark Hardening (`src/pea/sim/engine.py`, `src/pea/report/pdf_builder.py`, `src/pea/viewer/app.py`):**
  * Added `resolve_browser_executable()` fallback to `SimulatorManager._ensure_browser()` and `render_html_to_pdf()` when `browser_executable is None`.
  * Loaded bundled `local_assets/fonts/NotoSans-Regular.ttf` (with `ImageFont.load_default()` fallback) in `render_annotated_overlay()` so Cyrillic OCR tags (`Установка`, `Параметры системы`, `Магнитное поле кругового витка с током`) render cleanly without bitmap boxes.
  * Confined and normalized `latest_screenshot_relpath` inside `_summarize_run()` and `_detail_run()` in `src/pea/viewer/app.py` via `workspace.resolve_path(...)`, covered by a traversal/symlink regression test in `tests/test_mcp_protocol.py`.
* **Real Production Bugs Caught & Fixed by Live Dual-Lab E2E Benchmarks:**
  1. **OCR Box-Split Decimal Separator (`src/pea/ocr/base.py`):** Live `m96211.swf` sweep at `I = -15 A, r = 4 cm` produced adjacent `det_rec` boxes `'B X = -67'` and `',86 мкТл'` joined as `'B X = -67 ,86 мкТл'`. Updating `_NUMBER_RE` / `_LOOSE_NUMBER_RE` to allow optional whitespace before `[.,]\d+` (`(?:\s*[.,]\d+)?`, never matching CSV `"1, 2"`) raised live Lab 4 accuracy to **72/72 (100%)**.
  2. **Flex `FlaconNumericStepper` Decimal-Comma Stale-Physics Trap (`m96220.swf`):** Empirically proved that although `FlaconNumericStepper` *displays* Russian decimal commas (`3,2`, `5,0`), its internal AS3 `Number(text)` input parser expects dot-decimals (`decimal_comma=False`). Typing `"3,2"` left the visual box showing `3,2` while silently clamping internal physics to `R = 3.0 Ω` (`U_C/U = 4,19` instead of `3,93`) on the next arrow step! Using `decimal_comma=False` updates internal physics accurately while the Flex stepper auto-formats the display back to `3,2`.
  3. **Logical-Prefix Resolution Round-Trip (`src/pea/mcp_server.py`):** `sim_save_profile` returned logical `"profiles/local/<sha256>.json"`, which failed inside `report_build(profile_paths=[...])` when `PEA_PROFILES_DIR` was overridden outside `PEA_PROJECT_ROOT`. Teaching `_resolve_confined_input` to map allowed logical prefixes (`profiles/`, `runs/`, `local_assets/`) while enforcing `resolve_confined_path` fixes the round-trip while still blocking `profiles/../../secret.txt`.
  4. **Multi-Session & Multi-Search SHA-256 Provenance Immutability (`src/pea/sim/engine.py`, `src/pea/report/evidence.py`):** Sequential simulator sessions sharing one `run_id` (`m96210` + `m96211`) previously reset `_capture_seq = 0` and overwrote earlier evidence PNGs, and repeated `sweep`/`search_extremum` calls overwrote provenance-hashed `partial-readings.json`. Scanning `evidence/`, `crops/`, and `ocr/` for the highest existing sequence number and persisting immutable terminal snapshots to `readings/{kind}-{seq:04d}.json` (archived in `evidence.zip`) while keeping `partial-readings.json` as an unhashed live checkpoint made `lab_runs(action="verify").verification.ok == True` pass cleanly across all 301 Lab 5 artifacts and 96 Lab 4 artifacts.
  5. **Orphaned Q&A Header Pagination (`src/pea/report/templates/report.css`):** Visual QA of Lab 5 PDF Page 6 caught `Question 36 + Answer:` stranded at the bottom of Page 6 because `.qa-answer-label` was an inline `<span>` (where Chromium ignores `break-after: avoid`). Setting `display: block; break-after: avoid;` kept Question 36 intact on Page 7.
* **Live Dual-Lab Benchmark Results (Real `pea-mcp` over `mcp.client.stdio.stdio_client`):**
  * **Lab 5 (`tests/e2e/test_lab5_e2e.py`, `m96220.swf`, Team 2):**
    * 8-page PDF intake verified; live SHA-256 calibration verified.
    * Exact discrete `plateau_max` plateaus reproduced across all 4 resistors: `R=3.2 → [2445, 2472]` (`3.94`, 28 steps), `R=4.0 → [2427, 2444]` (`3.17`, 18 steps), `R=5.0 → [2361, 2434]` (`2.55`, 74-step wide plateau), `R=6.0 → [2333, 2370]` (`2.15`, 38 steps).
    * Zero-intercept OLS of $\omega_0/\Delta\omega$ vs $Q^2$: slope `3.849 ± 0.036` ($R^2 = 0.99856$, `-3.78%` vs asymptotic `4.000`).
    * Worked Problems `3, 4A–C, 5` verified via `calc_eval` (`18` ledger calculations), Self-Control Q&As `#1, #3, #36`, 5 vector figures (including redrawn Fig. 1 `schemdraw` circuit with $\varepsilon_0, U_{0C}, U_{0L}, U_{0R}$ and Problem 4C `equal_aspect=True` $q$–$I$ ellipse), 9-page A4 PDF visually verified page-by-page, `.docx`, `evidence.zip`, Teacher Evidence Packet PDF, and `lab_runs(action="verify").ok == True` (`301` hashed artifacts).
  * **Lab 4 (`tests/e2e/test_lab4_e2e.py`, `m96210.swf` `500×450` + `m96211.swf` `700×500`, Team 2):**
    * **72/72 exact repeatability match** against `local_assets/benchmarks/lab4-measurements.json` (36 wire `B` + 36 loop unclipped `Bx`, `By == 0` with `B = sqrt(Bx^2 + By^2)` derived via `data_analyze`), with explicit scientific-honesty disclaimer that independent manual reference verification remains pending.
    * Zero-intercept OLS recovers $\mu_0$ within `< 0.5%` on all 8 curves (`wire mean -0.013%`, `loop mean -0.000%`, $R^2 > 0.9999$).
    * Handout-compliant Tables 2 & 3 with transformed rows (`1/r`, `(R^2+r^2)^(-3/2)`), linearized plots + magnetic field diagram, 5-page A4 PDF visually verified page-by-page, `.docx`, `evidence.zip`, Teacher Evidence Packet PDF, and `lab_runs(action="verify").ok == True` (`96` hashed artifacts).
* **`.mcpb` Audit & `pea-viewer` Verification:**
  * `.venv/bin/python scripts/build_mcpb.py --check` passed (`v0.2.0`, manifest `v0.3`, 16 tools, zero binaries/secrets/traversals).
  * `pea-viewer` verified in headless Chromium at `1280×800` and `390×844` against the real preserved Phase 3 benchmark runs: both runs visible with `SHA-256 Verified (301)` / `SHA-256 Verified (96)`, 4 download links each, zero horizontal overflow, zero console/page errors, zero external network requests.

