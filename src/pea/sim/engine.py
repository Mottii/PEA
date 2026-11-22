"""Universal Playwright/Ruffle simulator session manager, controls, sweeps, and extremum search."""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import math
import os
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, ImageDraw, ImageFont

from pea.config import PeaConfig, is_origin_allowed, normalize_origin, resolve_browser_executable
from pea.ocr.base import OcrBackend, join_ocr_text
from pea.schemas import (
    ActionStep,
    CalibrationProfile,
    CommitMode,
    ControlSpec,
    MeasurementPoint,
    OcrMode,
    OcrResult,
    PlateauResult,
    PointPx,
    ReadoutSpec,
    SearchSpec,
    SweepSpec,
    SwfMeta,
)
from pea.sim.profiles import ProfileStore, validate_profile_geometry
from pea.sim.runtime_server import SimRuntimeServer
from pea.workspace import RunWorkspace, WorkspaceManager

_FLOAT_EPS = 1e-9
_SEQ_SUFFIX_RE = re.compile(r"-(\d+)\.[^.]+$")


def _max_existing_seq(*dirs: Path) -> int:
    """Return the highest ``-NNNN.<ext>`` sequence number across *dirs* (0 if none)."""
    highest = 0
    for directory in dirs:
        if not directory.is_dir():
            continue
        for item in directory.iterdir():
            match = _SEQ_SUFFIX_RE.search(item.name)
            if match is not None:
                highest = max(highest, int(match.group(1)))
    return highest


class SimCancelledError(RuntimeError):
    """Raised when a closed-loop search is cooperatively cancelled after saving partial evidence."""

    def __init__(
        self,
        message: str,
        *,
        partial_trace: list[MeasurementPoint],
        readings_relpath: str | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_trace = partial_trace
        self.readings_relpath = readings_relpath


@dataclass(frozen=True)
class StepperPlan:
    """Deterministic interaction sequence for setting a stepper/input control safely."""

    control_name: str
    commit_mode: CommitMode
    target_value: float
    typed_text: str | None
    input_xy: PointPx | None
    arrow_xy: PointPx | None
    arrow_direction: str | None
    press_enter: bool
    blur_xy: PointPx | None


def select_all_shortcut(platform_name: str | None = None) -> str:
    """Return OS-appropriate select-all key chord for Playwright ``keyboard.press``."""
    plat = platform_name if platform_name is not None else sys.platform
    return "Meta+a" if plat == "darwin" else "Control+a"


def _step_decimal_places(step: float | None) -> int | None:
    if step is None or not math.isfinite(step) or step <= 0:
        return None
    dec = Decimal(str(step)).normalize()
    exponent = dec.as_tuple().exponent
    if not isinstance(exponent, int):
        return None
    return max(0, -exponent)


def format_control_value(value: float | int | str, spec: ControlSpec) -> str:
    """Format *value* according to *spec*'s step precision and Russian ``decimal_comma`` rule."""
    if isinstance(value, bool):
        raise TypeError("Boolean is not a valid numeric control value")
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)

    fval = float(value)
    if not math.isfinite(fval):
        raise ValueError(f"Non-finite control value: {value!r}")

    decimals = _step_decimal_places(spec.step)
    if decimals is not None:
        rounded = round(fval, decimals)
        if abs(fval - rounded) <= 1e-7:
            if decimals == 0:
                formatted = str(int(round(rounded)))
            else:
                formatted = f"{rounded:.{decimals}f}"
        else:
            formatted = f"{fval:.10f}".rstrip("0").rstrip(".")
    else:
        if abs(fval - round(fval)) <= 1e-9:
            formatted = str(int(round(fval)))
        else:
            formatted = f"{fval:.10f}".rstrip("0").rstrip(".")

    if formatted in ("-0", "-0.0") or (
        formatted.startswith("-") and all(ch in "-0." for ch in formatted)
    ):
        formatted = formatted.lstrip("-")

    if spec.decimal_comma:
        formatted = formatted.replace(".", ",")
    return formatted


def compute_stepper_plan(
    target: float | int | str,
    spec: ControlSpec,
    *,
    default_blur_xy: PointPx | None = None,
    commit_mode_override: CommitMode | None = None,
) -> StepperPlan:
    """Compute a boundary-safe stepper commit plan for ``enter``, ``step_interior``, or ``blur``."""
    mode: CommitMode = commit_mode_override or spec.commit_mode
    blur_xy = spec.blur_xy or default_blur_xy

    if mode == "click":
        click_pt = spec.click_xy or spec.input_xy
        if click_pt is None:
            raise ValueError(f"Control {spec.name!r} with commit_mode='click' requires click_xy")
        return StepperPlan(
            control_name=spec.name,
            commit_mode=mode,
            target_value=float(target) if isinstance(target, (int, float)) else 0.0,
            typed_text=None,
            input_xy=click_pt,
            arrow_xy=None,
            arrow_direction=None,
            press_enter=False,
            blur_xy=blur_xy,
        )

    if isinstance(target, bool):
        raise TypeError("Boolean is not a valid stepper target")
    numeric_target = float(str(target).replace(",", "."))
    if not math.isfinite(numeric_target):
        raise ValueError(f"Control {spec.name!r} received non-finite target {target!r}")

    if spec.min_val is not None and numeric_target < spec.min_val - _FLOAT_EPS:
        raise ValueError(
            f"Target {numeric_target} for control {spec.name!r} is below min_val={spec.min_val}"
        )
    if spec.max_val is not None and numeric_target > spec.max_val + _FLOAT_EPS:
        raise ValueError(
            f"Target {numeric_target} for control {spec.name!r} exceeds max_val={spec.max_val}"
        )

    if spec.input_xy is None:
        raise ValueError(f"Control {spec.name!r} requires input_xy for commit_mode={mode!r}")

    if mode == "enter":
        return StepperPlan(
            control_name=spec.name,
            commit_mode=mode,
            target_value=numeric_target,
            typed_text=format_control_value(numeric_target, spec),
            input_xy=spec.input_xy,
            arrow_xy=None,
            arrow_direction=None,
            press_enter=True,
            blur_xy=blur_xy,
        )

    if mode == "blur":
        if blur_xy is None:
            raise ValueError(
                f"Control {spec.name!r} with commit_mode='blur' requires blur_xy "
                "on the control or CalibrationProfile"
            )
        return StepperPlan(
            control_name=spec.name,
            commit_mode=mode,
            target_value=numeric_target,
            typed_text=format_control_value(numeric_target, spec),
            input_xy=spec.input_xy,
            arrow_xy=None,
            arrow_direction=None,
            press_enter=False,
            blur_xy=blur_xy,
        )

    # mode == "step_interior" (Lab 4 boundary-safe commit)
    if spec.step is None or spec.step <= 0:
        raise ValueError(
            f"Control {spec.name!r} with commit_mode='step_interior' requires positive step"
        )

    step = spec.step
    can_from_above = spec.down_xy is not None and (
        spec.max_val is None or numeric_target + step <= spec.max_val + _FLOAT_EPS
    )
    can_from_below = spec.up_xy is not None and (
        spec.min_val is None or numeric_target - step >= spec.min_val - _FLOAT_EPS
    )

    if can_from_above:
        pre_value = numeric_target + step
        return StepperPlan(
            control_name=spec.name,
            commit_mode=mode,
            target_value=numeric_target,
            typed_text=format_control_value(pre_value, spec),
            input_xy=spec.input_xy,
            arrow_xy=spec.down_xy,
            arrow_direction="down",
            press_enter=False,
            blur_xy=blur_xy,
        )

    if can_from_below:
        pre_value = numeric_target - step
        return StepperPlan(
            control_name=spec.name,
            commit_mode=mode,
            target_value=numeric_target,
            typed_text=format_control_value(pre_value, spec),
            input_xy=spec.input_xy,
            arrow_xy=spec.up_xy,
            arrow_direction="up",
            press_enter=False,
            blur_xy=blur_xy,
        )

    raise ValueError(
        f"Control {spec.name!r} cannot execute step_interior at target={numeric_target} "
        f"(bounds=[{spec.min_val}, {spec.max_val}], step={step}, "
        f"up_xy={spec.up_xy}, down_xy={spec.down_xy})"
    )


