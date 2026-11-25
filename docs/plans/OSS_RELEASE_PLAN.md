# Open-Source Publication & Polish Plan — PEA v2 (`v0.3.0`)

> **Status:** `Ready for Implementation`
> **Baseline:** Milestone 2 complete on `main` (`98c9033`; `v0.3.0`; `18` MCP tools; `210/210` tests passing locally; `ruff` clean; `dist/pea-mcp-0.3.0.mcpb` audited).

---

## 1. Audit Summary & Release Blockers Identified

Auditing `git ls-files`, `pyproject.toml`, `README.md`, `tests/`, and `git branch -a` surfaced six concrete blockers and polish gaps that must be resolved before making `Mottii/PEA` public:

1. **Missing Legal & Package Metadata:**
   * `packaging/mcpb/manifest.json` declares `"license": "MIT"`, but there is no root `LICENSE` file, no `THIRD_PARTY_NOTICES.md` for fetched assets (`KaTeX`, `Ruffle`, `Noto Sans`, `RapidOCR`), and no `license`, `authors`, `keywords`, `classifiers`, or `[project.urls]` in `pyproject.toml`.
2. **Developer-Machine-Specific Test (`PEA-legacy-backup`):**
   * `tests/test_phase1_core.py:1604-1612` (`test_repo_git_hygiene_and_legacy_backup`) asserts `REPO_ROOT.parent / "PEA-legacy-backup"` exists on disk, which fails on every external clone and in GitHub Actions.
3. **Python 3.12 Constraint Mismatch & macOS-Specific Setup Command:**
   * `pyproject.toml:10` declares `requires-python = ">=3.12,<3.14"`, whereas `tests/test_phase1_core.py:79` asserts `sys.version_info[:2] == (3, 13)` (failing on Python 3.12).
   * `README.md:25` hardcodes `uv venv .venv --python "$HOME/.local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13"`.
4. **Offline Tests Dependent on Private/Non-Redistributable Assets:**
   * `local_assets/sims/*.swf` and `local_assets/benchmarks/*` are properly `.gitignore`d (third-party university materials), yet several offline tests assume they exist locally:
     * `tests/test_swf_meta.py:87-96` (`test_inspect_real_staged_swfs` fails when `m96210.swf` / `m96211.swf` / `m96220.swf` are absent).
     * `tests/test_sim_protocols.py:60-181` (`test_runtime_server_swf_mode_security_and_auto_sized_html` and `test_runtime_server_ipv6_loopback_and_host_guard` read `local_assets/sims/m96220.swf`).
     * `tests/test_ocr_and_profiles.py:159-225` (`test_rapid_ocr_lab4_crops_exact_value_sign_unit` reads `local_assets/benchmarks/lab4-crops/`).
     * `tests/test_pdf_intake.py` skips when `lab5-rlc-handout.pdf` is absent without an always-run synthetic PDF intake test.
5. **Cluttered Root Directory & Missing OSS Community/CI Files:**
   * Nine internal phase/milestone planning docs (`PLAN_V2.md`, `PHASE_0_PLAN.md`–`PHASE_3_PLAN.md`, `MILESTONE_2_PLAN.md`, `M2B_PLAN.md`–`M2D_PLAN.md`) sit in the repo root, and `MILESTONE_2_PLAN.md` / `M2B–M2D_PLAN.md` still carry `*Pending execution.*` placeholders in their Observed Findings sections.
   * No `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`, `.github/ISSUE_TEMPLATE/`, `.github/PULL_REQUEST_TEMPLATE.md`, or `.github/workflows/` (`ci.yml` / `release.yml`) exist yet.
6. **`origin/legacy-v1` Branch Exposure:**
   * `git branch -a` shows `remotes/origin/legacy-v1` (`1,535` files from the legacy prototype). It must be audited for third-party handouts, SWFs, student names, or host paths—and deleted or archived privately—before switching the GitHub repository to public.

---

## 2. Pass 1 — OSS Blockers, Test Portability & Repo/Docs Polish

### 2.1 Licensing & Package Metadata

1. **`[NEW] LICENSE`:**
   * Add standard MIT License text at repo root matching `packaging/mcpb/manifest.json` (`"license": "MIT"`).
