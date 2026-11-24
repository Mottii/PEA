"""Offline unit tests for sandboxed numerical, ODE, and FFT engine (``compute_sim``)."""

from __future__ import annotations

import concurrent.futures
import math
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from pea.math_engine import (
    ComputeSimError,
    ComputeSimLimitError,
    ComputeSimTimeoutError,
    data_ingest,
    load_dataset,
    run_compute_sim,
)
from pea.math_engine import compute_sim as compute_sim_mod
from pea.schemas import ComputeSimResult, ComputeSimSpec, DatasetSpec
from pea.workspace import RunWorkspace, WorkspaceManager, sha256_file


def _make_workspace(tmp_path: Path) -> RunWorkspace:
    mgr = WorkspaceManager(tmp_path / "runs")
    return mgr.create_run(label="compute-sim-test", source="model_computed")


# ---------------------------------------------------------------------------
# 1. 1D RC Discharge ODE (ode_ivp)
# ---------------------------------------------------------------------------


def test_ode_ivp_1d_rc_discharge_shorthand_and_custom_state(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    r_ohm = 1000.0
    c_farad = 5.0e-4
    tau = r_ohm * c_farad  # 0.5 s
    v0 = 10.0

    # Shorthand expression -> auto state_variables=["y"]
    spec_short = ComputeSimSpec(
        model_kind="ode_ivp",
        expression="-y / (R * C)",
        parameters={"R": r_ohm, "C": c_farad},
        grid_variable="t",
        grid_start=0.0,
        grid_stop=3.0,
        grid_points=61,
        initial_state=[v0],
        rtol=1e-8,
        atol=1e-10,
        units={"t": "s", "y": "V"},
    )
    res_short = run_compute_sim(spec_short, run_workspace=ws)
    assert isinstance(res_short, ComputeSimResult)
    assert res_short.provenance == "model_computed"
    assert res_short.columns == ["t", "y"]
    assert res_short.row_count == 61
    assert res_short.units == {"t": "s", "y": "V"}
    assert res_short.solver_meta["method"] == "RK45"
    assert res_short.solver_meta["status"] == 0
    assert res_short.solver_meta["rhs_calls"] >= res_short.solver_meta["nfev"] > 0

    ds_short = load_dataset(res_short.dataset_id, ws)
    assert ds_short.source == "model_computed"
    for row in ds_short.rows:
        expected = v0 * math.exp(-row["t"] / tau)
        assert abs(row["y"] - expected) < 1e-6

    # Custom state variable name ("Vc") + lazy where() in scalar ODE RHS (t == 0 guarded)
    spec_custom = ComputeSimSpec(
        model_kind="ode_ivp",
        expression="where(t > 0, -Vc / (R * C) + 0.0 * (1.0 / t), -Vc / (R * C))",
        state_variables=["Vc"],
        parameters={"R": r_ohm, "C": c_farad},
        grid_variable="t",
        grid_start=0.0,
        grid_stop=2.0,
        grid_points=41,
        initial_state=[v0],
        rtol=1e-8,
        atol=1e-10,
    )
    res_custom = run_compute_sim(spec_custom, run_workspace=ws)
    assert res_custom.columns == ["t", "Vc"]
    ds_custom = load_dataset(res_custom.dataset_id, ws)
    for row in ds_custom.rows:
        expected = v0 * math.exp(-row["t"] / tau)
        assert abs(row["Vc"] - expected) < 1e-6

    assert ws.verify_provenance().ok is True


# ---------------------------------------------------------------------------
# 2. 2D Coupled Damped & Driven RLC ODE (ode_ivp)
# ---------------------------------------------------------------------------


def test_ode_ivp_2d_coupled_underdamped_and_driven_rlc(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    r_ohm = 2.0
    l_henry = 1.0
    c_farad = 0.04  # omega0 = 5 rad/s, beta = 1 s^-1, omega_d = sqrt(24)
    beta = r_ohm / (2.0 * l_henry)
    omega0 = 1.0 / math.sqrt(l_henry * c_farad)
    omega_d = math.sqrt(omega0**2 - beta**2)

    # Undriven underdamped RLC
    spec_free = ComputeSimSpec(
        model_kind="ode_ivp",
        state_variables=["q", "i"],
        expressions=["i", "-(R / L) * i - q / (L * C)"],
        parameters={"R": r_ohm, "L": l_henry, "C": c_farad},
        grid_variable="t",
        grid_start=0.0,
        grid_stop=4.0,
        grid_points=81,
        initial_state=[1.0, 0.0],
        rtol=1e-8,
        atol=1e-10,
    )
    res_free = run_compute_sim(spec_free, run_workspace=ws)
    assert res_free.columns == ["t", "q", "i"]
    ds_free = load_dataset(res_free.dataset_id, ws)
    for row in ds_free.rows:
        t = row["t"]
        env = math.exp(-beta * t)
        q_exact = env * (math.cos(omega_d * t) + (beta / omega_d) * math.sin(omega_d * t))
        i_exact = -(omega0**2 / omega_d) * env * math.sin(omega_d * t)
        assert abs(row["q"] - q_exact) < 1e-5
        assert abs(row["i"] - i_exact) < 1e-5

    # Driven RLC initialized on exact steady-state harmonic trajectory
    v0 = 10.0
    omega = 3.0
    denom = complex((1.0 / c_farad) - l_henry * omega**2, r_ohm * omega)
    q_phasor = v0 / denom
    q0 = q_phasor.real
    i0 = -omega * q_phasor.imag

    spec_driven = ComputeSimSpec(
        model_kind="ode_ivp",
        state_variables=["q", "i"],
        expressions=["i", "-(R / L) * i - q / (L * C) + (V0 / L) * cos(omega * t)"],
        parameters={"R": r_ohm, "L": l_henry, "C": c_farad, "V0": v0, "omega": omega},
        grid_variable="t",
        grid_start=0.0,
        grid_stop=5.0,
        grid_points=101,
        initial_state=[q0, i0],
        rtol=1e-8,
        atol=1e-10,
    )
    res_driven = run_compute_sim(spec_driven, run_workspace=ws)
    ds_driven = load_dataset(res_driven.dataset_id, ws)
    for row in ds_driven.rows:
        t = row["t"]
        q_exact = q_phasor.real * math.cos(omega * t) - q_phasor.imag * math.sin(omega * t)
        i_exact = -omega * (
            q_phasor.real * math.sin(omega * t) + q_phasor.imag * math.cos(omega * t)
        )
        assert abs(row["q"] - q_exact) < 1e-5
        assert abs(row["i"] - i_exact) < 1e-5


# ---------------------------------------------------------------------------
# 3. Stiff ODE Solvers (Radau, BDF)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ode_method", ["Radau", "BDF"])
def test_ode_ivp_stiff_solvers_radau_and_bdf(tmp_path: Path, ode_method: str) -> None:
    ws = _make_workspace(tmp_path)
    k1 = 1.0e4
    k2 = 1.0

    # Stiff 2D system with exact analytical solution:
    # y1(t) = cos(t) + exp(-k1 * t), y2(t) = exp(-k2 * t)
    spec = ComputeSimSpec(
        model_kind="ode_ivp",
        ode_method=ode_method,  # type: ignore[arg-type]
        state_variables=["y1", "y2"],
        expressions=["-k1 * (y1 - cos(t)) - sin(t)", "-k2 * y2"],
        parameters={"k1": k1, "k2": k2},
        grid_variable="t",
        grid_start=0.0,
        grid_stop=2.0,
        grid_points=41,
        initial_state=[2.0, 1.0],
        rtol=1e-6,
        atol=1e-8,
    )
    res = run_compute_sim(spec, run_workspace=ws)
    assert res.solver_meta["method"] == ode_method
    assert res.solver_meta["status"] == 0
    assert res.solver_meta["nfev"] > 0
    assert res.solver_meta["njev"] >= 0

    ds = load_dataset(res.dataset_id, ws)
    for row in ds.rows:
        t = row["t"]
        y1_exact = math.cos(t) + math.exp(-k1 * t)
        y2_exact = math.exp(-k2 * t)
        assert abs(row["y1"] - y1_exact) < 1e-4
        assert abs(row["y2"] - y2_exact) < 1e-4


# ---------------------------------------------------------------------------
# 4. algebraic_grid Mode (Single, Descending, Multi, Constant, Lazy where, All Functions)
# ---------------------------------------------------------------------------


def test_algebraic_grid_single_descending_multi_constant_and_lazy_where(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)

    # Descending grid + lazy where(t > 0, 1/t, 0) with t == 0 at endpoint + nested where
    res_lazy = run_compute_sim(
        ComputeSimSpec(
            model_kind="algebraic_grid",
            expression="where(t > 0, where(t >= 2, 1.0 / t, sin(t) / t), 1.0)",
            state_variables=["sinc_like"],
            grid_variable="t",
            grid_start=4.0,
            grid_stop=0.0,
            grid_points=9,
        ),
        run_workspace=ws,
    )
    assert res_lazy.columns == ["t", "sinc_like"]
    ds_lazy = load_dataset(res_lazy.dataset_id, ws)
    assert ds_lazy.rows[0]["t"] == pytest.approx(4.0)
    assert ds_lazy.rows[-1]["t"] == pytest.approx(0.0)
    assert ds_lazy.rows[-1]["sinc_like"] == pytest.approx(1.0)
    assert ds_lazy.rows[0]["sinc_like"] == pytest.approx(0.25)
    # t = 1.0 -> sin(1) / 1
    row_t1 = [r for r in ds_lazy.rows if abs(r["t"] - 1.0) < 1e-12][0]
    assert row_t1["sinc_like"] == pytest.approx(math.sin(1.0), rel=1e-12)

    # Multi-expression parametric circle + constant expression
    res_multi = run_compute_sim(
        ComputeSimSpec(
            model_kind="algebraic_grid",
            expressions=["R * cos(theta)", "R * sin(theta)", "5.0"],
            state_variables=["x", "y", "const_col"],
            parameters={"R": 3.0},
            grid_variable="theta",
            grid_start=0.0,
            grid_stop=2.0 * math.pi,
            grid_points=5,
        ),
        run_workspace=ws,
    )
    assert res_multi.columns == ["theta", "x", "y", "const_col"]
    ds_multi = load_dataset(res_multi.dataset_id, ws)
    for row in ds_multi.rows:
        assert math.hypot(row["x"], row["y"]) == pytest.approx(3.0, rel=1e-12)
        assert row["const_col"] == pytest.approx(5.0)

    # Constant expression with no grid_variable reference + parameter shadowing built-in 'e'
    res_shadow = run_compute_sim(
        ComputeSimSpec(
            model_kind="algebraic_grid",
            expression="e + euler + pi",
            parameters={"e": 2.0},
            grid_variable="x",
            grid_start=0.0,
            grid_stop=1.0,
            grid_points=4,
        ),
        run_workspace=ws,
    )
    ds_shadow = load_dataset(res_shadow.dataset_id, ws)
    for row in ds_shadow.rows:
        assert row["y"] == pytest.approx(2.0 + math.e + math.pi, rel=1e-12)


def test_algebraic_grid_whitelisted_math_functions(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)

    res = run_compute_sim(
        ComputeSimSpec(
            model_kind="algebraic_grid",
            expressions=[
                "heaviside(x) + heaviside(x, 1.0)",
                "clip(x, -0.5, 0.5)",
                "sign(x) + floor(x) + ceil(x)",
                "min(x, 0.0, -0.25) + max(x, 0.0, 0.25)",
                "atan2(x, 1.0) + sinh(x) + cosh(x) + tanh(x)",
                "asin(clip(x, -1, 1)) + acos(clip(x, -1, 1)) + atan(x)",
                "log(abs(x) + 1) + log10(abs(x) + 1) + exp(x) + (-2.0) ** 3",
            ],
            state_variables=["h", "c", "sfc", "mm", "trig", "inv", "exp_pow"],
            grid_variable="x",
            grid_start=-1.0,
            grid_stop=1.0,
            grid_points=5,
        ),
        run_workspace=ws,
    )
    ds = load_dataset(res.dataset_id, ws)
    # Check x = 0.0 row (index 2): heaviside(0) = 0.5, heaviside(0, 1.0) = 1.0 -> 1.5
    mid = ds.rows[2]
    assert mid["x"] == pytest.approx(0.0)
    assert mid["h"] == pytest.approx(1.5)
    assert mid["c"] == pytest.approx(0.0)
    assert mid["sfc"] == pytest.approx(0.0)
    assert mid["mm"] == pytest.approx(-0.25 + 0.25)
    assert mid["trig"] == pytest.approx(0.0 + 0.0 + 1.0 + 0.0)
    assert mid["inv"] == pytest.approx(0.0 + math.pi / 2.0 + 0.0)
    assert mid["exp_pow"] == pytest.approx(0.0 + 0.0 + 1.0 - 8.0)


# ---------------------------------------------------------------------------
# 5. Inline fft_spectrum Mode (Even/Odd N, Hann Coherent Gain, DC/Nyquist, Detrend)
# ---------------------------------------------------------------------------


def test_fft_spectrum_inline_even_and_odd_window_and_detrend(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    n_even = 256
    fs = 256.0
    dt = 1.0 / fs
    t_stop = (n_even - 1) * dt
    f1, a1 = 10.0, 3.5
    f2, a2 = 35.0, 1.25
    dc_offset = 4.0

    # Rectangular window (window="none") on exact integer-cycle grid recovers exact amplitudes
    res_rect = run_compute_sim(
        ComputeSimSpec(
            model_kind="fft_spectrum",
            expression="dc + A1 * cos(2 * pi * f1 * t) + A2 * sin(2 * pi * f2 * t)",
            parameters={"dc": dc_offset, "A1": a1, "f1": f1, "A2": a2, "f2": f2},
            grid_variable="t",
            grid_start=0.0,
            grid_stop=t_stop,
            grid_points=n_even,
            window="none",
            detrend_mean=True,
        ),
        run_workspace=ws,
    )
    assert res_rect.columns == ["frequency_hz", "omega_rad_s", "amplitude", "power", "phase_rad"]
    assert res_rect.row_count == n_even // 2 + 1
    assert res_rect.solver_meta["dominant_frequency_hz"] == pytest.approx(f1, abs=1e-9)
    assert res_rect.solver_meta["dominant_omega_rad_s"] == pytest.approx(
        2.0 * math.pi * f1, rel=1e-9
    )
    assert res_rect.solver_meta["dominant_amplitude"] == pytest.approx(a1, rel=1e-9)
    assert res_rect.solver_meta["nyquist_hz"] == pytest.approx(fs / 2.0, rel=1e-9)
    assert res_rect.solver_meta["resampled"] is False

    ds_rect = load_dataset(res_rect.dataset_id, ws)
    assert ds_rect.rows[0]["amplitude"] == pytest.approx(0.0, abs=1e-12)  # detrended DC
    bin_f2 = [r for r in ds_rect.rows if abs(r["frequency_hz"] - f2) < 1e-9][0]
    assert bin_f2["amplitude"] == pytest.approx(a2, rel=1e-9)
    assert bin_f2["power"] == pytest.approx(a2**2, rel=1e-9)

    # detrend_mean=False preserves DC amplitude exactly (1/N scaling)
    res_dc = run_compute_sim(
        ComputeSimSpec(
            model_kind="fft_spectrum",
            expression="dc + A1 * cos(2 * pi * f1 * t)",
            parameters={"dc": dc_offset, "A1": a1, "f1": f1},
            grid_variable="t",
            grid_start=0.0,
            grid_stop=t_stop,
            grid_points=n_even,
            window="none",
            detrend_mean=False,
        ),
        run_workspace=ws,
    )
    ds_dc = load_dataset(res_dc.dataset_id, ws)
    assert ds_dc.rows[0]["amplitude"] == pytest.approx(dc_offset, rel=1e-12)
    # Dominant peak still excludes DC bin 0
    assert res_dc.solver_meta["dominant_frequency_hz"] == pytest.approx(f1, abs=1e-9)

    # Hann window with coherent gain recovers peak amplitude within < 2%
    res_hann = run_compute_sim(
        ComputeSimSpec(
            model_kind="fft_spectrum",
            expression="dc + A1 * cos(2 * pi * f1 * t) + A2 * sin(2 * pi * f2 * t)",
            parameters={"dc": dc_offset, "A1": a1, "f1": f1, "A2": a2, "f2": f2},
            grid_variable="t",
            grid_start=0.0,
            grid_stop=t_stop,
            grid_points=n_even,
            window="hann",
            detrend_mean=True,
        ),
        run_workspace=ws,
    )
    assert res_hann.solver_meta["dominant_frequency_hz"] == pytest.approx(f1, abs=1e-9)
    assert res_hann.solver_meta["dominant_amplitude"] == pytest.approx(a1, rel=0.02)

    # Even N Nyquist bin scaling (1/N): cos(pi * n) = (-1)^n has amplitude 2.0 at Nyquist
    res_nyq = run_compute_sim(
        ComputeSimSpec(
            model_kind="fft_spectrum",
            expression="2.0 * cos(pi * fs * t)",
            parameters={"fs": fs},
            grid_variable="t",
            grid_start=0.0,
            grid_stop=t_stop,
            grid_points=n_even,
            window="none",
            detrend_mean=False,
        ),
        run_workspace=ws,
    )
    ds_nyq = load_dataset(res_nyq.dataset_id, ws)
    assert ds_nyq.rows[-1]["frequency_hz"] == pytest.approx(fs / 2.0, rel=1e-9)
    assert ds_nyq.rows[-1]["amplitude"] == pytest.approx(2.0, rel=1e-9)

    # Odd N highest bin is NOT Nyquist -> must keep 2/N scaling (not halved!)
    n_odd = 65
    fs_odd = 65.0
    k_max = n_odd // 2  # 32 Hz (< 32.5 Hz Nyquist)
    res_odd = run_compute_sim(
        ComputeSimSpec(
            model_kind="fft_spectrum",
            expression="2.5 * cos(2 * pi * f_hi * t)",
            parameters={"f_hi": float(k_max)},
            grid_variable="t",
            grid_start=0.0,
            grid_stop=(n_odd - 1) / fs_odd,
            grid_points=n_odd,
            window="none",
            detrend_mean=False,
        ),
        run_workspace=ws,
    )
    ds_odd = load_dataset(res_odd.dataset_id, ws)
    assert ds_odd.rows[-1]["frequency_hz"] == pytest.approx(float(k_max), rel=1e-9)
    assert ds_odd.rows[-1]["amplitude"] == pytest.approx(2.5, rel=1e-9)


# ---------------------------------------------------------------------------
# 6. Dataset-Backed fft_spectrum Mode (Uniform, Jittered Resampling, OCR Misses, Rejections)
# ---------------------------------------------------------------------------


def test_fft_spectrum_dataset_uniform_jittered_and_ocr_misses(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    f0 = 4.0

    # 1. Uniform dataset inheriting signal unit ("V" -> amplitude="V", power="V^2")
    n = 128
    t_unif = np.linspace(0.0, (n - 1) / 64.0, n)
    rows_unif = [
        {"elapsed_s": float(t), "Vc": float(2.0 * math.cos(2.0 * math.pi * f0 * t))}
        for t in t_unif
    ]
    data_ingest(
        DatasetSpec(
            dataset_id="watch-unif",
            source="simulator_captured",
            rows=rows_unif,
            units={"elapsed_s": "s", "Vc": "V"},
        ),
        ws,
    )
    res_unif = run_compute_sim(
        ComputeSimSpec(
            model_kind="fft_spectrum",
            input_dataset_id="watch-unif",
            time_column="elapsed_s",
            signal_column="Vc",
        ),
        run_workspace=ws,
    )
    assert res_unif.provenance == "model_computed"
    assert res_unif.solver_meta["resampled"] is False
    assert res_unif.solver_meta["dropped_missing_rows"] == 0
    assert res_unif.solver_meta["dominant_frequency_hz"] == pytest.approx(f0, abs=1e-9)
    assert res_unif.units["amplitude"] == "V"
    assert res_unif.units["power"] == "V^2"
    assert res_unif.units["frequency_hz"] == "Hz"

    # 2. Jittered timestamps (>1% jitter) + None OCR misses + custom unit override
    rng = np.random.default_rng(42)
    base_t = np.linspace(0.0, 4.0, 200)
    # Deterministic +/- 15% timing jitter strictly preserving monotonicity
    dt_base = base_t[1] - base_t[0]
    jitter = rng.uniform(-0.25 * dt_base, 0.25 * dt_base, size=len(base_t))
    jitter[0] = 0.0
    jitter[-1] = 0.0
    t_jit = base_t + jitter
    assert np.all(np.diff(t_jit) > 0)

    rows_jit: list[dict[str, Any]] = []
    for idx, t in enumerate(t_jit):
        if idx in (17, 63, 129):
            val: float | None = None  # simulated OCR miss
        else:
            val = float(5.0 * math.exp(-0.2 * t) * math.cos(2.0 * math.pi * f0 * t))
        rows_jit.append({"elapsed_s": float(t), "Vc": val})

    data_ingest(
        DatasetSpec(
            dataset_id="watch-jitter",
            source="simulator_captured",
            rows=rows_jit,
            units={"elapsed_s": "s", "Vc": "m/s"},
        ),
        ws,
    )
    res_jit = run_compute_sim(
        ComputeSimSpec(
            model_kind="fft_spectrum",
            input_dataset_id="watch-jitter",
            time_column="elapsed_s",
            signal_column="Vc",
            window="hann",
            units={"amplitude": "custom_u"},
        ),
        run_workspace=ws,
    )
    assert res_jit.solver_meta["resampled"] is True
    assert res_jit.solver_meta["jitter_ratio"] > 0.01
    assert res_jit.solver_meta["dropped_missing_rows"] == 3
    assert res_jit.solver_meta["n_samples"] == 197
    assert res_jit.solver_meta["input_dataset_id"] == "watch-jitter"
    # Dominant frequency recovered within 1 FFT bin (~0.25 Hz)
    assert abs(res_jit.solver_meta["dominant_frequency_hz"] - f0) <= 0.3
    # Explicit spec.units overrides inherited unit while power keeps formatted inherited unit
    assert res_jit.units["amplitude"] == "custom_u"
    assert res_jit.units["power"] == "(m/s)^2"


def test_fft_spectrum_dataset_rejections(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)

    # Missing dataset -> FileNotFoundError
    with pytest.raises(FileNotFoundError):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="fft_spectrum",
                input_dataset_id="does-not-exist",
                time_column="t",
                signal_column="y",
            ),
            run_workspace=ws,
        )

    # Fewer than 4 valid samples after dropping None signal rows
    data_ingest(
        DatasetSpec(
            dataset_id="ds-short",
            source="simulator_captured",
            rows=[
                {"t": 0.0, "y": 1.0},
                {"t": 0.1, "y": None},
                {"t": 0.2, "y": 2.0},
                {"t": 0.3, "y": 3.0},
            ],
        ),
        ws,
    )
    with pytest.raises(ComputeSimError, match="at least 4 valid samples"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="fft_spectrum",
                input_dataset_id="ds-short",
                time_column="t",
                signal_column="y",
            ),
            run_workspace=ws,
        )

    # Non-monotonic / duplicate timestamps
    data_ingest(
        DatasetSpec(
            dataset_id="ds-nonmono",
            source="simulator_captured",
            rows=[
                {"t": 0.0, "y": 1.0},
                {"t": 0.2, "y": 2.0},
                {"t": 0.2, "y": 3.0},
                {"t": 0.4, "y": 4.0},
            ],
        ),
        ws,
    )
    with pytest.raises(ComputeSimError, match="strictly increasing"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="fft_spectrum",
                input_dataset_id="ds-nonmono",
                time_column="t",
                signal_column="y",
            ),
            run_workspace=ws,
        )

    # Missing column name
    with pytest.raises(ComputeSimError, match="not found"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="fft_spectrum",
                input_dataset_id="ds-nonmono",
                time_column="t",
                signal_column="missing_col",
            ),
            run_workspace=ws,
        )

    # None time value rejected
    data_ingest(
        DatasetSpec(
            dataset_id="ds-none-time",
            source="simulator_captured",
            rows=[
                {"t": 0.0, "y": 1.0},
                {"t": None, "y": 2.0},
                {"t": 0.2, "y": 3.0},
                {"t": 0.3, "y": 4.0},
                {"t": 0.4, "y": 5.0},
            ],
        ),
        ws,
    )
    with pytest.raises(ComputeSimError, match="Missing time value"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="fft_spectrum",
                input_dataset_id="ds-none-time",
                time_column="t",
                signal_column="y",
            ),
            run_workspace=ws,
        )

    # Non-numeric string or bool signal rejected
    data_ingest(
        DatasetSpec(
            dataset_id="ds-bad-type",
            source="simulator_captured",
            rows=[
                {"t": 0.0, "y": 1.0},
                {"t": 0.1, "y": True},
                {"t": 0.2, "y": 3.0},
                {"t": 0.3, "y": 4.0},
            ],
        ),
        ws,
    )
    with pytest.raises(ComputeSimError, match="Expected numeric value"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="fft_spectrum",
                input_dataset_id="ds-bad-type",
                time_column="t",
                signal_column="y",
            ),
            run_workspace=ws,
        )

    # Non-numeric string signal rejected
    data_ingest(
        DatasetSpec(
            dataset_id="ds-bad-str",
            source="simulator_captured",
            rows=[
                {"t": 0.0, "y": 1.0},
                {"t": 0.1, "y": "N/A"},
                {"t": 0.2, "y": 3.0},
                {"t": 0.3, "y": 4.0},
            ],
        ),
        ws,
    )
    with pytest.raises(ComputeSimError, match="Expected numeric value"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="fft_spectrum",
                input_dataset_id="ds-bad-str",
                time_column="t",
                signal_column="y",
            ),
            run_workspace=ws,
        )

    # Decreasing timestamps rejected
    data_ingest(
        DatasetSpec(
            dataset_id="ds-decreasing",
            source="simulator_captured",
            rows=[
                {"t": 0.0, "y": 1.0},
                {"t": 0.3, "y": 2.0},
                {"t": 0.2, "y": 3.0},
                {"t": 0.4, "y": 4.0},
            ],
        ),
        ws,
    )
    with pytest.raises(ComputeSimError, match="strictly increasing"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="fft_spectrum",
                input_dataset_id="ds-decreasing",
                time_column="t",
                signal_column="y",
            ),
            run_workspace=ws,
        )


