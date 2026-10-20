"""Public exceptions for bounded, local source ingestion."""

from __future__ import annotations

from typing import Any


class ImportSourceError(ValueError):
    """Base class for rejected source bytes or import instructions.

    ``code`` is stable enough for an HTTP adapter to map the failure without
    parsing the human-readable message.  This package itself performs no HTTP
    work and never uploads a source anywhere.
    """

    code = "IMPORT_SOURCE_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


class SourceLimitError(ImportSourceError):
    code = "SOURCE_LIMIT_EXCEEDED"


class UnsafeSourceNameError(ImportSourceError):
    code = "UNSAFE_SOURCE_NAME"


class UnsupportedSourceError(ImportSourceError):
    code = "UNSUPPORTED_SOURCE_FORMAT"


class MalformedSourceError(ImportSourceError):
    code = "MALFORMED_SOURCE"


class RepairRequiredError(MalformedSourceError):
    code = "SOURCE_REPAIR_REQUIRED"


class UnitError(ImportSourceError):
    code = "SOURCE_UNITS_INVALID"


class SelectionError(ImportSourceError):
    code = "SOURCE_SELECTION_INVALID"


class UnsupportedGeometryError(ImportSourceError):
    code = "UNSUPPORTED_SELECTED_GEOMETRY"


class AmbiguousTopologyError(ImportSourceError):
    code = "AMBIGUOUS_SOURCE_TOPOLOGY"


class ConvertedGeometryError(ImportSourceError):
    code = "CONVERTED_GEOMETRY_INVALID"

    def __init__(self, message: str, *, report, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.report = report


class SourceNotFoundError(FileNotFoundError):
    """A content-addressed source is not present in the selected scope."""

    code = "SOURCE_NOT_FOUND"


class SourceIntegrityError(ImportSourceError):
    code = "SOURCE_INTEGRITY_ERROR"
