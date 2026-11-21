---
name: pea-lab-assistant
description: Universal physics laboratory assistant workflow for the zero-API-cost pea-mcp server. Guides PDF handout inspection, team/variant confirmation, SHA-256 simulator calibration, verified data acquisition (sweeps and extremum/plateau searches), dimensional physics math, vector plots/schematics, submission-ready PDF/DOCX compilation, and visual QA.
---

# PEA v2 Lab Assistant Skill

Use this skill whenever executing a university physics laboratory experiment, analyzing bench or simulator data, or compiling a physics lab report through the **`pea-mcp`** MCP server.

**Core Operating Principle:** PEA v2 is a **zero-API-cost deterministic local toolkit**. You (the host AI agent) provide multimodal handout reading, experimental planning, visual coordinate calibration, and scientific prose. **`pea-mcp`** executes all browser/simulator interaction, offline ONNX OCR, dimensional calculations, OLS regressions, vector rendering, PDF/DOCX compilation, and SHA-256 provenance tracking.

---

## Non-Negotiable Scientific Integrity & Security Guardrails

1. **Never fabricate or back-fill readings.** If a simulator point fails OCR or control verification (`parsed_value is None` or `controls_matched is False`), re-probe or re-calibrate — **never** substitute values from theoretical equations.
2. **Preserve provenance separation.** Explicitly tag every dataset, table, and figure as:
   - `simulator_captured` — directly observed from a live simulator frame via OCR,
   - `bench_manual` — physical bench measurements or handout tables supplied by the user, or
   - `model_computed` — theoretical formulas, fits, schematics, or numerical models.
3. **Zero mental arithmetic.** Run every formula, unit conversion, complex AC impedance, 3D vector operation, and uncertainty propagation through **`calc_eval`** (attached to `run_id` so every result receives an immutable `calc_id` in `calculations.jsonl`), and run every column transformation and linear regression through **`data_analyze`**.
4. **Never claim independent manual verification automatically.** Record only what was actually measured and verified; leave any manual instructor/student sign-off blank unless explicitly confirmed by the user.
5. **Respect loopback & path confinement.** Local SWF/HTML5 simulators run strictly on loopback (`127.0.0.1`) with Ruffle `allowScriptAccess: false` and `allowNetworking: "none"`. Never attempt path traversal (`..`) or external URLs not listed in `PEA_ALLOWED_ORIGINS`.

---

## End-to-End 8-Stage Laboratory Workflow

### Stage 0 — Check Policy & Existing Runs
1. Read the read-only resource **`pea://policy`** to verify active security limits, OCR backend, and `allowed_origins`.
2. Call **`lab_runs(action="list")`** to check for existing runs or interrupted runs with preserved partial evidence before creating a new workspace.

### Stage 1 — Handout Intake (`lab_inspect_pdf`)
1. Call **`lab_inspect_pdf(pdf_path=..., dpi=180)`** on the lab handout PDF inside the workspace.
2. Inspect the returned `full_text` **and** visually view the rendered PNG pages (`pdf_pages/page-NNN.png`) to capture circuit diagrams, apparatus figures, tables, parameter variants, required linearized plots, self-control questions, and additional problems.

### Stage 2 — User Confirmation Gate (Before Running Experiments)
Before acquiring data or compiling the final report, confirm with the user:
- **Team / Variant parameters** (e.g., coil dimensions, $L$, $C$, $R$ set, wire currents, distance grid).
- **Report language** (`en` or `ru`) and metadata (`author`, `team`, `course`, `institution`, `date_str`).
- **Output formats** (`pdf`, `docx`, `evidence_zip`, and whether `include_teacher_packet=True` is requested).
- **Opt-in report sections** (`options.include_self_control`, `options.include_problems`, `options.include_evidence_appendix`).

