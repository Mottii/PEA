# Phase 0 Execution Plan — Day-0 Gate (`m96220.swf` in Ruffle `0.7.1`)

> **Status:** `Complete — GO WITH VERIFIED MITIGATION (m96220.swf + Ruffle 0.7.1 PASS; RapidOCR ONNX Cyrillic Backend Verified 100% on Lab 5 & Lab 4)`
> **Parent Plan:** [`PLAN_V2.md`](./PLAN_V2.md)
> **Scope Guarantee:** Strictly **non-destructive & read-only** with respect to the existing repository. Zero existing tracked or untracked project files are modified or deleted; all temporary probe scripts, HTML harnesses, screenshots, and OCR logs are written only to an isolated temporary directory (`$TMPDIR` or the session scratch directory).

---

## 1. Objective

Prove that the new Lab 5 simulator (**`m96220.swf` — Forced Oscillations in an RLC Circuit**) boots, renders cleanly, responds deterministically to Flex/Flacon controls, and yields accurate macOS Vision OCR readouts inside pinned **Ruffle `0.7.1`** *before* backing up the legacy repository and building PEA v2.

---

## 2. Verified Starting Inputs

| Asset / Binary | Verified Details | Location |
| :--- | :--- | :--- |
| **Lab 5 Simulator (`m96220.swf`)** | `3,740,127` bytes, SWF v9 (`CWS`), stage **`800×600 px`**, `24.0 fps`, `2` frames, Flex 3 / Flacon toolkit (`FlaconNumericStepper`, `FlaconButton`, `labDevices.voltmeter`, `inductanceCoil`, `condenser`) | `$HOME/.gemini/antigravity/brain/41114d59-42f7-419d-af08-7b7dc32e3db3/.user_uploaded/media_1795163130205_2c753e74.swf` |
| **Expected SHA-256 (`m96220.swf`)** | `35e4a257134073356c9796965ea3ea2d9362d51b2dc87486dc93e85a0fb96b15` | Verified read-only |
| **Ruffle `0.7.1`** | Existing local bundle (`ruffle.js` + WASM cores) | `tmp/swf-pilot/vendor/` |
| **macOS Vision OCR Binary** | Precompiled local Swift Vision executable (`VNRecognizeTextRequest`, `.accurate`, `usesCPUOnly = true`) | `lab-runner/runtime/ocr` |
| **Headless Chromium + Playwright** | Chromium `1228` + bundled Node/Playwright | `$HOME/Library/Caches/ms-playwright/chromium-1228/...` & `$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/` |

### Lab 5 Handout Reference Target (Team 2 Sanity Check)
* **Parameters (Team 2):** $L = 5.0\text{ mH}$, $C = 32\text{ µF}$
* **Expected natural frequency:** $\omega_0 = \frac{1}{\sqrt{LC}} = \frac{1}{\sqrt{5\times 10^{-3} \cdot 32\times 10^{-6}}} = 2500\text{ rad/s} = 2.50 \times 10^3\text{ rad/s}$
* **Resistor under probe:** $R = 3.2\,\Omega$ (first column of Table 2)
* **Theoretical sanity benchmarks (for comparison only — never used to replace readings):**
  * $Q_{\text{ideal}} = \frac{1}{R}\sqrt{\frac{L}{C}} = \frac{1}{3.2}\sqrt{\frac{5\times 10^{-3}}{32\times 10^{-6}}} = 3.90625$
  * $\omega_{\text{res,ideal}} = \omega_0 \sqrt{1 - \frac{1}{2Q^2}} \approx 2458.6\text{ rad/s}$ ($2.459 \times 10^3\text{ rad/s}$)
  * Expected handout relation: $\frac{\omega_0}{\Delta\omega} \approx 4Q^2 \approx 61.04$

---

## 3. Step-by-Step Execution Plan

