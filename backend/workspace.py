"""Versioned, validated optional annotations within v1 extension properties.

Source annotations carry source-local coordinates, never alter model geometry,
and follow their containing floor when projects are cloned. Calibration is an
explicit measured correspondence, not image/PDF DPI or guessed scale.
"""
from __future__ import annotations

import math
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from .domain.models import Identifier, Model, Name, Point2, Positive, Project
from .cad_sections import MAX_PROJECT_SECTION_POINTS, SectionSnapshot, SectionParameters, raw_point_count

SHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Calibration(Model):
    source_a: Point2
    source_b: Point2
    distance_mm: Positive
    origin_mm: Point2 = Field(default_factory=lambda: Point2(x=0, y=0))
    y_axis_down: bool = True

    @model_validator(mode="after")
    def distinct_points(self):
        if math.hypot(self.source_b.x - self.source_a.x, self.source_b.y - self.source_a.y) < 1e-6:
            raise ValueError("Точки калибровки должны различаться")
        return self

    def to_model(self, point: Point2) -> Point2:
        dx, dy = self.source_b.x - self.source_a.x, self.source_b.y - self.source_a.y
        denominator = dx * dx + dy * dy
        px, py = point.x - self.source_a.x, point.y - self.source_a.y
        factor = self.distance_mm / denominator
        return Point2(x=self.origin_mm.x + (px * dx + py * dy) * factor,
                      y=self.origin_mm.y + (px * -dy + py * dx) * factor * (-1 if self.y_axis_down else 1))


class SourceMark(Model):
    id: Identifier
    kind: Literal["room", "wall", "opening", "door", "window", "passage", "ignore", "note"]
    label: Name
    points: list[Point2] = Field(min_length=1, max_length=2000)
    note: Annotated[str, Field(max_length=4000)] = ""
    cad_handles: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=1000)
    exact_geometry: bool = True
    closed: bool = False
    height_mm: Positive = 2100
    sill_mm: Annotated[float, Field(ge=0, le=1_000_000, allow_inf_nan=False)] = 0
    model_ids: list[Identifier] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def enough_points(self):
        minimum = {"room": 3, "wall": 2, "opening": 2, "door": 2, "window": 2, "passage": 2, "ignore": 1, "note": 1}[self.kind]
        if len(self.points) < minimum:
            raise ValueError(f"Для разметки {self.kind} нужно не менее {minimum} точек")
        return self


class SourceAttachment(Model):
    source_id: SHA256
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    page: Annotated[int, Field(strict=True, ge=1, le=500)] = 1
    coordinate_space: Literal["preview_pixels_v1", "dxf_world_mm", "dxf_source_units"] = "preview_pixels_v1"
    preview_version: Literal["1"] = "1"
    preview_width_px: Annotated[int, Field(strict=True, ge=1, le=20000)] | None = None
    preview_height_px: Annotated[int, Field(strict=True, ge=1, le=20000)] | None = None
    calibration: Calibration | None = None
    units_override: Literal["mm", "cm", "m", "in", "ft"] | None = None
    unit_factor: Positive | None = None
    backend: Literal["cad_workspace"] | None = None
    layout_name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    requires_calibration: bool = False
    marks: list[SourceMark] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def distinct_marks(self):
        if len({m.id for m in self.marks}) != len(self.marks):
            raise ValueError("Идентификаторы пометок не должны повторяться")
        if self.backend == "cad_workspace" and (self.coordinate_space != "dxf_source_units" or not self.requires_calibration or not self.layout_name):
            raise ValueError("Разметка большого DXF должна сохранять имя пространства, исходные координаты и требование калибровки")
        return self


class SourceMarkup(Model):
    schema_version: Literal["source-markup/1.0"] = "source-markup/1.0"
    sources: list[SourceAttachment] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def distinct_sources(self):
        if len({(s.source_id, s.page) for s in self.sources}) != len(self.sources):
            raise ValueError("Одна страница исходника не должна повторяться на этаже")
        return self


class CadSection(Model):
    id: Identifier
    label: Name
    axis: Literal["x", "y", "z"]
    offset_mm: Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-1e9, le=1e9)]
    component_ids: list[Annotated[int, Field(strict=True, ge=1, le=256)]] | None = Field(default=None, min_length=1, max_length=256)
    note: Annotated[str, Field(max_length=4000)] = ""
    target_space_id: Identifier | None = None
    target_zone_id: Identifier | None = None
    geometry_snapshot: SectionSnapshot | None = None

    @model_validator(mode="after")
    def distinct_components(self):
        if self.component_ids is not None and len(set(self.component_ids)) != len(self.component_ids):
            raise ValueError("Номера тел сечения не должны повторяться")
        if self.geometry_snapshot is not None:
            request = SectionParameters(axis=self.axis, offset_mm=self.offset_mm, component_ids=self.component_ids)
            if request.canonical() != self.geometry_snapshot.request.canonical():
                raise ValueError("Параметры сечения изменены после построения геометрии. Постройте снимок заново.")
        return self


