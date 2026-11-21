"""Phase 1 core verification suite: environment, security config, schemas, workspace, hygiene."""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import pea
from pea.config import (
    PathConfinementError,
    PeaConfig,
    is_loopback_host,
    is_origin_allowed,
    normalize_origin,
    parse_allowed_origins,
    resolve_browser_executable,
    resolve_confined_path,
)
from pea.schemas import (
    ActionStep,
    AnalysisResult,
    CalcRequest,
    CalcResult,
    CalcVariable,
    CalibrationProfile,
    ComparisonOutput,
    ComparisonSpec,
    ComputeSimSpec,
    ControlSpec,
    DatasetSpec,
    DerivedColumnSpec,
    DiagramElement,
    DiagramSpec,
    MeasurementPoint,
    OcrBox,
    OcrResult,
    PlateauResult,
    PlotSeries,
    PlotSpec,
    ProblemItem,
    ProvenanceEntry,
    ReadoutSpec,
    RegressionOutput,
    RegressionSpec,
    ReportBuildResult,
    ReportFigure,
    ReportSectionOptions,
    ReportSpec,
    ReportTable,
    RunStatus,
    SearchSpec,
    SelfControlItem,
    SweepAxis,
    SweepSpec,
    SwfMeta,
    WatchSpec,
)
from pea.workspace import STANDARD_RUN_SUBDIRS, WorkspaceManager, generate_run_id

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SHA256 = "35e4a257134073356c9796965ea3ea2d9362d51b2dc87486dc93e85a0fb96b15"


# ---------------------------------------------------------------------------
# 1. Environment & Core Imports
# ---------------------------------------------------------------------------


def test_python_runtime_and_version() -> None:
    assert sys.version_info[:2] == (3, 13), f"Expected Python 3.13, got {sys.version}"
    assert pea.__version__ == "0.2.0"


@pytest.mark.parametrize(
    "module_name",
    [
        "mcp",
        "playwright",
        "pydantic",
        "rapidocr",
        "onnxruntime",
        "numpy",
        "scipy",
        "matplotlib",
        "schemdraw",
        "pypdfium2",
        "pypdf",
        "docx",
        "PIL",
        "starlette",
        "uvicorn",
        "httpx",
        "pea.config",
        "pea.schemas",
        "pea.workspace",
    ],
)
def test_core_library_imports(module_name: str) -> None:
    mod = importlib.import_module(module_name)
    assert mod is not None


# ---------------------------------------------------------------------------
# 2. Security, Path Confinement & Origin Guards (config.py)
# ---------------------------------------------------------------------------


def test_resolve_confined_path_allows_valid_relative_and_internal_symlink(tmp_path: Path) -> None:
    base = tmp_path / "run_base"
    (base / "evidence").mkdir(parents=True)
    target_file = base / "evidence" / "frame.png"
    target_file.write_bytes(b"png")

    # Standard nested path
    resolved = resolve_confined_path(base, "evidence/frame.png")
    assert resolved == target_file.resolve()

    # Relative path with internal '..' staying inside base
    normalized = resolve_confined_path(base, "evidence/../evidence/frame.png")
    assert normalized == target_file.resolve()

    # Symlink staying inside base
    internal_link = base / "latest.png"
    internal_link.symlink_to(target_file)
    assert resolve_confined_path(base, "latest.png") == target_file.resolve()


def test_resolve_confined_path_blocks_traversal_prefix_and_symlinks(tmp_path: Path) -> None:
    base = tmp_path / "run"
    sibling = tmp_path / "run2"
    outside = tmp_path / "outside"
    base.mkdir()
    sibling.mkdir()
    outside.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    # Traversal escapes
    for bad in [
        "../outside/secret.txt",
        "sub/../../outside/secret.txt",
        "../run2/secret.txt",
        str(outside / "secret.txt"),
        str(sibling / "secret.txt"),
        "/etc/passwd",
        "",
        "evidence/bad\x00name.png",
    ]:
        with pytest.raises(PathConfinementError):
            resolve_confined_path(base, bad)

    # File symlink escape
    file_link = base / "leak.txt"
    file_link.symlink_to(outside / "secret.txt")
    with pytest.raises(PathConfinementError):
        resolve_confined_path(base, "leak.txt")

    # Directory symlink escape
    dir_link = base / "escape_dir"
    dir_link.symlink_to(outside)
    with pytest.raises(PathConfinementError):
        resolve_confined_path(base, "escape_dir/secret.txt")

    # Dangling symlink pointing outside base
    dangling = base / "dangling_link"
    dangling.symlink_to(outside / "future_file.txt")
    with pytest.raises(PathConfinementError):
        resolve_confined_path(base, "dangling_link")


