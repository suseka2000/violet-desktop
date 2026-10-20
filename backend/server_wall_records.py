"""Validated server-owned wall results, without portable-album aggregate quotas.

Only background jobs and saved-ID building assembly call this module. Imported
projects/request bodies still use WholeWallCollection and its transport limits.
Every individual STEP, fingerprint, fastening and geometry proof is validated by
the unchanged WholeWallRecord model; only aggregate count/bytes are unbounded.
"""
from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator

from .domain.models import Model
from .whole_wall_assemblies import WholeWallRecord


class ServerWallCollection(Model):
    schema_version: Literal["whole-wall-assemblies/1.0"] = "whole-wall-assemblies/1.0"
    items: list[WholeWallRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        keys = [(record.wall_id, record.face_id) for record in self.items]
        if len(set(keys)) != len(keys):
            raise ValueError("Для стороны стены допускается одна текущая объёмная сборка")
        return self


def validate_server_whole_walls(project):
    """Strictly validate each saved/background record once for this operation."""
    return ServerWallCollection.model_validate(
        project.properties.get("whole_wall_assemblies", {"items": []}))


def merge_server_records(records, record):
    """Merge already validated native results; never accepts a client dictionary."""
    if not isinstance(record, WholeWallRecord) or any(not isinstance(item, WholeWallRecord) for item in records):
        raise TypeError("Нужны проверенные сборки стен")
    return [item for item in records if (item.wall_id, item.face_id) != (record.wall_id, record.face_id)] + [record]


def create_wall_planner(project, *, mounting=None):
    """One validated catalogue/index per immutable saved-project operation.

    Equivalent to make_plan, without validating every other mounting and the
    entire reference library again for each requested wall. Do not retain this
    closure across edits to the project; jobs already pin an input signature.
    """
    from .wall_framing import plan_wall_framing
    from .wall_mount import WallMounting, validate_wall_mounting, preview_wall_mount, pinned_step_assets
    if mounting is None:
        mounting = validate_wall_mounting(project)
    elif not isinstance(mounting, WallMounting):
        raise TypeError("Нужен проверенный монтаж сохранённого проекта")
    bindings = {(item.wall_id, item.face_id): item for item in mounting.walls} if mounting else {}
    assets = pinned_step_assets(mounting.snapshot) if mounting else []
    walls = {wall.id: wall for building in project.buildings for floor in building.floors for wall in floor.walls}

    def plan(wall_id, face_id, configuration=None):
        binding = bindings.get((wall_id, face_id))
        if binding is None:
            raise ValueError("Сначала примените монтаж к выбранной стороне стены")
        preview = preview_wall_mount(project, wall_id, face_id, binding.configuration, mounting.snapshot, assets)
        if binding.geometry_signature != preview["geometry_signature"]:
            raise ValueError("Геометрия стены изменилась; сначала пересчитайте монтаж")
        wall = walls.get(wall_id)
        settings = dict(configuration or {})
        if wall is not None:
            settings.setdefault("substrate_thickness_mm", wall.thickness_mm)
            preview = {**preview, "wall": {**preview["wall"], "modeled_substrate": wall.substrate,
                                          "modeled_wall_thickness_mm": wall.thickness_mm}}
        return plan_wall_framing(preview, settings)

    return plan