def test_edge_cases_boundaries_lazy_branches_shadowing_and_concurrent_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _make_workspace(tmp_path)

    # 1. All-true and all-false lazy where() branches never evaluate inactive 1/0
    res_all_true = run_compute_sim(
        ComputeSimSpec(
            model_kind="algebraic_grid",
            expression="where(t >= 1.0, t, 1.0 / 0.0)",
            grid_variable="t",
            grid_start=1.0,
            grid_stop=3.0,
            grid_points=2,
        ),
        run_workspace=ws,
    )
    assert res_all_true.row_count == 2
    assert [r["y"] for r in res_all_true.preview_rows] == [1.0, 3.0]

    res_all_false = run_compute_sim(
        ComputeSimSpec(
            model_kind="algebraic_grid",
            expression="where(t < 0.0, 1.0 / 0.0, where(t <= 2.0, t * 2.0, t + 10.0))",
            grid_variable="t",
            grid_start=1.0,
            grid_stop=4.0,
            grid_points=4,
        ),
        run_workspace=ws,
    )
    assert [r["y"] for r in res_all_false.preview_rows] == [2.0, 4.0, 13.0, 14.0]

    # 2. ODE with minimum grid_points=2, state variable shadowing 'e', and all 4 compare ops
    res_ode_edge = run_compute_sim(
        ComputeSimSpec(
            model_kind="ode_ivp",
            expression="where(t <= 0.5, where(t < 0.0, 1/0, -e), where(t >= 0.5, -e, 1/0))",
            state_variables=["e"],
            grid_variable="t",
            grid_start=0.0,
            grid_stop=1.0,
            grid_points=2,
            initial_state=[2.0],
            rtol=1e-8,
            atol=1e-10,
        ),
        run_workspace=ws,
    )
    assert res_ode_edge.row_count == 2
    assert res_ode_edge.columns == ["t", "e"]
    assert res_ode_edge.preview_rows[-1]["e"] == pytest.approx(2.0 * math.exp(-1.0), rel=1e-6)

    # 3. Minimum n=4 FFT (window="hann" and constant signal with detrend_mean=True)
    res_fft_min = run_compute_sim(
        ComputeSimSpec(
            model_kind="fft_spectrum",
            expression="3.0",
            grid_variable="t",
            grid_start=0.0,
            grid_stop=3.0,
            grid_points=4,
            window="hann",
            detrend_mean=True,
        ),
        run_workspace=ws,
    )
    assert res_fft_min.row_count == 3
    assert res_fft_min.solver_meta["dominant_amplitude"] == pytest.approx(0.0, abs=1e-12)

    # 4. Concurrent auto-ID collision retry advances sequence cleanly
    real_ingest = compute_sim_mod.data_ingest
    first_attempt = True

    def _flaky_ingest(*args: Any, **kwargs: Any) -> Any:
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            raise FileExistsError("Simulated concurrent writer race")
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(compute_sim_mod, "data_ingest", _flaky_ingest)
    res_retry = run_compute_sim(
        ComputeSimSpec(
            model_kind="algebraic_grid",
            expression="t",
            grid_variable="t",
            grid_start=0.0,
            grid_stop=1.0,
            grid_points=2,
        ),
        run_workspace=ws,
    )
    # Previous algebraic_grid runs created 0001 and 0002; race failed 0003 -> landed on 0004
    assert res_retry.dataset_id == "compute-algebraic_grid-0004"
    assert ws.verify_provenance().ok is True


