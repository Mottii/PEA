"""Sandboxed numerical, coupled ODE, and FFT modeling engine (``compute_sim``).

Strictly zero ``eval()`` or ``exec()`` calls. Validates all expressions via a
whitelisted AST walker with hard resource limits, evaluates ODE right-hand sides
with scalar ``math`` and grid/inline-FFT expressions with vectorized ``numpy``
(preserving lazy-branch ``where(cond, a, b)`` semantics in both paths), and
persists every result atomically as an immutable ``model_computed`` dataset in
``<run>/datasets/``.
"""

from __future__ import annotations

import ast
import math
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np
from scipy.fft import rfft, rfftfreq
from scipy.integrate import solve_ivp

from pea.math_engine.analysis import data_ingest, load_dataset
from pea.schemas import ComputeModelKind, ComputeSimResult, ComputeSimSpec, DatasetSpec
from pea.workspace import sha256_file

if TYPE_CHECKING:
    from pea.workspace import RunWorkspace

_MAX_EXPRESSION_LEN = 1000
_MAX_AST_DEPTH = 48
_MAX_EXPONENT_ABS = 1000.0
_MAX_PREVIEW_ROWS = 10
_FFT_JITTER_THRESHOLD = 0.01

_CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "euler": math.e,
}


class ComputeSimError(ValueError):
    """Base error for invalid expressions, domain errors, solver failures, or bad inputs."""


class ComputeSimTimeoutError(ComputeSimError):
    """Raised when monotonic wall-clock execution exceeds ``spec.timeout_s``."""


class ComputeSimLimitError(ComputeSimError):
    """Raised when expression length, AST depth, exponent magnitude, or ``max_nfev`` is exceeded."""


def _scalar_sign(x: float) -> float:
    if x > 0.0:
        return 1.0
    if x < 0.0:
        return -1.0
    return 0.0


def _scalar_heaviside(x: float, h0: float = 0.5) -> float:
    if x < 0.0:
        return 0.0
    if x > 0.0:
        return 1.0
    return h0


_SCALAR_UNARY_FUNCS: dict[str, Callable[[float], float]] = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "exp": math.exp,
    "log": math.log,
    "log10": math.log10,
    "sqrt": math.sqrt,
    "abs": abs,
    "sign": _scalar_sign,
    "floor": lambda x: float(math.floor(x)),
    "ceil": lambda x: float(math.ceil(x)),
}

_VECTOR_UNARY_FUNCS: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "sin": np.sin,
    "cos": np.cos,
    "tan": np.tan,
    "asin": np.arcsin,
    "acos": np.arccos,
    "atan": np.arctan,
    "sinh": np.sinh,
    "cosh": np.cosh,
    "tanh": np.tanh,
    "exp": np.exp,
    "log": np.log,
    "log10": np.log10,
    "sqrt": np.sqrt,
    "abs": np.abs,
    "sign": np.sign,
    "floor": np.floor,
    "ceil": np.ceil,
}

_UNARY_FUNC_NAMES: frozenset[str] = frozenset(_SCALAR_UNARY_FUNCS.keys())

_ALLOWED_COMPARE_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_ALLOWED_BIN_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_ALLOWED_UNARY_OPS = (ast.UAdd, ast.USub)


# ---------------------------------------------------------------------------
# Static AST Sandbox Validator (Zero eval/exec)
# ---------------------------------------------------------------------------


