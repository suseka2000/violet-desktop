"""Typed records returned by the import boundary."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from backend.domain.models import Project, ValidationReport


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class SourceRecord(FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    source_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str = Field(min_length=1, max_length=255)
    byte_length: int = Field(ge=1)
    media_type: str = Field(min_length=1, max_length=100)
    stored_at: AwareDatetime


class ImportReport(FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    source_format: Literal["dxf", "dwg"] = "dxf"
    source_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_filename: str
    selected_handles: tuple[str, ...]
    imported_space_count: int = Field(ge=0)
    imported_wall_count: int = Field(ge=0)
    shared_wall_count: int = Field(ge=0)
    split_segment_count: int = Field(ge=0)
    ignored_entity_count: int = Field(ge=0)
    ignored_entity_types: dict[str, int] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    missing_semantics: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class ImportResult(FrozenModel):
    project: Project
    report: ImportReport
    validation: ValidationReport
    source_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    imported_handles: tuple[str, ...]
    source_unit_code: int = Field(ge=0)
    source_units: str
    millimetre_factor: float = Field(gt=0, allow_inf_nan=False)
