"""Live E2E benchmark for Lab 4 (Magnetic Field of a Straight Wire & Circular Loop).

Drives the **exact same zero-hardcoded** ``pea-mcp`` server over real
``mcp.client.stdio.stdio_client`` against two completely different SWF simulators:
1. ``m96210.swf`` (``500x450`` native stage): Infinite straight wire, 36 live readings
   (``I in {-5, -10, -15, -20} A``, ``r in {2, 3, ..., 10} cm``).
2. ``m96211.swf`` (``700x500`` native stage): Circular current loop (``R = 3.0 cm``), 36 live
   readings capturing unclipped ``Bx`` and ``By`` (verifying ``By == 0``) and deriving
   ``B = sqrt(Bx**2 + By**2)`` deterministically via ``data_analyze``.

Verifies:
- **72/72 exact match** against ``local_assets/benchmarks/lab4-measurements.json`` (with an
  explicit scientific-honesty note that this confirms repeatability against the earlier
  automated Ruffle pilot while independent manual reference verification remains pending).
- Handout-compliant Tables 2 & 3 including transformed rows (``1/r`` and ``(R^2+r^2)^(-3/2)``).
- Linearized plots (``B`` vs ``1/r`` and ``B`` vs ``(R^2+r^2)^(-3/2)``) + magnetic field
  diagram (``diagram_render(kind="field")``).
- Permeability ``mu_0`` recovery across all currents & geometries with ``< 0.5%`` deviation
  from ``4*pi*1e-7 T*m/A``.
- Full offline A4 PDF, editable ``.docx``, raw ``evidence.zip``, Teacher Evidence Packet PDF,
  ``report_preview``, and ``lab_runs(action="verify").verification.ok == True``.

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
WIRE_SWF = LOCAL_ASSETS_DIR / "sims" / "m96210.swf"
LOOP_SWF = LOCAL_ASSETS_DIR / "sims" / "m96211.swf"
BENCHMARK_JSON = LOCAL_ASSETS_DIR / "benchmarks" / "lab4-measurements.json"

MU0_REF = 4.0 * math.pi * 1e-7
TEAM2_CURRENTS = [-5, -10, -15, -20]
TEAM2_DISTANCES_CM = [2, 3, 4, 5, 6, 7, 8, 9, 10]
LOOP_RADIUS_CM = 3.0


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


def _to_microtesla(value: float, unit: str | None) -> float:
    """Convert an OCR-extracted magnetic field reading to microtesla using its unit scale."""
    u = (unit or "").strip().lower()
    if u in ("мтл", "mt"):
        return value * 1000.0
    if u in ("тл", "t"):
        return value * 1e6
    return value


def _fmt_signed_pct(value: float, decimals: int = 3) -> str:
    """Format a signed percentage without emitting negative zero (``-0.000%``)."""
    if abs(value) < 0.5 * (10.0 ** (-decimals)):
        return f"{0.0:.{decimals}f}%"
    return f"{value:+.{decimals}f}%"


def _build_m96210_profile(swf_sha256: str) -> dict[str, Any]:
    """Calibration profile for ``m96210.swf`` (500x450 straight wire, 39 px top bar)."""
    return {
        "schema_version": 1,
        "swf_sha256": swf_sha256,
        "stage_width": 500,
        "stage_height": 450,
        "header_height_px": 39,
        "blur_xy": [250, 18],
        "controls": {
            "I": {
                "name": "I",
                "kind": "stepper",
                "input_xy": [68, 424],
                "up_xy": [103, 418],
                "down_xy": [103, 431],
                "readout_roi_px": [44, 412, 92, 437],
                "unit": "А",
                "step": 1.0,
                "min_val": -20.0,
                "max_val": 20.0,
                "decimal_comma": False,
                "commit_mode": "step_interior",
            },
            "r": {
                "name": "r",
                "kind": "stepper",
                "input_xy": [69, 457],
                "up_xy": [104, 451],
                "down_xy": [104, 464],
                "readout_roi_px": [45, 445, 93, 470],
                "unit": "см",
                "step": 0.01,
                "min_val": 1.0,
                "max_val": 10.0,
                "decimal_comma": False,
                "commit_mode": "step_interior",
            },
        },
        "readouts": {
            "B": {
                "name": "B",
                "roi_px": [150, 443, 286, 475],
                "ocr_mode": "det_rec",
                "pad_px": 24,
                "scale": 2.0,
                "parse_mode": "float",
                "unit_pattern": r"(?:мкТл|мТл|Тл)",
                "normalize_homoglyphs": True,
            }
        },
        "notes": "m96210.swf (Straight wire magnetic field) live calibration profile",
    }


def _build_m96211_profile(swf_sha256: str) -> dict[str, Any]:
    """Calibration profile for ``m96211.swf`` (700x500 circular loop, 39 px top bar)."""
    return {
        "schema_version": 1,
        "swf_sha256": swf_sha256,
        "stage_width": 700,
        "stage_height": 500,
        "header_height_px": 39,
        "blur_xy": [350, 18],
        "controls": {
            "x": {
                "name": "x",
                "kind": "stepper",
                "input_xy": [66, 478],
                "up_xy": [98, 472],
                "down_xy": [98, 485],
                "readout_roi_px": [44, 465, 88, 492],
                "unit": "см",
                "step": 0.1,
                "min_val": -20.0,
                "max_val": 20.0,
                "decimal_comma": False,
                "commit_mode": "step_interior",
            },
            "R": {
                "name": "R",
                "kind": "stepper",
                "input_xy": [195, 478],
                "up_xy": [225, 472],
                "down_xy": [225, 485],
                "readout_roi_px": [176, 465, 215, 492],
                "unit": "см",
                "step": 0.1,
                "min_val": 1.0,
                "max_val": 10.0,
                "decimal_comma": False,
                "commit_mode": "step_interior",
            },
            "I": {
                "name": "I",
                "kind": "stepper",
                "input_xy": [313, 478],
                "up_xy": [338, 472],
                "down_xy": [338, 485],
                "readout_roi_px": [298, 465, 328, 492],
                "unit": "А",
                "step": 1.0,
                "min_val": -20.0,
                "max_val": 20.0,
                "decimal_comma": False,
                "commit_mode": "step_interior",
            },
        },
        "readouts": {
            "Bx": {
                "name": "Bx",
                "roi_px": [546, 456, 696, 486],
                "ocr_mode": "det_rec",
                "pad_px": 24,
                "scale": 2.0,
                "parse_mode": "float",
                "unit_pattern": r"(?:мкТл|мТл|Тл)",
                "normalize_homoglyphs": True,
            },
            "By": {
                "name": "By",
                "roi_px": [546, 481, 696, 510],
                "ocr_mode": "det_rec",
                "pad_px": 24,
                "scale": 2.0,
                "parse_mode": "float",
                "unit_pattern": r"(?:мкТл|мТл|Тл)",
                "normalize_homoglyphs": True,
            },
        },
        "notes": "m96211.swf (Circular loop magnetic field) live calibration profile",
    }


@pytest.mark.live_sim
@pytest.mark.asyncio
async def test_lab4_magnetic_field_live_e2e_over_mcp_stdio(tmp_path: Path) -> None:
    if not WIRE_SWF.is_file() or not LOOP_SWF.is_file() or not BENCHMARK_JSON.is_file():
        pytest.skip("Lab 4 local benchmark assets not staged under local_assets/")
    browser_exe = resolve_browser_executable()
    if browser_exe is None:
        pytest.skip("Playwright Chromium executable not cached locally")

    benchmark = json.loads(BENCHMARK_JSON.read_text(encoding="utf-8"))
    ref_wire: dict[tuple[int, int], float] = {
        (int(row["currentA"]), int(row["distanceCm"])): float(row["fieldMagnitudeMicroT"])
        for row in benchmark["readings"]
        if row["model"] == "wire"
    }
    ref_loop: dict[tuple[int, int], tuple[float, float, float]] = {
        (int(row["currentA"]), int(row["distanceCm"])): (
            float(row["displayedBxMicroT"]),
            float(row["displayedByMicroT"]),
            float(row["fieldMagnitudeMicroT"]),
        )
        for row in benchmark["readings"]
        if row["model"] == "loop"
    }
    assert len(ref_wire) == 36
    assert len(ref_loop) == 36

    runs_dir, profiles_dir = _resolve_e2e_dirs(tmp_path, "lab4")
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

            created = _parse_tool_json(
                await session.call_tool(
                    "lab_runs",
                    {
                        "action": "create",
                        "label": "Lab 4 — Magnetic Field of Straight Wire & Circular Loop (Team 2)",
                        "source": "simulator_captured",
                    },
                )
            )
            run_id = created["run_id"]
            run_root = runs_dir / run_id
            saved_profile_relpaths: list[str] = []

            # ------------------------------------------------------------------
            # 1. Straight Wire (m96210.swf, 500x450): 36 live readings
            # ------------------------------------------------------------------
            w_open = _parse_tool_json(
                await session.call_tool(
                    "sim_open",
                    {
                        "swf_path": "local_assets/sims/m96210.swf",
                        "run_id": run_id,
                        "settle_ms": 1400,
                    },
                )
            )
            w_sid = w_open["session_id"]
            assert w_open["stage_width"] == 500
            assert w_open["stage_height"] == 450
            assert w_open["page_errors"] == []
            assert w_open["aborted_requests"] == []

            try:
                w_obs = _parse_tool_json(
                    await session.call_tool(
                        "sim_observe",
                        {"session_id": w_sid, "annotate": True, "blur_first": True},
                    )
                )
                assert w_obs["annotated_relpath"] is not None

                w_prof = _parse_tool_json(
                    await session.call_tool(
                        "sim_save_profile",
                        {
                            "session_id": w_sid,
                            "profile": _build_m96210_profile(w_open["swf_meta"]["sha256"]),
                            "verify_live": True,
                        },
                    )
                )
                assert w_prof["verified_live"] is True
                saved_profile_relpaths.append(w_prof["saved_relpath"])

                w_sweep = _parse_tool_json(
                    await session.call_tool(
                        "sim_sweep",
                        {
                            "session_id": w_sid,
                            "spec": {
                                "axes": [
                                    {"control": "I", "values": TEAM2_CURRENTS},
                                    {"control": "r", "values": TEAM2_DISTANCES_CM},
                                ],
                                "readouts": ["B"],
                                "settle_ms": 140,
                                "verify_controls": True,
                                "blur_before_capture": True,
                            },
                        },
                    )
                )
            finally:
                w_closed = _parse_tool_json(
                    await session.call_tool("sim_close", {"session_id": w_sid})
                )
                assert w_closed["closed"] is True

            assert w_sweep["status"] == "complete"
            assert w_sweep["captured_points"] == 36
            assert w_sweep["controls_matched_all"] is True
            assert w_sweep["missing_readouts"] == 0

            wire_rows: list[dict[str, Any]] = []
            wire_exact_matches = 0
            for pt in w_sweep["points"]:
                cur_a = int(round(float(pt["requested_settings"]["I"])))
                dist_cm = int(round(float(pt["requested_settings"]["r"])))
                b_uT = _to_microtesla(float(pt["readouts"]["B"]), pt["parsed_unit"])
                expected_uT = ref_wire[(cur_a, dist_cm)]
                assert b_uT == pytest.approx(expected_uT, abs=1e-6), (
                    f"Wire mismatch at I={cur_a} A, r={dist_cm} cm: "
                    f"live={b_uT} vs ref={expected_uT} (raw={pt['raw_ocr_text']!r})"
                )
                wire_exact_matches += 1
                wire_rows.append(
                    {
                        "I_A": float(cur_a),
                        "I_abs_A": float(abs(cur_a)),
                        "I_group": f"I = {cur_a} A",
                        "r_cm": float(dist_cm),
                        "r_m": dist_cm / 100.0,
                        "B_uT": b_uT,
                    }
                )
            assert wire_exact_matches == 36

            # ------------------------------------------------------------------
            # 2. Circular Loop (m96211.swf, 700x500): 36 live readings (Bx, By)
            # ------------------------------------------------------------------
            l_open = _parse_tool_json(
                await session.call_tool(
                    "sim_open",
                    {
                        "swf_path": "local_assets/sims/m96211.swf",
                        "run_id": run_id,
                        "settle_ms": 1400,
                    },
                )
            )
            l_sid = l_open["session_id"]
            assert l_open["stage_width"] == 700
            assert l_open["stage_height"] == 500
            assert l_open["page_errors"] == []
            assert l_open["aborted_requests"] == []

            try:
                l_obs = _parse_tool_json(
                    await session.call_tool(
                        "sim_observe",
                        {"session_id": l_sid, "annotate": True, "blur_first": True},
                    )
                )
                assert l_obs["annotated_relpath"] is not None

                l_prof = _parse_tool_json(
                    await session.call_tool(
                        "sim_save_profile",
                        {
                            "session_id": l_sid,
                            "profile": _build_m96211_profile(l_open["swf_meta"]["sha256"]),
                            "verify_live": True,
                        },
                    )
                )
                assert l_prof["verified_live"] is True
                saved_profile_relpaths.append(l_prof["saved_relpath"])

                l_sweep = _parse_tool_json(
                    await session.call_tool(
                        "sim_sweep",
                        {
                            "session_id": l_sid,
                            "spec": {
                                "fixed_controls": {"R": LOOP_RADIUS_CM},
                                "axes": [
                                    {"control": "I", "values": TEAM2_CURRENTS},
                                    {"control": "x", "values": TEAM2_DISTANCES_CM},
                                ],
                                "readouts": ["Bx", "By"],
                                "settle_ms": 140,
                                "verify_controls": True,
                                "blur_before_capture": True,
                            },
                        },
                    )
                )
            finally:
                l_closed = _parse_tool_json(
                    await session.call_tool("sim_close", {"session_id": l_sid})
                )
                assert l_closed["closed"] is True

            assert l_sweep["status"] == "complete"
            assert l_sweep["captured_points"] == 36
            assert l_sweep["controls_matched_all"] is True
            assert l_sweep["missing_readouts"] == 0

            loop_rows: list[dict[str, Any]] = []
            loop_exact_matches = 0
            for pt in l_sweep["points"]:
                cur_a = int(round(float(pt["requested_settings"]["I"])))
                dist_cm = int(round(float(pt["requested_settings"]["x"])))
                ro_units = pt.get("readout_units") or {}
                bx_uT = _to_microtesla(
                    float(pt["readouts"]["Bx"]), ro_units.get("Bx") or pt["parsed_unit"]
                )
                by_uT = _to_microtesla(float(pt["readouts"]["By"]), ro_units.get("By"))
                exp_bx, exp_by, _exp_mag = ref_loop[(cur_a, dist_cm)]
                assert bx_uT == pytest.approx(exp_bx, abs=1e-6), (
                    f"Loop Bx mismatch at I={cur_a} A, r={dist_cm} cm: "
                    f"live={bx_uT} vs ref={exp_bx} (raw={pt['raw_readouts']!r})"
                )
                assert by_uT == pytest.approx(0.0, abs=1e-9), (
                    f"Loop By expected 0.0 at I={cur_a} A, r={dist_cm} cm; got {by_uT}"
                )
                assert by_uT == pytest.approx(exp_by, abs=1e-9)
                loop_exact_matches += 1
                loop_rows.append(
                    {
                        "I_A": float(cur_a),
                        "I_abs_A": float(abs(cur_a)),
                        "I_group": f"I = {cur_a} A",
                        "R_cm": LOOP_RADIUS_CM,
                        "R_m": LOOP_RADIUS_CM / 100.0,
                        "r_cm": float(dist_cm),
                        "r_m": dist_cm / 100.0,
                        "Bx_uT": bx_uT,
                        "By_uT": by_uT,
                    }
                )
            assert loop_exact_matches == 36
            assert wire_exact_matches + loop_exact_matches == 72

            # ------------------------------------------------------------------
            # 3. Deterministic Analysis: B = sqrt(Bx^2 + By^2), Linearized OLS, mu_0
            # ------------------------------------------------------------------
            _parse_tool_json(
                await session.call_tool(
                    "data_ingest",
                    {
                        "run_id": run_id,
                        "source": "simulator_captured",
                        "dataset_id": "lab4_wire_raw",
                        "rows": wire_rows,
                        "units": {"I_A": "A", "r_cm": "cm", "r_m": "m", "B_uT": "uT"},
                        "notes": "m96210.swf 36 live wire readings (Team 2)",
                    },
                )
            )
            wire_analysis = _parse_tool_json(
                await session.call_tool(
                    "data_analyze",
                    {
                        "run_id": run_id,
                        "dataset_id": "lab4_wire_raw",
                        "derived_columns": [
                            {"name": "B_T", "formula": "B_uT * 1e-6", "unit": "T"},
                            {"name": "inv_r", "formula": "1 / r_m", "unit": "m^-1"},
                        ],
                        "regressions": [
                            {
                                "name": "wire_B_vs_inv_r",
                                "x_column": "inv_r",
                                "y_column": "B_T",
                                "intercept": "zero",
                                "group_by": "I_group",
                            }
                        ],
                        "persist_derived_id": "lab4_wire_derived",
                    },
                )
            )

            _parse_tool_json(
                await session.call_tool(
                    "data_ingest",
                    {
                        "run_id": run_id,
                        "source": "simulator_captured",
                        "dataset_id": "lab4_loop_raw",
                        "rows": loop_rows,
                        "units": {
                            "I_A": "A",
                            "R_cm": "cm",
                            "R_m": "m",
                            "r_cm": "cm",
                            "r_m": "m",
                            "Bx_uT": "uT",
                            "By_uT": "uT",
                        },
                        "notes": "m96211.swf 36 live circular loop readings (Bx, By; Team 2)",
                    },
                )
            )
            loop_analysis = _parse_tool_json(
                await session.call_tool(
                    "data_analyze",
                    {
                        "run_id": run_id,
                        "dataset_id": "lab4_loop_raw",
                        "derived_columns": [
                            {
                                "name": "B_uT",
                                "formula": "sqrt(Bx_uT**2 + By_uT**2)",
                                "unit": "uT",
                            },
                            {"name": "B_T", "formula": "B_uT * 1e-6", "unit": "T"},
                            {
                                "name": "geom_factor",
                                "formula": "(R_m**2 + r_m**2)**(-1.5)",
                                "unit": "m^-3",
                            },
                        ],
                        "regressions": [
                            {
                                "name": "loop_B_vs_geom",
                                "x_column": "geom_factor",
                                "y_column": "B_T",
                                "intercept": "zero",
                                "group_by": "I_group",
                            }
                        ],
                        "persist_derived_id": "lab4_loop_derived",
                    },
                )
            )

            # Verify derived B = sqrt(Bx^2 + By^2) matches all 36 benchmark magnitudes
            loop_derived_rows = json.loads(
                (run_root / "datasets" / "lab4_loop_derived.json").read_text(encoding="utf-8")
            )["rows"]
            for row in loop_derived_rows:
                key = (int(round(row["I_A"])), int(round(row["r_cm"])))
                assert float(row["B_uT"]) == pytest.approx(ref_loop[key][2], abs=1e-6)

            # Recover mu_0 for every current in both wire & loop via calc_eval (< 0.5% dev)
            calc_ids: list[str] = []
            wire_reg_by_group = {r["group"]: r for r in wire_analysis["regressions"]}
            loop_reg_by_group = {r["group"]: r for r in loop_analysis["regressions"]}

            wire_mu0_rows: list[dict[str, Any]] = []
            loop_mu0_rows: list[dict[str, Any]] = []
            mu0_comparisons: list[dict[str, Any]] = []

            for cur_a in TEAM2_CURRENTS:
                grp = f"I = {cur_a} A"
                w_reg = wire_reg_by_group[grp]
                assert w_reg["r_squared"] > 0.9999
                w_mu0_calc = _parse_tool_json(
                    await session.call_tool(
                        "calc_eval",
                        {
                            "expression": "2 * pi * slope / I_abs",
                            "variables": {
                                "slope": {
                                    "value": float(w_reg["slope"]),
                                    "unit": "T*m",
                                    "uncertainty": float(w_reg["slope_se"]),
                                },
                                "I_abs": {"value": float(abs(cur_a)), "unit": "A"},
                            },
                            "output_unit": "T*m/A",
                            "label": f"Wire mu_0 at {grp}",
                            "run_id": run_id,
                        },
                    )
                )
                w_mu0 = float(w_mu0_calc["output_value"])
                w_dev = (w_mu0 - MU0_REF) / MU0_REF * 100.0
                assert abs(w_dev) < 0.5, f"Wire mu_0 dev {w_dev:.3f}% >= 0.5% at {grp}"
                calc_ids.append(w_mu0_calc["calc_id"])
                wire_mu0_rows.append(
                    {
                        "current": cur_a,
                        "slope": float(w_reg["slope"]),
                        "slope_se": float(w_reg["slope_se"]),
                        "r_squared": float(w_reg["r_squared"]),
                        "mu0": w_mu0,
                        "dev_pct": w_dev,
                    }
                )
                mu0_comparisons.append(
                    {
                        "name": f"wire_mu0_{cur_a}A",
                        "measured": w_mu0,
                        "reference": MU0_REF,
                        "unit": "T*m/A",
                    }
                )

                l_reg = loop_reg_by_group[grp]
                assert l_reg["r_squared"] > 0.9999
                l_mu0_calc = _parse_tool_json(
                    await session.call_tool(
                        "calc_eval",
                        {
                            "expression": "2 * slope / (I_abs * R**2)",
                            "variables": {
                                "slope": {
                                    "value": float(l_reg["slope"]),
                                    "unit": "T*m^3",
                                    "uncertainty": float(l_reg["slope_se"]),
                                },
                                "I_abs": {"value": float(abs(cur_a)), "unit": "A"},
                                "R": {"value": LOOP_RADIUS_CM, "unit": "cm"},
                            },
                            "output_unit": "T*m/A",
                            "label": f"Loop mu_0 at {grp}",
                            "run_id": run_id,
                        },
                    )
                )
                l_mu0 = float(l_mu0_calc["output_value"])
                l_dev = (l_mu0 - MU0_REF) / MU0_REF * 100.0
                assert abs(l_dev) < 0.5, f"Loop mu_0 dev {l_dev:.3f}% >= 0.5% at {grp}"
                calc_ids.append(l_mu0_calc["calc_id"])
                loop_mu0_rows.append(
                    {
                        "current": cur_a,
                        "slope": float(l_reg["slope"]),
                        "slope_se": float(l_reg["slope_se"]),
                        "r_squared": float(l_reg["r_squared"]),
                        "mu0": l_mu0,
                        "dev_pct": l_dev,
                    }
                )
                mu0_comparisons.append(
                    {
                        "name": f"loop_mu0_{cur_a}A",
                        "measured": l_mu0,
                        "reference": MU0_REF,
                        "unit": "T*m/A",
                    }
                )

            # Confirm all 8 mu_0 deviations via data_analyze comparisons
            cmp_res = _parse_tool_json(
                await session.call_tool(
                    "data_analyze",
                    {
                        "run_id": run_id,
                        "dataset_id": "lab4_wire_derived",
                        "comparisons": mu0_comparisons,
                    },
                )
            )
            assert len(cmp_res["comparisons"]) == 8
            for cmp_item in cmp_res["comparisons"]:
                assert abs(float(cmp_item["percent_deviation"])) < 0.5

            mean_wire_mu0 = sum(r["mu0"] for r in wire_mu0_rows) / len(wire_mu0_rows)
            mean_loop_mu0 = sum(r["mu0"] for r in loop_mu0_rows) / len(loop_mu0_rows)
            mean_wire_dev = (mean_wire_mu0 - MU0_REF) / MU0_REF * 100.0
            mean_loop_dev = (mean_loop_mu0 - MU0_REF) / MU0_REF * 100.0

            # ------------------------------------------------------------------
            # 4. Vector Visuals: Linearized Wire & Loop Plots + Field Diagram
            # ------------------------------------------------------------------
            colors = {-5: "#1f4e79", -10: "#c0392b", -15: "#2e7d32", -20: "#d97706"}
            markers = {-5: "o", -10: "s", -15: "^", -20: "D"}

            wire_derived_rows = json.loads(
                (run_root / "datasets" / "lab4_wire_derived.json").read_text(encoding="utf-8")
            )["rows"]

            wire_series: list[dict[str, Any]] = []
            for cur_a in TEAM2_CURRENTS:
                pts = sorted(
                    (float(r["inv_r"]), float(r["B_uT"]))
                    for r in wire_derived_rows
                    if int(round(r["I_A"])) == cur_a
                )
                slope_uT_m = wire_reg_by_group[f"I = {cur_a} A"]["slope"] * 1e6
                wire_series.append(
                    {
                        "label": f"I = {cur_a} A (slope = {slope_uT_m:.3f} µT·m)",
                        "x": [p[0] for p in pts],
                        "y": [p[1] for p in pts],
                        "kind": "scatter",
                        "color": colors[cur_a],
                        "marker": markers[cur_a],
                    }
                )
                x_max = max(p[0] for p in pts)
                wire_series.append(
                    {
                        "label": "",
                        "x": [0.0, x_max],
                        "y": [0.0, slope_uT_m * x_max],
                        "kind": "fit_line",
                        "color": colors[cur_a],
                        "linestyle": "--",
                    }
                )

            fig_wire = _parse_tool_json(
                await session.call_tool(
                    "plot_render",
                    {
                        "run_id": run_id,
                        "source": "simulator_captured",
                        "spec": {
                            "plot_id": "lab4_fig1_wire_linearized",
                            "title": "Straight Wire: Linearized Magnetic Field B vs 1/r",
                            "x_label": "Inverse distance 1/r (m⁻¹)",
                            "y_label": "Magnetic field magnitude B (µT)",
                            "series": wire_series,
                            "annotations": [
                                f"Mean μ₀ = {mean_wire_mu0 * 1e7:.4f} × 10⁻⁷ T·m/A",
                                f"Deviation vs 4π×10⁻⁷: {_fmt_signed_pct(mean_wire_dev)}",
                            ],
                        },
                    },
                )
            )

            loop_series: list[dict[str, Any]] = []
            for cur_a in TEAM2_CURRENTS:
                pts = sorted(
                    (float(r["geom_factor"]), float(r["B_uT"]))
                    for r in loop_derived_rows
                    if int(round(r["I_A"])) == cur_a
                )
                slope_uT_m3 = loop_reg_by_group[f"I = {cur_a} A"]["slope"] * 1e6
                loop_series.append(
                    {
                        "label": f"I = {cur_a} A (slope = {slope_uT_m3:.4f} µT·m³)",
                        "x": [p[0] for p in pts],
                        "y": [p[1] for p in pts],
                        "kind": "scatter",
                        "color": colors[cur_a],
                        "marker": markers[cur_a],
                    }
                )
                x_max = max(p[0] for p in pts)
                loop_series.append(
                    {
                        "label": "",
                        "x": [0.0, x_max],
                        "y": [0.0, slope_uT_m3 * x_max],
                        "kind": "fit_line",
                        "color": colors[cur_a],
                        "linestyle": "--",
                    }
                )

            fig_loop = _parse_tool_json(
                await session.call_tool(
                    "plot_render",
                    {
                        "run_id": run_id,
                        "source": "simulator_captured",
                        "spec": {
                            "plot_id": "lab4_fig2_loop_linearized",
                            "title": (
                                "Circular Loop (R = 3.0 cm): Linearized Field B vs (R² + r²)⁻³/²"
                            ),
                            "x_label": "Geometric factor (R² + r²)⁻³/² (m⁻³)",
                            "y_label": "Magnetic field magnitude B = √(Bx² + By²) (µT)",
                            "series": loop_series,
                            "annotations": [
                                f"Mean μ₀ = {mean_loop_mu0 * 1e7:.4f} × 10⁻⁷ T·m/A",
                                f"Deviation vs 4π×10⁻⁷: {_fmt_signed_pct(mean_loop_dev)}",
                            ],
                        },
                    },
                )
            )

            fig_field = _parse_tool_json(
                await session.call_tool(
                    "diagram_render",
                    {
                        "run_id": run_id,
                        "source": "model_computed",
                        "spec": {
                            "diagram_id": "lab4_fig3_magnetic_field_geometry",
                            "kind": "field",
                            "title": (
                                "Magnetic Field Geometry: Straight Wire (Left) "
                                "and Axial Loop (Right)"
                            ),
                            "elements": [
                                {
                                    "kind": "wire_in",
                                    "at": [-2.8, 0.0],
                                    "length": 0.28,
                                    "style": {"color": "#c0392b"},
                                },
                                {
                                    "kind": "label",
                                    "label": "$I < 0$ (into page)",
                                    "at": [-2.8, 1.75],
                                    "style": {
                                        "color": "#c0392b",
                                        "ha": "center",
                                        "va": "bottom",
                                    },
                                },
                                {
                                    "kind": "field_circle",
                                    "label": r"$\vec{B}$ (CW)",
                                    "at": [-2.8, 0.0],
                                    "length": 1.35,
                                    "style": {
                                        "color": "#1f4e79",
                                        "sense": "cw",
                                        "arrow_angles_deg": [0, 90, 180, 270],
                                    },
                                },
                                {
                                    "kind": "vector",
                                    "label": "$r$",
                                    "at": [-2.8, -0.34],
                                    "to": [-2.8, -1.35],
                                    "style": {
                                        "color": "#334155",
                                        "lw": 1.6,
                                        "label_pos": "mid",
                                        "label_offset": [0.28, 0.0],
                                        "ha": "left",
                                    },
                                },
                                {
                                    "kind": "vector",
                                    "label": r"$\vec{B}(r)$",
                                    "at": [-2.8, -1.35],
                                    "to": [-4.25, -1.35],
                                    "style": {
                                        "color": "#2e7d32",
                                        "lw": 2.2,
                                        "label_offset": [0.0, -0.42],
                                        "ha": "center",
                                        "va": "top",
                                    },
                                },
                                {
                                    "kind": "wire",
                                    "at": [1.2, -1.25],
                                    "to": [1.2, 1.25],
                                    "style": {"color": "#0f172a", "lw": 3.0},
                                },
                                {
                                    "kind": "label",
                                    "label": "Loop ($R = 3$ cm, $I < 0$)",
                                    "at": [1.2, 1.75],
                                    "style": {
                                        "color": "#0f172a",
                                        "ha": "center",
                                        "va": "bottom",
                                    },
                                },
                                {
                                    "kind": "wire_in",
                                    "at": [1.2, 1.25],
                                    "length": 0.24,
                                    "style": {"color": "#c0392b"},
                                },
                                {
                                    "kind": "wire_out",
                                    "at": [1.2, -1.25],
                                    "length": 0.24,
                                    "style": {"color": "#c0392b"},
                                },
                                {
                                    "kind": "line",
                                    "at": [0.2, 0.0],
                                    "to": [4.8, 0.0],
                                    "style": {"color": "#64748b", "ls": "--", "lw": 1.3},
                                },
                                {
                                    "kind": "label",
                                    "label": "Axis $x = r$",
                                    "at": [4.75, -0.42],
                                    "style": {
                                        "color": "#64748b",
                                        "ha": "right",
                                        "va": "top",
                                    },
                                },
                                {
                                    "kind": "vector",
                                    "label": r"$\vec{B}_x(r)$",
                                    "at": [3.55, 0.0],
                                    "to": [2.05, 0.0],
                                    "style": {
                                        "color": "#1f4e79",
                                        "lw": 2.4,
                                        "label_pos": "mid",
                                        "label_offset": [0.0, 0.42],
                                        "ha": "center",
                                        "va": "bottom",
                                    },
                                },
                                {
                                    "kind": "point",
                                    "label": "$P(r, 0)$",
                                    "at": [3.55, 0.0],
                                    "style": {
                                        "color": "#d97706",
                                        "label_offset": [0.18, 0.28],
                                        "ha": "left",
                                        "va": "bottom",
                                    },
                                },
                            ],
                            "notes": [
                                "Left: straight wire B = μ₀|I|/(2πr)  |  "
                                "Right: axial loop (I < 0) Bₓ = μ₀IR²/[2(R²+r²)³/²], Bᵧ = 0"
                            ],
                        },
                    },
                )
            )

            # ------------------------------------------------------------------
            # 5. Handout-Compliant Tables 2 & 3 (with Transformed Rows) + Summary
            # ------------------------------------------------------------------
            wire_lookup = {
                (int(round(r["I_A"])), int(round(r["r_cm"]))): r for r in wire_derived_rows
            }
            loop_lookup = {
                (int(round(r["I_A"])), int(round(r["r_cm"]))): r for r in loop_derived_rows
            }

            table2_wire = {
                "table_id": "lab4_table2_wire",
                "caption": (
                    "Straight Wire (m96210.swf; Handout Table 2): "
                    "Live B(r) Measurements and Transformed 1/r Row"
                ),
                "headers": ["Quantity"] + [f"r = {d} cm" for d in TEAM2_DISTANCES_CM],
                "rows": [
                    ["1/r, m⁻¹"] + [f"{100.0 / d:.2f}" for d in TEAM2_DISTANCES_CM],
                ]
                + [
                    [f"B (I = {cur} A), µT"]
                    + [f"{wire_lookup[(cur, d)]['B_uT']:.1f}" for d in TEAM2_DISTANCES_CM]
                    for cur in TEAM2_CURRENTS
                ],
                "notes": (
                    "All 36 readings captured live from m96210.swf via step_interior commit mode "
                    "and offline RapidOCR (36/36 exact repeatability match with pilot benchmark)."
                ),
                "source": "simulator_captured",
            }

            table3_loop = {
                "table_id": "lab4_table3_loop",
                "caption": (
                    "Circular Loop (m96211.swf, R = 3.0 cm; Handout Table 3): "
                    "Live B = √(Bx²+By²) and Transformed (R² + r²)⁻³/² Row"
                ),
                "headers": ["Quantity"] + [f"r = {d} cm" for d in TEAM2_DISTANCES_CM],
                "rows": [
                    ["(R²+r²)⁻³/², 10³ m⁻³"]
                    + [
                        f"{((0.03**2 + (d / 100.0) ** 2) ** (-1.5)) / 1000.0:.2f}"
                        for d in TEAM2_DISTANCES_CM
                    ],
                ]
                + [
                    [f"B (I = {cur} A), µT"]
                    + [f"{loop_lookup[(cur, d)]['B_uT']:.2f}" for d in TEAM2_DISTANCES_CM]
                    for cur in TEAM2_CURRENTS
                ],
                "notes": (
                    "All 36 readings captured live from m96211.swf reading unclipped Bx and By "
                    "(By = 0.00 µT across all 36 points) and computing B = √(Bx²+By²) "
                    "via data_analyze."
                ),
                "source": "simulator_captured",
            }

            table4_mu0 = {
                "table_id": "lab4_table4_mu0_recovery",
                "caption": "Zero-Intercept Linearized Slopes and Permeability μ₀ Recovery",
                "headers": [
                    "Geometry",
                    "Current I (A)",
                    "OLS Slope",
                    "R²",
                    "Recovered μ₀ (10⁻⁷ T·m/A)",
                    "Deviation vs 4π×10⁻⁷",
                ],
                "rows": [
                    [
                        "Straight wire",
                        f"{row['current']}",
                        f"{row['slope'] * 1e6:.4f} µT·m",
                        f"{row['r_squared']:.6f}",
                        f"{row['mu0'] * 1e7:.4f}",
                        _fmt_signed_pct(row["dev_pct"]),
                    ]
                    for row in wire_mu0_rows
                ]
                + [
                    [
                        "Circular loop",
                        f"{row['current']}",
                        f"{row['slope'] * 1e6:.5f} µT·m³",
                        f"{row['r_squared']:.6f}",
                        f"{row['mu0'] * 1e7:.4f}",
                        _fmt_signed_pct(row["dev_pct"]),
                    ]
                    for row in loop_mu0_rows
                ],
                "notes": (
                    "Reference vacuum permeability μ₀ = 4π × 10⁻⁷ ≈ 12.5664 × 10⁻⁷ T·m/A. "
                    "Every recovered μ₀ value deviates by less than 0.5%."
                ),
                "source": "model_computed",
            }

            report_spec = {
                "title": "Laboratory Work 4: Magnetic Field of a Straight Wire and Circular Loop",
                "subtitle": (
                    "Live Dual-SWF Ruffle 0.7.1 Verification & "
                    "Biot–Savart Linearization (Team 2)"
                ),
                "lab_number": "4",
                "language": "en",
                "author": "PEA v2 Automated Benchmark",
                "team": "Team 2 (I ∈ {-5, -10, -15, -20} A, r = 2..10 cm, R = 3.0 cm)",
                "course": "General Physics — Electricity & Magnetism",
                "date_str": "2026-11-22",
                "objective": (
                    "Measure the spatial dependence of the magnetic induction B produced by an "
                    "infinitely long straight current-carrying wire (m96210.swf) and along the "
                    "axis of a circular current loop (m96211.swf), verify the Biot–Savart "
                    "scaling laws via linearized zero-intercept regression, and recover the "
                    "vacuum magnetic permeability μ₀."
                ),
                "apparatus": [
                    (
                        "Sandboxed m96210.swf straight-wire simulator "
                        "(500×450 native stage in Ruffle 0.7.1)"
                    ),
                    (
                        "Sandboxed m96211.swf circular-loop simulator "
                        "(700×500 native stage in Ruffle 0.7.1)"
                    ),
                    "Boundary-safe step_interior Flex numeric stepper controller with OCR readback",
                    (
                        "Offline RapidOCR ONNX backend with Cyrillic/Latin homoglyph "
                        "& unit-scale parser"
                    ),
                ],
                "theory_sections": [
                    {
                        "heading": "Biot–Savart Law and Linearized Coordinates",
                        "body": (
                            "For an infinitely long straight conductor carrying current $I$, the "
                            "magnetic induction magnitude decreases inversely with radial distance "
                            "$r$. Along the symmetry axis of a circular loop of radius $R$, the "
                            "transverse component vanishes ($B_y = 0$) and the axial magnitude "
                            "scales linearly with $(R^2 + r^2)^{-3/2}$."
                        ),
                        "equation": (
                            r"B_{\text{wire}}(r) = \frac{\mu_0 |I|}{2\pi}\,\frac{1}{r}, \qquad "
                            r"B_{\text{loop}}(r) = \frac{\mu_0 |I| R^2}{2}\,"
                            r"\left(R^2 + r^2\right)^{-3/2}"
                        ),
                    }
                ],
                "procedure": [
                    (
                        "Open m96210.swf (500×450), inspect and save its SHA-256 calibration "
                        "profile using decimal_comma=False and commit_mode='step_interior'."
                    ),
                    (
                        "Execute a 4 × 9 grid sweep across I ∈ {-5, -10, -15, -20} A and "
                        "r ∈ {2..10} cm with live control verification (36 readings)."
                    ),
                    (
                        "Open m96211.swf (700×500), fix loop radius R = 3.0 cm, and sweep "
                        "the same 4 × 9 grid capturing unclipped Bx and By (36 readings)."
                    ),
                    (
                        "Derive B = √(Bx² + By²), 1/r, and (R² + r²)⁻³/² via data_analyze, "
                        "fit zero-intercept OLS slopes, and recover μ₀ via calc_eval."
                    ),
                ],
                "tables": [table2_wire, table3_loop, table4_mu0],
                "figures": [fig_wire, fig_loop, fig_field],
                "analysis_paragraphs": [
                    (
                        "All 72 live simulator readings (36 straight-wire points from m96210.swf "
                        "and 36 circular-loop vector-component points from m96211.swf with "
                        "$B_y = 0.00\\,\\mu\\text{T}$ everywhere) passed control-readback checks "
                        "and matched the recorded 72-point automated pilot dataset 72/72 exactly."
                    ),
                    (
                        "Zero-intercept linearized regressions achieved $R^2 > 0.9999$ for all "
                        "eight curves. Mean recovered permeability is "
                        f"$\\mu_{{0,\\text{{wire}}}} = {mean_wire_mu0 * 1e7:.4f} "
                        f"\\times 10^{{-7}}$ T·m/A ({_fmt_signed_pct(mean_wire_dev)}) for the "
                        f"straight wire and $\\mu_{{0,\\text{{loop}}}} = {mean_loop_mu0 * 1e7:.4f} "
                        f"\\times 10^{{-7}}$ T·m/A ({_fmt_signed_pct(mean_loop_dev)}) for the "
                        "circular loop, with every individual current curve within $< 0.5\\%$ of "
                        "$\\mu_0 = 4\\pi \\times 10^{-7}$ T·m/A."
                    ),
                    (
                        "Scientific Honesty & Validation Status: All data in Handout Tables 2 "
                        "and 3 (Tables 1 and 2 above) are tagged simulator_captured (virtual SWF "
                        "readouts, not physical bench hall-probe measurements). The 72/72 "
                        "agreement against lab4-measurements.json proves deterministic "
                        "repeatability against the earlier automated Ruffle pilot; independent "
                        "manual human reference verification (manualReference72Passed = False) "
                        "and original Adobe Flash Player equivalence remain pending and are not "
                        "claimed automatically."
                    ),
                ],
                "conclusions": [
                    (
                        "The exact same zero-hardcoded pea-mcp server automated both "
                        "m96210.swf (500×450) and m96211.swf (700×500) using SHA-256 "
                        "calibration profiles."
                    ),
                    (
                        "Live sweep achieved 72/72 exact repeatability against the staged "
                        "pilot benchmark with zero missing readouts and 100% control verification."
                    ),
                    (
                        "Linearized Biot–Savart regressions recover μ₀ within < 0.5% across "
                        "all 8 current/geometry series "
                        f"(wire mean {_fmt_signed_pct(mean_wire_dev)}, "
                        f"loop mean {_fmt_signed_pct(mean_loop_dev)})."
                    ),
                    (
                        "Independent manual reference review remains explicitly marked pending "
                        "in accordance with PEA scientific-honesty policy."
                    ),
                ],
                "calc_ids": calc_ids,
                "options": {
                    "include_self_control": False,
                    "include_problems": False,
                    "include_evidence_appendix": False,
                },
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
                        "profile_paths": saved_profile_relpaths,
                    },
                )
            )
            assert built["pdf_relpath"] == "reports/report.pdf"
            assert built["docx_relpath"] == "reports/report.docx"
            assert built["evidence_zip_relpath"] == "reports/evidence.zip"
            assert built["teacher_packet_relpath"] == "reports/teacher-evidence.pdf"
            assert built["page_count"] >= 3
            assert len(built["preview_png_relpaths"]) == built["page_count"]

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

            doc_obj = docx.Document(str(run_root / built["docx_relpath"]))
            doc_text = "\n".join(p.text for p in doc_obj.paragraphs)
            assert "Magnetic Field of a Straight Wire and Circular Loop" in doc_text
            assert "manual reference" in doc_text.lower()
            assert len(doc_obj.tables) >= 3

            with zipfile.ZipFile(run_root / built["evidence_zip_relpath"]) as zf:
                names = set(zf.namelist())
                assert "provenance.jsonl" in names
                assert "manifest.json" in names
                evidence_pngs = [
                    n for n in names if n.startswith("evidence/") and n.endswith(".png")
                ]
                assert len(evidence_pngs) >= 72

            verified = _parse_tool_json(
                await session.call_tool("lab_runs", {"action": "verify", "run_id": run_id})
            )
            assert verified["verification"]["ok"] is True, (
                f"Provenance verification failed: {verified['verification']['errors']}"
            )
            assert verified["verification"]["checked_entries"] >= 80
