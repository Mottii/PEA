"""Safe 5-dim SI dimensional physics calculator with AST evaluation and uncertainty propagation.

Supports scalars, 3D vectors, complex AC impedance, mechanics + electromagnetism/AC units,
first-order correlated Gaussian uncertainty propagation via forward-mode gradients, and an
append-only ``calculations.jsonl`` ledger with immutable ``calc_id`` values.

Strictly zero ``eval()`` or ``exec()`` calls.
"""

from __future__ import annotations

import ast
import cmath
import math
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

from pea.schemas import CalcRequest, CalcResult, CalcVariable

_LEDGER_LOCK = threading.Lock()

if TYPE_CHECKING:
    from pea.workspace import RunWorkspace

Dims = tuple[int, int, int, int, int]  # (kg, m, s, A, K)
DIMLESS: Dims = (0, 0, 0, 0, 0)

_MAX_EXPRESSION_LEN = 2000
_MAX_AST_DEPTH = 64
_MAX_EXPONENT_ABS = 1000.0
_EPS = 1e-12


class CalcError(ValueError):
    """Base error raised for invalid physics calculations or unsafe expressions."""


class DimensionError(CalcError):
    """Raised when an operation violates SI dimensional homogeneity."""


class UnitParseError(CalcError):
    """Raised when a unit expression cannot be parsed safely."""


class UnsafeExpressionError(CalcError):
    """Raised when an expression contains forbidden syntax or identifiers."""


# ---------------------------------------------------------------------------
# 5-dim SI Unit Table & Compound Unit Parser
# ---------------------------------------------------------------------------

# Mapping: unit symbol -> (scale_to_si, (kg, m, s, A, K))
UNIT_TABLE: dict[str, tuple[float, Dims]] = {
    # Dimensionless & angles
    "1": (1.0, DIMLESS),
    "rad": (1.0, DIMLESS),
    "deg": (math.pi / 180.0, DIMLESS),
    "рад": (1.0, DIMLESS),
    "град": (math.pi / 180.0, DIMLESS),
    # Mass (kg)
    "kg": (1.0, (1, 0, 0, 0, 0)),
    "g": (1e-3, (1, 0, 0, 0, 0)),
    "mg": (1e-6, (1, 0, 0, 0, 0)),
    "ug": (1e-9, (1, 0, 0, 0, 0)),
    "t": (1e3, (1, 0, 0, 0, 0)),
    "кг": (1.0, (1, 0, 0, 0, 0)),
    "г": (1e-3, (1, 0, 0, 0, 0)),
    # Length (m)
    "m": (1.0, (0, 1, 0, 0, 0)),
    "km": (1e3, (0, 1, 0, 0, 0)),
    "dm": (1e-1, (0, 1, 0, 0, 0)),
    "cm": (1e-2, (0, 1, 0, 0, 0)),
    "mm": (1e-3, (0, 1, 0, 0, 0)),
    "um": (1e-6, (0, 1, 0, 0, 0)),
    "nm": (1e-9, (0, 1, 0, 0, 0)),
    "pm": (1e-12, (0, 1, 0, 0, 0)),
    "м": (1.0, (0, 1, 0, 0, 0)),
    "см": (1e-2, (0, 1, 0, 0, 0)),
    "мм": (1e-3, (0, 1, 0, 0, 0)),
    "мкм": (1e-6, (0, 1, 0, 0, 0)),
    "нм": (1e-9, (0, 1, 0, 0, 0)),
    # Time (s)
    "s": (1.0, (0, 0, 1, 0, 0)),
    "ms": (1e-3, (0, 0, 1, 0, 0)),
    "us": (1e-6, (0, 0, 1, 0, 0)),
    "ns": (1e-9, (0, 0, 1, 0, 0)),
    "ps": (1e-12, (0, 0, 1, 0, 0)),
    "min": (60.0, (0, 0, 1, 0, 0)),
    "h": (3600.0, (0, 0, 1, 0, 0)),
    "hr": (3600.0, (0, 0, 1, 0, 0)),
    "с": (1.0, (0, 0, 1, 0, 0)),
    "мс": (1e-3, (0, 0, 1, 0, 0)),
    "мкс": (1e-6, (0, 0, 1, 0, 0)),
    # Frequency (s^-1)
    "Hz": (1.0, (0, 0, -1, 0, 0)),
    "kHz": (1e3, (0, 0, -1, 0, 0)),
    "MHz": (1e6, (0, 0, -1, 0, 0)),
    "GHz": (1e9, (0, 0, -1, 0, 0)),
    "Гц": (1.0, (0, 0, -1, 0, 0)),
    "кГц": (1e3, (0, 0, -1, 0, 0)),
    "МГц": (1e6, (0, 0, -1, 0, 0)),
    # Force (N = kg*m/s^2)
    "N": (1.0, (1, 1, -2, 0, 0)),
    "mN": (1e-3, (1, 1, -2, 0, 0)),
    "kN": (1e3, (1, 1, -2, 0, 0)),
    "Н": (1.0, (1, 1, -2, 0, 0)),
    # Energy (J = kg*m^2/s^2)
    "J": (1.0, (1, 2, -2, 0, 0)),
    "mJ": (1e-3, (1, 2, -2, 0, 0)),
    "kJ": (1e3, (1, 2, -2, 0, 0)),
    "MJ": (1e6, (1, 2, -2, 0, 0)),
    "Дж": (1.0, (1, 2, -2, 0, 0)),
    "eV": (1.602176634e-19, (1, 2, -2, 0, 0)),
    "keV": (1.602176634e-16, (1, 2, -2, 0, 0)),
    "MeV": (1.602176634e-13, (1, 2, -2, 0, 0)),
    # Power (W = kg*m^2/s^3)
    "W": (1.0, (1, 2, -3, 0, 0)),
    "mW": (1e-3, (1, 2, -3, 0, 0)),
    "uW": (1e-6, (1, 2, -3, 0, 0)),
    "kW": (1e3, (1, 2, -3, 0, 0)),
    "MW": (1e6, (1, 2, -3, 0, 0)),
    "Вт": (1.0, (1, 2, -3, 0, 0)),
    # Pressure (Pa = kg/(m*s^2))
    "Pa": (1.0, (1, -1, -2, 0, 0)),
    "kPa": (1e3, (1, -1, -2, 0, 0)),
    "MPa": (1e6, (1, -1, -2, 0, 0)),
    "Па": (1.0, (1, -1, -2, 0, 0)),
    "кПа": (1e3, (1, -1, -2, 0, 0)),
    "МПа": (1e6, (1, -1, -2, 0, 0)),
    "bar": (1e5, (1, -1, -2, 0, 0)),
    "atm": (101325.0, (1, -1, -2, 0, 0)),
    # Temperature (K)
    "K": (1.0, (0, 0, 0, 0, 1)),
    "К": (1.0, (0, 0, 0, 0, 1)),
    # Electric Current (A)
    "A": (1.0, (0, 0, 0, 1, 0)),
    "mA": (1e-3, (0, 0, 0, 1, 0)),
    "uA": (1e-6, (0, 0, 0, 1, 0)),
    "nA": (1e-9, (0, 0, 0, 1, 0)),
    "pA": (1e-12, (0, 0, 0, 1, 0)),
    "kA": (1e3, (0, 0, 0, 1, 0)),
    "А": (1.0, (0, 0, 0, 1, 0)),
    "мА": (1e-3, (0, 0, 0, 1, 0)),
    "мкА": (1e-6, (0, 0, 0, 1, 0)),
    "кА": (1e3, (0, 0, 0, 1, 0)),
    # Electric Charge (C = A*s)
    "C": (1.0, (0, 0, 1, 1, 0)),
    "mC": (1e-3, (0, 0, 1, 1, 0)),
    "uC": (1e-6, (0, 0, 1, 1, 0)),
    "nC": (1e-9, (0, 0, 1, 1, 0)),
    "pC": (1e-12, (0, 0, 1, 1, 0)),
    "Кл": (1.0, (0, 0, 1, 1, 0)),
    # Voltage (V = kg*m^2/(s^3*A))
    "V": (1.0, (1, 2, -3, -1, 0)),
    "mV": (1e-3, (1, 2, -3, -1, 0)),
    "uV": (1e-6, (1, 2, -3, -1, 0)),
    "kV": (1e3, (1, 2, -3, -1, 0)),
    "MV": (1e6, (1, 2, -3, -1, 0)),
    "В": (1.0, (1, 2, -3, -1, 0)),
    "мВ": (1e-3, (1, 2, -3, -1, 0)),
    "мкВ": (1e-6, (1, 2, -3, -1, 0)),
    "кВ": (1e3, (1, 2, -3, -1, 0)),
    # Resistance / Impedance (Ohm = kg*m^2/(s^3*A^2))
    "Ohm": (1.0, (1, 2, -3, -2, 0)),
    "ohm": (1.0, (1, 2, -3, -2, 0)),
    "uOhm": (1e-6, (1, 2, -3, -2, 0)),
    "mOhm": (1e-3, (1, 2, -3, -2, 0)),
    "kOhm": (1e3, (1, 2, -3, -2, 0)),
    "MOhm": (1e6, (1, 2, -3, -2, 0)),
    "GOhm": (1e9, (1, 2, -3, -2, 0)),
    "Ом": (1.0, (1, 2, -3, -2, 0)),
    "кОм": (1e3, (1, 2, -3, -2, 0)),
    "МОм": (1e6, (1, 2, -3, -2, 0)),
    # Conductance (S = 1/Ohm)
    "S": (1.0, (-1, -2, 3, 2, 0)),
    "mS": (1e-3, (-1, -2, 3, 2, 0)),
    "uS": (1e-6, (-1, -2, 3, 2, 0)),
    # Capacitance (F = s^4*A^2/(kg*m^2))
    "F": (1.0, (-1, -2, 4, 2, 0)),
    "mF": (1e-3, (-1, -2, 4, 2, 0)),
    "uF": (1e-6, (-1, -2, 4, 2, 0)),
    "nF": (1e-9, (-1, -2, 4, 2, 0)),
    "pF": (1e-12, (-1, -2, 4, 2, 0)),
    "fF": (1e-15, (-1, -2, 4, 2, 0)),
    "Ф": (1.0, (-1, -2, 4, 2, 0)),
    "мкФ": (1e-6, (-1, -2, 4, 2, 0)),
    "нФ": (1e-9, (-1, -2, 4, 2, 0)),
    "пФ": (1e-12, (-1, -2, 4, 2, 0)),
    # Inductance (H = kg*m^2/(s^2*A^2))
    "H": (1.0, (1, 2, -2, -2, 0)),
    "mH": (1e-3, (1, 2, -2, -2, 0)),
    "uH": (1e-6, (1, 2, -2, -2, 0)),
    "nH": (1e-9, (1, 2, -2, -2, 0)),
    "Гн": (1.0, (1, 2, -2, -2, 0)),
    "мГн": (1e-3, (1, 2, -2, -2, 0)),
    "мкГн": (1e-6, (1, 2, -2, -2, 0)),
    # Magnetic Flux Density (T = kg/(s^2*A))
    "T": (1.0, (1, 0, -2, -1, 0)),
    "mT": (1e-3, (1, 0, -2, -1, 0)),
    "uT": (1e-6, (1, 0, -2, -1, 0)),
    "nT": (1e-9, (1, 0, -2, -1, 0)),
    "Тл": (1.0, (1, 0, -2, -1, 0)),
    "мТл": (1e-3, (1, 0, -2, -1, 0)),
    "мкТл": (1e-6, (1, 0, -2, -1, 0)),
    # Magnetic Flux (Wb = kg*m^2/(s^2*A))
    "Wb": (1.0, (1, 2, -2, -1, 0)),
    "mWb": (1e-3, (1, 2, -2, -1, 0)),
    "uWb": (1e-6, (1, 2, -2, -1, 0)),
    "Вб": (1.0, (1, 2, -2, -1, 0)),
    "мВб": (1e-3, (1, 2, -2, -1, 0)),
}

