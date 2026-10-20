"""Portable raw-Project JSON import/export for the canonical project model."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from pydantic import ValidationError

from ..domain.models import Project, SCHEMA_VERSION


FORMAT_VERSION = SCHEMA_VERSION


class UnsupportedJSONVersionError(ValueError):
    """The project JSON uses an unsupported schema version."""


class InvalidProjectJSONError(ValueError):
    """The raw project JSON does not satisfy the canonical model."""


def _reject_non_finite(token: str):
    raise InvalidProjectJSONError(f"Non-standard JSON number {token!r} is not allowed")


def _atomic_write(path: Path, data: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(path)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        if overwrite:
            os.replace(temporary, path)
        else:
            try:
                # Unlike an exists-then-replace sequence, linking cannot
                # overwrite a target created by another process in the race.
                os.link(temporary, path)
            except FileExistsError:
                raise
            else:
                temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def write_project(
    project: Project,
    path: str | Path,
    overwrite: bool = False,
) -> None:
    """Atomically write the canonical raw Project JSON object.

    Existing files are preserved unless ``overwrite=True`` is explicit.
    """

    if not isinstance(path, (str, Path)):
        raise TypeError("path must be str or pathlib.Path")
    try:
        validated = Project.model_validate(project.model_dump(mode="json"))
    except (ValidationError, ValueError, TypeError) as exc:
        raise InvalidProjectJSONError("project does not satisfy the Project contract") from exc

    encoded = (
        json.dumps(
            validated.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write(Path(path).expanduser(), encoded, overwrite=overwrite)


def load_project(path: str | Path) -> Project:
    """Load and validate a canonical raw Project JSON object.

    Missing/zero versions are rejected; no undocumented v0 migration is
    attempted.  Future versions are likewise rejected explicitly.
    """

    if not isinstance(path, (str, Path)):
        raise TypeError("path must be str or pathlib.Path")
    source = Path(path).expanduser()
    try:
        document = json.loads(
            source.read_text(encoding="utf-8"),
            parse_constant=_reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise InvalidProjectJSONError(f"Invalid JSON in {source}") from exc

    if not isinstance(document, dict):
        raise InvalidProjectJSONError("Project JSON root must be an object")
    version = document.get("schema_version")
    if not isinstance(version, str):
        raise UnsupportedJSONVersionError(
            f"Project JSON schema_version must be {SCHEMA_VERSION!r}"
        )
    if version != FORMAT_VERSION:
        raise UnsupportedJSONVersionError(
            f"Project JSON schema version {version!r} is unsupported; "
            f"supported version is {FORMAT_VERSION!r}"
        )

    try:
        return Project.model_validate(document)
    except (ValidationError, ValueError, TypeError) as exc:
        raise InvalidProjectJSONError("Project JSON payload violates the Project contract") from exc