def test_is_origin_allowed_enforces_loopback_and_strict_allowlist(tmp_path: Path) -> None:
    base_cfg = PeaConfig(
        project_root=tmp_path,
        local_assets_dir=tmp_path / "local_assets",
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
    )
    loopback = "http://127.0.0.1:5190"

    # Exact loopback allowed
    assert is_origin_allowed("http://127.0.0.1:5190/", loopback, base_cfg) is True
    assert is_origin_allowed("http://127.0.0.1:5190/sim/index.html?a=1", loopback, base_cfg) is True

    # Different loopback port or host blocked when not allowlisted
    assert is_origin_allowed("http://127.0.0.1:9999/", loopback, base_cfg) is False
    assert is_origin_allowed("http://localhost:5190/", loopback, base_cfg) is False

    # External origins blocked by default
    phet_url = "https://phet.colorado.edu/sims/html/rlc.html"
    assert is_origin_allowed(phet_url, loopback, base_cfg) is False

    # Non-HTTP(S) schemes & credential tricks blocked
    for malicious in [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<h1>hi</h1>",
        "ws://127.0.0.1:5190/ws",
        "http://127.0.0.1:5190@evil.com/",
        "http://127.0.0.1.evil.com:5190/",
        "",
        "not a url",
    ]:
        assert is_origin_allowed(malicious, loopback, base_cfg) is False

    # Non-loopback loopback_origin parameter itself must never be trusted
    assert is_origin_allowed("https://evil.com/a", "https://evil.com", base_cfg) is False

    # Explicit PEA_ALLOWED_ORIGINS allowlist
    allow_cfg = PeaConfig.from_env(
        {
            "PEA_PROJECT_ROOT": str(tmp_path),
            "PEA_ALLOWED_ORIGINS": "https://phet.colorado.edu/, http://127.0.0.1:8123",
        }
    )
    assert allow_cfg.allowed_origins == ("https://phet.colorado.edu", "http://127.0.0.1:8123")
    assert is_origin_allowed("https://phet.colorado.edu/en/sim", loopback, allow_cfg) is True
    assert is_origin_allowed("https://phet.colorado.edu:443/en/sim", loopback, allow_cfg) is True
    assert is_origin_allowed("http://127.0.0.1:8123/app.html", loopback, allow_cfg) is True

    # Scheme, port, subdomain, suffix, and userinfo spoofing still blocked
    for spoof in [
        "http://phet.colorado.edu/en/sim",
        "https://phet.colorado.edu:8443/en/sim",
        "https://evil.phet.colorado.edu/en/sim",
        "https://phet.colorado.edu.evil.com/en/sim",
        "https://phet.colorado.edu@evil.com/en/sim",
    ]:
        assert is_origin_allowed(spoof, loopback, allow_cfg) is False


def test_config_validation_rejects_unsafe_env_values(tmp_path: Path) -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("0.0.0.0") is False

    with pytest.raises(ValueError):
        PeaConfig.from_env({"PEA_PROJECT_ROOT": str(tmp_path), "PEA_LOOPBACK_HOST": "0.0.0.0"})

    with pytest.raises(ValueError):
        PeaConfig.from_env({"PEA_PROJECT_ROOT": str(tmp_path), "PEA_VIEWER_PORT": "0"})

    with pytest.raises(ValueError):
        PeaConfig.from_env({"PEA_PROJECT_ROOT": str(tmp_path), "PEA_OCR_BACKEND": "tesseract"})

    with pytest.raises(ValueError):
        parse_allowed_origins("https://*.colorado.edu")

    with pytest.raises(ValueError):
        normalize_origin("https://example.com/with/path")

    with pytest.raises(FileNotFoundError):
        resolve_browser_executable({"PEA_BROWSER": str(tmp_path / "does-not-exist")})

    # Default host config resolves cached Chromium cleanly
    cfg = PeaConfig.from_env({"PEA_PROJECT_ROOT": str(REPO_ROOT)})
    assert cfg.project_root == REPO_ROOT.resolve()
    assert cfg.browser_executable is not None
    assert cfg.browser_executable.is_file()