_CANONICAL_SI_NAMES: dict[Dims, str] = {
    DIMLESS: "1",
    (1, 0, 0, 0, 0): "kg",
    (0, 1, 0, 0, 0): "m",
    (0, 0, 1, 0, 0): "s",
    (0, 0, 0, 1, 0): "A",
    (0, 0, 0, 0, 1): "K",
    (0, 0, -1, 0, 0): "s^-1",
    (0, 1, -1, 0, 0): "m/s",
    (0, 1, -2, 0, 0): "m/s^2",
    (1, 1, -2, 0, 0): "N",
    (1, 2, -2, 0, 0): "J",
    (1, 2, -3, 0, 0): "W",
    (1, -1, -2, 0, 0): "Pa",
    (0, 0, 1, 1, 0): "C",
    (1, 2, -3, -1, 0): "V",
    (1, 2, -3, -2, 0): "Ohm",
    (-1, -2, 4, 2, 0): "F",
    (1, 2, -2, -2, 0): "H",
    (1, 0, -2, -1, 0): "T",
    (1, 2, -2, -1, 0): "Wb",
    (1, 1, -2, -2, 0): "T*m/A",
    (-1, -3, 4, 2, 0): "F/m",
}

_SI_BASE_SYMBOLS: tuple[str, ...] = ("kg", "m", "s", "A", "K")

_SUPERSCRIPT_MAP = str.maketrans(
    {
        "⁰": "0",
        "¹": "1",
        "²": "2",
        "³": "3",
        "⁴": "4",
        "⁵": "5",
        "⁶": "6",
        "⁷": "7",
        "⁸": "8",
        "⁹": "9",
        "⁻": "-",
        "⁺": "+",
    }
)
_SUPERSCRIPT_RUN_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+")


def dims_mul(a: Dims, b: Dims) -> Dims:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3], a[4] + b[4])


def dims_div(a: Dims, b: Dims) -> Dims:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2], a[3] - b[3], a[4] - b[4])


def dims_pow(a: Dims, exponent: float) -> Dims:
    if a == DIMLESS:
        return DIMLESS
    scaled: list[int] = []
    for d in a:
        raw = d * exponent
        rounded = round(raw)
        if abs(raw - rounded) > 1e-9:
            raise DimensionError(
                f"Non-integer SI dimension exponent ({raw:g}) raising dims {a} to {exponent:g}"
            )
        scaled.append(int(rounded))
    return (scaled[0], scaled[1], scaled[2], scaled[3], scaled[4])


def format_si_dims(dims: Dims) -> str:
    """Format a 5-dim SI tuple as a canonical parseable unit string."""
    if dims in _CANONICAL_SI_NAMES:
        return _CANONICAL_SI_NAMES[dims]
    parts: list[str] = []
    for sym, exp in zip(_SI_BASE_SYMBOLS, dims, strict=True):
        if exp == 0:
            continue
        if exp == 1:
            parts.append(sym)
        else:
            parts.append(f"{sym}^{exp}")
    return "*".join(parts) if parts else "1"


def _normalize_unit_expr(unit: str) -> str:
    text = unit.strip()
    if not text:
        return "1"
    # Convert Unicode superscript runs (e.g. s⁻¹ -> s**-1, m⁻³ -> m**-3)
    text = _SUPERSCRIPT_RUN_RE.sub(lambda m: f"**({m.group(0).translate(_SUPERSCRIPT_MAP)})", text)
    text = (
        text.replace("\u00b5", "u")  # MICRO SIGN µ
        .replace("\u03bc", "u")  # GREEK SMALL LETTER MU μ
        .replace("\u03a9", "Ohm")  # GREEK CAPITAL LETTER OMEGA Ω
        .replace("\u2126", "Ohm")  # OHM SIGN Ω
        .replace("·", "*")
        .replace("⋅", "*")
        .replace("×", "*")
        .replace("−", "-")
        .replace("^", "**")
    )
    return text