# ---------------------------------------------------------------------------
# 7. Provenance, Immutability, Scoped Auto-ID & Bounded preview_rows
# ---------------------------------------------------------------------------


def test_provenance_immutability_scoped_auto_id_and_preview_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _make_workspace(tmp_path)

    # Pre-populate unrelated datasets that must NEVER skew compute-ode_ivp numbering
    for unrelated_id in ("watch-0050", "lab5-2026", "compute-algebraic_grid-0099"):
        data_ingest(
            DatasetSpec(
                dataset_id=unrelated_id,
                source="simulator_captured",
                rows=[{"x": 1.0, "y": 2.0}],
            ),
            ws,
        )

    # 100-row ODE run -> auto ID compute-ode_ivp-0001 and bounded 10-row preview (5 head + 5 tail)
    res_ode = run_compute_sim(
        ComputeSimSpec(
            model_kind="ode_ivp",
            expression="-y",
            grid_variable="t",
            grid_start=0.0,
            grid_stop=1.0,
            grid_points=100,
            initial_state=[1.0],
        ),
        run_workspace=ws,
    )
    assert res_ode.dataset_id == "compute-ode_ivp-0001"
    assert res_ode.dataset_relpath == "datasets/compute-ode_ivp-0001.json"
    assert res_ode.row_count == 100
    assert len(res_ode.preview_rows) == 10

    full_ds = load_dataset(res_ode.dataset_id, ws)
    assert res_ode.preview_rows == full_ds.rows[:5] + full_ds.rows[-5:]

    actual_sha, _ = sha256_file(ws.resolve_path(res_ode.dataset_relpath))
    assert res_ode.dataset_sha256 == actual_sha
    assert ws.verify_provenance().ok is True

    prov_entries = [e for e in ws.read_provenance() if e.relpath == res_ode.dataset_relpath]
    assert len(prov_entries) == 1
    assert prov_entries[0].source == "model_computed"

    # Algebraic grid auto-ID picks up after existing compute-algebraic_grid-0099 -> 0100
    res_grid = run_compute_sim(
        ComputeSimSpec(
            model_kind="algebraic_grid",
            expression="x**2",
            grid_variable="x",
            grid_start=0.0,
            grid_stop=4.0,
            grid_points=5,
        ),
        run_workspace=ws,
    )
    assert res_grid.dataset_id == "compute-algebraic_grid-0100"
    assert len(res_grid.preview_rows) == 5

    # Explicit duplicate dataset_id fails PRE-FLIGHT before running ODE solver
    called = False

    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("Solver should not run on duplicate explicit dataset_id")

    monkeypatch.setattr(compute_sim_mod, "_run_ode_ivp", _fail_if_called)
    with pytest.raises(FileExistsError, match="already exists"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="ode_ivp",
                expression="-y",
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=10,
                initial_state=[1.0],
                dataset_id="compute-ode_ivp-0001",
            ),
            run_workspace=ws,
        )
    assert called is False


