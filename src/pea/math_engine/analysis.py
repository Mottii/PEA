"""Deterministic tabular dataset ingestion, safe AST column transforms, and OLS regression.

Strictly zero ``eval()`` or ``exec()`` calls. Preserves explicit ``ProvenanceSource`` and
records ingested dataset files in ``provenance.jsonl``.
"""

from __future__ import annotations

import ast
import csv
import errno
import json
import math
import os
import re
import secrets
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from pea.config import PathConfinementError
from pea.math_engine.calculator import UnsafeExpressionError
from pea.schemas import (
    AnalysisResult,
    ComparisonOutput,
    ComparisonSpec,
    DatasetSpec,
    DerivedColumnSpec,
    ProvenanceSource,
    RegressionOutput,
    RegressionSpec,
)

if TYPE_CHECKING:
    from pea.workspace import RunWorkspace

_DATASET_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_VALID_SOURCES: frozenset[str] = frozenset({"simulator_captured", "bench_manual", "model_computed"})
_MAX_FORMULA_LEN = 1000
_MAX_AST_DEPTH = 48
_MAX_EXPONENT_ABS = 1000.0


class AnalysisError(ValueError):
    """Raised when dataset ingestion, transformation, or regression fails."""


def _validate_dataset_id(dataset_id: str) -> str:
    cleaned = dataset_id.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or cleaned in (".", ".."):
        raise PathConfinementError(f"Invalid dataset_id: {dataset_id!r}")
    if not _DATASET_ID_RE.match(cleaned):
        raise PathConfinementError(f"dataset_id contains disallowed characters: {dataset_id!r}")
    return cleaned


def _coerce_csv_cell(raw: str | None) -> Any:
    if raw is None:
        return None
    text = raw.strip()
    if text == "":
        return None
    try:
        num = float(text)
    except ValueError:
        return text
    if not math.isfinite(num):
        return text
    return num


