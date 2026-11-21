"""SHA-256 keyed calibration profile persistence with atomic writes and geometry validation."""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import ValidationError

from pea.config import PeaConfig, resolve_confined_path
from pea.schemas import BBoxPx, CalibrationProfile, PointPx

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _normalize_sha256_key(sha256: str) -> str:
    cleaned = sha256.strip().lower()
    if not _SHA256_RE.match(cleaned):
        raise ValueError(f"Expected 64-char hex SHA-256 profile key; got {sha256!r}")
    return cleaned


def _check_point_in_bounds(
    point: PointPx,
    max_x: int,
    max_y: int,
    label: str,
) -> None:
    x, y = point
    if not (0 <= x <= max_x and 0 <= y <= max_y):
        raise ValueError(
            f"Profile point {label}={point} falls outside stage+header bounds "
            f"(0..{max_x}, 0..{max_y})"
        )


def _check_bbox_in_bounds(
    bbox: BBoxPx,
    max_x: int,
    max_y: int,
    label: str,
) -> None:
    x1, y1, x2, y2 = bbox
    if not (0 <= x1 < x2 <= max_x and 0 <= y1 < y2 <= max_y):
        raise ValueError(
            f"Profile ROI {label}={bbox} falls outside stage+header bounds "
            f"(0..{max_x}, 0..{max_y})"
        )


def validate_profile_geometry(
    profile: CalibrationProfile,
    *,
    expected_stage: tuple[int, int] | None = None,
) -> None:
    """Verify stage dimensions and ensure every declared point/ROI fits within the stage."""
    if expected_stage is not None:
        exp_w, exp_h = expected_stage
        if (profile.stage_width, profile.stage_height) != (exp_w, exp_h):
            raise ValueError(
                f"CalibrationProfile stage {(profile.stage_width, profile.stage_height)} "
                f"does not match expected stage {(exp_w, exp_h)}"
            )

    max_x = profile.stage_width
    max_y = profile.stage_height + profile.header_height_px

    if profile.blur_xy is not None:
        _check_point_in_bounds(profile.blur_xy, max_x, max_y, "blur_xy")

    for name, ctrl in profile.controls.items():
        for attr in ("input_xy", "up_xy", "down_xy", "click_xy", "blur_xy"):
            pt = getattr(ctrl, attr)
            if pt is not None:
                _check_point_in_bounds(pt, max_x, max_y, f"controls[{name!r}].{attr}")
        if ctrl.readout_roi_px is not None:
            _check_bbox_in_bounds(
                ctrl.readout_roi_px, max_x, max_y, f"controls[{name!r}].readout_roi_px"
            )

    for name, readout in profile.readouts.items():
        _check_bbox_in_bounds(readout.roi_px, max_x, max_y, f"readouts[{name!r}].roi_px")


class ProfileStore:
    """Loads and atomically persists SHA-256 keyed ``CalibrationProfile`` JSON files.

    Writes default to gitignored ``profiles/local/<sha256>.json`` and read falls back
    read-only to shared ``profiles/<sha256>.json``.
    """

    def __init__(self, profiles_dir: str | Path) -> None:
        self.profiles_dir: Path = Path(profiles_dir).expanduser().resolve(strict=False)
        self.local_dir: Path = self.profiles_dir / "local"

    @classmethod
    def from_config(cls, config: PeaConfig) -> ProfileStore:
        return cls(config.profiles_dir)

    def local_path(self, sha256: str) -> Path:
        digest = _normalize_sha256_key(sha256)
        return resolve_confined_path(self.local_dir, f"{digest}.json")

    def shared_path(self, sha256: str) -> Path:
        digest = _normalize_sha256_key(sha256)
        return resolve_confined_path(self.profiles_dir, f"{digest}.json")

    def load(
        self,
        sha256: str,
        *,
        expected_stage: tuple[int, int] | None = None,
    ) -> CalibrationProfile | None:
        """Load a profile by SHA-256 (local override first, then read-only shared fallback)."""
        digest = _normalize_sha256_key(sha256)
        candidates = (self.local_path(digest), self.shared_path(digest))

        target: Path | None = None
        for candidate in candidates:
            if candidate.is_file():
                target = candidate
                break

        if target is None:
            return None

        raw = target.read_text(encoding="utf-8")
        profile = CalibrationProfile.model_validate_json(raw)
        if profile.swf_sha256 != digest:
            raise ValueError(
                f"Profile file {target.name} declares swf_sha256={profile.swf_sha256!r}, "
                f"expected {digest!r}"
            )
        validate_profile_geometry(profile, expected_stage=expected_stage)
        return profile

    def save(
        self,
        profile: CalibrationProfile,
        *,
        shared: bool = False,
        expected_stage: tuple[int, int] | None = None,
    ) -> Path:
        """Validate and atomically persist *profile* via ``.tmp`` + ``fsync`` + ``os.replace``."""
        validate_profile_geometry(profile, expected_stage=expected_stage)
        target = (
            self.shared_path(profile.swf_sha256)
            if shared
            else self.local_path(profile.swf_sha256)
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_suffix(".json.tmp")
        payload = profile.model_dump_json(indent=2) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        return target

    def delete_local(self, sha256: str) -> bool:
        """Remove a local profile override if it exists (never touches shared profiles)."""
        target = self.local_path(sha256)
        if target.is_file():
            target.unlink()
            return True
        return False

    def list_profiles(self) -> list[CalibrationProfile]:
        """List all valid profiles (local overrides take precedence over shared files)."""
        merged: dict[str, CalibrationProfile] = {}

        for directory in (self.profiles_dir, self.local_dir):
            if not directory.is_dir():
                continue
            for item in sorted(directory.glob("*.json")):
                stem = item.stem.lower()
                if not _SHA256_RE.match(stem):
                    continue
                try:
                    profile = CalibrationProfile.model_validate_json(item.read_text("utf-8"))
                except (OSError, ValidationError):
                    continue
                if profile.swf_sha256 == stem:
                    merged[stem] = profile

        return list(merged.values())


__all__ = [
    "ProfileStore",
    "validate_profile_geometry",
]
