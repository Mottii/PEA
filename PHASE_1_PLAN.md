# Phase 1 Implementation Plan — Clean-Slate Migration, Python 3.13 `.venv`, Asset Pinning & Core Contracts

> **Status:** `Complete (2026-11-21)`
> **Parent Plan:** [`PLAN_V2.md`](./PLAN_V2.md) (Steps `1.1` – `1.7`)
> **Prerequisite:** Phase 0 (`GO WITH VERIFIED MITIGATION` — complete in [`PHASE_0_PLAN.md`](./PHASE_0_PLAN.md))

---

## 1. Goal Description

Transition the repository from the legacy 1.7 GB multi-prototype workspace to a clean, lightweight, reproducible **PEA v2** foundation on an orphan `main` branch—without losing any legacy code or reusable simulator/benchmark assets—and lock down the shared Python `3.13` environment, offline assets, and frozen Pydantic v2 contracts (`config.py`, `schemas.py`, `workspace.py`) that all Phase 2 modules will build on.

---

## 2. User Review Required (Safety & Environment Guards)

> [!CAUTION]
> **Orphan Branch Migration & Zero Remote Force-Push:**
> * Before clearing the working tree, the entire current repository will be cloned outside git to `../PEA-legacy-backup/` using APFS copy-on-write (`cp -cR`) and verified.
> * A clean orphan `main` branch will be created locally (`PLAN_V2.md`, `PHASE_0_PLAN.md`, and `PHASE_1_PLAN.md` preserved).
> * **`origin` (`github.com/Mottii/PEA.git`) will NOT be pushed or force-pushed.** You keep full control over if/when to push the clean history later.

> [!IMPORTANT]
> **Disk Space Conservation (`~21 GiB` free on disk):**
> * Using APFS `cp -cR` makes `../PEA-legacy-backup/` take minimal extra physical bytes.
> * `src/pea/config.py` will reuse your already-cached Playwright Chromium (`$HOME/Library/Caches/ms-playwright/chromium-1228`) where compatible so we avoid downloading a redundant ~300 MB Chromium copy unless Playwright explicitly requires a different build.
> * After harvesting the two verified ONNX models from `/tmp/pea-phase0-probe/`, we can optionally clean up `/tmp/pea-phase0-probe/.venv-ocr` (~200 MB).

> [!NOTE]
> **Python Runtime Pinning (from Phase 0):**
> * `.venv` will be built strictly with permanent uv-managed **Python `3.13.14`** (`$HOME/.local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13`) via `$HOME/.local/bin/uv`.
> * Do **not** use Python `3.14` (`onnxruntime` lacks arm64 wheels) or `$HOME/.local/bin/python3.12` (ephemeral symlink into `/private/tmp/...`).

---

## 3. Step-by-Step Proposed Changes

### Step 1.1 — Non-Destructive Full Legacy Backup
* Run APFS clone backup: `cp -cR . ../PEA-legacy-backup` (from repo root, preserving `.git/` and all legacy code/outputs).
* Verify backup presence and key markers (`../PEA-legacy-backup/.git`, `magnetic-field-lab`, `lab-runner`, `field-lab-mcp`, `outputs/swf-pilot/measurements.json`).

### Step 1.2 — Harvest Reusable Local Assets (Temporary Holding Folder)
Stage reusable binary assets into a temporary holding directory (`/tmp/pea-v2-stage/`) with SHA-256 verification before clearing the repo:
* **Ruffle `0.7.1`:** `tmp/swf-pilot/vendor/*` → later `local_assets/ruffle/`
* **Noto Sans Fonts:** `magnetic-field-lab/public/fonts/{NotoSans-Regular.ttf,NotoSans-Bold.ttf,LICENSE.txt}` → later `local_assets/fonts/`
* **RapidOCR Verified ONNX Models:** From `/tmp/pea-phase0-probe/.venv-ocr/lib/python3.13/site-packages/rapidocr/models/`:
  * `ch_PP-OCRv5_det_mobile.onnx` (`4.6 MB`, SHA-256 `4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae`) → later `local_assets/ocr/`
  * `cyrillic_PP-OCRv5_rec_mobile.onnx` (`7.7 MB`, SHA-256 `90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711`) → later `local_assets/ocr/`