def _validate_ast_node(
    node: ast.AST,
    allowed_names: set[str],
    depth: int,
    *,
    allow_compare: bool = False,
) -> None:
    if depth > _MAX_AST_DEPTH:
        raise ComputeSimLimitError(
            f"Expression exceeds maximum AST nesting depth ({_MAX_AST_DEPTH})"
        )

    if isinstance(node, ast.Expression):
        _validate_ast_node(node.body, allowed_names, depth + 1, allow_compare=False)
        return

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ComputeSimError(f"Unsupported literal in expression: {node.value!r}")
        try:
            literal_float = float(node.value)
        except OverflowError as exc:
            raise ComputeSimError(
                f"Numeric literal overflows float64 in expression: {node.value!r}"
            ) from exc
        if not math.isfinite(literal_float):
            raise ComputeSimError(f"Non-finite literal in expression: {node.value!r}")
        return

    if isinstance(node, ast.Name):
        ident = node.id
        if "__" in ident:
            raise ComputeSimError(f"Forbidden dunder identifier in expression: {ident!r}")
        if ident not in allowed_names and ident not in _CONSTANTS:
            raise ComputeSimError(f"Unknown or disallowed identifier in expression: {ident!r}")
        return

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARY_OPS):
            raise ComputeSimError(
                f"Unsupported unary operator in expression: {type(node.op).__name__}"
            )
        _validate_ast_node(node.operand, allowed_names, depth + 1, allow_compare=False)
        return

    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BIN_OPS):
            raise ComputeSimError(
                f"Unsupported binary operator in expression: {type(node.op).__name__}"
            )
        _validate_ast_node(node.left, allowed_names, depth + 1, allow_compare=False)
        _validate_ast_node(node.right, allowed_names, depth + 1, allow_compare=False)
        return

    if isinstance(node, ast.Compare):
        if not allow_compare:
            raise ComputeSimError(
                "Comparison operators are only permitted as the first argument to where(cond, a, b)"
            )
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise ComputeSimError("Chained comparisons are not supported in where()")
        if not isinstance(node.ops[0], _ALLOWED_COMPARE_OPS):
            raise ComputeSimError(
                f"Unsupported comparison operator in where(): {type(node.ops[0]).__name__}; "
                "allowed operators are <, <=, >, >="
            )
        _validate_ast_node(node.left, allowed_names, depth + 1, allow_compare=False)
        _validate_ast_node(node.comparators[0], allowed_names, depth + 1, allow_compare=False)
        return

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ComputeSimError(
                "Only direct calls to whitelisted mathematical functions are allowed"
            )
        if node.keywords:
            raise ComputeSimError("Keyword arguments are forbidden in compute_sim expressions")
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            raise ComputeSimError("Starred arguments are forbidden in compute_sim expressions")

        fn_name = node.func.id
        if fn_name.startswith("__") or fn_name.endswith("__"):
            raise ComputeSimError(f"Forbidden dunder call in expression: {fn_name!r}")

        n_args = len(node.args)
        if fn_name in _UNARY_FUNC_NAMES:
            if n_args != 1:
                raise ComputeSimError(f"{fn_name}() expects 1 argument; got {n_args}")
            _validate_ast_node(node.args[0], allowed_names, depth + 1, allow_compare=False)
            return

        if fn_name == "heaviside":
            if n_args not in (1, 2):
                raise ComputeSimError(f"heaviside() expects 1 or 2 arguments; got {n_args}")
            for arg in node.args:
                _validate_ast_node(arg, allowed_names, depth + 1, allow_compare=False)
            return

        if fn_name == "atan2":
            if n_args != 2:
                raise ComputeSimError(f"atan2() expects 2 arguments; got {n_args}")
            for arg in node.args:
                _validate_ast_node(arg, allowed_names, depth + 1, allow_compare=False)
            return

        if fn_name in ("min", "max"):
            if n_args < 2:
                raise ComputeSimError(f"{fn_name}() expects at least 2 arguments; got {n_args}")
            for arg in node.args:
                _validate_ast_node(arg, allowed_names, depth + 1, allow_compare=False)
            return

        if fn_name == "clip":
            if n_args != 3:
                raise ComputeSimError(f"clip() expects 3 arguments (x, lo, hi); got {n_args}")
            for arg in node.args:
                _validate_ast_node(arg, allowed_names, depth + 1, allow_compare=False)
            return

        if fn_name == "where":
            if n_args != 3:
                raise ComputeSimError(
                    f"where() expects 3 arguments (cond, a, b); got {n_args}"
                )
            if not isinstance(node.args[0], ast.Compare):
                raise ComputeSimError(
                    "where() first argument must be a comparison (<, <=, >, >=)"
                )
            _validate_ast_node(node.args[0], allowed_names, depth + 1, allow_compare=True)
            _validate_ast_node(node.args[1], allowed_names, depth + 1, allow_compare=False)
            _validate_ast_node(node.args[2], allowed_names, depth + 1, allow_compare=False)
            return

        raise ComputeSimError(f"Forbidden or unknown function in expression: {fn_name!r}")

    raise ComputeSimError(f"Disallowed AST construct in expression: {type(node).__name__}")


def _validate_compute_ast(expr: str, allowed_names: set[str]) -> ast.Expression:
    """Parse and statically validate *expr* against the ``compute_sim`` AST sandbox."""
    if not isinstance(expr, str) or not expr.strip():
        raise ComputeSimError("Expression must be a non-empty string")
    if len(expr) > _MAX_EXPRESSION_LEN:
        raise ComputeSimLimitError(
            f"Expression length ({len(expr)}) exceeds maximum ({_MAX_EXPRESSION_LEN})"
        )
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ComputeSimError(f"Invalid expression syntax ({exc.msg}): {expr!r}") from exc
    except RecursionError as exc:
        raise ComputeSimLimitError(
            f"Expression exceeds parser nesting depth ({_MAX_AST_DEPTH})"
        ) from exc

    _validate_ast_node(tree, allowed_names, depth=0, allow_compare=False)
    return tree


# ---------------------------------------------------------------------------
# Dual Evaluators: Scalar (ODE RHS) & Vectorized (Grids / Inline FFT)
# ---------------------------------------------------------------------------


def _ensure_finite_scalar(val: float, context: str) -> float:
    if not math.isfinite(val):
        raise ComputeSimError(f"Non-finite scalar result in {context}: {val!r}")
    return val


def _eval_compare_scalar(node: ast.Compare, env: dict[str, float]) -> bool:
    left = _eval_scalar(node.left, env)
    right = _eval_scalar(node.comparators[0], env)
    op = node.ops[0]
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    raise ComputeSimError(f"Unsupported comparison operator: {type(op).__name__}")