def _eval_unit_exponent(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise UnitParseError(f"Invalid unit exponent literal: {node.value!r}")
        val = float(node.value)
        if not math.isfinite(val) or abs(val) > 20:
            raise UnitParseError(f"Unit exponent out of bounds: {val!r}")
        return val
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _eval_unit_exponent(node.operand)
        return inner if isinstance(node.op, ast.UAdd) else -inner
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        num = _eval_unit_exponent(node.left)
        den = _eval_unit_exponent(node.right)
        if den == 0:
            raise UnitParseError("Division by zero in unit exponent")
        return num / den
    raise UnitParseError(f"Unsupported unit exponent AST node: {type(node).__name__}")


def _eval_unit_node(node: ast.AST, depth: int = 0) -> tuple[float, Dims]:
    if depth > 32:
        raise UnitParseError("Unit expression exceeds maximum nesting depth")
    if isinstance(node, ast.Expression):
        return _eval_unit_node(node.body, depth + 1)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise UnitParseError(f"Unsupported constant in unit: {node.value!r}")
        val = float(node.value)
        if not math.isfinite(val) or val <= 0:
            raise UnitParseError(f"Unit numeric scale must be positive and finite; got {val!r}")
        return (val, DIMLESS)
    if isinstance(node, ast.Name):
        sym = node.id
        if sym in UNIT_TABLE:
            return UNIT_TABLE[sym]
        raise UnitParseError(f"Unknown unit symbol: {sym!r}")
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mult):
            ls, ld = _eval_unit_node(node.left, depth + 1)
            rs, rd = _eval_unit_node(node.right, depth + 1)
            return (ls * rs, dims_mul(ld, rd))
        if isinstance(node.op, ast.Div):
            ls, ld = _eval_unit_node(node.left, depth + 1)
            rs, rd = _eval_unit_node(node.right, depth + 1)
            if rs == 0.0:
                raise UnitParseError("Division by zero in unit expression")
            return (ls / rs, dims_div(ld, rd))
        if isinstance(node.op, ast.Pow):
            ls, ld = _eval_unit_node(node.left, depth + 1)
            exp = _eval_unit_exponent(node.right)
            try:
                scale = float(ls**exp)
            except (OverflowError, ZeroDivisionError, ValueError) as exc:
                raise UnitParseError(f"Invalid unit scale exponentiation: {exc}") from exc
            return (scale, dims_pow(ld, exp))
        raise UnitParseError(f"Unsupported operator in unit expression: {type(node.op).__name__}")
    raise UnitParseError(f"Unsupported syntax in unit expression: {type(node).__name__}")


def parse_unit(unit: str) -> tuple[float, Dims]:
    """Safely parse a unit string (including compound expressions like ``T*m/A`` or ``m^-3``)."""
    if len(unit) > 256:
        raise UnitParseError("Unit string exceeds maximum length")
    if unit.strip() == "%":
        return (0.01, DIMLESS)
    normalized = _normalize_unit_expr(unit)
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise UnitParseError(f"Invalid unit expression {unit!r}: {exc.msg}") from exc
    scale, dims = _eval_unit_node(tree)
    if not math.isfinite(scale) or scale <= 0:
        raise UnitParseError(f"Non-finite or non-positive unit scale for {unit!r}")
    return scale, dims


# ---------------------------------------------------------------------------
# Internal Dimensional Quantity with Forward-Mode Gradient Tracking
# ---------------------------------------------------------------------------


def _ensure_finite_scalar(val: float | complex, context: str = "value") -> None:
    if isinstance(val, complex):
        if not (math.isfinite(val.real) and math.isfinite(val.imag)):
            raise CalcError(f"Non-finite complex {context}: {val}")
    else:
        if not math.isfinite(val):
            raise CalcError(f"Non-finite {context}: {val}")


def _add_grads(
    a: dict[str, float],
    b: dict[str, float],
    sa: float = 1.0,
    sb: float = 1.0,
) -> dict[str, float]:
    if not a and not b:
        return {}
    out: dict[str, float] = {}
    for key in a.keys() | b.keys():
        val = a.get(key, 0.0) * sa + b.get(key, 0.0) * sb
        if val != 0.0:
            if not math.isfinite(val):
                raise CalcError("Non-finite partial derivative in uncertainty propagation")
            out[key] = val
    return out


def _scale_grad(g: dict[str, float], factor: float) -> dict[str, float]:
    if not g or factor == 0.0:
        return {}
    out: dict[str, float] = {}
    for key, val in g.items():
        scaled = val * factor
        if not math.isfinite(scaled):
            raise CalcError("Non-finite partial derivative in uncertainty propagation")
        if scaled != 0.0:
            out[key] = scaled
    return out


@dataclass(frozen=True)
class Quantity:
    """SI-normalized dimensional quantity supporting real, complex, and 3D vector values.

    ``grad_re``, ``grad_im``, and ``grad_vec`` track first-order partial derivatives with
    respect to uncertain input variables (in SI units) for exact linear Gaussian propagation.
    """

    value: float | complex | tuple[float, ...]
    dims: Dims = DIMLESS
    grad_re: dict[str, float] = field(default_factory=dict)
    grad_im: dict[str, float] = field(default_factory=dict)
    grad_vec: tuple[dict[str, float], ...] = ()

    @property
    def is_vector(self) -> bool:
        return isinstance(self.value, tuple)

    @property
    def is_complex(self) -> bool:
        return isinstance(self.value, complex)

    @property
    def is_real_scalar(self) -> bool:
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool)

    def vec_grads(self) -> tuple[dict[str, float], ...]:
        assert isinstance(self.value, tuple)
        if self.grad_vec:
            return self.grad_vec
        return tuple({} for _ in self.value)

    def as_complex_parts(self) -> tuple[float, float, dict[str, float], dict[str, float]]:
        if isinstance(self.value, complex):
            return (self.value.real, self.value.imag, self.grad_re, self.grad_im)
        if isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
            return (float(self.value), 0.0, self.grad_re, {})
        raise CalcError("Expected scalar or complex quantity, got vector")


# Built-in physical & mathematical constants (CODATA SI + textbook exact mu0 = 4*pi*1e-7)
_C_LIGHT = 299_792_458.0
_MU0 = 4.0 * math.pi * 1e-7
_EPS0 = 1.0 / (_MU0 * _C_LIGHT * _C_LIGHT)

BUILTIN_CONSTANTS: dict[str, Quantity] = {
    "pi": Quantity(math.pi, DIMLESS),
    "euler": Quantity(math.e, DIMLESS),
    "j": Quantity(1j, DIMLESS),
    # Fundamental physical constants (scipy.constants convention: e = elementary charge)
    "e": Quantity(1.602176634e-19, (0, 0, 1, 1, 0)),
    "qe": Quantity(1.602176634e-19, (0, 0, 1, 1, 0)),
    "me": Quantity(9.1093837015e-31, (1, 0, 0, 0, 0)),
    "mp": Quantity(1.67262192369e-27, (1, 0, 0, 0, 0)),
    "c": Quantity(_C_LIGHT, (0, 1, -1, 0, 0)),
    "mu0": Quantity(_MU0, (1, 1, -2, -2, 0)),
    "eps0": Quantity(_EPS0, (-1, -3, 4, 2, 0)),
    "kB": Quantity(1.380649e-23, (1, 2, -2, 0, -1)),
    "g0": Quantity(9.80665, (0, 1, -2, 0, 0)),
}