* **Local Simulators & Benchmark Inputs (strictly gitignored):**
  * `m96210.swf` (SHA-256 `190e62862cce5d93a1d9f33d04ef2866c4d4c2dba1e97abe3f6c2e3fc635fa74`) → later `local_assets/sims/m96210.swf`
  * `m96211.swf` (SHA-256 `ce40f0e98a3d3b7e9b3cc900f27bfaa06bffb87be306f5c61328f3ba86909cbb`) → later `local_assets/sims/m96211.swf`
  * `m96220.swf` from `$HOME/.gemini/antigravity/brain/41114d59-42f7-419d-af08-7b7dc32e3db3/.user_uploaded/media_1795163130205_2c753e74.swf` (SHA-256 `35e4a257134073356c9796965ea3ea2d9362d51b2dc87486dc93e85a0fb96b15`) → later `local_assets/sims/m96220.swf`
  * Lab 5 handout PDF (`$HOME/.gemini/antigravity/brain/41114d59-42f7-419d-af08-7b7dc32e3db3/.user_uploaded/media_1795163130204_575b8861.pdf`) → later `local_assets/benchmarks/lab5-rlc-handout.pdf`
  * Lab 4 golden reference (`outputs/swf-pilot/measurements.json`) + 4 representative Lab 4 crop fixtures (for fast offline unit tests in Phase 2A without launching Chromium) → later `local_assets/benchmarks/`

### Step 1.3 — Clean Orphan `main` Branch Initialization
1. Copy `PLAN_V2.md`, `PHASE_0_PLAN.md`, and `PHASE_1_PLAN.md` to `/tmp/pea-v2-stage/docs/`.
2. Create orphan branch (`git checkout --orphan main-v2`), clear legacy index & working directory (`git rm -rf .` + remove leftover untracked legacy dirs like `node_modules/`, `.next/`, `lab-runner/runtime/`).
3. Restore `PLAN_V2.md`, `PHASE_0_PLAN.md`, and `PHASE_1_PLAN.md` into the clean root.
4. Write strict **`.gitignore`** immediately (before restoring any binary assets).
5. Move harvested assets from `/tmp/pea-v2-stage/` into gitignored `local_assets/`.
6. Replace local `main` pointer with the orphan branch (`git branch -M main-v2 main`) once Phase 1 scaffolding commits cleanly—preserving `origin` remote config untouched (zero push).

---

### Step 1.4 — Repository Scaffolding & Strict `.gitignore`

#### [NEW] `.gitignore`
Explicitly ignores:
* `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.DS_Store`
* `local_assets/`, `runs/`, `dist/`, `profiles/local/`
* `*.swf`, `*.pdf`, `*.docx`, `*.zip`, `*.onnx`, `*.wasm`, `*.mcpb`

#### [NEW] `pyproject.toml`
* Package name: `pea-mcp`, version `0.2.0`, `requires-python = ">=3.12,<3.14"` (`src`-layout: `src/pea`).
* Core dependencies (all verified arm64 Python 3.13 wheels in Phase 0):
  * `mcp`, `pydantic>=2.10`, `playwright`, `rapidocr==3.10.0`, `onnxruntime==1.32.0`, `pillow`, `numpy`, `scipy`, `matplotlib`, `schemdraw`, `pypdfium2`, `pypdf`, `python-docx`, `starlette`, `uvicorn`, `httpx`
* Optional dev group `[project.optional-dependencies].dev`: `pytest`, `pytest-asyncio`, `ruff`.
* CLI entry points:
  * `pea-mcp = "pea.mcp_server:main"`
  * `pea-viewer = "pea.viewer.app:main"`
* Tool configs for `[tool.pytest.ini_options]` (including `live_sim` marker) and `[tool.ruff]` (`line-length = 100`, `target-version = "py313"`).

#### [NEW] `README.md` (Phase 1 Initial)
* Concise overview of PEA v2, zero-API-cost MCP design, setup commands (`uv`, `scripts/fetch_assets.py`, `pytest`), and pointers to `PLAN_V2.md`, `PHASE_0_PLAN.md`, and `PHASE_1_PLAN.md`.

---

