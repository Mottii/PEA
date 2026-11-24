"""Live E2E benchmark for Milestone 2 (HTML5 RC Discharge & Damped RLC Transients).

Drives ``pea-mcp`` over real ``mcp.client.stdio.stdio_client`` against the deterministic,
zero-network HTML5 fixture ``tests/fixtures/html5_rc_sim/index.html``:
1. ``sim_open(html5_dir=...)`` -> deterministic ``sim_sha256`` + zero aborted requests.
2. ``sim_observe(annotate=True)`` -> ``sim_save_profile(verify_live=True)`` -> ``sim_close`` ->
   re-open verifying automatic ``profile_loaded is True``.
3. RC discharge ``sim_watch`` (``inline_ocr`` with threshold stop at ``Vc <= 1.5 V``) ->
   ``data_analyze`` (``ln(Vc)`` vs ``elapsed_s`` OLS) recovering ``tau = RC = 1.00 s`` (< 5%).
4. ``compute_sim(model_kind="ode_ivp")`` solving ``dVc/dt = -Vc / (R * C)`` tagged
   ``model_computed``, cross-checked pointwise against live ``sim_watch`` OCR readings.
5. Damped RLC ``sim_watch`` (``burst_post_ocr``, ``f0 = 2.0 Hz``, ``beta = 0.25 s^-1``) ->
   ``compute_sim(model_kind="fft_spectrum")`` with Hann windowing & automatic jitter resampling
   recovering ``f0 = 2.0 Hz`` within FFT bin resolution.
6. ``diagram_render`` + ``plot_render`` (RC overlay & RLC FFT spectrum) -> ``report_build``
   (A4 PDF, editable ``.docx``, ``evidence.zip``, Teacher Evidence PDF) -> ``report_preview`` ->
   ``lab_runs(action="verify").verification.ok is True``.

Set ``PEA_E2E_KEEP_DIR`` (e.g., ``runs/m2-qa``) to preserve run artifacts for Visual QA and
``pea-viewer`` inspection.
"""

from __future__ import annotations

import json
import math
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import docx
import numpy as np
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pea.config import resolve_browser_executable

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ASSETS_DIR = REPO_ROOT / "local_assets"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "html5_rc_sim"


def _parse_tool_json(result: Any) -> dict[str, Any]:
    assert result.is_error is False, f"Unexpected MCP tool error: {result.content}"
    if getattr(result, "structured_content", None) is not None:
        return dict(result.structured_content)
    assert result.content, "Expected non-empty content from MCP tool"
    return json.loads(result.content[0].text)


def _resolve_e2e_dirs(tmp_path: Path, suite_name: str) -> tuple[Path, Path]:
    keep_raw = os.environ.get("PEA_E2E_KEEP_DIR", "").strip()
    if keep_raw:
        base = Path(keep_raw)
        if not base.is_absolute():
            base = (REPO_ROOT / base).resolve()
        runs_dir = base / "runs"
        profiles_dir = base / "profiles"
    else:
        runs_dir = tmp_path / suite_name / "runs"
        profiles_dir = tmp_path / suite_name / "profiles"
    runs_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir, profiles_dir