> **Permission Note:** Steps `0.2` and `0.4` launch headless Chromium and invoke the macOS Vision OCR binary, which require running outside the default sandbox (`BypassSandbox: true`) when prompted.

### Step 0.1 — Stage Isolated Scratch Harness
1. Create a temporary scratch directory (e.g., `<scratch>/phase0-probe/`).
2. Copy `m96220.swf` into `<scratch>/phase0-probe/m96220.swf` and assert its SHA-256 matches `35e4a257134073356c9796965ea3ea2d9362d51b2dc87486dc93e85a0fb96b15`.
3. Symlink `tmp/swf-pilot/vendor` into `<scratch>/phase0-probe/vendor`.
4. Write `<scratch>/phase0-probe/index.html` with:
   * A fixed `39 px` status bar (`height: 39px; box-sizing: border-box; line-height: 19px`).
   * A player container sized to the exact native SWF stage **`800px × 600px`** (`#player { width: 800px; height: 600px; }`).
   * Ruffle loaded with strict security flags: `autoplay: 'on'`, `unmuteOverlay: 'hidden'`, `allowScriptAccess: false`, `allowNetworking: 'none'`, `logLevel: 'warn'`.

### Step 0.2 — Boot, Assert Geometry & Capture Baseline
1. Start an ephemeral loopback HTTP server bound strictly to `127.0.0.1` serving `<scratch>/phase0-probe/`.
2. Launch Playwright headless Chromium (`viewport: 900×700`, `deviceScaleFactor: 1`), aborting all non-loopback requests via `page.route`.
3. Collect all browser `console` warnings/errors and `pageerror` events.
4. Wait for `#status` confirmation (`m96220.swf — original SWF in Ruffle 0.7.1`) plus `1500 ms` settle time.
5. Assert `<ruffle-player>` bounding box is exact `(x=0, y=39, width=800, height=600)`.
6. Save `<scratch>/phase0-probe/01-initial.png` and run `lab-runner/runtime/ocr` against `01-initial.png` (and an upscaled bottom/panel crop if needed) to emit full OCR text + normalized bounding boxes.

### Step 0.3 — Visual & OCR Control Inventory
1. Inspect `01-initial.png` visually via `read_file` alongside the OCR bounding-box JSON.
2. Document every interactive UI element and readout on the `800×600` stage:
   * Input controls (`L`, `C`, `R`, `ω` / frequency) — their exact pixel coordinates, units (`мГн`, `мкФ`, `Ом`, `10³ рад/с` or `рад/с`), and default values.
   * Radio buttons / toggles / mode switches (e.g., circuit view vs. vector diagram / graphs, `Записать` / `Очистить`).
   * Output readouts ($U_{0C}/\varepsilon_0$, $U_C$, $U_L$, $U_R$, $I$, etc.) — exact locations, decimal separator (`,` vs `.`), units, and whether any scrollbars clip any readout.
   * Any Ruffle `console` / AS3 warnings.

### Step 0.4 — Interactive Stepper Commit & Resonance Peak Probe
1. In a live Playwright probe script against `m96220.swf`:
   * Discover whether typing + `Enter` or typing + native stepper click commits physics updates for `L`, `C`, `R`, and `ω`, and measure each stepper's exact increment/decrement step size and min/max bounds.
   * Configure **Team 2, first column**: $L = 5.0\text{ mH}$, $C = 32\text{ µF}$, $R = 3.2\,\Omega$, and set $\omega$ near $2.50 \times 10^3\text{ rad/s}$. Save `02-configured.png` + OCR dump.
   * Step $\omega$ downward through the expected resonance peak ($\approx 2.50 \to 2.40 \times 10^3\text{ rad/s}$) in discrete steps, logging `(step_index, omega_ocr, U0C_over_eps0_ocr)` at each step.
   * Save evidence screenshots around the peak (`03-peak-start.png`, `04-peak-end.png`, `05-post-peak.png`) and inspect them visually.