def _eval_scalar(node: ast.AST, env: dict[str, float]) -> float:
    """Evaluate a statically validated AST node with scalar ``math`` and lazy ``where()``."""
    if isinstance(node, ast.Expression):
        return _eval_scalar(node.body, env)

    if isinstance(node, ast.Constant):
        assert isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
        return float(node.value)

    if isinstance(node, ast.Name):
        ident = node.id
        if ident in env:
            return env[ident]
        if ident in _CONSTANTS:
            return _CONSTANTS[ident]
        raise ComputeSimError(f"Unknown identifier in scalar evaluation: {ident!r}")

    if isinstance(node, ast.UnaryOp):
        val = _eval_scalar(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return val
        if isinstance(node.op, ast.USub):
            return -val
        raise ComputeSimError(f"Unsupported unary operator: {type(node.op).__name__}")

    if isinstance(node, ast.BinOp):
        left = _eval_scalar(node.left, env)
        right = _eval_scalar(node.right, env)
        if isinstance(node.op, ast.Add):
            return _ensure_finite_scalar(left + right, "addition")
        if isinstance(node.op, ast.Sub):
            return _ensure_finite_scalar(left - right, "subtraction")
        if isinstance(node.op, ast.Mult):
            return _ensure_finite_scalar(left * right, "multiplication")
        if isinstance(node.op, ast.Div):
            if right == 0.0:
                raise ComputeSimError("Division by zero")
            try:
                res = left / right
            except (ZeroDivisionError, OverflowError) as exc:
                raise ComputeSimError(f"Division error: {exc}") from exc
            return _ensure_finite_scalar(res, "division")
        if isinstance(node.op, ast.Pow):
            if abs(right) > _MAX_EXPONENT_ABS:
                raise ComputeSimLimitError(
                    f"Exponent magnitude ({abs(right):g}) exceeds limit ({_MAX_EXPONENT_ABS:g})"
                )
            if left == 0.0 and right < 0.0:
                raise ComputeSimError("Zero raised to a negative power")
            if left < 0.0 and not float(right).is_integer():
                raise ComputeSimError(
                    f"Negative base ({left:g}) raised to non-integer exponent ({right:g})"
                )
            try:
                res = math.pow(left, right)
            except (ValueError, OverflowError, ZeroDivisionError) as exc:
                raise ComputeSimError(f"Power operation error: {exc}") from exc
            return _ensure_finite_scalar(res, "power")
        raise ComputeSimError(f"Unsupported binary operator: {type(node.op).__name__}")

    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        fn_name = node.func.id

        if fn_name == "where":
            assert isinstance(node.args[0], ast.Compare)
            if _eval_compare_scalar(node.args[0], env):
                return _eval_scalar(node.args[1], env)
            return _eval_scalar(node.args[2], env)

        if fn_name in _SCALAR_UNARY_FUNCS:
            arg = _eval_scalar(node.args[0], env)
            try:
                res = float(_SCALAR_UNARY_FUNCS[fn_name](arg))
            except (ValueError, OverflowError, ZeroDivisionError) as exc:
                raise ComputeSimError(f"{fn_name}() domain error for {arg:g}: {exc}") from exc
            return _ensure_finite_scalar(res, f"{fn_name}()")

        if fn_name == "heaviside":
            x_val = _eval_scalar(node.args[0], env)
            h0_val = _eval_scalar(node.args[1], env) if len(node.args) == 2 else 0.5
            return _ensure_finite_scalar(_scalar_heaviside(x_val, h0_val), "heaviside()")

        if fn_name == "atan2":
            y_val = _eval_scalar(node.args[0], env)
            x_val = _eval_scalar(node.args[1], env)
            return _ensure_finite_scalar(math.atan2(y_val, x_val), "atan2()")

        if fn_name == "min":
            vals = [_eval_scalar(arg, env) for arg in node.args]
            return min(vals)

        if fn_name == "max":
            vals = [_eval_scalar(arg, env) for arg in node.args]
            return max(vals)

        if fn_name == "clip":
            x_val = _eval_scalar(node.args[0], env)
            lo_val = _eval_scalar(node.args[1], env)
            hi_val = _eval_scalar(node.args[2], env)
            if lo_val > hi_val:
                raise ComputeSimError(
                    f"clip(x, lo, hi) requires lo <= hi; got lo={lo_val:g}, hi={hi_val:g}"
                )
            return min(max(x_val, lo_val), hi_val)

        raise ComputeSimError(f"Unsupported function in scalar evaluation: {fn_name!r}")

    raise ComputeSimError(f"Disallowed AST node in scalar evaluation: {type(node).__name__}")


def _ensure_finite_array(arr: np.ndarray, context: str) -> np.ndarray:
    if not np.all(np.isfinite(arr)):
        raise ComputeSimError(f"Non-finite value produced in {context}")
    return arr


def _slice_vector_env(
    env: dict[str, float | np.ndarray],
    mask: np.ndarray,
) -> dict[str, float | np.ndarray]:
    return {
        key: (val[mask] if isinstance(val, np.ndarray) else val)
        for key, val in env.items()
    }


def _make_finite_grid(start: float, stop: float, points: int, context: str) -> np.ndarray:
    """Build a uniform float64 grid without NumPy overflow warnings or non-finite values."""
    with np.errstate(all="ignore"):
        span = stop - start
        if not math.isfinite(span):
            raise ComputeSimError(
                f"Non-finite grid span ({start!r} to {stop!r}) in {context}"
            )
        grid = np.linspace(start, stop, points, dtype=np.float64)
    return _ensure_finite_array(grid, context)


def _eval_compare_vectorized(
    node: ast.Compare,
    env: dict[str, float | np.ndarray],
    length: int,
    *,
    deadline: float | None = None,
) -> np.ndarray:
    left = _eval_vectorized(node.left, env, length, deadline=deadline)
    right = _eval_vectorized(node.comparators[0], env, length, deadline=deadline)
    op = node.ops[0]
    if isinstance(op, ast.Lt):
        return np.asarray(left < right, dtype=bool)
    if isinstance(op, ast.LtE):
        return np.asarray(left <= right, dtype=bool)
    if isinstance(op, ast.Gt):
        return np.asarray(left > right, dtype=bool)
    if isinstance(op, ast.GtE):
        return np.asarray(left >= right, dtype=bool)
    raise ComputeSimError(f"Unsupported comparison operator: {type(op).__name__}")


def _eval_vectorized(
    node: ast.AST,
    env: dict[str, float | np.ndarray],
    length: int,
    *,
    deadline: float | None = None,
) -> np.ndarray:
    """Evaluate a validated AST node over a 1D ``float64`` vector with lazy ``where()``."""
    if deadline is not None and time.perf_counter() > deadline:
        raise ComputeSimTimeoutError("Vectorized expression evaluation exceeded timeout_s")

    with np.errstate(all="ignore"):
        if isinstance(node, ast.Expression):
            return _eval_vectorized(node.body, env, length, deadline=deadline)

        if isinstance(node, ast.Constant):
            assert isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
            return np.full(length, float(node.value), dtype=np.float64)

        if isinstance(node, ast.Name):
            ident = node.id
            if ident in env:
                val = env[ident]
                if isinstance(val, np.ndarray):
                    return val
                return np.full(length, float(val), dtype=np.float64)
            if ident in _CONSTANTS:
                return np.full(length, _CONSTANTS[ident], dtype=np.float64)
            raise ComputeSimError(f"Unknown identifier in vectorized evaluation: {ident!r}")

        if isinstance(node, ast.UnaryOp):
            operand = _eval_vectorized(node.operand, env, length, deadline=deadline)
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.USub):
                return -operand
            raise ComputeSimError(f"Unsupported unary operator: {type(node.op).__name__}")

        if isinstance(node, ast.BinOp):
            left = _eval_vectorized(node.left, env, length, deadline=deadline)
            right = _eval_vectorized(node.right, env, length, deadline=deadline)
            if isinstance(node.op, ast.Add):
                return _ensure_finite_array(left + right, "addition")
            if isinstance(node.op, ast.Sub):
                return _ensure_finite_array(left - right, "subtraction")
            if isinstance(node.op, ast.Mult):
                return _ensure_finite_array(left * right, "multiplication")
            if isinstance(node.op, ast.Div):
                if bool(np.any(right == 0.0)):
                    raise ComputeSimError("Division by zero")
                return _ensure_finite_array(left / right, "division")
            if isinstance(node.op, ast.Pow):
                if bool(np.any(np.abs(right) > _MAX_EXPONENT_ABS)):
                    raise ComputeSimLimitError(
                        f"Exponent magnitude exceeds limit ({_MAX_EXPONENT_ABS:g})"
                    )
                if bool(np.any((left == 0.0) & (right < 0.0))):
                    raise ComputeSimError("Zero raised to a negative power")
                is_non_int = right != np.trunc(right)
                if bool(np.any((left < 0.0) & is_non_int)):
                    raise ComputeSimError("Negative base raised to non-integer exponent")
                res = np.power(left, right)
                return _ensure_finite_array(np.asarray(res, dtype=np.float64), "power")
            raise ComputeSimError(f"Unsupported binary operator: {type(node.op).__name__}")

        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name)
            fn_name = node.func.id

            if fn_name == "where":
                assert isinstance(node.args[0], ast.Compare)
                mask = _eval_compare_vectorized(
                    node.args[0], env, length, deadline=deadline
                )
                out = np.empty(length, dtype=np.float64)
                if bool(np.any(mask)):
                    true_env = _slice_vector_env(env, mask)
                    true_len = int(np.count_nonzero(mask))
                    out[mask] = _eval_vectorized(
                        node.args[1], true_env, true_len, deadline=deadline
                    )
                inv_mask = ~mask
                if bool(np.any(inv_mask)):
                    false_env = _slice_vector_env(env, inv_mask)
                    false_len = int(np.count_nonzero(inv_mask))
                    out[inv_mask] = _eval_vectorized(
                        node.args[2], false_env, false_len, deadline=deadline
                    )
                return _ensure_finite_array(out, "where()")

            if fn_name in _VECTOR_UNARY_FUNCS:
                arg = _eval_vectorized(node.args[0], env, length, deadline=deadline)
                res = np.asarray(_VECTOR_UNARY_FUNCS[fn_name](arg), dtype=np.float64)
                return _ensure_finite_array(res, f"{fn_name}()")

            if fn_name == "heaviside":
                x_arr = _eval_vectorized(node.args[0], env, length, deadline=deadline)
                h0_arr = (
                    _eval_vectorized(node.args[1], env, length, deadline=deadline)
                    if len(node.args) == 2
                    else np.full(length, 0.5, dtype=np.float64)
                )
                res = np.asarray(np.heaviside(x_arr, h0_arr), dtype=np.float64)
                return _ensure_finite_array(res, "heaviside()")

            if fn_name == "atan2":
                y_arr = _eval_vectorized(node.args[0], env, length, deadline=deadline)
                x_arr = _eval_vectorized(node.args[1], env, length, deadline=deadline)
                res = np.asarray(np.arctan2(y_arr, x_arr), dtype=np.float64)
                return _ensure_finite_array(res, "atan2()")

            if fn_name == "min":
                acc = _eval_vectorized(node.args[0], env, length, deadline=deadline)
                for arg in node.args[1:]:
                    acc = np.minimum(
                        acc, _eval_vectorized(arg, env, length, deadline=deadline)
                    )
                return _ensure_finite_array(np.asarray(acc, dtype=np.float64), "min()")

            if fn_name == "max":
                acc = _eval_vectorized(node.args[0], env, length, deadline=deadline)
                for arg in node.args[1:]:
                    acc = np.maximum(
                        acc, _eval_vectorized(arg, env, length, deadline=deadline)
                    )
                return _ensure_finite_array(np.asarray(acc, dtype=np.float64), "max()")

            if fn_name == "clip":
                x_arr = _eval_vectorized(node.args[0], env, length, deadline=deadline)
                lo_arr = _eval_vectorized(node.args[1], env, length, deadline=deadline)
                hi_arr = _eval_vectorized(node.args[2], env, length, deadline=deadline)
                if bool(np.any(lo_arr > hi_arr)):
                    raise ComputeSimError("clip(x, lo, hi) requires lo <= hi at every point")
                res = np.asarray(np.clip(x_arr, lo_arr, hi_arr), dtype=np.float64)
                return _ensure_finite_array(res, "clip()")

            raise ComputeSimError(f"Unsupported function in vectorized evaluation: {fn_name!r}")

        raise ComputeSimError(
            f"Disallowed AST node in vectorized evaluation: {type(node).__name__}"
        )


