"""Frozen Pydantic v2 data contracts for PEA v2 (M1 + M2-ready interfaces)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

BBoxPx = tuple[int, int, int, int]
BBoxNorm = tuple[float, float, float, float]
PointPx = tuple[int, int]

ProvenanceSource = Literal["simulator_captured", "bench_manual", "model_computed"]
RunPhase = Literal["created", "running", "complete", "failed", "cancelled", "interrupted"]
TERMINAL_RUN_PHASES: frozenset[str] = frozenset({"complete", "failed", "cancelled", "interrupted"})

OcrBackendKind = Literal["rapidocr", "macos_vision"]
OcrMode = Literal["rec_only", "det_rec"]
ControlKind = Literal["stepper", "input", "button", "radio", "checkbox", "drag_handle"]
CommitMode = Literal["enter", "step_interior", "click", "blur"]
ParseMode = Literal["float", "int", "text"]
ActionKind = Literal["click", "double_click", "drag", "type", "key", "wait", "set_stepper", "blur"]
SearchMode = Literal["max", "min", "plateau_max", "threshold"]
ReportLanguage = Literal["en", "ru"]


def _validate_sha256(value: str) -> str:
    lowered = value.strip().lower()
    if not _SHA256_RE.match(lowered):
        raise ValueError(f"Expected 64-char hex SHA-256 digest; got {value!r}")
    return lowered


def _validate_bbox_px(bbox: BBoxPx, *, require_non_empty: bool = False) -> BBoxPx:
    x1, y1, x2, y2 = bbox
    if x1 < 0 or y1 < 0:
        raise ValueError(f"Pixel bbox coordinates must be non-negative; got {bbox}")
    if require_non_empty:
        if x2 <= x1 or y2 <= y1:
            raise ValueError(f"Pixel bbox must have positive width and height; got {bbox}")
    else:
        if x2 < x1 or y2 < y1:
            raise ValueError(f"Pixel bbox must satisfy x1 <= x2 and y1 <= y2; got {bbox}")
    return bbox


def _validate_relpath(relpath: str) -> str:
    cleaned = relpath.strip()
    if not cleaned:
        raise ValueError("Relative path must not be empty")
    if cleaned.startswith("/") or cleaned.startswith("\\"):
        raise ValueError(f"Path must be relative, not absolute: {relpath!r}")
    parts = re.split(r"[/\\]", cleaned)
    if ".." in parts:
        raise ValueError(f"Relative path must not contain '..': {relpath!r}")
    return cleaned


class StrictModel(BaseModel):
    """Base model forbidding unknown fields and validating attribute assignments."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------------------
# Provenance & Run Lifecycle
# ---------------------------------------------------------------------------