2. Verify:
   * Do all 4 controls update reliably without leaving stale physics values?
   * Does $U_{0C}/\varepsilon_0$ exhibit a clear discrete maximum / plateau $[\omega_1, \omega_2]$ near $\approx 3.91$ and $\omega_{\text{res}} \approx 2.46 \times 10^3\text{ rad/s}$?
   * Does `lab-runner/runtime/ocr` accurately parse all control settings and $U_{0C}/\varepsilon_0$ readouts?

### Step 0.5 — Go / No-Go Decision Matrix

| Outcome | Criteria | Next Action |
| :--- | :--- | :--- |
| **GO (Proceed to Phase 1)** | `m96220.swf` renders cleanly at `800×600` with zero fatal AS3/Ruffle exceptions; all 4 parameters (`L, C, R, ω`) commit deterministically; $U_{0C}/\varepsilon_0$ readout is completely unclipped and parsed cleanly by Vision OCR; resonance peak/plateau behaves physically. | Update Section 4 below + `PLAN_V2.md`, then begin Phase 1 (backup + orphan `main` + Python `.venv`). |
| **MITIGATE (Minor Issue)** | Simulation works, but a readout is partially clipped by a Flex container scrollbar OR OCR struggles on a decimal comma/superscript at `1×` scale. | Test `1.5×`/`2×` CSS/viewport scaling or targeted high-res ROI cropping in Phase 0 before proceeding. |
| **HOLD (Ruffle 0.7.1 Bug)** | Ruffle `0.7.1` throws ActionScript 3 runtime errors, freezes controls, or fails to render meters/waveforms in `m96220.swf`. | Download and test the latest Ruffle nightly/release inside the scratch harness before touching `main` or committing to Ruffle `0.7.1`. |

### Step 0.6 — Record Findings & Close Gate
1. Append the observed `m96220.swf` control coordinates, step sizes, readout precision, Ruffle console output, and Go/No-Go outcome to **Section 4** below.
2. Check off Phase 0 items in `PLAN_V2.md` and log the result in its Progress Log table.

---

## 4. Observed Phase 0 Findings *(Populated After Execution)*

