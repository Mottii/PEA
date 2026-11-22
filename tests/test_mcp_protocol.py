"""Offline protocol, security, viewer, and packaging tests for Phase 2D.

Exercises:
1. Real ``mcp.client.stdio.stdio_client`` against ``pea-mcp`` (all 16 universal tools,
   ToolAnnotations, ``pea://policy``, ``calc_eval``, ``data_ingest``/``data_analyze``,
   ``plot_render``, ``diagram_render``, ``lab_inspect_pdf``, ``report_build``,
   ``report_preview``, startup crash recovery, extra-field & path-traversal rejection).
2. Mocked simulator MCP tool wrappers + lifespan ``close_all()`` cleanup.
3. Starlette ``pea-viewer`` anti-DNS-rebinding ``Host``/``Origin`` guards, CSP/nosniff
   headers, artifact confinement, OCR overlay JSON payload, and CLI loopback guard.
4. Claude Desktop ``.mcpb`` deterministic builder, negative security audits, and live
   Node.js ``launcher.mjs`` stdio handshake.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image

from pea import __version__
from pea.config import PeaConfig, resolve_browser_executable
from pea.mcp_server import POLICY_URI, UNIVERSAL_TOOL_NAMES, create_mcp_server
from pea.schemas import (
    CalibrationProfile,
    ControlSpec,
    OcrBox,
    OcrResult,
    ReadoutSpec,
)
from pea.sim.engine import SimulatorManager, SimulatorSession
from pea.sim.profiles import ProfileStore
from pea.viewer.app import create_viewer_app
from pea.viewer.app import main as viewer_main
from pea.workspace import WorkspaceManager

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_ASSETS_DIR = REPO_ROOT / "local_assets"
LAUNCHER_PATH = REPO_ROOT / "packaging" / "mcpb" / "launcher.mjs"
SKILL_PATH = REPO_ROOT / "skills" / "pea-lab-assistant" / "SKILL.md"
_MACOS_HOME_MARKER = "/" + "Users/"


def _load_build_mcpb_module() -> Any:
    script_path = REPO_ROOT / "scripts" / "build_mcpb.py"
    spec = importlib.util.spec_from_file_location("build_mcpb_under_test", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_png_bytes(
    width: int = 120,
    height: int = 80,
    color: tuple[int, int, int] = (240, 244, 248),
) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color)
    img.save(buf, format="PNG")
    return buf.getvalue()


def _parse_tool_json(result: Any) -> dict[str, Any]:
    assert result.is_error is False, f"Unexpected tool error: {result.content}"
    if getattr(result, "structured_content", None) is not None:
        return dict(result.structured_content)
    assert result.content, "Expected non-empty content from tool result"
    return json.loads(result.content[0].text)


def _stdio_env(tmp_path: Path) -> dict[str, str]:
    project_dir = tmp_path / "project"
    runs_dir = tmp_path / "runs"
    profiles_dir = tmp_path / "profiles"
    project_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update(
        {
            "PEA_PROJECT_ROOT": str(project_dir),
            "PEA_LOCAL_ASSETS_DIR": str(LOCAL_ASSETS_DIR),
            "PEA_RUNS_DIR": str(runs_dir),
            "PEA_PROFILES_DIR": str(profiles_dir),
            "PEA_OCR_BACKEND": "rapidocr",
            "PEA_ALLOWED_ORIGINS": "",
        }
    )
    return env


# ---------------------------------------------------------------------------
# 1. Real MCP stdio Client End-to-End & Security Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_stdio_tools_policy_math_analysis_and_visuals(tmp_path: Path) -> None:
    env = _stdio_env(tmp_path)
    runs_dir = Path(env["PEA_RUNS_DIR"])
    project_dir = Path(env["PEA_PROJECT_ROOT"])

    # Pre-seed an interrupted/running run to verify startup crash recovery via lifespan
    wm = WorkspaceManager(runs_dir)
    stale = wm.create_run(label="pre-crash-run", source="simulator_captured")
    stale.update_status(phase="running", message="mid-sweep")
    partial_file = stale.resolve_path("evidence/keep.png")
    partial_file.write_bytes(_make_png_bytes())
    stale.record_artifact("evidence/keep.png", kind="screenshot", source="simulator_captured")

    # Seed a small CSV inside the confined project root for file-based ingest
    csv_path = project_dir / "bench.csv"
    csv_path.write_text("r,B\n1.0,2.0\n2.0,1.0\n4.0,0.5\n5.0,0.4\n", encoding="utf-8")

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
            assert init.server_info.version == __version__

            # Verify all 16 universal tools + annotations + strict additionalProperties=False
            tools_res = await session.list_tools()
            tool_names = tuple(t.name for t in tools_res.tools)
            assert tool_names == UNIVERSAL_TOOL_NAMES
            assert len(tools_res.tools) == 16

            forbidden_tool_names = {
                "shell",
                "bash",
                "exec",
                "run_command",
                "read_file",
                "write_file",
            }
            assert forbidden_tool_names.isdisjoint(set(tool_names))

            for tool in tools_res.tools:
                assert tool.annotations is not None, f"{tool.name} missing ToolAnnotations"
                assert tool.annotations.title, f"{tool.name} missing annotation title"
                assert isinstance(tool.annotations.read_only_hint, bool)
                assert isinstance(tool.annotations.destructive_hint, bool)
                assert isinstance(tool.annotations.idempotent_hint, bool)
                assert isinstance(tool.annotations.open_world_hint, bool)
                assert tool.input_schema.get("additionalProperties") is False, (
                    f"{tool.name} input_schema must set additionalProperties=False"
                )

            # Verify pea://policy resource
            resources_res = await session.list_resources()
            uris = [str(r.uri) for r in resources_res.resources]
            assert POLICY_URI in uris

            policy_read = await session.read_resource(POLICY_URI)
            assert len(policy_read.contents) == 1
            policy_text = policy_read.contents[0].text
            assert _MACOS_HOME_MARKER not in policy_text
            policy = json.loads(policy_text)
            assert policy["zero_api_cost"] is True
            assert policy["llm_api_calls"] == 0
            assert policy["tools_count"] == 16
            assert policy["security"]["loopback_only_default"] is True
            assert policy["security"]["arbitrary_shell_tools"] is False
            assert policy["security"]["strict_schemas_extra_forbid"] is True

            # Verify startup crash recovery transitioned pre-crash-run -> interrupted & kept files
            stale_get = _parse_tool_json(
                await session.call_tool("lab_runs", {"action": "get", "run_id": stale.run_id})
            )
            assert stale_get["status"]["phase"] == "interrupted"
            stale_verify = _parse_tool_json(
                await session.call_tool("lab_runs", {"action": "verify", "run_id": stale.run_id})
            )
            assert stale_verify["verification"]["ok"] is True

            # Create a fresh run
            created = _parse_tool_json(
                await session.call_tool(
                    "lab_runs",
                    {"action": "create", "label": "stdio-e2e", "source": "simulator_captured"},
                )
            )
            run_id = created["run_id"]

            # End-to-end calc_eval (Lab 5 resonance frequency 1/sqrt(L*C))
            calc_out = _parse_tool_json(
                await session.call_tool(
                    "calc_eval",
                    {
                        "expression": "1 / sqrt(L * C)",
                        "variables": {
                            "L": {"value": 5.0, "unit": "mH"},
                            "C": {"value": 32.0, "unit": "uF"},
                        },
                        "output_unit": "rad/s",
                        "run_id": run_id,
                    },
                )
            )
            assert calc_out["output_value"] == pytest.approx(2500.0, rel=1e-9)
            assert calc_out["output_unit"] == "rad/s"
            assert calc_out["calc_id"].startswith("calc-")
            assert calc_out["provenance"] == "model_computed"

            # End-to-end data_ingest (CSV inside project root) + data_analyze (OLS)
            ingest_out = _parse_tool_json(
                await session.call_tool(
                    "data_ingest",
                    {
                        "run_id": run_id,
                        "file_path": "bench.csv",
                        "source": "bench_manual",
                        "dataset_id": "wire_bench",
                        "units": {"r": "m", "B": "mT"},
                    },
                )
            )
            assert ingest_out["dataset_id"] == "wire_bench"
            assert ingest_out["row_count"] == 4

            analyze_out = _parse_tool_json(
                await session.call_tool(
                    "data_analyze",
                    {
                        "run_id": run_id,
                        "dataset_id": "wire_bench",
                        "derived_columns": [{"name": "inv_r", "formula": "1 / r", "unit": "m^-1"}],
                        "regressions": [
                            {
                                "name": "B_vs_inv_r",
                                "x_column": "inv_r",
                                "y_column": "B",
                                "intercept": "zero",
                            }
                        ],
                        "comparisons": [
                            {
                                "name": "slope_check",
                                "measured": 2.0,
                                "reference": 2.0,
                                "unit": "mT*m",
                            }
                        ],
                    },
                )
            )
            assert analyze_out["regressions"][0]["slope"] == pytest.approx(2.0, rel=1e-9)
            assert analyze_out["regressions"][0]["r_squared"] == pytest.approx(1.0, rel=1e-9)
            assert analyze_out["comparisons"][0]["percent_deviation"] == pytest.approx(0.0)

            # End-to-end plot_render & diagram_render
            plot_out = _parse_tool_json(
                await session.call_tool(
                    "plot_render",
                    {
                        "run_id": run_id,
                        "spec": {
                            "plot_id": "fig_stdio_plot",
                            "title": "B vs 1/r",
                            "x_label": "1/r (m^-1)",
                            "y_label": "B (mT)",
                            "series": [
                                {
                                    "label": "Bench",
                                    "x": [0.2, 0.25, 0.5, 1.0],
                                    "y": [0.4, 0.5, 1.0, 2.0],
                                }
                            ],
                        },
                        "source": "bench_manual",
                    },
                )
            )
            assert plot_out["svg_relpath"] == "figures/fig_stdio_plot.svg"
            assert plot_out["png_relpath"] == "figures/fig_stdio_plot.png"

            diag_out = _parse_tool_json(
                await session.call_tool(
                    "diagram_render",
                    {
                        "run_id": run_id,
                        "spec": {
                            "diagram_id": "fig_stdio_rc",
                            "kind": "circuit",
                            "title": "Series RC",
                            "elements": [
                                {"kind": "source_sin", "label": "V", "direction": "up"},
                                {"kind": "resistor", "label": "R", "direction": "right"},
                                {"kind": "capacitor", "label": "C", "direction": "down"},
                                {"kind": "line", "direction": "left"},
                            ],
                        },
                    },
                )
            )
            assert diag_out["svg_relpath"] == "figures/fig_stdio_rc.svg"

            # End-to-end report_build (DOCX + evidence_zip offline, fast)
            rep_out = _parse_tool_json(
                await session.call_tool(
                    "report_build",
                    {
                        "run_id": run_id,
                        "formats": ["docx", "evidence_zip"],
                        "render_previews": False,
                        "spec": {
                            "title": "Stdio Verification Report",
                            "language": "en",
                            "objective": "Verify MCP stdio report compilation.",
                            "calc_ids": [calc_out["calc_id"]],
                            "figures": [plot_out, diag_out],
                            "conclusions": ["All calculations and fits verified."],
                        },
                    },
                )
            )
            assert rep_out["docx_relpath"] == "reports/report.docx"
            assert rep_out["evidence_zip_relpath"] == "reports/evidence.zip"

            # Verify full run SHA-256 provenance integrity
            verify_out = _parse_tool_json(
                await session.call_tool("lab_runs", {"action": "verify", "run_id": run_id})
            )
            assert verify_out["verification"]["ok"] is True
            assert verify_out["verification"]["checked_entries"] >= 6


@pytest.mark.asyncio
async def test_mcp_stdio_rejects_extra_fields_traversal_and_unsafe_inputs(tmp_path: Path) -> None:
    env = _stdio_env(tmp_path)
    runs_dir = Path(env["PEA_RUNS_DIR"])
    project_dir = Path(env["PEA_PROJECT_ROOT"])

    outside_secret = tmp_path / "outside_secret.csv"
    outside_secret.write_text("a,b\n1,2\n", encoding="utf-8")

    # Symlink inside project pointing outside
    evil_link = project_dir / "escape_link.csv"
    evil_link.symlink_to(outside_secret)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pea.mcp_server"],
        env=env,
        cwd=str(REPO_ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # 1. Extra top-level argument rejected (additionalProperties=False)
            res_extra_top = await session.call_tool(
                "calc_eval",
                {"expression": "1 + 1", "unexpected_extra_field": 123},
            )
            assert res_extra_top.is_error is True

            # 2. Extra nested StrictModel field rejected
            res_extra_nested = await session.call_tool(
                "calc_eval",
                {
                    "expression": "x * 2",
                    "variables": {"x": {"value": 3.0, "unit": "m", "bogus": True}},
                },
            )
            assert res_extra_nested.is_error is True

            # 3. Unsafe AST expression rejected
            res_unsafe_expr = await session.call_tool(
                "calc_eval",
                {"expression": "__import__('os').system('echo pwned')"},
            )
            assert res_unsafe_expr.is_error is True

            # 4. Path traversal & symlink escapes rejected with ZERO orphan runs created
            runs_before = len(list(runs_dir.iterdir()))

            for bad_pdf in ("../../etc/passwd", "/etc/passwd", "../outside_secret.csv"):
                res = await session.call_tool("lab_inspect_pdf", {"pdf_path": bad_pdf})
                assert res.is_error is True

            for bad_csv in ("../outside_secret.csv", str(outside_secret), "escape_link.csv"):
                res = await session.call_tool(
                    "data_ingest",
                    {"file_path": bad_csv, "source": "bench_manual"},
                )
                assert res.is_error is True

            res_bad_swf = await session.call_tool("sim_open", {"swf_path": "../../etc/passwd"})
            assert res_bad_swf.is_error is True

            # 5. Non-allowlisted external URL rejected
            res_bad_url = await session.call_tool(
                "sim_open", {"url": "https://untrusted.example/sim.html"}
            )
            assert res_bad_url.is_error is True

            # 6. Traversal in run_id rejected
            res_bad_run = await session.call_tool(
                "lab_runs", {"action": "get", "run_id": "../project"}
            )
            assert res_bad_run.is_error is True

            assert len(list(runs_dir.iterdir())) == runs_before


@pytest.mark.asyncio
async def test_mcp_stdio_pdf_intake_and_report_preview(tmp_path: Path) -> None:
    handout = LOCAL_ASSETS_DIR / "benchmarks" / "lab5-rlc-handout.pdf"
    if not handout.is_file():
        pytest.skip("Benchmark PDF not present in local_assets/benchmarks/")

    env = _stdio_env(tmp_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pea.mcp_server"],
        env=env,
        cwd=str(REPO_ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Inspect page 1 of the benchmark PDF via relative path under local_assets
            pdf_out = _parse_tool_json(
                await session.call_tool(
                    "lab_inspect_pdf",
                    {
                        "pdf_path": "benchmarks/lab5-rlc-handout.pdf",
                        "pages": [1],
                        "dpi": 96,
                        "label": "pdf-stdio-test",
                    },
                )
            )
            assert pdf_out["rendered_pages"] == 1
            assert pdf_out["pages"][0]["image_relpath"] == "pdf_pages/page-001.png"

            # Copy PDF into run/reports/sample.pdf to exercise report_preview + traversal guard
            run_id = pdf_out["run_id"]
            run_dir = Path(env["PEA_RUNS_DIR"]) / run_id
            shutil.copy2(handout, run_dir / "reports" / "sample.pdf")

            prev_out = _parse_tool_json(
                await session.call_tool(
                    "report_preview",
                    {
                        "run_id": run_id,
                        "pdf_relpath": "reports/sample.pdf",
                        "pages": [1],
                        "dpi": 96,
                    },
                )
            )
            assert prev_out["page_count"] == 1
            assert prev_out["preview_png_relpaths"] == ["reports/previews/page-001.png"]

            # Reject traversal in report_preview
            bad_prev = await session.call_tool(
                "report_preview",
                {"run_id": run_id, "pdf_relpath": "../status.json"},
            )
            assert bad_prev.is_error is True


# ---------------------------------------------------------------------------
# 2. Mocked Simulator MCP Tool Wrappers + Lifespan Shutdown Cleanup
# ---------------------------------------------------------------------------


class _FakeMouse:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    async def click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))

    async def dblclick(self, x: int, y: int) -> None:
        self.events.append(("dblclick", x, y))

    async def move(self, x: int, y: int, **kwargs: Any) -> None:
        self.events.append(("move", x, y))

    async def down(self) -> None:
        self.events.append(("down",))

    async def up(self) -> None:
        self.events.append(("up",))


class _FakeKeyboard:
    def __init__(self, on_type: Any = None) -> None:
        self.events: list[tuple[Any, ...]] = []
        self._on_type = on_type

    async def press(self, key: str) -> None:
        self.events.append(("press", key))

    async def type(self, text: str) -> None:
        self.events.append(("type", text))
        if self._on_type:
            self._on_type(text)


class _FakePage:
    def __init__(self) -> None:
        self.current_x = 10.0
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard(on_type=self._handle_type)
        self.closed = False

    def _handle_type(self, text: str) -> None:
        try:
            self.current_x = float(text.replace(",", "."))
        except ValueError:
            pass

    async def screenshot(self, *, type: str = "png") -> bytes:  # noqa: A002
        return _make_png_bytes(400, 300)

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _FakeOcr:
    backend_name = "rapidocr"

    def __init__(self, page: _FakePage) -> None:
        self.page = page

    def recognize(self, image: Any, *, mode: str = "det_rec") -> OcrResult:
        return OcrResult(
            backend="rapidocr",
            mode=mode,  # type: ignore[arg-type]
            elapsed_ms=2.5,
            boxes=[
                OcrBox(
                    text="2458.5",
                    confidence=0.99,
                    bbox_px=(100, 100, 200, 130),
                    bbox_norm=(0.25, 0.333, 0.5, 0.433),
                )
            ],
        )

    def read_spec(
        self, image: Any, spec: ReadoutSpec
    ) -> tuple[OcrResult, float | int | str | None, str | None]:
        if spec.name.startswith("__ctrl_") or spec.name.startswith("ctrl_"):
            val = self.page.current_x
            txt = f"{val:g}"
        else:
            # Peak plateau at x in [12, 14]
            x = self.page.current_x
            val = 5.0 if 12.0 <= x <= 14.0 else max(1.0, 5.0 - abs(x - 13.0))
            txt = f"{val:.2f}"
        res = OcrResult(
            backend="rapidocr",
            mode=spec.ocr_mode,
            elapsed_ms=1.0,
            boxes=[
                OcrBox(
                    text=txt,
                    confidence=0.98,
                    bbox_px=(0, 0, 50, 20),
                    bbox_norm=(0.0, 0.0, 1.0, 1.0),
                )
            ],
        )
        return res, val, None


@pytest.mark.asyncio
async def test_mcp_sim_wrappers_ocr_handoff_and_lifespan_shutdown(tmp_path: Path) -> None:
    cfg = PeaConfig(
        project_root=tmp_path,
        local_assets_dir=LOCAL_ASSETS_DIR,
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
    )
    wm = WorkspaceManager.from_config(cfg)
    ps = ProfileStore.from_config(cfg)
    page = _FakePage()
    ocr = _FakeOcr(page)

    sim_mgr = SimulatorManager(cfg, workspace_manager=wm, profile_store=ps, ocr=ocr)
    ws = wm.create_run(label="mock-sim-run", source="simulator_captured")
    fake_sha = hashlib.sha256(b"fake-swf").hexdigest()

    session = SimulatorSession(
        session_id="sim-mock01",
        config=cfg,
        workspace=ws,
        ocr=ocr,
        profile_store=ps,
        page=page,
        target_url="http://127.0.0.1:12345/index.html",
        loopback_origin="http://127.0.0.1:12345",
        stage_width=400,
        stage_height=300,
        header_height_px=39,
    )
    sim_mgr._sessions[session.session_id] = session

    server = create_mcp_server(cfg, workspace_manager=wm, sim_manager=sim_mgr)

    # Exercise lifespan enter + tool calls + lifespan exit closing active sessions
    async with server._lowlevel_server.lifespan(server._lowlevel_server):
        obs = _parse_tool_json(
            await server.call_tool(
                "sim_observe",
                {"session_id": "sim-mock01", "annotate": True},
            )
        )
        assert obs["annotated_relpath"] == "crops/annotated-0001.png"
        assert obs["ocr_log_relpath"].startswith("ocr/")
        assert (ws.root / obs["ocr_log_relpath"]).is_file()

        # Save calibration profile
        profile = CalibrationProfile(
            swf_sha256=fake_sha,
            stage_width=400,
            stage_height=300,
            header_height_px=39,
            blur_xy=(10, 10),
            controls={
                "omega": ControlSpec(
                    name="omega",
                    kind="stepper",
                    input_xy=(120, 120),
                    readout_roi_px=(100, 100, 180, 130),
                    step=1.0,
                    min_val=1.0,
                    max_val=30.0,
                    commit_mode="enter",
                )
            },
            readouts={
                "amp": ReadoutSpec(
                    name="amp",
                    roi_px=(200, 100, 300, 130),
                    ocr_mode="rec_only",
                    parse_mode="float",
                )
            },
        )
        saved = _parse_tool_json(
            await server.call_tool(
                "sim_save_profile",
                {
                    "session_id": "sim-mock01",
                    "profile": profile.model_dump(mode="json"),
                    "verify_live": True,
                },
            )
        )
        assert saved["verified_live"] is True
        assert _MACOS_HOME_MARKER not in saved["saved_relpath"]
        assert saved["saved_relpath"].startswith("profiles/")

        # Passing sim_save_profile's logical 'profiles/...' relpath into report_build works
        # even when profiles_dir is separate from project_root, while traversal is blocked.
        rep_with_prof = _parse_tool_json(
            await server.call_tool(
                "report_build",
                {
                    "run_id": ws.run_id,
                    "formats": ["evidence_zip"],
                    "render_previews": False,
                    "profile_paths": [saved["saved_relpath"]],
                    "spec": {
                        "title": "Profile Logical Path Test",
                        "language": "en",
                    },
                },
            )
        )
        assert rep_with_prof["evidence_zip_relpath"] == "reports/evidence.zip"
        with zipfile.ZipFile(ws.root / "reports/evidence.zip") as zf:
            assert any(name.endswith(f"{fake_sha}.json") for name in zf.namelist())

        with pytest.raises(ToolError, match="Path confinement error"):
            await server.call_tool(
                "report_build",
                {
                    "run_id": ws.run_id,
                    "formats": ["evidence_zip"],
                    "render_previews": False,
                    "profile_paths": ["profiles/../../secret.txt"],
                    "spec": {"title": "Traversal Test", "language": "en"},
                },
            )

        # sim_act
        acted = _parse_tool_json(
            await server.call_tool(
                "sim_act",
                {
                    "session_id": "sim-mock01",
                    "steps": [{"action": "set_stepper", "control": "omega", "value": 12.0}],
                },
            )
        )
        assert acted["executed"][0]["action"] == "set_stepper"

        # sim_sweep
        swept = _parse_tool_json(
            await server.call_tool(
                "sim_sweep",
                {
                    "session_id": "sim-mock01",
                    "spec": {
                        "axes": [{"control": "omega", "values": [10.0, 12.0, 15.0]}],
                        "readouts": ["amp"],
                        "settle_ms": 0,
                    },
                },
            )
        )
        assert swept["status"] == "complete"
        assert swept["captured_points"] == 3
        assert swept["controls_matched_all"] is True

        # sim_search_extremum (plateau_max over [12, 14])
        found = _parse_tool_json(
            await server.call_tool(
                "sim_search_extremum",
                {
                    "session_id": "sim-mock01",
                    "spec": {
                        "mode": "plateau_max",
                        "sweep_control": "omega",
                        "readout": "amp",
                        "start": 10.0,
                        "step": 1.0,
                        "max_steps": 15,
                        "stop_after_drop": 2,
                        "settle_ms": 0,
                    },
                },
            )
        )
        assert found["peak_value"] == pytest.approx(5.0)
        assert found["low_bound"] == pytest.approx(12.0)
        assert found["high_bound"] == pytest.approx(14.0)
        assert found["midpoint"] == pytest.approx(13.0)

        # Verify unknown session raises clean ToolError
        with pytest.raises(ToolError, match="Unknown simulator session_id"):
            await server.call_tool("sim_observe", {"session_id": "sim-missing"})

        # Close idempotently? Keep open here to verify lifespan exit closes it!
        assert page.closed is False

    # Lifespan exit must have closed the active session cleanly
    assert page.closed is True
    assert sim_mgr.list_session_ids() == []

    # Calling sim_close on an already-closed session is safely idempotent
    closed_again = _parse_tool_json(
        await server.call_tool("sim_close", {"session_id": "sim-mock01"})
    )
    assert closed_again["closed"] is False


# ---------------------------------------------------------------------------
# 3. Starlette Local Viewer (`pea-viewer`) Security & API Tests
# ---------------------------------------------------------------------------


def _seed_viewer_run(cfg: PeaConfig) -> tuple[WorkspaceManager, str]:
    wm = WorkspaceManager.from_config(cfg)
    ws = wm.create_run(label="Lab 5 Resonance Run", source="simulator_captured")
    ws.update_status(phase="complete", message="Done", progress_current=4, progress_total=4)

    shot_path = ws.resolve_path("evidence/observe-0001.png")
    shot_path.write_bytes(_make_png_bytes(320, 240))
    shot_entry = ws.record_artifact(
        "evidence/observe-0001.png", kind="screenshot", source="simulator_captured"
    )

    ann_path = ws.resolve_path("crops/annotated-0001.png")
    ann_path.write_bytes(_make_png_bytes(320, 240, (230, 238, 248)))
    ws.record_artifact(
        "crops/annotated-0001.png", kind="annotated_overlay", source="simulator_captured"
    )

    ocr_path = ws.resolve_path("ocr/observe-0001.json")
    ocr_path.parent.mkdir(parents=True, exist_ok=True)
    ocr_path.write_text(
        json.dumps(
            {
                "screenshot_relpath": "evidence/observe-0001.png",
                "screenshot_sha256": shot_entry.sha256,
                "annotated_relpath": "crops/annotated-0001.png",
                "stage_width": 320,
                "stage_height": 240,
                "ocr": {
                    "backend": "rapidocr",
                    "mode": "det_rec",
                    "elapsed_ms": 14.2,
                    "boxes": [
                        {
                            "text": "2458.5",
                            "confidence": 0.99,
                            "bbox_px": [40, 50, 140, 80],
                            "bbox_norm": [0.125, 0.208, 0.4375, 0.333],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    ws.record_artifact("ocr/observe-0001.json", kind="ocr_log", source="simulator_captured")

    svg_path = ws.resolve_path("figures/fig1.svg")
    svg_path.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'></svg>",
        encoding="utf-8",
    )
    ws.record_artifact("figures/fig1.svg", kind="plot_svg", source="simulator_captured")

    (ws.resolve_path("reports/report.pdf")).write_bytes(b"%PDF-1.4\n%%EOF\n")
    ws.record_artifact("reports/report.pdf", kind="report_pdf", source="model_computed")
    (ws.resolve_path("reports/report.docx")).write_bytes(b"PK\x03\x04fake")
    ws.record_artifact("reports/report.docx", kind="report_docx", source="model_computed")
    (ws.resolve_path("reports/evidence.zip")).write_bytes(b"PK\x03\x04fake")
    ws.record_artifact("reports/evidence.zip", kind="evidence_zip", source="simulator_captured")
    (ws.resolve_path("reports/report.html")).write_text(
        "<html><body>hi</body></html>", encoding="utf-8"
    )
    (ws.resolve_path("reports/partial.tmp")).write_text("temp", encoding="utf-8")

    ws.update_status(
        metadata_updates={
            "latest_screenshot_relpath": "evidence/observe-0001.png",
            "latest_annotated_relpath": "crops/annotated-0001.png",
            "latest_ocr_relpath": "ocr/observe-0001.json",
        }
    )
    return wm, ws.run_id


@pytest.mark.asyncio
async def test_viewer_endpoints_headers_and_ocr_payload(tmp_path: Path) -> None:
    cfg = PeaConfig(
        project_root=tmp_path,
        local_assets_dir=LOCAL_ASSETS_DIR,
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
    )
    wm, run_id = _seed_viewer_run(cfg)
    app = create_viewer_app(cfg, workspace_manager=wm)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5190") as client:
        # Index & static assets
        r_index = await client.get("/")
        assert r_index.status_code == 200
        assert "PEA v2 Local Lab Viewer" in r_index.text
        assert r_index.headers["x-content-type-options"] == "nosniff"
        assert r_index.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in r_index.headers["content-security-policy"]

        r_js = await client.get("/static/app.js")
        assert r_js.status_code == 200
        assert "javascript" in r_js.headers["content-type"]

        r_css = await client.get("/static/style.css")
        assert r_css.status_code == 200

        # Health endpoint
        r_health = await client.get("/api/health")
        assert r_health.status_code == 200
        health = r_health.json()
        assert health["ok"] is True
        assert health["zero_api_cost"] is True
        assert health["runs_count"] == 1

        # Runs list
        r_runs = await client.get("/api/runs")
        assert r_runs.status_code == 200
        runs_data = r_runs.json()
        assert runs_data["total"] == 1
        assert runs_data["runs"][0]["run_id"] == run_id
        assert runs_data["runs"][0]["has_pdf"] is True
        assert runs_data["runs"][0]["has_docx"] is True
        assert runs_data["runs"][0]["has_evidence_zip"] is True

        # Run detail with OCR boxes & verified provenance
        r_detail = await client.get(f"/api/runs/{run_id}")
        assert r_detail.status_code == 200
        detail = r_detail.json()
        assert detail["provenance_verification"]["ok"] is True
        assert detail["latest_capture"]["screenshot_relpath"] == "evidence/observe-0001.png"
        assert detail["latest_capture"]["annotated_relpath"] == "crops/annotated-0001.png"
        assert len(detail["latest_capture"]["ocr_boxes"]) == 1
        assert detail["latest_capture"]["ocr_boxes"][0]["text"] == "2458.5"
        assert detail["downloads"]["pdf_url"] == f"/runs/{run_id}/reports/report.pdf"

        # Artifact streaming + safe headers
        r_png = await client.get(f"/runs/{run_id}/evidence/observe-0001.png")
        assert r_png.status_code == 200
        assert r_png.headers["content-type"] == "image/png"
        assert r_png.headers["x-content-type-options"] == "nosniff"

        r_svg = await client.get(f"/runs/{run_id}/figures/fig1.svg")
        assert r_svg.status_code == 200
        assert (
            r_svg.headers["content-security-policy"]
            == "default-src 'none'; style-src 'unsafe-inline'"
        )

        r_docx = await client.get(f"/runs/{run_id}/reports/report.docx")
        assert r_docx.status_code == 200
        assert "attachment" in r_docx.headers["content-disposition"]

        r_html = await client.get(f"/runs/{run_id}/reports/report.html")
        assert r_html.status_code == 200
        assert r_html.headers["content-security-policy"] == "sandbox"
        assert "attachment" in r_html.headers["content-disposition"]

        # Temporary files are hidden
        r_tmp = await client.get(f"/runs/{run_id}/reports/partial.tmp")
        assert r_tmp.status_code == 404

        # Tampering an artifact flips provenance_verification.ok -> False
        ws = wm.get_run(run_id)
        ws.resolve_path("evidence/observe-0001.png").write_bytes(b"tampered")
        r_tampered = await client.get(f"/api/runs/{run_id}")
        assert r_tampered.json()["provenance_verification"]["ok"] is False


@pytest.mark.asyncio
async def test_viewer_security_guards_host_origin_methods_and_traversal(tmp_path: Path) -> None:
    cfg = PeaConfig(
        project_root=tmp_path,
        local_assets_dir=LOCAL_ASSETS_DIR,
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
    )
    wm, run_id = _seed_viewer_run(cfg)
    ws = wm.get_run(run_id)

    # Plant an outside secret and an escaping symlink inside the run
    outside = tmp_path / "secret.txt"
    outside.write_text("top-secret", encoding="utf-8")
    (ws.root / "evidence" / "leak.txt").symlink_to(outside)

    app = create_viewer_app(cfg, workspace_manager=wm)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5190") as client:
        # Allowed loopback Hosts
        for ok_host in ("127.0.0.1:5190", "localhost:5190", "[::1]:5190", "127.0.0.1"):
            r = await client.get("/api/health", headers={"Host": ok_host})
            assert r.status_code == 200, f"Expected 200 for Host={ok_host}"

        # Rejected non-loopback / DNS-rebinding Hosts
        for bad_host in (
            "evil.com",
            "evil.com:5190",
            "127.0.0.1.evil.com:5190",
            "192.168.1.5:5190",
            "0.0.0.0:5190",
            "[::1",
            "127.0.0.1:99999",
        ):
            r = await client.get("/api/health", headers={"Host": bad_host})
            assert r.status_code == 403, f"Expected 403 for Host={bad_host}"
            assert r.headers["x-content-type-options"] == "nosniff"

        # Same-origin loopback Origin allowed
        r_same_origin = await client.get(
            "/api/health",
            headers={"Host": "127.0.0.1:5190", "Origin": "http://127.0.0.1:5190"},
        )
        assert r_same_origin.status_code == 200

        # Cross-origin / null / non-loopback / mismatched-port Origins rejected
        for bad_origin in (
            "https://evil.example",
            "http://evil.example",
            "http://localhost:5190",
            "http://127.0.0.1:9999",
            "https://127.0.0.1:5190",
            "null",
            "file://",
        ):
            r = await client.get(
                "/api/health",
                headers={"Host": "127.0.0.1:5190", "Origin": bad_origin},
            )
            assert r.status_code == 403, f"Expected 403 for Origin={bad_origin}"

        # Non-GET/HEAD methods rejected
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            r = await client.request(method, "/api/runs")
            assert r.status_code == 405

        # Path traversal & symlink escapes rejected with 403
        for bad_url in (
            f"/runs/{run_id}/..%2F..%2Fsecret.txt",
            f"/runs/{run_id}/%2e%2e/%2e%2e/secret.txt",
            f"/runs/{run_id}/evidence/leak.txt",
            "/static/..%2Fapp.py",
        ):
            r = await client.get(bad_url)
            assert r.status_code == 403, f"Expected 403 for {bad_url}, got {r.status_code}"

        # Escaping latest_screenshot_relpath in status.json must be confined in /api/runs too
        for bad_rel in ("../../secret.txt", str(outside), "evidence/leak.txt"):
            ws.update_status(metadata_updates={"latest_screenshot_relpath": bad_rel})
            r_list = await client.get("/api/runs")
            assert r_list.status_code == 200
            summary = r_list.json()["runs"][0]
            assert summary["latest_screenshot_relpath"] == "evidence/observe-0001.png"
            r_det = await client.get(f"/api/runs/{run_id}")
            assert r_det.status_code == 200
            assert (
                r_det.json()["latest_capture"]["screenshot_relpath"]
                == "evidence/observe-0001.png"
            )

    # CLI refuses non-loopback bind
    with pytest.raises(SystemExit) as exc_info:
        viewer_main(["--host", "0.0.0.0", "--port", "5190"])
    assert exc_info.value.code != 0


@pytest.mark.asyncio
async def test_viewer_responsive_desktop_and_mobile_zero_overflow_and_ocr_toggle(
    tmp_path: Path,
) -> None:
    browser_exe = resolve_browser_executable()
    if browser_exe is None:
        pytest.skip("Playwright Chromium executable not cached locally")

    import uvicorn
    from playwright.async_api import async_playwright

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    cfg = PeaConfig(
        project_root=tmp_path,
        local_assets_dir=LOCAL_ASSETS_DIR,
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
        loopback_host="127.0.0.1",
        viewer_port=port,
        browser_executable=browser_exe,
    )
    wm, _ = _seed_viewer_run(cfg)
    app = create_viewer_app(cfg, workspace_manager=wm)
    uconfig = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(uconfig)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "Local uvicorn viewer server failed to start"

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path=str(browser_exe))
            try:
                for vp in ({"width": 1280, "height": 800}, {"width": 390, "height": 844}):
                    ctx = await browser.new_context(viewport=vp, device_scale_factor=1)
                    page = await ctx.new_page()
                    console_errs: list[str] = []
                    page_errs: list[str] = []
                    ext_reqs: list[str] = []
                    page.on(
                        "console",
                        lambda msg, errs=console_errs: errs.append(msg.text)
                        if msg.type == "error"
                        else None,
                    )
                    page.on("pageerror", lambda err, errs=page_errs: errs.append(str(err)))
                    page.on(
                        "request",
                        lambda req, exts=ext_reqs: exts.append(req.url)
                        if not req.url.startswith(f"http://127.0.0.1:{port}")
                        and not req.url.startswith("data:")
                        else None,
                    )

                    await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                    await page.wait_for_selector("#run-detail:not(.hidden)", timeout=5000)
                    await page.wait_for_selector(".ocr-rect", timeout=5000)

                    metrics = await page.evaluate(
                        """() => ({
                            sw: document.documentElement.scrollWidth,
                            iw: window.innerWidth,
                            rects: document.querySelectorAll('.ocr-rect').length,
                            badge: document.getElementById('detail-provenance-badge').textContent,
                        })"""
                    )
                    assert metrics["sw"] <= metrics["iw"], f"Horizontal overflow at {vp}: {metrics}"
                    assert metrics["rects"] == 1
                    assert "SHA-256 Verified" in metrics["badge"]

                    # Toggle OCR boxes off & annotated PNG on
                    await page.click("#toggle-ocr")
                    assert await page.evaluate(
                        "() => document.getElementById('ocr-overlay').classList.contains('hidden')"
                    )
                    await page.click("#toggle-annotated")
                    img_src = await page.get_attribute("#capture-img", "src")
                    assert img_src is not None and "annotated-0001.png" in img_src

                    assert console_errs == []
                    assert page_errs == []
                    assert ext_reqs == []
                    await ctx.close()
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=3)


# ---------------------------------------------------------------------------
# 4. Claude Desktop `.mcpb` Packaging, Archive Audit & Node Launcher Tests
# ---------------------------------------------------------------------------


def test_mcpb_build_reproducibility_and_negative_audits(tmp_path: Path) -> None:
    mod = _load_build_mcpb_module()

    # Build twice into separate files and verify reproducible SHA-256 + clean audit
    out_a = mod.build_mcpb(tmp_path / "a.mcpb", repo_root=REPO_ROOT)
    out_b = mod.build_mcpb(tmp_path / "b.mcpb", repo_root=REPO_ROOT)
    assert mod.audit_mcpb_archive(out_a, expected_version=__version__) == []
    assert hashlib.sha256(out_a.read_bytes()).hexdigest() == hashlib.sha256(
        out_b.read_bytes()
    ).hexdigest()

    with zipfile.ZipFile(out_a, "r") as zf:
        names = set(zf.namelist())
        assert {"manifest.json", "launcher.mjs", "SKILL.md"}.issubset(names)
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["manifest_version"] == "0.3"
        assert tuple(t["name"] for t in manifest["tools"]) == UNIVERSAL_TOOL_NAMES

    # Negative audit 1: traversal entry
    bad_trav = tmp_path / "bad_trav.mcpb"
    with zipfile.ZipFile(bad_trav, "w") as zf:
        zf.writestr("../escape.txt", "boom")
    assert any("Unsafe archive entry" in e for e in mod.audit_mcpb_archive(bad_trav))

    # Negative audit 2: forbidden binary extensions (.swf, .pdf, .onnx)
    bad_bin = tmp_path / "bad_bin.mcpb"
    with zipfile.ZipFile(bad_bin, "w") as zf:
        zf.writestr("m96220.swf", b"CWSfake")
        zf.writestr("handout.pdf", b"%PDF-1.4")
        zf.writestr("model.onnx", b"\x00\x01")
    bin_errs = mod.audit_mcpb_archive(bad_bin)
    assert sum("Forbidden file type" in e for e in bin_errs) == 3

    # Negative audit 3: symlink entry
    bad_sym = tmp_path / "bad_sym.mcpb"
    with zipfile.ZipFile(bad_sym, "w") as zf:
        info = zipfile.ZipInfo("link.mjs")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    assert any("Symlink entry forbidden" in e for e in mod.audit_mcpb_archive(bad_sym))

    # Negative audit 4: literal home path & API secret leak
    bad_leak = tmp_path / "bad_leak.mcpb"
    with zipfile.ZipFile(bad_leak, "w") as zf:
        zf.writestr("manifest.json", "{}")
        fake_home_path = _MACOS_HOME_MARKER + "alice/PEA"
        zf.writestr(
            "launcher.mjs",
            f'const p = "{fake_home_path}"; const k = "sk-ant-1234567890abcdef1234";\n',
        )
    leak_errs = mod.audit_mcpb_archive(bad_leak)
    assert any("literal macOS home path" in e for e in leak_errs)
    assert any("API key" in e for e in leak_errs)


@pytest.mark.asyncio
async def test_mcpb_node_launcher_errors_and_real_stdio_handshake(tmp_path: Path) -> None:
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("Node.js not available on PATH")

    # 1. Unexpanded template placeholder exits non-zero to stderr with empty stdout
    env_bad = dict(os.environ)
    env_bad["PEA_PROJECT_ROOT"] = "${user_config.project_folder}"
    proc = subprocess.run(
        [node_bin, str(LAUNCHER_PATH)],
        env=env_bad,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "PEA_PROJECT_ROOT" in proc.stderr

    # 2. Real stdio handshake through launcher.mjs (even with unexpanded optional placeholders)
    env_ok = dict(os.environ)
    env_ok.update(
        {
            "PEA_PROJECT_ROOT": str(REPO_ROOT),
            "PEA_RUNS_DIR": str(tmp_path / "runs"),
            "PEA_PROFILES_DIR": str(tmp_path / "profiles"),
            "PEA_ALLOWED_ORIGINS": "${user_config.allowed_origins}",
            "PEA_OCR_BACKEND": "${user_config.ocr_backend}",
        }
    )
    params = StdioServerParameters(
        command=node_bin,
        args=[str(LAUNCHER_PATH)],
        env=env_ok,
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert init.server_info.name == "pea-mcp"
            tools_res = await session.list_tools()
            assert tuple(t.name for t in tools_res.tools) == UNIVERSAL_TOOL_NAMES
            pol = await session.read_resource(POLICY_URI)
            assert json.loads(pol.contents[0].text)["zero_api_cost"] is True


def test_companion_skill_covers_all_tools_and_has_no_home_paths() -> None:
    assert SKILL_PATH.is_file()
    content = SKILL_PATH.read_text(encoding="utf-8")
    assert content.startswith("---\nname: pea-lab-assistant\n")
    assert POLICY_URI in content
    for tool_name in UNIVERSAL_TOOL_NAMES:
        assert tool_name in content, f"SKILL.md missing tool reference: {tool_name}"
    assert _MACOS_HOME_MARKER not in content
    assert "/home/" not in content