class CadAsset(Model):
    source_id: SHA256
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    format: Literal["step"]
    sections: list[CadSection] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def distinct_sections(self):
        if len({section.id for section in self.sections}) != len(self.sections):
            raise ValueError("Идентификаторы сохранённых сечений не должны повторяться")
        if any(section.geometry_snapshot is not None and section.geometry_snapshot.source_id != self.source_id for section in self.sections):
            raise ValueError("Геометрия сечения относится к другому STEP-исходнику")
        return self


def validate_cad_assets(value) -> list[CadAsset]:
    point_count = 0
    if isinstance(value, list):
        for asset in value:
            if isinstance(asset, dict) and isinstance(asset.get("sections"), list):
                for section in asset["sections"]:
                    if isinstance(section, dict) and isinstance(section.get("geometry_snapshot"), dict):
                        point_count += raw_point_count(section["geometry_snapshot"].get("result"))
                        if point_count > MAX_PROJECT_SECTION_POINTS:
                            raise ValueError("Сохранённые сечения проекта превышают 120 000 точек. Разделите проект.")
    assets = TypeAdapter(Annotated[list[CadAsset], Field(max_length=50)]).validate_python(value)
    if len({asset.source_id for asset in assets}) != len(assets):
        raise ValueError("Одна STEP-модель не должна повторяться в списке проекта")
    if sum(section.geometry_snapshot.result.point_count for asset in assets for section in asset.sections if section.geometry_snapshot) > MAX_PROJECT_SECTION_POINTS:
        raise ValueError("Сохранённые сечения проекта превышают 120 000 точек. Удалите ненужные сечения или разделите проект.")
    return assets


class ConstructionGuide(Model):
    id: Identifier
    name: Name = "Вспомогательная линия"
    start: Point2
    end: Point2
    locked: bool = False

    @model_validator(mode="after")
    def distinct_endpoints(self):
        if math.hypot(self.end.x - self.start.x, self.end.y - self.start.y) <= 1e-6:
            raise ValueError("Вспомогательная линия должна иметь две разные точки")
        return self


class EditorGuides(Model):
    schema_version: Literal["editor-guides/1.0"] = "editor-guides/1.0"
    guides: list[ConstructionGuide] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def distinct_ids(self):
        if len({guide.id for guide in self.guides}) != len(self.guides):
            raise ValueError("Идентификаторы вспомогательных линий должны различаться")
        return self