* **Date Executed:** `2026-11-20`
* **Ruffle Version & Browser:** Ruffle `0.7.1` (`wgpu-webgl`, WASM extensions ON) in headless Chromium `149.0.7827.55` (`ms-playwright/chromium-1228`, macOS `27.2` Build `26B123`, arm64). Scratch harness isolated at `/tmp/pea-phase0-probe/` (zero repo files modified during probe).
* **SWF Verification:** Copied `m96220.swf` (`3,740,127` bytes) matched expected SHA-256 `35e4a257134073356c9796965ea3ea2d9362d51b2dc87486dc93e85a0fb96b15`. `<ruffle-player>` rendered at exact native geometry `(x=0, y=39, width=800, height=600)` inside `900×700` viewport; `abortedRequests = []`.
* **Gate Result:** **`GO WITH VERIFIED MITIGATION`**
  * **Simulator (`m96220.swf` in Ruffle `0.7.1`): `GO` (Clean Pass).** Renders cleanly in both `Схема` and `Установка` modes with zero fatal AS3/Ruffle exceptions; all 4 steppers (`R, L, C, ω`), mode radios, and 3 curve checkboxes commit deterministically; all 4 Team 2 Lab 5 resistor columns ($R \in \{3.2, 4.0, 5.0, 6.0\}\,\Omega$) match analytic RLC plateau physics (`0.008%` midpoint error at $R=3.2\,\Omega$).
  * **OCR Backend (`lab-runner/runtime/ocr` on macOS `27.2`): `MITIGATED & VERIFIED` → Promote `RapidOcrBackend` (`PP-OCRv5` Mobile Det + `cyrillic_PP-OCRv5_rec_mobile` ONNX) into M1 (`Phase 1.5` / `Phase 2A.1`).**
    * **macOS Vision Diagnostics:** Legacy `lab-runner/runtime/ocr` (`VNRecognizeTextRequest` `.accurate`, `usesCPUOnly = true`) **hung indefinitely (`>12 s` / `>30 s` timeout)** on macOS `27.2` (`Build 26B123`) for both `m96220.swf` captures and legacy Lab 4 crops (`tmp/swf-pilot/readout-crops/wire-I-5-r2.png`). Freshly compiled Swift probes confirmed `.accurate` hangs across **all three revisions (`1`, `2`, `3`)**, `CGImage` and `URL` handlers, `usesCPUOnly = true/false`, explicit `MLComputeDevice` (`CPU`, `GPU`, `ANE`), modern macOS 15+ `RecognizeTextRequest`, `en-US`-only vs `en-US`+`ru-RU`, clean `env`, and `NSApplication.shared` (and fails immediately with `OCR error: nilError` inside sandbox).
    * **Why Vision `.fast` is disqualified:** Querying `supportedRecognitionLanguages()` proved `.fast` has `has_ru = false` across all revisions (`1–3`) — Cyrillic (`ru-RU`) exists only in `rev=3` `.accurate`. Consequently `.fast` transliterates Cyrillic (`мГн → MFH`, `мкФ → MKty`, `Ом → 0M`), drops isolated `3,0`/`3,2`/`5,0` fields even at `deviceScaleFactor: 2`, and returns empty `[]` on `ω = 2444` across all scales (`1.5×–5×`) with both `usesLanguageCorrection = false` and `true` — a fatal failure because **`2444` is the exact upper plateau boundary $\omega_2$ of Team 2 Column 2 ($R = 4.0\,\Omega$)**!
    * **Empirically Verified Mitigation (`RapidOCR` in isolated `/tmp/pea-phase0-probe/.venv-ocr`):**
      * **Python & Wheel Gate:** `onnxruntime` has **no** arm64 macOS binary wheel for Python `3.14` (`uv pip compile --python-version 3.14` fails), whereas **Python `3.13.14`** (permanent uv-managed `~/.local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13`) resolves all 17 PEA v2 packages cleanly (`rapidocr==3.10.0`, `onnxruntime==1.32.0`). *(Warning: do NOT point `.venv` at `~/.local/bin/python3.12`, which symlinks into ephemeral `/private/tmp/gas-runtime-python/...`!)*
      * **Required Model Selection:** Default `RapidOCR()` (`PP-OCRv6_rec_small`, `LangRec.CH`) drops/garbles Russian UI labels (`Схема → Cxema`), whereas **`Det.ocr_version = PPOCRV5` (`MOBILE`) + `Rec.lang_type = LangRec.CYRILLIC` (`PPOCRV5`, `MOBILE`) + `Global.use_cls = False`** (`ch_PP-OCRv5_det_mobile.onnx` 4.6 MB + `cyrillic_PP-OCRv5_rec_mobile.onnx` 7.7 MB, `intra_op_num_threads = 4`) reads full Cyrillic headers (`Параметры системы`, `Выходные данные`, `Установка`, `На катушке`), Russian decimal commas, and digits cleanly on raw `1×` screenshots.
      * **Benchmark Proof (Lab 5 + Lab 4):**
        * **Lab 5 (`m96220.swf`, all 50 Team 2 sweep frames):** **`50/50` (`100%`)** on `R, L, C, ω, ω₀, U_C/U, U_L/U, U_R/U` (including **`ω = 2444` at `1.000` confidence!**) in BOTH right-panel `det+rec` (`904 ms/frame`, `0` failures) AND tight single-line `use_det=False` rec-only mode (**`400/400` crops, `17.4 ms/crop`, `139 ms/frame`, `0` failures!**).
        * **Lab 5 (`R = 4.0, 5.0, 6.0 Ω` plateau boundaries):** **`12/12` (`100%`)** exact parse.
        * **Legacy Lab 4 (`m96210`/`m96211`, all 72 crops in `tmp/swf-pilot/readout-crops/`):** **`72/72` (`100%`)** numeric value, sign, and micro/milli prefix match via padded readout-panel `det+rec` (`300 ms/crop`) with Cyrillic↔Latin homoglyph normalization (`m→м`, `k→к`, `t→т`, `l→л`, `o→о`, `c→с`, `h→н`), outperforming the Nov 3 Vision `.accurate` log (which garbled `мкТšșîę` on two crops). *(Notes: loose multi-margin crops MUST use padded `det+rec`, not `use_det=False`, which degraded to `9/72` on wide Lab 4 strips; in `rapidocr==3.10.0`, `use_det=False` mutates `engine.use_det` persistently, so callers MUST pass `use_det=True/False` explicitly on every call; and `scripts/fetch_assets.py` MUST pre-fetch the two ONNX models into `local_assets/ocr/` so runtime stays 100% offline.)*
      * Keep `MacOsVisionOcrBackend` optional behind a hard per-call timeout + automatic fallback to `RapidOcrBackend`.