def _build_html5_rc_profile(sim_sha256: str) -> dict[str, Any]:
    """Calibration profile for ``tests/fixtures/html5_rc_sim/index.html`` (640x420)."""
    return {
        "schema_version": 1,
        "swf_sha256": sim_sha256,
        "stage_width": 640,
        "stage_height": 420,
        "header_height_px": 0,
        "blur_xy": [320, 18],
        "controls": {
            "R": {
                "name": "R",
                "kind": "stepper",
                "input_xy": [151, 83],
                "readout_roi_px": [110, 68, 192, 98],
                "unit": "kOhm",
                "step": 0.1,
                "min_val": 0.1,
                "max_val": 1000.0,
                "decimal_comma": False,
                "commit_mode": "enter",
            },
            "C": {
                "name": "C",
                "kind": "stepper",
                "input_xy": [337, 83],
                "readout_roi_px": [296, 68, 378, 98],
                "unit": "uF",
                "step": 0.1,
                "min_val": 0.1,
                "max_val": 10000.0,
                "decimal_comma": False,
                "commit_mode": "enter",
            },
            "V0": {
                "name": "V0",
                "kind": "stepper",
                "input_xy": [525, 83],
                "readout_roi_px": [484, 68, 566, 98],
                "unit": "V",
                "step": 0.1,
                "min_val": 0.1,
                "max_val": 100.0,
                "decimal_comma": False,
                "commit_mode": "enter",
            },
            "mode_rc": {
                "name": "mode_rc",
                "kind": "button",
                "click_xy": [95, 135],
            },
            "mode_rlc": {
                "name": "mode_rlc",
                "kind": "button",
                "click_xy": [223, 135],
            },
            "reset": {
                "name": "reset",
                "kind": "button",
                "click_xy": [417, 135],
            },
            "start": {
                "name": "start",
                "kind": "button",
                "click_xy": [545, 135],
            },
        },
        "readouts": {
            "t": {
                "name": "t",
                "roi_px": [65, 252, 280, 296],
                "ocr_mode": "rec_only",
                "pad_px": 0,
                "scale": 1.0,
                "parse_mode": "float",
                "unit_pattern": "s",
                "normalize_homoglyphs": True,
            },
            "Vc": {
                "name": "Vc",
                "roi_px": [365, 252, 580, 296],
                "ocr_mode": "rec_only",
                "pad_px": 0,
                "scale": 1.0,
                "parse_mode": "float",
                "unit_pattern": "V",
                "normalize_homoglyphs": True,
            },
        },
        "notes": "Deterministic HTML5 RC/RLC transient fixture calibration profile (640x420).",
    }