# ---------------------------------------------------------------------------
# Arithmetic & Function Implementations on Quantity
# ---------------------------------------------------------------------------


def _holomorphic_grads(
    deriv: complex,
    grad_re: dict[str, float],
    grad_im: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    """Propagate complex gradients through holomorphic f(z) with derivative f'(z) = u + iv."""
    _ensure_finite_scalar(deriv, "complex derivative")
    if not grad_re and not grad_im:
        return {}, {}
    gr = _add_grads(grad_re, grad_im, deriv.real, -deriv.imag)
    gi = _add_grads(grad_re, grad_im, deriv.imag, deriv.real)
    return gr, gi


def _q_neg(a: Quantity) -> Quantity:
    if a.is_vector:
        assert isinstance(a.value, tuple)
        gvec = (
            tuple(_scale_grad(g, -1.0) for g in a.vec_grads()) if a.grad_vec else ()
        )
        return Quantity(tuple(-x for x in a.value), a.dims, grad_vec=gvec)
    if a.is_complex:
        assert isinstance(a.value, complex)
        return Quantity(
            -a.value,
            a.dims,
            _scale_grad(a.grad_re, -1.0),
            _scale_grad(a.grad_im, -1.0),
        )
    assert isinstance(a.value, (int, float))
    return Quantity(-float(a.value), a.dims, _scale_grad(a.grad_re, -1.0))


def _q_add_sub(a: Quantity, b: Quantity, sign: float) -> Quantity:
    if a.dims != b.dims:
        op_sym = "+" if sign > 0 else "-"
        raise DimensionError(
            f"Incompatible dimensions for '{op_sym}': "
            f"{format_si_dims(a.dims)} vs {format_si_dims(b.dims)}"
        )
    if a.is_vector or b.is_vector:
        if not (a.is_vector and b.is_vector):
            raise CalcError("Cannot add/subtract a vector and a scalar")
        assert isinstance(a.value, tuple) and isinstance(b.value, tuple)
        if len(a.value) != len(b.value):
            raise CalcError(f"Vector length mismatch: {len(a.value)} vs {len(b.value)}")
        res = tuple(x + sign * y for x, y in zip(a.value, b.value, strict=True))
        for item in res:
            _ensure_finite_scalar(item, "vector component")
        gvec: tuple[dict[str, float], ...] = ()
        if a.grad_vec or b.grad_vec:
            gvec = tuple(
                _add_grads(ga, gb, 1.0, sign)
                for ga, gb in zip(a.vec_grads(), b.vec_grads(), strict=True)
            )
        return Quantity(res, a.dims, grad_vec=gvec)

    if a.is_complex or b.is_complex:
        ar, ai, gar, gai = a.as_complex_parts()
        br, bi, gbr, gbi = b.as_complex_parts()
        val = complex(ar + sign * br, ai + sign * bi)
        _ensure_finite_scalar(val)
        return Quantity(
            val,
            a.dims,
            _add_grads(gar, gbr, 1.0, sign),
            _add_grads(gai, gbi, 1.0, sign),
        )

    assert isinstance(a.value, (int, float)) and isinstance(b.value, (int, float))
    val_f = float(a.value) + sign * float(b.value)
    _ensure_finite_scalar(val_f)
    return Quantity(val_f, a.dims, _add_grads(a.grad_re, b.grad_re, 1.0, sign))


def _q_mul(a: Quantity, b: Quantity) -> Quantity:
    out_dims = dims_mul(a.dims, b.dims)
    if a.is_vector and b.is_vector:
        raise CalcError("Direct vector * vector is ambiguous; use dot(a, b) or cross(a, b)")
    if a.is_vector or b.is_vector:
        vec_q, scalar_q = (a, b) if a.is_vector else (b, a)
        if scalar_q.is_complex:
            raise CalcError("Complex scalar * vector multiplication is not supported")
        assert isinstance(vec_q.value, tuple) and isinstance(scalar_q.value, (int, float))
        sf = float(scalar_q.value)
        res = tuple(x * sf for x in vec_q.value)
        for item in res:
            _ensure_finite_scalar(item, "vector component")
        gvec: tuple[dict[str, float], ...] = ()
        if vec_q.grad_vec or scalar_q.grad_re:
            gvec = tuple(
                _add_grads(gv, scalar_q.grad_re, sf, x)
                for x, gv in zip(vec_q.value, vec_q.vec_grads(), strict=True)
            )
        return Quantity(res, out_dims, grad_vec=gvec)

    if a.is_complex or b.is_complex:
        ar, ai, gar, gai = a.as_complex_parts()
        br, bi, gbr, gbi = b.as_complex_parts()
        val = complex(ar * br - ai * bi, ar * bi + ai * br)
        _ensure_finite_scalar(val)
        gr: dict[str, float] = {}
        gi: dict[str, float] = {}
        if gar or gai or gbr or gbi:
            # d(ar*br - ai*bi) = br*dar + ar*dbr - bi*dai - ai*dbi
            gr = _add_grads(
                _add_grads(gar, gbr, br, ar),
                _add_grads(gai, gbi, bi, ai),
                1.0,
                -1.0,
            )
            # d(ar*bi + ai*br) = bi*dar + ar*dbi + br*dai + ai*dbr
            gi = _add_grads(
                _add_grads(gar, gbi, bi, ar),
                _add_grads(gai, gbr, br, ai),
                1.0,
                1.0,
            )
        return Quantity(val, out_dims, gr, gi)

    assert isinstance(a.value, (int, float)) and isinstance(b.value, (int, float))
    af, bf = float(a.value), float(b.value)
    val_f = af * bf
    _ensure_finite_scalar(val_f)
    grad = _add_grads(a.grad_re, b.grad_re, bf, af) if (a.grad_re or b.grad_re) else {}
    return Quantity(val_f, out_dims, grad)


def _q_div(a: Quantity, b: Quantity) -> Quantity:
    if b.is_vector:
        raise CalcError("Division by a vector is undefined")
    out_dims = dims_div(a.dims, b.dims)
    if a.is_vector:
        if b.is_complex:
            raise CalcError("Division of a vector by a complex scalar is not supported")
        assert isinstance(a.value, tuple) and isinstance(b.value, (int, float))
        bf = float(b.value)
        if bf == 0.0:
            raise CalcError("Division by zero")
        res = tuple(x / bf for x in a.value)
        for item in res:
            _ensure_finite_scalar(item, "vector component")
        gvec: tuple[dict[str, float], ...] = ()
        if a.grad_vec or b.grad_re:
            try:
                inv_b = 1.0 / bf
                gvec = tuple(
                    _add_grads(gv, b.grad_re, inv_b, -(x / bf) / bf)
                    for x, gv in zip(a.value, a.vec_grads(), strict=True)
                )
            except (ZeroDivisionError, OverflowError) as exc:
                raise CalcError(
                    f"Unable to propagate uncertainty through vector division: {exc}"
                ) from exc
        return Quantity(res, out_dims, grad_vec=gvec)

    if a.is_complex or b.is_complex:
        ar, ai, gar, gai = a.as_complex_parts()
        br, bi, gbr, gbi = b.as_complex_parts()
        if br == 0.0 and bi == 0.0:
            raise CalcError("Division by zero")
        try:
            val = complex(ar, ai) / complex(br, bi)
        except (ZeroDivisionError, OverflowError) as exc:
            raise CalcError(f"Invalid complex division: {exc}") from exc
        _ensure_finite_scalar(val)
        gr: dict[str, float] = {}
        gi: dict[str, float] = {}
        if gar or gai or gbr or gbi:
            # Quotient rule: d(a/b) = (da - (a/b)*db) / b  (avoids b^2 underflow/overflow)
            b_complex = complex(br, bi)
            try:
                inv_b = 1.0 / b_complex
                neg_q_over_b = -val / b_complex
            except (ZeroDivisionError, OverflowError) as exc:
                raise CalcError(
                    f"Unable to propagate uncertainty through ill-scaled complex ratio: {exc}"
                ) from exc
            gr_a, gi_a = _holomorphic_grads(inv_b, gar, gai)
            gr_b, gi_b = _holomorphic_grads(neg_q_over_b, gbr, gbi)
            gr = _add_grads(gr_a, gr_b)
            gi = _add_grads(gi_a, gi_b)
        return Quantity(val, out_dims, gr, gi)

    assert isinstance(a.value, (int, float)) and isinstance(b.value, (int, float))
    af, bf = float(a.value), float(b.value)
    if bf == 0.0:
        raise CalcError("Division by zero")
    try:
        val_f = af / bf
    except (ZeroDivisionError, OverflowError) as exc:
        raise CalcError(f"Invalid division: {exc}") from exc
    _ensure_finite_scalar(val_f)
    grad: dict[str, float] = {}
    if a.grad_re or b.grad_re:
        try:
            da_coeff = 1.0 / bf if a.grad_re else 0.0
            db_coeff = -(val_f / bf) if b.grad_re else 0.0
        except (ZeroDivisionError, OverflowError) as exc:
            raise CalcError(f"Unable to propagate uncertainty through division: {exc}") from exc
        grad = _add_grads(a.grad_re, b.grad_re, da_coeff, db_coeff)
    return Quantity(val_f, out_dims, grad)


def _q_pow(a: Quantity, b: Quantity) -> Quantity:
    if a.is_vector or b.is_vector:
        raise CalcError("Power operator '**' is not supported on vectors")
    if b.dims != DIMLESS:
        raise DimensionError(
            f"Exponent must be dimensionless; got {format_si_dims(b.dims)}"
        )

    if b.is_complex:
        if a.dims != DIMLESS:
            raise DimensionError("Complex exponent requires a dimensionless base")
        assert isinstance(b.value, complex)
        if abs(b.value) > _MAX_EXPONENT_ABS:
            raise CalcError(f"Exponent magnitude exceeds safe limit ({_MAX_EXPONENT_ABS:g})")
        ar, ai, gar, gai = a.as_complex_parts()
        az = complex(ar, ai)
        try:
            val = az**b.value
        except (ZeroDivisionError, OverflowError, ValueError) as exc:
            raise CalcError(f"Invalid power operation: {exc}") from exc
        _ensure_finite_scalar(val)
        gr: dict[str, float] = {}
        gi: dict[str, float] = {}
        if gar or gai:
            if az == 0:
                raise CalcError("Cannot propagate uncertainty through 0**(complex exponent)")
            try:
                dz = b.value * (az ** (b.value - 1.0))
            except (ZeroDivisionError, OverflowError, ValueError) as exc:
                raise CalcError(f"Invalid complex power derivative: {exc}") from exc
            gr, gi = _holomorphic_grads(dz, gar, gai)
        if b.grad_re or b.grad_im:
            if az == 0:
                raise CalcError("Cannot propagate uncertainty through exponent of zero base")
            try:
                dw = val * cmath.log(az)
            except (ZeroDivisionError, OverflowError, ValueError) as exc:
                raise CalcError(f"Invalid complex power exponent derivative: {exc}") from exc
            gr_w, gi_w = _holomorphic_grads(dw, b.grad_re, b.grad_im)
            gr = _add_grads(gr, gr_w)
            gi = _add_grads(gi, gi_w)
        return Quantity(val, DIMLESS, gr, gi)

    assert isinstance(b.value, (int, float))
    exp = float(b.value)
    if abs(exp) > _MAX_EXPONENT_ABS:
        raise CalcError(f"Exponent magnitude exceeds safe limit ({_MAX_EXPONENT_ABS:g})")
    out_dims = dims_pow(a.dims, exp)

    if a.is_complex:
        assert isinstance(a.value, complex)
        try:
            val_c = a.value**exp
        except (ZeroDivisionError, OverflowError, ValueError) as exc:
            raise CalcError(f"Invalid complex power operation: {exc}") from exc
        _ensure_finite_scalar(val_c)
        gr = {}
        gi = {}
        if a.grad_re or a.grad_im:
            if exp == 0.0:
                pass
            elif a.value == 0:
                if exp < 1.0:
                    raise CalcError("Infinite derivative at zero complex base")
                if exp == 1.0:
                    gr, gi = dict(a.grad_re), dict(a.grad_im)
            else:
                try:
                    deriv = exp * (a.value ** (exp - 1.0))
                except (ZeroDivisionError, OverflowError, ValueError) as exc:
                    raise CalcError(f"Invalid complex power derivative: {exc}") from exc
                gr, gi = _holomorphic_grads(deriv, a.grad_re, a.grad_im)
        if b.grad_re:
            if a.value == 0:
                raise CalcError("Cannot propagate uncertainty through exponent of zero base")
            try:
                dw = val_c * cmath.log(a.value)
            except (ZeroDivisionError, OverflowError, ValueError) as exc:
                raise CalcError(f"Invalid complex power exponent derivative: {exc}") from exc
            gr_w, gi_w = _holomorphic_grads(dw, b.grad_re, {})
            gr = _add_grads(gr, gr_w)
            gi = _add_grads(gi, gi_w)
        return Quantity(val_c, out_dims, gr, gi)

    assert isinstance(a.value, (int, float))
    base = float(a.value)
    if base < 0.0 and not float(exp).is_integer():
        raise CalcError(
            f"Negative real base ({base:g}) raised to non-integer exponent ({exp:g})"
        )
    if base == 0.0 and exp < 0.0:
        raise CalcError("Zero raised to a negative power")
    try:
        val_f = float(base**exp)
    except (ZeroDivisionError, OverflowError, ValueError) as exc:
        raise CalcError(f"Invalid power operation: {exc}") from exc
    _ensure_finite_scalar(val_f)

    grad: dict[str, float] = {}
    if a.grad_re:
        if exp == 0.0:
            da = 0.0
        elif base == 0.0:
            da = 1.0 if exp == 1.0 else (0.0 if exp > 1.0 else float("inf"))
        else:
            try:
                da = float(exp * (base ** (exp - 1.0)))
            except (ZeroDivisionError, OverflowError, ValueError) as exc:
                raise CalcError(f"Unable to propagate uncertainty through power: {exc}") from exc
        grad = _scale_grad(a.grad_re, da)
    if b.grad_re:
        if base <= 0.0:
            raise CalcError("Cannot propagate uncertainty through exponent of non-positive base")
        try:
            db = float(val_f * math.log(base))
        except (OverflowError, ValueError) as exc:
            raise CalcError(f"Unable to propagate uncertainty through exponent: {exc}") from exc
        grad = _add_grads(grad, b.grad_re, 1.0, db)
    return Quantity(val_f, out_dims, grad)


def _require_arity(name: str, args: list[Quantity], expected: int) -> None:
    if len(args) != expected:
        raise CalcError(f"{name}() expects {expected} argument(s); got {len(args)}")


def _fn_sqrt(args: list[Quantity]) -> Quantity:
    _require_arity("sqrt", args, 1)
    q = args[0]
    if q.is_vector:
        raise CalcError("sqrt() is not defined for vectors")
    out_dims = dims_pow(q.dims, 0.5)
    if q.is_complex:
        assert isinstance(q.value, complex)
        val_c = cmath.sqrt(q.value)
        _ensure_finite_scalar(val_c)
        gr: dict[str, float] = {}
        gi: dict[str, float] = {}
        if q.grad_re or q.grad_im:
            if val_c == 0:
                raise CalcError("Infinite derivative in complex sqrt(0) uncertainty propagation")
            deriv = 0.5 / val_c
            gr, gi = _holomorphic_grads(deriv, q.grad_re, q.grad_im)
        return Quantity(val_c, out_dims, gr, gi)
    assert isinstance(q.value, (int, float))
    v = float(q.value)
    if v < 0.0:
        raise CalcError(f"sqrt() of negative real value ({v:g})")
    res = math.sqrt(v)
    if q.grad_re:
        if res == 0.0:
            raise CalcError("Infinite derivative in sqrt(0) uncertainty propagation")
        return Quantity(res, out_dims, _scale_grad(q.grad_re, 0.5 / res))
    return Quantity(res, out_dims)


def _fn_abs(args: list[Quantity]) -> Quantity:
    _require_arity("abs", args, 1)
    q = args[0]
    if q.is_vector:
        return _fn_norm(args)
    if q.is_complex:
        assert isinstance(q.value, complex)
        mag = abs(q.value)
        _ensure_finite_scalar(mag)
        if (q.grad_re or q.grad_im) and mag > 0.0:
            grad = _add_grads(q.grad_re, q.grad_im, q.value.real / mag, q.value.imag / mag)
            return Quantity(mag, q.dims, grad)
        return Quantity(mag, q.dims)
    assert isinstance(q.value, (int, float))
    v = float(q.value)
    sign = 1.0 if v >= 0.0 else -1.0
    return Quantity(abs(v), q.dims, _scale_grad(q.grad_re, sign))


def _fn_re(args: list[Quantity]) -> Quantity:
    _require_arity("re", args, 1)
    q = args[0]
    if q.is_vector:
        raise CalcError("re() is not defined for vectors")
    ar, _, gar, _ = q.as_complex_parts()
    return Quantity(ar, q.dims, dict(gar))


def _fn_im(args: list[Quantity]) -> Quantity:
    _require_arity("im", args, 1)
    q = args[0]
    if q.is_vector:
        raise CalcError("im() is not defined for vectors")
    _, ai, _, gai = q.as_complex_parts()
    return Quantity(ai, q.dims, dict(gai))


def _fn_conj(args: list[Quantity]) -> Quantity:
    _require_arity("conj", args, 1)
    q = args[0]
    if q.is_vector:
        raise CalcError("conj() is not defined for vectors")
    if q.is_complex:
        assert isinstance(q.value, complex)
        return Quantity(q.value.conjugate(), q.dims, dict(q.grad_re), _scale_grad(q.grad_im, -1.0))
    return q


def _fn_arg(args: list[Quantity]) -> Quantity:
    _require_arity("arg", args, 1)
    q = args[0]
    if q.is_vector:
        raise CalcError("arg() is not defined for vectors")
    ar, ai, gar, gai = q.as_complex_parts()
    if ar == 0.0 and ai == 0.0:
        raise CalcError("arg(0) is undefined")
    angle = math.atan2(ai, ar)
    grad: dict[str, float] = {}
    if gar or gai:
        h = math.hypot(ar, ai)
        if h == 0.0 or not math.isfinite(h):
            raise CalcError("Unable to propagate uncertainty through arg() at ill-scaled point")
        try:
            grad = _add_grads(gai, gar, (ar / h) / h, -(ai / h) / h)
        except (ZeroDivisionError, OverflowError) as exc:
            raise CalcError(f"Unable to propagate uncertainty through arg(): {exc}") from exc
    return Quantity(angle, DIMLESS, grad)


def _fn_vec(args: list[Quantity]) -> Quantity:
    if len(args) != 3:
        raise CalcError(f"vec() expects 3 scalar components; got {len(args)}")
    dims = args[0].dims
    comps: list[float] = []
    grads: list[dict[str, float]] = []
    has_grad = False
    for item in args:
        if not item.is_real_scalar:
            raise CalcError("vec() requires real scalar components")
        if item.dims != dims:
            raise DimensionError(
                f"All vec() components must share identical dimensions: "
                f"{format_si_dims(dims)} vs {format_si_dims(item.dims)}"
            )
        comps.append(float(item.value))  # type: ignore[arg-type]
        if item.grad_re:
            has_grad = True
        grads.append(dict(item.grad_re))
    return Quantity(
        (comps[0], comps[1], comps[2]),
        dims,
        grad_vec=tuple(grads) if has_grad else (),
    )


def _fn_dot(args: list[Quantity]) -> Quantity:
    _require_arity("dot", args, 2)
    a, b = args
    if not (a.is_vector and b.is_vector):
        raise CalcError("dot() requires two vectors")
    assert isinstance(a.value, tuple) and isinstance(b.value, tuple)
    if len(a.value) != len(b.value):
        raise CalcError(f"dot() vector length mismatch: {len(a.value)} vs {len(b.value)}")
    val = sum(x * y for x, y in zip(a.value, b.value, strict=True))
    _ensure_finite_scalar(val)
    grad: dict[str, float] = {}
    if a.grad_vec or b.grad_vec:
        for x, y, ga, gb in zip(a.value, b.value, a.vec_grads(), b.vec_grads(), strict=True):
            term_g = _add_grads(ga, gb, y, x)
            grad = _add_grads(grad, term_g)
    return Quantity(float(val), dims_mul(a.dims, b.dims), grad)


def _fn_cross(args: list[Quantity]) -> Quantity:
    _require_arity("cross", args, 2)
    a, b = args
    if not (a.is_vector and b.is_vector):
        raise CalcError("cross() requires two 3D vectors")
    assert isinstance(a.value, tuple) and isinstance(b.value, tuple)
    if len(a.value) != 3 or len(b.value) != 3:
        raise CalcError(
            f"cross() requires 3D vectors; got lengths {len(a.value)} and {len(b.value)}"
        )
    ax, ay, az = a.value
    bx, by, bz = b.value
    res = (ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx)
    for item in res:
        _ensure_finite_scalar(item, "cross product component")
    gvec: tuple[dict[str, float], ...] = ()
    if a.grad_vec or b.grad_vec:
        gax, gay, gaz = a.vec_grads()
        gbx, gby, gbz = b.vec_grads()
        gx = _add_grads(_add_grads(gay, gbz, bz, ay), _add_grads(gaz, gby, by, az), 1.0, -1.0)
        gy = _add_grads(_add_grads(gaz, gbx, bx, az), _add_grads(gax, gbz, bz, ax), 1.0, -1.0)
        gz = _add_grads(_add_grads(gax, gby, by, ax), _add_grads(gay, gbx, bx, ay), 1.0, -1.0)
        gvec = (gx, gy, gz)
    return Quantity(res, dims_mul(a.dims, b.dims), grad_vec=gvec)


def _fn_norm(args: list[Quantity]) -> Quantity:
    _require_arity("norm", args, 1)
    q = args[0]
    if not q.is_vector:
        return _fn_abs(args)
    assert isinstance(q.value, tuple)
    val = math.hypot(*q.value)
    _ensure_finite_scalar(val)
    grad: dict[str, float] = {}
    if q.grad_vec:
        if val == 0.0:
            raise CalcError("Undefined derivative in norm(0) uncertainty propagation")
        for comp, gc in zip(q.value, q.vec_grads(), strict=True):
            if gc:
                grad = _add_grads(grad, gc, 1.0, comp / val)
    return Quantity(val, q.dims, grad)


def _fn_dimless_unary(
    name: str,
    args: list[Quantity],
    real_fn: Any,
    deriv_fn: Any,
    complex_fn: Any | None = None,
    complex_deriv_fn: Any | None = None,
) -> Quantity:
    _require_arity(name, args, 1)
    q = args[0]
    if q.is_vector:
        raise CalcError(f"{name}() is not defined for vectors")
    if q.dims != DIMLESS:
        raise DimensionError(
            f"{name}() requires a dimensionless argument; got {format_si_dims(q.dims)}"
        )
    if q.is_complex:
        if complex_fn is None:
            raise CalcError(f"{name}() does not support complex arguments")
        assert isinstance(q.value, complex)
        try:
            val_c = complex_fn(q.value)
        except (ValueError, OverflowError, ZeroDivisionError) as exc:
            raise CalcError(f"{name}() domain error: {exc}") from exc
        _ensure_finite_scalar(val_c)
        gr: dict[str, float] = {}
        gi: dict[str, float] = {}
        if q.grad_re or q.grad_im:
            if complex_deriv_fn is None:
                raise CalcError(
                    f"{name}() uncertainty propagation is not supported for complex arguments"
                )
            try:
                dc = complex(complex_deriv_fn(q.value))
            except (ValueError, OverflowError, ZeroDivisionError) as exc:
                raise CalcError(f"{name}() complex derivative error: {exc}") from exc
            gr, gi = _holomorphic_grads(dc, q.grad_re, q.grad_im)
        return Quantity(val_c, DIMLESS, gr, gi)
    assert isinstance(q.value, (int, float))
    v = float(q.value)
    try:
        res = float(real_fn(v))
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        raise CalcError(f"{name}() domain error for {v:g}: {exc}") from exc
    _ensure_finite_scalar(res)
    if q.grad_re:
        try:
            d = float(deriv_fn(v))
        except (ValueError, OverflowError, ZeroDivisionError) as exc:
            raise CalcError(f"{name}() derivative error at {v:g}: {exc}") from exc
        _ensure_finite_scalar(d, "derivative")
        return Quantity(res, DIMLESS, _scale_grad(q.grad_re, d))
    return Quantity(res, DIMLESS)


def _fn_atan2(args: list[Quantity]) -> Quantity:
    _require_arity("atan2", args, 2)
    y, x = args
    if not (y.is_real_scalar and x.is_real_scalar):
        raise CalcError("atan2() requires two real scalars")
    if y.dims != x.dims:
        raise DimensionError(
            f"atan2() arguments must have identical dimensions: "
            f"{format_si_dims(y.dims)} vs {format_si_dims(x.dims)}"
        )
    yv, xv = float(y.value), float(x.value)  # type: ignore[arg-type]
    if yv == 0.0 and xv == 0.0:
        raise CalcError("atan2(0, 0) is undefined")
    res = math.atan2(yv, xv)
    grad: dict[str, float] = {}
    if y.grad_re or x.grad_re:
        h = math.hypot(xv, yv)
        if h == 0.0 or not math.isfinite(h):
            raise CalcError("Unable to propagate uncertainty through atan2() at ill-scaled point")
        try:
            grad = _add_grads(y.grad_re, x.grad_re, (xv / h) / h, -(yv / h) / h)
        except (ZeroDivisionError, OverflowError) as exc:
            raise CalcError(f"Unable to propagate uncertainty through atan2(): {exc}") from exc
    return Quantity(res, DIMLESS, grad)


_FUNCTIONS: dict[str, Any] = {
    "sqrt": _fn_sqrt,
    "abs": _fn_abs,
    "re": _fn_re,
    "im": _fn_im,
    "conj": _fn_conj,
    "arg": _fn_arg,
    "vec": _fn_vec,
    "dot": _fn_dot,
    "cross": _fn_cross,
    "norm": _fn_norm,
    "atan2": _fn_atan2,
    "sin": lambda a: _fn_dimless_unary("sin", a, math.sin, math.cos, cmath.sin, cmath.cos),
    "cos": lambda a: _fn_dimless_unary(
        "cos", a, math.cos, lambda x: -math.sin(x), cmath.cos, lambda z: -cmath.sin(z)
    ),
    "tan": lambda a: _fn_dimless_unary(
        "tan",
        a,
        math.tan,
        lambda x: 1.0 / (math.cos(x) ** 2),
        cmath.tan,
        lambda z: 1.0 / (cmath.cos(z) ** 2),
    ),
    "asin": lambda a: _fn_dimless_unary(
        "asin", a, math.asin, lambda x: 1.0 / math.sqrt(1.0 - x * x)
    ),
    "acos": lambda a: _fn_dimless_unary(
        "acos", a, math.acos, lambda x: -1.0 / math.sqrt(1.0 - x * x)
    ),
    "atan": lambda a: _fn_dimless_unary("atan", a, math.atan, lambda x: 1.0 / (1.0 + x * x)),
    "exp": lambda a: _fn_dimless_unary("exp", a, math.exp, math.exp, cmath.exp, cmath.exp),
    "log": lambda a: _fn_dimless_unary(
        "log", a, math.log, lambda x: 1.0 / x, cmath.log, lambda z: 1.0 / z
    ),
    "log10": lambda a: _fn_dimless_unary(
        "log10",
        a,
        math.log10,
        lambda x: 1.0 / (x * math.log(10.0)),
        cmath.log10,
        lambda z: 1.0 / (z * math.log(10.0)),
    ),
}


# ---------------------------------------------------------------------------
# Safe AST Evaluator & PhysicsCalculator
# ---------------------------------------------------------------------------


def _eval_ast_node(node: ast.AST, env: dict[str, Quantity], depth: int = 0) -> Quantity:
    if depth > _MAX_AST_DEPTH:
        raise UnsafeExpressionError(f"Expression exceeds maximum AST depth ({_MAX_AST_DEPTH})")

    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body, env, depth + 1)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            raise UnsafeExpressionError("Boolean literals are forbidden in physics expressions")
        if isinstance(node.value, (int, float)):
            val = float(node.value)
            _ensure_finite_scalar(val, "literal")
            return Quantity(val, DIMLESS)
        if isinstance(node.value, complex):
            _ensure_finite_scalar(node.value, "complex literal")
            return Quantity(node.value, DIMLESS)
        raise UnsafeExpressionError(f"Unsupported literal type: {type(node.value).__name__}")

    if isinstance(node, ast.Name):
        ident = node.id
        if ident.startswith("__") or ident.endswith("__"):
            raise UnsafeExpressionError(f"Forbidden dunder identifier: {ident!r}")
        if ident in env:
            return env[ident]
        if ident in BUILTIN_CONSTANTS:
            return BUILTIN_CONSTANTS[ident]
        raise CalcError(f"Unknown variable or constant: {ident!r}")

    if isinstance(node, (ast.List, ast.Tuple)):
        if len(node.elts) == 0:
            raise CalcError("Empty vector literals are not allowed")
        items = [_eval_ast_node(elt, env, depth + 1) for elt in node.elts]
        dims = items[0].dims
        comps: list[float] = []
        grads: list[dict[str, float]] = []
        has_grad = False
        for item in items:
            if not item.is_real_scalar:
                raise CalcError("Vector literals must contain real scalar entries")
            if item.dims != dims:
                raise DimensionError(
                    f"Vector elements must share identical dimensions: "
                    f"{format_si_dims(dims)} vs {format_si_dims(item.dims)}"
                )
            comps.append(float(item.value))  # type: ignore[arg-type]
            if item.grad_re:
                has_grad = True
            grads.append(dict(item.grad_re))
        return Quantity(tuple(comps), dims, grad_vec=tuple(grads) if has_grad else ())

    if isinstance(node, ast.UnaryOp):
        operand = _eval_ast_node(node.operand, env, depth + 1)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return _q_neg(operand)
        raise UnsafeExpressionError(f"Unsupported unary operator: {type(node.op).__name__}")

    if isinstance(node, ast.BinOp):
        left = _eval_ast_node(node.left, env, depth + 1)
        right = _eval_ast_node(node.right, env, depth + 1)
        if isinstance(node.op, ast.Add):
            return _q_add_sub(left, right, 1.0)
        if isinstance(node.op, ast.Sub):
            return _q_add_sub(left, right, -1.0)
        if isinstance(node.op, ast.Mult):
            return _q_mul(left, right)
        if isinstance(node.op, ast.Div):
            return _q_div(left, right)
        if isinstance(node.op, ast.Pow):
            return _q_pow(left, right)
        raise UnsafeExpressionError(f"Unsupported binary operator: {type(node.op).__name__}")

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise UnsafeExpressionError(
                "Only direct calls to whitelisted math functions are allowed"
            )
        if node.keywords:
            raise UnsafeExpressionError("Keyword arguments are not allowed in physics expressions")
        fn_name = node.func.id
        if fn_name.startswith("__") or fn_name not in _FUNCTIONS:
            raise UnsafeExpressionError(f"Forbidden or unknown function call: {fn_name!r}")
        eval_args = [_eval_ast_node(arg, env, depth + 1) for arg in node.args]
        return _FUNCTIONS[fn_name](eval_args)

    raise UnsafeExpressionError(f"Disallowed AST construct: {type(node).__name__}")


