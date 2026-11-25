"""Protocol tests for SimRuntimeServer, stepper planning, sweeps, plateau search, and M2 stubs."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import io
import json
import math
import struct
import time
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from PIL import Image

from pea.config import PathConfinementError, PeaConfig
from pea.math_engine import data_analyze
from pea.ocr.base import read_spec_with_backend
from pea.schemas import (
    ActionStep,
    CalibrationProfile,
    ComputeSimResult,
    ComputeSimSpec,
    ControlSpec,
    DerivedColumnSpec,
    OcrBox,
    OcrMode,
    OcrResult,
    ReadoutSpec,
    RegressionSpec,
    SearchSpec,
    SweepAxis,
    SweepSpec,
    WatchResult,
    WatchSpec,
)
from pea.sim import (
    ProfileStore,
    SimCancelledError,
    SimRuntimeServer,
    SimulatorManager,
    SimulatorSession,
    compute_html5_dir_sha256,
    compute_sim,
    compute_stepper_plan,
    compute_url_sha256,
    format_control_value,
    select_all_shortcut,
    sim_watch,
    values_match,
)
from pea.sim.engine import _is_route_url_allowed
from pea.workspace import WorkspaceManager

REPO_ROOT = Path(__file__).resolve().parents[1]
RUFFLE_DIR = REPO_ROOT / "local_assets" / "ruffle"
SAMPLE_SHA256 = "35e4a257134073356c9796965ea3ea2d9362d51b2dc87486dc93e85a0fb96b15"


def _write_synthetic_cws_swf(
    tmp_path: Path,
    *,
    filename: str = "sample.swf",
    width: int = 800,
    height: int = 600,
    fps: float = 24.0,
) -> tuple[Path, str]:
    """Create a valid CWS SWF header in ``tmp_path`` and return ``(path, sha256)``."""
    vals = (0, width * 20, 0, height * 20)
    nbits = 1
    for v in vals:
        while not (-(1 << (nbits - 1)) <= v <= (1 << (nbits - 1)) - 1):
            nbits += 1
    bits = f"{nbits:05b}"
    mask = (1 << nbits) - 1
    for v in vals:
        bits += f"{v & mask:0{nbits}b}"
    bits += "0" * ((-len(bits)) % 8)
    rect = bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits), 8))
    body = rect + struct.pack("<HH", int(round(fps * 256)), 1) + b"\x00\x00"
    raw = b"CWS" + bytes([9]) + struct.pack("<I", 8 + len(body)) + zlib.compress(body)
    swf_path = tmp_path / filename
    swf_path.write_bytes(raw)
    return swf_path, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# 1. SimRuntimeServer (Host Guard, Auto-Sized HTML, Path Confinement)
# ---------------------------------------------------------------------------


def test_runtime_server_swf_mode_security_and_auto_sized_html(tmp_path: Path) -> None:
    swf_path, swf_sha = _write_synthetic_cws_swf(
        tmp_path, filename="synthetic_rlc.swf", width=800, height=600
    )
    server = SimRuntimeServer(host="127.0.0.1", port=0, header_height_px=39)
    meta = server.configure_swf(swf_path, RUFFLE_DIR, expected_sha256=swf_sha)
    assert (meta.stage_width, meta.stage_height) == (800, 600)

    with server:
        origin = server.origin
        parsed = urlsplit(origin)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port is not None and parsed.port > 0

        # Valid Host -> auto-sized HTML with security headers & Ruffle sandbox flags
        r_idx = httpx.get(f"{origin}/index.html")
        assert r_idx.status_code == 200
        assert r_idx.headers["cache-control"] == "no-store"
        assert r_idx.headers["x-content-type-options"] == "nosniff"
        assert "width: 800px;" in r_idx.text
        assert "height: 600px;" in r_idx.text
        assert "height: 39px;" in r_idx.text
        assert "allowScriptAccess: false" in r_idx.text
        assert 'allowNetworking: "none"' in r_idx.text
        assert "/sim/synthetic_rlc.swf" in r_idx.text

        # Ruffle asset & verified SWF served with correct MIME
        r_js = httpx.get(f"{origin}/ruffle/ruffle.js")
        assert r_js.status_code == 200
        assert "javascript" in r_js.headers["content-type"]

        r_swf = httpx.get(f"{origin}/sim/synthetic_rlc.swf")
        assert r_swf.status_code == 200
        assert len(r_swf.content) == swf_path.stat().st_size

        # Unknown SWF under /sim/ rejected
        assert httpx.get(f"{origin}/sim/other_sim.swf").status_code == 404

        # Path traversal under /ruffle/ rejected (raw connection preserves ..)
        host_hdr = {"Host": f"127.0.0.1:{parsed.port}"}
        conn = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=3)
        conn.request("GET", "/ruffle/../../pyproject.toml", headers=host_hdr)
        resp_trav = conn.getresponse()
        resp_trav.read()
        assert resp_trav.status == 403
        conn.close()

        conn2 = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=3)
        conn2.request("GET", "/ruffle/%2e%2e/manifest.json", headers=host_hdr)
        resp_enc = conn2.getresponse()
        resp_enc.read()
        assert resp_enc.status == 403
        conn2.close()

        # Anti-DNS-rebinding Host header validation
        for bad_host in ("evil.com", f"evil.com:{parsed.port}", "127.0.0.1:9999", "localhost:80"):
            conn_h = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=3)
            conn_h.putrequest("GET", "/index.html", skip_host=True)
            conn_h.putheader("Host", bad_host)
            conn_h.endheaders()
            resp_h = conn_h.getresponse()
            resp_h.read()
            assert resp_h.status == 403, f"Expected 403 for Host={bad_host!r}, got {resp_h.status}"
            assert resp_h.getheader("X-Content-Type-Options") == "nosniff"
            conn_h.close()

        # POST disallowed
        assert httpx.post(f"{origin}/index.html").status_code == 405

    # Rejects non-loopback bind & mismatched expected_sha256
    with pytest.raises(ValueError):
        SimRuntimeServer(host="0.0.0.0")
    s_bad = SimRuntimeServer()
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        s_bad.configure_swf(swf_path, RUFFLE_DIR, expected_sha256="0" * 64)


def test_runtime_server_html5_dir_mode_confinement(tmp_path: Path) -> None:
    app_dir = tmp_path / "html5_sim"
    app_dir.mkdir()
    (app_dir / "index.html").write_text("<h1>HTML5 Sim</h1>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top-secret", encoding="utf-8")

    server = SimRuntimeServer(host="127.0.0.1", port=0, header_height_px=0)
    server.configure_html5_dir(app_dir)
    with server:
        origin = server.origin
        port = urlsplit(origin).port
        assert port is not None
        r = httpx.get(f"{origin}/")
        assert r.status_code == 200
        assert "HTML5 Sim" in r.text

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/../secret.txt", headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 403
        conn.close()


@pytest.mark.parametrize("ipv6_host", ["::1", "[::1]"])
def test_runtime_server_ipv6_loopback_and_host_guard(tmp_path: Path, ipv6_host: str) -> None:
    swf_path, _ = _write_synthetic_cws_swf(
        tmp_path, filename="synthetic_ipv6.swf", width=800, height=600
    )
    server = SimRuntimeServer(host=ipv6_host, port=0, header_height_px=39)
    server.configure_swf(swf_path, RUFFLE_DIR)
    with server:
        origin = server.origin
        assert origin.startswith("http://[::1]:")
        port = server.port

        r = httpx.get(f"{origin}/index.html")
        assert r.status_code == 200
        assert "/sim/synthetic_ipv6.swf" in r.text

        conn = http.client.HTTPConnection("::1", port, timeout=3)
        conn.putrequest("GET", "/index.html", skip_host=True)
        conn.putheader("Host", f"evil.com:{port}")
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 403
        conn.close()


# ---------------------------------------------------------------------------
# 2. Boundary-Safe Stepper Planning & Helpers
# ---------------------------------------------------------------------------


def test_stepper_plan_commit_modes_and_max_bound_safety() -> None:
    # Lab 4 step_interior control: I in [-20, 20], step = 1
    lab4_current = ControlSpec(
        name="I",
        kind="stepper",
        input_xy=(120, 400),
        up_xy=(155, 392),
        down_xy=(155, 408),
        step=1.0,
        min_val=-20.0,
        max_val=20.0,
        decimal_comma=False,
        commit_mode="step_interior",
        blur_xy=(20, 18),
    )

    # Interior value (-5): approaches from above (-4 + down_xy -> -5)
    plan_int = compute_stepper_plan(-5, lab4_current)
    assert plan_int.typed_text == "-4"
    assert plan_int.arrow_direction == "down"
    assert plan_int.arrow_xy == (155, 408)
    assert plan_int.press_enter is False
    assert plan_int.blur_xy == (20, 18)

    # Min bound (-20): approaches from above (-19 + down_xy -> -20)
    plan_min = compute_stepper_plan(-20, lab4_current)
    assert plan_min.typed_text == "-19"
    assert plan_min.arrow_direction == "down"

    # Max bound (+20): MUST approach from below (19 + up_xy -> +20) so it never clamps 21->20->19!
    plan_max = compute_stepper_plan(20, lab4_current)
    assert plan_max.typed_text == "19"
    assert plan_max.arrow_direction == "up"
    assert plan_max.arrow_xy == (155, 392)
    assert plan_max.blur_xy == (20, 18)

    # Near max bound (19.5): target + step (20.5) > 20 -> must approach from below!
    plan_near_max = compute_stepper_plan(19.5, lab4_current)
    assert plan_near_max.typed_text == "18.5"
    assert plan_near_max.arrow_direction == "up"

    # Out of bounds rejected
    with pytest.raises(ValueError, match="exceeds max_val"):
        compute_stepper_plan(21, lab4_current)
    with pytest.raises(ValueError, match="below min_val"):
        compute_stepper_plan(-21, lab4_current)

    # Lab 5 enter mode with Russian decimal comma (L = 5.0 sits right on max_val)
    lab5_L = ControlSpec(
        name="L",
        kind="stepper",
        input_xy=(642, 322),
        step=0.1,
        min_val=1.0,
        max_val=5.0,
        decimal_comma=True,
        commit_mode="enter",
    )
    plan_enter = compute_stepper_plan(5.0, lab5_L, default_blur_xy=(450, 18))
    assert plan_enter.typed_text == "5,0"
    assert plan_enter.press_enter is True
    assert plan_enter.arrow_xy is None
    assert plan_enter.blur_xy == (450, 18)

    # Blur commit mode requires blur_xy
    plan_blur = compute_stepper_plan(
        3.2,
        lab5_L,
        default_blur_xy=(450, 18),
        commit_mode_override="blur",
    )
    assert plan_blur.typed_text == "3,2"
    assert plan_blur.press_enter is False
    assert plan_blur.blur_xy == (450, 18)
    with pytest.raises(ValueError, match="requires blur_xy"):
        compute_stepper_plan(3.2, lab5_L, commit_mode_override="blur")

    assert format_control_value(-0.0, lab5_L) == "0,0"
    assert select_all_shortcut("darwin") == "Meta+a"
    assert select_all_shortcut("linux") == "Control+a"
    assert values_match(20, 20.0, spec=lab4_current) is True
    assert values_match(20, 19.0, spec=lab4_current) is False


# ---------------------------------------------------------------------------
# 3. Mocked Simulator Page + OCR Harness for Sweep, Plateau Search & Cancel
# ---------------------------------------------------------------------------


def _rlc_uc_over_u(omega: float, *, R: float = 5.0, L: float = 5e-3, C: float = 32e-6) -> float:
    """Exact rounded 2-decimal Lab 5 m96220.swf U_C/U response (produces 74-step plateau at R=5)."""
    w0 = 1.0 / (L * C) ** 0.5
    q = (1.0 / R) * (L / C) ** 0.5
    ratio = omega / w0
    val = 1.0 / (((1.0 - ratio**2) ** 2 + (ratio / q) ** 2) ** 0.5)
    return round(val, 2)


class _MockMouse:
    def __init__(self, driver: _MockSimPage) -> None:
        self._d = driver

    async def click(self, x: int, y: int) -> None:
        self._d.on_click(x, y)

    async def dblclick(self, x: int, y: int) -> None:
        self._d.events.append(("dblclick", x, y))

    async def move(self, x: int, y: int, *, steps: int = 1) -> None:
        self._d.events.append(("move", x, y, steps))

    async def down(self) -> None:
        self._d.events.append(("down",))

    async def up(self) -> None:
        self._d.events.append(("up",))


class _MockKeyboard:
    def __init__(self, driver: _MockSimPage) -> None:
        self._d = driver

    async def press(self, key: str) -> None:
        self._d.on_key(key)

    async def type(self, text: str) -> None:
        self._d.on_type(text)


class _MockSimPage:
    """Deterministic in-memory simulator page modeling controls, arrows, lag, and OCR encoding."""

    def __init__(
        self,
        *,
        initial_state: dict[str, float],
        profile: CalibrationProfile,
        readout_fn: Any,
        drop_first_set_for: set[tuple[str, float]] | None = None,
        on_screenshot_hook: Any = None,
    ) -> None:
        self.state = dict(initial_state)
        self.profile = profile
        self.readout_fn = readout_fn
        self.drop_first_set_for = set(drop_first_set_for or ())
        self.on_screenshot_hook = on_screenshot_hook

        self.mouse = _MockMouse(self)
        self.keyboard = _MockKeyboard(self)
        self.events: list[tuple[Any, ...]] = []

        self._focused_control: str | None = None
        self._typed_buffer: str = ""
        self._select_all_armed: bool = False
        self.screenshot_count: int = 0

    def _find_control_at(self, x: int, y: int) -> tuple[str, str] | None:
        for name, ctrl in self.profile.controls.items():
            if ctrl.input_xy == (x, y):
                return (name, "input")
            if ctrl.up_xy == (x, y):
                return (name, "up")
            if ctrl.down_xy == (x, y):
                return (name, "down")
            if ctrl.click_xy == (x, y):
                return (name, "click")
        return None

    def _commit_value(self, name: str, val: float) -> None:
        ctrl = self.profile.controls[name]
        if ctrl.min_val is not None:
            val = max(ctrl.min_val, val)
        if ctrl.max_val is not None:
            val = min(ctrl.max_val, val)
        key = (name, round(val, 6))
        if key in self.drop_first_set_for:
            # Simulate a one-time transient lag so retry logic is tested
            self.drop_first_set_for.remove(key)
            return
        self.state[name] = val

    def on_click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))
        hit = self._find_control_at(x, y)
        if hit is None:
            # Blur click
            if self._focused_control is not None and self._typed_buffer:
                ctrl = self.profile.controls[self._focused_control]
                if ctrl.commit_mode == "blur":
                    self._commit_value(
                        self._focused_control, float(self._typed_buffer.replace(",", "."))
                    )
            self._focused_control = None
            self._typed_buffer = ""
            return

        name, part = hit
        ctrl = self.profile.controls[name]
        if part == "input":
            self._focused_control = name
            self._typed_buffer = ""
        elif part in ("up", "down"):
            base = (
                float(self._typed_buffer.replace(",", "."))
                if (self._focused_control == name and self._typed_buffer)
                else self.state[name]
            )
            # Clamp base before stepping (mimicking Flex stepper boundary clamping!)
            if ctrl.min_val is not None:
                base = max(ctrl.min_val, base)
            if ctrl.max_val is not None:
                base = min(ctrl.max_val, base)
            delta = (ctrl.step or 1.0) * (1.0 if part == "up" else -1.0)
            self._commit_value(name, round(base + delta, 10))
            self._typed_buffer = ""

    def on_key(self, key: str) -> None:
        self.events.append(("key", key))
        if key in ("Meta+a", "Control+a"):
            self._select_all_armed = True
            self._typed_buffer = ""
        elif key == "Enter" and self._focused_control is not None and self._typed_buffer:
            self._commit_value(
                self._focused_control, float(self._typed_buffer.replace(",", "."))
            )
            self._typed_buffer = ""

    def on_type(self, text: str) -> None:
        self.events.append(("type", text))
        if self._select_all_armed:
            self._typed_buffer = text
            self._select_all_armed = False
        else:
            self._typed_buffer += text

    async def wait_for_timeout(self, ms: int) -> None:
        self.events.append(("wait", ms))

    async def screenshot(self, *, type: str = "png") -> bytes:  # noqa: A002
        self.screenshot_count += 1
        if self.on_screenshot_hook is not None:
            self.on_screenshot_hook(self.screenshot_count)
        # Encode current state into pixel RGB values at distinct ROI coordinates so each
        # screenshot is unique (distinct SHA-256) and decoded cleanly by _MockOcrBackend.
        img = Image.new("RGB", (800, 639), color=(255, 255, 255))
        # Store sequence number in pixel (0, 0) for unique frame bytes
        img.putpixel((0, 0), (self.screenshot_count % 255, (self.screenshot_count // 255) % 255, 1))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


class _MockOcrBackend:
    """ROI-aware mock OCR reading the live state from ``_MockSimPage`` without equations."""

    def __init__(
        self,
        page: _MockSimPage,
        profile: CalibrationProfile,
        *,
        missing_readout_steps: set[int] | None = None,
    ) -> None:
        self._page = page
        self._profile = profile
        self._missing_steps = set(missing_readout_steps or ())

    @property
    def backend_name(self) -> str:
        return "rapidocr"

    def recognize(self, image: object, *, mode: OcrMode = "det_rec") -> OcrResult:
        return OcrResult(
            backend="rapidocr",
            mode=mode,
            elapsed_ms=0.5,
            boxes=[
                OcrBox(
                    text=f"omega={self._page.state.get('omega', 0)}",
                    confidence=0.99,
                    bbox_px=(620, 374, 667, 401),
                    bbox_norm=(0.775, 0.585, 0.834, 0.628),
                )
            ],
        )

    def _text_for_spec(self, spec: ReadoutSpec) -> str:
        if spec.name.startswith("__ctrl_"):
            ctrl_name = spec.name.removeprefix("__ctrl_")
            val = self._page.state[ctrl_name]
            ctrl = self._profile.controls[ctrl_name]
            return format_control_value(val, ctrl)

        if self._page.screenshot_count in self._missing_steps:
            return ""

        return str(self._page.readout_fn(spec.name, self._page.state))

    def read_spec(
        self,
        image: object,
        spec: ReadoutSpec,
    ) -> tuple[OcrResult, float | int | str | None, str | None]:
        txt = self._text_for_spec(spec)

        class _SingleTextBackend:
            backend_name = "rapidocr"

            def recognize(self, crop: object, *, mode: OcrMode = "det_rec") -> OcrResult:
                boxes = (
                    [
                        OcrBox(
                            text=txt,
                            confidence=0.99,
                            bbox_px=(0, 0, 40, 20),
                            bbox_norm=(0.0, 0.0, 1.0, 1.0),
                        )
                    ]
                    if txt
                    else []
                )
                return OcrResult(backend="rapidocr", mode=mode, elapsed_ms=0.2, boxes=boxes)

            def read_spec(self, img: object, s: ReadoutSpec) -> tuple[OcrResult, Any, Any]:
                return read_spec_with_backend(self, img, s)  # type: ignore[arg-type]

        return read_spec_with_backend(_SingleTextBackend(), image, spec)  # type: ignore[arg-type]


def _build_lab5_profile() -> CalibrationProfile:
    return CalibrationProfile(
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
                readout_roi_px=(620, 275, 667, 302),
                unit="Ом",
                step=0.1,
                min_val=3.0,
                max_val=10.0,
                decimal_comma=True,
                commit_mode="enter",
            ),
            "omega": ControlSpec(
                name="omega",
                kind="stepper",
                input_xy=(644, 388),
                up_xy=(678, 381),
                down_xy=(678, 395),
                readout_roi_px=(620, 374, 667, 401),
                unit="с⁻¹",
                step=1.0,
                min_val=1000.0,
                max_val=5000.0,
                decimal_comma=False,
                commit_mode="enter",
            ),
        },
        readouts={
            "uc_over_u": ReadoutSpec(
                name="uc_over_u",
                roi_px=(625, 474, 710, 502),
                ocr_mode="rec_only",
                parse_mode="float",
            ),
        },
        blur_xy=(450, 18),
    )


def _make_session(
    tmp_path: Path,
    *,
    profile: CalibrationProfile,
    page: _MockSimPage,
    ocr: _MockOcrBackend,
) -> SimulatorSession:
    cfg = PeaConfig(
        project_root=REPO_ROOT,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
    )
    wm = WorkspaceManager.from_config(cfg)
    ws = wm.create_run(label="Protocol Test", source="simulator_captured")
    store = ProfileStore.from_config(cfg)
    return SimulatorSession(
        session_id="sim-test01",
        config=cfg,
        workspace=ws,
        ocr=ocr,  # type: ignore[arg-type]
        profile_store=store,
        page=page,
        target_url="http://127.0.0.1:50000/index.html",
        loopback_origin="http://127.0.0.1:50000",
        stage_width=800,
        stage_height=600,
        header_height_px=39,
        profile=profile,
        platform_name="darwin",
    )


def _rlc_readout(_name: str, st: dict[str, float]) -> str:
    return f"{_rlc_uc_over_u(st['omega'], R=st['R']):.2f}".replace(".", ",")


async def test_session_observe_act_and_save_profile(tmp_path: Path) -> None:
    profile = _build_lab5_profile()
    page = _MockSimPage(
        initial_state={"R": 3.2, "omega": 2500.0},
        profile=profile,
        readout_fn=_rlc_readout,
    )
    ocr = _MockOcrBackend(page, profile)
    session = _make_session(tmp_path, profile=profile, page=page, ocr=ocr)

    # Observe with annotation + readout extraction
    obs = await session.observe(annotate=True, readouts=["uc_over_u"], blur_first=True)
    assert obs["screenshot_relpath"].startswith("evidence/")
    assert obs["annotated_relpath"].startswith("crops/")
    assert obs["readouts"]["uc_over_u"]["value"] == pytest.approx(3.91)
    assert session.workspace.verify_provenance().ok is True

    # Act primitives including set_stepper, drag, type, key, blur, wait
    steps = [
        ActionStep(action="set_stepper", control="R", value=5.0),
        ActionStep(action="drag", x=10, y=20, to_x=30, to_y=40),
        ActionStep(action="type", x=644, y=388, text="2400", select_all=True),
        ActionStep(action="key", key="Enter"),
        ActionStep(action="wait", wait_ms=0),
        ActionStep(action="blur"),
    ]
    log = await session.act(steps)
    assert len(log) == 6
    assert page.state["R"] == pytest.approx(5.0)
    assert page.state["omega"] == pytest.approx(2400.0)

    # Save profile with live verification
    saved = await session.save_profile(profile, verify_live=True)
    assert Path(saved["saved_path"]).is_file()
    assert saved["verification"]["checked_readouts"]["uc_over_u"]["value"] == pytest.approx(2.55)

    # Live verification must reject empty/unreadable readout ROI
    empty_ocr = _MockOcrBackend(page, profile, missing_readout_steps={page.screenshot_count + 1})
    bad_session = _make_session(tmp_path / "bad_prof", profile=profile, page=page, ocr=empty_ocr)
    with pytest.raises(ValueError, match="returned empty OCR text"):
        await bad_session.save_profile(profile, verify_live=True)


async def test_session_step_interior_sweep_hits_exact_max_bound_plus_20a(tmp_path: Path) -> None:
    """End-to-end step_interior sweep reaches I=-20, -5, +20 A without clamping +20 to 19."""
    lab4_profile = CalibrationProfile(
        swf_sha256=SAMPLE_SHA256,
        stage_width=800,
        stage_height=600,
        header_height_px=39,
        controls={
            "I": ControlSpec(
                name="I",
                kind="stepper",
                input_xy=(120, 400),
                up_xy=(155, 392),
                down_xy=(155, 408),
                readout_roi_px=(95, 388, 145, 412),
                unit="А",
                step=1.0,
                min_val=-20.0,
                max_val=20.0,
                decimal_comma=False,
                commit_mode="step_interior",
            ),
        },
        readouts={
            "B": ReadoutSpec(
                name="B",
                roi_px=(300, 388, 420, 412),
                ocr_mode="rec_only",
                parse_mode="float",
                unit_pattern=r"мкТл|мТл",
            )
        },
        blur_xy=(20, 18),
    )
    page = _MockSimPage(
        initial_state={"I": 0.0},
        profile=lab4_profile,
        readout_fn=lambda _n, st: f"{10.0 * st['I']:.1f} мкТл".replace(".", ","),
    )
    session = _make_session(
        tmp_path,
        profile=lab4_profile,
        page=page,
        ocr=_MockOcrBackend(page, lab4_profile),
    )

    res = await session.sweep(
        SweepSpec(
            axes=[SweepAxis(control="I", values=[-20, -5, 20])],
            readouts=["B"],
            settle_ms=0,
            verify_controls=True,
        )
    )
    assert res["status"] == "complete"
    assert res["controls_matched_all"] is True
    pts = res["points"]
    assert [p.observed_settings["I"] for p in pts] == [-20.0, -5.0, 20.0]
    assert [p.parsed_value for p in pts] == [-200.0, -50.0, 200.0]
    assert all(p.parsed_unit == "мкТл" for p in pts)
    assert session.workspace.verify_provenance().ok is True


async def test_session_sweep_control_retry_missing_ocr_and_cancel(tmp_path: Path) -> None:
    profile = _build_lab5_profile()
    # Drop the first set of omega=2450 to verify control verification retry works;
    # make the 3rd screenshot return empty OCR to verify zero equation substitution!
    page = _MockSimPage(
        initial_state={"R": 3.0, "omega": 2500.0},
        profile=profile,
        readout_fn=_rlc_readout,
        drop_first_set_for={("omega", 2450.0)},
    )
    ocr = _MockOcrBackend(page, profile, missing_readout_steps={4})
    session = _make_session(tmp_path, profile=profile, page=page, ocr=ocr)

    sweep_spec = SweepSpec(
        fixed_controls={"R": 3.2},
        axes=[SweepAxis(control="omega", values=[2500, 2450, 2400])],
        readouts=["uc_over_u"],
        settle_ms=0,
        verify_controls=True,
    )
    res = await session.sweep(sweep_spec)
    assert res["status"] == "complete"
    assert res["captured_points"] == 3
    assert res["controls_matched_all"] is True
    # Point 0 (shot 1) -> 3.91; Point 1 (shot 2 lagged, retried -> shot 3) -> 3.94;
    # Point 2 (shot 4 empty OCR) -> MUST stay None (never fabricated!)
    pts = res["points"]
    assert pts[0].parsed_value == pytest.approx(3.91)
    assert pts[1].parsed_value == pytest.approx(3.94)
    assert pts[1].observed_settings["omega"] == pytest.approx(2450.0)
    assert pts[2].parsed_value is None
    assert res["missing_readouts"] == 1
    assert session.workspace.verify_provenance().ok is True

    # Test cooperative cancellation preserves partial evidence & provenance
    cancel_session: SimulatorSession

    def _cancel_after_two(shot_num: int) -> None:
        if shot_num >= 2:
            cancel_session.request_cancel()

    page2 = _MockSimPage(
        initial_state={"R": 3.2, "omega": 2500.0},
        profile=profile,
        readout_fn=_rlc_readout,
        on_screenshot_hook=_cancel_after_two,
    )
    ocr2 = _MockOcrBackend(page2, profile)
    cancel_session = _make_session(tmp_path / "cancel_run", profile=profile, page=page2, ocr=ocr2)

    long_spec = SweepSpec(
        fixed_controls={"R": 3.2},
        axes=[SweepAxis(control="omega", values=[2500, 2490, 2480, 2470, 2460])],
        readouts=["uc_over_u"],
        settle_ms=0,
    )
    cancel_res = await cancel_session.sweep(long_spec)
    assert cancel_res["status"] == "cancelled"
    assert cancel_res["captured_points"] == 2
    assert cancel_session.workspace.read_status().phase == "cancelled"
    partial_json = json.loads(
        (cancel_session.workspace.root / "partial-readings.json").read_text("utf-8")
    )
    assert partial_json["status"] == "cancelled"
    assert partial_json["count"] == 2
    assert cancel_session.workspace.verify_provenance().ok is True


async def test_session_search_extremum_74_step_plateau_and_modes(tmp_path: Path) -> None:
    profile = _build_lab5_profile()
    page = _MockSimPage(
        initial_state={"R": 3.0, "omega": 2500.0},
        profile=profile,
        readout_fn=_rlc_readout,
    )
    ocr = _MockOcrBackend(page, profile)
    session = _make_session(tmp_path, profile=profile, page=page, ocr=ocr)

    # Exact Lab 5 Team 2 R=5.0 Ohm scenario: 74-step plateau [2361, 2434] at U_C/U = 2.55!
    search_spec = SearchSpec(
        mode="plateau_max",
        fixed_controls={"R": 5.0},
        sweep_control="omega",
        readout="uc_over_u",
        start=2445.0,
        step=-1.0,
        max_steps=120,
        stop_after_drop=2,
        settle_ms=0,
        verify_controls=True,
    )
    plateau = await session.search_extremum(search_spec)
    assert plateau.peak_value == pytest.approx(2.55)
    assert plateau.low_bound == pytest.approx(2361.0)
    assert plateau.high_bound == pytest.approx(2434.0)
    assert plateau.midpoint == pytest.approx(2397.5)
    assert plateau.width_steps == 74
    assert {"low", "high", "pre_boundary", "post_boundary"} <= set(
        plateau.boundary_screenshots.keys()
    )
    # Stopped 2 steps after low_bound=2361 (at 2359), total steps = 2445 - 2359 + 1 = 87
    assert len(plateau.trace) == 87
    assert session.workspace.verify_provenance().ok is True

    # Test min mode on simple inverted parabola (min at omega=2010)
    min_page = _MockSimPage(
        initial_state={"R": 5.0, "omega": 2000.0},
        profile=profile,
        readout_fn=lambda name, st: f"{(st['omega'] - 2010.0) ** 2 + 1.5:.2f}",
    )
    min_session = _make_session(
        tmp_path / "min_run",
        profile=profile,
        page=min_page,
        ocr=_MockOcrBackend(min_page, profile),
    )
    min_res = await min_session.search_extremum(
        SearchSpec(
            mode="min",
            sweep_control="omega",
            readout="uc_over_u",
            start=2005.0,
            step=1.0,
            max_steps=20,
            stop_after_drop=2,
            settle_ms=0,
        )
    )
    assert min_res.peak_value == pytest.approx(1.5)
    assert min_res.low_bound == pytest.approx(2010.0)
    assert min_res.high_bound == pytest.approx(2010.0)
    assert min_res.width_steps == 1

    # Test threshold crossing mode (crosses 2.0 between omega=2002 -> 1.0 and 2003 -> 3.0)
    thr_page = _MockSimPage(
        initial_state={"R": 5.0, "omega": 2000.0},
        profile=profile,
        readout_fn=lambda name, st: f"{2.0 * (st['omega'] - 2001.5):.2f}",
    )
    thr_session = _make_session(
        tmp_path / "thr_run",
        profile=profile,
        page=thr_page,
        ocr=_MockOcrBackend(thr_page, profile),
    )
    thr_res = await thr_session.search_extremum(
        SearchSpec(
            mode="threshold",
            sweep_control="omega",
            readout="uc_over_u",
            start=2001.0,
            step=1.0,
            max_steps=10,
            target_value=2.0,
            tolerance=0.1,
            settle_ms=0,
        )
    )
    assert thr_res.low_bound == pytest.approx(2002.0)
    assert thr_res.high_bound == pytest.approx(2003.0)
    assert thr_res.midpoint == pytest.approx(2002.5)

    # Test cooperative cancellation during search preserves partial trace & provenance
    cancel_search_session: SimulatorSession

    def _cancel_at_5(shot_num: int) -> None:
        if shot_num >= 5:
            cancel_search_session.request_cancel()

    c_page = _MockSimPage(
        initial_state={"R": 5.0, "omega": 2445.0},
        profile=profile,
        readout_fn=_rlc_readout,
        on_screenshot_hook=_cancel_at_5,
    )
    cancel_search_session = _make_session(
        tmp_path / "cancel_search",
        profile=profile,
        page=c_page,
        ocr=_MockOcrBackend(c_page, profile),
    )
    with pytest.raises(SimCancelledError) as exc_info:
        await cancel_search_session.search_extremum(search_spec)
    assert len(exc_info.value.partial_trace) == 5
    assert cancel_search_session.workspace.read_status().phase == "cancelled"
    assert cancel_search_session.workspace.verify_provenance().ok is True


# ---------------------------------------------------------------------------
# 4. SimulatorManager Origin Guard & Milestone 2 Stubs
# ---------------------------------------------------------------------------


async def test_simulator_manager_blocks_unallowlisted_external_url(tmp_path: Path) -> None:
    cfg = PeaConfig(
        project_root=REPO_ROOT,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
        allowed_origins=(),
    )
    mgr = SimulatorManager(cfg)
    assert mgr is not None
    with pytest.raises(PermissionError, match="not allowlisted"):
        await mgr.open(url="https://phet.colorado.edu/sims/html/rlc.html")
    with pytest.raises(ValueError, match="Provide exactly one"):
        await mgr.open()

    # External URL mode (loopback_origin=None) must allow ONLY allowlisted origins and NEVER
    # leak access to the local viewer_origin.
    cfg_ext = PeaConfig(
        project_root=REPO_ROOT,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "runs_ext",
        profiles_dir=tmp_path / "profiles_ext",
        allowed_origins=("https://phet.colorado.edu",),
    )
    assert _is_route_url_allowed("https://phet.colorado.edu/sims/rlc.html", None, cfg_ext) is True
    assert _is_route_url_allowed("blob:https://phet.colorado.edu/abc", None, cfg_ext) is True
    assert _is_route_url_allowed(f"{cfg_ext.viewer_origin}/api/runs", None, cfg_ext) is False
    assert _is_route_url_allowed("https://evil.example.com/", None, cfg_ext) is False


async def test_session_edge_cases_retry_failure_cancel_and_control_type(tmp_path: Path) -> None:
    # 1. sweep() retry must NOT double-click an unverified checkbox/button in fixed_controls
    prof = _build_lab5_profile()
    prof.controls["mode_toggle"] = ControlSpec(
        name="mode_toggle",
        kind="checkbox",
        click_xy=(100, 100),
    )
    page = _MockSimPage(
        initial_state={"R": 3.2, "omega": 2500.0},
        profile=prof,
        readout_fn=_rlc_readout,
        drop_first_set_for={("omega", 2450.0)},
    )
    sess = _make_session(
        tmp_path / "retry_checkbox",
        profile=prof,
        page=page,
        ocr=_MockOcrBackend(page, prof),
    )
    res = await sess.sweep(
        SweepSpec(
            fixed_controls={"R": 3.2, "mode_toggle": 1},
            axes=[SweepAxis(control="omega", values=[2450])],
            readouts=["uc_over_u"],
            settle_ms=0,
            verify_controls=True,
        )
    )
    assert res["controls_matched_all"] is True
    assert [e for e in page.events if e == ("click", 100, 100)] == [("click", 100, 100)]

    # 2. act(action="type", control=...) clicks the control before typing
    await sess.act(
        [
            ActionStep(action="type", control="omega", text="2400", select_all=True),
            ActionStep(action="key", key="Enter"),
        ]
    )
    assert page.state["omega"] == pytest.approx(2400.0)

    # 3. search_extremum() retries mismatched verified fixed_controls too
    prof_fix = _build_lab5_profile()
    page_fix = _MockSimPage(
        initial_state={"R": 3.0, "omega": 2445.0},
        profile=prof_fix,
        readout_fn=_rlc_readout,
        drop_first_set_for={("R", 5.0)},
    )
    sess_fix = _make_session(
        tmp_path / "search_fix_retry",
        profile=prof_fix,
        page=page_fix,
        ocr=_MockOcrBackend(page_fix, prof_fix),
    )
    res_fix = await sess_fix.search_extremum(
        SearchSpec(
            mode="max",
            fixed_controls={"R": 5.0},
            sweep_control="omega",
            readout="uc_over_u",
            start=2445.0,
            step=-1.0,
            max_steps=2,
            settle_ms=0,
            verify_controls=True,
        )
    )
    assert res_fix.trace[0].observed_settings["R"] == pytest.approx(5.0)
    assert res_fix.trace[0].controls_matched is True

    # 4. search_extremum() failure sets workspace phase='failed' & partial-readings status='failed'
    page_fail = _MockSimPage(
        initial_state={"R": 5.0, "omega": 2445.0},
        profile=prof_fix,
        readout_fn=_rlc_readout,
    )
    sess_fail = _make_session(
        tmp_path / "search_fail",
        profile=prof_fix,
        page=page_fail,
        ocr=_MockOcrBackend(page_fail, prof_fix),
    )
    with pytest.raises(RuntimeError, match="not reached"):
        await sess_fail.search_extremum(
            SearchSpec(
                mode="threshold",
                sweep_control="omega",
                readout="uc_over_u",
                start=2445.0,
                step=-1.0,
                max_steps=3,
                target_value=999.0,
                settle_ms=0,
            )
        )
    assert sess_fail.workspace.read_status().phase == "failed"
    assert sess_fail.workspace.read_status().error is not None
    fail_partial = json.loads(
        (sess_fail.workspace.root / "partial-readings.json").read_text("utf-8")
    )
    assert fail_partial["status"] == "failed"
    assert fail_partial["count"] == 3
    assert sess_fail.workspace.verify_provenance().ok is True

    # 4b. sweep() mid-run error preserves partial evidence, hashes provenance, and sets failed
    page_boom = _MockSimPage(
        initial_state={"R": 5.0, "omega": 2500.0},
        profile=prof_fix,
        readout_fn=_rlc_readout,
    )
    orig_shot = page_boom.screenshot

    async def _fail_on_second(*, type: str = "png") -> bytes:  # noqa: A002
        if page_boom.screenshot_count >= 1:
            raise RuntimeError("simulated browser disconnect")
        return await orig_shot(type=type)

    page_boom.screenshot = _fail_on_second  # type: ignore[method-assign]
    sess_boom = _make_session(
        tmp_path / "sweep_fail",
        profile=prof_fix,
        page=page_boom,
        ocr=_MockOcrBackend(page_boom, prof_fix),
    )
    with pytest.raises(RuntimeError, match="simulated browser disconnect"):
        await sess_boom.sweep(
            SweepSpec(
                axes=[SweepAxis(control="omega", values=[2500, 2490])],
                readouts=["uc_over_u"],
                settle_ms=0,
            )
        )
    assert sess_boom.workspace.read_status().phase == "failed"
    boom_partial = json.loads(
        (sess_boom.workspace.root / "partial-readings.json").read_text("utf-8")
    )
    assert boom_partial["status"] == "failed"
    assert boom_partial["count"] == 1
    assert sess_boom.workspace.verify_provenance().ok is True

    # 5. External asyncio.Task.cancel() during sweep flushes partial evidence + provenance
    page_cancel = _MockSimPage(
        initial_state={"R": 5.0, "omega": 2500.0},
        profile=prof_fix,
        readout_fn=_rlc_readout,
    )

    async def _slow_wait(_ms: int) -> None:
        await asyncio.sleep(0.05)

    page_cancel.wait_for_timeout = _slow_wait  # type: ignore[method-assign]
    sess_cancel = _make_session(
        tmp_path / "task_cancel",
        profile=prof_fix,
        page=page_cancel,
        ocr=_MockOcrBackend(page_cancel, prof_fix),
    )
    task = asyncio.create_task(
        sess_cancel.sweep(
            SweepSpec(
                axes=[SweepAxis(control="omega", values=[2500, 2490, 2480, 2470, 2460])],
                readouts=["uc_over_u"],
                settle_ms=50,
            )
        )
    )
    await asyncio.sleep(0.08)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sess_cancel.workspace.read_status().phase == "cancelled"
    assert sess_cancel.workspace.verify_provenance().ok is True

    # 6. Multi-session / multi-sweep provenance immutability in the same RunWorkspace
    shared_wm = WorkspaceManager(tmp_path / "shared_multi" / "runs")
    shared_ws = shared_wm.create_run(label="multi-session-prov", source="simulator_captured")
    shared_cfg = PeaConfig(
        project_root=tmp_path,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "shared_multi" / "runs",
        profiles_dir=tmp_path / "shared_multi" / "profiles",
    )
    shared_ps = ProfileStore.from_config(shared_cfg)
    shot_paths: set[str] = set()
    reading_paths: set[str] = set()
    for sess_idx in range(2):
        p_multi = _MockSimPage(
            initial_state={"R": 5.0, "omega": 2500.0},
            profile=prof_fix,
            readout_fn=_rlc_readout,
        )
        s_multi = SimulatorSession(
            session_id=f"sim-multi-{sess_idx}",
            config=shared_cfg,
            workspace=shared_ws,
            ocr=_MockOcrBackend(p_multi, prof_fix),
            profile_store=shared_ps,
            page=p_multi,
            target_url="http://127.0.0.1:9999/index.html",
            loopback_origin="http://127.0.0.1:9999",
            stage_width=prof_fix.stage_width,
            stage_height=prof_fix.stage_height,
            profile=prof_fix,
            platform_name="darwin",
        )
        obs_m = await s_multi.observe(annotate=True)
        shot_paths.add(obs_m["screenshot_relpath"])
        sw_m = await s_multi.sweep(
            SweepSpec(
                axes=[SweepAxis(control="omega", values=[2500, 2490])],
                readouts=["uc_over_u"],
                settle_ms=0,
            )
        )
        reading_paths.add(sw_m["readings_relpath"])
        for pt in sw_m["points"]:
            shot_paths.add(pt.screenshot_relpath)
    assert len(shot_paths) == 6
    assert len(reading_paths) == 2
    assert all(rp.startswith("readings/") for rp in reading_paths)
    verify_shared = shared_ws.verify_provenance()
    assert verify_shared.ok is True, f"Multi-session provenance errors: {verify_shared.errors}"

    # 7. Concurrent multi-session observe(annotate=True) + sweep() sharing one RunWorkspace
    conc_wm = WorkspaceManager(tmp_path / "shared_conc" / "runs")
    conc_ws = conc_wm.create_run(label="concurrent-session-prov", source="simulator_captured")
    conc_cfg = PeaConfig(
        project_root=tmp_path,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "shared_conc" / "runs",
        profiles_dir=tmp_path / "shared_conc" / "profiles",
    )
    conc_ps = ProfileStore.from_config(conc_cfg)

    async def _run_conc_worker(worker_idx: int) -> tuple[set[str], set[str], str]:
        p_w = _MockSimPage(
            initial_state={"R": 5.0, "omega": 2500.0 - worker_idx * 5.0},
            profile=prof_fix,
            readout_fn=_rlc_readout,
        )
        s_w = SimulatorSession(
            session_id=f"sim-conc-{worker_idx}",
            config=conc_cfg,
            workspace=conc_ws,
            ocr=_MockOcrBackend(p_w, prof_fix),
            profile_store=conc_ps,
            page=p_w,
            target_url="http://127.0.0.1:9999/index.html",
            loopback_origin="http://127.0.0.1:9999",
            stage_width=prof_fix.stage_width,
            stage_height=prof_fix.stage_height,
            profile=prof_fix,
            platform_name="darwin",
        )
        obs_w = await s_w.observe(annotate=True)
        sw_w = await s_w.sweep(
            SweepSpec(
                axes=[SweepAxis(control="omega", values=[2500 - worker_idx, 2490 - worker_idx])],
                readouts=["uc_over_u"],
                settle_ms=0,
            )
        )
        assert all("uc_over_u" in pt.readout_units for pt in sw_w["points"])
        shots_w = {obs_w["screenshot_relpath"]} | {pt.screenshot_relpath for pt in sw_w["points"]}
        anns_w = {obs_w["annotated_relpath"]}
        return shots_w, anns_w, sw_w["readings_relpath"]

    conc_results = await asyncio.gather(
        _run_conc_worker(0), _run_conc_worker(1), _run_conc_worker(2)
    )
    all_conc_shots = set().union(*(r[0] for r in conc_results))
    all_conc_anns = set().union(*(r[1] for r in conc_results))
    all_conc_readings = {r[2] for r in conc_results}
    assert len(all_conc_shots) == 9
    assert len(all_conc_anns) == 3
    assert len(all_conc_readings) == 3
    verify_conc = conc_ws.verify_provenance()
    assert verify_conc.ok is True, f"Concurrent provenance errors: {verify_conc.errors}"


# ---------------------------------------------------------------------------
# 5. M2B — HTML5/URL Fingerprinting & sim_watch Timed Transient Capture
# ---------------------------------------------------------------------------


def _build_transient_profile(sha256: str = SAMPLE_SHA256) -> CalibrationProfile:
    return CalibrationProfile(
        swf_sha256=sha256,
        stage_width=800,
        stage_height=600,
        header_height_px=39,
        controls={
            "R": ControlSpec(
                name="R",
                kind="stepper",
                input_xy=(120, 120),
                step=0.5,
                min_val=1.0,
                max_val=50.0,
                commit_mode="enter",
            ),
            "reset": ControlSpec(
                name="reset",
                kind="button",
                click_xy=(220, 120),
                commit_mode="click",
            ),
            "start": ControlSpec(
                name="start",
                kind="button",
                click_xy=(320, 120),
                commit_mode="click",
            ),
        },
        readouts={
            "Vc": ReadoutSpec(
                name="Vc",
                roi_px=(100, 200, 260, 240),
                ocr_mode="rec_only",
                parse_mode="float",
                unit_pattern=r"V",
            ),
            "t_sim": ReadoutSpec(
                name="t_sim",
                roi_px=(300, 200, 460, 240),
                ocr_mode="rec_only",
                parse_mode="float",
                unit_pattern=r"s",
            ),
        },
        blur_xy=(450, 18),
    )


class _TransientMockOcrBackend:
    """Pixel-indexed mock OCR decoding historical frame state (supports burst_post_ocr)."""

    def __init__(
        self,
        frame_texts: dict[int, dict[str, str]],
        *,
        ocr_delay_s: float = 0.0,
    ) -> None:
        self._frame_texts = frame_texts
        self._ocr_delay_s = ocr_delay_s
        self.read_calls: list[tuple[int, str]] = []

    @property
    def backend_name(self) -> str:
        return "rapidocr"

    def recognize(self, image: object, *, mode: OcrMode = "det_rec") -> OcrResult:
        return OcrResult(backend="rapidocr", mode=mode, elapsed_ms=0.1, boxes=[])

    def read_spec(
        self,
        image: Image.Image,
        spec: ReadoutSpec,
    ) -> tuple[OcrResult, float | int | str | None, str | None]:
        if self._ocr_delay_s > 0:
            time.sleep(self._ocr_delay_s)
        px = image.getpixel((0, 0))
        assert isinstance(px, tuple)
        shot_num = int(px[0]) + 255 * int(px[1])
        self.read_calls.append((shot_num, spec.name))
        txt = self._frame_texts.get(shot_num, {}).get(spec.name, "")

        class _SingleTextBackend:
            backend_name = "rapidocr"

            def recognize(self, crop: object, *, mode: OcrMode = "det_rec") -> OcrResult:
                boxes = (
                    [
                        OcrBox(
                            text=txt,
                            confidence=0.99,
                            bbox_px=(0, 0, 40, 20),
                            bbox_norm=(0.0, 0.0, 1.0, 1.0),
                        )
                    ]
                    if txt
                    else []
                )
                return OcrResult(backend="rapidocr", mode=mode, elapsed_ms=0.1, boxes=boxes)

            def read_spec(self, img: object, s: ReadoutSpec) -> tuple[OcrResult, Any, Any]:
                return read_spec_with_backend(self, img, s)  # type: ignore[arg-type]

        return read_spec_with_backend(_SingleTextBackend(), image, spec)  # type: ignore[arg-type]


def test_compute_html5_dir_sha256_determinism_confinement_and_errors(tmp_path: Path) -> None:
    dir_a = tmp_path / "sim_a"
    dir_b = tmp_path / "sim_b"
    for root in (dir_a, dir_b):
        (root / "js").mkdir(parents=True)
        (root / "index.html").write_text("<h1>RC Sim</h1>", encoding="utf-8")
        (root / "js" / "app.js").write_text("console.log(42);", encoding="utf-8")

    sha_a = compute_html5_dir_sha256(dir_a)
    sha_b = compute_html5_dir_sha256(dir_b)
    assert len(sha_a) == 64
    assert sha_a == sha_b

    # Content modification changes digest
    (dir_b / "js" / "app.js").write_text("console.log(43);", encoding="utf-8")
    assert compute_html5_dir_sha256(dir_b) != sha_a

    # Filename / path rename changes digest even with identical bytes
    dir_c = tmp_path / "sim_c"
    (dir_c / "js").mkdir(parents=True)
    (dir_c / "index.html").write_text("<h1>RC Sim</h1>", encoding="utf-8")
    (dir_c / "js" / "main.js").write_text("console.log(42);", encoding="utf-8")
    assert compute_html5_dir_sha256(dir_c) != sha_a

    # Confined internal symlink resolves cleanly and deterministically
    dir_sym = tmp_path / "sim_sym"
    dir_sym.mkdir()
    target_file = dir_sym / "real.txt"
    target_file.write_text("confined-data", encoding="utf-8")
    (dir_sym / "alias.txt").symlink_to(target_file)
    sha_sym = compute_html5_dir_sha256(dir_sym)
    assert len(sha_sym) == 64
    assert sha_sym == compute_html5_dir_sha256(dir_sym)

    # Escaping file symlink raises PathConfinementError
    outside_secret = tmp_path / "outside.txt"
    outside_secret.write_text("secret", encoding="utf-8")
    dir_escape = tmp_path / "sim_escape"
    dir_escape.mkdir()
    (dir_escape / "index.html").write_text("ok", encoding="utf-8")
    (dir_escape / "leak.txt").symlink_to(outside_secret)
    with pytest.raises(PathConfinementError):
        compute_html5_dir_sha256(dir_escape)

    # Escaping directory symlink raises PathConfinementError
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    dir_escape_sub = tmp_path / "sim_escape_sub"
    dir_escape_sub.mkdir()
    (dir_escape_sub / "index.html").write_text("ok", encoding="utf-8")
    (dir_escape_sub / "linked_out").symlink_to(outside_dir)
    with pytest.raises(PathConfinementError):
        compute_html5_dir_sha256(dir_escape_sub)

    # Empty directory (or only empty subdirs) raises ValueError
    empty_dir = tmp_path / "empty_dir"
    (empty_dir / "nested").mkdir(parents=True)
    with pytest.raises(ValueError, match="no files"):
        compute_html5_dir_sha256(empty_dir)

    # Missing path and non-directory file raise FileNotFoundError
    with pytest.raises(FileNotFoundError):
        compute_html5_dir_sha256(tmp_path / "does_not_exist")
    with pytest.raises(FileNotFoundError):
        compute_html5_dir_sha256(outside_secret)

    # Broken internal symlink raises FileNotFoundError (fails closed)
    dir_broken = tmp_path / "sim_broken_sym"
    dir_broken.mkdir()
    (dir_broken / "index.html").write_text("ok", encoding="utf-8")
    (dir_broken / "dangling.js").symlink_to(dir_broken / "missing_target.js")
    with pytest.raises(FileNotFoundError):
        compute_html5_dir_sha256(dir_broken)


def test_compute_url_sha256_canonicalization_and_validation() -> None:
    base = compute_url_sha256("https://phet.colorado.edu/")
    assert len(base) == 64
    assert compute_url_sha256("HTTPS://PhET.Colorado.EDU:443") == base
    assert compute_url_sha256("https://phet.colorado.edu/#fragment") == base
    assert compute_url_sha256("  https://phet.colorado.edu:443/#other ") == base

    http_base = compute_url_sha256("http://example.org/sim")
    assert compute_url_sha256("HTTP://EXAMPLE.ORG:80/sim#top") == http_base
    assert compute_url_sha256("http://example.org:8080/sim") != http_base
    assert compute_url_sha256("http://example.org/sim?mode=rc") != http_base
    assert compute_url_sha256("http://example.org/sim?mode=rlc") != compute_url_sha256(
        "http://example.org/sim?mode=rc"
    )
    assert compute_url_sha256("http://example.org/other") != http_base

    # IPv6 bracketing and default port stripping
    v6 = compute_url_sha256("http://[::1]/app")
    assert compute_url_sha256("http://[::1]:80/app#sec") == v6
    assert compute_url_sha256("http://[::1]:8080/app") != v6

    for bad_url in (
        "",
        "   ",
        "file:///etc/passwd",
        "ftp://example.com/sim",
        "javascript:alert(1)",
        "https:///no-host",
        "https://user:pass@example.com/sim",
        "https://example.com/has space",
    ):
        with pytest.raises(ValueError):
            compute_url_sha256(bad_url)


async def test_html5_and_url_session_save_profile_sha256_verification(tmp_path: Path) -> None:
    html5_dir = tmp_path / "html5_app"
    html5_dir.mkdir()
    (html5_dir / "index.html").write_text("<div id='sim'>ok</div>", encoding="utf-8")
    html5_sha = compute_html5_dir_sha256(html5_dir)

    profile = _build_transient_profile(sha256=html5_sha)
    frame_texts = {1: {"Vc": "10.00 V", "t_sim": "0.00 s"}}
    page = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "10.00 V",
    )
    ocr = _TransientMockOcrBackend(frame_texts)
    cfg = PeaConfig(
        project_root=REPO_ROOT,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
    )
    ws = WorkspaceManager.from_config(cfg).create_run(source="simulator_captured")
    store = ProfileStore.from_config(cfg)

    session = SimulatorSession(
        session_id="sim-html5-01",
        config=cfg,
        workspace=ws,
        ocr=ocr,  # type: ignore[arg-type]
        profile_store=store,
        page=page,
        target_url="http://127.0.0.1:50001/",
        loopback_origin="http://127.0.0.1:50001",
        stage_width=800,
        stage_height=600,
        header_height_px=39,
        swf_meta=None,
        sim_sha256=html5_sha,
        platform_name="darwin",
    )

    # Matching sim_sha256 succeeds
    saved = await session.save_profile(profile, verify_live=True)
    assert saved["swf_sha256"] == html5_sha
    assert Path(saved["saved_path"]).is_file()

    # Mismatched swf_sha256 is rejected even when swf_meta is None
    wrong_profile = profile.model_copy(update={"swf_sha256": "a" * 64})
    with pytest.raises(ValueError, match="does not match loaded simulator"):
        await session.save_profile(wrong_profile, verify_live=False)

    # SimulatorManager.open(url=...) computes sim_sha256 and auto-loads matching saved profile
    url = "https://phet.colorado.edu/sims/html/rc/index.html"
    url_sha = compute_url_sha256(url)
    url_profile = CalibrationProfile(
        swf_sha256=url_sha,
        stage_width=1024,
        stage_height=768,
        header_height_px=0,
        readouts={
            "Vc": ReadoutSpec(
                name="Vc",
                roi_px=(10, 10, 100, 40),
                ocr_mode="rec_only",
                parse_mode="float",
            )
        },
    )
    cfg_url = PeaConfig(
        project_root=REPO_ROOT,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "runs_url",
        profiles_dir=tmp_path / "profiles_url",
        allowed_origins=("https://phet.colorado.edu",),
    )
    url_store = ProfileStore.from_config(cfg_url)
    url_store.save(url_profile, expected_stage=(1024, 768))

    class _FakePage:
        async def route(self, _pattern: str, _handler: Any) -> None:
            return

        def on(self, _event: str, _handler: Any) -> None:
            return

        async def goto(self, _target: str, *, wait_until: str = "domcontentloaded") -> None:
            return

        async def wait_for_timeout(self, _ms: int) -> None:
            return

        async def close(self) -> None:
            return

    class _FakeContext:
        async def new_page(self) -> _FakePage:
            return _FakePage()

        async def close(self) -> None:
            return

    class _FakeBrowser:
        async def new_context(self, **_kwargs: Any) -> _FakeContext:
            return _FakeContext()

    mgr = SimulatorManager(cfg_url, profile_store=url_store, ocr=ocr)  # type: ignore[arg-type]

    async def _fake_ensure_browser() -> _FakeBrowser:
        return _FakeBrowser()

    mgr._ensure_browser = _fake_ensure_browser  # type: ignore[method-assign]
    opened = await mgr.open(url=url, settle_ms=0)
    try:
        assert opened.sim_sha256 == url_sha
        assert opened.profile is not None
        assert opened.profile.swf_sha256 == url_sha
    finally:
        await mgr.close_session(opened.session_id)


async def test_sim_watch_burst_post_ocr_happy_path_and_data_analyze_handoff(
    tmp_path: Path,
) -> None:
    profile = _build_transient_profile()
    frame_texts: dict[int, dict[str, str]] = {}
    t_start: list[float] = []

    def _record_rc_frame(shot_num: int) -> None:
        now = time.perf_counter()
        if not t_start:
            t_start.append(now)
        dt = max(0.0, now - t_start[0])
        vc = 10.0 * math.exp(-dt / 1.0)
        frame_texts[shot_num] = {"Vc": f"{vc:.4f} V", "t_sim": f"{dt:.4f} s"}

    page = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "10.00 V",
        on_screenshot_hook=_record_rc_frame,
    )
    # Add 15ms per-readout OCR latency to prove actual_duration_s excludes post-OCR time!
    ocr = _TransientMockOcrBackend(frame_texts, ocr_delay_s=0.015)
    session = _make_session(tmp_path, profile=profile, page=page, ocr=ocr)  # type: ignore[arg-type]

    spec = WatchSpec(
        readouts=["Vc", "t_sim"],
        duration_s=0.10,
        interval_ms=25,
        save_dataset=True,
    )
    res = await session.watch(spec)
    assert isinstance(res, WatchResult)
    assert res.pacing_mode == "burst_post_ocr"
    assert res.status == "complete"
    assert res.captured_frames == len(res.points)
    assert res.captured_frames >= 3
    assert res.missing_readouts == 0
    # actual_duration_s must reflect capture loop (~0.10s), NOT post-OCR (which adds >0.1s)
    assert res.actual_duration_s < 0.25
    assert res.readings_relpath.startswith("readings/watch-")
    assert res.dataset_id == "watch-0001"
    assert res.dataset_relpath == "datasets/watch-0001.json"

    # Monotonic timing and unique hashed evidence frames
    elapsed_vals = [pt.elapsed_s for pt in res.points]
    assert all(e is not None and e >= 0.0 for e in elapsed_vals)
    assert elapsed_vals == sorted(elapsed_vals)  # type: ignore[type-var]
    for pt in res.points:
        assert pt.elapsed_s is not None
        assert pt.frame_timestamp_ms == pytest.approx(pt.elapsed_s * 1000.0)
        assert pt.screenshot_relpath.startswith("evidence/watch-")
        assert pt.readout_units["Vc"] == "V"
        assert pt.readout_units["t_sim"] == "s"
    assert len({pt.screenshot_relpath for pt in res.points}) == res.captured_frames
    assert len({pt.screenshot_sha256 for pt in res.points}) == res.captured_frames

    # Status metadata updated with latest screenshot
    status = session.workspace.read_status()
    assert status.phase == "complete"
    assert status.metadata["latest_screenshot_relpath"] == res.points[-1].screenshot_relpath

    # Dataset handoff into data_analyze recovers exponential RC decay cleanly
    analysis = data_analyze(
        res.dataset_id,
        session.workspace,
        derived_columns=[DerivedColumnSpec(name="ln_vc", formula="log(Vc)")],
        regressions=[
            RegressionSpec(name="rc_decay", x_column="elapsed_s", y_column="ln_vc")
        ],
    )
    assert analysis.source == "simulator_captured"
    assert analysis.row_count == res.captured_frames
    assert analysis.regressions[0].r_squared > 0.99
    assert analysis.regressions[0].slope == pytest.approx(-1.0, rel=0.15)

    # Consecutive watch call auto-increments dataset ID without collision
    res2 = await session.watch(
        WatchSpec(readouts=["Vc"], duration_s=0.05, interval_ms=25, save_dataset=True)
    )
    assert res2.dataset_id == "watch-0002"
    assert res2.dataset_relpath == "datasets/watch-0002.json"

    # Explicit colliding dataset_id fails fast BEFORE touching the simulator page
    shots_before = page.screenshot_count
    with pytest.raises(FileExistsError, match="already exists"):
        await session.watch(
            WatchSpec(
                readouts=["Vc"],
                duration_s=0.05,
                interval_ms=25,
                save_dataset=True,
                dataset_id="watch-0001",
            )
        )
    assert page.screenshot_count == shots_before
    assert session.workspace.verify_provenance().ok is True


async def test_sim_watch_inline_ocr_stability_stop_and_ocr_miss_anchor_reset(
    tmp_path: Path,
) -> None:
    profile = _build_transient_profile()
    # Shots 1..3 settle to 1.00 V; shot 4 is 1.00 V; shot 5 is an OCR miss ("");
    # shots 6..12 stay at 1.00 V (+/- 0.01). The OCR miss MUST reset the stability anchor!
    seq_values = [
        "10.00 V",
        "5.00 V",
        "1.00 V",
        "1.00 V",
        "",  # Shot 5: OCR miss -> resets stability anchor!
        "1.00 V",  # Shot 6: re-anchors (valid_count=1)
        "1.01 V",  # Shot 7: valid_count=2
        "0.99 V",  # Shot 8: valid_count=3
        "1.00 V",  # Shot 9: valid_count=4
        "1.00 V",  # Shot 10
        "1.00 V",  # Shot 11
        "1.00 V",  # Shot 12
    ]
    frame_texts = {idx + 1: {"Vc": val} for idx, val in enumerate(seq_values)}
    page = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "1.00 V",
    )
    ocr = _TransientMockOcrBackend(frame_texts)
    session = _make_session(tmp_path, profile=profile, page=page, ocr=ocr)  # type: ignore[arg-type]

    res = await session.watch(
        WatchSpec(
            readouts=["Vc"],
            duration_s=2.0,
            interval_ms=25,
            stop_when_stable_s=0.06,
            stability_tol=0.02,
            save_dataset=True,
        )
    )
    assert res.pacing_mode == "inline_ocr"
    assert res.status == "stopped_stable"
    assert res.missing_readouts == 1
    # Shot 5 (index 4) MUST remain None (zero fabrication) and stability must require
    # frames AFTER index 4 spanning >= 0.06s (so index >= 7, i.e., >= 8 frames).
    assert res.points[4].parsed_value is None
    assert res.points[4].readouts["Vc"] is None
    assert res.captured_frames >= 8
    anchor_t = res.points[5].elapsed_s
    final_t = res.points[-1].elapsed_s
    assert anchor_t is not None and final_t is not None
    assert (final_t - anchor_t) >= 0.06 - 1e-6
    assert session.workspace.verify_provenance().ok is True


@pytest.mark.parametrize(
    ("stop_op", "stop_value", "sequence", "expected_frames"),
    [
        ("le", 2.0, ["", "5.0 V", "2.0 V", "1.0 V"], 3),
        ("lt", 2.0, ["", "5.0 V", "2.0 V", "1.9 V", "1.0 V"], 4),
        ("ge", 8.0, ["", "5.0 V", "8.0 V", "9.0 V"], 3),
        ("gt", 8.0, ["", "5.0 V", "8.0 V", "8.1 V", "9.0 V"], 4),
    ],
)
async def test_sim_watch_inline_ocr_threshold_stop_operators_and_miss_guard(
    tmp_path: Path,
    stop_op: str,
    stop_value: float,
    sequence: list[str],
    expected_frames: int,
) -> None:
    profile = _build_transient_profile()
    frame_texts = {idx + 1: {"Vc": val} for idx, val in enumerate(sequence)}
    page = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "0.0 V",
    )
    ocr = _TransientMockOcrBackend(frame_texts)
    session = _make_session(
        tmp_path / f"thr_{stop_op}",
        profile=profile,
        page=page,
        ocr=ocr,  # type: ignore[arg-type]
    )

    res = await session.watch(
        WatchSpec(
            readouts=["Vc"],
            duration_s=2.0,
            interval_ms=15,
            stop_readout="Vc",
            stop_op=stop_op,  # type: ignore[arg-type]
            stop_value=stop_value,
            save_dataset=False,
        )
    )
    assert res.pacing_mode == "inline_ocr"
    assert res.status == "stopped_threshold"
    assert res.captured_frames == expected_frames
    assert res.missing_readouts == 1  # Leading OCR miss did NOT falsely trigger stop
    assert res.points[0].parsed_value is None
    assert session.workspace.verify_provenance().ok is True


async def test_sim_watch_setup_trigger_actions_settle_and_blur_ordering(tmp_path: Path) -> None:
    profile = _build_transient_profile()
    timeline: list[Any] = []

    def _on_shot(shot_num: int) -> None:
        timeline.append(("screenshot", shot_num))

    page = _MockSimPage(
        initial_state={"R": 2.0},
        profile=profile,
        readout_fn=lambda _n, _st: "10.0 V",
        on_screenshot_hook=_on_shot,
    )
    orig_click = page.on_click
    orig_wait = page.wait_for_timeout

    def _traced_click(x: int, y: int) -> None:
        timeline.append(("click", x, y))
        orig_click(x, y)

    async def _traced_wait(ms: int) -> None:
        timeline.append(("wait", ms))
        await orig_wait(ms)

    page.on_click = _traced_click  # type: ignore[method-assign]
    page.wait_for_timeout = _traced_wait  # type: ignore[method-assign]

    frame_texts = {i: {"Vc": "10.0 V"} for i in range(1, 10)}
    ocr = _TransientMockOcrBackend(frame_texts)
    session = _make_session(tmp_path, profile=profile, page=page, ocr=ocr)  # type: ignore[arg-type]

    res = await session.watch(
        WatchSpec(
            fixed_controls={"R": 5.0},
            settle_ms=15,
            blur_before_capture=True,
            trigger_actions=[
                ActionStep(action="click", control="reset"),
                ActionStep(action="click", control="start"),
            ],
            readouts=["Vc"],
            duration_s=0.05,
            interval_ms=25,
            max_frames=2,
            save_dataset=False,
        )
    )
    assert res.captured_frames == 2
    assert page.state["R"] == pytest.approx(5.0)

    # Verify strict order: fixed_control R -> wait(15) -> blur(450, 18) -> reset -> start -> shot 1
    idx_wait = timeline.index(("wait", 15))
    idx_blur = timeline.index(("click", 450, 18), idx_wait)
    idx_reset = timeline.index(("click", 220, 120), idx_blur)
    idx_start = timeline.index(("click", 320, 120), idx_reset)
    idx_shot1 = timeline.index(("screenshot", 1), idx_start)
    assert idx_wait < idx_blur < idx_reset < idx_start < idx_shot1

    # Pre-flight validation rejects unknown fixed_controls, readouts, or trigger controls
    shots_before = page.screenshot_count
    with pytest.raises(KeyError, match="Fixed control"):
        await session.watch(
            WatchSpec(fixed_controls={"NOPE": 1}, readouts=["Vc"], duration_s=0.1, interval_ms=20)
        )
    with pytest.raises(KeyError, match="Readout"):
        await session.watch(WatchSpec(readouts=["NOPE"], duration_s=0.1, interval_ms=20))
    with pytest.raises(KeyError, match="Trigger control"):
        await session.watch(
            WatchSpec(
                readouts=["Vc"],
                duration_s=0.1,
                interval_ms=20,
                trigger_action=ActionStep(action="click", control="NOPE"),
            )
        )
    assert page.screenshot_count == shots_before


async def test_sim_watch_max_frames_cap_and_save_dataset_false(tmp_path: Path) -> None:
    profile = _build_transient_profile()
    frame_texts = {i: {"Vc": f"{10.0 - i:.2f} V"} for i in range(1, 20)}
    page = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "5.0 V",
    )
    ocr = _TransientMockOcrBackend(frame_texts)
    session = _make_session(tmp_path, profile=profile, page=page, ocr=ocr)  # type: ignore[arg-type]

    res = await session.watch(
        WatchSpec(
            readouts=["Vc"],
            duration_s=5.0,
            interval_ms=15,
            max_frames=3,
            save_dataset=False,
        )
    )
    assert res.status == "max_frames_reached"
    assert res.captured_frames == 3
    assert len(res.points) == 3
    assert res.dataset_id is None
    assert res.dataset_relpath is None
    assert not (session.workspace.root / "datasets").exists()
    assert session.workspace.verify_provenance().ok is True


async def test_sim_watch_cooperative_cancel_task_cancel_and_mid_burst_error_recovery(
    tmp_path: Path,
) -> None:
    profile = _build_transient_profile()
    frame_texts = {i: {"Vc": f"{10.0 - 0.5 * i:.2f} V"} for i in range(1, 30)}

    # (a) Cooperative request_cancel() wakes immediately during a long 5000ms interval
    page_coop = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "9.0 V",
    )
    sess_coop = _make_session(
        tmp_path / "coop_cancel",
        profile=profile,
        page=page_coop,
        ocr=_TransientMockOcrBackend(frame_texts),  # type: ignore[arg-type]
    )

    async def _trigger_cancel_soon() -> None:
        await asyncio.sleep(0.04)
        sess_coop.request_cancel()

    t_wall0 = time.perf_counter()
    cancel_task = asyncio.create_task(_trigger_cancel_soon())
    res_coop = await sess_coop.watch(
        WatchSpec(
            readouts=["Vc"],
            duration_s=10.0,
            interval_ms=5000,
            save_dataset=True,
        )
    )
    await cancel_task
    wall_elapsed = time.perf_counter() - t_wall0
    assert wall_elapsed < 1.0  # Woke immediately instead of waiting 5s
    assert res_coop.status == "cancelled"
    assert res_coop.captured_frames == 1
    assert res_coop.dataset_id == "watch-0001"
    assert sess_coop.workspace.read_status().phase == "cancelled"
    assert sess_coop.workspace.verify_provenance().ok is True

    # (b) External asyncio.Task.cancel() during burst_post_ocr flushes buffered PNGs
    page_ext = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "8.0 V",
    )
    sess_ext = _make_session(
        tmp_path / "task_cancel",
        profile=profile,
        page=page_ext,
        ocr=_TransientMockOcrBackend(frame_texts),  # type: ignore[arg-type]
    )
    watch_task = asyncio.create_task(
        sess_ext.watch(
            WatchSpec(readouts=["Vc"], duration_s=5.0, interval_ms=25, save_dataset=True)
        )
    )
    await asyncio.sleep(0.07)
    watch_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watch_task
    assert sess_ext.workspace.read_status().phase == "cancelled"
    ext_partial = json.loads(
        (sess_ext.workspace.root / "partial-readings.json").read_text("utf-8")
    )
    assert ext_partial["status"] == "cancelled"
    assert ext_partial["count"] >= 2
    assert len(list(sess_ext.workspace.evidence_dir.glob("watch-*.png"))) == ext_partial["count"]
    assert sess_ext.workspace.verify_provenance().ok is True

    # (c) Mid-burst RuntimeError on 3rd screenshot flushes frames 0 & 1 before re-raising
    page_err = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "7.0 V",
    )
    orig_shot = page_err.screenshot

    async def _boom_on_third(*, type: str = "png") -> bytes:  # noqa: A002
        if page_err.screenshot_count >= 2:
            raise RuntimeError("simulated burst disconnect")
        return await orig_shot(type=type)

    page_err.screenshot = _boom_on_third  # type: ignore[method-assign]
    sess_err = _make_session(
        tmp_path / "burst_error",
        profile=profile,
        page=page_err,
        ocr=_TransientMockOcrBackend(frame_texts),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="simulated burst disconnect"):
        await sess_err.watch(
            WatchSpec(readouts=["Vc"], duration_s=5.0, interval_ms=15, save_dataset=True)
        )
    assert sess_err.workspace.read_status().phase == "failed"
    err_partial = json.loads(
        (sess_err.workspace.root / "partial-readings.json").read_text("utf-8")
    )
    assert err_partial["status"] == "failed"
    assert err_partial["count"] == 2
    assert len(list(sess_err.workspace.evidence_dir.glob("watch-*.png"))) == 2
    assert sess_err.workspace.verify_provenance().ok is True


async def test_protocols_m2_sim_watch_and_compute_sim_delegation(tmp_path: Path) -> None:
    profile = _build_transient_profile()
    frame_texts = {i: {"Vc": f"{5.0 - 0.1 * i:.2f} V"} for i in range(1, 10)}
    page = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "5.0 V",
    )
    session = _make_session(
        tmp_path,
        profile=profile,
        page=page,
        ocr=_TransientMockOcrBackend(frame_texts),  # type: ignore[arg-type]
    )

    # Accepts both WatchSpec and dict payloads via protocols_m2.sim_watch(session, spec)
    watch_spec = WatchSpec(readouts=["Vc"], duration_s=0.05, interval_ms=20, max_frames=2)
    res_model = await sim_watch(session, watch_spec)
    assert isinstance(res_model, WatchResult)
    assert res_model.captured_frames == 2

    res_dict = await sim_watch(
        session,
        {"readouts": ["Vc"], "duration_s": 0.05, "interval_ms": 20, "max_frames": 2},
    )
    assert isinstance(res_dict, WatchResult)
    assert res_dict.captured_frames == 2
    assert session.workspace.verify_provenance().ok is True

    # Accepts both ComputeSimSpec and dict payloads via protocols_m2.compute_sim(spec, workspace)
    compute_spec = ComputeSimSpec(
        model_kind="algebraic_grid",
        expression="1 / sqrt(1 + w**2)",
        grid_variable="w",
        grid_start=0.0,
        grid_stop=10.0,
        grid_points=21,
    )
    comp_model = compute_sim(compute_spec, session.workspace)
    assert isinstance(comp_model, ComputeSimResult)
    assert comp_model.provenance == "model_computed"
    assert comp_model.row_count == 21
    assert comp_model.columns == ["w", "y"]
    assert comp_model.dataset_id == "compute-algebraic_grid-0001"
    assert len(comp_model.preview_rows) == 10
    assert math.isclose(comp_model.preview_rows[0]["y"], 1.0, rel_tol=1e-12)
    assert math.isclose(comp_model.preview_rows[-1]["y"], 1.0 / math.sqrt(101.0), rel_tol=1e-12)

    comp_dict = compute_sim(
        {
            "model_kind": "algebraic_grid",
            "expression": "2 * w + 1",
            "grid_variable": "w",
            "grid_start": 0.0,
            "grid_stop": 4.0,
            "grid_points": 5,
        },
        session.workspace,
    )
    assert isinstance(comp_dict, ComputeSimResult)
    assert comp_dict.provenance == "model_computed"
    assert comp_dict.dataset_id == "compute-algebraic_grid-0002"
    assert len(comp_dict.preview_rows) == 5

    prov_entries = session.workspace.read_provenance()
    target_relpaths = {comp_model.dataset_relpath, comp_dict.dataset_relpath}
    compute_entries = [e for e in prov_entries if e.relpath in target_relpaths]
    assert len(compute_entries) == 2
    assert all(e.source == "model_computed" for e in compute_entries)
    assert session.workspace.verify_provenance().ok is True


async def test_sim_watch_edge_cases_zero_frame_cancel_custom_id_multi_readout_and_concurrent(
    tmp_path: Path,
) -> None:
    profile = _build_transient_profile()

    # 1. Calling watch() without an attached profile fails fast without touching the page
    page_no_prof = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "1.0 V",
    )
    cfg = PeaConfig(
        project_root=REPO_ROOT,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "runs",
        profiles_dir=tmp_path / "profiles",
    )
    ws_no_prof = WorkspaceManager.from_config(cfg).create_run(source="simulator_captured")
    sess_no_prof = SimulatorSession(
        session_id="sim-noprof",
        config=cfg,
        workspace=ws_no_prof,
        ocr=_TransientMockOcrBackend({}),  # type: ignore[arg-type]
        profile_store=ProfileStore.from_config(cfg),
        page=page_no_prof,
        target_url="http://127.0.0.1:50002/",
        loopback_origin="http://127.0.0.1:50002",
        stage_width=800,
        stage_height=600,
        profile=None,
    )
    with pytest.raises(RuntimeError, match="No CalibrationProfile"):
        await sess_no_prof.watch(WatchSpec(readouts=["Vc"], duration_s=0.1, interval_ms=20))
    assert page_no_prof.screenshot_count == 0

    # 2. Cooperative cancel BEFORE frame 0 (during settle_ms) returns valid 0-frame WatchResult
    page_zero = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "1.0 V",
    )
    sess_zero = _make_session(
        tmp_path / "zero_cancel",
        profile=profile,
        page=page_zero,
        ocr=_TransientMockOcrBackend({}),  # type: ignore[arg-type]
    )

    async def _cancel_during_settle(_ms: int) -> None:
        sess_zero.request_cancel()

    page_zero.wait_for_timeout = _cancel_during_settle  # type: ignore[method-assign]
    res_zero = await sess_zero.watch(
        WatchSpec(
            readouts=["Vc"],
            duration_s=1.0,
            interval_ms=25,
            settle_ms=10,
            save_dataset=True,
        )
    )
    assert res_zero.status == "cancelled"
    assert res_zero.captured_frames == 0
    assert res_zero.points == []
    assert res_zero.dataset_id is None
    assert res_zero.dataset_relpath is None
    assert sess_zero.workspace.read_status().phase == "cancelled"
    assert sess_zero.workspace.verify_provenance().ok is True

    # 3. Custom non-colliding dataset_id + multi-readout stability (2nd readout drift prevents stop)
    multi_frames: dict[int, dict[str, str]] = {
        1: {"Vc": "1.00 V", "t_sim": "0.10 s"},
        2: {"Vc": "1.00 V", "t_sim": "0.20 s"},
        3: {"Vc": "1.00 V", "t_sim": "0.30 s"},
        4: {"Vc": "1.00 V", "t_sim": "0.30 s"},
        5: {"Vc": "1.00 V", "t_sim": "0.30 s"},
        6: {"Vc": "1.00 V", "t_sim": "0.30 s"},
        7: {"Vc": "1.00 V", "t_sim": "0.30 s"},
    }
    page_multi = _MockSimPage(
        initial_state={"R": 10.0},
        profile=profile,
        readout_fn=lambda _n, _st: "1.00 V",
    )
    sess_multi = _make_session(
        tmp_path / "multi_stable",
        profile=profile,
        page=page_multi,
        ocr=_TransientMockOcrBackend(multi_frames),  # type: ignore[arg-type]
    )
    res_multi = await sess_multi.watch(
        WatchSpec(
            readouts=["Vc", "t_sim"],
            duration_s=2.0,
            interval_ms=20,
            stop_when_stable_s=0.04,
            stability_tol=0.005,
            save_dataset=True,
            dataset_id="rc_trial_custom",
        )
    )
    assert res_multi.status == "stopped_stable"
    assert res_multi.dataset_id == "rc_trial_custom"
    assert res_multi.dataset_relpath == "datasets/rc_trial_custom.json"
    # t_sim changed on shots 1->2->3, so stability could only anchor at shot 3 (index 2)
    assert res_multi.captured_frames >= 5
    assert sess_multi.workspace.verify_provenance().ok is True

    # 4. Concurrent multi-session watch() calls sharing one RunWorkspace produce unique artifacts
    shared_wm = WorkspaceManager(tmp_path / "conc_watch" / "runs")
    shared_ws = shared_wm.create_run(label="concurrent-watch", source="simulator_captured")
    shared_cfg = PeaConfig(
        project_root=REPO_ROOT,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "conc_watch" / "runs",
        profiles_dir=tmp_path / "conc_watch" / "profiles",
    )
    shared_ps = ProfileStore.from_config(shared_cfg)
    conc_frames = {i: {"Vc": f"{10.0 - 0.2 * i:.2f} V"} for i in range(1, 20)}

    async def _watch_worker(worker_idx: int) -> WatchResult:
        p_w = _MockSimPage(
            initial_state={"R": 10.0},
            profile=profile,
            readout_fn=lambda _n, _st: "5.0 V",
        )
        s_w = SimulatorSession(
            session_id=f"sim-watch-conc-{worker_idx}",
            config=shared_cfg,
            workspace=shared_ws,
            ocr=_TransientMockOcrBackend(conc_frames),  # type: ignore[arg-type]
            profile_store=shared_ps,
            page=p_w,
            target_url="http://127.0.0.1:50009/",
            loopback_origin="http://127.0.0.1:50009",
            stage_width=profile.stage_width,
            stage_height=profile.stage_height,
            profile=profile,
        )
        return await s_w.watch(
            WatchSpec(readouts=["Vc"], duration_s=0.05, interval_ms=20, max_frames=2)
        )

    w_results = await asyncio.gather(_watch_worker(0), _watch_worker(1), _watch_worker(2))
    assert len({r.dataset_id for r in w_results}) == 3
    assert len({r.readings_relpath for r in w_results}) == 3
    all_shots = {pt.screenshot_relpath for r in w_results for pt in r.points}
    assert len(all_shots) == sum(r.captured_frames for r in w_results)
    assert shared_ws.verify_provenance().ok is True


async def test_sim_watch_reserved_columns_settle_cancel_drift_and_combined_stops(
    tmp_path: Path,
) -> None:
    # 1. Reserved dataset column collision fails fast when save_dataset=True, succeeds when False
    prof_res = _build_transient_profile()
    prof_res.readouts["elapsed_s"] = ReadoutSpec(
        name="elapsed_s",
        roi_px=(10, 10, 80, 40),
        ocr_mode="rec_only",
        parse_mode="float",
    )
    prof_res.readouts["screenshot_relpath"] = ReadoutSpec(
        name="screenshot_relpath",
        roi_px=(10, 50, 80, 80),
        ocr_mode="rec_only",
        parse_mode="float",
    )
    page_res = _MockSimPage(
        initial_state={"R": 10.0},
        profile=prof_res,
        readout_fn=lambda _n, _st: "42.0",
    )
    ocr_res = _TransientMockOcrBackend(
        {1: {"elapsed_s": "999.0", "screenshot_relpath": "42.0"}}
    )
    sess_res = _make_session(
        tmp_path / "reserved_col",
        profile=prof_res,
        page=page_res,
        ocr=ocr_res,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="collide with reserved watch dataset columns"):
        await sess_res.watch(
            WatchSpec(
                readouts=["elapsed_s"],
                duration_s=0.05,
                interval_ms=20,
                save_dataset=True,
            )
        )
    with pytest.raises(ValueError, match="collide with reserved watch dataset columns"):
        await sess_res.watch(
            WatchSpec(
                readouts=["screenshot_relpath"],
                duration_s=0.05,
                interval_ms=20,
                save_dataset=True,
            )
        )
    assert page_res.screenshot_count == 0
    ok_no_ds = await sess_res.watch(
        WatchSpec(
            readouts=["elapsed_s", "screenshot_relpath"],
            duration_s=0.005,
            interval_ms=50,
            save_dataset=False,
        )
    )
    # Also verifies duration_s < interval_ms yields exactly 1 immediate frame
    assert ok_no_ds.status == "complete"
    assert ok_no_ds.captured_frames == 1
    assert ok_no_ds.points[0].readouts["elapsed_s"] == pytest.approx(999.0)
    assert ok_no_ds.points[0].readouts["screenshot_relpath"] == pytest.approx(42.0)

    # 2. Cooperative cancel during long settle_ms wakes immediately & skips blur + triggers
    prof_settle = _build_transient_profile()
    clicks_seen: list[tuple[int, int]] = []
    page_settle = _MockSimPage(
        initial_state={"R": 10.0},
        profile=prof_settle,
        readout_fn=lambda _n, _st: "10.0 V",
    )
    orig_click = page_settle.on_click

    def _track_click(x: int, y: int) -> None:
        clicks_seen.append((x, y))
        orig_click(x, y)

    async def _real_sleep_wait(ms: int) -> None:
        await asyncio.sleep(ms / 1000.0)

    page_settle.on_click = _track_click  # type: ignore[method-assign]
    page_settle.wait_for_timeout = _real_sleep_wait  # type: ignore[method-assign]
    sess_settle = _make_session(
        tmp_path / "settle_cancel",
        profile=prof_settle,
        page=page_settle,
        ocr=_TransientMockOcrBackend({}),  # type: ignore[arg-type]
    )

    async def _cancel_during_long_settle() -> None:
        await asyncio.sleep(0.03)
        sess_settle.request_cancel()

    t_wall0 = time.perf_counter()
    c_task = asyncio.create_task(_cancel_during_long_settle())
    res_settle = await sess_settle.watch(
        WatchSpec(
            readouts=["Vc"],
            duration_s=2.0,
            interval_ms=25,
            settle_ms=3000,
            blur_before_capture=True,
            trigger_action=ActionStep(action="click", control="start"),
            save_dataset=True,
        )
    )
    await c_task
    assert (time.perf_counter() - t_wall0) < 1.0
    assert res_settle.status == "cancelled"
    assert res_settle.captured_frames == 0
    assert res_settle.actual_duration_s == 0.0
    assert clicks_seen == []  # Neither blur nor start trigger executed after cancellation
    assert page_settle.screenshot_count == 0
    assert sess_settle.workspace.read_status().phase == "cancelled"
    assert sess_settle.workspace.verify_provenance().ok is True

    # 3. Single slow frame recovers onto original t0 grid without a back-to-back burst storm
    prof_stall = _build_transient_profile()
    page_stall = _MockSimPage(
        initial_state={"R": 10.0},
        profile=prof_stall,
        readout_fn=lambda _n, _st: "5.0 V",
    )
    orig_shot_stall = page_stall.screenshot

    async def _stall_frame0(*, type: str = "png") -> bytes:  # noqa: A002
        if page_stall.screenshot_count == 0:
            await asyncio.sleep(0.11)
        return await orig_shot_stall(type=type)

    page_stall.screenshot = _stall_frame0  # type: ignore[method-assign]
    sess_stall = _make_session(
        tmp_path / "stall_drift",
        profile=prof_stall,
        page=page_stall,
        ocr=_TransientMockOcrBackend({i: {"Vc": "5.0 V"} for i in range(1, 30)}),  # type: ignore[arg-type]
    )
    res_stall = await sess_stall.watch(
        WatchSpec(readouts=["Vc"], duration_s=0.22, interval_ms=30, save_dataset=False)
    )
    assert res_stall.status == "complete"
    # Without missed-slot skipping, frame 0 stalling 110ms would burst 4 frames within ~15ms;
    # with slot skipping, frame 1 fires immediately after the stall and frames 2+ resume 30ms grid.
    assert res_stall.captured_frames <= 6
    assert res_stall.captured_frames >= 3
    if len(res_stall.points) >= 4:
        # Frames after the immediate recovery frame must be paced near interval_ms (~30ms)
        post_gaps = [
            (res_stall.points[i + 1].elapsed_s or 0.0) - (res_stall.points[i].elapsed_s or 0.0)
            for i in range(2, len(res_stall.points) - 1)
        ]
        assert all(gap >= 0.015 for gap in post_gaps)

    # 4. Simultaneous stop_when_stable_s + threshold stop: threshold wins vs. stability wins
    prof_combo = _build_transient_profile()
    # (a) Threshold hits before stability duration
    page_c1 = _MockSimPage(
        initial_state={"R": 10.0},
        profile=prof_combo,
        readout_fn=lambda _n, _st: "1.0 V",
    )
    sess_c1 = _make_session(
        tmp_path / "combo_thr",
        profile=prof_combo,
        page=page_c1,
        ocr=_TransientMockOcrBackend({1: {"Vc": "5.0 V"}, 2: {"Vc": "3.0 V"}, 3: {"Vc": "1.5 V"}}),  # type: ignore[arg-type]
    )
    res_c1 = await sess_c1.watch(
        WatchSpec(
            readouts=["Vc"],
            duration_s=2.0,
            interval_ms=20,
            stop_when_stable_s=0.20,
            stability_tol=0.0,
            stop_readout="Vc",
            stop_op="le",
            stop_value=2.0,
            save_dataset=False,
        )
    )
    assert res_c1.status == "stopped_threshold"
    assert res_c1.captured_frames == 3

    # (b) Stability fires first with stability_tol=0.0 (1e-4 drift re-anchors, repeats stop)
    page_c2 = _MockSimPage(
        initial_state={"R": 10.0},
        profile=prof_combo,
        readout_fn=lambda _n, _st: "3.0 V",
    )
    sess_c2 = _make_session(
        tmp_path / "combo_stab",
        profile=prof_combo,
        page=page_c2,
        ocr=_TransientMockOcrBackend(  # type: ignore[arg-type]
            {
                1: {"Vc": "3.0000 V"},
                2: {"Vc": "3.0001 V"},  # Drift > 0.0 tol -> re-anchors here
                3: {"Vc": "3.0001 V"},
                4: {"Vc": "3.0001 V"},
                5: {"Vc": "3.0001 V"},
                6: {"Vc": "3.0001 V"},
            }
        ),
    )
    res_c2 = await sess_c2.watch(
        WatchSpec(
            readouts=["Vc"],
            duration_s=2.0,
            interval_ms=20,
            stop_when_stable_s=0.05,
            stability_tol=0.0,
            stop_readout="Vc",
            stop_op="le",
            stop_value=2.0,
            save_dataset=False,
        )
    )
    assert res_c2.status == "stopped_stable"
    assert res_c2.captured_frames >= 5

    # 5. Unrelated datasets (e.g. lab5-2026.json) must not pollute watch-0001 auto-numbering
    sess_num = _make_session(
        tmp_path / "watch_num",
        profile=prof_combo,
        page=_MockSimPage(
            initial_state={"R": 10.0}, profile=prof_combo, readout_fn=lambda _n, _st: "5.0 V"
        ),
        ocr=_TransientMockOcrBackend({1: {"Vc": "5.0 V"}}),  # type: ignore[arg-type]
    )
    ds_dir = sess_num.workspace.resolve_path("datasets")
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "lab5-2026.json").write_text("{}", encoding="utf-8")
    res_num = await sess_num.watch(
        WatchSpec(readouts=["Vc"], duration_s=0.01, interval_ms=20, max_frames=1, save_dataset=True)
    )
    assert res_num.dataset_id == "watch-0001"
    assert sess_num.workspace.verify_provenance().ok is True


async def test_sim_watch_ocr_failure_during_flush_and_html5_open_stage_handling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _build_transient_profile()

    class _BoomOcr:
        backend_name = "rapidocr"

        def __init__(self, fail_after: int) -> None:
            self.calls = 0
            self.fail_after = fail_after

        def recognize(self, _image: object, *, mode: OcrMode = "det_rec") -> OcrResult:
            return OcrResult(backend="rapidocr", mode=mode, elapsed_ms=0.1, boxes=[])

        def read_spec(
            self,
            _image: object,
            _spec: ReadoutSpec,
        ) -> tuple[OcrResult, float | int | str | None, str | None]:
            self.calls += 1
            if self.calls > self.fail_after:
                raise RuntimeError("ocr crash during flush")
            return (
                OcrResult(backend="rapidocr", mode="rec_only", elapsed_ms=0.1, boxes=[]),
                5.0,
                "V",
            )

    # (a) External asyncio.CancelledError mid-burst with failing OCR STILL persists cancelled
    # status/readings and propagates CancelledError (never swallowed by OCR crash).
    sess_cancel_boom = _make_session(
        tmp_path / "cancel_ocr_boom",
        profile=profile,
        page=_MockSimPage(
            initial_state={"R": 10.0}, profile=profile, readout_fn=lambda _n, _st: "5.0 V"
        ),
        ocr=_BoomOcr(fail_after=0),  # type: ignore[arg-type]
    )
    t_watch = asyncio.create_task(
        sess_cancel_boom.watch(WatchSpec(readouts=["Vc"], duration_s=5.0, interval_ms=20))
    )
    await asyncio.sleep(0.05)
    t_watch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t_watch
    assert sess_cancel_boom.workspace.read_status().phase == "cancelled"
    assert list((sess_cancel_boom.workspace.root / "readings").glob("watch-*.json"))
    assert sess_cancel_boom.workspace.verify_provenance().ok is True

    # (b) OCR crash during normal post-OCR flush does NOT re-invoke flush on remaining buffer
    sess_flush_boom = _make_session(
        tmp_path / "flush_ocr_boom",
        profile=profile,
        page=_MockSimPage(
            initial_state={"R": 10.0}, profile=profile, readout_fn=lambda _n, _st: "5.0 V"
        ),
        ocr=_BoomOcr(fail_after=1),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="ocr crash during flush"):
        await sess_flush_boom.watch(
            WatchSpec(readouts=["Vc"], duration_s=0.10, interval_ms=15, max_frames=4)
        )
    assert sess_flush_boom.workspace.read_status().phase == "failed"
    flush_partial = json.loads(
        (sess_flush_boom.workspace.root / "partial-readings.json").read_text("utf-8")
    )
    assert flush_partial["status"] == "failed"
    assert flush_partial["count"] == 1
    # Frame 0 succeeded (watch-0001.png), frame 1 wrote watch-0002.png then OCR threw;
    # frame 2 MUST NOT be written by a duplicate flush call in the except block.
    assert len(list(sess_flush_boom.workspace.evidence_dir.glob("watch-*.png"))) == 2
    assert sess_flush_boom.workspace.verify_provenance().ok is True

    # (c) SimulatorManager.open(html5_dir=...) auto-loads matching-stage profile and gracefully
    # leaves profile=None when reopened at a different viewport size (no ValueError lockout).
    html5_dir = tmp_path / "html5_rc"
    html5_dir.mkdir()
    (html5_dir / "index.html").write_text("<h1>RC Sim</h1>", encoding="utf-8")
    html5_sha = compute_html5_dir_sha256(html5_dir)

    cfg = PeaConfig(
        project_root=REPO_ROOT,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=tmp_path / "mgr_runs",
        profiles_dir=tmp_path / "mgr_profiles",
    )
    store = ProfileStore.from_config(cfg)
    saved_prof = CalibrationProfile(
        swf_sha256=html5_sha,
        stage_width=640,
        stage_height=420,
        header_height_px=0,
        readouts={
            "Vc": ReadoutSpec(
                name="Vc",
                roi_px=(10, 10, 120, 40),
                ocr_mode="rec_only",
                parse_mode="float",
            )
        },
    )
    store.save(saved_prof, expected_stage=(640, 420))

    class _FakePage:
        async def route(self, _pattern: str, _handler: Any) -> None:
            return

        def on(self, _event: str, _handler: Any) -> None:
            return

        async def goto(self, _target: str, *, wait_until: str = "domcontentloaded") -> None:
            return

        async def wait_for_timeout(self, _ms: int) -> None:
            return

        async def close(self) -> None:
            return

    class _FakeContext:
        async def new_page(self) -> _FakePage:
            return _FakePage()

        async def close(self) -> None:
            return

    class _FakeBrowser:
        async def new_context(self, **_kwargs: Any) -> _FakeContext:
            return _FakeContext()

    monkeypatch.setattr(SimRuntimeServer, "start", lambda self: "http://127.0.0.1:59991")
    monkeypatch.setattr(SimRuntimeServer, "stop", lambda self: None)

    mgr = SimulatorManager(
        cfg,
        profile_store=store,
        ocr=_TransientMockOcrBackend({}),  # type: ignore[arg-type]
    )

    async def _fake_browser() -> _FakeBrowser:
        return _FakeBrowser()

    mgr._ensure_browser = _fake_browser  # type: ignore[method-assign]

    # Matching viewport (640x420) auto-loads the saved profile
    sess_match = await mgr.open(
        html5_dir=html5_dir, viewport_width=640, viewport_height=420, settle_ms=0
    )
    try:
        assert sess_match.sim_sha256 == html5_sha
        assert sess_match.profile is not None
        assert sess_match.profile.swf_sha256 == html5_sha
    finally:
        await mgr.close_session(sess_match.session_id)

    # Different viewport (1024x768) opens cleanly with profile=None so caller can recalibrate
    sess_diff = await mgr.open(
        html5_dir=html5_dir, viewport_width=1024, viewport_height=768, settle_ms=0
    )
    try:
        assert sess_diff.sim_sha256 == html5_sha
        assert sess_diff.profile is None
    finally:
        await mgr.close_session(sess_diff.session_id)


async def test_simulator_manager_open_cleans_up_orphan_workspace_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed SimulatorManager.open(run_id=None) must remove newly created empty run dirs."""
    html5_dir = tmp_path / "html5_sim"
    html5_dir.mkdir()
    (html5_dir / "index.html").write_text("<h1>OK</h1>", encoding="utf-8")

    runs_dir = tmp_path / "runs"
    cfg = PeaConfig(
        project_root=REPO_ROOT,
        local_assets_dir=REPO_ROOT / "local_assets",
        runs_dir=runs_dir,
        profiles_dir=tmp_path / "profiles",
    )
    mgr = SimulatorManager(
        cfg,
        ocr=_TransientMockOcrBackend({}),  # type: ignore[arg-type]
    )

    # 1. Missing entry_file under html5_dir -> raises and cleans up orphan run dir
    with pytest.raises(FileNotFoundError):
        await mgr.open(html5_dir=html5_dir, entry_file="missing.html", settle_ms=0)
    assert list(runs_dir.iterdir()) == []

    # 2. Non-allowlisted URL -> raises PermissionError and cleans up orphan run dir
    with pytest.raises(PermissionError, match="not allowlisted"):
        await mgr.open(url="https://unlisted.example.com/sim", settle_ms=0)
    assert list(runs_dir.iterdir()) == []

    # 3. Missing SWF file -> raises FileNotFoundError and cleans up orphan run dir
    with pytest.raises(FileNotFoundError):
        await mgr.open(swf_path=tmp_path / "missing.swf", settle_ms=0)
    assert list(runs_dir.iterdir()) == []

    # 4. Browser launch failure after server start -> stops server and cleans up orphan run dir
    stop_calls: list[bool] = []
    monkeypatch.setattr(SimRuntimeServer, "start", lambda self: "http://127.0.0.1:59992")
    monkeypatch.setattr(SimRuntimeServer, "stop", lambda self: stop_calls.append(True))

    async def _failing_browser() -> Any:
        raise RuntimeError("Chromium launch failed")

    mgr._ensure_browser = _failing_browser  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Chromium launch failed"):
        await mgr.open(html5_dir=html5_dir, settle_ms=0)
    assert stop_calls == [True]
    assert list(runs_dir.iterdir()) == []

    # 5. Post-new_page() failure (page.goto raises) & even if server.stop raises ->
    #    closes page & context, stops server, removes orphan run dir, preserves original error
    events: list[str] = []

    class _FailingPage:
        def __init__(self, exc: BaseException) -> None:
            self._exc = exc

        async def route(self, _pattern: str, _handler: Any) -> None:
            pass

        def on(self, _event: str, _cb: Any) -> None:
            pass

        async def goto(self, _url: str, *, wait_until: str = "domcontentloaded") -> None:
            raise self._exc

        async def close(self) -> None:
            events.append("page.close")

    class _FailingContext:
        def __init__(self, exc: BaseException) -> None:
            self._exc = exc

        async def new_page(self) -> _FailingPage:
            return _FailingPage(self._exc)

        async def close(self) -> None:
            events.append("context.close")

    class _FailingBrowser:
        def __init__(self, exc: BaseException) -> None:
            self._exc = exc

        async def new_context(self, **_kwargs: Any) -> _FailingContext:
            return _FailingContext(self._exc)

    def _raising_stop(self: SimRuntimeServer) -> None:
        events.append("server.stop")
        raise RuntimeError("socket close glitch")

    monkeypatch.setattr(SimRuntimeServer, "stop", _raising_stop)

    async def _goto_error_browser() -> Any:
        return _FailingBrowser(RuntimeError("net::ERR_CONNECTION_REFUSED"))

    mgr._ensure_browser = _goto_error_browser  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="net::ERR_CONNECTION_REFUSED"):
        await mgr.open(html5_dir=html5_dir, settle_ms=0)
    assert events == ["page.close", "context.close", "server.stop"]
    assert list(runs_dir.iterdir()) == []

    # 6. asyncio.CancelledError (BaseException) mid-open must also clean up page, context,
    #    server, and orphan run dir before re-raising CancelledError
    events.clear()
    monkeypatch.setattr(SimRuntimeServer, "stop", lambda self: events.append("server.stop"))

    async def _cancelled_browser() -> Any:
        return _FailingBrowser(asyncio.CancelledError())

    mgr._ensure_browser = _cancelled_browser  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await mgr.open(html5_dir=html5_dir, settle_ms=0)
    assert events == ["page.close", "context.close", "server.stop"]
    assert list(runs_dir.iterdir()) == []

    # 7. Explicitly provided existing run_id must NEVER be deleted on failure
    existing = mgr.workspace_manager.create_run(label="Existing Run", source="simulator_captured")
    with pytest.raises(FileNotFoundError):
        await mgr.open(
            html5_dir=html5_dir,
            entry_file="missing.html",
            run_id=existing.run_id,
            settle_ms=0,
        )
    assert existing.root.is_dir()
