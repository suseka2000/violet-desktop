"""Transactional edits to portable project data, independent of an LLM."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from ..domain import models as dm
from ..geometry.validation import validate_project
from ..service import validated

DERIVED = {"mount_assemblies", "whole_wall_assemblies"}
PROTECTED = {"id", "revision", "created_at", "updated_at", "schema_version", "units"}


def compact(project):
    data = project.model_dump(mode="json") if isinstance(project, dm.Project) else deepcopy(project)
    for key in DERIVED:
        data.get("properties", {}).pop(key, None)
    return data


def signature(data):
    return hashlib.sha256(json.dumps(compact(data), sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def parse_json(value):
    def invalid(_):
        raise ValueError("JSON не должен содержать NaN или Infinity")
    return json.loads(value, parse_constant=invalid)


def tokens(path):
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 1500:
        raise ValueError("Нужен JSON Pointer, например /buildings/0/floors/0")
    if re.search(r"~(?![01])", path):
        raise ValueError("Некорректное экранирование JSON Pointer")
    parts = [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]
    if any(p in {"__proto__", "constructor", "prototype"} for p in parts):
        raise ValueError("Недопустимый путь")
    return parts


def index(container, key, *, append=False):
    if isinstance(container, list):
        if append and key == "-":
            return len(container)
        if not re.fullmatch(r"0|[1-9][0-9]*", key):
            raise ValueError("Индекс должен быть целым неотрицательным числом")
        n = int(key)
        if n >= len(container) + int(append):
            raise ValueError("Элемент по указанному индексу не найден")
        return n
    if not isinstance(container, dict):
        raise ValueError("Путь проходит через скалярное значение")
    return key


def at(data, path):
    if path in {"", "/"}:
        return data
    for part in tokens(path):
        data = data[index(data, part)]
    return data


def report(data):
    project = dm.Project.model_validate(data)
    from ..workspace import validate_workspace
    validate_workspace(project)
    return validate_project(project).model_dump(mode="json")


def checked(data):
    return validated(dm.Project.model_validate(data)).model_dump(mode="json")


def apply_patch(data, operations):
    if not isinstance(operations, list) or not 1 <= len(operations) <= 300:
        raise ValueError("Одна транзакция должна содержать 1–300 правок")
    result = deepcopy(data)
    for operation in operations:
        if set(operation) != {"op", "path", "value_json"}:
            raise ValueError("Правка содержит неизвестные поля")
        op, path = operation["op"], operation["path"]
        if op not in {"add", "remove", "replace", "test"}:
            raise ValueError("Допустимы add, remove, replace, test")
        parts = tokens(path)
        if op != "test" and (parts[0] in PROTECTED or
            (parts[0] == "properties" and (len(parts) == 1 or parts[1] in DERIVED)) or
            "snapshot" in parts or "geometry_signature" in parts):
            raise ValueError("Служебные поля, расчётные сборки и снимки методичек менять нельзя")
        parent = result
        for part in parts[:-1]:
            parent = parent[index(parent, part)]
        key = index(parent, parts[-1], append=op == "add")
        if op == "remove":
            del parent[key]
        else:
            value = parse_json(operation["value_json"])
            if op == "test":
                if parent[key] != value:
                    raise ValueError("Проверка исходного значения не прошла; перечитайте объект")
            elif op == "add" and isinstance(parent, list):
                parent.insert(key, value)
            else:
                if op == "replace" and isinstance(parent, dict) and key not in parent:
                    raise ValueError("Заменяемое свойство не существует; используйте add")
                parent[key] = value
    # Guard snapshots even when an ancestor object is replaced.
    old_snapshots = {k: v.get("snapshot") for k, v in data.get("properties", {}).items()
                     if isinstance(v, dict) and "snapshot" in v}
    new_snapshots = {k: v.get("snapshot") for k, v in result.get("properties", {}).items()
                     if isinstance(v, dict) and "snapshot" in v}
    if new_snapshots != old_snapshots:
        raise ValueError("Для назначения монтажа используйте инструмент configure_mount; снимки защищены")
    return checked(result)


def summary(data):
    buildings = []
    for bi, b in enumerate(data.get("buildings", [])):
        floors = []
        for fi, f in enumerate(b.get("floors", [])):
            floors.append({"id": f["id"], "name": f["name"], "path": f"/buildings/{bi}/floors/{fi}",
                           "elevation_mm": f["elevation_mm"], "height_mm": f["default_height_mm"],
                           **{key: len(f[key]) for key in ("walls", "spaces", "openings", "obstacles", "cladding_zones")}})
        buildings.append({"id": b["id"], "name": b["name"], "path": f"/buildings/{bi}", "floors": floors})
    return {"id": data["id"], "revision": data["revision"], "units": "mm", "metadata": data["metadata"],
            "buildings": buildings, "property_keys": sorted(data.get("properties", {})),
            "editable_extensions": {"/properties/finish_presentation": "project_schema(finish_presentation)",
                                    "/properties/document_preferences": "project_schema(document_preferences)",
                                    "/properties/wall_mounting": "configure_mount tool",
                                    "floor.properties.editor_guides": "project_schema(EditorGuides)",
                                    "floor.properties.source_markup": "project_schema(SourceMarkup)"},
            "panel_systems": len(data.get("panel_systems", []))}


def diff(before, after, path="", result=None):
    result = [] if result is None else result
    if before == after:
        return result
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            p = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before:
                result.append({"path": p, "change": "added", "after": short(after[key])})
            elif key not in after:
                result.append({"path": p, "change": "removed", "before": short(before[key])})
            else:
                diff(before[key], after[key], p, result)
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for i, (a, b) in enumerate(zip(before, after)):
            diff(a, b, f"{path}/{i}", result)
    else:
        result.append({"path": path, "change": "changed", "before": short(before), "after": short(after)})
    return result


def short(value):
    if isinstance(value, list):
        return f"Элементов: {len(value)}"
    if isinstance(value, dict):
        return str(value.get("name") or value.get("code") or f"Свойств: {len(value)}")
    return str(value)[:180]


ENTITY_TYPES = {"building": (dm.Building, "buildings"), "floor": (dm.Floor, "floors"),
                "wall": (dm.Wall, "walls"), "opening": (dm.Opening, "openings"),
                "obstacle": (dm.Obstacle, "obstacles"), "cladding_zone": (dm.CladdingZone, "cladding_zones"),
                "panel_system": (dm.PanelSystem, "panel_systems"), "level": (dm.LevelDatum, "levels"),
                "measurement": (dm.Measurement, "measurements"), "design_dimension": (dm.DesignDimension, "design_dimensions")}


def find(data, entity_id):
    if data.get("id") == entity_id:
        return data
    for b in data["buildings"]:
        if b["id"] == entity_id:
            return b
        for f in b["floors"]:
            if f["id"] == entity_id:
                return f
            for key in ("walls", "spaces", "openings", "obstacles", "cladding_zones", "levels"):
                for item in f[key]:
                    if item["id"] == entity_id:
                        return item
    raise ValueError("Объект не найден в текущем проекте")


def create_entity(data, kind, parent_id, payload):
    cls, collection = ENTITY_TYPES[kind]
    result = deepcopy(data)
    parent = result if kind in {"building", "panel_system", "measurement", "design_dimension"} else find(result, parent_id)
    if collection not in parent or not isinstance(parent[collection], list):
        raise ValueError("Этот тип объекта нельзя добавить выбранному родителю")
    if "id" in payload:
        raise ValueError("ID нового объекта создаёт программа")
    entity = cls.model_validate(payload).model_dump(mode="json")
    parent[collection].append(entity)
    return checked(result), entity