def _convert_variable(
    name: str,
    var: CalcVariable,
) -> tuple[Quantity, float | None]:
    if not name.isidentifier() or name.startswith("__") or name.endswith("__"):
        raise UnsafeExpressionError(f"Invalid variable name: {name!r}")
    scale, dims = parse_unit(var.unit)

    if isinstance(var.value, list):
        if var.imag is not None:
            raise CalcError(f"Vector variable {name!r} cannot specify an imaginary part")
        if len(var.value) == 0:
            raise CalcError(f"Vector variable {name!r} cannot be empty")
        comps = tuple(float(v) * scale for v in var.value)
        for item in comps:
            _ensure_finite_scalar(item, f"vector variable {name!r}")
        if var.uncertainty is not None:
            raise CalcError(f"Vector variable {name!r} does not support scalar uncertainty")
        return Quantity(comps, dims), None

    re_val = float(var.value) * scale
    _ensure_finite_scalar(re_val, f"variable {name!r}")
    sigma_si: float | None = None
    grad_re: dict[str, float] = {}
    if var.uncertainty is not None:
        sigma_si = float(var.uncertainty) * scale
        _ensure_finite_scalar(sigma_si, f"uncertainty for {name!r}")
        if sigma_si > 0.0:
            grad_re = {name: 1.0}

    if var.imag is not None:
        im_val = float(var.imag) * scale
        _ensure_finite_scalar(im_val, f"imaginary part of {name!r}")
        return Quantity(complex(re_val, im_val), dims, grad_re=grad_re), sigma_si

    return Quantity(re_val, dims, grad_re=grad_re), sigma_si


