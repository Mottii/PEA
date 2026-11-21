"""Run workspace lifecycle, atomic status persistence, and SHA-256 provenance ledger."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pea.config import PathConfinementError, PeaConfig, resolve_confined_path
from pea.schemas import (
    TERMINAL_RUN_PHASES,
    ProvenanceEntry,
    ProvenanceSource,
    ProvenanceVerificationResult,
    RunPhase,
    RunStatus,
)

STANDARD_RUN_SUBDIRS: tuple[str, ...] = (
    "evidence",
    "crops",
    "figures",
    "reports",
    "pdf_pages",
)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HASH_CHUNK_BYTES = 65_536


def generate_run_id(now: datetime | None = None) -> str:
    """Generate a unique sortable run ID: ``YYYY-MM-DDTHH-MM-SS-mmmZ-<8hex>``."""
    ts = (now or datetime.now(UTC)).astimezone(UTC)
    millis = ts.microsecond // 1000
    stamp = f"{ts:%Y-%m-%dT%H-%M-%S}-{millis:03d}Z"
    return f"{stamp}-{secrets.token_hex(4)}"


def sha256_file(path: Path) -> tuple[str, int]:
    """Compute ``(sha256_hex, size_bytes)`` for *path* via streaming reads."""
    digest = hashlib.sha256()
    total_bytes = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total_bytes += len(chunk)
    return digest.hexdigest(), total_bytes


class RunWorkspace:
    """Filesystem handle and provenance ledger for a single PEA execution run."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.root: Path = root.resolve(strict=False)
        self.run_id: str = run_id

    @property
    def status_path(self) -> Path:
        return self.root / "status.json"

    @property
    def provenance_path(self) -> Path:
        return self.root / "provenance.jsonl"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def crops_dir(self) -> Path:
        return self.root / "crops"

    @property
    def figures_dir(self) -> Path:
        return self.root / "figures"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def pdf_pages_dir(self) -> Path:
        return self.root / "pdf_pages"

    def ensure_dirs(self) -> None:
        """Create the run directory and all standard subdirectories idempotently."""
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in STANDARD_RUN_SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def resolve_path(self, relpath: str | Path) -> Path:
        """Resolve *relpath* strictly inside this run directory."""
        return resolve_confined_path(self.root, relpath)

    def write_status_atomic(self, status: RunStatus) -> None:
        """Atomically persist ``status.json`` via ``status.tmp`` + ``fsync`` + ``os.replace``."""
        self.root.mkdir(parents=True, exist_ok=True)
        tmp_path = self.root / "status.tmp"
        payload = status.model_dump_json(indent=2) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.status_path)

    def read_status(self) -> RunStatus:
        """Load and validate ``status.json`` for this run."""
        raw = self.status_path.read_text(encoding="utf-8")
        return RunStatus.model_validate_json(raw)

    def update_status(
        self,
        *,
        phase: RunPhase | None = None,
        message: str | None = None,
        error: str | None = None,
        source: ProvenanceSource | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> RunStatus:
        """Update fields on ``status.json`` atomically and return the new ``RunStatus``."""
        current = self.read_status()
        merged_metadata = dict(current.metadata)
        if metadata_updates:
            merged_metadata.update(metadata_updates)

        updated = current.model_copy(
            update={
                "phase": phase if phase is not None else current.phase,
                "updated_at": datetime.now(UTC),
                "message": message if message is not None else current.message,
                "error": error if error is not None else current.error,
                "source": source if source is not None else current.source,
                "progress_current": (
                    progress_current
                    if progress_current is not None
                    else current.progress_current
                ),
                "progress_total": (
                    progress_total if progress_total is not None else current.progress_total
                ),
                "metadata": merged_metadata,
            }
        )
        self.write_status_atomic(updated)
        return updated

    def record_artifact(
        self,
        relpath: str | Path,
        kind: str,
        metadata: dict[str, Any] | None = None,
        *,
        source: ProvenanceSource = "simulator_captured",
    ) -> ProvenanceEntry:
        """Hash an artifact confined inside this run and append an entry to ``provenance.jsonl``."""
        resolved = self.resolve_path(relpath)
        if not resolved.exists():
            raise FileNotFoundError(f"Artifact does not exist inside run {self.run_id}: {relpath}")
        if not resolved.is_file():
            raise ValueError(f"Artifact must be a regular file, not a directory: {relpath}")

        rel_posix = resolved.relative_to(self.root).as_posix()
        digest, size_bytes = sha256_file(resolved)
        entry = ProvenanceEntry(
            timestamp=datetime.now(UTC),
            kind=kind,
            relpath=rel_posix,
            sha256=digest,
            size_bytes=size_bytes,
            source=source,
            details=dict(metadata) if metadata else {},
        )

        with self.provenance_path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def read_provenance(self) -> list[ProvenanceEntry]:
        """Parse all entries from ``provenance.jsonl``."""
        if not self.provenance_path.exists():
            return []
        entries: list[ProvenanceEntry] = []
        with self.provenance_path.open("r", encoding="utf-8") as handle:
            for line_num, raw_line in enumerate(handle, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    entries.append(ProvenanceEntry.model_validate_json(stripped))
                except ValidationError as exc:
                    raise ValueError(
                        f"Malformed provenance entry at line {line_num} in {self.provenance_path}"
                    ) from exc
        return entries

    def verify_provenance(self) -> ProvenanceVerificationResult:
        """Cross-check every entry in ``provenance.jsonl`` against bytes on disk."""
        if not self.provenance_path.exists():
            return ProvenanceVerificationResult(ok=True, checked_entries=0, errors=[])

        errors: list[str] = []
        checked = 0
        with self.provenance_path.open("r", encoding="utf-8") as handle:
            for line_num, raw_line in enumerate(handle, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    entry = ProvenanceEntry.model_validate_json(stripped)
                except ValidationError as exc:
                    msg = exc.errors()[0]["msg"]
                    errors.append(f"Line {line_num}: invalid provenance JSON ({msg})")
                    continue

                checked += 1
                try:
                    resolved = self.resolve_path(entry.relpath)
                except PathConfinementError as exc:
                    errors.append(f"Line {line_num}: path escape for {entry.relpath!r} ({exc})")
                    continue

                if not resolved.exists() or not resolved.is_file():
                    errors.append(f"Line {line_num}: missing artifact {entry.relpath!r}")
                    continue

                actual_sha, actual_size = sha256_file(resolved)
                if actual_size != entry.size_bytes:
                    errors.append(
                        f"Line {line_num}: size mismatch for {entry.relpath!r} "
                        f"(expected {entry.size_bytes}, actual {actual_size})"
                    )
                if actual_sha != entry.sha256:
                    errors.append(
                        f"Line {line_num}: sha256 mismatch for {entry.relpath!r} "
                        f"(expected {entry.sha256}, actual {actual_sha})"
                    )

        return ProvenanceVerificationResult(
            ok=len(errors) == 0,
            checked_entries=checked,
            errors=errors,
        )


class WorkspaceManager:
    """Manages run workspaces under ``runs_dir`` with startup crash recovery."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir: Path = Path(runs_dir).expanduser().resolve(strict=False)

    @classmethod
    def from_config(cls, config: PeaConfig) -> WorkspaceManager:
        return cls(config.runs_dir)

    def _validate_run_id(self, run_id: str) -> str:
        if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
            raise PathConfinementError(f"Invalid run_id: {run_id!r}")
        if not _RUN_ID_RE.match(run_id):
            raise PathConfinementError(f"run_id contains disallowed characters: {run_id!r}")
        return run_id

    def find_by_request_id(self, request_id: str) -> RunWorkspace | None:
        """Return an existing ``RunWorkspace`` matching *request_id*, if any."""
        if not self.runs_dir.is_dir():
            return None
        for status in self.list_runs():
            if status.request_id == request_id:
                return self.get_run(status.run_id)
        return None

    def create_run(
        self,
        *,
        request_id: str | None = None,
        label: str | None = None,
        source: ProvenanceSource | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunWorkspace:
        """Create a new run workspace or reuse an existing one for ``request_id``."""
        self.runs_dir.mkdir(parents=True, exist_ok=True)

        if request_id is not None:
            existing = self.find_by_request_id(request_id)
            if existing is not None:
                return existing

        now = datetime.now(UTC)
        run_id = generate_run_id(now)
        run_root = resolve_confined_path(self.runs_dir, run_id)
        workspace = RunWorkspace(run_root, run_id)
        workspace.ensure_dirs()

        status = RunStatus(
            run_id=run_id,
            phase="created",
            created_at=now,
            updated_at=now,
            request_id=request_id,
            label=label,
            source=source,
            metadata=dict(metadata) if metadata else {},
        )
        workspace.write_status_atomic(status)
        return workspace

    def get_run(self, run_id: str) -> RunWorkspace:
        """Load an existing run workspace by ID (enforcing path confinement)."""
        validated_id = self._validate_run_id(run_id)
        run_root = resolve_confined_path(self.runs_dir, validated_id)
        if not run_root.is_dir():
            raise FileNotFoundError(f"Run workspace not found: {validated_id}")
        return RunWorkspace(run_root, validated_id)

    def list_runs(self) -> list[RunStatus]:
        """List all valid runs sorted newest-first by ``created_at`` (then ``run_id``)."""
        if not self.runs_dir.is_dir():
            return []
        statuses: list[RunStatus] = []
        for child in self.runs_dir.iterdir():
            if not child.is_dir():
                continue
            status_file = child / "status.json"
            if not status_file.is_file():
                continue
            try:
                statuses.append(RunStatus.model_validate_json(status_file.read_text("utf-8")))
            except (OSError, ValidationError):
                continue

        statuses.sort(key=lambda s: (s.created_at, s.run_id), reverse=True)
        return statuses

    def recover_interrupted_runs(self) -> list[RunStatus]:
        """Mark any non-terminal run (``created`` / ``running``) as ``interrupted``.

        Leaves terminal runs (``complete``, ``failed``, ``cancelled``, ``interrupted``)
        and all partial evidence artifacts completely untouched.
        """
        recovered: list[RunStatus] = []
        for status in self.list_runs():
            if status.phase in TERMINAL_RUN_PHASES:
                continue
            workspace = self.get_run(status.run_id)
            prior = f" ({status.message})" if status.message else ""
            updated = workspace.update_status(
                phase="interrupted",
                message=(
                    f"Recovered non-terminal run from phase '{status.phase}' on startup{prior}; "
                    "partial evidence preserved."
                ),
            )
            recovered.append(updated)
        return recovered


__all__ = [
    "STANDARD_RUN_SUBDIRS",
    "RunWorkspace",
    "WorkspaceManager",
    "generate_run_id",
    "sha256_file",
]
