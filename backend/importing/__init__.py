"""Safe local source ingestion and deliberately narrow DXF conversion."""

from ._common import MAX_SOURCE_BYTES
from .dxf import DEFAULT_LAYER, convert_dxf, inspect_dxf
from .cad import convert_cad_source, import_capabilities
from .errors import (
    AmbiguousTopologyError,
    ConvertedGeometryError,
    ImportSourceError,
    MalformedSourceError,
    RepairRequiredError,
    SelectionError,
    SourceIntegrityError,
    SourceLimitError,
    SourceNotFoundError,
    UnitError,
    UnsafeSourceNameError,
    UnsupportedGeometryError,
    UnsupportedSourceError,
)
from .inspection import inspect_source
from .records import ImportReport, ImportResult, SourceRecord
from .source_store import SourceStore

__all__ = [
    "AmbiguousTopologyError",
    "ConvertedGeometryError",
    "DEFAULT_LAYER",
    "ImportReport",
    "ImportResult",
    "ImportSourceError",
    "MAX_SOURCE_BYTES",
    "MalformedSourceError",
    "RepairRequiredError",
    "SelectionError",
    "SourceIntegrityError",
    "SourceLimitError",
    "SourceNotFoundError",
    "SourceRecord",
    "SourceStore",
    "UnitError",
    "UnsafeSourceNameError",
    "UnsupportedGeometryError",
    "UnsupportedSourceError",
    "convert_dxf",
    "convert_cad_source",
    "import_capabilities",
    "inspect_dxf",
    "inspect_source",
]
