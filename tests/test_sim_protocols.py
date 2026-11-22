"""Protocol tests for SimRuntimeServer, stepper planning, sweeps, plateau search, and M2 stubs."""

from __future__ import annotations

import asyncio
import http.client
import io
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from PIL import Image

from pea.config import PeaConfig
from pea.ocr.base import read_spec_with_backend
from pea.schemas import (
    ActionStep,
    CalibrationProfile,
    ComputeSimSpec,
    ControlSpec,
    OcrBox,
    OcrMode,
    OcrResult,
    ReadoutSpec,
    SearchSpec,
    SweepAxis,
    SweepSpec,
    WatchSpec,
)
from pea.sim import (
    ProfileStore,
    SimCancelledError,
    SimRuntimeServer,
    SimulatorManager,
    SimulatorSession,
    compute_sim,
    compute_stepper_plan,
    format_control_value,
    select_all_shortcut,
    sim_watch,
    values_match,
)
from pea.sim.engine import _is_route_url_allowed
from pea.workspace import WorkspaceManager

REPO_ROOT = Path(__file__).resolve().parents[1]
SIMS_DIR = REPO_ROOT / "local_assets" / "sims"
RUFFLE_DIR = REPO_ROOT / "local_assets" / "ruffle"
SAMPLE_SHA256 = "35e4a257134073356c9796965ea3ea2d9362d51b2dc87486dc93e85a0fb96b15"


# ---------------------------------------------------------------------------
# 1. SimRuntimeServer (Host Guard, Auto-Sized HTML, Path Confinement)
# ---------------------------------------------------------------------------


def test_runtime_server_swf_mode_security_and_auto_sized_html() -> None:
    swf_path = SIMS_DIR / "m96220.swf"
    server = SimRuntimeServer(host="127.0.0.1", port=0, header_height_px=39)
    meta = server.configure_swf(swf_path, RUFFLE_DIR, expected_sha256=SAMPLE_SHA256)
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
        assert "/sim/m96220.swf" in r_idx.text

        # Ruffle asset & verified SWF served with correct MIME
        r_js = httpx.get(f"{origin}/ruffle/ruffle.js")
        assert r_js.status_code == 200
        assert "javascript" in r_js.headers["content-type"]

        r_swf = httpx.get(f"{origin}/sim/m96220.swf")
        assert r_swf.status_code == 200
        assert len(r_swf.content) == swf_path.stat().st_size

        # Unknown SWF under /sim/ rejected
        assert httpx.get(f"{origin}/sim/m96210.swf").status_code == 404

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
def test_runtime_server_ipv6_loopback_and_host_guard(ipv6_host: str) -> None:
    swf_path = SIMS_DIR / "m96220.swf"
    server = SimRuntimeServer(host=ipv6_host, port=0, header_height_px=39)
    server.configure_swf(swf_path, RUFFLE_DIR)
    with server:
        origin = server.origin
        assert origin.startswith("http://[::1]:")
        port = server.port

        r = httpx.get(f"{origin}/index.html")
        assert r.status_code == 200
        assert "/sim/m96220.swf" in r.text

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


async def test_milestone2_protocol_stubs_raise_not_implemented() -> None:
    watch_spec = WatchSpec(readouts=["voltage_c"], duration_s=5.0, interval_ms=200)
    with pytest.raises(NotImplementedError, match="Milestone 2"):
        await sim_watch(watch_spec)

    compute_spec = ComputeSimSpec(
        model_kind="algebraic_grid",
        expression="1 / sqrt(1 + w**2)",
        grid_variable="w",
        grid_start=0.0,
        grid_stop=10.0,
    )
    with pytest.raises(NotImplementedError, match="Milestone 2"):
        compute_sim(compute_spec)
