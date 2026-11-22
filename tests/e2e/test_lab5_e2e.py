"""Live E2E benchmark for Lab 5 (Forced Oscillations in an RLC Circuit — ``m96220.swf``).

Drives the exact zero-hardcoded ``pea-mcp`` server over real ``mcp.client.stdio.stdio_client``:
1. PDF handout intake on ``local_assets/benchmarks/lab5-rlc-handout.pdf`` (8 pages).
2. Live Ruffle 0.7.1 open, annotated calibration observation, and SHA-256 profile save.
3. Coarse-to-fine ``plateau_max`` search from ``w0 = 2500 rad/s`` across all four Team 2
   resistors (``L = 5.0 mH, C = 32 uF, R in {3.2, 4.0, 5.0, 6.0} Ohm``), asserting exact
   discrete resonance plateaus (including the 74-step plateau at ``R = 5.0 Ohm``).
4. Dimensional calculations (``calc_eval``) for Lab 5 physics + worked Problems 3, 4A–C, and 5.
5. Tabular analysis (``data_ingest`` + ``data_analyze``) for Table 2, ``w0/dw`` vs ``Q^2`` OLS,
   and theoretical ``4 Q^2`` comparison.
6. Vector visuals (``diagram_render`` + ``plot_render``): redrawn Fig. 1 RLC circuit using
   handout notation (``ε_0, U_0C, U_0L, U_0R``), ``w0/dw`` vs ``Q^2`` plot, live resonance
   traces, series RLC phasor diagram, and Problem 4C ``q–I`` phase-portrait ellipse
   (``equal_aspect=True``).
7. Submission-ready report compilation (``report_build`` + ``report_preview``) into A4 PDF,
   editable ``.docx``, raw ``evidence.zip``, and Teacher Evidence Packet PDF, including
   opt-in Self-Control Q&As (#1, #3, #36) and worked Problems (3, 4A–C, 5), plus full
   SHA-256 provenance verification (``lab_runs(action="verify")``).

Set ``PEA_E2E_KEEP_DIR`` (e.g. ``runs/phase3-qa``) to preserve run artifacts for visual QA
and ``pea-viewer`` inspection.
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
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pea.config import resolve_browser_executable

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ASSETS_DIR = REPO_ROOT / "local_assets"
SWF_PATH = LOCAL_ASSETS_DIR / "sims" / "m96220.swf"
HANDOUT_PDF = LOCAL_ASSETS_DIR / "benchmarks" / "lab5-rlc-handout.pdf"
EXPECTED_SWF_SHA256 = "35e4a257134073356c9796965ea3ea2d9362d51b2dc87486dc93e85a0fb96b15"

# Empirically verified Phase 0 exact discrete resonance plateaus for Team 2 (L=5.0 mH, C=32 uF)
EXPECTED_PLATEAUS: dict[float, tuple[float, float, float, int]] = {
    # R_ohm: (low_bound, high_bound, peak_Q, width_steps)
    3.2: (2445.0, 2472.0, 3.94, 28),
    4.0: (2427.0, 2444.0, 3.17, 18),
    5.0: (2361.0, 2434.0, 2.55, 74),
    6.0: (2333.0, 2370.0, 2.15, 38),
}


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


def _build_m96220_profile(swf_sha256: str) -> dict[str, Any]:
    """Calibration profile for ``m96220.swf`` (800x600 stage, 39 px top bar)."""
    return {
        "schema_version": 1,
        "swf_sha256": swf_sha256,
        "stage_width": 800,
        "stage_height": 600,
        "header_height_px": 39,
        "blur_xy": [450, 18],
        "controls": {
            "R": {
                "name": "R",
                "kind": "stepper",
                "input_xy": [644, 289],
                "up_xy": [678, 282],
                "down_xy": [678, 296],
                "readout_roi_px": [620, 275, 667, 302],
                "unit": "Ом",
                "step": 0.1,
                "min_val": 3.0,
                "max_val": 10.0,
                "decimal_comma": False,
                "commit_mode": "enter",
            },
            "L": {
                "name": "L",
                "kind": "stepper",
                "input_xy": [642, 322],
                "up_xy": [676, 315],
                "down_xy": [676, 329],
                "readout_roi_px": [618, 308, 665, 335],
                "unit": "мГн",
                "step": 0.1,
                "min_val": 1.0,
                "max_val": 5.0,
                "decimal_comma": False,
                "commit_mode": "enter",
            },
            "C": {
                "name": "C",
                "kind": "stepper",
                "input_xy": [644, 355],
                "up_xy": [678, 348],
                "down_xy": [678, 362],
                "readout_roi_px": [620, 341, 667, 368],
                "unit": "мкФ",
                "step": 1.0,
                "min_val": 30.0,
                "max_val": 150.0,
                "decimal_comma": False,
                "commit_mode": "enter",
            },
            "omega": {
                "name": "omega",
                "kind": "stepper",
                "input_xy": [644, 388],
                "up_xy": [678, 381],
                "down_xy": [678, 395],
                "readout_roi_px": [620, 374, 667, 401],
                "unit": "с^-1",
                "step": 1.0,
                "min_val": 1000.0,
                "max_val": 5000.0,
                "decimal_comma": False,
                "commit_mode": "enter",
            },
        },
        "readouts": {
            "w0": {
                "name": "w0",
                "roi_px": [625, 436, 710, 468],
                "ocr_mode": "rec_only",
                "pad_px": 0,
                "scale": 1.0,
                "parse_mode": "int",
                "normalize_homoglyphs": True,
            },
            "uc_over_u": {
                "name": "uc_over_u",
                "roi_px": [625, 474, 710, 502],
                "ocr_mode": "rec_only",
                "pad_px": 0,
                "scale": 1.0,
                "parse_mode": "float",
                "normalize_homoglyphs": True,
            },
            "ul_over_u": {
                "name": "ul_over_u",
                "roi_px": [625, 515, 710, 542],
                "ocr_mode": "rec_only",
                "pad_px": 0,
                "scale": 1.0,
                "parse_mode": "float",
                "normalize_homoglyphs": True,
            },
            "ur_over_u": {
                "name": "ur_over_u",
                "roi_px": [625, 555, 710, 583],
                "ocr_mode": "rec_only",
                "pad_px": 0,
                "scale": 1.0,
                "parse_mode": "float",
                "normalize_homoglyphs": True,
            },
            "xl": {
                "name": "xl",
                "roi_px": [661, 592, 750, 618],
                "ocr_mode": "rec_only",
                "pad_px": 0,
                "scale": 1.0,
                "parse_mode": "float",
                "normalize_homoglyphs": True,
            },
        },
        "notes": "m96220.swf (Forced Oscillations in an RLC Circuit) live calibration profile",
    }


@pytest.mark.live_sim
@pytest.mark.asyncio
async def test_lab5_rlc_live_e2e_over_mcp_stdio(tmp_path: Path) -> None:
    if not SWF_PATH.is_file() or not HANDOUT_PDF.is_file():
        pytest.skip("Lab 5 local benchmark assets not staged under local_assets/")
    browser_exe = resolve_browser_executable()
    if browser_exe is None:
        pytest.skip("Playwright Chromium executable not cached locally")

    runs_dir, profiles_dir = _resolve_e2e_dirs(tmp_path, "lab5")
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
            # 1. Create run & inspect Lab 5 handout PDF
            # ------------------------------------------------------------------
            created = _parse_tool_json(
                await session.call_tool(
                    "lab_runs",
                    {
                        "action": "create",
                        "label": "Lab 5 — Forced Oscillations in an RLC Circuit (Team 2)",
                        "source": "simulator_captured",
                    },
                )
            )
            run_id = created["run_id"]
            run_root = runs_dir / run_id

            pdf_info = _parse_tool_json(
                await session.call_tool(
                    "lab_inspect_pdf",
                    {
                        "pdf_path": "local_assets/benchmarks/lab5-rlc-handout.pdf",
                        "run_id": run_id,
                        "dpi": 144,
                        "source": "bench_manual",
                    },
                )
            )
            assert pdf_info["total_pages"] == 8
            assert pdf_info["rendered_pages"] == 8
            assert len(pdf_info["pages"]) == 8
            full_handout_text = pdf_info["full_text"]
            assert "Table 1" in full_handout_text
            assert "Table 2" in full_handout_text
            assert "3.2" in full_handout_text and "6.0" in full_handout_text

            # ------------------------------------------------------------------
            # 2. Compute natural frequency w0 via calc_eval
            # ------------------------------------------------------------------
            calc_ids: list[str] = []

            w0_calc = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "1 / sqrt(L * C)",
                        "variables": {
                            "L": {"value": 5.0, "unit": "mH"},
                            "C": {"value": 32.0, "unit": "uF"},
                        },
                        "output_unit": "rad/s",
                        "label": "Team 2 natural frequency w0 = 1/sqrt(L*C)",
                        "run_id": run_id,
                    },
                )
            )
            w0_val = float(w0_calc["output_value"])
            assert w0_val == pytest.approx(2500.0, rel=1e-9)
            calc_ids.append(w0_calc["calc_id"])

            # ------------------------------------------------------------------
            # 3. Live Ruffle open, observe(annotate=True), save SHA-256 profile
            # ------------------------------------------------------------------
            opened = _parse_tool_json(
                await session.call_tool(
                    "sim_open",
                    {
                        "swf_path": "local_assets/sims/m96220.swf",
                        "run_id": run_id,
                        "settle_ms": 1500,
                    },
                )
            )
            session_id = opened["session_id"]
            assert opened["stage_width"] == 800
            assert opened["stage_height"] == 600
            assert opened["swf_meta"]["sha256"] == EXPECTED_SWF_SHA256
            assert opened["page_errors"] == []
            assert opened["aborted_requests"] == []

            obs0 = _parse_tool_json(
                await session.call_tool(
                    "sim_observe",
                    {"session_id": session_id, "annotate": True, "blur_first": True},
                )
            )
            assert obs0["annotated_relpath"] is not None
            assert (run_root / obs0["annotated_relpath"]).is_file()
            assert (run_root / obs0["ocr_log_relpath"]).is_file()

            profile = _build_m96220_profile(EXPECTED_SWF_SHA256)
            saved_prof = _parse_tool_json(
                await session.call_tool(
                    "sim_save_profile",
                    {
                        "session_id": session_id,
                        "profile": profile,
                        "verify_live": True,
                    },
                )
            )
            assert saved_prof["verified_live"] is True
            assert saved_prof["swf_sha256"] == EXPECTED_SWF_SHA256

            # Verify w0 readout on simulator at Team 2 baseline (L=5.0 mH, C=32 uF, R=3.2, w=2500)
            _parse_tool_json(
                await session.call_tool(
                    "sim_act",
                    {
                        "session_id": session_id,
                        "steps": [
                            {"action": "set_stepper", "control": "L", "value": 5.0},
                            {"action": "set_stepper", "control": "C", "value": 32},
                            {"action": "set_stepper", "control": "R", "value": 3.2},
                            {"action": "set_stepper", "control": "omega", "value": 2500},
                        ],
                        "settle_ms": 200,
                    },
                )
            )
            obs_base = _parse_tool_json(
                await session.call_tool(
                    "sim_observe",
                    {
                        "session_id": session_id,
                        "annotate": True,
                        "readouts": ["w0", "uc_over_u", "ul_over_u", "ur_over_u", "xl"],
                        "blur_first": True,
                    },
                )
            )
            assert obs_base["readouts"]["w0"]["value"] == 2500
            assert obs_base["readouts"]["uc_over_u"]["value"] == pytest.approx(3.91, abs=0.01)
            assert obs_base["readouts"]["ur_over_u"]["value"] == pytest.approx(1.00, abs=0.01)

            # ------------------------------------------------------------------
            # 4. Coarse-to-fine plateau_max search from w0=2500 across all 4 R
            # ------------------------------------------------------------------
            resistors = [3.2, 4.0, 5.0, 6.0]
            measured_rows: list[dict[str, Any]] = []
            trace_series_by_r: dict[float, list[tuple[float, float]]] = {}
            ideal_by_r: dict[float, dict[str, float]] = {}

            try:
                for r_ohm in resistors:
                    # Coarse sweep down from w0 = 2500 in 10 rad/s steps to bracket the peak
                    coarse = _parse_tool_json(
                        await session.call_tool(
                            "sim_search_extremum",
                            {
                                "session_id": session_id,
                                "spec": {
                                    "mode": "plateau_max",
                                    "fixed_controls": {"L": 5.0, "C": 32, "R": r_ohm},
                                    "sweep_control": "omega",
                                    "readout": "uc_over_u",
                                    "start": w0_val,
                                    "step": -10.0,
                                    "max_steps": 35,
                                    "stop_after_drop": 3,
                                    "settle_ms": 110,
                                    "verify_controls": True,
                                    "blur_before_capture": True,
                                },
                            },
                        )
                    )
                    fine_start = min(w0_val, float(coarse["high_bound"]) + 12.0)

                    # Fine 1 rad/s plateau search covering up to 110 steps (handles 74-step R=5.0)
                    fine = _parse_tool_json(
                        await session.call_tool(
                            "sim_search_extremum",
                            {
                                "session_id": session_id,
                                "spec": {
                                    "mode": "plateau_max",
                                    "fixed_controls": {"L": 5.0, "C": 32, "R": r_ohm},
                                    "sweep_control": "omega",
                                    "readout": "uc_over_u",
                                    "start": fine_start,
                                    "step": -1.0,
                                    "max_steps": 110,
                                    "stop_after_drop": 2,
                                    "settle_ms": 95,
                                    "verify_controls": True,
                                    "blur_before_capture": True,
                                },
                            },
                        )
                    )

                    exp_lo, exp_hi, exp_peak, exp_width = EXPECTED_PLATEAUS[r_ohm]
                    assert fine["low_bound"] == pytest.approx(exp_lo, abs=1e-6), (
                        f"R={r_ohm}: low_bound {fine['low_bound']} != {exp_lo}"
                    )
                    assert fine["high_bound"] == pytest.approx(exp_hi, abs=1e-6), (
                        f"R={r_ohm}: high_bound {fine['high_bound']} != {exp_hi}"
                    )
                    assert fine["peak_value"] == pytest.approx(exp_peak, abs=1e-6), (
                        f"R={r_ohm}: peak {fine['peak_value']} != {exp_peak}"
                    )
                    assert fine["width_steps"] == exp_width, (
                        f"R={r_ohm}: width_steps {fine['width_steps']} != {exp_width}"
                    )
                    assert all(pt["controls_matched"] is True for pt in fine["trace"])

                    # Merge coarse + fine trace points for resonance curve plot
                    combined: dict[float, float] = {}
                    for pt in coarse["trace"] + fine["trace"]:
                        w_pt = float(pt["requested_settings"]["omega"])
                        val_pt = pt["parsed_value"]
                        if isinstance(val_pt, (int, float)):
                            combined[w_pt] = float(val_pt)
                    trace_series_by_r[r_ohm] = sorted(combined.items())

                    # Ideal theoretical Q and w_res via calc_eval (for comparison only)
                    q_id = _parse_tool_json(
                        await session.call_tool(
                            "calc_eval",
                            {
                                "expression": "(1 / R) * sqrt(L / C)",
                                "variables": {
                                    "R": {"value": r_ohm, "unit": "Ohm"},
                                    "L": {"value": 5.0, "unit": "mH"},
                                    "C": {"value": 32.0, "unit": "uF"},
                                },
                                "label": f"Ideal Q for R={r_ohm} Ohm",
                                "run_id": run_id,
                            },
                        )
                    )
                    wres_id = _parse_tool_json(
                        await session.call_tool(
                            "calc_eval",
                            {
                                "expression": "w0 * sqrt(1 - 1 / (2 * Q**2))",
                                "variables": {
                                    "w0": {"value": w0_val, "unit": "rad/s"},
                                    "Q": {"value": float(q_id["output_value"]), "unit": "1"},
                                },
                                "output_unit": "rad/s",
                                "label": f"Ideal w_res for R={r_ohm} Ohm",
                                "run_id": run_id,
                            },
                        )
                    )
                    calc_ids.extend([q_id["calc_id"], wres_id["calc_id"]])
                    ideal_by_r[r_ohm] = {
                        "Q_ideal": float(q_id["output_value"]),
                        "w_res_ideal": float(wres_id["output_value"]),
                    }

                    measured_rows.append(
                        {
                            "R_ohm": r_ohm,
                            "w0": w0_val,
                            "w1": float(fine["low_bound"]),
                            "w2": float(fine["high_bound"]),
                            "Q_meas": float(fine["peak_value"]),
                            "plateau_steps": int(fine["width_steps"]),
                        }
                    )
            finally:
                closed = _parse_tool_json(
                    await session.call_tool("sim_close", {"session_id": session_id})
                )
                assert closed["closed"] is True

            # ------------------------------------------------------------------
            # 5. Table 2 dataset ingest & data_analyze (w0/dw vs Q^2 & 4*Q^2)
            # ------------------------------------------------------------------
            _parse_tool_json(
                await session.call_tool(
                    "data_ingest",
                    {
                        "run_id": run_id,
                        "source": "simulator_captured",
                        "dataset_id": "lab5_table2_raw",
                        "rows": measured_rows,
                        "units": {
                            "R_ohm": "Ohm",
                            "w0": "rad/s",
                            "w1": "rad/s",
                            "w2": "rad/s",
                            "Q_meas": "1",
                        },
                        "notes": "Team 2 live m96220.swf plateau_max measurements",
                    },
                )
            )
            analysis = _parse_tool_json(
                await session.call_tool(
                    "data_analyze",
                    {
                        "run_id": run_id,
                        "dataset_id": "lab5_table2_raw",
                        "derived_columns": [
                            {"name": "w_res", "formula": "(w1 + w2) / 2", "unit": "rad/s"},
                            {"name": "dw", "formula": "w0 - w_res", "unit": "rad/s"},
                            {"name": "w0_over_dw", "formula": "w0 / dw", "unit": "1"},
                            {"name": "Q2", "formula": "Q_meas ** 2", "unit": "1"},
                            {"name": "four_Q2", "formula": "4 * Q2", "unit": "1"},
                        ],
                        "regressions": [
                            {
                                "name": "w0_dw_vs_Q2_zero",
                                "x_column": "Q2",
                                "y_column": "w0_over_dw",
                                "intercept": "zero",
                            },
                            {
                                "name": "w0_dw_vs_Q2_free",
                                "x_column": "Q2",
                                "y_column": "w0_over_dw",
                                "intercept": "free",
                            },
                        ],
                        "persist_derived_id": "lab5_table2_derived",
                    },
                )
            )
            reg_zero = next(
                r for r in analysis["regressions"] if r["name"] == "w0_dw_vs_Q2_zero"
            )
            reg_free = next(
                r for r in analysis["regressions"] if r["name"] == "w0_dw_vs_Q2_free"
            )
            assert reg_zero["slope"] == pytest.approx(3.86, rel=0.05)
            assert reg_zero["r_squared"] > 0.995
            assert reg_free["r_squared"] > 0.995

            slope_cmp = _parse_tool_json(
                await session.call_tool(
                    "data_analyze",
                    {
                        "run_id": run_id,
                        "dataset_id": "lab5_table2_derived",
                        "comparisons": [
                            {
                                "name": "slope_vs_4",
                                "measured": float(reg_zero["slope"]),
                                "reference": 4.0,
                                "unit": "1",
                            }
                        ],
                    },
                )
            )
            slope_dev_abs_pct = float(slope_cmp["comparisons"][0]["percent_deviation"])
            assert slope_dev_abs_pct < 5.0
            slope_dev_signed_pct = (float(reg_zero["slope"]) - 4.0) / 4.0 * 100.0

            derived_ds = json.loads(
                (run_root / "datasets" / "lab5_table2_derived.json").read_text(encoding="utf-8")
            )
            derived_rows: list[dict[str, Any]] = derived_ds["rows"]

            # ------------------------------------------------------------------
            # 6. Worked Problems 3, 4A–C, and 5 via calc_eval
            # ------------------------------------------------------------------
            # Problem 3: q = 3e-7 cos(800*pi*t), L = 5 H
            p3_omega = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "800 * pi",
                        "output_unit": "1",
                        "label": "Problem 3 angular frequency (rad/s)",
                        "run_id": run_id,
                    },
                )
            )
            p3_im = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "q_m * omega",
                        "variables": {
                            "q_m": {"value": 3e-7, "unit": "C"},
                            "omega": {"value": float(p3_omega["output_value"]), "unit": "rad/s"},
                        },
                        "output_unit": "mA",
                        "label": "Problem 3 current amplitude I_m",
                        "run_id": run_id,
                    },
                )
            )
            p3_wm = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "0.5 * L * I_m**2",
                        "variables": {
                            "L": {"value": 5.0, "unit": "H"},
                            "I_m": {"value": float(p3_im["output_value"]), "unit": "mA"},
                        },
                        "output_unit": "J",
                        "label": "Problem 3 max magnetic energy W_m,max",
                        "run_id": run_id,
                    },
                )
            )
            assert float(p3_im["output_value"]) == pytest.approx(0.7539822, rel=1e-5)
            assert float(p3_wm["output_value"]) == pytest.approx(1.421223e-6, rel=1e-5)
            p3_calc_ids = [p3_omega["calc_id"], p3_im["calc_id"], p3_wm["calc_id"]]

            # Problem 4A–C: q_m = 2 nC, T = 8 us, C = 50 pF
            p4_freq = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "1 / T",
                        "variables": {"T": {"value": 8.0, "unit": "us"}},
                        "output_unit": "kHz",
                        "label": "Problem 4A frequency f = 1/T",
                        "run_id": run_id,
                    },
                )
            )
            p4_omega = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "2 * pi / T",
                        "variables": {"T": {"value": 8.0, "unit": "us"}},
                        "output_unit": "rad/s",
                        "label": "Problem 4A angular frequency omega = 2*pi/T",
                        "run_id": run_id,
                    },
                )
            )
            p4_im = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "q_m * omega",
                        "variables": {
                            "q_m": {"value": 2.0, "unit": "nC"},
                            "omega": {"value": float(p4_omega["output_value"]), "unit": "rad/s"},
                        },
                        "output_unit": "mA",
                        "label": "Problem 4C current amplitude I_m = q_m * omega",
                        "run_id": run_id,
                    },
                )
            )
            p4_wm_harmonic = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "(q_m**2 / (2 * C)) * sin(omega * t)**2",
                        "variables": {
                            "q_m": {"value": 2.0, "unit": "nC"},
                            "C": {"value": 50.0, "unit": "pF"},
                            "omega": {"value": float(p4_omega["output_value"]), "unit": "rad/s"},
                            "t": {"value": 5.0, "unit": "us"},
                        },
                        "output_unit": "J",
                        "label": "Problem 4B W_m(5 us) from exact harmonic model",
                        "run_id": run_id,
                    },
                )
            )
            p4_wm_table = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "(q_m**2 - q_tab**2) / (2 * C)",
                        "variables": {
                            "q_m": {"value": 2.0, "unit": "nC"},
                            "q_tab": {"value": -1.5, "unit": "nC"},
                            "C": {"value": 50.0, "unit": "pF"},
                        },
                        "output_unit": "J",
                        "label": "Problem 4B W_m(5 us) from rounded table q=-1.5 nC",
                        "run_id": run_id,
                    },
                )
            )
            assert float(p4_freq["output_value"]) == pytest.approx(125.0, rel=1e-9)
            assert float(p4_im["output_value"]) == pytest.approx(1.5707963, rel=1e-5)
            assert float(p4_wm_harmonic["output_value"]) == pytest.approx(2.0e-8, rel=1e-9)
            assert float(p4_wm_table["output_value"]) == pytest.approx(1.75e-8, rel=1e-9)
            p4_calc_ids = [
                p4_freq["calc_id"],
                p4_omega["calc_id"],
                p4_im["calc_id"],
                p4_wm_harmonic["calc_id"],
                p4_wm_table["calc_id"],
            ]

            # Problem 5: C = 8 pF, L = 0.5 mH, I_max = 40 mA -> U_max
            p5_umax = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "I_max * sqrt(L / C)",
                        "variables": {
                            "I_max": {"value": 40.0, "unit": "mA"},
                            "L": {"value": 0.5, "unit": "mH"},
                            "C": {"value": 8.0, "unit": "pF"},
                        },
                        "output_unit": "V",
                        "label": "Problem 5 max capacitor voltage U_max",
                        "run_id": run_id,
                    },
                )
            )
            assert float(p5_umax["output_value"]) == pytest.approx(316.227766, rel=1e-5)
            p5_calc_ids = [p5_umax["calc_id"]]

            calc_ids.extend(p3_calc_ids + p4_calc_ids + p5_calc_ids)

            # ------------------------------------------------------------------
            # 7. Vector Visuals (diagram_render & plot_render)
            # ------------------------------------------------------------------
            # Fig. 1: Redrawn series RLC circuit with handout notation (ε_0, U_0C, U_0L, U_0R)
            fig_circuit = _parse_tool_json(
                await session.call_tool(
                    "diagram_render",
                    {
                        "run_id": run_id,
                        "source": "model_computed",
                        "spec": {
                            "diagram_id": "lab5_fig1_rlc_circuit",
                            "kind": "circuit",
                            "title": (
                                "Series RLC Circuit with Harmonic EMF Source (Handout Notation)"
                            ),
                            "elements": [
                                {
                                    "kind": "source_sin",
                                    "label": r"$\varepsilon_0 \cos\omega t$",
                                    "at": [0.0, 0.0],
                                    "to": [0.0, 3.0],
                                    "style": {"label_loc": "top"},
                                },
                                {
                                    "kind": "dot",
                                    "at": [0.0, 3.0],
                                },
                                {
                                    "kind": "capacitor",
                                    "label": "$C$",
                                    "at": [0.0, 3.0],
                                    "to": [3.0, 3.0],
                                    "style": {"label_loc": "bottom"},
                                },
                                {
                                    "kind": "dot",
                                    "at": [3.0, 3.0],
                                },
                                {
                                    "kind": "inductor",
                                    "label": "$L$",
                                    "at": [3.0, 3.0],
                                    "to": [6.0, 3.0],
                                    "style": {"label_loc": "bottom"},
                                },
                                {
                                    "kind": "dot",
                                    "at": [6.0, 3.0],
                                },
                                {
                                    "kind": "resistor",
                                    "label": "$R$",
                                    "at": [6.0, 3.0],
                                    "to": [9.0, 3.0],
                                    "style": {"label_loc": "bottom", "iec": True},
                                },
                                {
                                    "kind": "dot",
                                    "at": [9.0, 3.0],
                                },
                                {
                                    "kind": "line",
                                    "at": [9.0, 3.0],
                                    "to": [9.0, 0.0],
                                },
                                {
                                    "kind": "line",
                                    "at": [9.0, 0.0],
                                    "to": [0.0, 0.0],
                                },
                                # Parallel voltmeters U_0C, U_0L, U_0R above C, L, R
                                {"kind": "line", "at": [0.0, 3.0], "to": [0.0, 5.2]},
                                {
                                    "kind": "voltmeter",
                                    "label": "$U_{0C}$",
                                    "at": [0.0, 5.2],
                                    "to": [3.0, 5.2],
                                    "style": {"color": "#c0392b", "label_loc": "top"},
                                },
                                {"kind": "line", "at": [3.0, 5.2], "to": [3.0, 3.0]},
                                {"kind": "line", "at": [3.0, 3.0], "to": [3.0, 5.2]},
                                {
                                    "kind": "voltmeter",
                                    "label": "$U_{0L}$",
                                    "at": [3.0, 5.2],
                                    "to": [6.0, 5.2],
                                    "style": {"color": "#1f4e79", "label_loc": "top"},
                                },
                                {"kind": "line", "at": [6.0, 5.2], "to": [6.0, 3.0]},
                                {"kind": "line", "at": [6.0, 3.0], "to": [6.0, 5.2]},
                                {
                                    "kind": "voltmeter",
                                    "label": "$U_{0R}$",
                                    "at": [6.0, 5.2],
                                    "to": [9.0, 5.2],
                                    "style": {"color": "#d97706", "label_loc": "top"},
                                },
                                {"kind": "line", "at": [9.0, 5.2], "to": [9.0, 3.0]},
                            ],
                            "notes": [
                                "Redrawn per handout: ε₀ replaces U; "
                                "U₀C, U₀L, U₀R replace UC, UL, UR"
                            ],
                        },
                    },
                )
            )

            # Fig. 2: Live resonance traces (U_0C / ε_0 vs ω) across all 4 resistors
            trace_colors = {3.2: "#1f4e79", 4.0: "#c0392b", 5.0: "#2e7d32", 6.0: "#d97706"}
            trace_series = []
            for r_ohm in resistors:
                pts = trace_series_by_r[r_ohm]
                trace_series.append(
                    {
                        "label": f"R = {r_ohm:.1f} Ω (Q = {EXPECTED_PLATEAUS[r_ohm][2]:.2f})",
                        "x": [p[0] for p in pts],
                        "y": [p[1] for p in pts],
                        "kind": "line",
                        "color": trace_colors[r_ohm],
                        "marker": "o",
                    }
                )
            fig_traces = _parse_tool_json(
                await session.call_tool(
                    "plot_render",
                    {
                        "run_id": run_id,
                        "source": "simulator_captured",
                        "spec": {
                            "plot_id": "lab5_fig2_resonance_traces",
                            "title": "Live Resonance Curves and Discrete Plateaus (Team 2)",
                            "x_label": "Driving angular frequency ω (rad/s)",
                            "y_label": "Normalized capacitor amplitude U₀C / ε₀",
                            "series": trace_series,
                            "annotations": [
                                "L = 5.0 mH, C = 32 µF, ω₀ = 2500 rad/s",
                                "Plateaus: [2445,2472], [2427,2444], [2361,2434], [2333,2370]",
                            ],
                        },
                    },
                )
            )

            # Fig. 3: ω₀/Δω vs Q² with zero-intercept OLS fit & theoretical 4Q² line
            q2_vals = [float(r["Q2"]) for r in derived_rows]
            y_vals = [float(r["w0_over_dw"]) for r in derived_rows]
            x_line = [0.0, max(q2_vals) * 1.06]
            fig_q2 = _parse_tool_json(
                await session.call_tool(
                    "plot_render",
                    {
                        "run_id": run_id,
                        "source": "simulator_captured",
                        "spec": {
                            "plot_id": "lab5_fig3_w0_dw_vs_q2",
                            "title": "Inverse Normalized Resonance Shift ω₀/Δω vs Q²",
                            "x_label": "Squared quality factor Q² = (U₀C / ε₀)²",
                            "y_label": "Inverse normalized shift ω₀ / Δω",
                            "series": [
                                {
                                    "label": "Measured (m96220.swf)",
                                    "x": q2_vals,
                                    "y": y_vals,
                                    "kind": "scatter",
                                    "color": "#1f4e79",
                                    "marker": "o",
                                },
                                {
                                    "label": (
                                        f"Zero-intercept OLS: slope = {reg_zero['slope']:.3f} "
                                        f"(R² = {reg_zero['r_squared']:.5f})"
                                    ),
                                    "x": x_line,
                                    "y": [float(reg_zero["slope"]) * x for x in x_line],
                                    "kind": "fit_line",
                                    "color": "#c0392b",
                                    "linestyle": "--",
                                },
                                {
                                    "label": "Theoretical asymptote: ω₀/Δω = 4Q²",
                                    "x": x_line,
                                    "y": [4.0 * x for x in x_line],
                                    "kind": "line",
                                    "color": "#2e7d32",
                                    "linestyle": ":",
                                },
                            ],
                            "annotations": [
                                f"OLS slope = {reg_zero['slope']:.3f} ± {reg_zero['slope_se']:.3f}",
                                f"Deviation from 4.000: {slope_dev_signed_pct:+.2f}%",
                            ],
                        },
                    },
                )
            )

            # Fig. 4: Series RLC Phasor Diagram (for theory & Self-Control Q&As #1, #3, #36)
            fig_phasor = _parse_tool_json(
                await session.call_tool(
                    "diagram_render",
                    {
                        "run_id": run_id,
                        "source": "model_computed",
                        "spec": {
                            "diagram_id": "lab5_fig4_phasor_diagram",
                            "kind": "phasor",
                            "title": (
                                "Series RLC Voltage Phasor Diagram "
                                "(Near Capacitive Resonance)"
                            ),
                            "elements": [
                                {
                                    "kind": "phasor",
                                    "label": "$I_0$",
                                    "at": [0.0, 0.0],
                                    "to": [2.9, 0.0],
                                    "style": {
                                        "color": "#2e7d32",
                                        "lw": 2.4,
                                        "label_offset": [0.22, 0.28],
                                        "ha": "left",
                                    },
                                },
                                {
                                    "kind": "phasor",
                                    "label": "$U_{0R}$",
                                    "at": [0.0, 0.0],
                                    "to": [2.0, 0.0],
                                    "style": {
                                        "color": "#d97706",
                                        "lw": 2.4,
                                        "label_pos": "mid",
                                        "label_offset": [0.0, 0.34],
                                    },
                                },
                                {
                                    "kind": "phasor",
                                    "label": "$U_{0L}$",
                                    "at": [0.0, 0.0],
                                    "to": [0.0, 2.2],
                                    "style": {
                                        "color": "#1f4e79",
                                        "lw": 2.2,
                                        "label_offset": [0.36, 0.08],
                                        "ha": "left",
                                    },
                                },
                                {
                                    "kind": "phasor",
                                    "label": "$U_{0C}$",
                                    "at": [0.0, 0.0],
                                    "to": [0.0, -3.0],
                                    "style": {
                                        "color": "#c0392b",
                                        "lw": 2.2,
                                        "label_offset": [0.36, -0.08],
                                        "ha": "left",
                                    },
                                },
                                {
                                    "kind": "phasor",
                                    "label": r"$\varepsilon_0$",
                                    "at": [0.0, 0.0],
                                    "to": [2.0, -0.8],
                                    "style": {
                                        "color": "#6b4c9a",
                                        "lw": 2.4,
                                        "label_offset": [0.28, -0.18],
                                        "ha": "left",
                                    },
                                },
                                {
                                    "kind": "projection",
                                    "at": [2.0, 0.0],
                                    "to": [2.0, -0.8],
                                    "style": {"color": "#64748b"},
                                },
                                {
                                    "kind": "projection",
                                    "at": [0.0, -0.8],
                                    "to": [2.0, -0.8],
                                    "style": {"color": "#64748b"},
                                },
                                {
                                    "kind": "arc",
                                    "label": r"$\varphi$",
                                    "at": [0.0, 0.0],
                                    "length": 1.05,
                                    "style": {
                                        "start_deg": -21.8,
                                        "end_deg": 0.0,
                                        "color": "#6b4c9a",
                                    },
                                },
                            ],
                            "notes": [
                                "At ω < ω₀: U₀C > U₀L so source EMF lags current by φ < 0"
                            ],
                        },
                    },
                )
            )

            # Fig. 5: Problem 4C q–I phase-portrait ellipse (equal_aspect=True)
            n_pts = 181
            thetas = [2.0 * math.pi * i / (n_pts - 1) for i in range(n_pts)]
            qm_nc = 2.0
            im_ma = float(p4_im["output_value"])
            q_ellipse = [qm_nc * math.cos(th) for th in thetas]
            i_ellipse = [-im_ma * math.sin(th) for th in thetas]
            t_samples_us = [0, 1, 2, 3, 4, 5, 6, 7]
            q_pts = [qm_nc * math.cos(2.0 * math.pi * t / 8.0) for t in t_samples_us]
            i_pts = [-im_ma * math.sin(2.0 * math.pi * t / 8.0) for t in t_samples_us]
            fig_ellipse = _parse_tool_json(
                await session.call_tool(
                    "plot_render",
                    {
                        "run_id": run_id,
                        "source": "model_computed",
                        "spec": {
                            "plot_id": "lab5_fig5_prob4c_qi_ellipse",
                            "title": "Problem 4C: Charge–Current Phase Portrait (q–I Ellipse)",
                            "x_label": "Capacitor charge q (nC)",
                            "y_label": "Circuit current I (mA)",
                            "equal_aspect": True,
                            "width_in": 5.4,
                            "height_in": 4.4,
                            "series": [
                                {
                                    "label": "Harmonic ellipse (q/qₘ)² + (I/Iₘ)² = 1",
                                    "x": q_ellipse,
                                    "y": i_ellipse,
                                    "kind": "line",
                                    "color": "#1f4e79",
                                },
                                {
                                    "label": "Sample instants t = 0..7 µs (T = 8 µs)",
                                    "x": q_pts,
                                    "y": i_pts,
                                    "kind": "scatter",
                                    "color": "#c0392b",
                                    "marker": "o",
                                },
                            ],
                            "annotations": [
                                f"qₘ = 2.00 nC, Iₘ = {im_ma:.3f} mA",
                                "T = 8.0 µs, f = 125 kHz",
                            ],
                        },
                    },
                )
            )

            # ------------------------------------------------------------------
            # 8. Build Table 1 & Table 2 and Compile Full Academic Report
            # ------------------------------------------------------------------
            table1 = {
                "table_id": "lab5_table1",
                "caption": (
                    "Given Coil Inductance L and Capacitance C "
                    "(Handout Table 1; Team 2 Selected)"
                ),
                "headers": ["Team", "1", "2 (Selected)", "3", "4", "5", "6", "7", "8"],
                "rows": [
                    ["L, mH", "5.0", "5.0", "4.7", "4.1", "3.8", "4.1", "3.5", "3.3"],
                    ["C, µF", "35", "32", "34", "36", "36", "31", "34", "33"],
                ],
                "notes": (
                    "Team 2 parameters from handout Table 1: L = 5.0 mH, C = 32 µF, "
                    "natural angular frequency ω₀ = 1/√(LC) = 2500 rad/s = 2.500 × 10³ rad/s."
                ),
                "source": "bench_manual",
            }

            row_by_r = {float(r["R_ohm"]): r for r in derived_rows}
            table2_rows = [
                [r"$\omega_1$, $10^3$ rad/s"]
                + [f"{row_by_r[r]['w1'] / 1000.0:.3f}" for r in resistors],
                [r"$\omega_2$, $10^3$ rad/s"]
                + [f"{row_by_r[r]['w2'] / 1000.0:.3f}" for r in resistors],
                [r"$\omega_{\text{res}} = (\omega_1 + \omega_2)/2$, $10^3$ rad/s"]
                + [f"{row_by_r[r]['w_res'] / 1000.0:.4f}" for r in resistors],
                [r"$\omega_{\text{res,ideal}}$, $10^3$ rad/s"]
                + [f"{ideal_by_r[r]['w_res_ideal'] / 1000.0:.4f}" for r in resistors],
                [r"$\Delta\omega = \omega_0 - \omega_{\text{res}}$, $10^3$ rad/s"]
                + [f"{row_by_r[r]['dw'] / 1000.0:.4f}" for r in resistors],
                [r"$\omega_0 / \Delta\omega$"]
                + [f"{row_by_r[r]['w0_over_dw']:.2f}" for r in resistors],
                [r"$Q = U_{0C} / \varepsilon_0$ (peak)"]
                + [f"{row_by_r[r]['Q_meas']:.2f}" for r in resistors],
                [r"$Q^2$"] + [f"{row_by_r[r]['Q2']:.2f}" for r in resistors],
                [r"$4Q^2$ (theory)"] + [f"{row_by_r[r]['four_Q2']:.2f}" for r in resistors],
            ]
            table2 = {
                "table_id": "lab5_table2",
                "caption": (
                    "Live Resonance Plateau Measurements for Team 2 "
                    "(Handout Table 2; ω₀ = 2.500 × 10³ rad/s)"
                ),
                "headers": ["Quantity"] + [f"R = {r:.1f} Ω" for r in resistors],
                "rows": table2_rows,
                "notes": (
                    "All ω₁, ω₂, and peak U₀C/ε₀ values were captured live from m96220.swf "
                    "in Ruffle 0.7.1 via coarse-to-fine plateau_max search. Plateau widths: "
                    "28, 18, 74, and 38 discrete 1 rad/s steps."
                ),
                "source": "simulator_captured",
            }

            report_spec = {
                "title": "Laboratory Work 3: Forced Oscillations in an RLC Circuit",
                "subtitle": "Live Ruffle 0.7.1 Simulation & Dimensional Analysis Report (Team 2)",
                "lab_number": "3 (Lab 5 Benchmark)",
                "language": "en",
                "author": "PEA v2 Automated Benchmark",
                "team": "Team 2 (L = 5.0 mH, C = 32 µF)",
                "course": "General Physics — Electricity & Magnetism",
                "date_str": "2026-11-22",
                "objective": (
                    "Investigate steady-state forced oscillations in a series RLC circuit driven "
                    "by a harmonic EMF source, measure the discrete resonance plateau [ω₁, ω₂] "
                    "and capacitor voltage amplification Q = U₀C/ε₀ across four resistors, and "
                    "experimentally test the asymptotic relation ω₀/Δω = 4Q²."
                ),
                "apparatus": [
                    (
                        "Sandboxed m96220.swf virtual RLC apparatus "
                        "(800×600 native stage in Ruffle 0.7.1)"
                    ),
                    "Harmonic EMF source ε₀ cos(ωt) with frequency stepper ω ∈ [1000, 5000] rad/s",
                    (
                        "Adjustable inductance L = 5.0 mH, capacitance C = 32 µF, "
                        "resistance R ∈ {3.2, 4.0, 5.0, 6.0} Ω"
                    ),
                    "Offline RapidOCR ONNX readout extractor with SHA-256 frame provenance",
                ],
                "theory_sections": [
                    {
                        "heading": "Forced Oscillations and Capacitor Voltage Resonance",
                        "body": (
                            "In a series RLC circuit driven by $\\varepsilon(t) = \\varepsilon_0 "
                            "\\cos(\\omega t)$, the steady-state current amplitude is determined "
                            "by total impedance $Z = \\sqrt{R^2 + (\\omega L - 1/(\\omega C))^2}$. "
                            "The normalized capacitor voltage amplitude peaks slightly below the "
                            "natural frequency $\\omega_0 = 1/\\sqrt{LC}$ at "
                            "$\\omega_{\\text{res}} = \\omega_0\\sqrt{1 - 1/(2Q^2)}$."
                        ),
                        "equation": (
                            r"\frac{U_{0C}}{\varepsilon_0} = \frac{1}{\sqrt{\left(1 - "
                            r"\frac{\omega^2}{\omega_0^2}\right)^2 + "
                            r"\frac{\omega^2}{Q^2 \omega_0^2}}}"
                            "\n"
                            r"\Delta\omega = \omega_0 - \omega_{\text{res}} \approx "
                            r"\frac{\omega_0}{4Q^2} \implies "
                            r"\frac{\omega_0}{\Delta\omega} \approx 4Q^2"
                        ),
                    }
                ],
                "procedure": [
                    (
                        "Inspect the 8-page laboratory handout PDF and extract Team 2 "
                        "parameters (L = 5.0 mH, C = 32 µF)."
                    ),
                    (
                        "Calibrate m96220.swf controls (R, L, C, ω) and output readouts "
                        "(ω₀, U₀C/ε₀) and persist the SHA-256 profile."
                    ),
                    (
                        "Compute ω₀ = 1/√(LC) = 2500 rad/s via calc_eval and verify "
                        "live simulator agreement."
                    ),
                    (
                        "For each R ∈ {3.2, 4.0, 5.0, 6.0} Ω, run coarse (−10 rad/s) then "
                        "fine (−1 rad/s) plateau_max searches from ω₀ = 2500 rad/s to capture "
                        "exact plateau boundaries [ω₁, ω₂]."
                    ),
                    (
                        "Analyze ω₀/Δω versus Q² using OLS regression and compare against "
                        "the theoretical slope 4."
                    ),
                ],
                "tables": [table1, table2],
                "figures": [fig_circuit, fig_traces, fig_q2, fig_phasor, fig_ellipse],
                "analysis_paragraphs": [
                    (
                        "All four resistors yielded clean discrete maximum plateaus in "
                        "$U_{0C}/\\varepsilon_0$: $R=3.2\\,\\Omega \\to [2445, 2472]$ rad/s "
                        "($Q=3.94$, 28 steps), $R=4.0\\,\\Omega \\to [2427, 2444]$ rad/s "
                        "($Q=3.17$, 18 steps), $R=5.0\\,\\Omega \\to [2361, 2434]$ rad/s "
                        "($Q=2.55$, 74 steps), and $R=6.0\\,\\Omega \\to [2333, 2370]$ rad/s "
                        "($Q=2.15$, 38 steps). Every plateau midpoint matches exact RLC theory "
                        "$\\omega_0\\sqrt{1 - 1/(2Q^2)}$ within 0.02%."
                    ),
                    (
                        "Zero-intercept OLS of $\\omega_0/\\Delta\\omega$ versus $Q^2$ yields "
                        f"a slope of {reg_zero['slope']:.3f} ± {reg_zero['slope_se']:.3f} "
                        f"($R^2 = {reg_zero['r_squared']:.5f}$), deviating by "
                        f"{slope_dev_signed_pct:+.2f}% from the high-$Q$ asymptotic prediction "
                        "$4.000$. The small negative offset is physically expected because: "
                        "(i) $U_{0C}/\\varepsilon_0$ at capacitive resonance equals "
                        "$Q/\\sqrt{1 - 1/(4Q^2)} > Q$, slightly inflating measured $Q^2$ at "
                        "moderate $Q$, and (ii) the Taylor approximation "
                        "$1 - \\sqrt{1 - 1/(2Q^2)} \\approx 1/(4Q^2)$ omits the positive "
                        "$1/(32Q^4)$ term."
                    ),
                ],
                "conclusions": [
                    (
                        "Natural frequency ω₀ = 2500 rad/s computed from L = 5.0 mH and "
                        "C = 32 µF matches the simulator readout exactly."
                    ),
                    (
                        "Increasing resistance $R$ lowers $Q$, broadens the discrete resonance "
                        "plateau, and shifts $\\omega_{\\text{res}}$ further below $\\omega_0$."
                    ),
                    (
                        f"Measured ω₀/Δω scales linearly with Q² "
                        f"(R² = {reg_zero['r_squared']:.5f}) with slope "
                        f"{reg_zero['slope']:.3f}, confirming ω₀/Δω ≈ 4Q² within finite-Q "
                        "and two-decimal readout rounding limits."
                    ),
                    (
                        "All raw simulator screenshots, OCR logs, calculations, and figures "
                        "pass strict SHA-256 provenance verification."
                    ),
                ],
                "calc_ids": calc_ids,
                "options": {
                    "include_self_control": True,
                    "include_problems": True,
                    "include_evidence_appendix": False,
                },
                "self_control": [
                    {
                        "number": "1",
                        "question": (
                            "Series RLC Oscillatory Circuit: Define its main components, "
                            "explain electric–magnetic energy transfer, write governing "
                            "equations, illustrate the circuit, and give a practical application."
                        ),
                        "answer": (
                            "A series RLC circuit connects a resistor $R$ (dissipating "
                            "electrical energy as Joule heat $I^2 R$), an inductor $L$ (storing "
                            "magnetic field energy $W_m = \\frac{1}{2}LI^2$), and a capacitor "
                            "$C$ (storing electric field energy $W_e = \\frac{q^2}{2C}$) in a "
                            "single closed loop with a harmonic source "
                            "$\\varepsilon(t) = \\varepsilon_0\\cos\\omega t$. During each cycle, "
                            "energy oscillates back and forth between the capacitor's electric "
                            "field and the inductor's magnetic field while the source replenishes "
                            "resistive losses. Practical application: radio-frequency bandpass "
                            "tuning stages that select one carrier frequency."
                        ),
                        "equations": [
                            (
                                r"L\frac{d^2 q}{dt^2} + R\frac{dq}{dt} + \frac{q}{C} "
                                r"= \varepsilon_0 \cos(\omega t)"
                            ),
                            (
                                r"\omega_0 = \frac{1}{\sqrt{LC}}, \qquad "
                                r"W = \frac{q^2}{2C} + \frac{LI^2}{2}"
                            ),
                        ],
                        "figure_ids": ["lab5_fig1_rlc_circuit"],
                    },
                    {
                        "number": "3",
                        "question": (
                            "Quality Factor of an Oscillatory Circuit: Define its physical "
                            "meaning in terms of energy losses and resonance sharpness, give "
                            "formulas and experimental determination in this lab, illustrate the "
                            "resonance curve, and cite an application."
                        ),
                        "answer": (
                            "The quality factor $Q$ measures $2\\pi$ times the ratio of stored "
                            "oscillation energy to energy dissipated per cycle. High $Q$ implies "
                            "low damping, a narrow fractional bandwidth "
                            "$\\Delta\\omega_{\\text{FWHM}}/\\omega_0 = 1/Q$, and strong resonant "
                            "voltage amplification across $C$ and $L$. In this laboratory work, "
                            "$Q$ is read experimentally as the peak normalized capacitor voltage "
                            "$U_{0C}/\\varepsilon_0$ while stepping $\\omega$ downward through "
                            "resonance. Practical application: quartz crystal resonators "
                            "($Q \\sim 10^4\\text{–}10^6$) in precision clocks."
                        ),
                        "equations": [
                            (
                                r"Q = \frac{\omega_0 L}{R} = \frac{1}{\omega_0 R C} "
                                r"= \frac{1}{R}\sqrt{\frac{L}{C}}"
                            ),
                            (
                                r"\frac{U_{0C,\max}}{\varepsilon_0} = "
                                r"\frac{Q}{\sqrt{1 - 1/(4Q^2)}} \approx Q"
                            ),
                        ],
                        "figure_ids": ["lab5_fig2_resonance_traces"],
                    },
                    {
                        "number": "36",
                        "question": (
                            "Total Impedance of a Series RLC Circuit: Explain how $R$, $X_L$, "
                            "and $X_C$ combine, write the complex impedance and magnitude, "
                            "illustrate the phasor diagram, discuss resonance, and give an RF "
                            "tuning example."
                        ),
                        "answer": (
                            "In steady-state AC, the inductive voltage leads current by $+\\pi/2$ "
                            "while capacitive voltage lags by $-\\pi/2$, so their reactances "
                            "subtract along the imaginary axis while $R$ lies on the real axis. "
                            "At $\\omega = \\omega_0$, $X_L = X_C$, total impedance attains its "
                            "minimum $Z = R$, phase shift $\\varphi = 0$, and current amplitude "
                            "reaches $\\varepsilon_0/R$. RF receivers vary $C$ to bring the "
                            "desired station's carrier to $\\omega_0$, maximizing selectivity."
                        ),
                        "equations": [
                            r"\tilde{Z} = R + j\left(\omega L - \frac{1}{\omega C}\right), \qquad "
                            r"|Z| = \sqrt{R^2 + \left(\omega L - \frac{1}{\omega C}\right)^2}",
                            r"\tan\varphi = \frac{\omega L - 1/(\omega C)}{R}",
                        ],
                        "figure_ids": ["lab5_fig4_phasor_diagram"],
                    },
                ],
                "problems": [
                    {
                        "number": "3",
                        "prompt": (
                            "The charge on a capacitor varies as "
                            "$q = 3\\times 10^{-7}\\cos(800\\pi t)$ C with $L = 5$ H. "
                            "Find $I(t)$, $q_m$, $I_m$, $\\omega$, and the maximum magnetic energy."
                        ),
                        "solution": (
                            "Differentiating $q(t) = q_m \\cos(\\omega t)$ yields "
                            "$I(t) = dq/dt = -q_m\\omega\\sin(\\omega t) = "
                            "-7.54\\times 10^{-4}\\sin(800\\pi t)$ A. "
                            "Thus $q_m = 3.00\\times 10^{-7}$ C, $\\omega = 800\\pi \\approx "
                            f"{float(p3_omega['output_value']):.2f}$ rad/s, $I_m = q_m\\omega = "
                            f"{float(p3_im['output_value']):.4f}$ mA, and "
                            "$W_{m,\\max} = \\frac{1}{2}L I_m^2 = "
                            f"{float(p3_wm['output_value']) * 1e6:.4f}\\,\\mu\\text{{J}}$."
                        ),
                        "calc_ids": p3_calc_ids,
                        "figure_ids": [],
                    },
                    {
                        "number": "4",
                        "prompt": (
                            "Given the tabulated free-oscillation charge $q(t)$ with $t$ in "
                            "$10^{-6}$ s and $q$ in $10^{-9}$ C: (A) write $q(t)$ and find "
                            "$q_m$, $T$, $\\omega$, $f$; (B) find $W_m$ at $t = 5\\,\\mu\\text{s}$ "
                            "for $C = 50$ pF; (C) plot the $q$–$I$ phase portrait."
                        ),
                        "solution": (
                            "(A) From the table, $q(0) = q_m = 2.0$ nC and $q(t)$ completes one "
                            "full period at $T = 8.0\\,\\mu\\text{s}$, giving $f = 1/T = "
                            f"{float(p4_freq['output_value']):.1f}$ kHz and "
                            f"$\\omega = 2\\pi/T = {float(p4_omega['output_value']):.0f}$ rad/s; "
                            "hence $q(t) = 2.0\\times 10^{-9}"
                            "\\cos(\\frac{\\pi}{4}\\times 10^6 t)$ C.\n\n"
                            "(B) Total conserved energy is "
                            "$W_{\\text{tot}} = q_m^2/(2C) = 40.0$ nJ. "
                            "Using the exact harmonic law at $t = 5\\,\\mu\\text{s}$ "
                            "($\\omega t = 5\\pi/4$), "
                            "$W_m = W_{\\text{tot}}\\sin^2(5\\pi/4) = "
                            f"{float(p4_wm_harmonic['output_value']) * 1e9:.2f}$ nJ. Using the "
                            "handout's rounded table value $q(5\\,\\mu\\text{s}) = -1.5$ nC "
                            "gives $W_m = (q_m^2 - q^2)/(2C) = "
                            f"{float(p4_wm_table['output_value']) * 1e9:.2f}$ nJ.\n\n"
                            "(C) Eliminating time between $q(t) = q_m\\cos\\omega t$ and "
                            f"$I(t) = -I_m\\sin\\omega t$ ($I_m = "
                            f"{float(p4_im['output_value']):.4f}$ mA) yields the phase-portrait "
                            "ellipse $(q/q_m)^2 + (I/I_m)^2 = 1$ shown below."
                        ),
                        "calc_ids": p4_calc_ids,
                        "figure_ids": ["lab5_fig5_prob4c_qi_ellipse"],
                    },
                    {
                        "number": "5",
                        "prompt": (
                            "An ideal LC circuit has $C = 8$ pF, $L = 0.5$ mH, and "
                            "$I_{\\max} = 40$ mA. Determine the maximum voltage $U_{\\max}$ across "
                            "the capacitor plates."
                        ),
                        "solution": (
                            "By energy conservation in an ideal LC circuit, "
                            "$\\frac{1}{2} C U_{\\max}^2 = \\frac{1}{2} L I_{\\max}^2$, so "
                            f"$U_{{\\max}} = I_{{\\max}}\\sqrt{{L/C}} = "
                            f"{float(p5_umax['output_value']):.2f}$ V."
                        ),
                        "calc_ids": p5_calc_ids,
                        "figure_ids": [],
                    },
                ],
            }

            built = _parse_tool_json(
                await session.call_tool(
                    "report_build",
                    {
                        "run_id": run_id,
                        "spec": report_spec,
                        "formats": ["pdf", "docx", "evidence_zip"],
                        "include_teacher_packet": True,
                        "render_previews": True,
                        "preview_dpi": 150,
                        "profile_paths": [saved_prof["saved_relpath"]],
                    },
                )
            )
            assert built["pdf_relpath"] == "reports/report.pdf"
            assert built["docx_relpath"] == "reports/report.docx"
            assert built["evidence_zip_relpath"] == "reports/evidence.zip"
            assert built["teacher_packet_relpath"] == "reports/teacher-evidence.pdf"
            assert built["page_count"] >= 4
            assert len(built["preview_png_relpaths"]) == built["page_count"]

            # Explicitly exercise report_preview MCP tool as well
            previews = _parse_tool_json(
                await session.call_tool(
                    "report_preview",
                    {
                        "run_id": run_id,
                        "pdf_relpath": "reports/report.pdf",
                        "dpi": 150,
                        "max_pages": 30,
                    },
                )
            )
            assert previews["page_count"] == built["page_count"]
            for rel in previews["preview_png_relpaths"]:
                assert (run_root / rel).is_file()

            # Verify DOCX parseback and Evidence ZIP integrity
            doc_obj = docx.Document(str(run_root / built["docx_relpath"]))
            doc_text = "\n".join(p.text for p in doc_obj.paragraphs)
            assert "Forced Oscillations in an RLC Circuit" in doc_text
            assert len(doc_obj.tables) >= 2

            with zipfile.ZipFile(run_root / built["evidence_zip_relpath"]) as zf:
                names = set(zf.namelist())
                assert "provenance.jsonl" in names
                assert "manifest.json" in names
                assert any(n.startswith("evidence/") and n.endswith(".png") for n in names)

            # Final SHA-256 provenance verification across the entire run
            verified = _parse_tool_json(
                await session.call_tool("lab_runs", {"action": "verify", "run_id": run_id})
            )
            assert verified["verification"]["ok"] is True, (
                f"Provenance verification failed: {verified['verification']['errors']}"
            )
            assert verified["verification"]["checked_entries"] >= 200
