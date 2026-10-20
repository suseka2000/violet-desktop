"""Portable bounded STEP section snapshots: integrity, not design approval.

The original SHA and exact cutting request travel with the sampled geometry.
Digests detect accidental editing; they do not authenticate an imported JSON
file or replace a designer's review. No materials or hatching are inferred.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .domain.models import Model, Name, Number

SHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
BodyId = Annotated[int, Field(strict=True, ge=1, le=256)]
Count = Annotated[int, Field(strict=True, ge=0, le=60_000)]
WarningText = Annotated[str, Field(min_length=1, max_length=2000)]
Point = Annotated[list[Number], Field(min_length=2, max_length=2)]
Path = Annotated[list[Point], Field(min_length=2, max_length=2049)]
MAX_PROJECT_SECTION_POINTS = 120_000


def raw_point_count(result, maximum=60_000):
    """Reject excessive nested arrays before Pydantic copies every coordinate."""
    count = 0
    if isinstance(result, SectionResult):
        return result.point_count
    if isinstance(result, dict) and isinstance(result.get("components"), list):
        for component in result["components"]:
            if isinstance(component, dict) and isinstance(component.get("paths"), list):
                for path in component["paths"]:
                    if isinstance(path, list):
                        count += len(path)
                        if count > maximum:
                            raise ValueError(f"Сечения превышают лимит {maximum:,} точек".replace(",", " "))
    return count


class SectionParameters(Model):
    axis: Literal["x", "y", "z"]
    offset_mm: Number
    component_ids: list[BodyId] | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_components(self):
        if self.component_ids is not None and len(set(self.component_ids)) != len(self.component_ids):
            raise ValueError("Номера тел сечения не должны повторяться")
        return self

    def canonical(self):
        return {"axis": self.axis, "offset_mm": float(self.offset_mm),
                "component_ids": sorted(self.component_ids) if self.component_ids is not None else None}


class SectionBounds(Model):
    min: Point
    max: Point
    size: Point


class SectionComponent(Model):
    id: BodyId
    label: Name
    edge_count: Count
    wire_count: Count
    closed_wire_count: Count
    open_wire_count: Count
    paths: list[Path] = Field(max_length=12_000)
    warnings: list[WarningText] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def coherent_counts(self):
        if self.wire_count != self.closed_wire_count + self.open_wire_count:
            raise ValueError("Число контуров сечения не соответствует сумме замкнутых и открытых контуров")
        if len(self.paths) > self.edge_count or self.wire_count > self.edge_count:
            raise ValueError("Число рёбер сечения не соответствует контурам")
        return self


class SectionResult(Model):
    schema_: Literal["step-section/1"] = Field(alias="schema")
    source_id: SHA256
    byte_length: Annotated[int, Field(strict=True, ge=1, le=64 * 1024**2)]
    units: Literal["mm"]
    occt_version: Annotated[str, Field(min_length=1, max_length=100)]
    engineering_approved: Literal[False]
    axis: Literal["x", "y", "z"]
    offset_mm: Number
    projection_axes: list[Literal["x", "y", "z"]] = Field(min_length=2, max_length=2)
    components: list[SectionComponent] = Field(min_length=1, max_length=256)
    bounds_mm: SectionBounds | None
    point_count: Count
    warnings: list[WarningText] = Field(max_length=1024)
    approximate: Literal[True]
    deflection_mm: Annotated[float, Field(strict=True, gt=0, le=1, allow_inf_nan=False)]
    connection_tolerance_mm: Annotated[float, Field(strict=True, gt=0, le=1, allow_inf_nan=False)]

    @model_validator(mode="before")
    @classmethod
    def early_point_budget(cls, value):
        raw_point_count(value)
        return value

    @model_validator(mode="after")
    def coherent_geometry(self):
        if self.projection_axes != [axis for axis in "xyz" if axis != self.axis]:
            raise ValueError("Оси проекции не соответствуют плоскости сечения")
        if len({c.id for c in self.components}) != len(self.components):
            raise ValueError("Тела в снимке сечения не должны повторяться")
        actual_count = sum(len(path) for c in self.components for path in c.paths)
        if actual_count != self.point_count or actual_count > 60_000:
            raise ValueError("Число точек снимка сечения неверно или превышает 60 000")
        if actual_count == 0:
            if self.bounds_mm is not None:
                raise ValueError("Пустое сечение не должно содержать габарит")
        else:
            if self.bounds_mm is None:
                raise ValueError("Сечение должно содержать габарит")
            points = [point for c in self.components for path in c.paths for point in path]
            minimum = [min(p[i] for p in points) for i in range(2)]
            maximum = [max(p[i] for p in points) for i in range(2)]
            for actual, expected in zip(self.bounds_mm.min + self.bounds_mm.max + self.bounds_mm.size,
                                        minimum + maximum + [maximum[i] - minimum[i] for i in range(2)]):
                if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-7):
                    raise ValueError("Габарит снимка не соответствует его точкам")
        return self

    def portable(self):
        return self.model_dump(mode="json", by_alias=True)


def geometry_digest(result: SectionResult) -> str:
    # JavaScript JSON.stringify(-0) emits 0. CAD kernels can produce -0.0,
    # which is geometrically identical and must survive a browser save.
    def normalize(value):
        if isinstance(value, float) and value == 0:
            return 0.0
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value
    return hashlib.sha256(json.dumps(normalize(result.portable()), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class SectionSnapshot(Model):
    schema_version: Literal["cad-section-snapshot/1.0"] = "cad-section-snapshot/1.0"
    source_id: SHA256
    request: SectionParameters
    result: SectionResult
    geometry_sha256: SHA256

    @model_validator(mode="after")
    def bound_to_request(self):
        if self.source_id != self.result.source_id:
            raise ValueError("Исходник снимка не соответствует геометрии сечения")
        if self.request.axis != self.result.axis or self.request.offset_mm != self.result.offset_mm:
            raise ValueError("Плоскость снимка не соответствует геометрии сечения")
        actual_ids = sorted(c.id for c in self.result.components)
        expected_ids = sorted(self.request.component_ids) if self.request.component_ids is not None else list(range(1, len(actual_ids) + 1))
        if actual_ids != expected_ids:
            raise ValueError("Выбранные тела снимка не соответствуют геометрии сечения")
        if geometry_digest(self.result) != self.geometry_sha256:
            raise ValueError("Контрольная сумма геометрии сечения не совпадает")
        return self


def make_section_snapshot(source_id, request, result) -> dict:
    geometry = SectionResult.model_validate(result)
    parameters = SectionParameters.model_validate(request)
    return SectionSnapshot(source_id=source_id, request=parameters.canonical(), result=geometry,
                           geometry_sha256=geometry_digest(geometry)).model_dump(mode="json", by_alias=True)


def section_readiness(project):
    from .workspace import validate_cad_assets
    sections, issues = [], []
    for ai, asset in enumerate(validate_cad_assets(project.properties.get("cad_assets", []))):
        for si, section in enumerate(asset.sections):
            snapshot = section.geometry_snapshot
            status = "geometry_missing" if snapshot is None else "empty" if not snapshot.result.point_count else "unreviewed"
            sections.append({"source_id": asset.source_id, "filename": asset.filename,
                             "section_id": section.id, "label": section.label, "status": status,
                             "geometry_sha256": snapshot.geometry_sha256 if snapshot else None,
                             "target_space_id": section.target_space_id, "target_zone_id": section.target_zone_id,
                             "engineering_approved": False})
            code, message = {
                "geometry_missing": ("CAD_SECTION_GEOMETRY_MISSING", "Сохранены только параметры STEP-сечения. Постройте и сохраните геометрию для документации."),
                "empty": ("CAD_SECTION_EMPTY", "Плоскость STEP-сечения не пересекает выбранные тела. Геометрия на листе отсутствует."),
                "unreviewed": ("CAD_SECTION_UNREVIEWED", "STEP-сечение сохранено как приближённая геометрия. Назначение, материалы и монтажное решение не утверждены."),
            }[status]
            issues.append({"code": code, "severity": "warning", "message": f"{section.label}: {message}",
                           "path": f"/properties/cad_assets/{ai}/sections/{si}", "entity_ids": [section.id], "details": {"source_id": asset.source_id}})
    return {"sections": sections, "issues": issues, "engineering_approved": False}