class PhysicsCalculator:
    """Deterministic dimensional-analysis physics calculator with optional run ledger."""

    def evaluate(
        self,
        request: CalcRequest | dict[str, Any],
        workspace: RunWorkspace | None = None,
    ) -> CalcResult:
        req = request if isinstance(request, CalcRequest) else CalcRequest.model_validate(request)

        expr = req.expression.strip()
        if not expr:
            raise CalcError("Expression must not be empty")
        if len(expr) > _MAX_EXPRESSION_LEN:
            raise UnsafeExpressionError(
                f"Expression exceeds maximum length ({_MAX_EXPRESSION_LEN} chars)"
            )

        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as exc:
            raise CalcError(f"Syntax error in expression {expr!r}: {exc.msg}") from exc

        env: dict[str, Quantity] = {}
        sigmas: dict[str, float] = {}
        has_any_uncertainty = False

        for var_name, var_spec in req.variables.items():
            qty, sigma_si = _convert_variable(var_name, var_spec)
            env[var_name] = qty
            if sigma_si is not None:
                has_any_uncertainty = True
                sigmas[var_name] = sigma_si

        result_qty = _eval_ast_node(tree, env)

        # Output unit conversion & dimensional verification
        if req.output_unit is not None:
            cleaned_unit = req.output_unit.strip()
            if not cleaned_unit:
                raise UnitParseError("output_unit must not be empty or whitespace-only")
            out_scale, out_dims = parse_unit(cleaned_unit)
            if out_dims != result_qty.dims:
                raise DimensionError(
                    f"Output unit {req.output_unit!r} has dims {format_si_dims(out_dims)}, "
                    f"incompatible with result dims {format_si_dims(result_qty.dims)}"
                )
            output_unit_str = cleaned_unit
        else:
            out_scale = 1.0
            output_unit_str = format_si_dims(result_qty.dims)

        # Extract SI & output values
        si_imag: float | None = None
        output_imag: float | None = None
        si_value: float | list[float]
        output_value: float | list[float]

        if result_qty.is_vector:
            assert isinstance(result_qty.value, tuple)
            si_value = [float(x) for x in result_qty.value]
            output_value = [float(x / out_scale) for x in result_qty.value]
            for x in output_value:
                _ensure_finite_scalar(x, "output vector component")
        elif result_qty.is_complex:
            assert isinstance(result_qty.value, complex)
            si_value = float(result_qty.value.real)
            si_imag = float(result_qty.value.imag)
            output_value = float(si_value / out_scale)
            output_imag = float(si_imag / out_scale)
            _ensure_finite_scalar(output_value, "output real part")
            _ensure_finite_scalar(output_imag, "output imaginary part")
        else:
            assert isinstance(result_qty.value, (int, float))
            si_value = float(result_qty.value)
            output_value = float(si_value / out_scale)
            _ensure_finite_scalar(output_value, "output value")

        # Propagated 1-sigma uncertainty in SI units (linear Gaussian)
        uncertainty_si: float | None = None
        if has_any_uncertainty and result_qty.is_real_scalar:
            variance = 0.0
            for var_name, deriv in result_qty.grad_re.items():
                sigma = sigmas.get(var_name, 0.0)
                variance += (deriv * sigma) ** 2
            uncertainty_si = math.sqrt(variance)
            _ensure_finite_scalar(uncertainty_si, "propagated uncertainty")

        calc_id = f"calc-{secrets.token_hex(6)}"
        calc_result = CalcResult(
            calc_id=calc_id,
            expression=expr,
            si_value=si_value,
            si_imag=si_imag,
            si_dims=result_qty.dims,
            output_value=output_value,
            output_imag=output_imag,
            output_unit=output_unit_str,
            uncertainty_si=uncertainty_si,
        )

        if workspace is not None:
            _append_calculation_ledger(workspace, calc_result)

        return calc_result