def validate_project_properties_budget(project: Project, *, derived_assemblies=False):
    """Keep portable extensions within the transport envelope before native work."""
    # Complete walls store checks for every actual occurrence, not only the
    # smaller local detail snapshots.  Their reserved extension is validated
    # separately below; ordinary projects retain their existing 20-MiB bound.
    if derived_assemblies:
        # This option is set only by the server's saved-project document route.
        # Request/import/save envelopes keep their original portable bounds.
        derived_keys={"mount_assemblies","whole_wall_assemblies"}
        source=project.model_copy()
        source.properties={key:value for key,value in project.properties.items() if key not in derived_keys}
        validate_project_properties_budget(source)
        derived={key:value for key,value in project.properties.items() if key in derived_keys}
        if len(json.dumps(derived,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode())>256*1024**2:
            raise ValueError("Кэш сборок проекта превышает 256 МиБ")
        return
    limit_mib = 48 if "whole_wall_assemblies" in project.properties else 20
    if len(json.dumps(project.properties,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode("utf-8"))>limit_mib*1024*1024:
        raise ValueError(f"Свойства и сохранённые CAD-сборки проекта превышают {limit_mib} МиБ. Разделите проект или удалите ненужные сборки.")


def validate_workspace(project: Project, *, derived_assemblies=False, resolve_whole_walls=False, resolve_node_records=False):
    """Reject invalid reserved extensions without touching user properties.

    Document generation also needs the active/stale wall classification. Resolve
    it at this validation boundary so its own full record validation is the one
    validation of this build, not a second decompression of every saved STEP.
    Nothing is cached or accepted as previously trusted input across calls.
    """
    validate_project_properties_budget(project,derived_assemblies=derived_assemblies)
    if "special_installations" in project.properties or "special_assemblies" in project.properties:
        from .special_assemblies import validate_special
        validate_special(project,derived_assemblies=derived_assemblies)
    node_records = None
    if "mount_assemblies" in project.properties or resolve_node_records:
        if derived_assemblies:
            from .server_node_records import validate_server_nodes
            node_records = validate_server_nodes(project)
        else:
            from .mount_assemblies import validate_assemblies
            node_records = validate_assemblies(project)
    if "whole_wall_assemblies" in project.properties and not resolve_whole_walls:
        if derived_assemblies:
            from .server_wall_records import validate_server_whole_walls
            validate_server_whole_walls(project)
        else:
            from .whole_wall_assemblies import validate_whole_walls
            validate_whole_walls(project)
    if "whole_wall_settings" in project.properties:
        from .whole_wall_assemblies import validate_wall_settings
        validate_wall_settings(project)
    if "wall_mounting" in project.properties:
        from .wall_mount import validate_wall_mounting
        validate_wall_mounting(project)
    if "cad_assets" in project.properties:
        assets = validate_cad_assets(project.properties["cad_assets"])
        spaces = {space.id for b in project.buildings for f in b.floors for space in f.spaces}
        zones = {zone.id for b in project.buildings for f in b.floors for zone in f.cladding_zones}
        for asset in assets:
            for section in asset.sections:
                if section.target_space_id is not None and section.target_space_id not in spaces:
                    raise ValueError("Помещение, к которому привязано STEP-сечение, не найдено")
                if section.target_zone_id is not None and section.target_zone_id not in zones:
                    raise ValueError("Зона, к которой привязано STEP-сечение, не найдена")
    if "vendor_design" in project.properties:
        from .vendor_design import validate_vendor_design
        result = validate_vendor_design(project)
        if not result["valid"]:
            raise ValueError("Ссылки на материалы не соответствуют схеме: " + str(result["issues"][:3]))
    if "node_catalog" in project.properties:
        from .documents.catalog import preflight_catalog
        result = preflight_catalog(project.properties["node_catalog"])
        if not result["valid"]:
            raise ValueError("Библиотека узлов не соответствует схеме: " + str(result["issues"][:3]))
    for building in project.buildings:
        for floor in building.floors:
            if "source_markup" in floor.properties:
                SourceMarkup.model_validate(floor.properties["source_markup"])
            if "editor_guides" in floor.properties:
                EditorGuides.model_validate(floor.properties["editor_guides"])
    if resolve_whole_walls:
        from .whole_wall_assemblies import whole_wall_usage
        result = whole_wall_usage(project, server_owned=derived_assemblies)
        return (*result, node_records) if resolve_node_records else result
    if resolve_node_records:
        return node_records


def validate_source_references(project: Project, source_store):
    """HTTP ingestion guard; pure JSON/model code remains portable offline.

    The original must be uploaded before attaching annotations to it. Byte hashes
    are checked, not just filesystem existence. Duplicate references cost once.
    """
    checked = set()
    for asset in validate_cad_assets(project.properties.get("cad_assets", [])):
        try:
            if Path(source_store.get_record(asset.source_id).filename).suffix.lower() not in {".step", ".stp"}:
                raise ValueError(f"Исходник {asset.filename} не является сохранённым STEP-файлом")
            original = source_store.get_bytes(asset.source_id)
            if any(section.geometry_snapshot is not None and section.geometry_snapshot.result.byte_length != len(original) for section in asset.sections):
                raise ValueError(f"Размер исходника в снимке сечения не соответствует файлу {asset.filename}")
        except FileNotFoundError as exc:
            raise ValueError(f"Отсутствует исходник {asset.filename}. Сначала загрузите STEP или восстановите полную резервную копию.") from exc
        checked.add(asset.source_id)
    for building in project.buildings:
        for floor in building.floors:
            markup = floor.properties.get("source_markup")
            references = [(source.source_id, source.filename) for source in SourceMarkup.model_validate(markup).sources] if markup else []
            imported = floor.properties.get("source_import")
            if isinstance(imported, dict) and "source_id" in imported:
                source_id = imported["source_id"]
                if not isinstance(source_id, str) or len(source_id) != 64 or any(c not in "0123456789abcdef" for c in source_id):
                    raise ValueError("source_import.source_id должен содержать SHA-256 исходника")
                references.append((source_id, imported.get("filename", "DXF")))
            for source_id, filename in references:
                if source_id not in checked:
                    try:
                        source_store.get_bytes(source_id)
                    except FileNotFoundError as exc:
                        raise ValueError(f"Отсутствует исходник {filename}. Сначала загрузите оригинал или восстановите полную резервную копию.") from exc
                    checked.add(source_id)