# ---------------------------------------------------------------------------
# 8. AST Sandbox, Resource Caps & Runtime Rejection Suite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_expr",
    [
        "__import__('os').system('echo hi')",
        "t.real",
        "t[0]",
        "(lambda x: x)(t)",
        "[t for t in [1]]",
        "sin(x=t)",
        "min(*t)",
        "t > 0",
        "0 < t < 1",
        "where(0 < t < 1, 1, 0)",
        "where(t == 0, 1, 0)",
        "where(t, 1, 0)",
        "sin(t, 1)",
        "min(t)",
        "clip(t, 0)",
        "heaviside()",
        "heaviside(t, 0.5, 1.0)",
        "atan2(t)",
        "unknown_var + t",
        "__builtins__",
        "'hello'",
        "True",
        "t % 2",
        "t // 2",
        "~t",
        "not t",
        "t if t > 0 else 0",
        "eval('1+1')",
        "exec('x=1')",
        "1e309",
        "nan",
        "inf",
        "f(t)",
    ],
)
def test_ast_sandbox_rejections(tmp_path: Path, bad_expr: str) -> None:
    ws = _make_workspace(tmp_path)
    with pytest.raises(ComputeSimError):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="algebraic_grid",
                expression=bad_expr,
                parameters={"f": 2.0},
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=5,
            ),
            run_workspace=ws,
        )


