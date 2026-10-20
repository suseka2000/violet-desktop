"""Application operations. Every saved change is validated and revision-checked."""
from __future__ import annotations

from typing import Callable

from .domain.builders import add_rectangle, make_demo_project
from .domain.models import Building, Floor, Opening, Project, ProjectMetadata, new_id, utc_now
from .geometry.validation import validate_project
from .storage.repository import Repository


class InvalidProjectError(ValueError):
    def __init__(self, report):
        super().__init__("В проекте есть ошибки. Изменения не сохранены.")
        self.report = report


class EntityNotFoundError(LookupError):
    pass


def validated(project: Project):
    # Revalidate nested containers too: direct list mutation bypasses assignment validation.
    project = Project.model_validate(project.model_dump(mode="json"))
    from .workspace import validate_workspace
    validate_workspace(project)
    report = validate_project(project)
    if not report.valid:
        raise InvalidProjectError(report)
    return project


def clone_project(project: Project) -> Project:
    """Copy all entity IDs, preserving explicit references but not arbitrary text."""
    data = project.model_dump(mode="json")
    mapping: dict[str, str] = {}

    def collect(value):
        if isinstance(value, dict):
            if "id" in value:
                mapping[value["id"]] = new_id()
            for key, item in value.items():
                if key != "properties":
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    def replace(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "properties":
                    continue
                if (key == "id" or key.endswith("_id")) and isinstance(item, str):
                    value[key] = mapping.get(item, item)
                elif key.endswith("_ids") and isinstance(item, list):
                    value[key] = [mapping.get(v, v) for v in item]
                else:
                    replace(item)
        elif isinstance(value, list):
            for item in value:
                replace(item)

    collect(data)
    replace(data)
    # This reserved extension contains references to panel systems, unlike
    # arbitrary user properties (which must remain untouched).
    catalog = data.get("properties", {}).get("node_catalog")
    if isinstance(catalog, dict):
        for node in catalog.get("nodes", []):
            rules = node.get("rules", {})
            if "panel_system_ids" in rules:
                rules["panel_system_ids"] = [mapping.get(v, v) for v in rules["panel_system_ids"]]
    vendor_design = data.get("properties", {}).get("vendor_design")
    if isinstance(vendor_design, dict):
        # Source/document IDs and the immutable library hash are not model IDs.
        # A copied project must explicitly recheck choices in its new context.
        for binding in vendor_design.get("zone_bindings", []):
            binding["zone_id"] = mapping.get(binding["zone_id"], binding["zone_id"])
            binding.pop("zone_signature", None)
    wall_mounting = data.get("properties", {}).get("wall_mounting")
    if isinstance(wall_mounting, dict):
        # A mounting specification belongs to a model face; its source snapshot
        # belongs to the library and must remain byte-for-byte unchanged.
        for binding in wall_mounting.get("walls", []):
            for key in ("wall_id", "face_id"):
                if binding.get(key):
                    binding[key] = mapping.get(binding[key], binding[key])
            binding.pop("geometry_signature", None)
    wall_settings = data.get("properties", {}).get("whole_wall_settings")
    if isinstance(wall_settings, dict):
        for binding in wall_settings.get("walls", []):
            for key in ("wall_id", "face_id"):
                binding[key] = mapping.get(binding.get(key), binding.get(key))
    # Reserved CAD references follow the copied model, while source hashes,
    # native component numbers and geometric snapshots keep their provenance.
    for asset in data.get("properties", {}).get("cad_assets", []):
        for section in asset.get("sections", []):
            section["id"] = new_id()
            for key in ("target_space_id", "target_zone_id"):
                if section.get(key):
                    section[key] = mapping.get(section[key], section[key])
    for building in data.get("buildings", []):
        for floor in building.get("floors", []):
            for source in floor.get("properties", {}).get("source_markup", {}).get("sources", []):
                for mark in source.get("marks", []):
                    mark["model_ids"] = [mapping.get(entity_id, entity_id) for entity_id in mark.get("model_ids", [])]
    now = utc_now().isoformat()
    data.update(revision=0, created_at=now, updated_at=now)
    cloned=Project.model_validate(data)
    if "special_installations" in project.properties:
        from .special_assemblies import clone_special_properties
        clone_special_properties(project,cloned,mapping)
    return cloned


class ProjectService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def save(self, project: Project, *, expected_revision: int, message: str = "Изменение проекта") -> Project:
        return self.repository.save(validated(project), expected_revision=expected_revision, message=message)

    def create(self, name: str, address: str = "", author: str = "") -> Project:
        project = Project(metadata=ProjectMetadata(name=name, address=address, author=author))
        return self.save(project, expected_revision=0, message="Создан проект")

    def demo(self) -> Project:
        return self.save(make_demo_project(), expected_revision=0, message="Создан учебный пример")

    def import_copy(self, project: Project) -> Project:
        validated(project)
        return self.save(clone_project(project), expected_revision=0, message="Импортирована копия проекта")

    def change(self, project_id: str, expected_revision: int, operation: Callable, message: str) -> Project:
        project = self.repository.get(project_id)
        operation(project)
        return self.save(project, expected_revision=expected_revision, message=message)

    def add_building(self, project_id: str, expected_revision: int, *, name: str) -> Project:
        return self.change(project_id, expected_revision,
                           lambda p: p.buildings.append(Building(name=name)), "Добавлено здание")

    def add_floor(self, project_id: str, expected_revision: int, *, building_id: str,
                  name: str, elevation_mm: float = 0, default_height_mm: float = 3000) -> Project:
        def operation(project):
            building = next((b for b in project.buildings if b.id == building_id), None)
            if building is None:
                raise EntityNotFoundError("Здание не найдено в проекте")
            building.floors.append(Floor(name=name, elevation_mm=elevation_mm, default_height_mm=default_height_mm))
        return self.change(project_id, expected_revision, operation, "Добавлен этаж")

    def add_room(self, project_id: str, expected_revision: int, *, floor_id: str, **parameters) -> Project:
        def operation(project):
            floor = find_floor(project, floor_id)
            add_rectangle(floor, **parameters)
        return self.change(project_id, expected_revision, operation, "Добавлено помещение")

    def add_opening(self, project_id: str, expected_revision: int, *, floor_id: str, opening: Opening) -> Project:
        def operation(project):
            find_floor(project, floor_id).openings.append(opening)
        return self.change(project_id, expected_revision, operation, "Добавлен проём")

    def restore(self, project_id: str, source_revision: int, expected_revision: int) -> Project:
        validated(self.repository.get(project_id, revision=source_revision))
        return self.repository.restore(project_id, source_revision, expected_revision)


def find_floor(project: Project, floor_id: str) -> Floor:
    for building in project.buildings:
        for floor in building.floors:
            if floor.id == floor_id:
                return floor
    raise EntityNotFoundError("Этаж не найден в проекте")