2. **`[NEW] THIRD_PARTY_NOTICES.md`:**
   * Attribute all four pinned offline asset groups managed by [`scripts/fetch_assets.py`](../../scripts/fetch_assets.py) plus key runtime dependencies:
     * **KaTeX `0.16.21`** — MIT License
     * **Ruffle `0.7.1`** — Apache-2.0 / MIT Dual License
     * **Noto Sans (Regular/Bold TTF)** — SIL Open Font License 1.1
     * **RapidOCR / PaddleOCR PP-OCRv5 Mobile ONNX models** — Apache-2.0 License
     * Core Python runtime stack (`mcp`, `playwright`, `pydantic`, `numpy`, `scipy`, `matplotlib`, `schemdraw`, `pypdfium2`, `pypdf`, `python-docx`, `starlette`, `uvicorn`, `httpx`, `pillow`, `onnxruntime`).
3. **`[MODIFY] pyproject.toml`:**
   * Add OSS metadata:
     ```toml
     license = "MIT"
     authors = [{ name = "PEA Contributors" }]
     keywords = ["physics", "laboratory", "mcp", "simulator", "ocr", "scientific-computing", "reports"]
     classifiers = [
         "Development Status :: 4 - Beta",
         "Intended Audience :: Education",
         "Intended Audience :: Science/Research",
         "License :: OSI Approved :: MIT License",
         "Programming Language :: Python :: 3",
         "Programming Language :: Python :: 3.12",
         "Programming Language :: Python :: 3.13",
         "Topic :: Scientific/Engineering :: Physics",
     ]

     [project.urls]
     Homepage = "https://github.com/Mottii/PEA"
     Repository = "https://github.com/Mottii/PEA"
     Issues = "https://github.com/Mottii/PEA/issues"
     Changelog = "https://github.com/Mottii/PEA/blob/main/CHANGELOG.md"
     ```

---

### 2.2 Fresh-Clone & Python 3.12–3.13 Test Portability

1. **`[MODIFY] tests/test_phase1_core.py`:**
   * Line 79 (`test_python_runtime_and_version`): replace `sys.version_info[:2] == (3, 13)` with `(3, 12) <= sys.version_info[:2] < (3, 14)` to match `pyproject.toml`.
   * Line 268 (`test_config_validation_rejects_unsafe_env_values`): guard Playwright check so environments without pre-cached Chromium don't crash (`if cfg.browser_executable is not None: assert cfg.browser_executable.is_file()`).
   * Lines 1604–1640: rename `test_repo_git_hygiene_and_legacy_backup` → `test_repo_git_hygiene`, remove the `REPO_ROOT.parent / "PEA-legacy-backup"` local filesystem assertions, and retain all git-tracked forbidden-extension, forbidden-prefix, and literal home-path checks.
2. **`[MODIFY] tests/test_swf_meta.py`:**
   * In `test_inspect_real_staged_swfs`, call `pytest.skip(f"Optional staged SWF not present: {swf_path.name}")` when `not swf_path.is_file()` (synthetic FWS/CWS/ZWS and corrupted-header tests run unconditionally everywhere).
3. **`[MODIFY] tests/test_sim_protocols.py`:**
   * Add a lightweight `_write_synthetic_cws_swf(tmp_path: Path, *, width: int = 800, height: int = 600) -> tuple[Path, str]` helper building a valid 800×600 CWS SWF header + SHA-256 in `tmp_path` (zero dependency on `local_assets/sims/m96220.swf`).
   * Update `test_runtime_server_swf_mode_security_and_auto_sized_html` and `test_runtime_server_ipv6_loopback_and_host_guard` to use the synthetic SWF so all `SimRuntimeServer` host-guard, traversal, MIME, and IPv6 loopback checks execute on every fresh clone and CI runner.
4. **`[MODIFY] tests/test_ocr_and_profiles.py`:**
   * Guard `test_rapid_ocr_lab4_crops_exact_value_sign_unit` with `@pytest.mark.skipif(not LAB4_CROPS_DIR.is_dir(), reason="Optional Lab 4 benchmark crops not staged")`.
   * Add `test_rapid_ocr_synthetic_rendered_readouts(tmp_path)` that renders high-contrast numeric/unit readouts (`10.00 V`, `-60,32 мкТл`) with bundled `local_assets/fonts/NotoSans-Bold.ttf` via Pillow and verifies real ONNX `RapidOcrBackend.read_spec(...)` parses values, negative signs, and units accurately on any machine.
