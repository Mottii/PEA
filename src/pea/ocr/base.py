"""Pluggable OCR protocol and pure-Python crop/homoglyph/numeric parsing helpers."""

from __future__ import annotations

import io
import math
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from PIL import Image, ImageOps

from pea.schemas import BBoxPx, OcrBackendKind, OcrBox, OcrMode, OcrResult, ParseMode, ReadoutSpec

ImageInput = Image.Image | Path | str | bytes

# Visual Latin -> Cyrillic homoglyphs commonly confused on Russian physics UI labels/units.
_LATIN_TO_CYRILLIC_HOMOGLYPHS: dict[str, str] = {
    "a": "а",
    "c": "с",
    "e": "е",
    "h": "н",
    "k": "к",
    "l": "л",
    "m": "м",
    "o": "о",
    "p": "р",
    "t": "т",
    "x": "х",
    "A": "А",
    "B": "В",
    "C": "С",
    "E": "Е",
    "H": "Н",
    "K": "К",
    "M": "М",
    "O": "О",
    "P": "Р",
    "T": "Т",
    "X": "Х",
}
_HOMOGLYPH_TABLE = str.maketrans(_LATIN_TO_CYRILLIC_HOMOGLYPHS)

_DASH_REPLACEMENTS = str.maketrans(
    {
        "\u2212": "-",  # Unicode minus sign
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u00a0": " ",  # non-breaking space
    }
)

# Signed number (supports Russian decimal comma/dot, OCR box-split decimal separator
# like '-67 ,86', and scientific notation with Latin/Cyrillic e).
# Requires a non-identifier boundary before the number so trailing exponents like 'с-1'
# or identifiers are not misparsed ahead of the primary reading.
_NUMBER_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s=:(\[|]))([+-]?\s*\d+(?:[ \t]?[.,]\d+)?(?:[eEеЕ][+-]?\d+)?)"
)

# Fallback matcher when digits are glued to a prefix symbol without spaces (e.g. 'В=50,0').
_LOOSE_NUMBER_RE = re.compile(r"([+-]?\d+(?:[ \t]?[.,]\d+)?(?:[eEеЕ][+-]?\d+)?)")

# Unit-exponent suffixes (e.g. 'с-1', 'c-1', 's^-1', 'м^-3') masked before numeric parsing
# so header/unit-only strings like 'f, с-1' do not falsely parse '-1' as a reading.
_UNIT_EXPONENT_RE = re.compile(
    r"(?<![0-9A-Za-zА-Яа-яЁё.,])(?:с|c|s|м|m)\s*\^?\s*[-−]\d+(?![0-9.,])",
    re.IGNORECASE,
)

# Leading variable assignment prefix (e.g. 'B =', 'I =', 'Сила тока I =') stripped before
# fallback unit search when no unit follows the numeric reading.
_LEADING_ASSIGN_RE = re.compile(r"^[^=]*=\s*")

# Case-sensitive token-bounded physical units, ordered longest-first so 'мкТл' matches
# before 'мТл' and 'Тл', and single-letter units never match inside words ('Сила', 'Магнитная').
_UNIT_LEFT_BOUNDARY = r"(?<![A-Za-zА-Яа-яЁё])"
_UNIT_RIGHT_BOUNDARY = r"(?![A-Za-zА-Яа-яЁё])"
_DEFAULT_UNIT_RE = re.compile(
    _UNIT_LEFT_BOUNDARY
    + r"(?:"
    r"мкТл|мТл|Тл|"
    r"мкГн|мГн|Гн|"
    r"мкФ|нФ|пФ|мФ|Ф|"
    r"МОм|кОм|мОм|Ом|"
    r"рад/с|с\s*\^?\s*[-−]1|с⁻¹|"
    r"МГц|кГц|Гц|"
    r"мкА|мА|кА|А|"
    r"мкВ|мВ|кВ|В|"
    r"мкс|мс|нс|с|"
    r"см|мм|км|м|"
    r"µT|uT|mT|T|"
    r"mH|uH|µH|H|"
    r"uF|µF|nF|pF|mF|F|"
    r"MOhm|kOhm|mOhm|Ohm|Ω|"
    r"rad/s|s\s*\^?\s*[-−]1|s⁻¹|"
    r"MHz|kHz|Hz|"
    r"mA|uA|µA|kA|A|"
    r"mV|uV|µV|kV|V|"
    r"ms|us|µs|ns|s|"
    r"cm|mm|km|m"
    r")"
    + _UNIT_RIGHT_BOUNDARY
)