# ---------------------------------------------------------------------------
# Computational Modes
# ---------------------------------------------------------------------------


def _run_ode_ivp(
    spec: ComputeSimSpec,
    *,
    start_perf: float,
    deadline: float,
) -> tuple[list[dict[str, Any]], list[str], dict[str, str], dict[str, Any]]:
    assert spec.grid_start is not None and spec.grid_stop is not None
    state_vars = list(spec.state_variables) if spec.state_variables else ["y"]
    expr_strings = [spec.expression] if spec.expression is not None else list(spec.expressions)

    allowed_names = {spec.grid_variable, *state_vars, *spec.parameters.keys()}
    compiled_exprs = [
        _validate_compute_ast(expr, allowed_names).body for expr in expr_strings
    ]

    t_eval = _make_finite_grid(
        float(spec.grid_start),
        float(spec.grid_stop),
        spec.grid_points,
        "ode_ivp grid",
    )
    y0 = np.asarray(spec.initial_state, dtype=np.float64)
    grid_var = spec.grid_variable
    env: dict[str, float] = {k: float(v) for k, v in spec.parameters.items()}
    rhs_calls = 0

    def rhs(t: float, y: np.ndarray) -> list[float]:
        nonlocal rhs_calls
        rhs_calls += 1
        if time.perf_counter() > deadline:
            raise ComputeSimTimeoutError(
                f"ODE integration exceeded timeout_s={spec.timeout_s:g} "
                f"after {rhs_calls} RHS evaluations"
            )
        if rhs_calls > spec.max_nfev:
            raise ComputeSimLimitError(
                f"ODE integration exceeded max_nfev={spec.max_nfev} RHS evaluations"
            )
        tf = float(t)
        if not math.isfinite(tf):
            raise ComputeSimError(f"ODE solver encountered non-finite {grid_var}={t!r}")
        env[grid_var] = tf
        for idx, sv in enumerate(state_vars):
            val = float(y[idx])
            if not math.isfinite(val):
                raise ComputeSimError(
                    f"ODE solver diverged with non-finite state {sv}={val!r} "
                    f"at {grid_var}={tf:g}"
                )
            env[sv] = val
        return [_eval_scalar(expr_node, env) for expr_node in compiled_exprs]

    try:
        sol = solve_ivp(
            rhs,
            (float(spec.grid_start), float(spec.grid_stop)),
            y0,
            method=spec.ode_method,
            t_eval=t_eval,
            rtol=spec.rtol,
            atol=spec.atol,
        )
    except ComputeSimError:
        raise
    except Exception as exc:
        raise ComputeSimError(f"ODE solver ({spec.ode_method}) failed: {exc}") from exc

    if not sol.success:
        raise ComputeSimError(
            f"ODE solver ({spec.ode_method}) failed to converge "
            f"(status={sol.status}): {sol.message}"
        )
    if sol.y.shape != (len(state_vars), len(t_eval)):
        raise ComputeSimError(
            f"ODE solver returned shape {sol.y.shape}; "
            f"expected {(len(state_vars), len(t_eval))}"
        )
    if not np.all(np.isfinite(sol.y)) or not np.all(np.isfinite(sol.t)):
        raise ComputeSimError("ODE solver returned non-finite solution values")

    columns = [grid_var, *state_vars]
    rows: list[dict[str, Any]] = []
    for i in range(len(t_eval)):
        if (i & 4095) == 0 and time.perf_counter() > deadline:
            raise ComputeSimTimeoutError(
                f"ODE result assembly exceeded timeout_s={spec.timeout_s:g}"
            )
        row: dict[str, Any] = {grid_var: float(t_eval[i])}
        for j, sv in enumerate(state_vars):
            row[sv] = float(sol.y[j, i])
        rows.append(row)

    elapsed_ms = round((time.perf_counter() - start_perf) * 1000.0, 3)
    solver_meta: dict[str, Any] = {
        "method": spec.ode_method,
        "nfev": int(sol.nfev),
        "njev": int(sol.njev),
        "nlu": int(sol.nlu),
        "rhs_calls": rhs_calls,
        "status": int(sol.status),
        "message": str(sol.message),
        "rtol": float(spec.rtol),
        "atol": float(spec.atol),
        "elapsed_ms": elapsed_ms,
    }
    return rows, columns, dict(spec.units), solver_meta