5. **`[MODIFY] tests/test_pdf_intake.py`:**
   * Add `test_inspect_pdf_synthetic_document(tmp_path)` generating a minimal 2-page PDF in `tmp_path` (via Pillow PDF save + `pypdf`) and verifying `inspect_pdf` page rendering, text extraction, SHA-256 provenance, and immutability checks run unconditionally even when `lab5-rlc-handout.pdf` is absent.

---

### 2.3 Root Directory Tidying & Final Milestone 2 Findings

1. **Populate Verified Milestone 2 Results:**
   * Update Observed Findings in `MILESTONE_2_PLAN.md` (§6), `M2B_PLAN.md` (§6), `M2C_PLAN.md` (§6), and `M2D_PLAN.md` (§7) with final verified metrics (`98c9033`, `210/210` tests passing, `tau = 1.001 s` / `0.07%` dev, `f_peak = 1.995 Hz` / `0.27%` dev, `dist/pea-mcp-0.3.0.mcpb` audited).
2. **Organize Planning Docs Under `docs/plans/`:**
   * Move the 9 historical planning files (`PLAN_V2.md`, `PHASE_0_PLAN.md`, `PHASE_1_PLAN.md`, `PHASE_2_PLAN.md`, `PHASE_3_PLAN.md`, `MILESTONE_2_PLAN.md`, `M2B_PLAN.md`, `M2C_PLAN.md`, `M2D_PLAN.md`) from repo root into `docs/plans/` via `git mv`.
   * Update relative markdown links in `README.md` (`docs/plans/PLAN_V2.md`) and inside `docs/plans/` so zero broken relative links exist.

---

### 2.4 Standard Community & Governance Files

1. **`[NEW] CHANGELOG.md`:**
   * Document `v0.3.0` (Milestone 2: `sim_watch`, `compute_sim`, HTML5/URL SHA-256 fingerprinting, 18 MCP tools, `pea-viewer` Datasets & Model Outputs card, `.mcpb v0.3.0`, live HTML5 RC/RLC E2E benchmark), `v0.2.0` (Phase 2–3: 16 MCP tools, `pea-viewer`, PDF/DOCX/Teacher Packet compiler, Lab 4 & Lab 5 live E2E verification), and `v0.1.0` (Phase 0–1 foundations).
2. **`[NEW] SECURITY.md`:**
   * Document supported version (`0.3.x`), security boundaries (zero-API-cost operation, loopback-only simulator & viewer defaults, Ruffle `allowScriptAccess: false` / `allowNetworking: "none"`, `compute_sim` zero-`eval`/`exec` AST sandbox + resource caps, strict path confinement, `.mcpb` audit gate), and private vulnerability reporting instructions (GitHub Security Advisories).
3. **`[NEW] CONTRIBUTING.md`:**
   * Cross-platform development setup (`uv venv`, `fetch_assets.py`, `playwright install chromium`), required checks (`ruff check`, `pytest tests/ -m "not live_sim"`, `pytest tests/e2e/test_transient_m2_e2e.py -m live_sim`, `build_mcpb.py --check`), guide for contributing new `CalibrationProfile`s or self-contained HTML5 simulator fixtures, and PR checklist.
4. **`[NEW] .github/ISSUE_TEMPLATE/bug_report.md`, `.github/ISSUE_TEMPLATE/feature_request.md`, `.github/PULL_REQUEST_TEMPLATE.md`:**
   * Lightweight templates capturing OS, Python version (`3.12`/`3.13`), MCP client / `.mcpb` version, OCR backend, and PR verification checklist.

---

### 2.5 README Showcase & Curated Visuals

1. **`[NEW] docs/images/`:**
   * Generate and commit 2–3 lightweight curated PNGs exclusively from the open-source `tests/fixtures/html5_rc_sim` benchmark (zero university handouts, zero host home paths):
     * `docs/images/viewer-datasets-desktop.png` (`pea-viewer` showing `Datasets & Model Outputs` with `simulator_captured` & `model_computed` badges + SHA-256 verification).
     * `docs/images/report-transient-plots.png` (`page-003.png` from the transient benchmark showing RC discharge `sim_watch` vs `compute_sim` RK45 overlay + damped RLC Hann FFT spectrum).