* **Console / AS3 Errors:**
  * `pageErrors`: `0`; browser `console` warnings/errors: `0`.
  * Ruffle stdout log contained only benign Flex 3 device-font fallbacks (`Courier New`, `Times New Roman`, `Noto Sans`, `Arial`, `Tahoma`, `Times`, `Verdana`) and standard non-fatal AVM2 stubs (`Dictionary` weak keys, `TextField.mouseWheelEnabled`, `NativeMenuItem`, `Capabilities.isDebugger`, `Loader.load`, `describeTypeJSON`, `BitmapData.generateFilterRect`, `Security.allowDomain`).
* **Verified `m96220.swf` Control & Readout Map (`800×600` stage, `39 px` top status bar → `page_y = stage_y + 39`):**
  * **Mode Radios (`Режим`, panel `x=576..793, stage_y=5..83`):**
    * `Схема` (default — schematic circuit): page `(593, 79)` / stage `(593, 40)`
    * `Установка` (3D benchtop RLC apparatus with analog voltmeter `0..6 В`, ammeter `0..2 А`, rheostat, solenoid coil): page `(593, 108)` / stage `(593, 69)`
  * **Curve Checkboxes (`Показывать напряжение`, panel `x=576..793, stage_y=87..199`):**
    * `На конденсаторе` (red): page `(594, 165)` / stage `(594, 126)`
    * `На резисторе` (orange): page `(594, 193)` / stage `(594, 154)`
    * `На катушке` (blue): page `(594, 221)` / stage `(594, 182)`
  * **System Parameter Steppers (`Параметры системы`, panel `x=576..793, stage_y=203..375`; note `L` box is shifted `2 px` left of `R/C/ω`):**
    | Control | Units & Format | Default | Verified Bounds | Native Step | Text Input Box (Page px) | Input Center (Page / Stage) | `▲` Up (Page / Stage) | `▼` Down (Page / Stage) |
    | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
    | **`R`** | `Ом` (decimal `,`) | `3,0` | `[3.0, 10.0]` | `0.1` | `x=[620..667], y=[275..302]` | `(644, 289)` / `(644, 250)` | `(678, 282)` / `(678, 243)` | `(678, 296)` / `(678, 257)` |
    | **`L`** | `мГн` (decimal `,`) | `3,0` | `[1.0, 5.0]` | `0.1` | `x=[618..665], y=[308..335]` | `(642, 322)` / `(642, 283)` | `(676, 315)` / `(676, 276)` | `(676, 329)` / `(676, 290)` |
    | **`C`** | `мкФ` (integer) | `50` | `[30, 150]` | `1` | `x=[620..667], y=[341..368]` | `(644, 355)` / `(644, 316)` | `(678, 348)` / `(678, 309)` | `(678, 362)` / `(678, 323)` |
    | **`ω`** | `с⁻¹` (integer) | `1500` | `[1000, 5000]` | `1` | `x=[620..667], y=[374..401]` | `(644, 388)` / `(644, 349)` | `(678, 381)` / `(678, 342)` | `(678, 395)` / `(678, 356)` |
  * **Commit Semantics & Edge-Case Traps:**
    * Typing with **either `.` or `,`** (e.g., `5.2` or `4,2`) after **`Meta+a` or `Control+a`** (both verified working in Ruffle `0.7.1` on macOS) followed by **`Enter`**, **`Tab`**, or **blurring onto an inert point** (e.g., status bar `(450, 18)`) auto-formats to Russian decimal comma and **commits physics immediately** — verified across interior values, min bounds (`R=3.0, L=1.0, C=30, ω=1000` → `ω₀ = 5774 с⁻¹, X_L = 1,0 Ом`), and max bounds (`R=10.0, L=5.0, C=150, ω=5000` → `ω₀ = 1155 с⁻¹, X_L = 25,0 Ом`). Note `L = 5.0 mH` (Team 2) sits right on `max_val`.
    * **Stale-Physics Boundary Trap:** Typing an out-of-range value + clicking a boundary arrow (`▼` at min or `▲` at max) *without* pressing `Enter` updates the displayed text to the clamped bound while leaving `Выходные данные` and plots **stale** (confirmed in `01h`/`01i` where `ω₀` remained stuck at `2515 с⁻¹`). Always commit via `Enter` (or step inward first) and blur focus onto an inert point `(450, 18)` before screenshots to remove the blinking text caret `|` (which can otherwise be OCR'd as a leading `1`, as observed on legacy Lab 4 `|4,0 → 14,0`).
    * Single `▲`/`▼` native arrow clicks inside bounds step deterministically by `(0.1, 0.1, 1, 1)` and recompute physics in `<120 ms`.
    * In `Установка` mode at Team 2 resonance (`R=3,2, L=5,0, C=32, ω=2500`), the 3D apparatus updates live (coil iron core advances, rheostat slider shifts, analog ammeter needle deflects from `~0.5 A` to `~1.6 A`, voltmeter holds `5.0 V`) while `Выходные данные` remains live and identical.
  * **Output Readouts (`Выходные данные`, non-scrolling panel `x=576..793, stage_y=379..597`; values formatted with `,`):**
    * `ω₀ = <int> с⁻¹` — value+unit ROI page `x=[625..710], y=[436..468]` (`stage_y=[397..429]`, includes full `-1` superscript starting at `y=438`), completely unclipped.
    * `U_C / U = <0.00>` (stacked fraction; corresponds to handout $U_{0C}/\varepsilon_0$) — value ROI page `x=[625..710], y=[474..502]` (`stage_y=[435..463]`), completely unclipped.
    * `U_L / U = <0.00>` — value ROI page `x=[625..710], y=[515..542]` (`stage_y=[476..503]`), completely unclipped.
    * `U_R / U = <0.00>` — value ROI page `x=[625..710], y=[555..583]` (`stage_y=[516..544]`), completely unclipped.
    * `X_L = ωL = <0.0> Ом` — value+unit ROI page `x=[661..750], y=[592..618]` (`stage_y=[553..579]`, starts cleanly after `ωL =`), completely unclipped.
    * *(Cosmetic note)* A 6th line (`X_C = 1/(ωC)`, not required by Lab 5 Table 2) is partially clipped below the bottom edge of the fixed `Выходные данные` panel (mouse wheel does not scroll the container, and enlarging `#player` to `800×660` merely adds a 60 px letterbox without unclipping it — proving it is inherent to the fixed `800×600` SWF stage + Ruffle fallback font metrics).
    * Bottom-left panel (`Векторная диаграмма`) renders live rotating/relative phasors (`U_L, U_C, U_R, U, I`); bottom-center graph renders `U_C/U`, `U_L/U`, `U_R/U` vs `ω ∈ [0, 6000] с⁻¹`.
* **Team 2 ($L=5.0\text{ mH}, C=32\text{ µF}$) Resonance Probe Summary:**
  * At $R = 3.2\,\Omega$, $\omega = 2500\text{ s}^{-1}$ (`02-configured.png`): `ω₀ = 2500 с⁻¹` (exact match to $1/\sqrt{LC} = 2500.0\text{ s}^{-1}$), `U_C/U = 3,91` (matches $Q = 3.90625$), `U_L/U = 3,91`, `U_R/U = 1,00`, `X_L = 12,5 Ом`.
  * Swept `50` frequency points across $\omega \in [2500 \to 2400]\text{ s}^{-1}$ (`2500, 2490, 2480` via `type+Enter`, `45` consecutive `1 s⁻¹` native `▼` clicks from `2479` down to `2435` giving `46` contiguous 1 s⁻¹ points `2480..2435`, plus `2420, 2400` via `type+Enter`):
    * `ω = 2500` → `3,91`; `2490` → `3,92`; `2480..2473` → `3,93`.
    * **Discrete Resonance Plateau Maximum ($R = 3.2\,\Omega$):** **`U_C/U = 3,94`** across **`ω ∈ [2445, 2472] с⁻¹`** (`28` consecutive `1 s⁻¹` steps; verified visually in `03-peak-start.png` at `2472`, `04-peak-end.png` at `2445`, and `05-post-peak.png` at `2444` where `U_C/U` drops to `3,93`).
    * `ω = 2444..2435` → `3,93`; `2420` → `3,91`; `2400` → `3,88`. Zero rounding mismatches across all `50/50` points vs exact analytic RLC.
  * **Remaining Team 2 Table 2 Columns (Live Boundary Verification in Ruffle `0.7.1` + RapidOCR `12/12`):**
    * **$R = 4.0\,\Omega$:** plateau **`[2427, 2444] с⁻¹`** (`18` steps, `U_C/U = 3,17`; drops to `3,16` at `2426` & `2445`; midpoint `2435.5` vs ideal `2435.16 s⁻¹`).
    * **$R = 5.0\,\Omega$:** plateau **`[2361, 2434] с⁻¹`** (**`74` steps wide!**, `U_C/U = 2,55`; drops to `2,54` at `2360` & `2435`; midpoint `2397.5` vs ideal `2397.92 s⁻¹`). *(Design note: `sim_search_extremum` must support wide plateaus ≥80 steps!)*
    * **$R = 6.0\,\Omega$:** plateau **`[2333, 2370] с⁻¹`** (`38` steps, `U_C/U = 2,15`; drops to `2,14` at `2332` & `2371`; midpoint `2351.5` vs ideal `2351.60 s⁻¹`).
  * **Physics Sanity Cross-Check ($R = 3.2\,\Omega$):**
    * Plateau midpoint: $\omega_{\text{res}} = \frac{2445 + 2472}{2} = \mathbf{2458.5\text{ s}^{-1}}$ vs ideal $\omega_0\sqrt{1 - \frac{1}{2Q^2}} = \mathbf{2458.70\text{ s}^{-1}}$ (`0.008%` deviation).
    * Peak amplitude: $U_C/U = \mathbf{3.94}$ vs exact $\frac{Q}{\sqrt{1 - 1/(4Q^2)}} = \mathbf{3.9386}$.
    * Handout relation: $\Delta\omega = 2500 - 2458.5 = 41.5\text{ s}^{-1} \implies \frac{\omega_0}{\Delta\omega} = \mathbf{60.24}$ vs ideal $4Q^2 = \mathbf{61.04}$ (`1.3%` discrete-rounding agreement).
