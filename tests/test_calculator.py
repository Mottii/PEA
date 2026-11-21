"""Offline unit tests for Phase 2B safe 5-dim SI dimensional physics calculator."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pea.math_engine import (
    CalcError,
    DimensionError,
    PhysicsCalculator,
    UnitParseError,
    UnsafeExpressionError,
    calc_eval,
    format_si_dims,
    load_calculations,
    parse_unit,
)
from pea.schemas import CalcRequest, CalcVariable
from pea.workspace import WorkspaceManager


class TestUnitsAndDimensions:
    def test_compound_and_unicode_units(self) -> None:
        scale_ut, dims_ut = parse_unit("µT")
        assert math.isclose(scale_ut, 1e-6, rel_tol=1e-12)
        assert dims_ut == (1, 0, -2, -1, 0)

        scale_ohm, dims_ohm = parse_unit("kΩ")
        assert math.isclose(scale_ohm, 1e3, rel_tol=1e-12)
        assert dims_ohm == (1, 2, -3, -2, 0)

        scale_rad_s, dims_rad_s = parse_unit("rad/s")
        assert math.isclose(scale_rad_s, 1.0, rel_tol=1e-12)
        assert dims_rad_s == (0, 0, -1, 0, 0)

        scale_cyr_s, dims_cyr_s = parse_unit("с⁻¹")
        assert math.isclose(scale_cyr_s, 1.0, rel_tol=1e-12)
        assert dims_cyr_s == (0, 0, -1, 0, 0)

        scale_mu, dims_mu = parse_unit("T*m/A")
        assert math.isclose(scale_mu, 1.0, rel_tol=1e-12)
        assert dims_mu == (1, 1, -2, -2, 0)

        scale_ka_m, dims_ka_m = parse_unit("kA/m")
        assert math.isclose(scale_ka_m, 1e3, rel_tol=1e-12)
        assert dims_ka_m == (0, -1, 0, 1, 0)

        scale_m3, dims_m3 = parse_unit("m^-3")
        assert math.isclose(scale_m3, 1.0, rel_tol=1e-12)
        assert dims_m3 == (0, -3, 0, 0, 0)

        # Canonical SI names round-trip back to scale=1.0 and identical dims
        for dims in [
            (0, 0, 0, 0, 0),
            (1, 0, -2, -1, 0),
            (1, 2, -3, -2, 0),
            (-1, -2, 4, 2, 0),
            (1, 2, -2, -2, 0),
            (1, 1, -2, -2, 0),
            (0, -3, 0, 0, 0),
        ]:
            formatted = format_si_dims(dims)
            s, d = parse_unit(formatted)
            assert math.isclose(s, 1.0, rel_tol=1e-12)
            assert d == dims

    def test_unknown_unit_rejected(self) -> None:
        with pytest.raises(UnitParseError):
            parse_unit("furlongs/fortnight")
        with pytest.raises(UnitParseError):
            parse_unit("m + s")


class TestPhysicsCalculations:
    def test_rlc_resonance_and_quality_factor_lab5(self) -> None:
        # Team 2 Lab 5 parameters: L = 5.0 mH, C = 32 uF, R = 3.2 Ohm
        w0 = calc_eval(
            CalcRequest(
                expression="1 / sqrt(L * C)",
                variables={
                    "L": CalcVariable(value=5.0, unit="mH"),
                    "C": CalcVariable(value=32.0, unit="uF"),
                },
                output_unit="rad/s",
            )
        )
        assert w0.si_dims == (0, 0, -1, 0, 0)
        assert math.isclose(float(w0.output_value), 2500.0, rel_tol=1e-12)
        assert w0.output_unit == "rad/s"
        assert w0.provenance == "model_computed"

        # Capacitor-voltage resonance frequency w_C = w0 * sqrt(1 - 1/(2*Q^2))
        wc = calc_eval(
            CalcRequest(
                expression="(1 / sqrt(L * C)) * sqrt(1 - (R**2 * C) / (2 * L))",
                variables={
                    "L": CalcVariable(value=5.0, unit="mH"),
                    "C": CalcVariable(value=32.0, unit="uF"),
                    "R": CalcVariable(value=3.2, unit="Ohm"),
                },
                output_unit="rad/s",
            )
        )
        assert math.isclose(float(wc.output_value), 2458.698842884179, rel_tol=1e-9)

        # Verify picofarad scaling (1000 pF == 1 nF)
        cap = calc_eval(
            CalcRequest(
                expression="C1 + C2",
                variables={
                    "C1": CalcVariable(value=1000.0, unit="pF"),
                    "C2": CalcVariable(value=2.0, unit="nF"),
                },
                output_unit="nF",
            )
        )
        assert math.isclose(float(cap.output_value), 3.0, rel_tol=1e-12)

    def test_biot_savart_wire_and_loop_lab4(self) -> None:
        # Straight wire: B = mu0 * I / (2 * pi * r)
        b_wire = calc_eval(
            CalcRequest(
                expression="mu0 * I / (2 * pi * r)",
                variables={
                    "I": CalcVariable(value=10.0, unit="A"),
                    "r": CalcVariable(value=2.0, unit="cm"),
                },
                output_unit="µT",
            )
        )
        assert b_wire.si_dims == (1, 0, -2, -1, 0)
        assert math.isclose(float(b_wire.output_value), 100.0, rel_tol=1e-12)

        # Circular loop on axis: B = mu0 * I * R^2 / (2 * (R^2 + r^2)^(3/2))
        b_loop = calc_eval(
            CalcRequest(
                expression="mu0 * I * R**2 / (2 * (R**2 + r**2)**1.5)",
                variables={
                    "I": CalcVariable(value=20.0, unit="A"),
                    "R": CalcVariable(value=5.0, unit="cm"),
                    "r": CalcVariable(value=0.0, unit="cm"),
                },
                output_unit="uT",
            )
        )
        expected_ut = (4 * math.pi * 1e-7 * 20.0 / (2 * 0.05)) * 1e6
        assert math.isclose(float(b_loop.output_value), expected_ut, rel_tol=1e-12)

    def test_builtin_constants_relationships(self) -> None:
        # 1 / sqrt(mu0 * eps0) == c
        c_check = calc_eval(CalcRequest(expression="1 / sqrt(mu0 * eps0)", output_unit="m/s"))
        assert math.isclose(float(c_check.output_value), 299_792_458.0, rel_tol=1e-12)

        # e * 1 V == 1 eV
        ev_check = calc_eval(
            CalcRequest(
                expression="e * U",
                variables={"U": CalcVariable(value=1.0, unit="V")},
                output_unit="eV",
            )
        )
        assert math.isclose(float(ev_check.output_value), 1.0, rel_tol=1e-12)

        # Boltzmann thermal energy kB * T in meV / eV and gravity g0
        grav = calc_eval(
            CalcRequest(
                expression="m * g0",
                variables={"m": CalcVariable(value=100.0, unit="g")},
                output_unit="N",
            )
        )
        assert math.isclose(float(grav.output_value), 0.980665, rel_tol=1e-12)

        # Electron/proton mass ratio
        ratio = calc_eval(CalcRequest(expression="mp / me"))
        assert ratio.si_dims == (0, 0, 0, 0, 0)
        assert math.isclose(float(ratio.output_value), 1836.15267343, rel_tol=1e-7)

    def test_complex_ac_impedance(self) -> None:
        # Z = R + j*w*L + 1/(j*w*C) at R=3.2 Ohm, w=2500 rad/s, L=5 mH, C=32 uF
        # Note w*L = 12.5 Ohm, 1/(w*C) = 12.5 Ohm -> exact resonance Z = 3.2 + 0j
        z_res = calc_eval(
            CalcRequest(
                expression="R + j * w * L + 1 / (j * w * C)",
                variables={
                    "R": CalcVariable(value=3.2, unit="Ohm"),
                    "w": CalcVariable(value=2500.0, unit="rad/s"),
                    "L": CalcVariable(value=5.0, unit="mH"),
                    "C": CalcVariable(value=32.0, unit="uF"),
                },
                output_unit="Ohm",
            )
        )
        assert z_res.si_dims == (1, 2, -3, -2, 0)
        assert math.isclose(float(z_res.output_value), 3.2, rel_tol=1e-12)
        assert z_res.output_imag is not None
        assert abs(z_res.output_imag) < 1e-12

        # Inductive impedance Z = 3 + 4j Ohm -> |Z| = 5 Ohm, arg(Z) = atan2(4, 3)
        z_mag = calc_eval(
            CalcRequest(
                expression="abs(R + j * w * L)",
                variables={
                    "R": CalcVariable(value=3.0, unit="Ohm"),
                    "w": CalcVariable(value=1000.0, unit="rad/s"),
                    "L": CalcVariable(value=4.0, unit="mH"),
                },
                output_unit="Ohm",
            )
        )
        assert math.isclose(float(z_mag.output_value), 5.0, rel_tol=1e-12)
        assert z_mag.output_imag is None

        z_phase = calc_eval(
            CalcRequest(
                expression="arg(R + j * w * L)",
                variables={
                    "R": CalcVariable(value=3.0, unit="Ohm"),
                    "w": CalcVariable(value=1000.0, unit="rad/s"),
                    "L": CalcVariable(value=4.0, unit="mH"),
                },
                output_unit="deg",
            )
        )
        expected_deg = math.degrees(math.atan2(4, 3))
        assert math.isclose(float(z_phase.output_value), expected_deg, rel_tol=1e-12)

        # re, im, conj, and complex input variable
        z_conj_im = calc_eval(
            CalcRequest(
                expression="im(conj(Z))",
                variables={"Z": CalcVariable(value=3.0, imag=4.0, unit="Ohm")},
                output_unit="Ohm",
            )
        )
        assert math.isclose(float(z_conj_im.output_value), -4.0, rel_tol=1e-12)

    def test_3d_vectors_and_lorentz_force(self) -> None:
        # F = q * (v x B)
        force = calc_eval(
            CalcRequest(
                expression="q * cross(v, B)",
                variables={
                    "q": CalcVariable(value=2.0, unit="uC"),
                    "v": CalcVariable(value=[3.0, 0.0, 0.0], unit="m/s"),
                    "B": CalcVariable(value=[0.0, 4.0, 0.0], unit="mT"),
                },
                output_unit="N",
            )
        )
        assert force.si_dims == (1, 1, -2, 0, 0)
        assert isinstance(force.output_value, list)
        assert force.output_value == pytest.approx([0.0, 0.0, 2.4e-8], rel=1e-12)

        # Dot product work W = dot(F, d) in J
        work = calc_eval(
            CalcRequest(
                expression="dot(F, d)",
                variables={
                    "F": CalcVariable(value=[3.0, 4.0, 0.0], unit="N"),
                    "d": CalcVariable(value=[20.0, 0.0, 5.0], unit="cm"),
                },
                output_unit="J",
            )
        )
        assert math.isclose(float(work.output_value), 0.6, rel_tol=1e-12)

        # Vector norm
        speed = calc_eval(
            CalcRequest(
                expression="norm(v)",
                variables={"v": CalcVariable(value=[3.0, 4.0, 12.0], unit="m/s")},
                output_unit="m/s",
            )
        )
        assert math.isclose(float(speed.output_value), 13.0, rel_tol=1e-12)

    def test_unary_precedence(self) -> None:
        res1 = calc_eval(CalcRequest(expression="-2**2"))
        assert float(res1.output_value) == -4.0

        res2 = calc_eval(CalcRequest(expression="(-2)**2"))
        assert float(res2.output_value) == 4.0

        res3 = calc_eval(CalcRequest(expression="2**-3"))
        assert math.isclose(float(res3.output_value), 0.125, rel_tol=1e-12)

    def test_gaussian_uncertainty_propagation(self) -> None:
        # Uncorrelated: w0 = 1 / sqrt(L * C)
        # Relative uncertainty = 0.5 * sqrt((dL/L)^2 + (dC/C)^2)
        res = calc_eval(
            CalcRequest(
                expression="1 / sqrt(L * C)",
                variables={
                    "L": CalcVariable(value=5.0, unit="mH", uncertainty=0.05),
                    "C": CalcVariable(value=32.0, unit="uF", uncertainty=0.64),
                },
                output_unit="rad/s",
            )
        )
        expected_sigma = 2500.0 * 0.5 * math.hypot(0.05 / 5.0, 0.64 / 32.0)
        assert res.uncertainty_si is not None
        assert math.isclose(res.uncertainty_si, expected_sigma, rel_tol=1e-12)

        # Correlated cancellation: x - x must have zero uncertainty
        cancel = calc_eval(
            CalcRequest(
                expression="x - x",
                variables={"x": CalcVariable(value=10.0, unit="m", uncertainty=0.3)},
            )
        )
        assert cancel.uncertainty_si == 0.0

        # Correlated doubling: x + x must have 2*sigma, not sqrt(2)*sigma
        doubled = calc_eval(
            CalcRequest(
                expression="x + x",
                variables={"x": CalcVariable(value=10.0, unit="m", uncertainty=0.3)},
            )
        )
        assert doubled.uncertainty_si is not None
        assert math.isclose(doubled.uncertainty_si, 0.6, rel_tol=1e-12)

        # Complex impedance magnitude uncertainty: |R + j*w*L| at R=3, w*L=4
        z_unc = calc_eval(
            CalcRequest(
                expression="abs(R + j * w * L)",
                variables={
                    "R": CalcVariable(value=3.0, unit="Ohm", uncertainty=0.1),
                    "w": CalcVariable(value=1000.0, unit="rad/s"),
                    "L": CalcVariable(value=4.0, unit="mH", uncertainty=0.1),
                },
            )
        )
        # d|Z|/dR = 3/5 = 0.6; d|Z|/d(wL) = 4/5 = 0.8; sigma_wL = 1000 * 0.1e-3 = 0.1
        expected_z_sigma = math.hypot(0.6 * 0.1, 0.8 * 0.1)
        assert z_unc.uncertainty_si is not None
        assert math.isclose(z_unc.uncertainty_si, expected_z_sigma, rel_tol=1e-12)

        # Holomorphic complex transcendental & sqrt uncertainty: Re(exp(j*phi)) and |sqrt(R+j*X)|
        exp_unc = calc_eval(
            CalcRequest(
                expression="re(exp(j * phi))",
                variables={"phi": CalcVariable(value=math.pi / 4.0, uncertainty=0.1)},
            )
        )
        assert exp_unc.uncertainty_si is not None
        assert math.isclose(exp_unc.uncertainty_si, math.sin(math.pi / 4.0) * 0.1, rel_tol=1e-12)

        sqrt_unc = calc_eval(
            CalcRequest(
                expression="abs(sqrt(R + j * X))",
                variables={
                    "R": CalcVariable(value=3.0, uncertainty=0.1),
                    "X": CalcVariable(value=4.0, uncertainty=0.1),
                },
            )
        )
        # |sqrt(R+jX)| = (R^2+X^2)^(1/4) -> d/dR = R / (2*(R^2+X^2)^(3/4))
        expected_sqrt_sigma = math.hypot(3.0 / (2.0 * 5.0**1.5) * 0.1, 4.0 / (2.0 * 5.0**1.5) * 0.1)
        assert sqrt_unc.uncertainty_si is not None
        assert math.isclose(sqrt_unc.uncertainty_si, expected_sqrt_sigma, rel_tol=1e-12)

        # Vector uncertainty propagation through vec(), scalar*vec, cross(), norm(), and dot()
        vec_unc = calc_eval(
            CalcRequest(
                expression="norm(vec(vx, vy, vz))",
                variables={
                    "vx": CalcVariable(value=3.0, unit="m/s", uncertainty=0.1),
                    "vy": CalcVariable(value=4.0, unit="m/s", uncertainty=0.2),
                    "vz": CalcVariable(value=0.0, unit="m/s"),
                },
            )
        )
        assert vec_unc.uncertainty_si is not None
        assert math.isclose(vec_unc.uncertainty_si, math.hypot(0.6 * 0.1, 0.8 * 0.2), rel_tol=1e-12)

        lorentz_unc = calc_eval(
            CalcRequest(
                expression="norm(q * cross(v, B))",
                variables={
                    "q": CalcVariable(value=2.0, unit="C", uncertainty=0.1),
                    "v": CalcVariable(value=[3.0, 0.0, 0.0], unit="m/s"),
                    "B": CalcVariable(value=[0.0, 4.0, 0.0], unit="T"),
                },
                output_unit="N",
            )
        )
        assert lorentz_unc.uncertainty_si is not None
        assert math.isclose(lorentz_unc.uncertainty_si, 1.2, rel_tol=1e-12)

        # Negative base integer power & dimensional atan2 uncertainty
        neg_pow = calc_eval(
            CalcRequest(
                expression="x**3",
                variables={"x": CalcVariable(value=-2.0, uncertainty=0.1)},
            )
        )
        assert neg_pow.uncertainty_si is not None
        assert math.isclose(neg_pow.uncertainty_si, 1.2, rel_tol=1e-12)

        angle_unc = calc_eval(
            CalcRequest(
                expression="atan2(y, x)",
                variables={
                    "y": CalcVariable(value=4.0, unit="m", uncertainty=0.1),
                    "x": CalcVariable(value=3.0, unit="m", uncertainty=0.1),
                },
            )
        )
        assert angle_unc.uncertainty_si is not None
        assert math.isclose(angle_unc.uncertainty_si, 0.02, rel_tol=1e-12)

    def test_extreme_scales_and_blank_output_unit_guards(self) -> None:
        # Very small non-zero values must not trigger spurious derivative ZeroDivisionError
        ratio = calc_eval(CalcRequest(expression="1e-200 / 1e-200"))
        assert math.isclose(float(ratio.output_value), 1.0, rel_tol=1e-12)
        arg_tiny = calc_eval(CalcRequest(expression="arg(1e-200 + 1e-200 * j)"))
        assert math.isclose(float(arg_tiny.output_value), math.pi / 4.0, rel_tol=1e-12)
        atan2_tiny = calc_eval(CalcRequest(expression="atan2(1e-200, 1e-200)"))
        assert math.isclose(float(atan2_tiny.output_value), math.pi / 4.0, rel_tol=1e-12)

        # Derivative overflow must raise CalcError, not raw OverflowError
        with pytest.raises(CalcError):
            calc_eval(
                CalcRequest(
                    expression="x**-1",
                    variables={"x": CalcVariable(value=1e-200, uncertainty=1e-205)},
                )
            )

        # Whitespace-only output_unit must raise UnitParseError, not Pydantic ValidationError
        with pytest.raises(UnitParseError):
            calc_eval(CalcRequest(expression="1 + 1", output_unit="   "))

    def test_concurrent_calculation_ledger_writes(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="concurrent-calc")

        def _worker(i: int) -> str:
            return calc_eval(CalcRequest(expression=f"{i} * 3 + 1"), workspace=ws).calc_id

        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(_worker, range(24)))

        loaded = load_calculations(ws)
        assert len(loaded) == 24
        assert set(loaded.keys()) == set(ids)

    def test_dimensional_mismatch_errors(self) -> None:
        with pytest.raises(DimensionError):
            calc_eval(
                CalcRequest(
                    expression="x + t",
                    variables={
                        "x": CalcVariable(value=1.0, unit="m"),
                        "t": CalcVariable(value=2.0, unit="s"),
                    },
                )
            )
        with pytest.raises(DimensionError):
            calc_eval(
                CalcRequest(
                    expression="sin(x)",
                    variables={"x": CalcVariable(value=1.0, unit="m")},
                )
            )
        with pytest.raises(DimensionError):
            calc_eval(
                CalcRequest(
                    expression="sqrt(x)",
                    variables={"x": CalcVariable(value=4.0, unit="m")},
                )
            )
        with pytest.raises(DimensionError):
            calc_eval(
                CalcRequest(
                    expression="R",
                    variables={"R": CalcVariable(value=5.0, unit="Ohm")},
                    output_unit="F",
                )
            )

    def test_persistent_calculation_ledger(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="calc-ledger-test")
        calc = PhysicsCalculator()

        r1 = calc.evaluate(
            CalcRequest(
                expression="1 / sqrt(L * C)",
                variables={
                    "L": CalcVariable(value=5.0, unit="mH"),
                    "C": CalcVariable(value=32.0, unit="uF"),
                },
                output_unit="rad/s",
            ),
            workspace=ws,
        )
        r2 = calc.evaluate(CalcRequest(expression="2 * pi * 50"), workspace=ws)

        assert r1.calc_id != r2.calc_id
        assert r1.calc_id.startswith("calc-")
        loaded = load_calculations(ws)
        assert set(loaded.keys()) == {r1.calc_id, r2.calc_id}
        assert math.isclose(float(loaded[r1.calc_id].output_value), 2500.0, rel_tol=1e-12)
        assert ws.verify_provenance().ok


class TestCalculatorSecurity:
    @pytest.mark.parametrize(
        "expr",
        [
            "__import__('os').system('echo hacked')",
            "eval('1 + 1')",
            "exec('x = 1')",
            "(1).__class__.__mro__",
            "open('/etc/passwd')",
            "[x for x in [1, 2]]",
            "(lambda x: x)(1)",
            "True + 1",
            "abs(x=1)",
            "__builtins__",
        ],
    )
    def test_rejects_unsafe_expressions(self, expr: str) -> None:
        with pytest.raises((UnsafeExpressionError, CalcError)):
            calc_eval(CalcRequest(expression=expr))

    @pytest.mark.parametrize(
        "expr",
        [
            "1 / 0",
            "1e308 * 10",
            "log(0)",
            "sqrt(-4)",
            "2 ** 100000",
            "(1+" * 70 + "1" + ")" * 70,
        ],
    )
    def test_rejects_non_finite_and_dos_expressions(self, expr: str) -> None:
        with pytest.raises(CalcError):
            calc_eval(CalcRequest(expression=expr))

    def test_rejects_non_finite_variable_values(self) -> None:
        with pytest.raises(CalcError):
            calc_eval(
                CalcRequest(
                    expression="x + 1",
                    variables={"x": CalcVariable(value=float("nan"))},
                )
            )
        with pytest.raises(CalcError):
            calc_eval(
                CalcRequest(
                    expression="x",
                    variables={"x": CalcVariable(value=1.0, uncertainty=float("inf"))},
                )
            )