def load_pil_image(image: ImageInput) -> Image.Image:
    """Convert any supported ``ImageInput`` into an in-memory RGB ``PIL.Image.Image``."""
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    if isinstance(image, bytes):
        with Image.open(io.BytesIO(image)) as opened:
            return opened.convert("RGB")
    path = Path(image)
    if not path.is_file():
        raise FileNotFoundError(f"OCR image file not found: {path}")
    with Image.open(path) as opened:
        return opened.convert("RGB")


def normalize_homoglyphs(text: str) -> str:
    """Normalize Unicode minus/spaces and Latin visual lookalikes into Cyrillic equivalents."""
    cleaned = text.translate(_DASH_REPLACEMENTS)
    return cleaned.translate(_HOMOGLYPH_TABLE)


def prepare_crop(
    image: ImageInput,
    roi_px: BBoxPx | None = None,
    pad_px: int = 0,
    scale: float = 1.0,
) -> Image.Image:
    """Crop ``roi_px``, upscale with Lanczos resampling, and add white border padding."""
    if pad_px < 0:
        raise ValueError(f"pad_px must be >= 0; got {pad_px}")
    if scale <= 0.0 or not math.isfinite(scale):
        raise ValueError(f"scale must be finite and > 0; got {scale}")

    img = load_pil_image(image)

    if roi_px is not None:
        x1, y1, x2, y2 = roi_px
        width, height = img.size
        cx1 = max(0, min(x1, width))
        cy1 = max(0, min(y1, height))
        cx2 = max(0, min(x2, width))
        cy2 = max(0, min(y2, height))
        if cx2 <= cx1 or cy2 <= cy1:
            raise ValueError(
                f"ROI {roi_px} produces an empty crop on image of size {(width, height)}"
            )
        img = img.crop((cx1, cy1, cx2, cy2))

    if scale != 1.0:
        new_w = max(1, int(round(img.width * scale)))
        new_h = max(1, int(round(img.height * scale)))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    if pad_px > 0:
        img = ImageOps.expand(img, border=pad_px, fill=(255, 255, 255))

    return img


def sort_boxes_reading_order(boxes: list[OcrBox]) -> list[OcrBox]:
    """Sort OCR boxes in row-major natural reading order (top-to-bottom, left-to-right)."""
    if len(boxes) <= 1:
        return list(boxes)

    def _y_center(box: OcrBox) -> float:
        return 0.5 * (box.bbox_px[1] + box.bbox_px[3])

    def _height(box: OcrBox) -> float:
        return max(1.0, float(box.bbox_px[3] - box.bbox_px[1]))

    by_y = sorted(boxes, key=lambda b: (_y_center(b), b.bbox_px[0]))
    rows: list[list[OcrBox]] = []

    for box in by_y:
        yc = _y_center(box)
        bh = _height(box)
        placed = False
        for row in rows:
            row_yc = sum(_y_center(item) for item in row) / len(row)
            row_h = sum(_height(item) for item in row) / len(row)
            if abs(yc - row_yc) <= 0.55 * max(row_h, bh):
                row.append(box)
                placed = True
                break
        if not placed:
            rows.append([box])

    ordered: list[OcrBox] = []
    for row in rows:
        row.sort(key=lambda b: (b.bbox_px[0], b.bbox_px[1]))
        ordered.extend(row)
    return ordered


def join_ocr_text(result: OcrResult) -> str:
    """Join all boxes of an ``OcrResult`` in natural reading order."""
    ordered = sort_boxes_reading_order(result.boxes)
    parts = [box.text.strip() for box in ordered if box.text.strip()]
    return " ".join(parts)


def _search_unit_regex(
    cleaned_fragment: str,
    normalized_fragment: str,
    regex: re.Pattern[str],
    use_homoglyphs: bool,
) -> str | None:
    match = regex.search(normalized_fragment)
    if match is not None:
        return match.group(0)
    if use_homoglyphs:
        raw_match = regex.search(cleaned_fragment)
        if raw_match is not None:
            return raw_match.group(0)
    return None