# ---------------------------------------------------------------------------
# 3. Frozen Pydantic v2 Schema Strictness & Round-Trips (schemas.py)
# ---------------------------------------------------------------------------


def test_schemas_reject_unknown_extra_fields() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValidationError):
        CalibrationProfile(
            swf_sha256=SAMPLE_SHA256,
            stage_width=800,
            stage_height=600,
            unexpected_field=True,  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        SearchSpec(
            mode="plateau_max",
            sweep_control="omega",
            readout="uc_over_u",
            start=2500,
            step=-1,
            extra_flag=123,  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        CalcRequest(expression="1 / sqrt(L * C)", bogus="nope")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        ReportSpec(title="Lab 5", secret_key="nope")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        RunStatus(run_id="r1", created_at=now, updated_at=now, unknown="x")  # type: ignore[call-arg]


def test_schemas_enforce_domain_invariants() -> None:
    # Bad SHA-256
    with pytest.raises(ValidationError):
        CalibrationProfile(swf_sha256="not-a-sha", stage_width=800, stage_height=600)

    # Inverted stepper bounds
    with pytest.raises(ValidationError):
        ControlSpec(name="R", kind="stepper", min_val=10.0, max_val=3.0)

    # Zero-width ROI
    with pytest.raises(ValidationError):
        ReadoutSpec(name="uc", roi_px=(100, 100, 100, 130))

    # Zero step or missing threshold target
    with pytest.raises(ValidationError):
        SearchSpec(mode="max", sweep_control="omega", readout="uc", start=2500, step=0)
    with pytest.raises(ValidationError):
        SearchSpec(mode="threshold", sweep_control="omega", readout="uc", start=2500, step=-1)

    # ActionStep missing required arguments
    with pytest.raises(ValidationError):
        ActionStep(action="drag", x=1, y=2)
    with pytest.raises(ValidationError):
        ActionStep(action="set_stepper", control="R")

    # Mismatched PlotSeries lengths
    with pytest.raises(ValidationError):
        PlotSeries(label="s", x=[1.0, 2.0], y=[1.0])

    # Mismatched ReportTable row width
    with pytest.raises(ValidationError):
        ReportTable(table_id="t1", caption="Cap", headers=["A", "B"], rows=[["1"]])

    # ReportFigure without any asset
    with pytest.raises(ValidationError):
        ReportFigure(figure_id="f1", caption="Cap")

    # Path traversal in ProvenanceEntry
    with pytest.raises(ValidationError):
        ProvenanceEntry(
            timestamp=datetime.now(UTC),
            kind="screenshot",
            relpath="../escape.png",
            sha256=SAMPLE_SHA256,
            size_bytes=10,
        )

    # PlateauResult midpoint outside bounds
    with pytest.raises(ValidationError):
        PlateauResult(
            control="omega",
            readout="uc_over_u",
            peak_value=3.94,
            low_bound=2445.0,
            high_bound=2472.0,
            midpoint=2500.0,
            width_steps=28,
        )


def test_schemas_json_round_trip_core_contracts() -> None:
    now = datetime.now(UTC)

    profile = CalibrationProfile(
        swf_sha256=SAMPLE_SHA256,
        stage_width=800,
        stage_height=600,
        header_height_px=39,
        controls={
            "R": ControlSpec(
                name="R",
                kind="stepper",
                input_xy=(644, 289),
                up_xy=(678, 282),
                down_xy=(678, 296),
                unit="Ом",
                step=0.1,
                min_val=3.0,
                max_val=10.0,
                decimal_comma=True,
                commit_mode="enter",
                blur_xy=(450, 18),
            )
        },
        readouts={
            "uc_over_u": ReadoutSpec(
                name="uc_over_u",
                roi_px=(625, 474, 710, 502),
                ocr_mode="rec_only",
                pad_px=0,
                scale=1.0,
                parse_mode="float",
                normalize_homoglyphs=True,
            )
        },
        blur_xy=(450, 18),
        verified_at=now,
        notes="m96220.swf Phase 0 calibration",
    )
    assert CalibrationProfile.model_validate_json(profile.model_dump_json()) == profile

    sweep = SweepSpec(
        fixed_controls={"L": 5.0, "C": 32},
        axes=[SweepAxis(control="R", values=[3.2, 4.0, 5.0, 6.0])],
        readouts=["omega0", "uc_over_u"],
        settle_ms=150,
        verify_controls=True,
    )
    assert SweepSpec.model_validate_json(sweep.model_dump_json()) == sweep

    search = SearchSpec(
        mode="plateau_max",
        fixed_controls={"L": 5.0, "C": 32, "R": 5.0},
        sweep_control="omega",
        readout="uc_over_u",
        start=2500.0,
        step=-1.0,
        max_steps=120,
        stop_after_drop=2,
        settle_ms=150,
    )
    assert search.max_steps == 120
    assert SearchSpec.model_validate_json(search.model_dump_json()) == search

    point = MeasurementPoint(
        index=0,
        requested_settings={"R": 3.2, "omega": 2472},
        observed_settings={"R": 3.2, "omega": 2472},
        readout_name="uc_over_u",
        raw_ocr_text="3,94",
        parsed_value=3.94,
        parsed_unit="1",
        screenshot_relpath="evidence/step-000.png",
        screenshot_sha256=SAMPLE_SHA256,
        provenance="simulator_captured",
        captured_at=now,
        controls_matched=True,
    )
    plateau = PlateauResult(
        control="omega",
        readout="uc_over_u",
        peak_value=3.94,
        low_bound=2445.0,
        high_bound=2472.0,
        midpoint=2458.5,
        width_steps=28,
        trace=[point],
        boundary_screenshots={"low": "evidence/low.png", "high": "evidence/high.png"},
    )
    assert PlateauResult.model_validate_json(plateau.model_dump_json()) == plateau

    swf_meta = SwfMeta(
        path="local_assets/sims/m96220.swf",
        sha256=SAMPLE_SHA256,
        swf_version=9,
        compressed=True,
        signature="CWS",
        stage_width=800,
        stage_height=600,
        fps=24.0,
        frame_count=2,
    )
    assert SwfMeta.model_validate_json(swf_meta.model_dump_json()) == swf_meta

    ocr_res = OcrResult(
        backend="rapidocr",
        mode="rec_only",
        elapsed_ms=17.4,
        boxes=[
            OcrBox(
                text="2444",
                confidence=1.0,
                bbox_px=(620, 374, 667, 401),
                bbox_norm=(0.68, 0.53, 0.74, 0.57),
            )
        ],
    )
    assert OcrResult.model_validate_json(ocr_res.model_dump_json()) == ocr_res

    watch = WatchSpec(
        readouts=["voltage_c"],
        duration_s=10.0,
        interval_ms=200,
        trigger_action=ActionStep(action="click", x=100, y=200),
    )
    assert WatchSpec.model_validate_json(watch.model_dump_json()) == watch

    compute = ComputeSimSpec(
        model_kind="algebraic_grid",
        expression="1 / sqrt((1 - (w/w0)**2)**2 + (w/(w0*Q))**2)",
        parameters={"w0": 2500.0, "Q": 3.90625},
        grid_variable="w",
        grid_start=2000.0,
        grid_stop=3000.0,
    )
    assert ComputeSimSpec.model_validate_json(compute.model_dump_json()) == compute

    calc_req = CalcRequest(
        expression="1 / sqrt(L * C)",
        variables={
            "L": CalcVariable(value=5.0, unit="mH"),
            "C": CalcVariable(value=32.0, unit="uF"),
        },
        output_unit="rad/s",
        label="natural_frequency",
    )
    assert CalcRequest.model_validate_json(calc_req.model_dump_json()) == calc_req

    calc_res = CalcResult(
        calc_id="calc_001",
        expression=calc_req.expression,
        si_value=2500.0,
        si_dims=(0, 0, -1, 0, 0),
        output_value=2500.0,
        output_unit="rad/s",
    )
    assert CalcResult.model_validate_json(calc_res.model_dump_json()) == calc_res

    dataset = DatasetSpec(
        dataset_id="ds_lab5",
        source="simulator_captured",
        rows=[{"R": 3.2, "omega_res": 2458.5}],
        units={"R": "Ohm", "omega_res": "rad/s"},
    )
    derived = DerivedColumnSpec(name="delta_omega", formula="2500 - omega_res", unit="rad/s")
    reg_spec = RegressionSpec(name="q2_fit", x_column="Q2", y_column="ratio", intercept="zero")
    comp_spec = ComparisonSpec(name="slope_check", measured=3.95, reference=4.0, unit="1")
    analysis = AnalysisResult(
        dataset_id="ds_lab5",
        source="simulator_captured",
        row_count=4,
        columns=["R", "omega_res", "delta_omega"],
        derived_columns=[derived.name],
        regressions=[
            RegressionOutput(
                name=reg_spec.name,
                intercept_mode="zero",
                slope=3.95,
                slope_se=0.04,
                intercept=0.0,
                r_squared=0.999,
                n_points=4,
            )
        ],
        comparisons=[
            ComparisonOutput(
                name=comp_spec.name,
                measured=3.95,
                reference=4.0,
                abs_diff=-0.05,
                percent_deviation=-1.25,
            )
        ],
    )
    assert DatasetSpec.model_validate_json(dataset.model_dump_json()) == dataset
    assert AnalysisResult.model_validate_json(analysis.model_dump_json()) == analysis

    plot = PlotSpec(
        plot_id="fig_resonance",
        title="Resonance Shift",
        x_label="Q^2",
        y_label="omega_0 / Delta omega",
        series=[PlotSeries(label="Measured", x=[6.0, 15.2], y=[24.1, 60.2])],
    )
    assert PlotSpec.model_validate_json(plot.model_dump_json()) == plot

    diagram = DiagramSpec(
        diagram_id="fig_circuit",
        kind="circuit",
        title="Series RLC Circuit",
        elements=[
            DiagramElement(kind="resistor", label="R"),
            DiagramElement(kind="inductor", label="L"),
        ],
    )
    assert DiagramSpec.model_validate_json(diagram.model_dump_json()) == diagram

    report = ReportSpec(
        title="Laboratory Work 5: Forced Oscillations in an RLC Circuit",
        language="en",
        team="Team 2",
        tables=[
            ReportTable(
                table_id="table_2",
                caption="Resonance Frequency Shift vs Resistance",
                headers=["R (Ohm)", "omega_res (rad/s)"],
                rows=[["3.2", "2458.5"]],
            )
        ],
        figures=[
            ReportFigure(
                figure_id="fig_1",
                caption="Series RLC schematic",
                svg_relpath="figures/fig_1.svg",
                png_relpath="figures/fig_1.png",
            )
        ],
        options=ReportSectionOptions(include_self_control=True, include_problems=True),
        self_control=[
            SelfControlItem(
                number="1",
                question="Define Q-factor.",
                answer="Q = (1/R)*sqrt(L/C).",
                figure_ids=["fig_1"],
            )
        ],
        problems=[
            ProblemItem(
                number="4C",
                prompt="Plot q-I ellipse.",
                solution="Phase portrait computed.",
            )
        ],
    )
    assert ReportSpec.model_validate_json(report.model_dump_json()) == report

    build_res = ReportBuildResult(
        run_id="2026-11-20T16-00-00-000Z-abcd1234",
        pdf_relpath="reports/report.pdf",
        pdf_sha256=SAMPLE_SHA256,
        page_count=5,
    )
    assert ReportBuildResult.model_validate_json(build_res.model_dump_json()) == build_res


# ---------------------------------------------------------------------------
# 4. Run Workspace, Provenance Tamper Detection & Crash Recovery (workspace.py)
# ---------------------------------------------------------------------------


def test_workspace_run_lifecycle_idempotency_and_ordering(tmp_path: Path) -> None:
    mgr = WorkspaceManager(tmp_path / "runs")

    # Run ID format check
    rid = generate_run_id()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z-[0-9a-f]{8}$", rid)

    run1 = mgr.create_run(request_id="req-alpha", label="Lab 5 Team 2", source="simulator_captured")
    for sub in STANDARD_RUN_SUBDIRS:
        assert (run1.root / sub).is_dir()
    assert run1.status_path.is_file()
    assert not (run1.root / "status.tmp").exists()

    # Idempotent request_id lookup returns same workspace
    run1_dup = mgr.create_run(request_id="req-alpha", label="Should Not Duplicate")
    assert run1_dup.run_id == run1.run_id
    assert len(mgr.list_runs()) == 1

    # Ensure distinct creation timestamp for ordering check
    time.sleep(0.01)
    run2 = mgr.create_run(label="Lab 4 Wire")
    updated = run2.update_status(
        phase="running",
        message="Sweeping I=-5A",
        progress_current=3,
        progress_total=9,
        metadata_updates={"sim": "m96210"},
    )
    assert updated.phase == "running"
    assert updated.progress_current == 3
    assert updated.metadata["sim"] == "m96210"
    assert not (run2.root / "status.tmp").exists()

    # list_runs returns newest-first and ignores stray files/dirs
    (mgr.runs_dir / "stray_dir").mkdir()
    (mgr.runs_dir / ".DS_Store").write_bytes(b"junk")
    listed = mgr.list_runs()
    assert [r.run_id for r in listed] == [run2.run_id, run1.run_id]

    # get_run enforces confinement
    with pytest.raises(PathConfinementError):
        mgr.get_run("../outside")
    with pytest.raises(PathConfinementError):
        mgr.get_run("a/b")


def test_workspace_provenance_verify_and_tamper_detection(tmp_path: Path) -> None:
    mgr = WorkspaceManager(tmp_path / "runs")
    run = mgr.create_run(label="Provenance Test")

    # Empty provenance passes cleanly
    empty_res = run.verify_provenance()
    assert empty_res.ok is True
    assert bool(empty_res) is True
    assert empty_res.checked_entries == 0

    img = run.evidence_dir / "step-001.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\noriginal-frame-bytes")
    entry = run.record_artifact(
        "evidence/step-001.png",
        kind="screenshot",
        metadata={"omega": 2458},
    )
    assert entry.relpath == "evidence/step-001.png"
    assert entry.size_bytes == len(img.read_bytes())

    ok_res = run.verify_provenance()
    assert ok_res.ok is True
    assert ok_res.checked_entries == 1
    assert ok_res.errors == []

    # Reject recording path outside workspace
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(PathConfinementError):
        run.record_artifact("../outside.txt", kind="escape")

    # Tamper bytes -> verify_provenance MUST fail
    img.write_bytes(b"\x89PNG\r\n\x1a\ntampered-frame-bytes!")
    tampered_res = run.verify_provenance()
    assert tampered_res.ok is False
    assert bool(tampered_res) is False
    assert any("sha256 mismatch" in err for err in tampered_res.errors)

    # Delete artifact -> verify_provenance MUST fail
    img.unlink()
    missing_res = run.verify_provenance()
    assert missing_res.ok is False
    assert any("missing artifact" in err for err in missing_res.errors)

    # Replace artifact with symlink escaping run root -> verify_provenance MUST fail
    img.symlink_to(outside)
    escape_res = run.verify_provenance()
    assert escape_res.ok is False
    assert any("path escape" in err for err in escape_res.errors)

    # Corrupted JSONL line -> verify_provenance reports error cleanly without crashing
    run.provenance_path.write_text("{not valid json\n", encoding="utf-8")
    corrupt_res = run.verify_provenance()
    assert corrupt_res.ok is False
    assert any("invalid provenance JSON" in err for err in corrupt_res.errors)


def test_workspace_recover_interrupted_preserves_evidence_and_terminals(tmp_path: Path) -> None:
    mgr = WorkspaceManager(tmp_path / "runs")

    r_created = mgr.create_run(label="created-run")
    r_running = mgr.create_run(label="running-run")
    r_complete = mgr.create_run(label="complete-run")
    r_cancelled = mgr.create_run(label="cancelled-run")
    r_failed = mgr.create_run(label="failed-run")

    # Add partial evidence to running run
    partial_file = r_running.evidence_dir / "partial-001.png"
    partial_file.write_bytes(b"partial-evidence")
    r_running.record_artifact("evidence/partial-001.png", kind="screenshot")
    r_running.update_status(phase="running", message="Step 12/50")

    complete_status = r_complete.update_status(phase="complete", message="Done")
    cancelled_status = r_cancelled.update_status(phase="cancelled", message="User stopped")
    failed_status = r_failed.update_status(phase="failed", error="Timeout")

    recovered = mgr.recover_interrupted_runs()
    recovered_ids = {s.run_id for s in recovered}
    assert recovered_ids == {r_created.run_id, r_running.run_id}

    assert r_created.read_status().phase == "interrupted"
    assert r_running.read_status().phase == "interrupted"

    # Partial evidence and provenance ledger remain completely intact
    assert partial_file.read_bytes() == b"partial-evidence"
    assert r_running.verify_provenance().ok is True

    # Terminal runs remain untouched (including exact updated_at timestamps)
    assert r_complete.read_status() == complete_status
    assert r_cancelled.read_status() == cancelled_status
    assert r_failed.read_status() == failed_status

    # Running recovery a second time is a no-op
    assert mgr.recover_interrupted_runs() == []


# ---------------------------------------------------------------------------
# 5. Offline Assets & Repository Hygiene
# ---------------------------------------------------------------------------


def test_offline_assets_check_and_tamper_detection(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "fetch_assets.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"fetch_assets --check failed:\n{proc.stdout}\n{proc.stderr}"
    assert "katex, ocr, ruffle, fonts" in proc.stdout

    # Verify manifest exists and contains all 4 pinned groups
    manifest = json.loads((REPO_ROOT / "local_assets" / "manifest.json").read_text("utf-8"))
    assert set(manifest["groups"].keys()) == {"katex", "ocr", "ruffle", "fonts"}
    assert manifest["groups"]["ruffle"]["version"] == "0.7.1"

    # Negative check: empty assets directory fails cleanly
    bad_proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "fetch_assets.py"),
            "--check",
            "--assets-dir",
            str(tmp_path / "empty_assets"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_proc.returncode == 1
    assert "MISSING" in bad_proc.stderr


def test_repo_git_hygiene_and_legacy_backup() -> None:
    # Legacy backup exists outside git
    backup_root = REPO_ROOT.parent / "PEA-legacy-backup"
    assert (backup_root / ".git").is_dir()
    assert (backup_root / "magnetic-field-lab").is_dir()
    assert (backup_root / "lab-runner").is_dir()
    assert (backup_root / "field-lab-mcp").is_dir()
    assert (backup_root / "outputs" / "swf-pilot" / "measurements.json").is_file()

    # Tracked files must never contain forbidden binaries, runtime dirs, legacy dirs, or home paths
    ls_proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = [line for line in ls_proc.stdout.splitlines() if line.strip()]
    assert len(tracked) > 0, "Expected committed files on clean main"
    forbidden_exts = (".swf", ".pdf", ".docx", ".zip", ".onnx", ".wasm", ".mcpb")
    forbidden_prefixes = (
        ".venv/",
        "local_assets/",
        "runs/",
        "dist/",
        "profiles/local/",
        "magnetic-field-lab/",
        "lab-runner/",
        "field-lab-mcp/",
        "swf-pilot/",
    )
    home_marker = "/" + "Users/"

    for rel in tracked:
        assert not rel.lower().endswith(forbidden_exts), f"Forbidden binary tracked in git: {rel}"
        assert not rel.startswith(forbidden_prefixes), f"Forbidden directory tracked in git: {rel}"
        content = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        assert home_marker not in content, f"Literal home path found in tracked file: {rel}"
