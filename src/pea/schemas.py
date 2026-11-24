"""Frozen Pydantic v2 data contracts for PEA v2 (M1 + M2 interfaces)."""

from __future__ import annotations

import keyword
import math
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATASET_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

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
WatchStopOp = Literal["le", "lt", "ge", "gt"]
WatchPacingMode = Literal["burst_post_ocr", "inline_ocr"]
WatchStatus = Literal[
    "complete", "stopped_stable", "stopped_threshold", "max_frames_reached", "cancelled"
]
ComputeModelKind = Literal["ode_ivp", "algebraic_grid", "fft_spectrum"]
OdeMethod = Literal["RK45", "Radau", "BDF"]
FftWindow = Literal["none", "hann"]
ReportLanguage = Literal["en", "ru"]


def _validate_sha256(value: str) -> str:
    lowered = value.strip().lower()
    if not _SHA256_RE.match(lowered):
        raise ValueError(f"Expected 64-char hex SHA-256 digest; got {value!r}")
    return lowered


def _validate_finite(value: float, field_name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite; got {value!r}")
    return value


def _validate_identifier(name: str, context: str) -> str:
    if not name or not name.isidentifier() or keyword.iskeyword(name) or "__" in name:
        raise ValueError(
            f"{context} must be a valid non-keyword Python identifier without '__'; got {name!r}"
        )
    return name


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


def _validate_dataset_id(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned in (".", "..") or not _DATASET_ID_RE.match(cleaned):
        raise ValueError(
            f"{field_name} must be a non-empty confined identifier matching "
            f"[A-Za-z0-9._-]+ (not '.' or '..'); got {value!r}"
        )
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
# Simulator Primitives (M1 + M2)
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
    elapsed_s: float | None = Field(default=None, ge=0.0)
    frame_timestamp_ms: float | None = Field(default=None, ge=0.0)
    controls_matched: bool | None = None

    @field_validator("screenshot_relpath")
    @classmethod
    def _check_relpath(cls, value: str) -> str:
        return _validate_relpath(value)

    @field_validator("screenshot_sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("elapsed_s", "frame_timestamp_ms")
    @classmethod
    def _check_optional_finite_timing(
        cls, value: float | None, info: ValidationInfo
    ) -> float | None:
        if value is not None:
            return _validate_finite(value, info.field_name or "timing")
        return None


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
    """Timed transient / stopwatch capture specification for ``sim_watch``."""

    readouts: list[str] = Field(min_length=1)
    duration_s: float = Field(gt=0.0)
    interval_ms: int = Field(default=250, ge=10)
    trigger_action: ActionStep | None = None
    trigger_actions: list[ActionStep] = Field(default_factory=list)
    stop_when_stable_s: float | None = Field(default=None, gt=0.0)
    stability_tol: float = Field(default=0.0, ge=0.0)
    stop_readout: str | None = None
    stop_op: WatchStopOp | None = None
    stop_value: float | None = None
    fixed_controls: dict[str, float | int | str] = Field(default_factory=dict)
    max_frames: int = Field(default=500, ge=1, le=2000)
    blur_before_capture: bool = True
    settle_ms: int = Field(default=0, ge=0, le=30_000)
    save_dataset: bool = True
    dataset_id: str | None = None

    @field_validator("dataset_id")
    @classmethod
    def _check_dataset_id(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_dataset_id(value, "WatchSpec.dataset_id")
        return None

    @model_validator(mode="after")
    def _check_watch_spec(self) -> WatchSpec:
        for idx, readout in enumerate(self.readouts):
            if not readout.strip():
                raise ValueError(f"WatchSpec.readouts[{idx}] must not be empty or whitespace")
        if len(set(self.readouts)) != len(self.readouts):
            raise ValueError(f"WatchSpec.readouts must not contain duplicates; got {self.readouts}")

        _validate_finite(self.duration_s, "WatchSpec.duration_s")
        _validate_finite(self.stability_tol, "WatchSpec.stability_tol")
        if self.stop_when_stable_s is not None:
            _validate_finite(self.stop_when_stable_s, "WatchSpec.stop_when_stable_s")
        if self.stop_value is not None:
            _validate_finite(self.stop_value, "WatchSpec.stop_value")

        for ctrl, val in self.fixed_controls.items():
            if not ctrl.strip():
                raise ValueError("WatchSpec.fixed_controls keys must not be empty or whitespace")
            if isinstance(val, float):
                _validate_finite(val, f"WatchSpec.fixed_controls[{ctrl!r}]")

        if self.trigger_action is not None and self.trigger_actions:
            raise ValueError(
                "WatchSpec cannot specify both 'trigger_action' and non-empty 'trigger_actions'"
            )

        threshold_flags = (
            self.stop_readout is not None,
            self.stop_op is not None,
            self.stop_value is not None,
        )
        if any(threshold_flags) and not all(threshold_flags):
            raise ValueError(
                "WatchSpec requires 'stop_readout', 'stop_op', and 'stop_value' to all be "
                "specified together or all omitted"
            )
        if self.stop_readout is not None and self.stop_readout not in self.readouts:
            raise ValueError(
                f"WatchSpec.stop_readout ({self.stop_readout!r}) must be one of "
                f"WatchSpec.readouts ({self.readouts!r})"
            )

        if self.dataset_id is not None and not self.save_dataset:
            raise ValueError("WatchSpec.dataset_id cannot be set when save_dataset is False")

        return self


class WatchResult(StrictModel):
    """Structured result returned by ``sim_watch`` timed transient capture."""

    status: WatchStatus
    pacing_mode: WatchPacingMode
    requested_duration_s: float = Field(gt=0.0)
    actual_duration_s: float = Field(ge=0.0)
    interval_ms: int = Field(ge=10)
    captured_frames: int = Field(ge=0)
    missing_readouts: int = Field(default=0, ge=0)
    stop_reason: str
    readings_relpath: str = Field(min_length=1)
    dataset_id: str | None = None
    dataset_relpath: str | None = None
    points: list[MeasurementPoint] = Field(default_factory=list)

    @field_validator("requested_duration_s", "actual_duration_s")
    @classmethod
    def _check_finite_durations(cls, value: float, info: ValidationInfo) -> float:
        return _validate_finite(value, info.field_name or "duration")

    @field_validator("readings_relpath")
    @classmethod
    def _check_readings_relpath(cls, value: str) -> str:
        return _validate_relpath(value)

    @field_validator("dataset_id")
    @classmethod
    def _check_dataset_id(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_dataset_id(value, "WatchResult.dataset_id")
        return None

    @field_validator("dataset_relpath")
    @classmethod
    def _check_dataset_relpath(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_relpath(value)
        return None

    @model_validator(mode="after")
    def _check_watch_result(self) -> WatchResult:
        if self.captured_frames != len(self.points):
            raise ValueError(
                f"WatchResult.captured_frames ({self.captured_frames}) must equal "
                f"len(points) ({len(self.points)})"
            )
        if (self.dataset_id is None) != (self.dataset_relpath is None):
            raise ValueError(
                "WatchResult requires 'dataset_id' and 'dataset_relpath' to both be "
                "specified or both omitted"
            )
        return self


class ComputeSimSpec(StrictModel):
    """Sandboxed numerical, ODE, and FFT modeling specification tagged ``model_computed``."""

    model_kind: ComputeModelKind
    expression: str | None = None
    expressions: list[str] = Field(default_factory=list)
    state_variables: list[str] = Field(default_factory=list)
    parameters: dict[str, float] = Field(default_factory=dict)
    grid_variable: str = Field(default="t", min_length=1)
    grid_start: float | None = None
    grid_stop: float | None = None
    grid_points: int = Field(default=200, ge=2, le=50_000)
    initial_state: list[float] = Field(default_factory=list)
    ode_method: OdeMethod = "RK45"
    rtol: float = Field(default=1e-6, gt=0.0, le=1e-1)
    atol: float = Field(default=1e-9, gt=0.0, le=1e-1)
    max_nfev: int = Field(default=50_000, ge=10, le=250_000)
    timeout_s: float = Field(default=5.0, gt=0.0, le=30.0)
    input_dataset_id: str | None = None
    time_column: str | None = None
    signal_column: str | None = None
    window: FftWindow = "none"
    detrend_mean: bool = True
    dataset_id: str | None = None
    units: dict[str, str] = Field(default_factory=dict)
    notes: str = ""
    provenance: Literal["model_computed"] = "model_computed"

    @field_validator("dataset_id", "input_dataset_id")
    @classmethod
    def _check_dataset_ids(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None:
            return _validate_dataset_id(value, f"ComputeSimSpec.{info.field_name}")
        return None

    @model_validator(mode="after")
    def _check_compute_spec(self) -> ComputeSimSpec:
        _validate_finite(self.rtol, "ComputeSimSpec.rtol")
        _validate_finite(self.atol, "ComputeSimSpec.atol")
        _validate_finite(self.timeout_s, "ComputeSimSpec.timeout_s")
        _validate_identifier(self.grid_variable, "ComputeSimSpec.grid_variable")

        if self.grid_start is not None:
            _validate_finite(self.grid_start, "ComputeSimSpec.grid_start")
        if self.grid_stop is not None:
            _validate_finite(self.grid_stop, "ComputeSimSpec.grid_stop")

        for key, val in self.parameters.items():
            _validate_identifier(key, f"ComputeSimSpec.parameters[{key!r}]")
            _validate_finite(val, f"ComputeSimSpec.parameters[{key!r}]")

        for idx, val in enumerate(self.initial_state):
            _validate_finite(val, f"ComputeSimSpec.initial_state[{idx}]")

        for col_name in self.units:
            if not col_name.strip():
                raise ValueError("ComputeSimSpec.units keys must not be empty or whitespace")

        if self.expression is not None and not self.expression.strip():
            raise ValueError("ComputeSimSpec.expression must not be empty or whitespace")
        for idx, expr in enumerate(self.expressions):
            if not expr.strip():
                raise ValueError(f"ComputeSimSpec.expressions[{idx}] must not be empty")
        if self.expression is not None and self.expressions:
            raise ValueError(
                "ComputeSimSpec cannot specify both 'expression' and non-empty 'expressions'"
            )

        has_dataset_inputs = any(
            v is not None for v in (self.input_dataset_id, self.time_column, self.signal_column)
        )

        if self.model_kind == "ode_ivp":
            if has_dataset_inputs:
                raise ValueError(
                    "input_dataset_id, time_column, and signal_column are only valid for "
                    "model_kind='fft_spectrum'"
                )
            if self.grid_start is None or self.grid_stop is None:
                raise ValueError("ode_ivp requires both 'grid_start' and 'grid_stop'")
            if self.grid_stop <= self.grid_start:
                raise ValueError(
                    f"ode_ivp requires grid_stop > grid_start; got "
                    f"({self.grid_start}, {self.grid_stop})"
                )
            n_dim = len(self.initial_state)
            if n_dim == 0:
                raise ValueError("ode_ivp requires non-empty 'initial_state'")
            if n_dim > 16:
                raise ValueError(f"ode_ivp supports at most 16 state variables; got {n_dim}")
            if self.grid_variable in self.parameters:
                raise ValueError(
                    f"grid_variable ({self.grid_variable!r}) must not collide with parameters"
                )

            should_default_state_vars = False
            if self.expression is not None:
                if n_dim != 1:
                    raise ValueError(
                        f"ode_ivp with single 'expression' requires len(initial_state) == 1; "
                        f"got {n_dim}"
                    )
                if not self.state_variables:
                    effective_state_vars = ["y"]
                    should_default_state_vars = True
                elif len(self.state_variables) != 1:
                    raise ValueError(
                        "ode_ivp with single 'expression' accepts at most one state_variable"
                    )
                else:
                    effective_state_vars = self.state_variables
            elif self.expressions:
                if len(self.expressions) != n_dim:
                    raise ValueError(
                        f"ode_ivp requires len(expressions) ({len(self.expressions)}) == "
                        f"len(initial_state) ({n_dim})"
                    )
                if len(self.state_variables) != n_dim:
                    raise ValueError(
                        f"ode_ivp with 'expressions' requires len(state_variables) "
                        f"({len(self.state_variables)}) == len(initial_state) ({n_dim})"
                    )
                effective_state_vars = self.state_variables
            else:
                raise ValueError("ode_ivp requires either 'expression' or 'expressions'")

            seen_vars: set[str] = set()
            for idx, var in enumerate(effective_state_vars):
                _validate_identifier(var, f"ComputeSimSpec.state_variables[{idx}]")
                if var in seen_vars:
                    raise ValueError(f"Duplicate state variable {var!r} in state_variables")
                if var == self.grid_variable:
                    raise ValueError(
                        f"state_variable {var!r} must be distinct from grid_variable"
                    )
                if var in self.parameters:
                    raise ValueError(
                        f"state_variable {var!r} must be distinct from parameters"
                    )
                seen_vars.add(var)

            if should_default_state_vars:
                object.__setattr__(self, "state_variables", ["y"])

        elif self.model_kind == "algebraic_grid":
            if has_dataset_inputs:
                raise ValueError(
                    "input_dataset_id, time_column, and signal_column are only valid for "
                    "model_kind='fft_spectrum'"
                )
            if self.initial_state:
                raise ValueError("initial_state is only valid for model_kind='ode_ivp'")
            if self.grid_start is None or self.grid_stop is None:
                raise ValueError("algebraic_grid requires both 'grid_start' and 'grid_stop'")
            if self.grid_stop == self.grid_start:
                raise ValueError(
                    f"algebraic_grid requires grid_stop != grid_start; got {self.grid_start}"
                )
            if self.grid_variable in self.parameters:
                raise ValueError(
                    f"grid_variable ({self.grid_variable!r}) must not collide with parameters"
                )

            if self.expression is not None:
                if len(self.state_variables) > 1:
                    raise ValueError(
                        "algebraic_grid with single 'expression' accepts at most one state_variable"
                    )
                output_cols = self.state_variables if self.state_variables else ["y"]
            elif self.expressions:
                if len(self.expressions) > 16:
                    raise ValueError(
                        f"algebraic_grid supports at most 16 expressions; got "
                        f"{len(self.expressions)}"
                    )
                if len(self.state_variables) != len(self.expressions):
                    raise ValueError(
                        f"algebraic_grid with 'expressions' requires len(state_variables) "
                        f"({len(self.state_variables)}) == len(expressions) "
                        f"({len(self.expressions)})"
                    )
                output_cols = self.state_variables
            else:
                raise ValueError("algebraic_grid requires either 'expression' or 'expressions'")

            seen_cols: set[str] = set()
            for idx, col in enumerate(output_cols):
                _validate_identifier(col, f"ComputeSimSpec.state_variables[{idx}]")
                if col in seen_cols:
                    raise ValueError(f"Duplicate output column {col!r} in state_variables")
                if col == self.grid_variable:
                    raise ValueError(
                        f"Output column {col!r} must be distinct from grid_variable"
                    )
                seen_cols.add(col)

        elif self.model_kind == "fft_spectrum":
            if self.initial_state:
                raise ValueError("initial_state is only valid for model_kind='ode_ivp'")
            if self.state_variables:
                raise ValueError(
                    "state_variables is not valid for model_kind='fft_spectrum'"
                )
            if has_dataset_inputs:
                if (
                    not (self.input_dataset_id and self.input_dataset_id.strip())
                    or not (self.time_column and self.time_column.strip())
                    or not (self.signal_column and self.signal_column.strip())
                ):
                    raise ValueError(
                        "Dataset-backed fft_spectrum requires non-empty 'input_dataset_id', "
                        "'time_column', and 'signal_column'"
                    )
                if self.expression is not None or self.expressions:
                    raise ValueError(
                        "Dataset-backed fft_spectrum must not specify 'expression' or 'expressions'"
                    )
                if self.grid_start is not None or self.grid_stop is not None:
                    raise ValueError(
                        "Dataset-backed fft_spectrum must not specify 'grid_start' or 'grid_stop'"
                    )
                if self.parameters:
                    raise ValueError(
                        "Dataset-backed fft_spectrum must not specify 'parameters'"
                    )
            else:
                if self.expressions:
                    raise ValueError(
                        "Inline fft_spectrum requires a single 'expression', not 'expressions'"
                    )
                if self.expression is None:
                    raise ValueError(
                        "fft_spectrum requires either dataset inputs "
                        "('input_dataset_id', 'time_column', 'signal_column') or inline "
                        "'expression' with 'grid_start' and 'grid_stop'"
                    )
                if self.grid_start is None or self.grid_stop is None:
                    raise ValueError(
                        "Inline fft_spectrum requires both 'grid_start' and 'grid_stop'"
                    )
                if self.grid_stop <= self.grid_start:
                    raise ValueError(
                        f"Inline fft_spectrum requires grid_stop > grid_start; got "
                        f"({self.grid_start}, {self.grid_stop})"
                    )
                if self.grid_points < 4:
                    raise ValueError(
                        f"Inline fft_spectrum requires grid_points >= 4; got {self.grid_points}"
                    )
                if self.grid_variable in self.parameters:
                    raise ValueError(
                        f"grid_variable ({self.grid_variable!r}) must not collide with parameters"
                    )

        return self


class ComputeSimResult(StrictModel):
    """Structured output returned by ``compute_sim``, unconditionally tagged ``model_computed``."""

    model_kind: ComputeModelKind
    provenance: Literal["model_computed"] = "model_computed"
    dataset_id: str = Field(min_length=1)
    dataset_relpath: str = Field(min_length=1)
    dataset_sha256: str
    row_count: int = Field(ge=0)
    columns: list[str] = Field(min_length=1)
    units: dict[str, str] = Field(default_factory=dict)
    solver_meta: dict[str, Any] = Field(default_factory=dict)
    preview_rows: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("dataset_id")
    @classmethod
    def _check_dataset_id(cls, value: str) -> str:
        return _validate_dataset_id(value, "ComputeSimResult.dataset_id")

    @field_validator("dataset_relpath")
    @classmethod
    def _check_dataset_relpath(cls, value: str) -> str:
        return _validate_relpath(value)

    @field_validator("dataset_sha256")
    @classmethod
    def _check_dataset_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @model_validator(mode="after")
    def _check_compute_result(self) -> ComputeSimResult:
        for idx, col in enumerate(self.columns):
            if not col.strip():
                raise ValueError(
                    f"ComputeSimResult.columns[{idx}] must not be empty or whitespace"
                )
        if len(set(self.columns)) != len(self.columns):
            raise ValueError(
                f"ComputeSimResult.columns must not contain duplicates; got {self.columns}"
            )
        if len(self.preview_rows) > self.row_count:
            raise ValueError(
                f"ComputeSimResult len(preview_rows) ({len(self.preview_rows)}) "
                f"cannot exceed row_count ({self.row_count})"
            )
        return self


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
    "ComputeModelKind",
    "ComputeSimResult",
    "ComputeSimSpec",
    "ControlKind",
    "ControlSpec",
    "DatasetSpec",
    "DerivedColumnSpec",
    "DiagramElement",
    "DiagramSpec",
    "FftWindow",
    "MeasurementPoint",
    "OcrBackendKind",
    "OcrBox",
    "OcrMode",
    "OcrResult",
    "OdeMethod",
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
    "WatchPacingMode",
    "WatchResult",
    "WatchSpec",
    "WatchStatus",
    "WatchStopOp",
]