### Step 1.5 — Python `3.13` `.venv` Creation & Chromium Check
1. Create `.venv` via `$HOME/.local/bin/uv venv .venv --python $HOME/.local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13`.
2. Install editable package + dev deps via `$HOME/.local/bin/uv pip install --python .venv/bin/python -e ".[dev]"`.
3. Verify every core import succeeds in `.venv/bin/python`.
4. Check whether installed `playwright` resolves an existing cached Chromium in `$HOME/Library/Caches/ms-playwright/`; if mismatched, configure fallback executable path in `config.py` or run `.venv/bin/playwright install chromium` only if strictly required.

---

### Step 1.6 — Offline Asset Bootstrapper

#### [NEW] `scripts/fetch_assets.py`
Idempotent CLI script (`--check` and `--fetch`) with pinned SHA-256 verification for every offline asset under `local_assets/`:
1. **Offline KaTeX** (`local_assets/katex/`: `katex.min.css`, `katex.min.js`, `contrib/auto-render.min.js`, and `fonts/*.woff2`) — fetched once in Phase 1 from official jsDelivr/GitHub release tarball so report PDF rendering works 100% offline with zero network calls.
2. **RapidOCR ONNX Models** (`local_assets/ocr/`: `ch_PP-OCRv5_det_mobile.onnx` + `cyrillic_PP-OCRv5_rec_mobile.onnx`) — verifies SHA-256 of local harvested copies; downloads pinned URLs on fresh clones.
3. **Ruffle `0.7.1`** (`local_assets/ruffle/`) — verifies `package.json == "0.7.1"` + `ruffle.js` + `.wasm` files; fetches from npm tarball on fresh clones.
4. **Noto Sans Fonts** (`local_assets/fonts/`: `NotoSans-Regular.ttf`, `NotoSans-Bold.ttf`, `LICENSE.txt`) — verifies existing copies or fetches from Google Fonts repo.
5. Writes `local_assets/manifest.json` recording versions and SHA-256 hashes.

---

### Step 1.7 — Frozen Core Contracts (`src/pea/`)

Phase 2A–2D all import from these three foundational files, so we get them rock-solid and well-tested in Phase 1:

#### [NEW] `src/pea/__init__.py`
Exports `__version__ = "0.2.0"`.

#### [NEW] `src/pea/config.py`
* `PeaConfig` dataclass/model loaded from env with safe defaults:
  * `project_root: Path`, `local_assets_dir: Path`, `runs_dir: Path`, `profiles_dir: Path`
  * `loopback_host: str = "127.0.0.1"`, `viewer_port: int = 5190`
  * `allowed_origins: tuple[str, ...] = ()` (parsed from `PEA_ALLOWED_ORIGINS`; empty = strictly loopback-only)
  * `ocr_backend: Literal["rapidocr", "macos_vision"] = "rapidocr"`, `ocr_threads: int = 4`, `ocr_timeout_s: float = 10.0`
  * `browser_executable: Path | None` (resolves `PEA_BROWSER` → Playwright bundled → cached `$HOME/Library/Caches/ms-playwright/chromium-*/...`)
* Security helpers:
  * `resolve_confined_path(base: Path, target: str | Path) -> Path` (blocks `..` traversal & symlink escapes)
  * `is_origin_allowed(url: str, loopback_origin: str, config: PeaConfig) -> bool`

#### [NEW] `src/pea/schemas.py`
Strict Pydantic v2 (`ConfigDict(extra="forbid")`) contracts covering all M1 + M2-ready interfaces (incorporating every Phase 0 finding):
* **Provenance & Run Lifecycle:**
  * `ProvenanceSource = Literal["simulator_captured", "bench_manual", "model_computed"]`
  * `RunPhase = Literal["created", "running", "complete", "failed", "cancelled", "interrupted"]`
  * `RunStatus`, `ProvenanceEntry` (timestamp, event kind, relative path, SHA-256, details)