def values_match(
    requested: float | int | str,
    observed: float | int | str | None,
    *,
    spec: ControlSpec | None = None,
    abs_tol: float | None = None,
) -> bool:
    """Return True if an OCR-observed control setting agrees with the requested setting."""
    if observed is None:
        return False
    try:
        req_num = float(str(requested).replace(",", "."))
        obs_num = float(str(observed).replace(",", "."))
    except ValueError:
        return str(requested).strip().lower() == str(observed).strip().lower()

    if not (math.isfinite(req_num) and math.isfinite(obs_num)):
        return False

    if abs_tol is not None:
        tol = abs_tol
    elif spec is not None and spec.step is not None:
        tol = max(1e-6, spec.step * 0.25)
    else:
        tol = max(1e-4, abs(req_num) * 1e-4)
    return abs(req_num - obs_num) <= tol


def _load_overlay_font(
    font_path: Path | None,
    size: int = 11,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load bundled Noto Sans TTF for Cyrillic+Latin overlay tags with safe bitmap fallback."""
    if font_path is not None and font_path.is_file():
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_annotated_overlay(
    image: Image.Image,
    ocr_result: OcrResult,
    *,
    grid_step: int = 100,
    profile: CalibrationProfile | None = None,
    font_path: Path | None = None,
) -> Image.Image:
    """Draw a coordinate grid, numbered OCR boxes, and optional profile ROIs onto a copy."""
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    font = _load_overlay_font(font_path, size=11)
    width, height = canvas.size

    if grid_step > 0:
        grid_color = (205, 215, 230)
        label_color = (60, 80, 120)
        for x in range(0, width, grid_step):
            draw.line([(x, 0), (x, height)], fill=grid_color, width=1)
            draw.text((x + 2, 2), str(x), fill=label_color, font=font)
        for y in range(0, height, grid_step):
            draw.line([(0, y), (width, y)], fill=grid_color, width=1)
            draw.text((2, y + 2), str(y), fill=label_color, font=font)

    for idx, box in enumerate(ocr_result.boxes):
        x1, y1, x2, y2 = box.bbox_px
        draw.rectangle([x1, y1, x2, y2], outline=(220, 38, 38), width=2)
        tag = f"[{idx}] {box.text}"
        tag_y = max(0, y1 - 14)
        draw.text((x1 + 1, tag_y), tag, fill=(185, 28, 28), font=font)

    if profile is not None:
        for name, readout in profile.readouts.items():
            x1, y1, x2, y2 = readout.roi_px
            draw.rectangle([x1, y1, x2, y2], outline=(22, 163, 74), width=2)
            draw.text((x1 + 2, max(0, y1 - 14)), f"R:{name}", fill=(21, 128, 61), font=font)
        for name, ctrl in profile.controls.items():
            pt = ctrl.input_xy or ctrl.click_xy
            if pt is not None:
                cx, cy = pt
                draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], outline=(37, 99, 235), width=2)
                draw.text((cx + 6, max(0, cy - 7)), f"C:{name}", fill=(29, 78, 216), font=font)

    return canvas


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _origin_matches_allowlist(url: str, config: PeaConfig) -> bool:
    """Return True iff *url* is an HTTP(S) URL whose canonical origin is in ``allowed_origins``."""
    if not config.allowed_origins:
        return False
    text = url.strip()
    if not text or any(ch.isspace() for ch in text) or "*" in text:
        return False
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return False
    if parsed.username is not None or parsed.password is not None or not parsed.hostname:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname.lower()
    host_fmt = f"[{host}]" if ":" in host else host
    origin_str = f"{scheme}://{host_fmt}" if port is None else f"{scheme}://{host_fmt}:{port}"
    try:
        canonical = normalize_origin(origin_str)
    except ValueError:
        return False
    return canonical in config.allowed_origins


def _is_route_url_allowed(
    url: str,
    loopback_origin: str | None,
    config: PeaConfig,
) -> bool:
    """Allow loopback/allowlisted HTTP(S) origins plus origin-scoped ``blob:`` URLs."""
    target = url.removeprefix("blob:") if url.startswith("blob:") else url
    if loopback_origin is None:
        return _origin_matches_allowlist(target, config)
    return is_origin_allowed(target, loopback_origin, config)


class SimulatorSession:
    """Live or mock-backed simulator session with provenance logging, sweeps, and search."""

    def __init__(
        self,
        *,
        session_id: str,
        config: PeaConfig,
        workspace: RunWorkspace,
        ocr: OcrBackend,
        profile_store: ProfileStore,
        page: Any,
        target_url: str,
        loopback_origin: str | None,
        stage_width: int,
        stage_height: int,
        header_height_px: int = 39,
        server: SimRuntimeServer | None = None,
        swf_meta: SwfMeta | None = None,
        profile: CalibrationProfile | None = None,
        context: Any = None,
        platform_name: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.config = config
        self.workspace = workspace
        self.ocr = ocr
        self.profile_store = profile_store
        self.page = page
        self.target_url = target_url
        self.loopback_origin = loopback_origin
        self.stage_width = stage_width
        self.stage_height = stage_height
        self.header_height_px = header_height_px
        self.server = server
        self.swf_meta = swf_meta
        self.profile = profile
        self._context = context
        self._platform_name = platform_name

        self.console_messages: list[str] = []
        self.page_errors: list[str] = []
        self.aborted_requests: list[str] = []

        self._capture_seq = _max_existing_seq(
            self.workspace.evidence_dir,
            self.workspace.crops_dir,
            self.workspace.root / "ocr",
        )
        self._cancel_event = asyncio.Event()
        self._closed = False

    def request_cancel(self) -> None:
        """Signal cooperative cancellation for any in-progress sweep or extremum search."""
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        """Clear any prior cooperative cancellation signal."""
        self._cancel_event.clear()

    def _require_profile(self) -> CalibrationProfile:
        if self.profile is None:
            raise RuntimeError(
                "No CalibrationProfile is attached to this session; call save_profile() first."
            )
        return self.profile

    async def _sleep_ms(self, ms: int) -> None:
        if ms <= 0:
            return
        if hasattr(self.page, "wait_for_timeout"):
            await self.page.wait_for_timeout(ms)
        else:
            await asyncio.sleep(ms / 1000.0)

    async def _blur_focus(self, xy: PointPx | None = None) -> PointPx:
        target = xy
        if target is None and self.profile is not None:
            target = self.profile.blur_xy
        if target is None:
            tx = min(10, max(1, self.stage_width // 2))
            ty = min(10, max(1, self.header_height_px // 2))
            target = (tx, ty)
        await self.page.mouse.click(target[0], target[1])
        return target

    async def capture_frame(
        self,
        name_prefix: str = "frame",
        *,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Image.Image, str, str]:
        """Capture an unmodified raw PNG into ``evidence/`` and hash it in provenance."""
        while True:
            self._capture_seq += 1
            filename = f"{name_prefix}-{self._capture_seq:04d}.png"
            relpath = f"evidence/{filename}"
            abs_path = self.workspace.resolve_path(relpath)
            if not abs_path.exists():
                break
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        png_bytes = await self.page.screenshot(type="png")
        abs_path.write_bytes(png_bytes)
        entry = self.workspace.record_artifact(
            relpath,
            kind="screenshot",
            metadata=metadata,
            source="simulator_captured",
        )
        with Image.open(io.BytesIO(png_bytes)) as opened:
            pil_img = opened.convert("RGB")
        return (pil_img, entry.relpath, entry.sha256)

    async def observe(
        self,
        *,
        annotate: bool = False,
        readouts: list[str] | None = None,
        ocr_mode: OcrMode = "det_rec",
        blur_first: bool = False,
    ) -> dict[str, Any]:
        """Capture a raw hashed frame, run OCR, and optionally save an annotated grid overlay."""
        if blur_first:
            await self._blur_focus()

        img, relpath, digest = await self.capture_frame(
            "observe",
            metadata={"annotate": annotate, "ocr_mode": ocr_mode},
        )
        ocr_result = self.ocr.recognize(img, mode=ocr_mode)

        annotated_relpath: str | None = None
        if annotate:
            overlay_font = self.config.local_assets_dir / "fonts" / "NotoSans-Regular.ttf"
            overlay = render_annotated_overlay(
                img,
                ocr_result,
                profile=self.profile,
                font_path=overlay_font,
            )
            annotated_relpath = f"crops/annotated-{self._capture_seq:04d}.png"
            ann_path = self.workspace.resolve_path(annotated_relpath)
            ann_path.parent.mkdir(parents=True, exist_ok=True)
            overlay.save(ann_path, format="PNG")
            self.workspace.record_artifact(
                annotated_relpath,
                kind="annotated_overlay",
                metadata={"source_screenshot": relpath},
                source="simulator_captured",
            )

        parsed_readouts: dict[str, dict[str, Any]] = {}
        if readouts:
            profile = self._require_profile()
            for name in readouts:
                if name not in profile.readouts:
                    raise KeyError(f"Readout {name!r} not found in calibration profile")
                spec = profile.readouts[name]
                ro_ocr, val, unit = self.ocr.read_spec(img, spec)
                parsed_readouts[name] = {
                    "raw_text": join_ocr_text(ro_ocr),
                    "value": val,
                    "unit": unit,
                }

        return {
            "session_id": self.session_id,
            "run_id": self.workspace.run_id,
            "screenshot_relpath": relpath,
            "screenshot_sha256": digest,
            "annotated_relpath": annotated_relpath,
            "stage_width": self.stage_width,
            "stage_height": self.stage_height,
            "header_height_px": self.header_height_px,
            "profile_loaded": self.profile is not None,
            "ocr": ocr_result,
            "readouts": parsed_readouts,
        }

    async def _execute_stepper_plan(self, plan: StepperPlan) -> dict[str, Any]:
        if plan.commit_mode == "click":
            assert plan.input_xy is not None
            await self.page.mouse.click(plan.input_xy[0], plan.input_xy[1])
            return {"action": "set_stepper", "control": plan.control_name, "mode": "click"}

        assert plan.input_xy is not None
        assert plan.typed_text is not None
        await self.page.mouse.click(plan.input_xy[0], plan.input_xy[1])
        await self.page.keyboard.press(select_all_shortcut(self._platform_name))
        await self.page.keyboard.type(plan.typed_text)

        if plan.press_enter:
            await self.page.keyboard.press("Enter")
        if plan.arrow_xy is not None:
            await self.page.mouse.click(plan.arrow_xy[0], plan.arrow_xy[1])
        if plan.blur_xy is not None:
            await self.page.mouse.click(plan.blur_xy[0], plan.blur_xy[1])

        return {
            "action": "set_stepper",
            "control": plan.control_name,
            "mode": plan.commit_mode,
            "target": plan.target_value,
            "typed_text": plan.typed_text,
            "arrow_direction": plan.arrow_direction,
            "blurred": plan.blur_xy is not None,
        }

    async def act(
        self,
        steps: list[ActionStep],
        *,
        settle_ms: int = 0,
    ) -> list[dict[str, Any]]:
        """Execute a sequence of ``ActionStep`` primitives against the simulator page."""
        executed: list[dict[str, Any]] = []
        for step in steps:
            if step.action in ("click", "double_click"):
                if step.x is not None and step.y is not None:
                    pt: PointPx = (step.x, step.y)
                else:
                    profile = self._require_profile()
                    assert step.control is not None
                    if step.control not in profile.controls:
                        raise KeyError(f"Control {step.control!r} not found in profile")
                    ctrl = profile.controls[step.control]
                    resolved = ctrl.click_xy or ctrl.input_xy
                    if resolved is None:
                        raise ValueError(f"Control {step.control!r} has no click_xy or input_xy")
                    pt = resolved
                if step.action == "click":
                    await self.page.mouse.click(pt[0], pt[1])
                else:
                    await self.page.mouse.dblclick(pt[0], pt[1])
                executed.append({"action": step.action, "x": pt[0], "y": pt[1]})

            elif step.action == "drag":
                assert None not in (step.x, step.y, step.to_x, step.to_y)
                await self.page.mouse.move(step.x, step.y)
                await self.page.mouse.down()
                await self.page.mouse.move(step.to_x, step.to_y, steps=10)
                await self.page.mouse.up()
                executed.append(
                    {
                        "action": "drag",
                        "from": (step.x, step.y),
                        "to": (step.to_x, step.to_y),
                    }
                )

            elif step.action == "type":
                assert step.text is not None
                if step.x is not None and step.y is not None:
                    await self.page.mouse.click(step.x, step.y)
                elif step.control is not None:
                    profile = self._require_profile()
                    if step.control not in profile.controls:
                        raise KeyError(f"Control {step.control!r} not found in profile")
                    ctrl = profile.controls[step.control]
                    resolved = ctrl.input_xy or ctrl.click_xy
                    if resolved is None:
                        raise ValueError(f"Control {step.control!r} has no input_xy or click_xy")
                    await self.page.mouse.click(resolved[0], resolved[1])
                if step.select_all:
                    await self.page.keyboard.press(select_all_shortcut(self._platform_name))
                await self.page.keyboard.type(step.text)
                executed.append(
                    {"action": "type", "text": step.text, "select_all": step.select_all}
                )

            elif step.action == "key":
                assert step.key is not None
                await self.page.keyboard.press(step.key)
                executed.append({"action": "key", "key": step.key})

            elif step.action == "wait":
                assert step.wait_ms is not None
                await self._sleep_ms(step.wait_ms)
                executed.append({"action": "wait", "wait_ms": step.wait_ms})

            elif step.action == "blur":
                explicit = (
                    (step.x, step.y) if (step.x is not None and step.y is not None) else None
                )
                pt = await self._blur_focus(explicit)
                executed.append({"action": "blur", "x": pt[0], "y": pt[1]})

            elif step.action == "set_stepper":
                profile = self._require_profile()
                assert step.control is not None and step.value is not None
                if step.control not in profile.controls:
                    raise KeyError(f"Control {step.control!r} not found in calibration profile")
                ctrl = profile.controls[step.control]
                plan = compute_stepper_plan(
                    step.value,
                    ctrl,
                    default_blur_xy=profile.blur_xy,
                    commit_mode_override=step.commit_mode,
                )
                executed.append(await self._execute_stepper_plan(plan))

        if settle_ms > 0:
            await self._sleep_ms(settle_ms)
        return executed

    async def save_profile(
        self,
        profile: CalibrationProfile,
        *,
        verify_live: bool = True,
        shared: bool = False,
    ) -> dict[str, Any]:
        """Validate geometry and live control/readout visibility before persisting a profile."""
        if self.swf_meta is not None and profile.swf_sha256 != self.swf_meta.sha256:
            raise ValueError(
                f"Profile swf_sha256={profile.swf_sha256} does not match loaded SWF "
                f"{self.swf_meta.sha256}"
            )
        validate_profile_geometry(
            profile,
            expected_stage=(self.stage_width, self.stage_height),
        )

        verification: dict[str, Any] = {"checked_readouts": {}, "checked_controls": {}}
        if verify_live:
            img, relpath, _ = await self.capture_frame(
                "profile-verify",
                metadata={"swf_sha256": profile.swf_sha256},
            )
            verification["screenshot_relpath"] = relpath
            img_w, img_h = img.size

            for name, rspec in profile.readouts.items():
                x1, y1, x2, y2 = rspec.roi_px
                if x2 > img_w or y2 > img_h:
                    raise ValueError(
                        f"Readout {name!r} ROI {rspec.roi_px} exceeds "
                        f"live frame size {(img_w, img_h)}"
                    )
                ro_ocr, val, unit = self.ocr.read_spec(img, rspec)
                raw_text = join_ocr_text(ro_ocr)
                if not raw_text:
                    raise ValueError(
                        f"Live verification failed: readout {name!r} at {rspec.roi_px} "
                        "returned empty OCR text"
                    )
                verification["checked_readouts"][name] = {
                    "raw_text": raw_text,
                    "value": val,
                    "unit": unit,
                }

            for name, cspec in profile.controls.items():
                if cspec.readout_roi_px is None:
                    continue
                x1, y1, x2, y2 = cspec.readout_roi_px
                if x2 > img_w or y2 > img_h:
                    raise ValueError(
                        f"Control {name!r} readout_roi_px {cspec.readout_roi_px} exceeds "
                        f"live frame size {(img_w, img_h)}"
                    )
                probe_spec = ReadoutSpec(
                    name=f"ctrl_{name}",
                    roi_px=cspec.readout_roi_px,
                    ocr_mode="rec_only",
                    parse_mode="text",
                )
                co_ocr, val, _ = self.ocr.read_spec(img, probe_spec)
                raw_text = join_ocr_text(co_ocr)
                if not raw_text:
                    raise ValueError(
                        f"Live verification failed: control {name!r} at {cspec.readout_roi_px} "
                        "returned empty OCR text"
                    )
                verification["checked_controls"][name] = {"raw_text": raw_text, "value": val}

        stamped = profile.model_copy(update={"verified_at": datetime.now(UTC)})
        saved_path = self.profile_store.save(
            stamped,
            shared=shared,
            expected_stage=(self.stage_width, self.stage_height),
        )
        self.profile = stamped
        return {
            "saved_path": str(saved_path),
            "swf_sha256": stamped.swf_sha256,
            "verified_live": verify_live,
            "verification": verification,
        }

    async def _apply_single_control(
        self,
        name: str,
        value: float | int | str,
    ) -> None:
        profile = self._require_profile()
        if name not in profile.controls:
            raise KeyError(f"Control {name!r} not found in calibration profile")
        ctrl = profile.controls[name]
        if ctrl.kind in ("stepper", "input"):
            plan = compute_stepper_plan(value, ctrl, default_blur_xy=profile.blur_xy)
            await self._execute_stepper_plan(plan)
        elif ctrl.kind in ("button", "radio", "checkbox"):
            pt = ctrl.click_xy or ctrl.input_xy
            if pt is None:
                raise ValueError(f"Control {name!r} has no click_xy")
            await self.page.mouse.click(pt[0], pt[1])
        else:
            raise ValueError(f"Unsupported automated control kind {ctrl.kind!r} for {name!r}")

    def _control_readout_spec(
        self,
        ctrl: ControlSpec,
        requested_value: float | int | str,
    ) -> ReadoutSpec | None:
        profile = self._require_profile()
        if ctrl.readout_roi_px is not None:
            parse_mode = "float" if isinstance(requested_value, (int, float)) else "text"
            return ReadoutSpec(
                name=f"__ctrl_{ctrl.name}",
                roi_px=ctrl.readout_roi_px,
                ocr_mode="rec_only",
                parse_mode=parse_mode,
                normalize_homoglyphs=True,
            )
        if ctrl.name in profile.readouts:
            return profile.readouts[ctrl.name]
        return None

    async def _capture_and_read_point(
        self,
        *,
        index: int,
        requested_settings: dict[str, float | int | str],
        readout_names: list[str],
        verify_controls: bool,
        blur_before_capture: bool,
        prefix: str,
    ) -> MeasurementPoint:
        profile = self._require_profile()
        if blur_before_capture:
            blur_target: PointPx | None = profile.blur_xy
            if blur_target is None:
                for ctrl_name in requested_settings:
                    ctrl = profile.controls.get(ctrl_name)
                    if ctrl is not None and ctrl.blur_xy is not None:
                        blur_target = ctrl.blur_xy
                        break
            if blur_target is not None:
                await self._blur_focus(blur_target)
            elif self.header_height_px > 0:
                await self._blur_focus()

        img, relpath, digest = await self.capture_frame(
            prefix,
            metadata={"index": index, "requested_settings": requested_settings},
        )

        readouts_parsed: dict[str, float | int | str | None] = {}
        readouts_raw: dict[str, str] = {}
        primary_unit: str | None = None

        for ro_idx, ro_name in enumerate(readout_names):
            ro_spec = profile.readouts[ro_name]
            ro_ocr, ro_val, ro_unit = self.ocr.read_spec(img, ro_spec)
            readouts_raw[ro_name] = join_ocr_text(ro_ocr)
            readouts_parsed[ro_name] = ro_val
            if ro_idx == 0:
                primary_unit = ro_unit

        observed_settings: dict[str, float | int | str | None] = {}
        controls_matched: bool | None = None
        if verify_controls:
            checks: list[bool] = []
            for ctrl_name, req_val in requested_settings.items():
                ctrl = profile.controls.get(ctrl_name)
                if ctrl is None:
                    continue
                ctrl_ro_spec = self._control_readout_spec(ctrl, req_val)
                if ctrl_ro_spec is None:
                    continue
                _, obs_val, _ = self.ocr.read_spec(img, ctrl_ro_spec)
                observed_settings[ctrl_name] = obs_val
                checks.append(values_match(req_val, obs_val, spec=ctrl))
            if checks:
                controls_matched = all(checks)

        primary_name = readout_names[0]
        return MeasurementPoint(
            index=index,
            requested_settings=dict(requested_settings),
            observed_settings=observed_settings,
            readout_name=primary_name,
            raw_ocr_text=readouts_raw[primary_name],
            parsed_value=readouts_parsed[primary_name],
            parsed_unit=primary_unit,
            readouts=readouts_parsed,
            raw_readouts=readouts_raw,
            screenshot_relpath=relpath,
            screenshot_sha256=digest,
            provenance="simulator_captured",
            captured_at=datetime.now(UTC),
            controls_matched=controls_matched,
        )

    def _persist_partial_readings(
        self,
        *,
        kind: str,
        status: str,
        points: list[MeasurementPoint],
        extra: dict[str, Any] | None = None,
        record_provenance: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "kind": kind,
            "status": status,
            "updated_at": datetime.now(UTC).isoformat(),
            "count": len(points),
            "points": [pt.model_dump(mode="json") for pt in points],
        }
        if extra:
            payload.update(extra)

        # Always keep partial-readings.json updated as the mutable live checkpoint.
        checkpoint_relpath = "partial-readings.json"
        _atomic_write_json(self.workspace.resolve_path(checkpoint_relpath), payload)

        if not record_provenance:
            return checkpoint_relpath

        # Write a unique immutable snapshot under readings/ for SHA-256 provenance integrity
        # so multi-sweep / multi-search runs never overwrite previously hashed files.
        readings_dir = self.workspace.resolve_path("readings")
        readings_dir.mkdir(parents=True, exist_ok=True)
        seq = _max_existing_seq(readings_dir)
        safe_kind = re.sub(r"[^A-Za-z0-9_.-]+", "_", kind).strip("_") or "readings"
        while True:
            seq += 1
            snapshot_relpath = f"readings/{safe_kind}-{seq:04d}.json"
            snapshot_abs = self.workspace.resolve_path(snapshot_relpath)
            if not snapshot_abs.exists():
                break
        _atomic_write_json(snapshot_abs, payload)
        self.workspace.record_artifact(
            snapshot_relpath,
            kind="partial_readings",
            metadata={"status": status, "count": len(points), "operation": kind},
            source="simulator_captured",
        )
        return snapshot_relpath

    async def sweep(self, spec: SweepSpec) -> dict[str, Any]:
        """Run an N-dimensional parameter sweep with OCR verification & partial evidence."""
        profile = self._require_profile()
        for name in spec.fixed_controls:
            if name not in profile.controls:
                raise KeyError(f"Fixed control {name!r} not found in calibration profile")
        for axis in spec.axes:
            if axis.control not in profile.controls:
                raise KeyError(
                    f"Sweep axis control {axis.control!r} not found in calibration profile"
                )
        for ro_name in spec.readouts:
            if ro_name not in profile.readouts:
                raise KeyError(f"Readout {ro_name!r} not found in calibration profile")

        grid = list(itertools.product(*(axis.values for axis in spec.axes)))
        total_points = len(grid)
        if total_points > spec.max_points:
            raise ValueError(
                f"Sweep grid has {total_points} points, exceeding max_points={spec.max_points}"
            )

        self.reset_cancel()
        self.workspace.update_status(
            phase="running",
            message=f"Starting grid sweep ({total_points} points)",
            source="simulator_captured",
            progress_current=0,
            progress_total=total_points,
        )

        points: list[MeasurementPoint] = []
        last_applied: dict[str, float | int | str] = {}

        try:
            for ctrl_name, val in spec.fixed_controls.items():
                await self._apply_single_control(ctrl_name, val)
                last_applied[ctrl_name] = val

            for idx, combo in enumerate(grid):
                if self._cancel_event.is_set():
                    rel = self._persist_partial_readings(
                        kind="sweep",
                        status="cancelled",
                        points=points,
                        extra={"total_points": total_points},
                        record_provenance=True,
                    )
                    self.workspace.update_status(
                        phase="cancelled",
                        message=(
                            f"Sweep cancelled after {len(points)}/{total_points} points; "
                            "partial evidence preserved."
                        ),
                        progress_current=len(points),
                        progress_total=total_points,
                    )
                    return {
                        "status": "cancelled",
                        "total_points": total_points,
                        "captured_points": len(points),
                        "controls_matched_all": all(
                            pt.controls_matched is not False for pt in points
                        ),
                        "missing_readouts": sum(1 for pt in points if pt.parsed_value is None),
                        "readings_relpath": rel,
                        "points": points,
                    }

                requested: dict[str, float | int | str] = dict(spec.fixed_controls)
                for axis, val in zip(spec.axes, combo, strict=True):
                    requested[axis.control] = val
                    if last_applied.get(axis.control) != val:
                        await self._apply_single_control(axis.control, val)
                        last_applied[axis.control] = val

                if spec.settle_ms > 0:
                    await self._sleep_ms(spec.settle_ms)

                point = await self._capture_and_read_point(
                    index=idx,
                    requested_settings=requested,
                    readout_names=spec.readouts,
                    verify_controls=spec.verify_controls,
                    blur_before_capture=spec.blur_before_capture,
                    prefix="sweep",
                )

                # Single retry on transient control-readback mismatch (zero equation substitution).
                # Only re-apply controls that were actually verified in observed_settings so
                # unverified toggle/checkbox controls in fixed_controls are never double-clicked.
                if spec.verify_controls and point.controls_matched is False:
                    for ctrl_name, req_val in requested.items():
                        if ctrl_name not in point.observed_settings:
                            continue
                        obs = point.observed_settings[ctrl_name]
                        ctrl = profile.controls.get(ctrl_name)
                        if ctrl is not None and not values_match(req_val, obs, spec=ctrl):
                            await self._apply_single_control(ctrl_name, req_val)
                    if spec.settle_ms > 0:
                        await self._sleep_ms(spec.settle_ms)
                    point = await self._capture_and_read_point(
                        index=idx,
                        requested_settings=requested,
                        readout_names=spec.readouts,
                        verify_controls=spec.verify_controls,
                        blur_before_capture=spec.blur_before_capture,
                        prefix="sweep-retry",
                    )

                points.append(point)
                self._persist_partial_readings(
                    kind="sweep",
                    status="running",
                    points=points,
                    extra={"total_points": total_points},
                    record_provenance=False,
                )
                self.workspace.update_status(
                    phase="running",
                    message=f"Captured sweep point {idx + 1}/{total_points}",
                    progress_current=idx + 1,
                    progress_total=total_points,
                )

            rel = self._persist_partial_readings(
                kind="sweep",
                status="complete",
                points=points,
                extra={"total_points": total_points},
                record_provenance=True,
            )
            self.workspace.update_status(
                phase="complete",
                message=f"Sweep completed ({len(points)}/{total_points} points)",
                progress_current=len(points),
                progress_total=total_points,
            )
            return {
                "status": "complete",
                "total_points": total_points,
                "captured_points": len(points),
                "controls_matched_all": all(pt.controls_matched is not False for pt in points),
                "missing_readouts": sum(1 for pt in points if pt.parsed_value is None),
                "readings_relpath": rel,
                "points": points,
            }

        except asyncio.CancelledError:
            self._persist_partial_readings(
                kind="sweep",
                status="cancelled",
                points=points,
                extra={"total_points": total_points},
                record_provenance=True,
            )
            self.workspace.update_status(
                phase="cancelled",
                message=(
                    f"Sweep task cancelled after {len(points)}/{total_points} points; "
                    "partial evidence preserved."
                ),
                progress_current=len(points),
                progress_total=total_points,
            )
            raise
        except Exception as exc:
            self._persist_partial_readings(
                kind="sweep",
                status="failed",
                points=points,
                extra={"total_points": total_points, "error": str(exc)},
                record_provenance=True,
            )
            self.workspace.update_status(
                phase="failed",
                error=str(exc),
                message=(
                    f"Sweep failed after {len(points)}/{total_points} points; "
                    "partial evidence preserved."
                ),
                progress_current=len(points),
                progress_total=total_points,
            )
            raise

    async def search_extremum(self, spec: SearchSpec) -> PlateauResult:
        """Closed-loop search supporting ``max``, ``min``, ``plateau_max``, and ``threshold``."""
        profile = self._require_profile()
        for name in spec.fixed_controls:
            if name not in profile.controls:
                raise KeyError(f"Fixed control {name!r} not found in calibration profile")
        if spec.sweep_control not in profile.controls:
            raise KeyError(f"Sweep control {spec.sweep_control!r} not found in calibration profile")
        if spec.readout not in profile.readouts:
            raise KeyError(f"Readout {spec.readout!r} not found in calibration profile")

        ctrl = profile.controls[spec.sweep_control]
        self.reset_cancel()
        self.workspace.update_status(
            phase="running",
            message=f"Starting {spec.mode} search on {spec.sweep_control}",
            source="simulator_captured",
            progress_current=0,
            progress_total=spec.max_steps,
        )

        trace: list[MeasurementPoint] = []
        best_val: float | None = None
        plateau_pairs: list[tuple[float, MeasurementPoint]] = []
        pre_plateau_point: MeasurementPoint | None = None
        post_plateau_point: MeasurementPoint | None = None
        consecutive_drops = 0

        # Threshold-mode tracking
        prev_pair: tuple[float, float, MeasurementPoint] | None = None
        threshold_hits: list[tuple[float, float, MeasurementPoint]] = []

        can_arrow_step = (
            ctrl.step is not None
            and abs(abs(spec.step) - ctrl.step) <= 1e-9
            and (
                (spec.step > 0 and ctrl.up_xy is not None)
                or (spec.step < 0 and ctrl.down_xy is not None)
            )
        )

        try:
            for ctrl_name, val in spec.fixed_controls.items():
                await self._apply_single_control(ctrl_name, val)

            for step_idx in range(spec.max_steps):
                if self._cancel_event.is_set():
                    rel = self._persist_partial_readings(
                        kind=f"search_{spec.mode}",
                        status="cancelled",
                        points=trace,
                        extra={"sweep_control": spec.sweep_control, "readout": spec.readout},
                        record_provenance=True,
                    )
                    self.workspace.update_status(
                        phase="cancelled",
                        message=(
                            f"Search cancelled after {len(trace)} steps; "
                            "partial evidence preserved."
                        ),
                        progress_current=len(trace),
                        progress_total=spec.max_steps,
                    )
                    raise SimCancelledError(
                        f"search_extremum cancelled after {len(trace)} steps",
                        partial_trace=trace,
                        readings_relpath=rel,
                    )

                x_val = round(spec.start + step_idx * spec.step, 10)
                if ctrl.min_val is not None and x_val < ctrl.min_val - _FLOAT_EPS:
                    break
                if ctrl.max_val is not None and x_val > ctrl.max_val + _FLOAT_EPS:
                    break

                if step_idx == 0 or not can_arrow_step:
                    await self._apply_single_control(spec.sweep_control, x_val)
                else:
                    arrow_xy = ctrl.up_xy if spec.step > 0 else ctrl.down_xy
                    assert arrow_xy is not None
                    await self.page.mouse.click(arrow_xy[0], arrow_xy[1])

                if spec.settle_ms > 0:
                    await self._sleep_ms(spec.settle_ms)

                requested: dict[str, float | int | str] = dict(spec.fixed_controls)
                requested[spec.sweep_control] = x_val

                point = await self._capture_and_read_point(
                    index=step_idx,
                    requested_settings=requested,
                    readout_names=[spec.readout],
                    verify_controls=spec.verify_controls,
                    blur_before_capture=spec.blur_before_capture,
                    prefix="search",
                )

                # If an arrow click or fixed control lagged, retry mismatched verified controls.
                if spec.verify_controls and point.controls_matched is False:
                    for ctrl_name, req_val in requested.items():
                        if ctrl_name not in point.observed_settings:
                            continue
                        obs = point.observed_settings[ctrl_name]
                        ctrl_item = profile.controls.get(ctrl_name)
                        if ctrl_item is not None and not values_match(req_val, obs, spec=ctrl_item):
                            await self._apply_single_control(ctrl_name, req_val)
                    if spec.settle_ms > 0:
                        await self._sleep_ms(spec.settle_ms)
                    point = await self._capture_and_read_point(
                        index=step_idx,
                        requested_settings=requested,
                        readout_names=[spec.readout],
                        verify_controls=spec.verify_controls,
                        blur_before_capture=spec.blur_before_capture,
                        prefix="search-retry",
                    )

                trace.append(point)
                self._persist_partial_readings(
                    kind=f"search_{spec.mode}",
                    status="running",
                    points=trace,
                    extra={"sweep_control": spec.sweep_control, "readout": spec.readout},
                    record_provenance=False,
                )
                self.workspace.update_status(
                    phase="running",
                    message=(
                        f"Search step {step_idx + 1}/{spec.max_steps} "
                        f"({spec.sweep_control}={x_val})"
                    ),
                    progress_current=step_idx + 1,
                    progress_total=spec.max_steps,
                )

                if point.parsed_value is None or not isinstance(point.parsed_value, (int, float)):
                    continue
                y_val = float(point.parsed_value)
                if not math.isfinite(y_val):
                    continue

                if spec.mode == "threshold":
                    assert spec.target_value is not None
                    target = spec.target_value
                    if abs(y_val - target) <= spec.tolerance:
                        threshold_hits.append((x_val, y_val, point))
                        consecutive_drops = 0
                    elif prev_pair is not None:
                        prev_x, prev_y, prev_pt = prev_pair
                        crossed = (prev_y - target) * (y_val - target) < 0
                        if crossed and not threshold_hits:
                            threshold_hits.append((prev_x, prev_y, prev_pt))
                            threshold_hits.append((x_val, y_val, point))
                            break
                        if threshold_hits:
                            consecutive_drops += 1
                            if consecutive_drops >= spec.stop_after_drop:
                                break
                    prev_pair = (x_val, y_val, point)
                    continue

                is_min = spec.mode == "min"
                if best_val is None:
                    better = True
                elif is_min:
                    better = y_val < best_val - spec.tolerance
                else:
                    better = y_val > best_val + spec.tolerance
                tied = best_val is not None and abs(y_val - best_val) <= spec.tolerance

                if better:
                    if len(trace) >= 2:
                        pre_plateau_point = trace[-2]
                    best_val = y_val
                    plateau_pairs = [(x_val, point)]
                    consecutive_drops = 0
                    post_plateau_point = None
                elif tied:
                    plateau_pairs.append((x_val, point))
                    consecutive_drops = 0
                    post_plateau_point = None
                else:
                    if consecutive_drops == 0:
                        post_plateau_point = point
                    consecutive_drops += 1
                    if consecutive_drops >= spec.stop_after_drop:
                        break

            if spec.mode == "threshold":
                if not threshold_hits:
                    raise RuntimeError(
                        f"Threshold target={spec.target_value} was not reached within "
                        f"{len(trace)} steps for {spec.sweep_control}"
                    )
                low_pair = min(threshold_hits, key=lambda item: item[0])
                high_pair = max(threshold_hits, key=lambda item: item[0])
                low_x, high_x = low_pair[0], high_pair[0]
                midpoint = 0.5 * (low_x + high_x)
                boundary_shots = {
                    "low": low_pair[2].screenshot_relpath,
                    "high": high_pair[2].screenshot_relpath,
                }
                result = PlateauResult(
                    control=spec.sweep_control,
                    readout=spec.readout,
                    peak_value=float(spec.target_value),
                    low_bound=low_x,
                    high_bound=high_x,
                    midpoint=midpoint,
                    width_steps=len(threshold_hits),
                    trace=trace,
                    boundary_screenshots=boundary_shots,
                )
            else:
                if best_val is None or not plateau_pairs:
                    raise RuntimeError(
                        f"No valid numeric readings captured during {spec.mode} search "
                        f"for {spec.sweep_control}/{spec.readout}"
                    )
                low_entry = min(plateau_pairs, key=lambda item: item[0])
                high_entry = max(plateau_pairs, key=lambda item: item[0])
                low_x, high_x = low_entry[0], high_entry[0]
                midpoint = 0.5 * (low_x + high_x)
                boundary_shots = {
                    "low": low_entry[1].screenshot_relpath,
                    "high": high_entry[1].screenshot_relpath,
                }
                if pre_plateau_point is not None:
                    boundary_shots["pre_boundary"] = pre_plateau_point.screenshot_relpath
                if post_plateau_point is not None:
                    boundary_shots["post_boundary"] = post_plateau_point.screenshot_relpath

                result = PlateauResult(
                    control=spec.sweep_control,
                    readout=spec.readout,
                    peak_value=best_val,
                    low_bound=low_x,
                    high_bound=high_x,
                    midpoint=midpoint,
                    width_steps=len(plateau_pairs),
                    trace=trace,
                    boundary_screenshots=boundary_shots,
                )

            self._persist_partial_readings(
                kind=f"search_{spec.mode}",
                status="complete",
                points=trace,
                extra={"sweep_control": spec.sweep_control, "readout": spec.readout},
                record_provenance=True,
            )
            self.workspace.update_status(
                phase="complete",
                message=(
                    f"Search {spec.mode} complete: peak={result.peak_value} on "
                    f"[{result.low_bound}, {result.high_bound}] (midpoint={result.midpoint})"
                ),
                progress_current=len(trace),
                progress_total=len(trace),
            )
            return result

        except SimCancelledError:
            raise
        except asyncio.CancelledError:
            self._persist_partial_readings(
                kind=f"search_{spec.mode}",
                status="cancelled",
                points=trace,
                extra={"sweep_control": spec.sweep_control, "readout": spec.readout},
                record_provenance=True,
            )
            self.workspace.update_status(
                phase="cancelled",
                message=(
                    f"Search task cancelled after {len(trace)} steps; "
                    "partial evidence preserved."
                ),
                progress_current=len(trace),
                progress_total=spec.max_steps,
            )
            raise
        except Exception as exc:
            self._persist_partial_readings(
                kind=f"search_{spec.mode}",
                status="failed",
                points=trace,
                extra={
                    "sweep_control": spec.sweep_control,
                    "readout": spec.readout,
                    "error": str(exc),
                },
                record_provenance=True,
            )
            self.workspace.update_status(
                phase="failed",
                error=str(exc),
                message=(
                    f"Search {spec.mode} failed after {len(trace)} steps; "
                    "partial evidence preserved."
                ),
                progress_current=len(trace),
                progress_total=spec.max_steps,
            )
            raise

    async def close(self) -> None:
        """Close Playwright page/context and stop the loopback runtime server if owned."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.page is not None and hasattr(self.page, "close"):
                await self.page.close()
        finally:
            try:
                if self._context is not None and hasattr(self._context, "close"):
                    await self._context.close()
            finally:
                if self.server is not None:
                    self.server.stop()


class SimulatorManager:
    """Lifecycle manager for Playwright Chromium and active ``SimulatorSession`` instances."""

    def __init__(
        self,
        config: PeaConfig | None = None,
        *,
        workspace_manager: WorkspaceManager | None = None,
        profile_store: ProfileStore | None = None,
        ocr: OcrBackend | None = None,
    ) -> None:
        self.config = config or PeaConfig.from_env()
        self.workspace_manager = workspace_manager or WorkspaceManager.from_config(self.config)
        self.profile_store = profile_store or ProfileStore.from_config(self.config)
        self._ocr: OcrBackend | None = ocr
        self._playwright: Any = None
        self._browser: Any = None
        self._sessions: dict[str, SimulatorSession] = {}

    def _get_ocr(self) -> OcrBackend:
        if self._ocr is None:
            from pea.ocr import create_ocr_backend

            self._ocr = create_ocr_backend(self.config)
        return self._ocr

    async def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": True}
        browser_exec = self.config.browser_executable or resolve_browser_executable()
        if browser_exec is not None:
            launch_kwargs["executable_path"] = str(browser_exec)
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        return self._browser

    def get_session(self, session_id: str) -> SimulatorSession:
        if session_id not in self._sessions:
            raise KeyError(f"Unknown simulator session_id: {session_id!r}")
        return self._sessions[session_id]

    def list_session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    async def open(
        self,
        *,
        swf_path: str | Path | None = None,
        html5_dir: str | Path | None = None,
        url: str | None = None,
        entry_file: str = "index.html",
        run_id: str | None = None,
        header_height_px: int = 39,
        settle_ms: int = 1500,
        viewport_width: int | None = None,
        viewport_height: int | None = None,
    ) -> SimulatorSession:
        """Open a local SWF, local HTML5 directory, or allowlisted URL inside isolated Chromium."""
        provided = sum(x is not None for x in (swf_path, html5_dir, url))
        if provided != 1:
            raise ValueError("Provide exactly one of swf_path, html5_dir, or url")

        workspace = (
            self.workspace_manager.get_run(run_id)
            if run_id is not None
            else self.workspace_manager.create_run(source="simulator_captured")
        )

        server: SimRuntimeServer | None = None
        swf_meta: SwfMeta | None = None
        loopback_origin: str | None = None
        stage_w = viewport_width or 900
        stage_h = viewport_height or 700
        eff_header_h = header_height_px

        if swf_path is not None:
            server = SimRuntimeServer(
                host=self.config.loopback_host,
                port=0,
                header_height_px=header_height_px,
            )
            swf_meta = server.configure_swf(swf_path, self.config.local_assets_dir / "ruffle")
            loopback_origin = server.start()
            target_url = f"{loopback_origin}/index.html"
            stage_w = swf_meta.stage_width
            stage_h = swf_meta.stage_height
            vp_w = viewport_width or max(900, stage_w + 40)
            vp_h = viewport_height or max(700, stage_h + eff_header_h + 40)
        elif html5_dir is not None:
            eff_header_h = 0
            server = SimRuntimeServer(
                host=self.config.loopback_host,
                port=0,
                header_height_px=0,
            )
            server.configure_html5_dir(html5_dir, entry_file=entry_file)
            loopback_origin = server.start()
            target_url = f"{loopback_origin}/"
            vp_w = viewport_width or 1024
            vp_h = viewport_height or 768
            stage_w, stage_h = vp_w, vp_h
        else:
            assert url is not None
            eff_header_h = 0
            loopback_origin = None
            if not _origin_matches_allowlist(url, self.config):
                parsed = urlsplit(url)
                raise PermissionError(
                    f"URL origin {parsed.scheme}://{parsed.netloc} is not allowlisted. "
                    "Add it explicitly via PEA_ALLOWED_ORIGINS."
                )
            target_url = url
            vp_w = viewport_width or 1024
            vp_h = viewport_height or 768
            stage_w, stage_h = vp_w, vp_h

        context = None
        page = None
        try:
            browser = await self._ensure_browser()
            context = await browser.new_context(
                viewport={"width": vp_w, "height": vp_h},
                device_scale_factor=1,
            )
            page = await context.new_page()

            session_id = f"sim-{secrets.token_hex(4)}"
            loaded_profile: CalibrationProfile | None = None
            if swf_meta is not None:
                loaded_profile = self.profile_store.load(
                    swf_meta.sha256,
                    expected_stage=(swf_meta.stage_width, swf_meta.stage_height),
                )

            session = SimulatorSession(
                session_id=session_id,
                config=self.config,
                workspace=workspace,
                ocr=self._get_ocr(),
                profile_store=self.profile_store,
                page=page,
                target_url=target_url,
                loopback_origin=loopback_origin,
                stage_width=stage_w,
                stage_height=stage_h,
                header_height_px=eff_header_h,
                server=server,
                swf_meta=swf_meta,
                profile=loaded_profile,
                context=context,
            )

            async def _route_handler(route: Any) -> None:
                req_url = route.request.url
                if _is_route_url_allowed(req_url, loopback_origin, self.config):
                    await route.continue_()
                else:
                    session.aborted_requests.append(req_url)
                    await route.abort()

            await page.route("**/*", _route_handler)
            page.on(
                "console",
                lambda msg: session.console_messages.append(f"[{msg.type}] {msg.text}"),
            )
            page.on("pageerror", lambda err: session.page_errors.append(str(err)))

            await page.goto(target_url, wait_until="domcontentloaded")

            if swf_meta is not None:
                await page.wait_for_function(
                    "() => window.__peaReady === true || window.__peaError !== null",
                    timeout=30_000,
                )
                err = await page.evaluate("() => window.__peaError")
                if err is not None:
                    raise RuntimeError(f"Ruffle failed to load {swf_meta.path}: {err}")

                player_locator = page.locator("ruffle-player")
                await player_locator.wait_for(state="visible", timeout=15_000)
                box = await player_locator.bounding_box()
                if box is None:
                    raise RuntimeError("Could not measure <ruffle-player> bounding box")
                actual = (
                    round(box["x"]),
                    round(box["y"]),
                    round(box["width"]),
                    round(box["height"]),
                )
                expected = (0, eff_header_h, swf_meta.stage_width, swf_meta.stage_height)
                if actual != expected:
                    raise RuntimeError(
                        f"<ruffle-player> bounds mismatch: actual={actual}, expected={expected}"
                    )

            if settle_ms > 0:
                await page.wait_for_timeout(settle_ms)

            self._sessions[session_id] = session
            return session

        except Exception:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if server is not None:
                server.stop()
            raise

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        for sid in list(self._sessions.keys()):
            await self.close_session(sid)
        if self._browser is not None:
            try:
                await self._browser.close()
            finally:
                self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None


__all__ = [
    "SimCancelledError",
    "SimulatorManager",
    "SimulatorSession",
    "StepperPlan",
    "compute_stepper_plan",
    "format_control_value",
    "render_annotated_overlay",
    "select_all_shortcut",
    "values_match",
]