def test_resource_limits_length_depth_and_exponent(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)

    # Expression length > 1000 -> ComputeSimLimitError
    long_expr = "1+" * 501 + "1"
    assert len(long_expr) > 1000
    with pytest.raises(ComputeSimLimitError, match="length"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="algebraic_grid",
                expression=long_expr,
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=4,
            ),
            run_workspace=ws,
        )

    # AST depth > 48 -> ComputeSimLimitError
    deep_expr = "sin(" * 50 + "t" + ")" * 50
    assert len(deep_expr) <= 1000
    with pytest.raises(ComputeSimLimitError, match="depth"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="algebraic_grid",
                expression=deep_expr,
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=4,
            ),
            run_workspace=ws,
        )

    # Exponent magnitude > 1000 -> ComputeSimLimitError (both vectorized & scalar)
    with pytest.raises(ComputeSimLimitError, match="Exponent"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="algebraic_grid",
                expression="t ** 1001",
                grid_variable="t",
                grid_start=1.0,
                grid_stop=2.0,
                grid_points=4,
            ),
            run_workspace=ws,
        )

    with pytest.raises(ComputeSimLimitError, match="Exponent"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="ode_ivp",
                expression="y ** -1001",
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=5,
                initial_state=[2.0],
            ),
            run_workspace=ws,
        )