### Stage 3 — Simulator Boot & SHA-256 Calibration (`sim_open`, `sim_observe`, `sim_act`, `sim_save_profile`)
1. Call **`sim_open(swf_path=..., run_id=...)`** (or `html5_dir=...` / allowlisted `url=...`).
2. Check `profile_loaded`:
   - If **`profile_loaded == True`**, reuse the verified SHA-256 `CalibrationProfile` immediately after a quick sanity **`sim_observe(session_id, readouts=[...])`**.
   - If **`profile_loaded == False`**, calibrate visually:
     1. Call **`sim_observe(session_id, annotate=True)`** and inspect the annotated coordinate-grid PNG (`crops/annotated-NNNN.png`) plus OCR bounding boxes.
     2. Identify every interactive control (`input_xy`, `up_xy`, `down_xy`, `click_xy`, `readout_roi_px`, `min_val`, `max_val`, `step`, `decimal_comma`) and every readout ROI (`roi_px`, `ocr_mode`, `pad_px`, `scale`, `parse_mode`, `unit_pattern`).
     3. Probe control commit behavior with **`sim_act`** before committing to a full sweep:
        - **`commit_mode="enter"`**: click → select-all → type → `Enter` → blur (commits even at `min_val`/`max_val`).
        - **`commit_mode="step_interior"`**: type `target ± step` inside bounds and click `down_xy`/`up_xy` (handles Flex steppers that only emit change events on arrow click, avoiding boundary clamping at `max_val`).
        - **`commit_mode="blur"`**: click → select-all → type → click inert `blur_xy`.
     4. Always define an inert **`blur_xy`** (e.g., on the fixed top header bar) so blinking text carets (`|`) are dismissed before OCR capture.
     5. Persist and live-verify via **`sim_save_profile(session_id, profile, verify_live=True)`**.

### Stage 4 — Verified Data Acquisition (`sim_sweep`, `sim_search_extremum`, or `data_ingest`)
- **N-dimensional parameter grids:** Call **`sim_sweep(session_id, spec=SweepSpec(...))`** with `verify_controls=True` and `blur_before_capture=True`. Inspect `controls_matched_all` and `missing_readouts`.
- **Peaks, minima, thresholds, or wide discrete plateaus:** Call **`sim_search_extremum(session_id, spec=SearchSpec(...))`**:
  - For resonance plateaus where discrete readouts remain tied across many steps (e.g., 50–80+ steps), use `mode="plateau_max"`, set `max_steps >= 120`, and set `stop_after_drop >= 2`.
  - Use the returned `PlateauResult` (`low_bound`, `high_bound`, `midpoint`, `peak_value`, `width_steps`, `boundary_screenshots`, `trace`).
- **Physical bench measurements:** Ingest CSV/JSON or inline rows via **`data_ingest(run_id=..., source="bench_manual", ...)`**.
- Always close finished browser sessions cleanly with **`sim_close(session_id)`**.

### Stage 5 — Deterministic Data Analysis & Physics Math (`data_ingest`, `data_analyze`, `calc_eval`)
1. Ingest simulator readings into a structured dataset via **`data_ingest(run_id=..., source="simulator_captured", rows=..., units=...)`**.
2. Call **`data_analyze`** to compute:
   - Safe AST derived columns (e.g., `1/r`, `(R**2 + r**2)**(-1.5)`, `(w1 + w2)/2`, `w0 / dw`, `Q**2`).
   - Explicit **`intercept="free"`** or **`intercept="zero"`** OLS regressions (recording `slope`, `slope_se`, `intercept`, `intercept_se`, `r_squared`, `n_points`).
   - Reference `comparisons` (`abs_diff`, `percent_deviation`).
3. Call **`calc_eval(..., run_id=run_id)`** for every derived physical quantity, theoretical reference, complex AC impedance, vector calculation, and Gaussian uncertainty propagation. Record every returned **`calc_id`** for citation in `ReportSpec`.

### Stage 6 — Publication-Grade Vector Visuals (`plot_render`, `diagram_render`)
- Call **`plot_render`** to generate vector `.svg` + 300 DPI `.png` plots (linearized fits with regression annotations, resonance curves, error bars, or `equal_aspect=True` phase portraits such as $q$–$I$ ellipses).
- Call **`diagram_render`** to generate redrawn `schemdraw` circuit schematics (`kind="circuit"`), phasor/impedance/waveform diagrams (`kind="phasor"`), or magnetic/electric field diagrams (`kind="field"`).