* **OCR & Calibration Profiles (incorporating Phase 0 lessons):**
  * `BBoxPx = tuple[int, int, int, int]` (`x1, y1, x2, y2`)
  * `OcrBox` (`text`, `confidence`, `bbox_px`, `bbox_norm`) & `OcrResult` (`backend`, `mode`, `elapsed_ms`, `boxes`)
  * `ControlSpec` (`name`, `kind: Literal["stepper","input","button","radio","checkbox","drag_handle"]`, `input_xy`, `up_xy`, `down_xy`, `click_xy`, `unit`, `step`, `min_val`, `max_val`, `decimal_comma`, `commit_mode: Literal["enter","step_interior","click","blur"]`, `blur_xy`)
  * `ReadoutSpec` (`name`, `roi_px: BBoxPx`, `ocr_mode: Literal["rec_only","det_rec"]`, `pad_px: int = 0`, `scale: float = 1.0`, `parse_mode: Literal["float","int","text"]`, `unit_pattern: str | None`, `normalize_homoglyphs: bool = True`)
  * `CalibrationProfile` (`schema_version=1`, `swf_sha256`, `stage_width`, `stage_height`, `header_height_px=39`, `controls: dict[str, ControlSpec]`, `readouts: dict[str, ReadoutSpec]`, `blur_xy`, `verified_at`, `notes`)