2. **`[MODIFY] README.md`:**
   * Add status/metadata badges (`CI`, `License: MIT`, `Python 3.12–3.13`, `MCP stdio`, `Zero API Cost`).
   * Replace macOS-arm64-specific `uv venv` line with cross-platform `uv venv .venv --python 3.13` (plus `python3 -m venv .venv` and Windows PowerShell activation notes).
   * Add a concise **Architecture** Mermaid flowchart (`Host Agent` ↔ `pea-mcp (18 tools)` ↔ `Playwright/Ruffle/HTML5`, `RapidOCR`, `NumPy/SciPy`, `Matplotlib/Schemdraw/PDF/DOCX` ↔ `pea-viewer`).
   * Add a **60-Second Self-Contained Demo** running against `tests/fixtures/html5_rc_sim` (`pytest tests/e2e/test_transient_m2_e2e.py -m live_sim` + `pea-viewer`), showcase the curated images, and link `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `THIRD_PARTY_NOTICES.md`, and `docs/plans/`.

---

## 3. Pass 2 — CI/CD Workflows, Minor Hardening & `v0.3.0` Release Prep

### 3.1 Minor Engineering Polish

1. **`[MODIFY] src/pea/sim/engine.py` (`SimulatorManager.open`):**
   * Mirror `compute_sim`'s orphan-run cleanup: if `open(..., run_id=None)` creates a fresh `RunWorkspace` and then fails before returning a live session (e.g., invalid `entry_file`, missing `swf_path`/`html5_dir`, non-allowlisted `url`, or browser launch failure) with zero recorded provenance entries, remove the empty workspace directory (`shutil.rmtree(..., ignore_errors=True)`).
   * Add a regression test in `tests/test_sim_protocols.py` verifying zero orphan run directories remain after a failed `SimulatorManager.open(html5_dir=..., entry_file="missing.html")`.

### 3.2 GitHub Actions CI & Release Automation

1. **`[NEW] .github/workflows/ci.yml`:**
   * Triggers on `push` to `main` and `pull_request` targeting `main`.
   * **Job 1 (`lint-and-packaging`)** on `ubuntu-latest`, Python `3.13`:
     * `ruff check src/ tests/ scripts/`
     * `python scripts/build_mcpb.py --check`
     * Tracked-file literal home-path check
   * **Job 2 (`offline-tests`)** matrix `os: [ubuntu-latest, macos-latest]` × `python-version: ["3.12", "3.13"]`:
     * Install `uv`, create venv, `uv pip install -e ".[dev]"`.
     * Cache `local_assets/` keyed by `hashFiles('scripts/fetch_assets.py')` and run `python scripts/fetch_assets.py --fetch && python scripts/fetch_assets.py --check`.
     * Run `pytest tests/ -m "not live_sim" -k "not test_viewer_responsive_desktop_and_mobile_zero_overflow_and_ocr_toggle and not test_report_builders" -v` (or install Playwright Chromium and run the **entire** `-m "not live_sim"` suite on every OS/Python!). Prefer installing Chromium with `actions/cache` on `~/.cache/ms-playwright` so **100% of `tests/ -m "not live_sim"`** runs in CI without skipping viewer/PDF tests!
   * **Job 3 (`live-html5-e2e`)** on `ubuntu-latest`, Python `3.13`:
     * Install Playwright Chromium + `fetch_assets.py --fetch`.
     * Run `pytest tests/e2e/test_transient_m2_e2e.py -m live_sim -v` — verifies live headless Chromium + RapidOCR + `sim_watch` + `compute_sim` + PDF/DOCX/ZIP end-to-end on every PR.
2. **`[NEW] .github/workflows/release.yml`:**
   * Triggers on `push` tags matching `v*` (and `workflow_dispatch`).
   * Verifies `GITHUB_REF_NAME` (`vX.Y.Z`) matches `pyproject.toml` version (`X.Y.Z`).
   * Runs `ruff check src/ tests/ scripts/`, `python scripts/build_mcpb.py --check`, and `python scripts/build_mcpb.py`.
   * Generates `SHA256SUMS.txt` for `dist/pea-mcp-<version>.mcpb` and creates a GitHub Release via `softprops/action-gh-release@v2` (or `gh release create`) attaching `dist/pea-mcp-<version>.mcpb` and `SHA256SUMS.txt`.

### 3.3 Pre-Public Audit, Fresh-Clone Simulation & `v0.3.0` Release Checklist

1. **Fresh-Clone Simulation (Local Dry Run):**
   * Clone `main` into a temporary directory (without copying `local_assets/sims/`, `local_assets/benchmarks/`, `runs/`, or `../PEA-legacy-backup`), create a fresh `.venv`, install `.[dev]` + Chromium, run `scripts/fetch_assets.py --fetch`, and confirm:
     * `.venv/bin/pytest tests/ -m "not live_sim" -v` → **100% pass (zero failures)**
     * `.venv/bin/pytest tests/e2e/test_transient_m2_e2e.py -m live_sim -v` → **1 pass**
     * `.venv/bin/ruff check src/ tests/ scripts/` → **0 errors**
     * `.venv/bin/python scripts/build_mcpb.py --check` → **OK (`v0.3.0`, 18 tools)**
2. **Local Workspace Cleanup:**
   * Delete gitignored stale `dist/pea-mcp-0.2.0.mcpb` and temporary `runs/m2-qa` after generating `docs/images/`.
3. **Owner Gate — `origin/legacy-v1` Branch Audit:**
   * Scan `origin/legacy-v1` (`1,535` files) for any university handouts, SWFs, student names, or secrets; confirm with owner and delete `origin/legacy-v1` (`git push origin --delete legacy-v1`) before making the repository public.
4. **Commit, Push, Verify CI & Tag `v0.3.0`:**
   * Commit Pass 1 + Pass 2 on `main`, push to `origin/main`, confirm all GitHub Actions CI jobs turn green, then create & push annotated tag `v0.3.0` (`git tag -a v0.3.0 -m "PEA v2 v0.3.0"` && `git push origin v0.3.0`) to publish the first `.mcpb v0.3.0` GitHub Release.

---

## 4. Verification Commands & Acceptance Gates

```sh
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/python scripts/build_mcpb.py --check
.venv/bin/pytest tests/ -m "not live_sim" -v
.venv/bin/pytest tests/e2e/test_transient_m2_e2e.py -m live_sim -v
.venv/bin/python -c "import yaml, glob; [yaml.safe_load(open(p)) for p in glob.glob('.github/workflows/*.yml')]"  # or safe line/syntax validation if PyYAML not installed
```

*(Note: PyYAML is not in `pyproject.toml` dependencies; validate workflow YAML cleanly via Python/Node or `actionlint` if installed without adding unnecessary dependencies.)*

Acceptance gates:
* `LICENSE`, `THIRD_PARTY_NOTICES.md`, `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`, `.github/` templates, and `.github/workflows/{ci,release}.yml` present and clean.
* Fresh-clone simulation passes 100% of offline tests + `test_transient_m2_e2e.py` on a repo clone with zero private `sims/` or `benchmarks/`.
* Zero literal host home paths, zero secrets, and zero forbidden binaries across all `git ls-files` and `origin` branches.

---

## 5. Risks & Owner Decisions Required Before Flipping Public

1. **`origin/legacy-v1` Retention vs. Deletion:** `origin/legacy-v1` contains 1,535 legacy prototype files (`field-lab-mcp`, `lab-runner`, `magnetic-field-lab`). Confirm whether to delete `origin/legacy-v1` before making `Mottii/PEA` public.
2. **Copyright Holder Name in `LICENSE`:** Defaulting to `PEA Contributors` (matching `packaging/mcpb/manifest.json`); confirm if you prefer your own name/handle instead.
3. **PyPI Publication Scope:** `v0.3.0` targets source clone (`uv pip install -e .`) + GitHub Release `.mcpb` bundle; publishing wheels to PyPI later would additionally require packaging/wrapping `scripts/fetch_assets.py` inside `src/pea/` so `pea-fetch-assets` works without cloning the repo.
