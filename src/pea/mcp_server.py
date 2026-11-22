"""PEA v2 universal MCP stdio server built on ``mcp.server.mcpserver.MCPServer`` (mcp==2.3.0).

Exposes 16 deterministic, zero-API-cost laboratory tools plus the read-only ``pea://policy``
resource. Zero LLM API calls, zero arbitrary shell/filesystem tools, strict path confinement,
and ``extra="forbid"`` validation on every tool input schema.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, NoReturn

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Annotations, ToolAnnotations
from pydantic import ConfigDict, ValidationError

from pea import __version__
from pea.config import PathConfinementError, PeaConfig, resolve_confined_path
from pea.math_engine.analysis import (
    AnalysisError,
)
from pea.math_engine.analysis import (
    data_analyze as _data_analyze,
)
from pea.math_engine.analysis import (
    data_ingest as _data_ingest,
)
from pea.math_engine.calculator import (
    CalcError,
    load_calculations,
)
from pea.math_engine.calculator import (
    calc_eval as _calc_eval,
)
from pea.pdf_intake import PdfInspectionResult, inspect_pdf
from pea.schemas import (
    ActionStep,
    AnalysisResult,
    CalcRequest,
    CalcResult,
    CalcVariable,
    CalibrationProfile,
    ComparisonSpec,
    DatasetSpec,
    DerivedColumnSpec,
    DiagramSpec,
    OcrMode,
    PlateauResult,
    PlotSpec,
    ProvenanceSource,
    RegressionSpec,
    ReportBuildResult,
    ReportFigure,
    ReportSpec,
    SearchSpec,
    SweepSpec,
)
from pea.sim.engine import SimCancelledError, SimulatorManager, _origin_matches_allowlist
from pea.sim.swf_meta import SwfFormatError
from pea.workspace import RunWorkspace, WorkspaceManager

logger = logging.getLogger(__name__)

POLICY_URI = "pea://policy"

UNIVERSAL_TOOL_NAMES: tuple[str, ...] = (
    "lab_inspect_pdf",
    "lab_runs",
    "sim_open",
    "sim_observe",
    "sim_act",
    "sim_save_profile",
    "sim_sweep",
    "sim_search_extremum",
    "sim_close",
    "data_ingest",
    "data_analyze",
    "calc_eval",
    "plot_render",
    "diagram_render",
    "report_build",
    "report_preview",
)

_REPORT_FORMATS_DEFAULT: tuple[Literal["pdf", "docx", "evidence_zip"], ...] = (
    "pdf",
    "docx",
    "evidence_zip",
)


class PeaServerRuntime:
    """Shared runtime state owned by a ``pea-mcp`` server instance."""

    def __init__(
        self,
        config: PeaConfig | None = None,
        *,
        workspace_manager: WorkspaceManager | None = None,
        sim_manager: SimulatorManager | None = None,
    ) -> None:
        self.config: PeaConfig = config or PeaConfig.from_env()
        self.workspace_manager: WorkspaceManager = (
            workspace_manager or WorkspaceManager.from_config(self.config)
        )
        self.sim_manager: SimulatorManager = sim_manager or SimulatorManager(
            self.config,
            workspace_manager=self.workspace_manager,
        )

    def startup(self) -> list[str]:
        """Run startup crash recovery without discarding any partial evidence."""
        recovered = self.workspace_manager.recover_interrupted_runs()
        return [status.run_id for status in recovered]

    async def shutdown(self) -> None:
        """Cleanly close all Playwright sessions, contexts, and loopback servers."""
        await self.sim_manager.close_all()


def _sanitize_text(text: str, config: PeaConfig) -> str:
    """Replace absolute local filesystem prefixes with logical tokens in client-facing text."""
    replacements: list[tuple[str, str]] = [
        (str(config.local_assets_dir.resolve(strict=False)), "<local_assets>"),
        (str(config.runs_dir.resolve(strict=False)), "<runs>"),
        (str(config.profiles_dir.resolve(strict=False)), "<profiles>"),
        (str(config.project_root.resolve(strict=False)), "<project>"),
        (str(Path.home().resolve(strict=False)), "<home>"),
    ]
    # Replace longest prefixes first so nested paths map to their most specific token.
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)
    cleaned = text
    for raw, token in replacements:
        if raw and raw != "/":
            cleaned = cleaned.replace(raw, token)
    return cleaned


def _display_confined_path(path: Path, config: PeaConfig) -> str:
    """Format a confined path relative to a logical root when possible."""
    resolved = path.resolve(strict=False)
    for label, base in (
        ("runs", config.runs_dir),
        ("profiles", config.profiles_dir),
        ("local_assets", config.local_assets_dir),
        ("project", config.project_root),
    ):
        base_resolved = base.resolve(strict=False)
        if resolved.is_relative_to(base_resolved):
            rel = resolved.relative_to(base_resolved).as_posix()
            return rel if label == "project" else f"{label}/{rel}"
    return resolved.name


def _resolve_confined_input(
    raw_path: str,
    allowed_roots: Sequence[Path],
    *,
    label: str,
    logical_prefixes: dict[str, Path] | None = None,
) -> Path:
    """Resolve *raw_path* strictly within one of *allowed_roots*, rejecting traversal/escapes.

    When *logical_prefixes* is provided (e.g. ``{"profiles": cfg.profiles_dir}``), a
    relative path prefixed with ``"<prefix>/"`` returned by ``_display_confined_path`` is
    also resolved against the corresponding confined root if that root is in *allowed_roots*.
    """
    if not raw_path or "\x00" in raw_path:
        raise PathConfinementError(f"Invalid {label}: {raw_path!r}")

    unique_roots: list[Path] = []
    seen: set[Path] = set()
    for root in allowed_roots:
        r = root.expanduser().resolve(strict=False)
        if r not in seen:
            seen.add(r)
            unique_roots.append(r)

    first_confined: Path | None = None
    if logical_prefixes and not Path(raw_path).is_absolute():
        norm_raw = raw_path.replace("\\", "/")
        for prefix, mapped_root in logical_prefixes.items():
            prefix_slash = f"{prefix}/"
            if norm_raw.startswith(prefix_slash):
                mapped_resolved = mapped_root.expanduser().resolve(strict=False)
                if mapped_resolved in seen:
                    sub = norm_raw[len(prefix_slash) :]
                    if sub:
                        try:
                            candidate = resolve_confined_path(mapped_resolved, sub)
                        except PathConfinementError:
                            raise
                        if first_confined is None:
                            first_confined = candidate
                        if candidate.exists():
                            return candidate

    for root in unique_roots:
        try:
            candidate = resolve_confined_path(root, raw_path)
        except PathConfinementError:
            continue
        if first_confined is None:
            first_confined = candidate
        if candidate.exists():
            return candidate

    if first_confined is not None:
        return first_confined

    raise PathConfinementError(f"{label} escapes allowed workspace roots: {raw_path!r}")


def _raise_tool_error(exc: Exception, config: PeaConfig) -> NoReturn:
    """Translate domain, validation, and security errors into sanitized ``ToolError``s."""
    if isinstance(exc, ToolError):
        raise exc
    if isinstance(exc, SimCancelledError):
        msg = (
            f"Operation cancelled ({len(exc.partial_trace)} partial point(s) saved"
            + (f" to {exc.readings_relpath}" if exc.readings_relpath else "")
            + f"): {_sanitize_text(str(exc), config)}"
        )
        raise ToolError(msg) from exc
    if isinstance(exc, PathConfinementError):
        raise ToolError(f"Path confinement error: {_sanitize_text(str(exc), config)}") from exc
    if isinstance(exc, PermissionError):
        raise ToolError(f"Permission denied: {_sanitize_text(str(exc), config)}") from exc
    if isinstance(exc, FileNotFoundError):
        raise ToolError(f"Not found: {_sanitize_text(str(exc), config)}") from exc
    if isinstance(exc, FileExistsError):
        raise ToolError(f"Already exists: {_sanitize_text(str(exc), config)}") from exc
    if isinstance(exc, ValidationError):
        raise ToolError(f"Validation error: {_sanitize_text(str(exc), config)}") from exc
    if isinstance(
        exc,
        (
            CalcError,
            AnalysisError,
            SwfFormatError,
            KeyError,
            ValueError,
            TypeError,
            RuntimeError,
        ),
    ):
        raise ToolError(_sanitize_text(str(exc), config)) from exc
    raise exc


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    encoded = json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _collect_run_artifacts(workspace: RunWorkspace) -> dict[str, list[str]]:
    datasets: list[str] = []
    datasets_dir = workspace.root / "datasets"
    if datasets_dir.is_dir():
        datasets = sorted(
            p.relative_to(workspace.root).as_posix()
            for p in datasets_dir.glob("*.json")
            if p.is_file() and not p.name.endswith(".tmp")
        )

    figures: list[str] = []
    if workspace.figures_dir.is_dir():
        figures = sorted(
            p.relative_to(workspace.root).as_posix()
            for p in workspace.figures_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() in (".svg", ".png")
            and not p.name.endswith(".tmp")
        )

    reports: list[str] = []
    if workspace.reports_dir.is_dir():
        reports = sorted(
            p.relative_to(workspace.root).as_posix()
            for p in workspace.reports_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() in (".pdf", ".docx", ".zip")
            and not p.name.endswith(".tmp")
        )

    return {"datasets": datasets, "figures": figures, "reports": reports}


def build_policy_payload(config: PeaConfig) -> dict[str, Any]:
    """Build the read-only ``pea://policy`` JSON document (no host home paths)."""
    return {
        "server": "pea-mcp",
        "version": __version__,
        "zero_api_cost": True,
        "llm_api_calls": 0,
        "requires_api_keys": False,
        "scientific_integrity": {
            "never_fabricate_readings": True,
            "preserve_partial_evidence_on_failure_or_cancel": True,
            "provenance_sources": ["simulator_captured", "bench_manual", "model_computed"],
            "immutable_provenance_ledger": "provenance.jsonl",
            "immutable_calculation_ledger": "calculations.jsonl",
            "auto_claim_manual_verification": False,
        },
        "security": {
            "loopback_only_default": True,
            "loopback_host": config.loopback_host,
            "viewer_origin": config.viewer_origin,
            "allowed_origins": list(config.allowed_origins),
            "ruffle_sandbox": {
                "allowScriptAccess": False,
                "allowNetworking": "none",
            },
            "arbitrary_shell_tools": False,
            "arbitrary_filesystem_tools": False,
            "path_confinement": True,
            "strict_schemas_extra_forbid": True,
        },
        "ocr": {
            "backend": config.ocr_backend,
            "threads": config.ocr_threads,
            "timeout_s": config.ocr_timeout_s,
        },
        "tools_count": len(UNIVERSAL_TOOL_NAMES),
        "tools": list(UNIVERSAL_TOOL_NAMES),
    }