@pytest.mark.parametrize(
    "expr",
    [
        "0 ** -1",
        "(-1) ** 0.5",
        "clip(t, 5, 1)",
        "1 / (t - 0.5)",
        "log(-1)",
        "sqrt(-1)",
        "asin(2)",
        "exp(1000)",
    ],
)
def test_runtime_domain_errors_vectorized_and_scalar(tmp_path: Path, expr: str) -> None:
    ws = _make_workspace(tmp_path)

    # Vectorized (algebraic_grid) hits t = 0.5 cleanly
    with pytest.raises(ComputeSimError):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="algebraic_grid",
                expression=expr,
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=3,
            ),
            run_workspace=ws,
        )

    # Scalar (ode_ivp) starting at t = 0.5 so 1 / (t - 0.5) faults immediately
    with pytest.raises(ComputeSimError):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="ode_ivp",
                expression=expr,
                grid_variable="t",
                grid_start=0.5,
                grid_stop=1.0,
                grid_points=5,
                initial_state=[1.0],
            ),
            run_workspace=ws,
        )


def test_ode_blowup_max_nfev_and_timeout_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _make_workspace(tmp_path)

    # Finite-time blowup dy/dt = y^2, y(0) = 1 explodes at t = 1 < grid_stop = 2
    with pytest.raises(ComputeSimError):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="ode_ivp",
                expression="y ** 2",
                grid_variable="t",
                grid_start=0.0,
                grid_stop=2.0,
                grid_points=20,
                initial_state=[1.0],
                max_nfev=2000,
            ),
            run_workspace=ws,
        )

    # max_nfev cap exceeded -> ComputeSimLimitError (both RK45 and stiff Radau Jacobian)
    for method in ("RK45", "Radau"):
        with pytest.raises(ComputeSimLimitError, match="max_nfev"):
            run_compute_sim(
                ComputeSimSpec(
                    model_kind="ode_ivp",
                    ode_method=method,  # type: ignore[arg-type]
                    expression="cos(50 * t) - y",
                    grid_variable="t",
                    grid_start=0.0,
                    grid_stop=10.0,
                    grid_points=50,
                    initial_state=[0.0],
                    max_nfev=10,
                ),
                run_workspace=ws,
            )

    # timeout_s exceeded inside ODE RHS -> deterministic ComputeSimTimeoutError
    ticks = iter([100.0, 100.1, 105.0])
    monkeypatch.setattr(compute_sim_mod.time, "perf_counter", lambda: next(ticks, 200.0))
    with pytest.raises(ComputeSimTimeoutError, match="timeout_s"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="ode_ivp",
                expression="-y",
                grid_variable="t",
                grid_start=0.0,
                grid_stop=5.0,
                grid_points=20,
                initial_state=[1.0],
                timeout_s=1.0,
            ),
            run_workspace=ws,
        )

    # timeout_s exceeded in algebraic_grid -> deterministic ComputeSimTimeoutError
    grid_ticks = iter([200.0, 205.0])
    monkeypatch.setattr(compute_sim_mod.time, "perf_counter", lambda: next(grid_ticks, 300.0))
    with pytest.raises(ComputeSimTimeoutError, match="timeout_s"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="algebraic_grid",
                expression="sin(t)",
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=10,
                timeout_s=1.0,
            ),
            run_workspace=ws,
        )

    # Mid-expression vectorized timeout_s inside _eval_vectorized (algebraic_grid & inline fft)
    mid_grid_ticks = iter([300.0, 300.1, 300.2, 305.0])
    monkeypatch.setattr(compute_sim_mod.time, "perf_counter", lambda: next(mid_grid_ticks, 400.0))
    with pytest.raises(ComputeSimTimeoutError, match="timeout_s"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="algebraic_grid",
                expression="sin(t) + cos(t)",
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=10,
                timeout_s=1.0,
            ),
            run_workspace=ws,
        )

    mid_fft_ticks = iter([500.0, 500.1, 505.0])
    monkeypatch.setattr(compute_sim_mod.time, "perf_counter", lambda: next(mid_fft_ticks, 600.0))
    with pytest.raises(ComputeSimTimeoutError, match="timeout_s"):
        run_compute_sim(
            ComputeSimSpec(
                model_kind="fft_spectrum",
                expression="sin(2 * pi * t) + cos(2 * pi * t)",
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=16,
                timeout_s=1.0,
            ),
            run_workspace=ws,
        )


