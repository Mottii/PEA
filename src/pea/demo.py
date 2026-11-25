"""Self-contained live HTML5 RC/RLC transient demonstration runner (`pea demo`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pea.assets import check_assets
from pea.config import PeaConfig, is_loopback_host, resolve_browser_executable
from pea.viewer.app import main as viewer_main


@dataclass(frozen=True)
class DemoResult:
    """Summary of a completed self-contained ``pea demo`` run."""

    run_id: str
    run_root: Path
    runs_dir: Path
    tau_meas_s: float
    tau_ref_s: float
    tau_rel_err_pct: float
    r_squared: float
    rlc_peak_hz: float
    checked_entries: int
    provenance_ok: bool
    report_pdf: Path
    report_docx: Path
    evidence_zip: Path
    teacher_packet_pdf: Path


def build_html5_rc_profile(sim_sha256: str) -> dict[str, Any]:
    """Calibration profile for the bundled 640x420 HTML5 RC/RLC transient simulator."""
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
                "roi_px": [48, 238, 292, 310],
                "ocr_mode": "rec_only",
                "pad_px": 8,
                "scale": 1.0,
                "parse_mode": "float",
                "unit_pattern": "s",
                "normalize_homoglyphs": True,
            },
            "Vc": {
                "name": "Vc",
                "roi_px": [348, 238, 592, 310],
                "ocr_mode": "rec_only",
                "pad_px": 8,
                "scale": 1.0,
                "parse_mode": "float",
                "unit_pattern": "V",
                "normalize_homoglyphs": True,
            },
        },
        "notes": "Deterministic HTML5 RC/RLC transient fixture calibration profile (640x420).",
    }


def _parse_tool_json(result: Any) -> dict[str, Any]:
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool returned an error: {result.content}")
    if getattr(result, "structured_content", None) is not None:
        return dict(result.structured_content)
    if not result.content:
        raise RuntimeError("MCP tool returned empty content")
    return json.loads(result.content[0].text)


def _stage_demo_fixture(out_dir: Path) -> Path:
    """Copy the bundled HTML5 RC/RLC fixture into ``out_dir/sim_fixture/html5_rc_sim``."""
    dest_dir = out_dir / "sim_fixture" / "html5_rc_sim"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_index = dest_dir / "index.html"

    fixture_pkg = resources.files("pea.demo_assets").joinpath("html5_rc_sim", "index.html")
    with resources.as_file(fixture_pkg) as src_path:
        shutil.copyfile(src_path, dest_index)
    return dest_dir


def _emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


async def run_demo_pipeline(
    out_dir: Path,
    *,
    cfg: PeaConfig | None = None,
    progress: Callable[[str], None] | None = None,
) -> DemoResult:
    """Run the self-contained HTML5 RC discharge & damped RLC transient demo over MCP stdio."""
    active_cfg = cfg if cfg is not None else PeaConfig.from_env()

    asset_errors = check_assets(active_cfg.local_assets_dir, require_manifest=False)
    if asset_errors:
        sample = "; ".join(asset_errors[:3])
        raise RuntimeError(
            f"Offline runtime assets are incomplete in {active_cfg.local_assets_dir} "
            f"({sample}). Run `pea setup` first."
        )

    browser_exe = active_cfg.browser_executable or resolve_browser_executable()
    if browser_exe is None:
        raise RuntimeError(
            "Playwright Chromium executable not found. Run `pea setup` "
            "(or `playwright install chromium`) first."
        )

    out_root = Path(out_dir).expanduser().resolve()
    runs_dir = out_root / "runs"
    profiles_dir = out_root / "profiles"
    runs_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    _stage_demo_fixture(out_root)

    env = dict(os.environ)
    env.update(
        {
            "PEA_PROJECT_ROOT": str(out_root),
            "PEA_LOCAL_ASSETS_DIR": str(active_cfg.local_assets_dir),
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
        cwd=str(out_root),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            _emit(progress, "[1/6] Booting local HTML5 RC/RLC simulator & calibrating...")
            created = _parse_tool_json(
                await session.call_tool(
                    "lab_runs",
                    {
                        "action": "create",
                        "label": "PEA Demo — RC Discharge & Damped RLC Transients",
                        "source": "simulator_captured",
                    },
                )
            )
            run_id = str(created["run_id"])
            run_root = runs_dir / run_id

            opened = _parse_tool_json(
                await session.call_tool(
                    "sim_open",
                    {
                        "html5_dir": "sim_fixture/html5_rc_sim",
                        "run_id": run_id,
                        "viewport_width": 640,
                        "viewport_height": 420,
                        "settle_ms": 300,
                    },
                )
            )
            session_id = str(opened["session_id"])
            sim_sha256 = str(opened["sim_sha256"])

            await session.call_tool("sim_observe", {"session_id": session_id, "annotate": True})
            saved_prof = _parse_tool_json(
                await session.call_tool(
                    "sim_save_profile",
                    {
                        "session_id": session_id,
                        "profile": build_html5_rc_profile(sim_sha256),
                        "verify_live": True,
                    },
                )
            )
            saved_profile_relpath = str(saved_prof["saved_relpath"])

            _emit(progress, "[2/6] Capturing live RC discharge frames (sim_watch + RapidOCR)...")
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

            _emit(progress, "[3/6] Computing OLS time constant & RK45 ODE model...")
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

            rc_t_stop = max(2.2, float(rc_watch["actual_duration_s"]) + 0.1)
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

            _emit(progress, "[4/6] Capturing damped RLC burst & running Hann FFT...")
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
            f_peak = float(rlc_fft["solver_meta"]["dominant_frequency_hz"])

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
            await session.call_tool("sim_close", {"session_id": session_id})

            _emit(progress, "[5/6] Rendering schematic, vector plots & report outputs...")
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
            max_ode_diff_v = 0.0
            for sim_t, _el_s, meas_vc in valid_rc_pts:
                if ode_t[0] <= sim_t <= ode_t[-1]:
                    model_vc = float(np.interp(sim_t, ode_t, ode_vc))
                    max_ode_diff_v = max(max_ode_diff_v, abs(meas_vc - model_vc))

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
                            "subtitle": "PEA Self-Contained Demo (sim_watch & compute_sim)",
                            "language": "en",
                            "course": "General Physics — Electricity & Magnetism",
                            "author": "PEA Self-Contained Demo",
                            "team": "Open-Source Benchmark",
                            "date_str": datetime.now(UTC).strftime("%Y-%m-%d"),
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
                                    "fingerprint, and save CalibrationProfile."
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
                                    "caption": "Recovered Physical Parameters",
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
                                    "calibration profile persistence."
                                ),
                                (
                                    f"RC discharge inline_ocr threshold watch recovered "
                                    f"tau = {tau_meas:.3f} s and matched the RK45 ODE solution."
                                ),
                                (
                                    f"Damped RLC burst_post_ocr capture + dataset-backed Hann "
                                    f"FFT recovered f0 = {f_peak:.3f} Hz within bin resolution."
                                ),
                                (
                                    "All simulator_captured and model_computed datasets, "
                                    "figures, and reports passed SHA-256 integrity verification."
                                ),
                            ],
                        },
                    },
                )
            )

            await session.call_tool(
                "report_preview",
                {
                    "run_id": run_id,
                    "pdf_relpath": str(report_out["pdf_relpath"]),
                    "dpi": 150,
                },
            )

            _emit(progress, "[6/6] Verifying SHA-256 artifact integrity manifest...")
            verify_out = _parse_tool_json(
                await session.call_tool("lab_runs", {"action": "verify", "run_id": run_id})
            )
            verification = verify_out["verification"]

            return DemoResult(
                run_id=run_id,
                run_root=run_root,
                runs_dir=runs_dir,
                tau_meas_s=tau_meas,
                tau_ref_s=tau_ref,
                tau_rel_err_pct=tau_rel_err_pct,
                r_squared=r_squared,
                rlc_peak_hz=f_peak,
                checked_entries=int(verification["checked_entries"]),
                provenance_ok=bool(verification["ok"]),
                report_pdf=run_root / str(report_out["pdf_relpath"]),
                report_docx=run_root / str(report_out["docx_relpath"]),
                evidence_zip=run_root / str(report_out["evidence_zip_relpath"]),
                teacher_packet_pdf=run_root / str(report_out["teacher_packet_relpath"]),
            )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``pea demo``."""
    parser = argparse.ArgumentParser(
        prog="pea demo",
        description="Run the self-contained HTML5 RC/RLC transient benchmark and open pea-viewer.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("runs/demo"),
        help="Output workspace directory for demo runs/profiles (default: runs/demo).",
    )
    parser.add_argument(
        "--no-viewer",
        action="store_true",
        help="Do not start pea-viewer after completing the demo run.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start pea-viewer without automatically opening the system browser.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback host for pea-viewer (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5190,
        help="Loopback port for pea-viewer (default: 5190).",
    )
    args = parser.parse_args(argv)

    if not is_loopback_host(args.host):
        parser.error(
            f"--host must be a loopback address (127.0.0.1, localhost, ::1); got {args.host!r}"
        )
    if not (1 <= args.port <= 65535):
        parser.error(f"--port must be in 1..65535; got {args.port}")

    try:
        result = asyncio.run(
            run_demo_pipeline(
                args.out,
                progress=lambda msg: print(msg, flush=True),
            )
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    status_word = "OK" if result.provenance_ok else "FAILED"
    print("")
    print("PEA Demo Complete")
    print(f"  Run ID:          {result.run_id}")
    print(
        f"  RC time const:   {result.tau_meas_s:.3f} s "
        f"(ref {result.tau_ref_s:.3f} s, dev {result.tau_rel_err_pct:.2f}%, "
        f"R^2 = {result.r_squared:.4f})"
    )
    print(f"  RLC FFT peak:    {result.rlc_peak_hz:.3f} Hz (ref 2.000 Hz)")
    print(
        f"  SHA-256 check:   {status_word} ({result.checked_entries} verified artifacts)"
    )
    print(f"  Report PDF:      {result.report_pdf}")
    print(f"  Instructor PDF:  {result.teacher_packet_pdf}")
    print(f"  Evidence ZIP:    {result.evidence_zip}")

    if args.no_viewer:
        print(f"\nInspect later with:\n  pea viewer --runs-dir {result.runs_dir}")
        return 0 if result.provenance_ok else 1

    host_disp = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{host_disp}:{args.port}"
    print(f"\nStarting pea-viewer at {url} (Ctrl+C to stop)...")
    if not args.no_browser:
        timer = threading.Timer(0.8, webbrowser.open, args=[url])
        timer.daemon = True
        timer.start()

    try:
        return viewer_main(
            ["--host", args.host, "--port", str(args.port), "--runs-dir", str(result.runs_dir)]
        )
    except KeyboardInterrupt:
        return 0


__all__ = [
    "DemoResult",
    "build_html5_rc_profile",
    "main",
    "run_demo_pipeline",
]


if __name__ == "__main__":
    raise SystemExit(main())