def _reject_non_finite_values(obj: Any, context: str) -> None:
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise AnalysisError(f"Non-finite float value ({obj!r}) in {context}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _reject_non_finite_values(v, f"{context}.{k}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _reject_non_finite_values(item, f"{context}[{idx}]")


def _load_file_to_dataset_spec(
    file_path: Path,
    *,
    source: ProvenanceSource | None,
    dataset_id: str | None,
    units: dict[str, str] | None,
    notes: str,
) -> DatasetSpec:
    if not file_path.is_file():
        raise FileNotFoundError(f"Dataset file not found: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        if source is None:
            raise AnalysisError(
                "Explicit ProvenanceSource ('simulator_captured', 'bench_manual', or "
                "'model_computed') is required when ingesting a CSV file"
            )
        rows: list[dict[str, Any]] = []
        with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise AnalysisError(f"CSV file has no header row: {file_path}")
            seen_headers: set[str] = set()
            header_map: list[tuple[str, str]] = []
            for raw_hdr in reader.fieldnames:
                cleaned_hdr = (raw_hdr or "").strip()
                if not cleaned_hdr:
                    raise AnalysisError(f"CSV file contains empty column header in {file_path}")
                if cleaned_hdr in seen_headers:
                    raise AnalysisError(
                        f"CSV file contains duplicate column header {cleaned_hdr!r} in {file_path}"
                    )
                seen_headers.add(cleaned_hdr)
                header_map.append((raw_hdr, cleaned_hdr))
            for raw_row in reader:
                if None in raw_row:
                    raise AnalysisError(f"CSV row has more columns than header in {file_path}")
                if all(v is None or str(v).strip() == "" for v in raw_row.values()):
                    continue
                rows.append(
                    {
                        cleaned_hdr: _coerce_csv_cell(raw_row.get(raw_hdr))
                        for raw_hdr, cleaned_hdr in header_map
                    }
                )
        if not rows:
            raise AnalysisError(f"CSV file contains no data rows: {file_path}")
        return DatasetSpec(
            dataset_id=dataset_id,
            source=source,
            rows=rows,
            units=dict(units) if units else {},
            notes=notes,
        )

    if suffix == ".json":
        def _reject_non_finite_json(token: str) -> Any:
            raise AnalysisError(f"Non-standard JSON constant {token!r} in {file_path}")

        try:
            payload = json.loads(
                file_path.read_text(encoding="utf-8"),
                parse_constant=_reject_non_finite_json,
            )
        except json.JSONDecodeError as exc:
            raise AnalysisError(f"Invalid JSON in dataset file {file_path}: {exc.msg}") from exc
        if isinstance(payload, dict) and "rows" in payload:
            merged = dict(payload)
            if source is not None:
                merged["source"] = source
            elif "source" not in merged:
                raise AnalysisError(
                    "JSON dataset lacks 'source'; provide an explicit ProvenanceSource"
                )
            if dataset_id is not None:
                merged["dataset_id"] = dataset_id
            if units:
                merged_units = dict(merged.get("units") or {})
                merged_units.update(units)
                merged["units"] = merged_units
            if notes:
                merged["notes"] = notes
            return DatasetSpec.model_validate(merged)

        if isinstance(payload, list):
            if source is None:
                raise AnalysisError(
                    "Explicit ProvenanceSource is required when ingesting a raw JSON row list"
                )
            if not payload or not all(isinstance(r, dict) for r in payload):
                raise AnalysisError("JSON row list must be a non-empty list of objects")
            return DatasetSpec(
                dataset_id=dataset_id,
                source=source,
                rows=payload,
                units=dict(units) if units else {},
                notes=notes,
            )
        raise AnalysisError(
            f"Unsupported JSON dataset structure in {file_path}; expected DatasetSpec or row list"
        )

    raise AnalysisError(f"Unsupported dataset file format {suffix!r}; expected .csv or .json")


def data_ingest(
    spec_or_path: DatasetSpec | dict[str, Any] | Path | str,
    run_workspace: RunWorkspace,
    *,
    source: ProvenanceSource | None = None,
    dataset_id: str | None = None,
    units: dict[str, str] | None = None,
    notes: str = "",
) -> DatasetSpec:
    """Import inline rows, CSV, or JSON into ``<run>/datasets/<dataset_id>.json``.

    Preserves explicit ``ProvenanceSource``, writes atomically, refuses to overwrite an
    existing dataset ID (protecting provenance hashes), and logs SHA-256 in ``provenance.jsonl``.
    """
    if source is not None and source not in _VALID_SOURCES:
        raise AnalysisError(f"Invalid ProvenanceSource: {source!r}")

    if isinstance(spec_or_path, DatasetSpec):
        ds = spec_or_path.model_copy(
            update={
                "dataset_id": dataset_id if dataset_id is not None else spec_or_path.dataset_id,
                "source": source if source is not None else spec_or_path.source,
                "units": {**spec_or_path.units, **(units or {})},
                "notes": notes or spec_or_path.notes,
            }
        )
    elif isinstance(spec_or_path, dict):
        merged = dict(spec_or_path)
        if source is not None:
            merged["source"] = source
        if dataset_id is not None:
            merged["dataset_id"] = dataset_id
        if units:
            merged["units"] = {**(merged.get("units") or {}), **units}
        if notes:
            merged["notes"] = notes
        ds = DatasetSpec.model_validate(merged)
    elif isinstance(spec_or_path, (Path, str)):
        ds = _load_file_to_dataset_spec(
            Path(spec_or_path),
            source=source,
            dataset_id=dataset_id,
            units=units,
            notes=notes,
        )
    else:
        raise AnalysisError(
            f"Unsupported input type for data_ingest: {type(spec_or_path).__name__}"
        )

    _reject_non_finite_values(ds.rows, "dataset rows")
    raw_id = ds.dataset_id if ds.dataset_id is not None else f"ds-{secrets.token_hex(4)}"
    resolved_id = _validate_dataset_id(raw_id)
    final_ds = ds.model_copy(update={"dataset_id": resolved_id})

    datasets_dir = run_workspace.resolve_path("datasets")
    datasets_dir.mkdir(parents=True, exist_ok=True)
    relpath = f"datasets/{resolved_id}.json"
    target_path = run_workspace.resolve_path(relpath)
    exists_msg = (
        f"Dataset {resolved_id!r} already exists in run {run_workspace.run_id}; "
        "use a unique dataset_id to preserve provenance immutability"
    )
    if target_path.exists():
        raise FileExistsError(exists_msg)

    payload = final_ds.model_dump_json(indent=2) + "\n"
    tmp_path = run_workspace.resolve_path(
        f"datasets/.{resolved_id}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    )
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Atomic non-overwriting publish: fails with FileExistsError if another
            # thread/process claimed target_path concurrently, while ensuring readers
            # never observe a partially written JSON file.
            os.link(tmp_path, target_path)
        except FileExistsError as exc:
            raise FileExistsError(exists_msg) from exc
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            try:
                with target_path.open("xb") as handle:
                    handle.write(payload.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError as fex:
                raise FileExistsError(exists_msg) from fex
    finally:
        tmp_path.unlink(missing_ok=True)

    run_workspace.record_artifact(
        relpath,
        kind="dataset",
        metadata={
            "dataset_id": resolved_id,
            "row_count": len(final_ds.rows),
            "columns": _collect_columns(final_ds.rows),
        },
        source=final_ds.source,
    )
    return final_ds


def load_dataset(dataset_id: str, run_workspace: RunWorkspace) -> DatasetSpec:
    """Load a persisted ``DatasetSpec`` from ``<run>/datasets/<dataset_id>.json``."""
    validated_id = _validate_dataset_id(dataset_id)
    target = run_workspace.resolve_path(f"datasets/{validated_id}.json")
    if not target.is_file():
        raise FileNotFoundError(
            f"Dataset {validated_id!r} not found in run {run_workspace.run_id}"
        )
    return DatasetSpec.model_validate_json(target.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Safe AST Row-Level Formula Evaluator (Zero eval/exec)
# ---------------------------------------------------------------------------

_ROW_CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "euler": math.e,
}

_ROW_FUNCTIONS: dict[str, Any] = {
    "sqrt": lambda x: math.sqrt(x),
    "abs": lambda x: abs(x),
    "sin": lambda x: math.sin(x),
    "cos": lambda x: math.cos(x),
    "tan": lambda x: math.tan(x),
    "asin": lambda x: math.asin(x),
    "acos": lambda x: math.acos(x),
    "atan": lambda x: math.atan(x),
    "exp": lambda x: math.exp(x),
    "log": lambda x: math.log(x),
    "log10": lambda x: math.log10(x),
}

_ROW_BINARY_FUNCTIONS: dict[str, Any] = {
    "atan2": lambda y, x: math.atan2(y, x),
}


def _to_finite_float(val: Any, context: str) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise AnalysisError(f"Expected numeric value for {context}; got {val!r}")
    out = float(val)
    if not math.isfinite(out):
        raise AnalysisError(f"Non-finite numeric value for {context}: {val!r}")
    return out


def _eval_row_node(node: ast.AST, row: dict[str, Any], depth: int = 0) -> float:
    if depth > _MAX_AST_DEPTH:
        raise UnsafeExpressionError(f"Formula exceeds maximum AST depth ({_MAX_AST_DEPTH})")

    if isinstance(node, ast.Expression):
        return _eval_row_node(node.body, row, depth + 1)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise UnsafeExpressionError(f"Unsupported literal in formula: {node.value!r}")
        return _to_finite_float(node.value, "formula literal")

    if isinstance(node, ast.Name):
        ident = node.id
        if ident.startswith("__") or ident.endswith("__"):
            raise UnsafeExpressionError(f"Forbidden dunder identifier in formula: {ident!r}")
        if ident in row:
            return _to_finite_float(row[ident], f"column {ident!r}")
        if ident in _ROW_CONSTANTS:
            return _ROW_CONSTANTS[ident]
        raise AnalysisError(f"Unknown column or constant in formula: {ident!r}")

    if isinstance(node, ast.UnaryOp):
        operand = _eval_row_node(node.operand, row, depth + 1)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise UnsafeExpressionError(f"Unsupported unary operator: {type(node.op).__name__}")

    if isinstance(node, ast.BinOp):
        left = _eval_row_node(node.left, row, depth + 1)
        right = _eval_row_node(node.right, row, depth + 1)
        if isinstance(node.op, ast.Add):
            res = left + right
        elif isinstance(node.op, ast.Sub):
            res = left - right
        elif isinstance(node.op, ast.Mult):
            res = left * right
        elif isinstance(node.op, ast.Div):
            if right == 0.0:
                raise AnalysisError("Division by zero in derived column formula")
            res = left / right
        elif isinstance(node.op, ast.Pow):
            if abs(right) > _MAX_EXPONENT_ABS:
                raise AnalysisError(
                    f"Exponent magnitude exceeds safe limit ({_MAX_EXPONENT_ABS:g})"
                )
            if left < 0.0 and not float(right).is_integer():
                raise AnalysisError(
                    f"Negative base ({left:g}) raised to non-integer exponent ({right:g})"
                )
            if left == 0.0 and right < 0.0:
                raise AnalysisError("Zero raised to a negative power in derived column formula")
            try:
                res = float(left**right)
            except (OverflowError, ZeroDivisionError, ValueError) as exc:
                raise AnalysisError(f"Invalid power operation in formula: {exc}") from exc
        else:
            raise UnsafeExpressionError(
                f"Unsupported binary operator in formula: {type(node.op).__name__}"
            )
        return _to_finite_float(res, "formula result")

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise UnsafeExpressionError(
                "Only direct calls to whitelisted math functions are allowed"
            )
        if node.keywords:
            raise UnsafeExpressionError("Keyword arguments are forbidden in column formulas")
        fn_name = node.func.id
        if fn_name in _ROW_FUNCTIONS:
            if len(node.args) != 1:
                raise AnalysisError(f"{fn_name}() expects 1 argument; got {len(node.args)}")
            arg_val = _eval_row_node(node.args[0], row, depth + 1)
            try:
                res = float(_ROW_FUNCTIONS[fn_name](arg_val))
            except (ValueError, OverflowError, ZeroDivisionError) as exc:
                raise AnalysisError(f"{fn_name}() domain error for {arg_val:g}: {exc}") from exc
            return _to_finite_float(res, f"{fn_name}() result")
        if fn_name in _ROW_BINARY_FUNCTIONS:
            if len(node.args) != 2:
                raise AnalysisError(f"{fn_name}() expects 2 arguments; got {len(node.args)}")
            a1 = _eval_row_node(node.args[0], row, depth + 1)
            a2 = _eval_row_node(node.args[1], row, depth + 1)
            try:
                res = float(_ROW_BINARY_FUNCTIONS[fn_name](a1, a2))
            except (ValueError, OverflowError, ZeroDivisionError) as exc:
                raise AnalysisError(f"{fn_name}() domain error: {exc}") from exc
            return _to_finite_float(res, f"{fn_name}() result")
        raise UnsafeExpressionError(f"Forbidden or unknown function in formula: {fn_name!r}")

    raise UnsafeExpressionError(f"Disallowed AST construct in formula: {type(node).__name__}")


def _validate_row_ast(node: ast.AST, depth: int = 0) -> None:
    """Validate AST node whitelist and depth statically before evaluating over rows."""
    if depth > _MAX_AST_DEPTH:
        raise UnsafeExpressionError(f"Formula exceeds maximum AST depth ({_MAX_AST_DEPTH})")
    if isinstance(node, ast.Expression):
        _validate_row_ast(node.body, depth + 1)
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise UnsafeExpressionError(f"Unsupported literal in formula: {node.value!r}")
        _to_finite_float(node.value, "formula literal")
        return
    if isinstance(node, ast.Name):
        ident = node.id
        if ident.startswith("__") or ident.endswith("__"):
            raise UnsafeExpressionError(f"Forbidden dunder identifier in formula: {ident!r}")
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub)):
            raise UnsafeExpressionError(f"Unsupported unary operator: {type(node.op).__name__}")
        _validate_row_ast(node.operand, depth + 1)
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            raise UnsafeExpressionError(
                f"Unsupported binary operator in formula: {type(node.op).__name__}"
            )
        _validate_row_ast(node.left, depth + 1)
        _validate_row_ast(node.right, depth + 1)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise UnsafeExpressionError(
                "Only direct calls to whitelisted math functions are allowed"
            )
        if node.keywords:
            raise UnsafeExpressionError("Keyword arguments are forbidden in column formulas")
        fn_name = node.func.id
        if fn_name in _ROW_FUNCTIONS:
            if len(node.args) != 1:
                raise AnalysisError(f"{fn_name}() expects 1 argument; got {len(node.args)}")
        elif fn_name in _ROW_BINARY_FUNCTIONS:
            if len(node.args) != 2:
                raise AnalysisError(f"{fn_name}() expects 2 arguments; got {len(node.args)}")
        else:
            raise UnsafeExpressionError(f"Forbidden or unknown function in formula: {fn_name!r}")
        for arg in node.args:
            _validate_row_ast(arg, depth + 1)
        return
    raise UnsafeExpressionError(f"Disallowed AST construct in formula: {type(node).__name__}")


def compute_derived_rows(
    rows: Sequence[dict[str, Any]],
    derived_columns: Sequence[DerivedColumnSpec],
) -> list[dict[str, Any]]:
    """Evaluate derived column formulas sequentially over *rows*, returning new row dicts."""
    working_rows = [dict(r) for r in rows]
    for spec in derived_columns:
        col_name = spec.name.strip()
        if not col_name:
            raise AnalysisError("Derived column name must not be empty or whitespace-only")
        formula = spec.formula.strip()
        if not formula:
            raise AnalysisError(f"Derived column {col_name!r} has an empty formula")
        if len(formula) > _MAX_FORMULA_LEN:
            raise UnsafeExpressionError(
                f"Formula for {col_name!r} exceeds maximum length ({_MAX_FORMULA_LEN} chars)"
            )
        try:
            tree = ast.parse(formula, mode="eval")
        except SyntaxError as exc:
            raise AnalysisError(
                f"Syntax error in derived column {col_name!r} formula {formula!r}: {exc.msg}"
            ) from exc
        _validate_row_ast(tree)

        for row_idx, row in enumerate(working_rows):
            try:
                row[col_name] = _eval_row_node(tree, row)
            except (AnalysisError, UnsafeExpressionError) as exc:
                raise type(exc)(
                    f"Derived column {col_name!r} failed at row {row_idx}: {exc}"
                ) from exc
    return working_rows


# ---------------------------------------------------------------------------
# OLS Linear Regression (Free & Zero Intercept) & Comparisons
# ---------------------------------------------------------------------------


def run_ols_regression(
    x_values: Sequence[float],
    y_values: Sequence[float],
    *,
    name: str,
    intercept: str = "free",
    group: str | None = None,
) -> RegressionOutput:
    """Compute exact ordinary least-squares regression for free or zero-intercept models."""
    if len(x_values) != len(y_values):
        raise AnalysisError(
            f"Regression {name!r}: x ({len(x_values)}) and y ({len(y_values)}) lengths differ"
        )
    n = len(x_values)
    if n < 2:
        raise AnalysisError(
            f"Regression {name!r} (group={group!r}) requires at least 2 points; got {n}"
        )

    x = np.asarray(x_values, dtype=np.float64)
    y = np.asarray(y_values, dtype=np.float64)
    if not (np.all(np.isfinite(x)) and np.all(np.isfinite(y))):
        raise AnalysisError(f"Regression {name!r} contains non-finite x or y values")

    # Rescale coordinates internally to prevent intermediate square overflow/underflow
    sx = float(np.max(np.abs(x)))
    sy = float(np.max(np.abs(y)))
    if sx == 0.0:
        if intercept == "free":
            raise AnalysisError(
                f"Regression {name!r} (group={group!r}) failed: x column has zero variance"
            )
        raise AnalysisError(
            f"Zero-intercept regression {name!r} (group={group!r}) failed: sum(x^2) == 0"
        )
    xs = x / sx
    ys = (y / sy) if sy > 0.0 else np.zeros_like(y)
    scale_ratio = sy / sx

    ys_mean = float(np.mean(ys))
    ss_tot = float(np.sum((ys - ys_mean) ** 2))

    if intercept == "free":
        xs_mean = float(np.mean(xs))
        dx = xs - xs_mean
        sxx = float(np.sum(dx * dx))
        if sxx == 0.0:
            raise AnalysisError(
                f"Regression {name!r} (group={group!r}) failed: x column has zero variance"
            )
        sxy = float(np.sum(dx * (ys - ys_mean)))
        slope_s = sxy / sxx
        b_s = ys_mean - slope_s * xs_mean
        residuals = ys - (slope_s * xs + b_s)
        ss_res = float(np.sum(residuals * residuals))

        if n > 2:
            s2 = max(0.0, ss_res / (n - 2))
            slope_se_s = math.sqrt(s2 / sxx)
            intercept_se_s: float | None = math.sqrt(s2 * (1.0 / n + (xs_mean * xs_mean) / sxx))
        else:
            slope_se_s = 0.0
            intercept_se_s = 0.0

        if ss_tot == 0.0:
            r_squared = 1.0 if ss_res <= 1e-30 else 0.0
        else:
            r_squared = min(1.0, max(0.0, 1.0 - ss_res / ss_tot))

        slope = float(slope_s * scale_ratio)
        slope_se = float(slope_se_s * scale_ratio)
        b = float(b_s * sy)
        intercept_se = float(intercept_se_s * sy) if intercept_se_s is not None else None
        if not (
            math.isfinite(slope)
            and math.isfinite(slope_se)
            and math.isfinite(b)
            and (intercept_se is None or math.isfinite(intercept_se))
            and math.isfinite(r_squared)
        ):
            raise AnalysisError(f"Regression {name!r} produced non-finite statistics")

        return RegressionOutput(
            name=name,
            group=group,
            intercept_mode="free",
            slope=slope,
            slope_se=slope_se,
            intercept=b,
            intercept_se=intercept_se,
            r_squared=float(r_squared),
            n_points=n,
        )

    if intercept == "zero":
        sum_x2 = float(np.sum(xs * xs))
        if sum_x2 == 0.0:
            raise AnalysisError(
                f"Zero-intercept regression {name!r} (group={group!r}) failed: sum(x^2) == 0"
            )
        slope_s = float(np.sum(xs * ys)) / sum_x2
        residuals = ys - slope_s * xs
        ss_res = float(np.sum(residuals * residuals))
        s2 = max(0.0, ss_res / (n - 1))
        slope_se_s = math.sqrt(s2 / sum_x2)

        if ss_tot == 0.0:
            r_squared = 1.0 if ss_res <= 1e-30 else 0.0
        else:
            r_squared = min(1.0, 1.0 - ss_res / ss_tot)

        slope = float(slope_s * scale_ratio)
        slope_se = float(slope_se_s * scale_ratio)
        if not (math.isfinite(slope) and math.isfinite(slope_se) and math.isfinite(r_squared)):
            raise AnalysisError(
                f"Zero-intercept regression {name!r} produced non-finite statistics"
            )

        return RegressionOutput(
            name=name,
            group=group,
            intercept_mode="zero",
            slope=slope,
            slope_se=slope_se,
            intercept=0.0,
            intercept_se=None,
            r_squared=float(r_squared),
            n_points=n,
        )

    raise AnalysisError(f"Unsupported intercept mode: {intercept!r}")


def compute_comparison(spec: ComparisonSpec) -> ComparisonOutput:
    """Compute absolute difference and percentage deviation against a reference value."""
    measured = _to_finite_float(spec.measured, f"comparison {spec.name!r} measured")
    reference = _to_finite_float(spec.reference, f"comparison {spec.name!r} reference")
    abs_diff = abs(measured - reference)
    pct_dev: float | None = None
    if reference != 0.0:
        pct_dev = (abs_diff / abs(reference)) * 100.0
    return ComparisonOutput(
        name=spec.name,
        measured=measured,
        reference=reference,
        abs_diff=abs_diff,
        percent_deviation=pct_dev,
        unit=spec.unit,
    )


def _collect_columns(rows: Sequence[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen.keys())


def _compute_summary_stats(
    rows: Sequence[dict[str, Any]],
    columns: Sequence[str],
) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for col in columns:
        vals: list[float] = []
        for row in rows:
            raw = row.get(col)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            fval = float(raw)
            if math.isfinite(fval):
                vals.append(fval)
        if not vals:
            continue
        arr = np.asarray(vals, dtype=np.float64)
        std_val = float(np.std(arr, ddof=1)) if len(arr) >= 2 else 0.0
        stats[col] = {
            "count": float(len(arr)),
            "mean": float(np.mean(arr)),
            "std": std_val,
            "min": float(np.min(arr)),
            "max": float(np.max(arr)),
            "median": float(np.median(arr)),
        }
    return stats


def _extract_xy(
    rows: Sequence[dict[str, Any]],
    x_col: str,
    y_col: str,
    reg_name: str,
) -> tuple[list[float], list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    for idx, row in enumerate(rows):
        if x_col not in row:
            raise AnalysisError(f"Regression {reg_name!r}: missing x column {x_col!r} at row {idx}")
        if y_col not in row:
            raise AnalysisError(f"Regression {reg_name!r}: missing y column {y_col!r} at row {idx}")
        xs.append(_to_finite_float(row[x_col], f"{reg_name}.{x_col}[{idx}]"))
        ys.append(_to_finite_float(row[y_col], f"{reg_name}.{y_col}[{idx}]"))
    return xs, ys


def data_analyze(
    dataset_id: str,
    run_workspace: RunWorkspace,
    *,
    derived_columns: Sequence[DerivedColumnSpec | dict[str, Any]] | None = None,
    regressions: Sequence[RegressionSpec | dict[str, Any]] | None = None,
    comparisons: Sequence[ComparisonSpec | dict[str, Any]] | None = None,
    persist_derived_id: str | None = None,
) -> AnalysisResult:
    """Run safe derived transforms, OLS regressions, comparisons, and column summary stats."""
    dataset = load_dataset(dataset_id, run_workspace)

    derived_specs = [
        spec if isinstance(spec, DerivedColumnSpec) else DerivedColumnSpec.model_validate(spec)
        for spec in (derived_columns or ())
    ]
    regression_specs = [
        spec if isinstance(spec, RegressionSpec) else RegressionSpec.model_validate(spec)
        for spec in (regressions or ())
    ]
    comparison_specs = [
        spec if isinstance(spec, ComparisonSpec) else ComparisonSpec.model_validate(spec)
        for spec in (comparisons or ())
    ]

    if persist_derived_id is not None:
        validated_persist_id = _validate_dataset_id(persist_derived_id)
        if run_workspace.resolve_path(f"datasets/{validated_persist_id}.json").exists():
            raise FileExistsError(
                f"Dataset {validated_persist_id!r} already exists in run {run_workspace.run_id}; "
                "use a unique persist_derived_id to preserve provenance immutability"
            )

    working_rows = compute_derived_rows(dataset.rows, derived_specs)
    columns = _collect_columns(working_rows)

    reg_outputs: list[RegressionOutput] = []
    for reg in regression_specs:
        if reg.group_by is None:
            xs, ys = _extract_xy(working_rows, reg.x_column, reg.y_column, reg.name)
            reg_outputs.append(
                run_ols_regression(xs, ys, name=reg.name, intercept=reg.intercept, group=None)
            )
        else:
            groups: dict[str, list[dict[str, Any]]] = {}
            for idx, row in enumerate(working_rows):
                if reg.group_by not in row:
                    raise AnalysisError(
                        f"Regression {reg.name!r}: missing group_by column "
                        f"{reg.group_by!r} at row {idx}"
                    )
                raw_gval = row[reg.group_by]
                if raw_gval is None or (isinstance(raw_gval, str) and not raw_gval.strip()):
                    raise AnalysisError(
                        f"Regression {reg.name!r}: empty/missing group_by value in "
                        f"{reg.group_by!r} at row {idx}"
                    )
                gkey = str(raw_gval)
                groups.setdefault(gkey, []).append(row)
            for gkey, grows in groups.items():
                xs, ys = _extract_xy(grows, reg.x_column, reg.y_column, reg.name)
                reg_outputs.append(
                    run_ols_regression(xs, ys, name=reg.name, intercept=reg.intercept, group=gkey)
                )

    comp_outputs = [compute_comparison(comp) for comp in comparison_specs]
    summary_stats = _compute_summary_stats(working_rows, columns)

    if persist_derived_id is not None:
        merged_units = dict(dataset.units)
        for dspec in derived_specs:
            merged_units[dspec.name.strip()] = dspec.unit
        data_ingest(
            DatasetSpec(
                dataset_id=persist_derived_id,
                source=dataset.source,
                rows=working_rows,
                units=merged_units,
                notes=dataset.notes,
            ),
            run_workspace,
        )

    return AnalysisResult(
        dataset_id=dataset.dataset_id or dataset_id,
        source=dataset.source,
        row_count=len(working_rows),
        columns=columns,
        derived_columns=[spec.name.strip() for spec in derived_specs],
        regressions=reg_outputs,
        comparisons=comp_outputs,
        summary_stats=summary_stats,
    )


__all__ = [
    "AnalysisError",
    "compute_comparison",
    "compute_derived_rows",
    "data_analyze",
    "data_ingest",
    "load_dataset",
    "run_ols_regression",
]
