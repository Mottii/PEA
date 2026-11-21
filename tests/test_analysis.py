"""Offline unit tests for Phase 2B dataset ingestion, safe AST transforms, and OLS regression."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from pea.config import PathConfinementError
from pea.math_engine import (
    AnalysisError,
    UnsafeExpressionError,
    compute_comparison,
    compute_derived_rows,
    data_analyze,
    data_ingest,
    load_dataset,
    run_ols_regression,
)
from pea.schemas import (
    ComparisonSpec,
    DatasetSpec,
    DerivedColumnSpec,
    RegressionSpec,
)
from pea.workspace import WorkspaceManager


class TestDataIngest:
    def test_ingest_csv_json_and_inline_with_provenance(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="ingest-test")

        # 1. CSV ingest with UTF-8 BOM, string group column, and numeric coercion
        csv_path = tmp_path / "bench.csv"
        csv_path.write_text(
            "\ufeffgroup,r_m,B_T\nI=10A,0.02,0.0001\nI=10A,0.04,0.00005\n",
            encoding="utf-8",
        )
        ds_csv = data_ingest(
            csv_path,
            ws,
            source="bench_manual",
            dataset_id="lab4-csv",
            units={"r_m": "m", "B_T": "T"},
        )
        assert ds_csv.dataset_id == "lab4-csv"
        assert ds_csv.source == "bench_manual"
        assert ds_csv.rows[0]["group"] == "I=10A"
        assert math.isclose(ds_csv.rows[0]["r_m"], 0.02)

        # 2. JSON DatasetSpec preserves embedded ProvenanceSource
        json_path = tmp_path / "sim.json"
        json_path.write_text(
            json.dumps(
                {
                    "source": "simulator_captured",
                    "rows": [{"x": 1.0, "y": 2.0}, {"x": 2.0, "y": 4.0}],
                    "units": {"x": "1", "y": "1"},
                }
            ),
            encoding="utf-8",
        )
        ds_json = data_ingest(json_path, ws, dataset_id="sim-json")
        assert ds_json.source == "simulator_captured"

        # 3. Inline DatasetSpec with auto-generated ID
        ds_inline = data_ingest(
            DatasetSpec(
                source="model_computed",
                rows=[{"a": 1, "b": 2}, {"a": 3, "b": 6}],
            ),
            ws,
        )
        assert ds_inline.dataset_id is not None
        loaded = load_dataset(ds_inline.dataset_id, ws)
        assert loaded.source == "model_computed"
        assert len(loaded.rows) == 2

        # All datasets are recorded and pass provenance verification
        ver = ws.verify_provenance()
        assert ver.ok
        assert ver.checked_entries == 3

    def test_ingest_rejects_missing_source_traversal_and_overwrite(self, tmp_path: Path) -> None:
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="ingest-guards")

        csv_path = tmp_path / "raw.csv"
        csv_path.write_text("x,y\n1,2\n3,4\n", encoding="utf-8")

        with pytest.raises(AnalysisError):
            data_ingest(csv_path, ws)  # missing explicit ProvenanceSource

        with pytest.raises(PathConfinementError):
            data_ingest(csv_path, ws, source="bench_manual", dataset_id="../escape")

        data_ingest(csv_path, ws, source="bench_manual", dataset_id="unique-ds")
        with pytest.raises(FileExistsError):
            data_ingest(csv_path, ws, source="bench_manual", dataset_id="unique-ds")


class TestPhysicsBenchmarksAnalysis:
    def test_lab4_mu0_wire_and_loop_regressions(self, tmp_path: Path) -> None:
        """Exact Lab 4 benchmark: recover mu0 from straight wire (1/r) and loop (R^2+r^2)^(-3/2)."""
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="lab4-mu0")

        mu0_true = 4.0 * math.pi * 1e-7
        wire_rows = []
        for current_a in (5.0, 10.0, 20.0):
            for r_cm in (2.0, 4.0, 6.0, 8.0, 10.0):
                r_m = r_cm * 1e-2
                b_tesla = mu0_true * current_a / (2.0 * math.pi * r_m)
                wire_rows.append({"I_A": current_a, "r": r_m, "B_T": b_tesla})

        data_ingest(
            DatasetSpec(dataset_id="lab4-wire", source="simulator_captured", rows=wire_rows),
            ws,
        )

        res = data_analyze(
            "lab4-wire",
            ws,
            derived_columns=[DerivedColumnSpec(name="inv_r", formula="1 / r", unit="m^-1")],
            regressions=[
                RegressionSpec(
                    name="B_vs_inv_r_free",
                    x_column="inv_r",
                    y_column="B_T",
                    intercept="free",
                    group_by="I_A",
                ),
                RegressionSpec(
                    name="B_vs_inv_r_zero",
                    x_column="inv_r",
                    y_column="B_T",
                    intercept="zero",
                    group_by="I_A",
                ),
            ],
        )

        assert res.derived_columns == ["inv_r"]
        assert len(res.regressions) == 6
        free_by_group = {
            r.group: r for r in res.regressions if r.name == "B_vs_inv_r_free"
        }
        zero_by_group = {
            r.group: r for r in res.regressions if r.name == "B_vs_inv_r_zero"
        }

        for current_a in (5.0, 10.0, 20.0):
            gkey = str(current_a)
            for reg in (free_by_group[gkey], zero_by_group[gkey]):
                recovered_mu0 = reg.slope * 2.0 * math.pi / current_a
                assert math.isclose(recovered_mu0, mu0_true, rel_tol=1e-12)
                assert reg.r_squared == pytest.approx(1.0, abs=1e-12)
            assert zero_by_group[gkey].intercept == 0.0
            assert zero_by_group[gkey].intercept_se is None

        # Circular loop on axis: B = (mu0 * I * R^2 / 2) * (R^2 + r^2)^(-1.5)
        radius_m = 0.05
        loop_rows = []
        for r_m in (0.0, 0.02, 0.04, 0.06, 0.08, 0.10):
            b_t = mu0_true * 20.0 * (radius_m**2) / (2.0 * (radius_m**2 + r_m**2) ** 1.5)
            loop_rows.append({"R": radius_m, "r": r_m, "I_A": 20.0, "B_T": b_t})

        data_ingest(
            DatasetSpec(dataset_id="lab4-loop", source="simulator_captured", rows=loop_rows),
            ws,
        )
        loop_res = data_analyze(
            "lab4-loop",
            ws,
            derived_columns=[
                DerivedColumnSpec(
                    name="geom",
                    formula="(R**2 + r**2)**(-1.5)",
                    unit="m^-3",
                )
            ],
            regressions=[
                RegressionSpec(
                    name="loop_zero",
                    x_column="geom",
                    y_column="B_T",
                    intercept="zero",
                )
            ],
            comparisons=[
                ComparisonSpec(
                    name="mu0_check",
                    measured=mu0_true * 1.002,
                    reference=mu0_true,
                    unit="T*m/A",
                )
            ],
            persist_derived_id="lab4-loop-derived",
        )
        loop_slope = loop_res.regressions[0].slope
        mu0_from_loop = 2.0 * loop_slope / (20.0 * radius_m**2)
        assert math.isclose(mu0_from_loop, mu0_true, rel_tol=1e-12)
        assert loop_res.comparisons[0].percent_deviation == pytest.approx(0.2, rel=1e-9)
        assert "geom" in loop_res.summary_stats
        assert load_dataset("lab4-loop-derived", ws).rows[0]["geom"] == pytest.approx(
            radius_m ** (-3.0), rel=1e-12
        )
        assert ws.verify_provenance().ok

    def test_lab5_4q2_resonance_plateau_benchmark(self, tmp_path: Path) -> None:
        """Exact Lab 5 benchmark: w0/dw vs Q^2 with slope ~ 4 (theoretical w0/dw = 4*Q^2)."""
        mgr = WorkspaceManager(tmp_path / "runs")
        ws = mgr.create_run(label="lab5-4q2")

        # Synthetic exact-relationship rows for L = 5 mH, C = 32 uF, R in {3.2, 4.0, 5.0, 6.0}
        # Using w0 = 2500 rad/s, Q = (1/R)*sqrt(L/C), dw = w0 / (4 * Q^2)
        l_h = 5.0e-3
        c_f = 32.0e-6
        w0_exact = 1.0 / math.sqrt(l_h * c_f)
        rows = []
        for r_ohm in (3.2, 4.0, 5.0, 6.0):
            q_val = (1.0 / r_ohm) * math.sqrt(l_h / c_f)
            dw = w0_exact / (4.0 * q_val**2)
            rows.append(
                {
                    "R": r_ohm,
                    "L": l_h,
                    "C": c_f,
                    "w1": w0_exact - dw / 2.0,
                    "w2": w0_exact + dw / 2.0,
                }
            )

        data_ingest(
            DatasetSpec(dataset_id="lab5-rlc", source="simulator_captured", rows=rows),
            ws,
        )
        res = data_analyze(
            "lab5-rlc",
            ws,
            derived_columns=[
                DerivedColumnSpec(name="w0", formula="(w1 + w2) / 2", unit="rad/s"),
                DerivedColumnSpec(name="dw", formula="w2 - w1", unit="rad/s"),
                DerivedColumnSpec(name="ratio", formula="w0 / dw", unit="1"),
                DerivedColumnSpec(name="Q", formula="(1 / R) * sqrt(L / C)", unit="1"),
                DerivedColumnSpec(name="Q2", formula="Q**2", unit="1"),
            ],
            regressions=[
                RegressionSpec(
                    name="ratio_vs_Q2_zero",
                    x_column="Q2",
                    y_column="ratio",
                    intercept="zero",
                ),
                RegressionSpec(
                    name="ratio_vs_Q2_free",
                    x_column="Q2",
                    y_column="ratio",
                    intercept="free",
                ),
            ],
        )

        assert res.derived_columns == ["w0", "dw", "ratio", "Q", "Q2"]
        reg_zero, reg_free = res.regressions
        assert math.isclose(reg_zero.slope, 4.0, rel_tol=1e-12)
        assert reg_zero.intercept == 0.0
        assert reg_zero.intercept_se is None
        assert math.isclose(reg_free.slope, 4.0, rel_tol=1e-12)
        assert abs(reg_free.intercept) < 1e-10
        assert reg_free.r_squared == pytest.approx(1.0, abs=1e-12)


class TestRegressionAndFormulaEdgeCases:
    def test_n2_free_and_zero_intercept_no_nan(self) -> None:
        out_free = run_ols_regression([1.0, 3.0], [3.0, 7.0], name="two-pts", intercept="free")
        assert math.isclose(out_free.slope, 2.0)
        assert math.isclose(out_free.intercept, 1.0)
        assert out_free.slope_se == 0.0
        assert out_free.intercept_se == 0.0
        assert math.isfinite(out_free.r_squared)

        out_zero = run_ols_regression([1.0, 2.0], [2.1, 3.9], name="two-pts-z", intercept="zero")
        assert out_zero.intercept == 0.0
        assert out_zero.intercept_se is None
        assert out_zero.slope_se > 0.0
        assert math.isfinite(out_zero.r_squared)

    def test_free_vs_zero_intercept_differ_when_offset_present(self) -> None:
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [12.0, 14.0, 16.0, 18.0]  # exact y = 2x + 10
        free = run_ols_regression(xs, ys, name="free", intercept="free")
        zero = run_ols_regression(xs, ys, name="zero", intercept="zero")
        assert math.isclose(free.slope, 2.0)
        assert math.isclose(free.intercept, 10.0)
        assert free.r_squared == pytest.approx(1.0)
        assert zero.slope > 4.0
        assert zero.r_squared < free.r_squared

    def test_regression_error_paths(self) -> None:
        with pytest.raises(AnalysisError):
            run_ols_regression([1.0], [2.0], name="too-few")
        with pytest.raises(AnalysisError):
            run_ols_regression([2.0, 2.0, 2.0], [1.0, 2.0, 3.0], name="zero-var-x")
        with pytest.raises(AnalysisError):
            run_ols_regression([0.0, 0.0], [1.0, 2.0], name="zero-sum-x2", intercept="zero")

    def test_comparison_zero_reference(self) -> None:
        out = compute_comparison(ComparisonSpec(name="zero-ref", measured=0.05, reference=0.0))
        assert math.isclose(out.abs_diff, 0.05)
        assert out.percent_deviation is None

    def test_derived_formula_security_rejection(self) -> None:
        rows = [{"x": 2.0, "y": 0.0}]
        with pytest.raises(UnsafeExpressionError):
            compute_derived_rows(rows, [DerivedColumnSpec(name="bad", formula="__import__('os')")])
        with pytest.raises(UnsafeExpressionError):
            compute_derived_rows(rows, [DerivedColumnSpec(name="bad", formula="x.__class__")])
        with pytest.raises(AnalysisError):
            compute_derived_rows(rows, [DerivedColumnSpec(name="bad", formula="x / y")])
        with pytest.raises(AnalysisError):
            compute_derived_rows(rows, [DerivedColumnSpec(name="bad", formula="x ** 5000")])
