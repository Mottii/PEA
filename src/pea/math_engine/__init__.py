"""Physics math & data analysis subsystem (dimensional AST calculator + OLS regression)."""

from __future__ import annotations

from pea.math_engine.analysis import (
    AnalysisError,
    compute_comparison,
    compute_derived_rows,
    data_analyze,
    data_ingest,
    load_dataset,
    run_ols_regression,
)
from pea.math_engine.calculator import (
    BUILTIN_CONSTANTS,
    DIMLESS,
    UNIT_TABLE,
    CalcError,
    DimensionError,
    Dims,
    PhysicsCalculator,
    Quantity,
    UnitParseError,
    UnsafeExpressionError,
    calc_eval,
    dims_div,
    dims_mul,
    dims_pow,
    format_si_dims,
    load_calculations,
    parse_unit,
)

__all__ = [
    "BUILTIN_CONSTANTS",
    "DIMLESS",
    "UNIT_TABLE",
    "AnalysisError",
    "CalcError",
    "DimensionError",
    "Dims",
    "PhysicsCalculator",
    "Quantity",
    "UnitParseError",
    "UnsafeExpressionError",
    "calc_eval",
    "compute_comparison",
    "compute_derived_rows",
    "data_analyze",
    "data_ingest",
    "dims_div",
    "dims_mul",
    "dims_pow",
    "format_si_dims",
    "load_calculations",
    "load_dataset",
    "parse_unit",
    "run_ols_regression",
]