def test_overflow_guards_zero_warnings_and_max_grid_points(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        # 1. Huge integer literal (> float64 max) raises ComputeSimError, not OverflowError
        with pytest.raises(ComputeSimError, match="overflows float64"):
            run_compute_sim(
                ComputeSimSpec(
                    model_kind="algebraic_grid",
                    expression="1" + "0" * 350,
                    grid_variable="t",
                    grid_start=0.0,
                    grid_stop=1.0,
                    grid_points=4,
                ),
                run_workspace=ws,
            )

        # 2. Overflowing grid span [-1e308, 1e308] rejected cleanly across all grid modes
        with pytest.raises(ComputeSimError, match="Non-finite grid span"):
            run_compute_sim(
                ComputeSimSpec(
                    model_kind="algebraic_grid",
                    expression="5.0",
                    grid_variable="t",
                    grid_start=-1e308,
                    grid_stop=1e308,
                    grid_points=4,
                ),
                run_workspace=ws,
            )

        with pytest.raises(ComputeSimError, match="Non-finite grid span"):
            run_compute_sim(
                ComputeSimSpec(
                    model_kind="ode_ivp",
                    expression="0.0",
                    grid_variable="t",
                    grid_start=-1e308,
                    grid_stop=1e308,
                    grid_points=4,
                    initial_state=[1.0],
                ),
                run_workspace=ws,
            )

        with pytest.raises(ComputeSimError, match="Non-finite grid span"):
            run_compute_sim(
                ComputeSimSpec(
                    model_kind="fft_spectrum",
                    expression="1.0",
                    grid_variable="t",
                    grid_start=-1e308,
                    grid_stop=1e308,
                    grid_points=4,
                ),
                run_workspace=ws,
            )

        # 3. Tiny dt_s overflowing omega_rad_s -> ComputeSimError (never AnalysisError)
        with pytest.raises(ComputeSimError, match="angular frequencies"):
            run_compute_sim(
                ComputeSimSpec(
                    model_kind="fft_spectrum",
                    expression="1.0",
                    grid_variable="t",
                    grid_start=0.0,
                    grid_stop=3e-308,
                    grid_points=4,
                ),
                run_workspace=ws,
            )

        # 4. Huge signal detrend mean overflow -> ComputeSimError with zero RuntimeWarnings
        with pytest.raises(ComputeSimError, match="Non-finite signal mean"):
            run_compute_sim(
                ComputeSimSpec(
                    model_kind="fft_spectrum",
                    expression="1e308",
                    grid_variable="t",
                    grid_start=0.0,
                    grid_stop=3.0,
                    grid_points=4,
                    detrend_mean=True,
                ),
                run_workspace=ws,
            )

        # 5. Dataset-backed FFT with overflowing timestamp span -> ComputeSimError
        data_ingest(
            DatasetSpec(
                dataset_id="huge-t-span",
                source="simulator_captured",
                rows=[
                    {"t": -0.9e308, "y": 1.0},
                    {"t": -0.4e308, "y": 2.0},
                    {"t": 0.2e308, "y": 3.0},
                    {"t": 0.9e308, "y": 4.0},
                ],
            ),
            ws,
        )
        with pytest.raises(ComputeSimError, match="Invalid mean sample spacing"):
            run_compute_sim(
                ComputeSimSpec(
                    model_kind="fft_spectrum",
                    input_dataset_id="huge-t-span",
                    time_column="t",
                    signal_column="y",
                ),
                run_workspace=ws,
            )

    runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
    assert runtime_warnings == []

    # 6. Full 50,000-point grid persists cleanly with bounded 10-row preview
    res_max = run_compute_sim(
        ComputeSimSpec(
            model_kind="algebraic_grid",
            expression="sin(2 * pi * t)",
            grid_variable="t",
            grid_start=0.0,
            grid_stop=1.0,
            grid_points=50_000,
        ),
        run_workspace=ws,
    )
    assert res_max.row_count == 50_000
    assert len(res_max.preview_rows) == 10
    assert ws.verify_provenance().ok is True


def test_real_multithreaded_concurrent_auto_id_and_explicit_id_immutability(
    tmp_path: Path,
) -> None:
    ws_auto = _make_workspace(tmp_path / "auto")

    def _auto_worker(idx: int) -> ComputeSimResult:
        return run_compute_sim(
            ComputeSimSpec(
                model_kind="algebraic_grid",
                expression=f"t + {idx}",
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=25,
            ),
            run_workspace=ws_auto,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = [fut.result() for fut in [pool.submit(_auto_worker, i) for i in range(16)]]

    ids = [r.dataset_id for r in results]
    assert len(results) == 16
    assert len(set(ids)) == 16
    assert sorted(ids) == [f"compute-algebraic_grid-{i:04d}" for i in range(1, 17)]
    assert list((ws_auto.root / "datasets").glob("*.tmp")) == []
    assert ws_auto.verify_provenance().ok is True

    # Real concurrent contention on the SAME explicit dataset_id: 1 wins, 7 raise FileExistsError
    ws_explicit = _make_workspace(tmp_path / "explicit")

    def _explicit_worker(idx: int) -> ComputeSimResult:
        return run_compute_sim(
            ComputeSimSpec(
                model_kind="algebraic_grid",
                expression=f"t + {idx}",
                grid_variable="t",
                grid_start=0.0,
                grid_stop=1.0,
                grid_points=25,
                dataset_id="shared-explicit-id",
            ),
            run_workspace=ws_explicit,
        )

    wins: list[ComputeSimResult] = []
    conflicts = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for fut in [pool.submit(_explicit_worker, i) for i in range(8)]:
            try:
                wins.append(fut.result())
            except FileExistsError:
                conflicts += 1

    assert len(wins) == 1
    assert conflicts == 7
    assert list((ws_explicit.root / "datasets").glob("*.tmp")) == []
    assert ws_explicit.verify_provenance().ok is True