@pytest.mark.live_sim
@pytest.mark.asyncio
async def test_m2_html5_transient_watch_ode_fft_live_e2e(tmp_path: Path) -> None:
    assert (FIXTURE_DIR / "index.html").is_file(), "HTML5 transient fixture missing"
    browser_exe = resolve_browser_executable()
    if browser_exe is None:
        pytest.skip("Playwright Chromium executable not cached locally")

    runs_dir, profiles_dir = _resolve_e2e_dirs(tmp_path, "transient_m2")
    env = dict(os.environ)
    env.update(
        {
            "PEA_PROJECT_ROOT": str(REPO_ROOT),
            "PEA_LOCAL_ASSETS_DIR": str(LOCAL_ASSETS_DIR),
            "PEA_RUNS_DIR": str(runs_dir),
            "PEA_PROFILES_DIR": str(profiles_dir),
            "PEA_OCR_BACKEND": "rapidocr",
            "PEA_ALLOWED_ORIGINS": "",
            "PEA_BROWSER": str(browser_exe),
        }
    )

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pea.mcp_server"],
        env=env,
        cwd=str(REPO_ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert init.server_info.name == "pea-mcp"

            # ------------------------------------------------------------------
            # 1. Create run, open HTML5 fixture, calibrate, save & reload profile
            # ------------------------------------------------------------------
            created = _parse_tool_json(
                await session.call_tool(
                    "lab_runs",
                    {
                        "action": "create",
                        "label": "Milestone 2 — RC Discharge & Damped RLC Transients",
                        "source": "simulator_captured",
                    },
                )
            )
            run_id = created["run_id"]
            run_root = runs_dir / run_id

            opened = _parse_tool_json(
                await session.call_tool(
                    "sim_open",
                    {
                        "html5_dir": "tests/fixtures/html5_rc_sim",
                        "run_id": run_id,
                        "viewport_width": 640,
                        "viewport_height": 420,
                        "settle_ms": 300,
                    },
                )
            )
            session_id = opened["session_id"]
            sim_sha256 = opened["sim_sha256"]
            assert isinstance(sim_sha256, str) and len(sim_sha256) == 64
            assert opened["swf_meta"] is None
            assert opened["aborted_requests"] == []
            assert opened["page_errors"] == []

            obs = _parse_tool_json(
                await session.call_tool(
                    "sim_observe",
                    {"session_id": session_id, "annotate": True},
                )
            )
            assert obs["annotated_relpath"] is not None

            saved_prof = _parse_tool_json(
                await session.call_tool(
                    "sim_save_profile",
                    {
                        "session_id": session_id,
                        "profile": _build_html5_rc_profile(sim_sha256),
                        "verify_live": True,
                    },
                )
            )
            assert saved_prof["verified_live"] is True
            assert saved_prof["swf_sha256"] == sim_sha256
            saved_profile_relpath = saved_prof["saved_relpath"]

            await session.call_tool("sim_close", {"session_id": session_id})

            # Re-open same HTML5 fixture and assert SHA-256 profile auto-loads
            reopened = _parse_tool_json(
                await session.call_tool(
                    "sim_open",
                    {
                        "html5_dir": "tests/fixtures/html5_rc_sim",
                        "run_id": run_id,
                        "viewport_width": 640,
                        "viewport_height": 420,
                        "settle_ms": 300,
                    },
                )
            )
            session_id = reopened["session_id"]
            assert reopened["profile_loaded"] is True
            assert reopened["sim_sha256"] == sim_sha256
            assert reopened["aborted_requests"] == []

            # ------------------------------------------------------------------
            # 2. RC discharge sim_watch (inline_ocr threshold stop) + OLS fit
            # ------------------------------------------------------------------
            rc_watch = _parse_tool_json(
                await session.call_tool(
                    "sim_watch",
                    {
                        "session_id": session_id,
                        "spec": {
                            "readouts": ["t", "Vc"],
                            "duration_s": 4.0,
                            "interval_ms": 120,
                            "fixed_controls": {"R": 10.0, "C": 100.0, "V0": 10.0},
                            "trigger_actions": [
                                {"action": "click", "control": "mode_rc"},
                                {"action": "click", "control": "reset"},
                                {"action": "click", "control": "start"},
                            ],
                            "stop_readout": "Vc",
                            "stop_op": "le",
                            "stop_value": 1.5,
                            "save_dataset": True,
                            "dataset_id": "rc_watch",
                        },
                    },
                )
            )
            assert rc_watch["pacing_mode"] == "inline_ocr"
            assert rc_watch["status"] == "stopped_threshold"
            assert rc_watch["captured_frames"] >= 5
            assert rc_watch["dataset_id"] == "rc_watch"
            assert rc_watch["dataset_relpath"] == "datasets/rc_watch.json"

            # Theoretical reference tau = R * C via calc_eval
            calc_tau_ref = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "run_id": run_id,
                        "expression": "R * C",
                        "variables": {
                            "R": {"value": 10.0, "unit": "kOhm"},
                            "C": {"value": 100.0, "unit": "uF"},
                        },
                        "output_unit": "s",
                        "label": "rc_tau_reference",
                    },
                )
            )
            tau_ref = float(calc_tau_ref["output_value"])
            assert tau_ref == pytest.approx(1.0, rel=1e-9)

            rc_analysis = _parse_tool_json(
                await session.call_tool(
                    "data_analyze",
                    {
                        "run_id": run_id,
                        "dataset_id": "rc_watch",
                        "derived_columns": [
                            {"name": "ln_Vc", "formula": "log(Vc)", "unit": "1"},
                        ],
                        "regressions": [
                            {
                                "name": "ln_Vc_vs_elapsed",
                                "x_column": "elapsed_s",
                                "y_column": "ln_Vc",
                                "intercept": "free",
                            }
                        ],
                    },
                )
            )
            reg = rc_analysis["regressions"][0]
            slope = float(reg["slope"])
            r_squared = float(reg["r_squared"])
            assert slope < 0.0
            assert r_squared > 0.99

            calc_tau_meas = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "run_id": run_id,
                        "expression": "-1 / slope",
                        "variables": {
                            "slope": {
                                "value": slope,
                                "unit": "s^-1",
                                "uncertainty": float(reg["slope_se"]),
                            }
                        },
                        "output_unit": "s",
                        "label": "rc_tau_measured",
                    },
                )
            )
            tau_meas = float(calc_tau_meas["output_value"])
            tau_rel_err_pct = abs(tau_meas - tau_ref) / tau_ref * 100.0
            assert tau_rel_err_pct < 5.0, (
                f"Recovered tau={tau_meas:.4f} s deviated {tau_rel_err_pct:.2f}% from {tau_ref} s"
            )

            # ------------------------------------------------------------------
            # 3. compute_sim (ode_ivp) analytical RC model & pointwise check
            # ------------------------------------------------------------------
            rc_t_stop = max(2.2, float(rc_watch["actual_duration_s"]) + 0.1)
            rc_ode = _parse_tool_json(
                await session.call_tool(
                    "compute_sim",
                    {
                        "run_id": run_id,
                        "spec": {
                            "model_kind": "ode_ivp",
                            "expression": "-Vc / (R * C)",
                            "state_variables": ["Vc"],
                            "parameters": {"R": 10_000.0, "C": 100e-6},
                            "grid_variable": "t",
                            "grid_start": 0.0,
                            "grid_stop": rc_t_stop,
                            "grid_points": 100,
                            "initial_state": [10.0],
                            "ode_method": "RK45",
                            "dataset_id": "rc_ode",
                            "units": {"t": "s", "Vc": "V"},
                            "notes": "RK45 RC discharge ODE model (R=10 kOhm, C=100 uF)",
                        },
                    },
                )
            )
            assert rc_ode["provenance"] == "model_computed"
            assert rc_ode["dataset_id"] == "rc_ode"
            assert rc_ode["dataset_relpath"] == "datasets/rc_ode.json"
            assert rc_ode["row_count"] == 100

            rc_watch_rows = json.loads(
                (run_root / "datasets" / "rc_watch.json").read_text(encoding="utf-8")
            )["rows"]
            rc_ode_rows = json.loads(
                (run_root / "datasets" / "rc_ode.json").read_text(encoding="utf-8")
            )["rows"]
            ode_t = np.array([float(r["t"]) for r in rc_ode_rows], dtype=np.float64)
            ode_vc = np.array([float(r["Vc"]) for r in rc_ode_rows], dtype=np.float64)

            valid_rc_pts = [
                (float(r["t"]), float(r["elapsed_s"]), float(r["Vc"]))
                for r in rc_watch_rows
                if r.get("t") is not None and r.get("Vc") is not None
            ]
            assert len(valid_rc_pts) >= 5
            max_ode_diff_v = 0.0
            for sim_t, _el_s, meas_vc in valid_rc_pts:
                if ode_t[0] <= sim_t <= ode_t[-1]:
                    model_vc = float(np.interp(sim_t, ode_t, ode_vc))
                    max_ode_diff_v = max(max_ode_diff_v, abs(meas_vc - model_vc))
            assert max_ode_diff_v < 0.35, (
                f"RC live OCR vs RK45 ODE mismatch too large: {max_ode_diff_v:.3f} V"
            )

            # ------------------------------------------------------------------
            # 4. Damped RLC sim_watch (burst_post_ocr) + compute_sim (fft_spectrum)
            # ------------------------------------------------------------------
            rlc_watch = _parse_tool_json(
                await session.call_tool(
                    "sim_watch",
                    {
                        "session_id": session_id,
                        "spec": {
                            "readouts": ["Vc"],
                            "duration_s": 3.0,
                            "interval_ms": 40,
                            "trigger_actions": [
                                {"action": "click", "control": "mode_rlc"},
                                {"action": "click", "control": "reset"},
                                {"action": "click", "control": "start"},
                            ],
                            "save_dataset": True,
                            "dataset_id": "rlc_watch",
                        },
                    },
                )
            )
            assert rlc_watch["pacing_mode"] == "burst_post_ocr"
            assert rlc_watch["status"] == "complete"
            assert rlc_watch["captured_frames"] >= 30
            assert rlc_watch["dataset_id"] == "rlc_watch"
            assert rlc_watch["dataset_relpath"] == "datasets/rlc_watch.json"

            rlc_watch_rows = json.loads(
                (run_root / "datasets" / "rlc_watch.json").read_text(encoding="utf-8")
            )["rows"]
            rlc_vc_vals = [float(r["Vc"]) for r in rlc_watch_rows if r.get("Vc") is not None]
            assert len(rlc_vc_vals) >= 25
            assert min(rlc_vc_vals) < -3.0, "Expected negative signed voltage readings in RLC mode"
            assert max(rlc_vc_vals) > 3.0, "Expected positive signed voltage readings in RLC mode"

            rlc_fft = _parse_tool_json(
                await session.call_tool(
                    "compute_sim",
                    {
                        "run_id": run_id,
                        "spec": {
                            "model_kind": "fft_spectrum",
                            "input_dataset_id": "rlc_watch",
                            "time_column": "elapsed_s",
                            "signal_column": "Vc",
                            "window": "hann",
                            "detrend_mean": True,
                            "dataset_id": "rlc_fft",
                            "notes": "One-sided Hann-windowed FFT of live damped RLC capture",
                        },
                    },
                )
            )
            assert rlc_fft["provenance"] == "model_computed"
            assert rlc_fft["dataset_id"] == "rlc_fft"
            assert rlc_fft["dataset_relpath"] == "datasets/rlc_fft.json"
            f_peak = float(rlc_fft["solver_meta"]["dominant_frequency_hz"])
            actual_rlc_dur = max(1.0, float(rlc_watch["actual_duration_s"]))
            freq_tol = (1.0 / actual_rlc_dur) + 0.1
            assert abs(f_peak - 2.0) <= freq_tol, (
                f"FFT peak {f_peak:.3f} Hz outside 2.0 ± {freq_tol:.3f} Hz"
            )

            calc_f_err = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "run_id": run_id,
                        "expression": "abs(f_meas - f_ref) / f_ref * 100",
                        "variables": {
                            "f_meas": {"value": f_peak, "unit": "Hz"},
                            "f_ref": {"value": 2.0, "unit": "Hz"},
                        },
                        "output_unit": "1",
                        "label": "rlc_freq_pct_dev",
                    },
                )
            )

            # Close simulator session cleanly
            closed = _parse_tool_json(
                await session.call_tool("sim_close", {"session_id": session_id})
            )
            assert closed["closed"] is True

            # ------------------------------------------------------------------
            # 5. Schematic, vector plots, academic report, previews & provenance
            # ------------------------------------------------------------------
            fig_circuit = _parse_tool_json(
                await session.call_tool(
                    "diagram_render",
                    {
                        "run_id": run_id,
                        "source": "model_computed",
                        "spec": {
                            "diagram_id": "fig_transient_circuit",
                            "kind": "circuit",
                            "title": "Transient RC / Damped RLC Test Circuit",
                            "elements": [
                                {"kind": "source_v", "label": "$V_0$", "direction": "up"},
                                {"kind": "resistor", "label": "$R$", "direction": "right"},
                                {"kind": "inductor", "label": "$L$", "direction": "right"},
                                {"kind": "capacitor", "label": "$C$", "direction": "down"},
                                {"kind": "line", "direction": "left"},
                                {"kind": "line", "direction": "left"},
                            ],
                        },
                    },
                )
            )

            fig_rc = _parse_tool_json(
                await session.call_tool(
                    "plot_render",
                    {
                        "run_id": run_id,
                        "source": "simulator_captured",
                        "spec": {
                            "plot_id": "fig_rc_transient",
                            "title": "RC Discharge: Live sim_watch Capture vs RK45 ODE Model",
                            "x_label": "Time t (s)",
                            "y_label": "Capacitor Voltage Vc (V)",
                            "series": [
                                {
                                    "label": "sim_watch live OCR (simulator_captured)",
                                    "x": [pt[0] for pt in valid_rc_pts],
                                    "y": [pt[2] for pt in valid_rc_pts],
                                    "kind": "scatter",
                                    "color": "#1f4e79",
                                },
                                {
                                    "label": "compute_sim RK45 ODE (model_computed)",
                                    "x": [float(v) for v in ode_t],
                                    "y": [float(v) for v in ode_vc],
                                    "kind": "line",
                                    "color": "#c0392b",
                                },
                            ],
                            "annotations": [
                                (
                                    f"OLS tau = {tau_meas:.3f} s (ref 1.000 s, "
                                    f"dev {tau_rel_err_pct:.2f}%, R^2 = {r_squared:.4f})"
                                )
                            ],
                        },
                    },
                )
            )

            fft_rows = json.loads(
                (run_root / "datasets" / "rlc_fft.json").read_text(encoding="utf-8")
            )["rows"]
            fft_plot_rows = [r for r in fft_rows if float(r["frequency_hz"]) <= 8.0]
            fig_fft = _parse_tool_json(
                await session.call_tool(
                    "plot_render",
                    {
                        "run_id": run_id,
                        "source": "model_computed",
                        "spec": {
                            "plot_id": "fig_rlc_fft",
                            "title": "Damped RLC One-Sided FFT Spectrum (Hann Window)",
                            "x_label": "Frequency f (Hz)",
                            "y_label": "Single-Sided Amplitude (V)",
                            "series": [
                                {
                                    "label": "compute_sim FFT spectrum (model_computed)",
                                    "x": [float(r["frequency_hz"]) for r in fft_plot_rows],
                                    "y": [float(r["amplitude"]) for r in fft_plot_rows],
                                    "kind": "line",
                                    "color": "#1f4e79",
                                }
                            ],
                            "annotations": [
                                (
                                    f"Dominant peak f_peak = {f_peak:.3f} Hz "
                                    f"(ref 2.000 Hz, N = {rlc_fft['solver_meta']['n_samples']})"
                                )
                            ],
                        },
                    },
                )
            )

            # Build summary comparison table
            sample_rc_rows = []
            step_stride = max(1, len(valid_rc_pts) // 6)
            for sim_t, el_s, meas_vc in valid_rc_pts[::step_stride][:6]:
                mod_vc = float(np.interp(sim_t, ode_t, ode_vc))
                sample_rc_rows.append(
                    [
                        f"{sim_t:.2f}",
                        f"{el_s:.3f}",
                        f"{meas_vc:.2f}",
                        f"{mod_vc:.2f}",
                        f"{meas_vc - mod_vc:+.2f}",
                    ]
                )

            report_out = _parse_tool_json(
                await session.call_tool(
                    "report_build",
                    {
                        "run_id": run_id,
                        "formats": ["pdf", "docx", "evidence_zip"],
                        "include_teacher_packet": True,
                        "render_previews": True,
                        "preview_dpi": 150,
                        "profile_paths": [saved_profile_relpath],
                        "spec": {
                            "title": "Transient Dynamics in RC Discharge & Damped RLC Circuits",
                            "subtitle": "Milestone 2 Live sim_watch & compute_sim Benchmark",
                            "language": "en",
                            "course": "General Physics — Electricity & Magnetism",
                            "author": "PEA Automated Verification",
                            "team": "Milestone 2 QA",
                            "date_str": "2026-11-24",
                            "objective": (
                                "Capture real-time RC discharge and damped RLC oscillations "
                                "via drift-compensated sim_watch OCR, recover the RC time "
                                "constant via linearized OLS, verify pointwise agreement with "
                                "an RK45 ODE IVP model, and recover the damped oscillation "
                                "frequency via a Hann-windowed one-sided FFT spectrum."
                            ),
                            "apparatus": [
                                (
                                    "Deterministic local HTML5 RC / Damped RLC transient "
                                    "simulator (640x420 loopback viewport, zero network calls)."
                                ),
                                (
                                    "SHA-256 CalibrationProfile with RapidOCR ONNX readout "
                                    "extraction and drift-compensated sim_watch pacing."
                                ),
                            ],
                            "theory_sections": [
                                {
                                    "heading": "RC Exponential Discharge",
                                    "body": (
                                        "For an RC circuit discharging from initial voltage "
                                        "$V_0$ through resistance $R$ and capacitance $C$, "
                                        "Kirchhoff's loop law gives "
                                        "$$\\frac{dV_C}{dt} = -\\frac{V_C}{RC}, \\qquad "
                                        "V_C(t) = V_0 e^{-t/\\tau}, \\quad \\tau = RC.$$ "
                                        "Taking the natural logarithm yields a linear relation "
                                        "$\\ln V_C(t) = \\ln V_0 - t/\\tau$ whose slope equals "
                                        "$-1/\\tau$."
                                    ),
                                },
                                {
                                    "heading": "Damped RLC Oscillation & Spectral Recovery",
                                    "body": (
                                        "For the damped RLC mode with natural frequency "
                                        "$f_0 = 2.0\\,\\text{Hz}$ and damping rate "
                                        "$\\beta = 0.25\\,\\text{s}^{-1}$, the voltage evolves "
                                        "as $$V_C(t) = V_0 e^{-\\beta t}\\cos(2\\pi f_0 t),$$ "
                                        "whose dominant spectral component is recovered by a "
                                        "one-sided Hann-windowed FFT after uniform-grid "
                                        "resampling."
                                    ),
                                },
                            ],
                            "procedure": [
                                (
                                    "Open the local HTML5 fixture via sim_open, verify SHA-256 "
                                    "fingerprint, and save/auto-reload CalibrationProfile."
                                ),
                                (
                                    "Run sim_watch in inline_ocr mode (interval 120 ms, "
                                    "threshold stop Vc <= 1.5 V) for RC discharge "
                                    "(R = 10 kOhm, C = 100 uF, V0 = 10 V)."
                                ),
                                (
                                    "Run data_analyze (ln Vc vs elapsed_s OLS) and "
                                    "compute_sim (ode_ivp RK45) tagged model_computed."
                                ),
                                (
                                    "Switch to RLC mode, capture a 3.0 s burst_post_ocr "
                                    "transient (interval 40 ms), and run compute_sim "
                                    "(fft_spectrum, Hann window)."
                                ),
                            ],
                            "tables": [
                                {
                                    "table_id": "table_rc_compare",
                                    "caption": (
                                        "Sampled RC Discharge Points: Live sim_watch OCR "
                                        "(simulator_captured) vs RK45 ODE (model_computed)"
                                    ),
                                    "headers": [
                                        "Stopwatch t (s)",
                                        "Elapsed s",
                                        "Measured Vc (V)",
                                        "ODE Model Vc (V)",
                                        "Residual (V)",
                                    ],
                                    "rows": sample_rc_rows,
                                    "source": "simulator_captured",
                                },
                                {
                                    "table_id": "table_m2_summary",
                                    "caption": "Milestone 2 Recovered Physical Parameters",
                                    "headers": [
                                        "Parameter",
                                        "Measured / Recovered",
                                        "Reference",
                                        "Metric / Notes",
                                    ],
                                    "rows": [
                                        [
                                            "RC time constant tau",
                                            f"{tau_meas:.3f} s",
                                            f"{tau_ref:.3f} s",
                                            f"dev {tau_rel_err_pct:.2f}%, R^2 = {r_squared:.4f}",
                                        ],
                                        [
                                            "Max |Vc_meas - Vc_ode|",
                                            f"{max_ode_diff_v:.3f} V",
                                            "0.000 V",
                                            f"{len(valid_rc_pts)} live frames vs RK45",
                                        ],
                                        [
                                            "RLC FFT peak f0",
                                            f"{f_peak:.3f} Hz",
                                            "2.000 Hz",
                                            f"dev {float(calc_f_err['output_value']):.2f}%, "
                                            f"resampled={rlc_fft['solver_meta']['resampled']}",
                                        ],
                                    ],
                                    "source": "model_computed",
                                },
                            ],
                            "figures": [
                                {
                                    "figure_id": fig_circuit["figure_id"],
                                    "caption": "Transient RC / Damped RLC test circuit schematic.",
                                    "svg_relpath": fig_circuit["svg_relpath"],
                                    "png_relpath": fig_circuit["png_relpath"],
                                    "source": "model_computed",
                                },
                                {
                                    "figure_id": fig_rc["figure_id"],
                                    "caption": (
                                        "RC discharge overlay comparing simulator_captured "
                                        "sim_watch OCR points with the model_computed RK45 curve."
                                    ),
                                    "svg_relpath": fig_rc["svg_relpath"],
                                    "png_relpath": fig_rc["png_relpath"],
                                    "source": "simulator_captured",
                                },
                                {
                                    "figure_id": fig_fft["figure_id"],
                                    "caption": (
                                        "One-sided Hann-windowed FFT amplitude spectrum of the "
                                        "live burst_post_ocr damped RLC capture."
                                    ),
                                    "svg_relpath": fig_fft["svg_relpath"],
                                    "png_relpath": fig_fft["png_relpath"],
                                    "source": "model_computed",
                                },
                            ],
                            "calc_ids": [
                                calc_tau_ref["calc_id"],
                                calc_tau_meas["calc_id"],
                                calc_f_err["calc_id"],
                            ],
                            "analysis_paragraphs": [
                                (
                                    f"Linearized OLS regression of $\\ln V_C$ vs monotonic "
                                    f"elapsed time gave $\\tau = {tau_meas:.3f}\\,\\text{{s}}$ "
                                    f"($R^2 = {r_squared:.4f}$), agreeing with the theoretical "
                                    f"time constant $\\tau_{{ref}} = RC = 1.000\\,\\text{{s}}$ "
                                    f"within ${tau_rel_err_pct:.2f}\\%$. Pointwise comparison "
                                    f"against the RK45 ODE solution stayed within "
                                    f"${max_ode_diff_v:.3f}\\,\\text{{V}}$ across all live frames."
                                ),
                                (
                                    f"During the 3.0 s damped RLC burst capture "
                                    f"({rlc_watch['captured_frames']} frames), signed positive and "
                                    f"negative voltage swings were extracted via RapidOCR and "
                                    f"analyzed by compute_sim's one-sided Hann-windowed FFT, "
                                    f"recovering $f_{{peak}} = {f_peak:.3f}\\,\\text{{Hz}}$ "
                                    f"against the reference $f_0 = 2.000\\,\\text{{Hz}}$."
                                ),
                            ],
                            "conclusions": [
                                (
                                    "HTML5 directory SHA-256 fingerprinting enabled automatic "
                                    "calibration profile reload on session reopen."
                                ),
                                (
                                    f"RC discharge inline_ocr threshold watch recovered "
                                    f"tau = {tau_meas:.3f} s (< 5% error) and matched RK45 ODE."
                                ),
                                (
                                    f"Damped RLC burst_post_ocr capture + dataset-backed Hann "
                                    f"FFT recovered f0 = {f_peak:.3f} Hz within bin resolution."
                                ),
                                (
                                    "All simulator_captured and model_computed datasets, "
                                    "figures, and reports passed SHA-256 provenance audit."
                                ),
                            ],
                        },
                    },
                )
            )
            assert report_out["pdf_relpath"] == "reports/report.pdf"
            assert report_out["docx_relpath"] == "reports/report.docx"
            assert report_out["evidence_zip_relpath"] == "reports/evidence.zip"
            assert report_out["teacher_packet_relpath"] == "reports/teacher-evidence.pdf"
            assert report_out["page_count"] >= 2

            preview_out = _parse_tool_json(
                await session.call_tool(
                    "report_preview",
                    {
                        "run_id": run_id,
                        "pdf_relpath": "reports/report.pdf",
                        "dpi": 150,
                    },
                )
            )
            assert preview_out["page_count"] == report_out["page_count"]
            for rel_png in preview_out["preview_png_relpaths"]:
                assert (run_root / rel_png).is_file()

            # Verify DOCX and evidence.zip contents
            doc = docx.Document(str(run_root / "reports" / "report.docx"))
            assert len(doc.paragraphs) >= 5
            assert len(doc.tables) >= 2

            with zipfile.ZipFile(run_root / "reports" / "evidence.zip", "r") as zf:
                znames = set(zf.namelist())
                assert "datasets/rc_watch.json" in znames
                assert "datasets/rc_ode.json" in znames
                assert "datasets/rlc_watch.json" in znames
                assert "datasets/rlc_fft.json" in znames
                assert "provenance.jsonl" in znames
                assert any(n.startswith("evidence/watch-") and n.endswith(".png") for n in znames)
                assert any(n.startswith("readings/watch-") and n.endswith(".json") for n in znames)

            assert (run_root / "reports" / "teacher-evidence.pdf").is_file()

            # Full SHA-256 provenance audit
            verify_out = _parse_tool_json(
                await session.call_tool("lab_runs", {"action": "verify", "run_id": run_id})
            )
            assert verify_out["verification"]["ok"] is True
            assert verify_out["verification"]["checked_entries"] >= 40
            assert not math.isnan(tau_meas)