def _extract_unit(
    cleaned_text: str,
    normalized_text: str,
    unit_pattern: str | None,
    use_homoglyphs: bool,
    *,
    after_pos: int | None = None,
) -> str | None:
    regex = (
        re.compile(unit_pattern, re.IGNORECASE) if unit_pattern is not None else _DEFAULT_UNIT_RE
    )

    if after_pos is not None and 0 <= after_pos <= len(cleaned_text):
        tail_cleaned = cleaned_text[after_pos:]
        tail_norm = (
            globals()["normalize_homoglyphs"](tail_cleaned) if use_homoglyphs else tail_cleaned
        )
        found_tail = _search_unit_regex(tail_cleaned, tail_norm, regex, use_homoglyphs)
        if found_tail is not None:
            return found_tail

    # Strip leading variable-name assignment (e.g. 'B = 50,0') so variable symbols before '='
    # are not misidentified as physical units when no unit follows the reading.
    fallback_cleaned = _LEADING_ASSIGN_RE.sub("", cleaned_text)
    fallback_norm = (
        globals()["normalize_homoglyphs"](fallback_cleaned)
        if use_homoglyphs
        else fallback_cleaned
    )
    return _search_unit_regex(fallback_cleaned, fallback_norm, regex, use_homoglyphs)


def parse_numeric_and_unit(
    text: str,
    parse_mode: ParseMode = "float",
    unit_pattern: str | None = None,
    normalize_homoglyphs: bool = True,
) -> tuple[float | int | str | None, str | None]:
    """Parse a signed number (supporting Russian decimal comma) and unit from OCR text."""
    cleaned = text.translate(_DASH_REPLACEMENTS).strip()
    if not cleaned:
        return (None, None)

    homoglyph_normalized = (
        globals()["normalize_homoglyphs"](cleaned) if normalize_homoglyphs else cleaned
    )

    if parse_mode == "text":
        unit = _extract_unit(cleaned, homoglyph_normalized, unit_pattern, normalize_homoglyphs)
        return (homoglyph_normalized, unit)

    # Remove literal blinking-caret pipe characters so '|4,0' does not break boundaries,
    # and mask explicit unit exponents ('с-1', 's^-1') preserving string offsets.
    numeric_surface = cleaned.replace("|", " ")
    numeric_surface = _UNIT_EXPONENT_RE.sub(lambda m: " " * len(m.group(0)), numeric_surface)

    match = _NUMBER_RE.search(numeric_surface)
    if match is None:
        match = _LOOSE_NUMBER_RE.search(numeric_surface)

    unit = _extract_unit(
        cleaned,
        homoglyph_normalized,
        unit_pattern,
        normalize_homoglyphs,
        after_pos=match.end() if match is not None else None,
    )
    if match is None:
        return (None, unit)

    token = (
        re.sub(r"\s+", "", match.group(1))
        .replace(",", ".")
        .replace("е", "e")
        .replace("Е", "E")
    )
    try:
        numeric_val = float(token)
    except ValueError:
        return (None, unit)

    if not math.isfinite(numeric_val):
        return (None, unit)

    if parse_mode == "int":
        rounded = round(numeric_val)
        if abs(numeric_val - rounded) > 1e-9:
            return (None, unit)
        return (int(rounded), unit)

    return (numeric_val, unit)


@runtime_checkable
class OcrBackend(Protocol):
    """Cross-platform pluggable OCR backend contract."""

    @property
    def backend_name(self) -> OcrBackendKind: ...

    def recognize(self, image: ImageInput, *, mode: OcrMode = "det_rec") -> OcrResult: ...

    def read_spec(
        self,
        image: ImageInput,
        spec: ReadoutSpec,
    ) -> tuple[OcrResult, float | int | str | None, str | None]: ...


def read_spec_with_backend(
    backend: OcrBackend,
    image: ImageInput,
    spec: ReadoutSpec,
) -> tuple[OcrResult, float | int | str | None, str | None]:
    """Shared implementation of ``OcrBackend.read_spec`` using ``prepare_crop`` and parsing."""
    cropped = prepare_crop(
        image,
        roi_px=spec.roi_px,
        pad_px=spec.pad_px,
        scale=spec.scale,
    )
    ocr_result = backend.recognize(cropped, mode=spec.ocr_mode)
    joined = join_ocr_text(ocr_result)
    parsed_value, parsed_unit = parse_numeric_and_unit(
        joined,
        parse_mode=spec.parse_mode,
        unit_pattern=spec.unit_pattern,
        normalize_homoglyphs=spec.normalize_homoglyphs,
    )
    return (ocr_result, parsed_value, parsed_unit)


__all__ = [
    "ImageInput",
    "OcrBackend",
    "join_ocr_text",
    "load_pil_image",
    "normalize_homoglyphs",
    "parse_numeric_and_unit",
    "prepare_crop",
    "read_spec_with_backend",
    "sort_boxes_reading_order",
]