class RunStatus(StrictModel):
    """Persistent status metadata for an execution run."""

    run_id: str = Field(min_length=1)
    phase: RunPhase = "created"
    created_at: datetime
    updated_at: datetime
    request_id: str | None = None
    label: str | None = None
    source: ProvenanceSource | None = None
    message: str | None = None
    error: str | None = None
    progress_current: int | None = Field(default=None, ge=0)
    progress_total: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProvenanceEntry(StrictModel):
    """Immutable append-only ledger entry recording an artifact's SHA-256 hash."""

    timestamp: datetime
    kind: str = Field(min_length=1)
    relpath: str = Field(min_length=1)
    sha256: str
    size_bytes: int = Field(ge=0)
    source: ProvenanceSource = "simulator_captured"
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("relpath")
    @classmethod
    def _check_relpath(cls, value: str) -> str:
        return _validate_relpath(value)

    @field_validator("sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class ProvenanceVerificationResult(StrictModel):
    """Result of verifying every recorded artifact against ``provenance.jsonl``."""

    ok: bool
    checked_entries: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


# ---------------------------------------------------------------------------
# OCR & Calibration Profiles
# ---------------------------------------------------------------------------


class OcrBox(StrictModel):
    """Single OCR detection/recognition result with pixel and normalized bounds."""

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    bbox_px: BBoxPx
    bbox_norm: BBoxNorm

    @field_validator("bbox_px")
    @classmethod
    def _check_bbox_px(cls, value: BBoxPx) -> BBoxPx:
        return _validate_bbox_px(value, require_non_empty=False)

    @field_validator("bbox_norm")
    @classmethod
    def _check_bbox_norm(cls, value: BBoxNorm) -> BBoxNorm:
        x1, y1, x2, y2 = value
        if not (0.0 <= x1 <= x2 <= 1.0 and 0.0 <= y1 <= y2 <= 1.0):
            raise ValueError(f"Normalized bbox coordinates must lie in [0, 1]; got {value}")
        return value


class OcrResult(StrictModel):
    """Structured output returned by any ``OcrBackend`` implementation."""

    backend: OcrBackendKind
    mode: OcrMode
    elapsed_ms: float = Field(ge=0.0)
    boxes: list[OcrBox] = Field(default_factory=list)


class ControlSpec(StrictModel):
    """Interactive control definition inside a SHA-256 keyed calibration profile."""

    name: str = Field(min_length=1)
    kind: ControlKind
    input_xy: PointPx | None = None
    up_xy: PointPx | None = None
    down_xy: PointPx | None = None
    click_xy: PointPx | None = None
    readout_roi_px: BBoxPx | None = None
    unit: str | None = None
    step: float | None = Field(default=None, gt=0.0)
    min_val: float | None = None
    max_val: float | None = None
    decimal_comma: bool = False
    commit_mode: CommitMode = "enter"
    blur_xy: PointPx | None = None

    @field_validator("readout_roi_px")
    @classmethod
    def _check_readout_roi(cls, value: BBoxPx | None) -> BBoxPx | None:
        if value is not None:
            return _validate_bbox_px(value, require_non_empty=True)
        return None

    @model_validator(mode="after")
    def _check_bounds(self) -> ControlSpec:
        if self.min_val is not None and self.max_val is not None and self.min_val > self.max_val:
            raise ValueError(f"min_val ({self.min_val}) must be <= max_val ({self.max_val})")
        return self


class ReadoutSpec(StrictModel):
    """Readout crop & OCR parsing specification inside a calibration profile."""

    name: str = Field(min_length=1)
    roi_px: BBoxPx
    ocr_mode: OcrMode = "rec_only"
    pad_px: int = Field(default=0, ge=0)
    scale: float = Field(default=1.0, gt=0.0)
    parse_mode: ParseMode = "float"
    unit_pattern: str | None = None
    normalize_homoglyphs: bool = True

    @field_validator("roi_px")
    @classmethod
    def _check_roi(cls, value: BBoxPx) -> BBoxPx:
        return _validate_bbox_px(value, require_non_empty=True)


class CalibrationProfile(StrictModel):
    """SHA-256 keyed simulator control & readout calibration map."""

    schema_version: Literal[1] = 1
    swf_sha256: str
    stage_width: int = Field(gt=0)
    stage_height: int = Field(gt=0)
    header_height_px: int = Field(default=39, ge=0)
    controls: dict[str, ControlSpec] = Field(default_factory=dict)
    readouts: dict[str, ReadoutSpec] = Field(default_factory=dict)
    blur_xy: PointPx | None = None
    verified_at: datetime | None = None
    notes: str = ""

    @field_validator("swf_sha256")
    @classmethod
    def _check_swf_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


# ---------------------------------------------------------------------------
# Simulator Primitives (M1 + M2 Stubs)
# ---------------------------------------------------------------------------


class SwfMeta(StrictModel):
    """Metadata parsed directly from an SWF (FWS/CWS/ZWS) binary header."""

    path: str = Field(min_length=1)
    sha256: str
    swf_version: int = Field(ge=1)
    compressed: bool
    signature: Literal["FWS", "CWS", "ZWS"] = "CWS"
    stage_width: int = Field(gt=0)
    stage_height: int = Field(gt=0)
    fps: float = Field(gt=0.0)
    frame_count: int = Field(ge=0)

    @field_validator("sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class ActionStep(StrictModel):
    """Single atomic interaction step for ``sim_act``."""

    action: ActionKind
    x: int | None = None
    y: int | None = None
    to_x: int | None = None
    to_y: int | None = None
    text: str | None = None
    key: str | None = None
    wait_ms: int | None = Field(default=None, ge=0)
    control: str | None = None
    value: float | int | str | None = None
    select_all: bool = True
    commit_mode: CommitMode | None = None

    @model_validator(mode="after")
    def _check_action_args(self) -> ActionStep:
        if self.action in ("click", "double_click"):
            has_xy = self.x is not None and self.y is not None
            if not has_xy and not self.control:
                raise ValueError(f"action={self.action!r} requires (x, y) or control")
        elif self.action == "drag":
            if None in (self.x, self.y, self.to_x, self.to_y):
                raise ValueError("action='drag' requires x, y, to_x, and to_y")
        elif self.action == "type":
            if self.text is None:
                raise ValueError("action='type' requires text")
        elif self.action == "key":
            if not self.key:
                raise ValueError("action='key' requires key")
        elif self.action == "wait":
            if self.wait_ms is None:
                raise ValueError("action='wait' requires wait_ms")
        elif self.action == "set_stepper":
            if not self.control or self.value is None:
                raise ValueError("action='set_stepper' requires control and value")
        return self


class SweepAxis(StrictModel):
    """Single parameter axis for a deterministic grid sweep."""

    control: str = Field(min_length=1)
    values: list[float | int | str] = Field(min_length=1)


class SweepSpec(StrictModel):
    """Specification for an N-dimensional simulator parameter sweep."""

    fixed_controls: dict[str, float | int | str] = Field(default_factory=dict)
    axes: list[SweepAxis] = Field(min_length=1)
    readouts: list[str] = Field(min_length=1)
    settle_ms: int = Field(default=150, ge=0)
    verify_controls: bool = True
    blur_before_capture: bool = True
    max_points: int = Field(default=500, ge=1)


class SearchSpec(StrictModel):
    """Specification for closed-loop extremum, wide-plateau, or threshold search."""

    mode: SearchMode
    fixed_controls: dict[str, float | int | str] = Field(default_factory=dict)
    sweep_control: str = Field(min_length=1)
    readout: str = Field(min_length=1)
    start: float
    step: float
    max_steps: int = Field(default=120, ge=1)
    stop_after_drop: int = Field(default=2, ge=1)
    target_value: float | None = None
    tolerance: float = Field(default=0.0, ge=0.0)
    settle_ms: int = Field(default=150, ge=0)
    verify_controls: bool = True
    blur_before_capture: bool = True

    @model_validator(mode="after")
    def _check_search_params(self) -> SearchSpec:
        if self.step == 0:
            raise ValueError("SearchSpec.step must be non-zero")
        if self.mode == "threshold" and self.target_value is None:
            raise ValueError("SearchSpec.target_value is required when mode='threshold'")
        return self


class MeasurementPoint(StrictModel):
    """Single captured measurement point with raw OCR text and screenshot hash."""

    index: int = Field(default=0, ge=0)
    requested_settings: dict[str, float | int | str] = Field(default_factory=dict)
    observed_settings: dict[str, float | int | str | None] = Field(default_factory=dict)
    readout_name: str | None = None
    raw_ocr_text: str = ""
    parsed_value: float | int | str | None = None
    parsed_unit: str | None = None
    readouts: dict[str, float | int | str | None] = Field(default_factory=dict)
    readout_units: dict[str, str | None] = Field(default_factory=dict)
    raw_readouts: dict[str, str] = Field(default_factory=dict)
    screenshot_relpath: str = Field(min_length=1)
    screenshot_sha256: str
    provenance: ProvenanceSource = "simulator_captured"
    captured_at: datetime | None = None
    controls_matched: bool | None = None

    @field_validator("screenshot_relpath")
    @classmethod
    def _check_relpath(cls, value: str) -> str:
        return _validate_relpath(value)

    @field_validator("screenshot_sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class PlateauResult(StrictModel):
    """Detected discrete extremum or wide-plateau interval with evidence trace."""

    control: str = Field(min_length=1)
    readout: str = Field(min_length=1)
    peak_value: float
    low_bound: float
    high_bound: float
    midpoint: float
    width_steps: int = Field(ge=1)
    trace: list[MeasurementPoint] = Field(default_factory=list)
    boundary_screenshots: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_bounds(self) -> PlateauResult:
        if not (self.low_bound <= self.midpoint <= self.high_bound):
            raise ValueError(
                f"Expected low_bound <= midpoint <= high_bound; got "
                f"({self.low_bound}, {self.midpoint}, {self.high_bound})"
            )
        return self


class WatchSpec(StrictModel):
    """M2 stub: timed transient / stopwatch capture specification."""

    readouts: list[str] = Field(min_length=1)
    duration_s: float = Field(gt=0.0)
    interval_ms: int = Field(default=250, ge=10)
    trigger_action: ActionStep | None = None
    stop_when_stable_s: float | None = Field(default=None, gt=0.0)
    fixed_controls: dict[str, float | int | str] = Field(default_factory=dict)


class ComputeSimSpec(StrictModel):
    """M2 stub: sandboxed numerical/ODE modeling specification tagged ``model_computed``."""

    model_kind: Literal["ode_ivp", "algebraic_grid", "fft_spectrum"]
    expression: str = Field(min_length=1)
    parameters: dict[str, float] = Field(default_factory=dict)
    grid_variable: str = Field(min_length=1)
    grid_start: float
    grid_stop: float
    grid_points: int = Field(default=200, ge=2, le=100_000)
    initial_state: list[float] = Field(default_factory=list)
    provenance: Literal["model_computed"] = "model_computed"


# ---------------------------------------------------------------------------
# Math, Analysis, Visuals & Reports
# ---------------------------------------------------------------------------


class CalcVariable(StrictModel):
    """Typed physical variable input for the 5-dim SI dimensional calculator."""

    value: float | list[float]
    imag: float | None = None
    unit: str = "1"
    uncertainty: float | None = Field(default=None, ge=0.0)


class CalcRequest(StrictModel):
    """Safe AST dimensional calculation request."""

    expression: str = Field(min_length=1)
    variables: dict[str, CalcVariable] = Field(default_factory=dict)
    output_unit: str | None = None
    label: str | None = None


class CalcResult(StrictModel):
    """Immutable dimensional calculation record persisted in the calc ledger."""

    calc_id: str = Field(min_length=1)
    expression: str = Field(min_length=1)
    si_value: float | list[float]
    si_imag: float | None = None
    si_dims: tuple[int, int, int, int, int]
    output_value: float | list[float]
    output_imag: float | None = None
    output_unit: str = Field(min_length=1)
    uncertainty_si: float | None = Field(default=None, ge=0.0)
    provenance: Literal["model_computed"] = "model_computed"


class DatasetSpec(StrictModel):
    """Structured measurement dataset ingested from simulator runs or bench data."""

    dataset_id: str | None = None
    source: ProvenanceSource
    rows: list[dict[str, Any]] = Field(min_length=1)
    units: dict[str, str] = Field(default_factory=dict)
    notes: str = ""


class DerivedColumnSpec(StrictModel):
    """Safe column transformation formula evaluated over a dataset."""

    name: str = Field(min_length=1)
    formula: str = Field(min_length=1)
    unit: str = "1"


class RegressionSpec(StrictModel):
    """Ordinary least-squares linear regression specification (free or zero intercept)."""

    name: str = Field(min_length=1)
    x_column: str = Field(min_length=1)
    y_column: str = Field(min_length=1)
    intercept: Literal["free", "zero"] = "free"
    group_by: str | None = None


class ComparisonSpec(StrictModel):
    """Quantitative comparison between a measured quantity and a reference value."""

    name: str = Field(min_length=1)
    measured: float
    reference: float
    unit: str = "1"


class RegressionOutput(StrictModel):
    """Computed OLS regression statistics."""

    name: str = Field(min_length=1)
    group: str | None = None
    intercept_mode: Literal["free", "zero"]
    slope: float
    slope_se: float = Field(ge=0.0)
    intercept: float
    intercept_se: float | None = Field(default=None, ge=0.0)
    r_squared: float
    n_points: int = Field(ge=2)


class ComparisonOutput(StrictModel):
    """Computed deviation between a measured quantity and a reference value."""

    name: str = Field(min_length=1)
    measured: float
    reference: float
    abs_diff: float
    percent_deviation: float | None = None
    unit: str = "1"


class AnalysisResult(StrictModel):
    """Aggregated deterministic data analysis output."""

    dataset_id: str = Field(min_length=1)
    source: ProvenanceSource
    row_count: int = Field(ge=0)
    columns: list[str]
    derived_columns: list[str] = Field(default_factory=list)
    regressions: list[RegressionOutput] = Field(default_factory=list)
    comparisons: list[ComparisonOutput] = Field(default_factory=list)
    summary_stats: dict[str, dict[str, float]] = Field(default_factory=dict)


class PlotSeries(StrictModel):
    """Single plotted series (scatter, curve, linear fit, or errorbar)."""

    label: str
    x: list[float] = Field(min_length=1)
    y: list[float] = Field(min_length=1)
    x_err: list[float] | None = None
    y_err: list[float] | None = None
    kind: Literal["scatter", "line", "fit_line", "errorbar"] = "scatter"
    color: str | None = None
    marker: str | None = None
    linestyle: str | None = None

    @model_validator(mode="after")
    def _check_lengths(self) -> PlotSeries:
        n = len(self.x)
        if len(self.y) != n:
            raise ValueError(f"PlotSeries x ({n}) and y ({len(self.y)}) lengths must match")
        if self.x_err is not None and len(self.x_err) != n:
            raise ValueError(f"PlotSeries x_err ({len(self.x_err)}) length must match x ({n})")
        if self.y_err is not None and len(self.y_err) != n:
            raise ValueError(f"PlotSeries y_err ({len(self.y_err)}) length must match y ({n})")
        return self


class PlotSpec(StrictModel):
    """Specification for vector SVG + 300 DPI PNG scientific plots."""

    plot_id: str = Field(min_length=1)
    title: str
    x_label: str
    y_label: str
    series: list[PlotSeries] = Field(min_length=1)
    annotations: list[str] = Field(default_factory=list)
    grid: bool = True
    legend: bool = True
    equal_aspect: bool = False
    width_in: float = Field(default=6.5, gt=0.0)
    height_in: float = Field(default=4.2, gt=0.0)
    dpi: int = Field(default=300, ge=72, le=600)


class DiagramElement(StrictModel):
    """Element inside a schematic circuit, phasor, or field diagram."""

    kind: str = Field(min_length=1)
    label: str = ""
    direction: Literal["right", "left", "up", "down"] = "right"
    length: float | None = Field(default=None, gt=0.0)
    at: tuple[float, float] | None = None
    to: tuple[float, float] | None = None
    style: dict[str, Any] = Field(default_factory=dict)


class DiagramSpec(StrictModel):
    """Specification for ``schemdraw`` circuit schematics and parametric SVGs."""

    diagram_id: str = Field(min_length=1)
    kind: Literal["circuit", "phasor", "field"]
    title: str = ""
    elements: list[DiagramElement] = Field(min_length=1)
    notes: list[str] = Field(default_factory=list)


class ReportSectionOptions(StrictModel):
    """Modular opt-in report section toggles."""

    include_self_control: bool = False
    include_problems: bool = False
    include_evidence_appendix: bool = False


class SelfControlItem(StrictModel):
    """Single self-control Q&A item for optional report inclusion."""

    number: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    equations: list[str] = Field(default_factory=list)
    figure_ids: list[str] = Field(default_factory=list)


class ProblemItem(StrictModel):
    """Single worked additional problem item for optional report inclusion."""

    number: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    solution: str = Field(min_length=1)
    calc_ids: list[str] = Field(default_factory=list)
    figure_ids: list[str] = Field(default_factory=list)


class ReportTable(StrictModel):
    """Academic report data table with column consistency enforcement."""

    table_id: str = Field(min_length=1)
    caption: str = Field(min_length=1)
    headers: list[str] = Field(min_length=1)
    rows: list[list[str]]
    notes: str | None = None
    source: ProvenanceSource = "simulator_captured"

    @model_validator(mode="after")
    def _check_row_widths(self) -> ReportTable:
        width = len(self.headers)
        for idx, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(
                    f"Table {self.table_id!r} row {idx} has {len(row)} cells; expected {width}"
                )
        return self


class ReportFigure(StrictModel):
    """Academic report figure pointing to SVG and/or 300 DPI PNG run artifacts."""

    figure_id: str = Field(min_length=1)
    caption: str = Field(min_length=1)
    svg_relpath: str | None = None
    png_relpath: str | None = None
    source: ProvenanceSource = "simulator_captured"

    @field_validator("svg_relpath", "png_relpath")
    @classmethod
    def _check_relpaths(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_relpath(value)
        return None

    @model_validator(mode="after")
    def _check_has_asset(self) -> ReportFigure:
        if not self.svg_relpath and not self.png_relpath:
            raise ValueError("ReportFigure requires at least one of svg_relpath or png_relpath")
        return self


class ReportSpec(StrictModel):
    """Complete submission-ready academic laboratory report specification."""

    title: str = Field(min_length=1)
    subtitle: str | None = None
    lab_number: str | None = None
    language: ReportLanguage = "en"
    author: str | None = None
    team: str | None = None
    course: str | None = None
    institution: str | None = None
    date_str: str | None = None
    objective: str = ""
    apparatus: list[str] = Field(default_factory=list)
    theory_sections: list[dict[str, str]] = Field(default_factory=list)
    procedure: list[str] = Field(default_factory=list)
    tables: list[ReportTable] = Field(default_factory=list)
    figures: list[ReportFigure] = Field(default_factory=list)
    analysis_paragraphs: list[str] = Field(default_factory=list)
    conclusions: list[str] = Field(default_factory=list)
    calc_ids: list[str] = Field(default_factory=list)
    options: ReportSectionOptions = Field(default_factory=ReportSectionOptions)
    self_control: list[SelfControlItem] = Field(default_factory=list)
    problems: list[ProblemItem] = Field(default_factory=list)


class ReportBuildResult(StrictModel):
    """Compiled report output artifacts with SHA-256 hashes and page previews."""

    run_id: str = Field(min_length=1)
    pdf_relpath: str | None = None
    pdf_sha256: str | None = None
    docx_relpath: str | None = None
    docx_sha256: str | None = None
    evidence_zip_relpath: str | None = None
    evidence_zip_sha256: str | None = None
    teacher_packet_relpath: str | None = None
    teacher_packet_sha256: str | None = None
    preview_png_relpaths: list[str] = Field(default_factory=list)
    page_count: int = Field(default=0, ge=0)

    @field_validator("pdf_sha256", "docx_sha256", "evidence_zip_sha256", "teacher_packet_sha256")
    @classmethod
    def _check_optional_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_sha256(value)
        return None


__all__ = [
    "ActionKind",
    "ActionStep",
    "AnalysisResult",
    "BBoxNorm",
    "BBoxPx",
    "CalcRequest",
    "CalcResult",
    "CalcVariable",
    "CalibrationProfile",
    "CommitMode",
    "ComparisonOutput",
    "ComparisonSpec",
    "ComputeSimSpec",
    "ControlKind",
    "ControlSpec",
    "DatasetSpec",
    "DerivedColumnSpec",
    "DiagramElement",
    "DiagramSpec",
    "MeasurementPoint",
    "OcrBackendKind",
    "OcrBox",
    "OcrMode",
    "OcrResult",
    "ParseMode",
    "PlateauResult",
    "PlotSeries",
    "PlotSpec",
    "PointPx",
    "ProblemItem",
    "ProvenanceEntry",
    "ProvenanceSource",
    "ProvenanceVerificationResult",
    "ReadoutSpec",
    "RegressionOutput",
    "RegressionSpec",
    "ReportBuildResult",
    "ReportFigure",
    "ReportLanguage",
    "ReportSectionOptions",
    "ReportSpec",
    "ReportTable",
    "RunPhase",
    "RunStatus",
    "SearchMode",
    "SearchSpec",
    "SelfControlItem",
    "StrictModel",
    "SweepAxis",
    "SweepSpec",
    "SwfMeta",
    "TERMINAL_RUN_PHASES",
    "WatchSpec",
]