def _enforce_strict_tool_schemas(server: MCPServer[Any]) -> None:
    """Enforce ``extra='forbid'`` (``additionalProperties: false``) on every registered tool."""
    for tool_name in UNIVERSAL_TOOL_NAMES:
        tool = server._tool_manager.get_tool(tool_name)
        if tool is None:
            raise RuntimeError(f"Expected universal tool {tool_name!r} was not registered")
        arg_model = tool.fn_metadata.arg_model
        arg_model.model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")
        arg_model.model_rebuild(force=True)
        tool.parameters = arg_model.model_json_schema(by_alias=True)


def create_mcp_server(
    config: PeaConfig | None = None,
    *,
    workspace_manager: WorkspaceManager | None = None,
    sim_manager: SimulatorManager | None = None,
) -> MCPServer[PeaServerRuntime]:
    """Create and configure the ``pea-mcp`` ``MCPServer`` with all 16 tools and ``pea://policy``."""
    runtime = PeaServerRuntime(
        config=config,
        workspace_manager=workspace_manager,
        sim_manager=sim_manager,
    )
    cfg = runtime.config

    @asynccontextmanager
    async def _lifespan(_server: MCPServer[PeaServerRuntime]) -> AsyncIterator[PeaServerRuntime]:
        recovered = runtime.startup()
        if recovered:
            logger.info("Recovered %d interrupted run(s) on startup", len(recovered))
        try:
            yield runtime
        finally:
            await runtime.shutdown()

    server: MCPServer[PeaServerRuntime] = MCPServer(
        name="pea-mcp",
        title="PEA v2 — Physics Experiments Automation",
        description=(
            "Zero-API-cost deterministic physics laboratory MCP server: PDF intake, "
            "sandboxed Ruffle/HTML5 simulator automation, offline ONNX OCR, dimensional math, "
            "OLS regression, vector plots/schematics, and academic PDF/DOCX report compilation."
        ),
        instructions=(
            "Read pea://policy before starting a lab workflow. Never fabricate simulator "
            "readings, always compute formulas via calc_eval and fits via data_analyze, "
            "and inspect compiled report pages via report_preview."
        ),
        version=__version__,
        lifespan=_lifespan,
        log_level="WARNING",
    )

    # ------------------------------------------------------------------
    # Resource: pea://policy
    # ------------------------------------------------------------------

    @server.resource(
        POLICY_URI,
        name="pea_policy",
        title="PEA v2 Security & Scientific Integrity Policy",
        description=(
            "Read-only runtime policy covering zero-API-cost operation, loopback sandboxing, "
            "path confinement, provenance separation, and the 16 universal tools."
        ),
        mime_type="application/json",
        annotations=Annotations(audience=["assistant", "user"], priority=1.0),
    )
    def pea_policy() -> str:
        return json.dumps(build_policy_payload(cfg), indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Group 1: Handout & Runs (2 tools)
    # ------------------------------------------------------------------

    logical_roots: dict[str, Path] = {
        "local_assets": cfg.local_assets_dir,
        "runs": cfg.runs_dir,
        "profiles": cfg.profiles_dir,
    }

    @server.tool(
        name="lab_inspect_pdf",
        title="Inspect PDF Handout",
        description=(
            "Render a local PDF laboratory handout to PNG pages via pypdfium2 and extract "
            "page-indexed text via pypdf inside a confined run workspace with SHA-256 provenance."
        ),
        annotations=ToolAnnotations(
            title="Inspect PDF Handout",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def lab_inspect_pdf(
        pdf_path: str,
        run_id: str | None = None,
        dpi: int = 180,
        max_pages: int = 30,
        pages: list[int] | None = None,
        subdir: str | None = None,
        source: ProvenanceSource = "bench_manual",
        overwrite: bool = False,
        label: str | None = None,
    ) -> PdfInspectionResult:
        try:
            resolved_pdf = _resolve_confined_input(
                pdf_path,
                (cfg.project_root, cfg.local_assets_dir, cfg.runs_dir),
                label="pdf_path",
                logical_prefixes=logical_roots,
            )
            if not resolved_pdf.is_file():
                raise FileNotFoundError(f"PDF file not found: {resolved_pdf.name}")
            workspace = (
                runtime.workspace_manager.get_run(run_id)
                if run_id is not None
                else runtime.workspace_manager.create_run(label=label, source=source)
            )
            return inspect_pdf(
                resolved_pdf,
                workspace,
                dpi=dpi,
                max_pages=max_pages,
                pages=pages,
                subdir=subdir,
                source=source,
                overwrite=overwrite,
            )
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="lab_runs",
        title="Manage Lab Runs",
        description=(
            "List, inspect, create, or SHA-256-verify PEA execution run workspaces and view "
            "active simulator session IDs."
        ),
        annotations=ToolAnnotations(
            title="Manage Lab Runs",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def lab_runs(
        action: Literal["list", "get", "create", "verify"] = "list",
        run_id: str | None = None,
        label: str | None = None,
        request_id: str | None = None,
        source: ProvenanceSource | None = None,
        limit: int = 20,
        include_provenance: bool = False,
    ) -> dict[str, Any]:
        try:
            if action == "list":
                if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 200):
                    raise ValueError(f"limit must be an integer in 1..200; got {limit!r}")
                all_runs = runtime.workspace_manager.list_runs()
                sliced = all_runs[:limit]
                return {
                    "action": "list",
                    "total": len(all_runs),
                    "runs": [status.model_dump(mode="json") for status in sliced],
                    "active_sessions": runtime.sim_manager.list_session_ids(),
                }

            if action == "create":
                ws = runtime.workspace_manager.create_run(
                    request_id=request_id,
                    label=label,
                    source=source,
                )
                return {
                    "action": "create",
                    "run_id": ws.run_id,
                    "status": ws.read_status().model_dump(mode="json"),
                }

            if not run_id:
                raise ValueError(f"run_id is required for lab_runs action={action!r}")
            ws = runtime.workspace_manager.get_run(run_id)

            if action == "verify":
                verification = ws.verify_provenance()
                return {
                    "action": "verify",
                    "run_id": ws.run_id,
                    "verification": verification.model_dump(mode="json"),
                }

            # action == "get"
            status = ws.read_status()
            entries = ws.read_provenance()
            calcs = load_calculations(ws)
            artifacts = _collect_run_artifacts(ws)
            payload: dict[str, Any] = {
                "action": "get",
                "run_id": ws.run_id,
                "status": status.model_dump(mode="json"),
                "provenance_count": len(entries),
                "calculations_count": len(calcs),
                "datasets": artifacts["datasets"],
                "figures": artifacts["figures"],
                "reports": artifacts["reports"],
                "active_sessions": runtime.sim_manager.list_session_ids(),
            }
            if include_provenance:
                payload["provenance"] = [entry.model_dump(mode="json") for entry in entries]
            return payload
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    # ------------------------------------------------------------------
    # Group 2: Universal Simulator (7 tools)
    # ------------------------------------------------------------------

    @server.tool(
        name="sim_open",
        title="Open Simulator Session",
        description=(
            "Open a local SWF (sandboxed Ruffle on loopback), local HTML5 simulator directory, "
            "or explicitly allowlisted URL in headless Chromium and auto-load any matching "
            "SHA-256 calibration profile."
        ),
        annotations=ToolAnnotations(
            title="Open Simulator Session",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def sim_open(
        swf_path: str | None = None,
        html5_dir: str | None = None,
        url: str | None = None,
        entry_file: str = "index.html",
        run_id: str | None = None,
        header_height_px: int = 39,
        settle_ms: int = 1500,
        viewport_width: int | None = None,
        viewport_height: int | None = None,
    ) -> dict[str, Any]:
        try:
            provided = sum(item is not None for item in (swf_path, html5_dir, url))
            if provided != 1:
                raise ValueError("Provide exactly one of swf_path, html5_dir, or url")

            allowed_roots = (cfg.project_root, cfg.local_assets_dir, cfg.runs_dir)
            resolved_swf: Path | None = None
            resolved_html5: Path | None = None
            if swf_path is not None:
                resolved_swf = _resolve_confined_input(
                    swf_path,
                    allowed_roots,
                    label="swf_path",
                    logical_prefixes=logical_roots,
                )
                if not resolved_swf.is_file():
                    raise FileNotFoundError(f"SWF file not found: {resolved_swf.name}")
            elif html5_dir is not None:
                resolved_html5 = _resolve_confined_input(
                    html5_dir,
                    allowed_roots,
                    label="html5_dir",
                    logical_prefixes=logical_roots,
                )
                if not resolved_html5.is_dir():
                    raise FileNotFoundError(f"HTML5 directory not found: {resolved_html5.name}")
            else:
                assert url is not None
                if not _origin_matches_allowlist(url, cfg):
                    raise PermissionError(
                        "URL origin is not allowlisted. Add it explicitly via PEA_ALLOWED_ORIGINS."
                    )

            session = await runtime.sim_manager.open(
                swf_path=resolved_swf,
                html5_dir=resolved_html5,
                url=url,
                entry_file=entry_file,
                run_id=run_id,
                header_height_px=header_height_px,
                settle_ms=settle_ms,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
            )

            swf_meta_payload: dict[str, Any] | None = None
            if session.swf_meta is not None:
                swf_meta_payload = session.swf_meta.model_dump(mode="json")
                swf_meta_payload["path"] = _display_confined_path(
                    Path(session.swf_meta.path), cfg
                )

            return {
                "session_id": session.session_id,
                "run_id": session.workspace.run_id,
                "stage_width": session.stage_width,
                "stage_height": session.stage_height,
                "header_height_px": session.header_height_px,
                "swf_meta": swf_meta_payload,
                "profile_loaded": session.profile is not None,
                "profile": (
                    session.profile.model_dump(mode="json")
                    if session.profile is not None
                    else None
                ),
                "console_messages_count": len(session.console_messages),
                "page_errors": list(session.page_errors),
                "aborted_requests": list(session.aborted_requests),
            }
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="sim_observe",
        title="Observe Simulator Frame",
        description=(
            "Capture an unmodified raw PNG (hashed in provenance.jsonl), run offline OCR, "
            "optionally generate an annotated coordinate-grid overlay PNG, and optionally read "
            "named calibration profile readouts."
        ),
        annotations=ToolAnnotations(
            title="Observe Simulator Frame",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def sim_observe(
        session_id: str,
        annotate: bool = False,
        readouts: list[str] | None = None,
        ocr_mode: OcrMode = "det_rec",
        blur_first: bool = False,
    ) -> dict[str, Any]:
        try:
            session = runtime.sim_manager.get_session(session_id)
            result = await session.observe(
                annotate=annotate,
                readouts=readouts,
                ocr_mode=ocr_mode,
                blur_first=blur_first,
            )
            ocr_payload = result["ocr"].model_dump(mode="json")
            shot_rel = str(result["screenshot_relpath"])
            ocr_rel = f"ocr/{Path(shot_rel).stem}.json"
            ocr_abs = session.workspace.resolve_path(ocr_rel)
            _atomic_write_json(
                ocr_abs,
                {
                    "session_id": session.session_id,
                    "screenshot_relpath": shot_rel,
                    "screenshot_sha256": result["screenshot_sha256"],
                    "annotated_relpath": result["annotated_relpath"],
                    "stage_width": result["stage_width"],
                    "stage_height": result["stage_height"],
                    "header_height_px": result["header_height_px"],
                    "ocr": ocr_payload,
                    "readouts": result["readouts"],
                },
            )
            session.workspace.record_artifact(
                ocr_rel,
                kind="ocr_log",
                metadata={"screenshot_relpath": shot_rel, "ocr_mode": ocr_mode},
                source="simulator_captured",
            )
            if session.workspace.status_path.is_file():
                meta_updates: dict[str, Any] = {
                    "latest_screenshot_relpath": shot_rel,
                    "latest_ocr_relpath": ocr_rel,
                }
                if result["annotated_relpath"] is not None:
                    meta_updates["latest_annotated_relpath"] = result["annotated_relpath"]
                session.workspace.update_status(metadata_updates=meta_updates)

            return {
                **result,
                "ocr": ocr_payload,
                "ocr_log_relpath": ocr_rel,
            }
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="sim_act",
        title="Execute Simulator Actions",
        description=(
            "Execute atomic UI actions (click, double_click, drag, type, key, wait, blur, "
            "or profile-aware boundary-safe set_stepper) against an active simulator session."
        ),
        annotations=ToolAnnotations(
            title="Execute Simulator Actions",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def sim_act(
        session_id: str,
        steps: list[ActionStep],
        settle_ms: int = 0,
    ) -> dict[str, Any]:
        try:
            if not steps:
                raise ValueError("steps must contain at least one ActionStep")
            if isinstance(settle_ms, bool) or not isinstance(settle_ms, int) or settle_ms < 0:
                raise ValueError(f"settle_ms must be an integer >= 0; got {settle_ms!r}")
            session = runtime.sim_manager.get_session(session_id)
            executed = await session.act(steps, settle_ms=settle_ms)
            return {
                "session_id": session.session_id,
                "run_id": session.workspace.run_id,
                "executed": executed,
            }
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="sim_save_profile",
        title="Save Calibration Profile",
        description=(
            "Validate geometry and live OCR visibility for a SHA-256-keyed CalibrationProfile "
            "and persist it atomically for reuse across future sessions."
        ),
        annotations=ToolAnnotations(
            title="Save Calibration Profile",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    async def sim_save_profile(
        session_id: str,
        profile: CalibrationProfile,
        verify_live: bool = True,
        shared: bool = False,
    ) -> dict[str, Any]:
        try:
            session = runtime.sim_manager.get_session(session_id)
            result = await session.save_profile(
                profile,
                verify_live=verify_live,
                shared=shared,
            )
            saved_display = _display_confined_path(Path(result["saved_path"]), cfg)
            return {
                "session_id": session.session_id,
                "run_id": session.workspace.run_id,
                "saved_relpath": saved_display,
                "swf_sha256": result["swf_sha256"],
                "verified_live": result["verified_live"],
                "verification": result["verification"],
            }
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="sim_sweep",
        title="Run Parameter Grid Sweep",
        description=(
            "Execute a deterministic N-dimensional simulator parameter sweep with per-point "
            "OCR control verification, hashed evidence PNGs, and partial evidence retention."
        ),
        annotations=ToolAnnotations(
            title="Run Parameter Grid Sweep",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def sim_sweep(
        session_id: str,
        spec: SweepSpec,
    ) -> dict[str, Any]:
        try:
            session = runtime.sim_manager.get_session(session_id)
            result = await session.sweep(spec)
            points = result["points"]
            if points and session.workspace.status_path.is_file():
                session.workspace.update_status(
                    metadata_updates={
                        "latest_screenshot_relpath": points[-1].screenshot_relpath,
                    }
                )
            return {
                "session_id": session.session_id,
                "run_id": session.workspace.run_id,
                "status": result["status"],
                "total_points": result["total_points"],
                "captured_points": result["captured_points"],
                "controls_matched_all": result["controls_matched_all"],
                "missing_readouts": result["missing_readouts"],
                "readings_relpath": result["readings_relpath"],
                "points": [pt.model_dump(mode="json") for pt in points],
            }
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="sim_search_extremum",
        title="Search Extremum or Plateau",
        description=(
            "Run a closed-loop simulator search (max, min, plateau_max, or threshold) with "
            "wide-plateau detection, full measurement trace, and boundary evidence screenshots."
        ),
        annotations=ToolAnnotations(
            title="Search Extremum or Plateau",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def sim_search_extremum(
        session_id: str,
        spec: SearchSpec,
    ) -> PlateauResult:
        try:
            session = runtime.sim_manager.get_session(session_id)
            result = await session.search_extremum(spec)
            if result.trace and session.workspace.status_path.is_file():
                session.workspace.update_status(
                    metadata_updates={
                        "latest_screenshot_relpath": result.trace[-1].screenshot_relpath,
                    }
                )
            return result
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="sim_close",
        title="Close Simulator Session",
        description=(
            "Close an active simulator session's Playwright page/context and stop its loopback "
            "runtime server."
        ),
        annotations=ToolAnnotations(
            title="Close Simulator Session",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    async def sim_close(session_id: str) -> dict[str, Any]:
        try:
            existed = session_id in runtime.sim_manager.list_session_ids()
            await runtime.sim_manager.close_session(session_id)
            return {
                "session_id": session_id,
                "closed": existed,
                "active_sessions": runtime.sim_manager.list_session_ids(),
            }
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    # ------------------------------------------------------------------
    # Group 3: Math & Data (3 tools)
    # ------------------------------------------------------------------

    @server.tool(
        name="data_ingest",
        title="Ingest Measurement Dataset",
        description=(
            "Ingest inline rows or a confined CSV/JSON dataset file into <run>/datasets/<id>.json "
            "with explicit ProvenanceSource and SHA-256 provenance logging."
        ),
        annotations=ToolAnnotations(
            title="Ingest Measurement Dataset",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def data_ingest(
        run_id: str | None = None,
        source: ProvenanceSource | None = None,
        rows: list[dict[str, Any]] | None = None,
        file_path: str | None = None,
        dataset_id: str | None = None,
        units: dict[str, str] | None = None,
        notes: str = "",
        label: str | None = None,
    ) -> dict[str, Any]:
        try:
            if (rows is None) == (file_path is None):
                raise ValueError("Provide exactly one of 'rows' or 'file_path'")

            if rows is not None:
                if source is None:
                    raise ValueError(
                        "Explicit ProvenanceSource ('simulator_captured', 'bench_manual', or "
                        "'model_computed') is required when ingesting inline rows"
                    )
                pending_spec = DatasetSpec(
                    dataset_id=dataset_id,
                    source=source,
                    rows=rows,
                    units=units or {},
                    notes=notes,
                )
                workspace = (
                    runtime.workspace_manager.get_run(run_id)
                    if run_id is not None
                    else runtime.workspace_manager.create_run(label=label, source=source)
                )
                ds = _data_ingest(pending_spec, workspace)
            else:
                assert file_path is not None
                if run_id is not None:
                    workspace = runtime.workspace_manager.get_run(run_id)
                    resolved_file = _resolve_confined_input(
                        file_path,
                        (workspace.root, cfg.project_root, cfg.local_assets_dir, cfg.runs_dir),
                        label="file_path",
                        logical_prefixes=logical_roots,
                    )
                else:
                    resolved_file = _resolve_confined_input(
                        file_path,
                        (cfg.project_root, cfg.local_assets_dir, cfg.runs_dir),
                        label="file_path",
                        logical_prefixes=logical_roots,
                    )
                    if not resolved_file.is_file():
                        raise FileNotFoundError(f"Dataset file not found: {resolved_file.name}")
                    workspace = runtime.workspace_manager.create_run(label=label, source=source)
                ds = _data_ingest(
                    resolved_file,
                    workspace,
                    source=source,
                    dataset_id=dataset_id,
                    units=units,
                    notes=notes,
                )

            columns: list[str] = []
            seen_cols: set[str] = set()
            for row in ds.rows:
                for key in row:
                    if key not in seen_cols:
                        seen_cols.add(key)
                        columns.append(key)

            return {
                "run_id": workspace.run_id,
                "dataset_id": ds.dataset_id,
                "source": ds.source,
                "row_count": len(ds.rows),
                "columns": columns,
                "units": ds.units,
                "relpath": f"datasets/{ds.dataset_id}.json",
            }
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="data_analyze",
        title="Analyze Dataset & Run OLS",
        description=(
            "Evaluate safe AST derived column transforms, free or zero-intercept OLS regressions, "
            "reference comparisons, and summary statistics on an ingested run dataset."
        ),
        annotations=ToolAnnotations(
            title="Analyze Dataset & Run OLS",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def data_analyze(
        run_id: str,
        dataset_id: str,
        derived_columns: list[DerivedColumnSpec] | None = None,
        regressions: list[RegressionSpec] | None = None,
        comparisons: list[ComparisonSpec] | None = None,
        persist_derived_id: str | None = None,
    ) -> AnalysisResult:
        try:
            workspace = runtime.workspace_manager.get_run(run_id)
            return _data_analyze(
                dataset_id,
                workspace,
                derived_columns=derived_columns,
                regressions=regressions,
                comparisons=comparisons,
                persist_derived_id=persist_derived_id,
            )
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="calc_eval",
        title="Evaluate Dimensional Physics Calculation",
        description=(
            "Evaluate a safe zero-eval 5-dim SI dimensional physics expression (scalars, "
            "3D vectors, complex AC impedance, Gaussian uncertainty propagation) and optionally "
            "append an immutable calc_id entry to <run>/calculations.jsonl."
        ),
        annotations=ToolAnnotations(
            title="Evaluate Dimensional Physics Calculation",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def calc_eval(
        expression: str,
        variables: dict[str, CalcVariable] | None = None,
        output_unit: str | None = None,
        label: str | None = None,
        run_id: str | None = None,
    ) -> CalcResult:
        try:
            workspace = (
                runtime.workspace_manager.get_run(run_id) if run_id is not None else None
            )
            request = CalcRequest(
                expression=expression,
                variables=variables or {},
                output_unit=output_unit,
                label=label,
            )
            return _calc_eval(request, workspace=workspace)
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    # ------------------------------------------------------------------
    # Group 4: Visuals & Reports (4 tools)
    # ------------------------------------------------------------------

    @server.tool(
        name="plot_render",
        title="Render Scientific Plot",
        description=(
            "Render a scientific PlotSpec (scatter, line, fit_line, errorbar, equal_aspect) "
            "with bundled English/Cyrillic Noto Sans fonts to vector SVG and 300 DPI PNG."
        ),
        annotations=ToolAnnotations(
            title="Render Scientific Plot",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def plot_render(
        run_id: str,
        spec: PlotSpec,
        source: ProvenanceSource = "simulator_captured",
        overwrite: bool = False,
    ) -> ReportFigure:
        try:
            from pea.visuals.plots import render_plot

            workspace = runtime.workspace_manager.get_run(run_id)
            return render_plot(
                spec,
                workspace,
                config=cfg,
                source=source,
                overwrite=overwrite,
            )
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="diagram_render",
        title="Render Circuit or Physics Diagram",
        description=(
            "Render a declarative DiagramSpec (schemdraw circuit schematic, phasor/waveform, "
            "or magnetic/electric field diagram) to vector SVG and 300 DPI PNG."
        ),
        annotations=ToolAnnotations(
            title="Render Circuit or Physics Diagram",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def diagram_render(
        run_id: str,
        spec: DiagramSpec,
        source: ProvenanceSource = "model_computed",
        dpi: int = 300,
        overwrite: bool = False,
    ) -> ReportFigure:
        try:
            from pea.visuals.diagrams import DiagramError, render_diagram

            workspace = runtime.workspace_manager.get_run(run_id)
            try:
                return render_diagram(
                    spec,
                    workspace,
                    config=cfg,
                    source=source,
                    dpi=dpi,
                    overwrite=overwrite,
                )
            except DiagramError as derr:
                raise ToolError(_sanitize_text(str(derr), cfg)) from derr
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="report_build",
        title="Compile Laboratory Report",
        description=(
            "Compile a ReportSpec offline (HTML + KaTeX + Noto Sans) into A4 PDF, editable "
            "DOCX, raw evidence.zip, and optional Teacher Evidence Packet PDF with SHA-256 "
            "provenance logging."
        ),
        annotations=ToolAnnotations(
            title="Compile Laboratory Report",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def report_build(
        run_id: str,
        spec: ReportSpec,
        formats: list[Literal["pdf", "docx", "evidence_zip"]] | None = None,
        include_teacher_packet: bool = False,
        render_previews: bool = True,
        preview_dpi: int = 150,
        profile_paths: list[str] | None = None,
        overwrite: bool = False,
    ) -> ReportBuildResult:
        try:
            from pea.report import build_report

            workspace = runtime.workspace_manager.get_run(run_id)
            resolved_profiles: list[Path] | None = None
            if profile_paths is not None:
                resolved_profiles = [
                    _resolve_confined_input(
                        p,
                        (cfg.profiles_dir, workspace.root, cfg.project_root),
                        label="profile_paths",
                        logical_prefixes=logical_roots,
                    )
                    for p in profile_paths
                ]

            selected_formats: Sequence[str] = (
                _REPORT_FORMATS_DEFAULT if formats is None else formats
            )
            return await build_report(
                spec,
                workspace,
                config=cfg,
                formats=selected_formats,
                include_teacher_packet=include_teacher_packet,
                render_previews=render_previews,
                preview_dpi=preview_dpi,
                profile_paths=resolved_profiles,
                overwrite=overwrite,
            )
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    @server.tool(
        name="report_preview",
        title="Render Report Page Previews",
        description=(
            "Render pages of a compiled PDF report inside a run workspace to PNG images via "
            "pypdfium2 for visual quality inspection."
        ),
        annotations=ToolAnnotations(
            title="Render Report Page Previews",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def report_preview(
        run_id: str,
        pdf_relpath: str = "reports/report.pdf",
        dpi: int = 150,
        max_pages: int = 30,
        pages: list[int] | None = None,
        output_subdir: str = "reports/previews",
        prefix: str = "page",
    ) -> dict[str, Any]:
        try:
            from pea.report.pdf_builder import preview_report_pages

            workspace = runtime.workspace_manager.get_run(run_id)
            png_relpaths = preview_report_pages(
                pdf_relpath,
                workspace,
                dpi=dpi,
                max_pages=max_pages,
                pages=pages,
                output_subdir=output_subdir,
                prefix=prefix,
            )
            return {
                "run_id": workspace.run_id,
                "pdf_relpath": pdf_relpath,
                "page_count": len(png_relpaths),
                "preview_png_relpaths": png_relpaths,
            }
        except Exception as exc:
            _raise_tool_error(exc, cfg)

    _enforce_strict_tool_schemas(server)
    return server


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``pea-mcp`` (runs over stdio with stderr-only logging)."""
    parser = argparse.ArgumentParser(
        prog="pea-mcp",
        description="PEA v2 universal MCP stdio server (zero-API-cost deterministic physics core).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"pea-mcp {__version__}",
    )
    parser.parse_args(argv)

    # Keep stdout strictly reserved for JSON-RPC frames on MCP stdio transport.
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s [%(name)s] %(message)s",
        force=True,
    )

    server = create_mcp_server()
    server.run(transport="stdio")
    return 0


__all__ = [
    "POLICY_URI",
    "UNIVERSAL_TOOL_NAMES",
    "PeaServerRuntime",
    "build_policy_payload",
    "create_mcp_server",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