* **Simulator Primitives (M1 + M2 stubs):**
  * `SwfMeta` (`path`, `sha256`, `swf_version`, `compressed`, `stage_width`, `stage_height`, `fps`, `frame_count`)
  * `ActionStep` (`click`, `double_click`, `drag`, `type`, `key`, `wait`, `set_stepper`, `blur`)
  * `SweepAxis`, `SweepSpec` (fixed conditions, axes, target readouts, `settle_ms`, `verify_controls=True`)
  * `SearchSpec` (`mode: Literal["max","min","plateau_max","threshold"]`, `fixed_controls`, `sweep_control`, `readout`, `start`, `step`, `max_steps: int = 120` [comfortably above $R=5.0\,\Omega$'s 74-step plateau], `stop_after_drop: int = 2`, `target_value`, `settle_ms: int = 150`)
  * `MeasurementPoint` (requested settings, observed settings, raw OCR text, parsed value, parsed unit, `screenshot_relpath`, `screenshot_sha256`, `provenance`)
  * `PlateauResult` (`peak_value`, `low_bound`, `high_bound`, `midpoint`, `width_steps`, `trace`, `boundary_screenshots`)
  * M2 stubs: `WatchSpec`, `ComputeSimSpec`
* **Math, Analysis, Visuals & Reports:**
  * `CalcVariable`, `CalcRequest`, `CalcResult` (immutable `calc_id`, SI value, 5-dim SI signature, output unit)
  * `DatasetSpec`, `DerivedColumnSpec`, `RegressionSpec` (`intercept: Literal["free","zero"]`), `ComparisonSpec`, `AnalysisResult`
  * `PlotSeries`, `PlotSpec`, `DiagramElement`, `DiagramSpec`
  * `ReportLanguage = Literal["en", "ru"]`, `ReportSectionOptions` (`include_self_control`, `include_problems`, `include_evidence_appendix`), `SelfControlItem`, `ProblemItem`, `ReportTable`, `ReportFigure`, `ReportSpec`, `ReportBuildResult`

#### [NEW] `src/pea/workspace.py`
* `WorkspaceManager` + `RunWorkspace`:
  * Generates unique run IDs (`YYYY-MM-DDTHH-MM-SS-mmmZ-<8hex>`), creates standard subdirectories (`evidence/`, `crops/`, `figures/`, `reports/`, `pdf_pages/`).
  * Atomic status persistence (`status.tmp` → `os.replace` → `status.json`) and idempotent `request_id` lookup.
  * `record_artifact(relpath, kind, metadata)` computing SHA-256 and appending to `provenance.jsonl`.
  * `verify_provenance()` cross-checking every recorded artifact's bytes against `provenance.jsonl`.
  * `recover_interrupted_runs()` marking non-terminal runs as `interrupted` cleanly on startup (never silently completing or discarding partial evidence).
  * `list_runs()` sorted newest-first.

---

## 4. Verification Plan (Phase 1 Exit Criteria)

### Automated Checks
```sh
.venv/bin/pytest tests/test_phase1_core.py -v
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/python scripts/fetch_assets.py --check
git status --short && git rev-list --count HEAD
```

`tests/test_phase1_core.py` will verify:
1. **Environment & imports:** All core libraries (`mcp`, `playwright`, `pydantic`, `rapidocr`, `onnxruntime`, `numpy`, `scipy`, `matplotlib`, `schemdraw`, `pypdfium2`, `pypdf`, `docx`, `PIL`, `starlette`) import cleanly under `.venv` Python `3.13`.
2. **Security & confinement (`config.py`):** Path traversal (`../`, symlink escape) is rejected; non-loopback origins are blocked by default and allowed strictly when in `PEA_ALLOWED_ORIGINS`.
3. **Schema strictness (`schemas.py`):** Extra/unknown fields rejected (`extra="forbid"`), valid `CalibrationProfile`, `SweepSpec`, `SearchSpec` (wide plateau `max_steps=120`), `CalcRequest`, `ReportSpec` round-trip cleanly to/from JSON.
4. **Workspace integrity (`workspace.py`):** Run creation, atomic status updates, SHA-256 artifact ledger append + `verify_provenance()` pass, tamper detection fails `verify_provenance()`, `recover_interrupted_runs()` transitions active runs to `interrupted` while leaving `complete`/`cancelled` intact.
5. **Repo & asset hygiene:** `git rev-list --count HEAD == 1` (clean orphan `main`), `git ls-files` contains zero `.swf`/`.pdf`/`.onnx`/`.wasm`/`.venv`/`runs/` files, `../PEA-legacy-backup/` exists intact, and `scripts/fetch_assets.py --check` confirms all 4 offline asset groups (`katex`, `ocr`, `ruffle`, `fonts`) match pinned SHA-256 hashes.
6. **`PLAN_V2.md` updated:** Phase 1 checkboxes `1.1–1.7` checked off and Progress Log updated.

---

## 5. Observed Phase 1 Findings *(Populated After Execution)*

* **Date Executed:** `2026-11-21` (migrated and scaffolded `2026-11-20`, polished, committed, and verified `2026-11-21`).
* **Backup & Orphan Migration:**
  * Legacy workspace cloned cleanly outside git to `../PEA-legacy-backup/` (verified `.git/`, `magnetic-field-lab/`, `lab-runner/`, `field-lab-mcp/`, `outputs/swf-pilot/measurements.json`).
  * Clean orphan `main` branch created locally (`git rev-list --count HEAD == 1`) with `12` clean text/code/plan files tracked (`git ls-files`); zero binaries, legacy directories, or literal home paths tracked; `origin` remote left untouched (no force-push).
* **Python `.venv` & Chromium Check:**
  * `.venv` built via `$HOME/.local/bin/uv` pinned to permanent uv-managed Python `3.13.14` (`$HOME/.local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13`); all 17 core dependencies (`mcp`, `pydantic`, `playwright`, `rapidocr==3.10.0`, `onnxruntime==1.32.0`, `pillow`, `numpy`, `scipy`, `matplotlib`, `schemdraw`, `pypdfium2`, `pypdf`, `python-docx`, `starlette`, `uvicorn`, `httpx`) plus dev tooling (`pytest`, `pytest-asyncio`, `ruff`) installed cleanly in editable mode.
  * `PeaConfig.from_env()` automatically resolves cached Playwright Chromium (`$HOME/Library/Caches/ms-playwright/chromium-1228`) with zero duplicate browser downloads.
* **Offline Asset Manifest (`fetch_assets.py`):**
  * `.venv/bin/python scripts/fetch_assets.py --check` passed offline across all `37` SHA-256-pinned files (`katex` `0.16.21`: 24 files; `ocr`: `ch_PP-OCRv5_det_mobile.onnx` + `cyrillic_PP-OCRv5_rec_mobile.onnx`: 2 files; `ruffle` `0.7.1`: 8 files; `fonts`: Noto Sans Regular/Bold + license: 3 files), recorded in `local_assets/manifest.json`.
  * Local simulators (`m96210.swf`, `m96211.swf`, `m96220.swf`) and benchmark fixtures (`lab4-measurements.json`, 4 Lab 4 crops, `lab5-rlc-handout.pdf`) staged under gitignored `local_assets/`.
* **Core Contract & Verification Results:**
  * `.venv/bin/pytest tests/test_phase1_core.py -v`: `32/32` passed (`0` failures, `0` skips).
  * `.venv/bin/ruff check src/ tests/ scripts/`: `All checks passed!`.
  * Core Pydantic v2 schemas (`config.py`, `schemas.py`, `workspace.py`) frozen, including `SelfControlItem.figure_ids` for Phase 2C illustrated Q&A support.