def _append_calculation_ledger(workspace: RunWorkspace, result: CalcResult) -> None:
    workspace.root.mkdir(parents=True, exist_ok=True)
    ledger_path = workspace.resolve_path("calculations.jsonl")
    line = result.model_dump_json() + "\n"
    with _LEDGER_LOCK:
        with ledger_path.open("a", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_calculations(workspace: RunWorkspace) -> dict[str, CalcResult]:
    """Load all persisted ``CalcResult`` records from ``<run>/calculations.jsonl``."""
    ledger_path = workspace.resolve_path("calculations.jsonl")
    if not ledger_path.is_file():
        return {}
    results: dict[str, CalcResult] = {}
    with ledger_path.open("r", encoding="utf-8") as handle:
        for line_num, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record = CalcResult.model_validate_json(stripped)
            except Exception as exc:
                raise ValueError(
                    f"Malformed calculation ledger entry at line {line_num} in {ledger_path}"
                ) from exc
            results[record.calc_id] = record
    return results


def calc_eval(
    request: CalcRequest | dict[str, Any],
    workspace: RunWorkspace | None = None,
) -> CalcResult:
    """Functional entry point for evaluating a safe dimensional physics calculation."""
    return PhysicsCalculator().evaluate(request, workspace=workspace)


__all__ = [
    "BUILTIN_CONSTANTS",
    "DIMLESS",
    "UNIT_TABLE",
    "CalcError",
    "DimensionError",
    "Dims",
    "PhysicsCalculator",
    "Quantity",
    "UnitParseError",
    "UnsafeExpressionError",
    "calc_eval",
    "dims_div",
    "dims_mul",
    "dims_pow",
    "format_si_dims",
    "load_calculations",
    "parse_unit",
]