def _run_algebraic_grid(
    spec: ComputeSimSpec,
    *,
    start_perf: float,
    deadline: float,
) -> tuple[list[dict[str, Any]], list[str], dict[str, str], dict[str, Any]]:
    assert spec.grid_start is not None and spec.grid_stop is not None
    if spec.expression is not None:
        expr_strings = [spec.expression]
        out_cols = [spec.state_variables[0]] if spec.state_variables else ["y"]
    else:
        expr_strings = list(spec.expressions)
        out_cols = list(spec.state_variables)

    allowed_names = {spec.grid_variable, *spec.parameters.keys()}
    compiled_exprs = [
        _validate_compute_ast(expr, allowed_names).body for expr in expr_strings
    ]

    grid = _make_finite_grid(
        float(spec.grid_start),
        float(spec.grid_stop),
        spec.grid_points,
        "algebraic_grid grid",
    )
    env: dict[str, float | np.ndarray] = {k: float(v) for k, v in spec.parameters.items()}
    env[spec.grid_variable] = grid

    evaluated_cols: list[np.ndarray] = []
    for expr_node in compiled_exprs:
        if time.perf_counter() > deadline:
            raise ComputeSimTimeoutError(
                f"algebraic_grid evaluation exceeded timeout_s={spec.timeout_s:g}"
            )
        arr = _eval_vectorized(expr_node, env, spec.grid_points, deadline=deadline)
        if arr.shape != (spec.grid_points,):
            raise ComputeSimError(
                f"algebraic_grid expression returned shape {arr.shape}; "
                f"expected {(spec.grid_points,)}"
            )
        _ensure_finite_array(arr, "algebraic_grid")
        evaluated_cols.append(arr)

    if time.perf_counter() > deadline:
        raise ComputeSimTimeoutError(
            f"algebraic_grid evaluation exceeded timeout_s={spec.timeout_s:g}"
        )

    columns = [spec.grid_variable, *out_cols]
    rows: list[dict[str, Any]] = []
    for i in range(spec.grid_points):
        if (i & 4095) == 0 and time.perf_counter() > deadline:
            raise ComputeSimTimeoutError(
                f"algebraic_grid assembly exceeded timeout_s={spec.timeout_s:g}"
            )
        row: dict[str, Any] = {spec.grid_variable: float(grid[i])}
        for col_name, arr in zip(out_cols, evaluated_cols, strict=True):
            row[col_name] = float(arr[i])
        rows.append(row)

    elapsed_ms = round((time.perf_counter() - start_perf) * 1000.0, 3)
    solver_meta: dict[str, Any] = {
        "grid_points": int(spec.grid_points),
        "elapsed_ms": elapsed_ms,
    }
    return rows, columns, dict(spec.units), solver_meta