### Stage 7 — Submission-Ready Report Compilation (`report_build`)
Call **`report_build(run_id=..., spec=ReportSpec(...), formats=..., include_teacher_packet=...)`**:
- Populate clean academic prose (`objective`, `apparatus`, `theory_sections`, `procedure`, `analysis_paragraphs`, `conclusions`) with standard LaTeX math (`$...$` and `$$...$$`, including multi-line `\begin{aligned}...\end{aligned}`).
- Include only verified `tables`, `figures`, and `calc_ids`.
- Populate `self_control` and `problems` only when confirmed by the user (and set the matching `options.include_*` toggles).

### Stage 8 — Mandatory Visual QA & Provenance Audit (`report_preview`, `lab_runs`)
1. Inspect every preview PNG returned by `report_build` (or generate targeted renders with **`report_preview`**):
   - Confirm zero clipped or raw unrendered KaTeX equations.
   - Confirm clean English/Cyrillic glyph rendering, balanced pagination, non-overlapping annotations, and intact tables/figures.
   - If any layout defect appears, fix `ReportSpec` or figure layout and rebuild with a clean versioned artifact or fresh run.
2. Run **`lab_runs(action="verify", run_id=...)`** and confirm `verification.ok == True` before delivering final outputs to the user.

---

## Tool Reference (`pea-mcp` — 16 Universal Tools + 1 Resource)

| Category | Name | Purpose |
| :--- | :--- | :--- |
| **Policy** | `pea://policy` *(resource)* | Read-only runtime security, OCR, and scientific integrity policy |
| **Handout & Runs** | `lab_inspect_pdf` | Render handout PDF pages to PNG + extract page-indexed text |
| **Handout & Runs** | `lab_runs` | `list`, `get`, `create`, or `verify` run workspaces & SHA-256 provenance |
| **Simulator** | `sim_open` | Open local SWF (Ruffle), local HTML5 dir, or allowlisted URL |
| **Simulator** | `sim_observe` | Capture raw hashed PNG, run offline OCR, optional annotated grid overlay |
| **Simulator** | `sim_act` | Click, double-click, drag, type, key, wait, blur, or `set_stepper` |
| **Simulator** | `sim_save_profile` | Live-verify and save SHA-256 `CalibrationProfile` |
| **Simulator** | `sim_sweep` | Deterministic N-D grid sweep with OCR control verification |
| **Simulator** | `sim_search_extremum` | Closed-loop `max`, `min`, `plateau_max`, or `threshold` search |
| **Simulator** | `sim_close` | Close Playwright session & stop loopback server |
| **Math & Data** | `data_ingest` | Ingest inline rows or confined CSV/JSON with explicit provenance source |
| **Math & Data** | `data_analyze` | Safe AST column transforms, free/zero-intercept OLS, % deviations |
| **Math & Data** | `calc_eval` | Safe 5-dim SI dimensional calculator (complex, 3D vectors, uncertainties) |
| **Visuals** | `plot_render` | Headless Matplotlib vector SVG + 300 DPI PNG scientific plots |
| **Visuals** | `diagram_render` | `schemdraw` circuits and parametric phasor/waveform/field SVGs |
| **Reports** | `report_build` | Offline HTML+KaTeX → A4 PDF, editable DOCX, `evidence.zip`, Teacher PDF |
| **Reports** | `report_preview` | Render compiled PDF pages to PNG for mandatory visual QA |

---

## Known Simulator & OCR Pitfalls to Watch For

- **Blinking caret (`|`) OCR artifact:** Always blur focus onto an inert `blur_xy` coordinate before capturing frames so a blinking cursor inside a stepper never OCRs as a leading `1`.
- **Flex stepper boundary clamping:** When calibrating `step_interior` steppers, verify both `min_val` and `max_val` endpoints (`compute_stepper_plan` automatically approaches `max_val` from below with `target - step` + `up_xy`).
- **Unit scaling & Cyrillic↔Latin homoglyphs:** Keep `normalize_homoglyphs=True` and explicit `unit_pattern` on readouts so `мкТл` vs `мТл` or `мкФ` vs `пФ` are never conflated; always preserve negative signs.
- **Wide discrete plateaus:** Do not stop at the first tie during resonance searches — use `mode="plateau_max"` with sufficient `max_steps` so the full plateau `[low_bound, high_bound]` and its midpoint are captured with boundary screenshots.