def _extract_finite_numeric(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComputeSimError(f"Expected numeric value for {context}; got {value!r}")
    out = float(value)
    if not math.isfinite(out):
        raise ComputeSimError(f"Non-finite numeric value for {context}: {value!r}")
    return out


def _format_power_unit(signal_unit: str) -> str:
    if any(ch in signal_unit for ch in " /*^"):
        return f"({signal_unit})^2"
    return f"{signal_unit}^2"


def _run_fft_spectrum(
    spec: ComputeSimSpec,
    run_workspace: RunWorkspace,
    *,
    start_perf: float,
    deadline: float,
) -> tuple[list[dict[str, Any]], list[str], dict[str, str], dict[str, Any]]:
    input_signal_unit: str | None = None

    if spec.input_dataset_id is not None:
        assert spec.time_column is not None and spec.signal_column is not None
        dataset = load_dataset(spec.input_dataset_id, run_workspace)
        time_col = spec.time_column.strip()
        signal_col = spec.signal_column.strip()

        available_cols: set[str] = set()
        for row in dataset.rows:
            available_cols.update(row.keys())
        if time_col not in available_cols:
            raise ComputeSimError(
                f"Time column {time_col!r} not found in dataset {spec.input_dataset_id!r}"
            )
        if signal_col not in available_cols:
            raise ComputeSimError(
                f"Signal column {signal_col!r} not found in dataset {spec.input_dataset_id!r}"
            )

        raw_unit = dataset.units.get(signal_col)
        if isinstance(raw_unit, str) and raw_unit.strip():
            input_signal_unit = raw_unit.strip()

        t_vals: list[float] = []
        y_vals: list[float] = []
        dropped_missing_rows = 0
        for idx, row in enumerate(dataset.rows):
            if (idx & 4095) == 0 and time.perf_counter() > deadline:
                raise ComputeSimTimeoutError(
                    f"fft_spectrum exceeded timeout_s={spec.timeout_s:g}"
                )
            raw_t = row.get(time_col)
            if raw_t is None:
                raise ComputeSimError(
                    f"Missing time value in column {time_col!r} at row {idx} "
                    f"of dataset {spec.input_dataset_id!r}"
                )
            t_num = _extract_finite_numeric(raw_t, f"row[{idx}].{time_col}")

            raw_y = row.get(signal_col)
            if raw_y is None:
                dropped_missing_rows += 1
                continue
            y_num = _extract_finite_numeric(raw_y, f"row[{idx}].{signal_col}")

            t_vals.append(t_num)
            y_vals.append(y_num)

        if len(t_vals) < 4:
            raise ComputeSimError(
                f"fft_spectrum requires at least 4 valid samples; got {len(t_vals)} "
                f"(dropped_missing_rows={dropped_missing_rows})"
            )

        with np.errstate(all="ignore"):
            t_arr = np.asarray(t_vals, dtype=np.float64)
            y_arr = np.asarray(y_vals, dtype=np.float64)
            dts = np.diff(t_arr)
            if not bool(np.all(np.isfinite(dts))) or not bool(np.all(dts > 0.0)):
                raise ComputeSimError(
                    f"Timestamps in column {time_col!r} must be strictly increasing "
                    "and have finite spacing for fft_spectrum"
                )

            n = len(t_arr)
            dt_mean = float(np.mean(dts))
            if not math.isfinite(dt_mean) or dt_mean <= 0.0:
                raise ComputeSimError(f"Invalid mean sample spacing dt={dt_mean!r}")

            jitter_ratio = float(np.max(np.abs(dts - dt_mean)) / dt_mean)
            if not math.isfinite(jitter_ratio):
                raise ComputeSimError(
                    f"Non-finite timestamp jitter_ratio={jitter_ratio!r} in fft_spectrum"
                )
            if jitter_ratio > _FFT_JITTER_THRESHOLD:
                t_uniform = _make_finite_grid(
                    float(t_arr[0]),
                    float(t_arr[-1]),
                    n,
                    "fft_spectrum resampled grid",
                )
                y_arr = np.asarray(np.interp(t_uniform, t_arr, y_arr), dtype=np.float64)
                _ensure_finite_array(y_arr, "fft_spectrum resampled signal")
                resampled = True
                dt_s = float((t_arr[-1] - t_arr[0]) / (n - 1))
            else:
                resampled = False
                dt_s = dt_mean
    else:
        assert (
            spec.expression is not None
            and spec.grid_start is not None
            and spec.grid_stop is not None
        )
        n = spec.grid_points
        if n < 4:
            raise ComputeSimError(f"Inline fft_spectrum requires grid_points >= 4; got {n}")
        allowed_names = {spec.grid_variable, *spec.parameters.keys()}
        compiled_expr = _validate_compute_ast(spec.expression, allowed_names).body
        grid = _make_finite_grid(
            float(spec.grid_start),
            float(spec.grid_stop),
            n,
            "fft_spectrum inline grid",
        )
        env: dict[str, float | np.ndarray] = {k: float(v) for k, v in spec.parameters.items()}
        env[spec.grid_variable] = grid
        y_arr = _eval_vectorized(compiled_expr, env, n, deadline=deadline)
        dt_s = float((float(spec.grid_stop) - float(spec.grid_start)) / (n - 1))
        resampled = False
        jitter_ratio = 0.0
        dropped_missing_rows = 0

    if time.perf_counter() > deadline:
        raise ComputeSimTimeoutError(
            f"fft_spectrum exceeded timeout_s={spec.timeout_s:g}"
        )

    if not math.isfinite(dt_s) or dt_s <= 0.0:
        raise ComputeSimError(f"Invalid sample interval dt_s={dt_s!r} for fft_spectrum")

    with np.errstate(all="ignore"):
        if spec.detrend_mean:
            mean_val = float(np.mean(y_arr))
            if not math.isfinite(mean_val):
                raise ComputeSimError(
                    f"Non-finite signal mean ({mean_val!r}) during fft_spectrum detrending"
                )
            y_work = y_arr - mean_val
        else:
            y_work = y_arr.copy()
        _ensure_finite_array(y_work, "fft signal")

        if spec.window == "hann":
            win = np.hanning(n)
            win_mean = float(np.mean(win))
            if not math.isfinite(win_mean) or win_mean <= 0.0:
                raise ComputeSimError("Invalid Hann window normalization factor")
            gain = 1.0 / win_mean
            y_work = y_work * win * gain
            _ensure_finite_array(y_work, "fft windowed signal")

        freqs = np.asarray(rfftfreq(n, d=dt_s), dtype=np.float64)
        spec_c = np.asarray(rfft(y_work), dtype=np.complex128)

        amplitude = np.abs(spec_c) * (2.0 / n)
        amplitude[0] *= 0.5
        if n % 2 == 0:
            amplitude[-1] *= 0.5
        power = amplitude * amplitude
        phase_rad = np.angle(spec_c)
        omega_rad_s = (2.0 * math.pi) * freqs
        nyquist_hz = float(0.5 / dt_s)

    _ensure_finite_array(freqs, "fft frequencies")
    _ensure_finite_array(omega_rad_s, "fft angular frequencies")
    _ensure_finite_array(amplitude, "fft amplitude")
    _ensure_finite_array(power, "fft power")
    _ensure_finite_array(phase_rad, "fft phase")
    if not math.isfinite(nyquist_hz):
        raise ComputeSimError(f"Non-finite Nyquist frequency ({nyquist_hz!r}) for dt_s={dt_s!r}")

    if time.perf_counter() > deadline:
        raise ComputeSimTimeoutError(
            f"fft_spectrum exceeded timeout_s={spec.timeout_s:g}"
        )

    if len(freqs) > 1:
        dom_idx = int(np.argmax(amplitude[1:])) + 1
    else:
        dom_idx = 0
    dominant_frequency_hz = float(freqs[dom_idx])
    dominant_omega_rad_s = float(omega_rad_s[dom_idx])
    dominant_amplitude = float(amplitude[dom_idx])

    columns = ["frequency_hz", "omega_rad_s", "amplitude", "power", "phase_rad"]
    rows: list[dict[str, Any]] = []
    for i in range(len(freqs)):
        if (i & 4095) == 0 and time.perf_counter() > deadline:
            raise ComputeSimTimeoutError(
                f"fft_spectrum assembly exceeded timeout_s={spec.timeout_s:g}"
            )
        rows.append(
            {
                "frequency_hz": float(freqs[i]),
                "omega_rad_s": float(omega_rad_s[i]),
                "amplitude": float(amplitude[i]),
                "power": float(power[i]),
                "phase_rad": float(phase_rad[i]),
            }
        )

    units: dict[str, str] = {
        "frequency_hz": "Hz",
        "omega_rad_s": "rad/s",
        "phase_rad": "rad",
    }
    if input_signal_unit:
        units["amplitude"] = input_signal_unit
        units["power"] = _format_power_unit(input_signal_unit)
    units.update(spec.units)

    elapsed_ms = round((time.perf_counter() - start_perf) * 1000.0, 3)
    solver_meta: dict[str, Any] = {
        "dominant_frequency_hz": dominant_frequency_hz,
        "dominant_omega_rad_s": dominant_omega_rad_s,
        "dominant_amplitude": dominant_amplitude,
        "nyquist_hz": nyquist_hz,
        "dt_s": dt_s,
        "n_samples": int(n),
        "window": spec.window,
        "detrend_mean": bool(spec.detrend_mean),
        "resampled": resampled,
        "jitter_ratio": jitter_ratio,
        "dropped_missing_rows": dropped_missing_rows,
        "input_dataset_id": spec.input_dataset_id,
        "elapsed_ms": elapsed_ms,
    }
    return rows, columns, units, solver_meta


# ---------------------------------------------------------------------------
# Dataset Persistence & Public Entrypoint
# ---------------------------------------------------------------------------


def _persist_compute_dataset(
    *,
    model_kind: ComputeModelKind,
    requested_dataset_id: str | None,
    rows: list[dict[str, Any]],
    units: dict[str, str],
    notes: str,
    run_workspace: RunWorkspace,
) -> tuple[str, str]:
    if requested_dataset_id is not None:
        ingested = data_ingest(
            DatasetSpec(
                dataset_id=requested_dataset_id,
                source="model_computed",
                rows=rows,
                units=units,
                notes=notes,
            ),
            run_workspace,
            source="model_computed",
        )
        assert ingested.dataset_id is not None
        return ingested.dataset_id, f"datasets/{ingested.dataset_id}.json"

    pattern = re.compile(rf"^compute-{re.escape(model_kind)}-(\d+)\.json$")
    datasets_dir = run_workspace.resolve_path("datasets")
    seq = 0
    if datasets_dir.is_dir():
        for item in datasets_dir.iterdir():
            match = pattern.match(item.name)
            if match is not None:
                seq = max(seq, int(match.group(1)))

    while True:
        seq += 1
        candidate_id = f"compute-{model_kind}-{seq:04d}"
        if run_workspace.resolve_path(f"datasets/{candidate_id}.json").exists():
            continue
        try:
            ingested = data_ingest(
                DatasetSpec(
                    dataset_id=candidate_id,
                    source="model_computed",
                    rows=rows,
                    units=units,
                    notes=notes,
                ),
                run_workspace,
                source="model_computed",
            )
            assert ingested.dataset_id is not None
            return ingested.dataset_id, f"datasets/{ingested.dataset_id}.json"
        except FileExistsError:
            continue


def run_compute_sim(
    spec: ComputeSimSpec | dict[str, Any],
    *,
    run_workspace: RunWorkspace,
) -> ComputeSimResult:
    """Execute a sandboxed ``compute_sim`` model and persist it as a ``model_computed`` dataset."""
    validated = (
        spec if isinstance(spec, ComputeSimSpec) else ComputeSimSpec.model_validate(spec)
    )

    if validated.dataset_id is not None:
        existing_path = run_workspace.resolve_path(f"datasets/{validated.dataset_id}.json")
        if existing_path.exists():
            raise FileExistsError(
                f"Dataset {validated.dataset_id!r} already exists in run {run_workspace.run_id}; "
                "use a unique dataset_id to preserve provenance immutability"
            )

    start_perf = time.perf_counter()
    deadline = start_perf + validated.timeout_s

    if validated.model_kind == "ode_ivp":
        rows, columns, units, solver_meta = _run_ode_ivp(
            validated,
            start_perf=start_perf,
            deadline=deadline,
        )
    elif validated.model_kind == "algebraic_grid":
        rows, columns, units, solver_meta = _run_algebraic_grid(
            validated,
            start_perf=start_perf,
            deadline=deadline,
        )
    elif validated.model_kind == "fft_spectrum":
        rows, columns, units, solver_meta = _run_fft_spectrum(
            validated,
            run_workspace,
            start_perf=start_perf,
            deadline=deadline,
        )
    else:  # pragma: no cover - guarded by ComputeModelKind Literal
        raise ComputeSimError(f"Unsupported model_kind: {validated.model_kind!r}")

    notes = (
        validated.notes.strip()
        if validated.notes.strip()
        else f"compute_sim ({validated.model_kind}, rows={len(rows)})"
    )
    dataset_id, dataset_relpath = _persist_compute_dataset(
        model_kind=validated.model_kind,
        requested_dataset_id=validated.dataset_id,
        rows=rows,
        units=units,
        notes=notes,
        run_workspace=run_workspace,
    )
    dataset_sha256, _ = sha256_file(run_workspace.resolve_path(dataset_relpath))
    preview_rows = (
        rows if len(rows) <= _MAX_PREVIEW_ROWS else rows[:5] + rows[-5:]
    )

    return ComputeSimResult(
        model_kind=validated.model_kind,
        provenance="model_computed",
        dataset_id=dataset_id,
        dataset_relpath=dataset_relpath,
        dataset_sha256=dataset_sha256,
        row_count=len(rows),
        columns=columns,
        units=units,
        solver_meta=solver_meta,
        preview_rows=preview_rows,
    )


__all__ = [
    "ComputeSimError",
    "ComputeSimLimitError",
    "ComputeSimTimeoutError",
    "run_compute_sim",
]
