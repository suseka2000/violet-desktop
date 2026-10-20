"""Deterministic engineering derivations for wall elevations and panel layouts.

The functions in this module never mutate the canonical :class:`Project` and
never claim a mounting solution.  They derive reviewable geometry from wall
reference lines, propose node-library candidates, and keep every approval
explicitly unresolved.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from math import atan2, ceil, degrees, floor, hypot, isfinite
import re
from typing import Any, Iterable

from backend.domain.models import (
    BoundaryEdge,
    Building,
    CladdingZone,
    Floor,
    Opening,
    PanelSystem,
    Project,
    Space,
    Wall,
    WallFace,
)
from backend.geometry.validation import validate_project
from .openings import door_parameter_issues, opening_details, subtract_intervals, visible_finish_segments, visible_opening_edges

__all__ = ["build_engineering", "catalog_schema", "validate_catalog"]


ENGINEERING_SCHEMA_VERSION = "engineering-report/1.0"
CATALOG_SCHEMA_VERSION = "node-catalog/1.0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_COLOR = re.compile(r"^(?:#[0-9A-Fa-f]{6}|none)$")
_NODE_CONTEXTS = {
    "floor",
    "ceiling",
    "wall_start",
    "wall_end",
    "opening_head",
    "opening_sill",
    "opening_jamb_left",
    "opening_jamb_right",
    "corner",
}
_OPENING_KINDS = {"door", "window", "passage", "hatch", "custom"}
_FACE_SIDES = {"left", "right"}
_CORNER_TURNS = {"left", "right", "straight"}
_NODE_STATUSES = {"draft", "approved", "retired"}

# Explicit resource bounds.  A caller receives an issue instead of an
# unbounded allocation or a silently simplified result.
MAX_CATALOG_NODES = 2_000
MAX_PRIMITIVES_PER_NODE = 2_000
MAX_POINTS_PER_PRIMITIVE = 2_000
MAX_STOCK_WIDTH_OPTIONS = 64
MAX_LAYOUT_PANELS = 10_000
MAX_LAYOUT_SEARCH_COUNTS = 256
MAX_EXCLUDED_OPENINGS_PER_ZONE = 1_000
MAX_MATERIAL_PIECES_PER_LAYOUT = 100_000
MAX_PANEL_OPENING_OPERATIONS = 200_000
MAX_ZONE_OVERLAP_COMPARISONS = 100_000
MAX_VECTOR_COORDINATE_MM = 1_000_000.0


def _issue(
    code: str,
    severity: str,
    message: str,
    path: str,
    entity_ids: Iterable[str] = (),
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "path": path,
        "entity_ids": list(entity_ids),
        "details": details or {},
    }


def _number(value: float) -> float:
    """Remove negative zero and insignificant arithmetic noise."""

    result = round(float(value), 9)
    return 0.0 if result == 0.0 else result


def _wall_length(wall: Wall) -> float:
    return hypot(float(wall.end.x - wall.start.x), float(wall.end.y - wall.start.y))


def _view_offset(face_side: str, wall_length: float, offset: float, width: float) -> float:
    if face_side == "right":
        return _number(wall_length - offset - width)
    return _number(offset)


def _strict_keys(
    value: dict[str, Any],
    allowed: set[str],
    required: set[str],
    path: str,
    issues: list[dict[str, Any]],
) -> None:
    for key in sorted(set(value) - allowed):
        issues.append(
            _issue(
                "CATALOG_UNKNOWN_FIELD",
                "error",
                "Каталог узлов содержит неизвестное поле.",
                f"{path}/{key}",
                details={"field": key},
            )
        )
    for key in sorted(required - set(value)):
        issues.append(
            _issue(
                "CATALOG_REQUIRED_FIELD_MISSING",
                "error",
                "В каталоге узлов отсутствует обязательное поле.",
                f"{path}/{key}",
                details={"field": key},
            )
        )


def _valid_text(
    value: Any,
    path: str,
    issues: list[dict[str, Any]],
    *,
    nonempty: bool = True,
    maximum: int = 1_000,
    identifier: bool = False,
) -> bool:
    valid = isinstance(value, str)
    if valid and nonempty:
        valid = bool(value.strip())
    if valid:
        valid = len(value) <= maximum and not any(
            ord(char) < 32 and char not in "\n\t" for char in value
        )
    if valid and identifier:
        valid = bool(_IDENTIFIER.fullmatch(value))
    if not valid:
        issues.append(
            _issue(
                "CATALOG_INVALID_TEXT",
                "error",
                "Текстовое поле каталога имеет недопустимое значение.",
                path,
            )
        )
    return valid


def _valid_number(
    value: Any,
    path: str,
    issues: list[dict[str, Any]],
    *,
    minimum: float = -MAX_VECTOR_COORDINATE_MM,
    maximum: float = MAX_VECTOR_COORDINATE_MM,
    positive: bool = False,
) -> bool:
    valid = (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(float(value))
        and minimum <= float(value) <= maximum
    )
    if positive and valid:
        valid = float(value) > 0.0
    if not valid:
        issues.append(
            _issue(
                "CATALOG_INVALID_NUMBER",
                "error",
                "Числовое поле каталога должно быть конечным и находиться в допустимом диапазоне.",
                path,
                details={"minimum": minimum, "maximum": maximum, "positive": positive},
            )
        )
    return valid


def _validate_string_list(
    value: Any,
    path: str,
    issues: list[dict[str, Any]],
    *,
    allowed: set[str] | None = None,
    identifiers: bool = False,
    maximum: int = 256,
) -> None:
    if not isinstance(value, list) or not value or len(value) > maximum:
        issues.append(
            _issue(
                "CATALOG_INVALID_LIST",
                "error",
                "Список каталога должен быть непустым и иметь ограниченную длину.",
                path,
                details={"maximum_items": maximum},
            )
        )
        return
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}/{index}"
        if not _valid_text(item, item_path, issues, identifier=identifiers):
            continue
        if allowed is not None and item not in allowed:
            issues.append(
                _issue(
                    "CATALOG_UNSUPPORTED_VALUE",
                    "error",
                    "Поле каталога содержит неподдерживаемое значение.",
                    item_path,
                    details={"value": item},
                )
            )
        if item in seen:
            issues.append(
                _issue(
                    "CATALOG_DUPLICATE_VALUE",
                    "error",
                    "Значение в списке каталога повторяется.",
                    item_path,
                    details={"value": item},
                )
            )
        seen.add(item)


def _validate_rules(value: Any, path: str, issues: list[dict[str, Any]]) -> None:
    if not isinstance(value, dict):
        issues.append(
            _issue("CATALOG_RULES_INVALID", "error", "Правила узла должны быть объектом.", path)
        )
        return
    list_rules: dict[str, tuple[set[str] | None, bool]] = {
        "panel_system_ids": (None, True),
        "adjacent_panel_system_ids": (None, True),
        "substrates": (None, False),
        "opening_kinds": (_OPENING_KINDS, False),
        "face_sides": (_FACE_SIDES, False),
        "corner_turns": (_CORNER_TURNS, False),
        "profile_codes": (None, False),
        "face_finishes": (None, False),
        "reveal_finishes": (None, False),
    }
    numeric_rules = {
        "minimum_panel_thickness_mm",
        "maximum_panel_thickness_mm",
        "minimum_joint_mm",
        "maximum_joint_mm",
    }
    allowed = set(list_rules) | numeric_rules | {"requires_reveal_finish"}
    _strict_keys(value, allowed, set(), path, issues)
    for key, (choices, identifiers) in list_rules.items():
        if key in value:
            _validate_string_list(
                value[key],
                f"{path}/{key}",
                issues,
                allowed=choices,
                identifiers=identifiers,
            )
    for key in numeric_rules:
        if key in value:
            _valid_number(value[key], f"{path}/{key}", issues, minimum=0.0)
    if "requires_reveal_finish" in value and not isinstance(value["requires_reveal_finish"], bool):
        issues.append(
            _issue(
                "CATALOG_INVALID_BOOLEAN",
                "error",
                "Поле requires_reveal_finish должно быть логическим.",
                f"{path}/requires_reveal_finish",
            )
        )
    for minimum_key, maximum_key in (
        ("minimum_panel_thickness_mm", "maximum_panel_thickness_mm"),
        ("minimum_joint_mm", "maximum_joint_mm"),
    ):
        if minimum_key in value and maximum_key in value:
            minimum_value, maximum_value = value[minimum_key], value[maximum_key]
            if (
                isinstance(minimum_value, (int, float))
                and not isinstance(minimum_value, bool)
                and isinstance(maximum_value, (int, float))
                and not isinstance(maximum_value, bool)
                and isfinite(float(minimum_value))
                and isfinite(float(maximum_value))
                and float(minimum_value) > float(maximum_value)
            ):
                issues.append(
                    _issue(
                        "CATALOG_RULE_RANGE_REVERSED",
                        "error",
                        "Минимальное значение правила больше максимального.",
                        path,
                        details={"minimum_field": minimum_key, "maximum_field": maximum_key},
                    )
                )


def _validate_color(value: Any, path: str, issues: list[dict[str, Any]]) -> None:
    if not isinstance(value, str) or not _COLOR.fullmatch(value):
        issues.append(
            _issue(
                "CATALOG_UNSAFE_COLOR",
                "error",
                "Цвет примитива должен быть #RRGGBB или none.",
                path,
            )
        )


def _validate_primitive(value: Any, path: str, issues: list[dict[str, Any]]) -> None:
    if not isinstance(value, dict):
        issues.append(
            _issue("CATALOG_PRIMITIVE_INVALID", "error", "Примитив должен быть объектом.", path)
        )
        return
    primitive_type = value.get("type")
    common = {"type", "stroke", "stroke_width_mm", "fill"}
    required: set[str]
    allowed: set[str]
    numeric: list[tuple[str, bool]] = []
    if primitive_type == "line":
        required = {"type", "x1", "y1", "x2", "y2"}
        allowed = common | required
        numeric = [(key, False) for key in ("x1", "y1", "x2", "y2")]
    elif primitive_type in {"polyline", "polygon"}:
        required = {"type", "points"}
        allowed = common | required
    elif primitive_type == "rect":
        required = {"type", "x", "y", "width", "height"}
        allowed = common | required
        numeric = [("x", False), ("y", False), ("width", True), ("height", True)]
    elif primitive_type == "circle":
        required = {"type", "cx", "cy", "r"}
        allowed = common | required
        numeric = [("cx", False), ("cy", False), ("r", True)]
    elif primitive_type == "text":
        required = {"type", "x", "y", "text", "font_size"}
        allowed = common | required | {"anchor"}
        numeric = [("x", False), ("y", False), ("font_size", True)]
    else:
        issues.append(
            _issue(
                "CATALOG_PRIMITIVE_UNSUPPORTED",
                "error",
                "Тип векторного примитива не поддерживается.",
                f"{path}/type",
                details={"value": primitive_type},
            )
        )
        return

    _strict_keys(value, allowed, required, path, issues)
    for key, positive in numeric:
        if key in value:
            _valid_number(value[key], f"{path}/{key}", issues, positive=positive)
    if primitive_type in {"polyline", "polygon"} and "points" in value:
        points = value["points"]
        minimum = 3 if primitive_type == "polygon" else 2
        if not isinstance(points, list) or not minimum <= len(points) <= MAX_POINTS_PER_PRIMITIVE:
            issues.append(
                _issue(
                    "CATALOG_POINTS_INVALID",
                    "error",
                    "Число точек примитива находится вне допустимого диапазона.",
                    f"{path}/points",
                    details={"minimum_items": minimum, "maximum_items": MAX_POINTS_PER_PRIMITIVE},
                )
            )
        else:
            for index, point in enumerate(points):
                point_path = f"{path}/points/{index}"
                if not isinstance(point, list) or len(point) != 2:
                    issues.append(
                        _issue(
                            "CATALOG_POINT_INVALID",
                            "error",
                            "Точка должна быть массивом [x, y].",
                            point_path,
                        )
                    )
                    continue
                for coordinate_index, coordinate_value in enumerate(point):
                    _valid_number(
                        coordinate_value,
                        f"{point_path}/{coordinate_index}",
                        issues,
                    )
    if primitive_type == "text":
        if "text" in value:
            _valid_text(value["text"], f"{path}/text", issues, maximum=500)
        if "anchor" in value and value["anchor"] not in {"start", "middle", "end"}:
            issues.append(
                _issue(
                    "CATALOG_UNSUPPORTED_VALUE",
                    "error",
                    "Недопустимая привязка текста.",
                    f"{path}/anchor",
                )
            )
    for key in ("stroke", "fill"):
        if key in value:
            _validate_color(value[key], f"{path}/{key}", issues)
    if "stroke_width_mm" in value:
        _valid_number(
            value["stroke_width_mm"],
            f"{path}/stroke_width_mm",
            issues,
            minimum=0.0,
            maximum=1_000.0,
            positive=True,
        )


def _primitive_bounds(value: Any) -> tuple[float, float, float, float] | None:
    """Return numeric ink-anchor bounds after structural validation.

    Text metrics are renderer/font dependent, so text contributes its anchor
    only; its finite font size is validated separately.
    """

    if not isinstance(value, dict):
        return None
    primitive_type = value.get("type")
    try:
        if primitive_type == "line":
            xs = [float(value["x1"]), float(value["x2"])]
            ys = [float(value["y1"]), float(value["y2"])]
        elif primitive_type in {"polyline", "polygon"}:
            points = value["points"]
            if not isinstance(points, list) or not points:
                return None
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
        elif primitive_type == "rect":
            x, y = float(value["x"]), float(value["y"])
            width, height = float(value["width"]), float(value["height"])
            xs, ys = [x, x + width], [y, y + height]
        elif primitive_type == "circle":
            cx, cy, radius = float(value["cx"]), float(value["cy"]), float(value["r"])
            xs, ys = [cx - radius, cx + radius], [cy - radius, cy + radius]
        elif primitive_type == "text":
            xs, ys = [float(value["x"])], [float(value["y"])]
        else:
            return None
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not all(isfinite(item) for item in [*xs, *ys]):
        return None
    expansion = 0.0
    if primitive_type != "text" and isinstance(value.get("stroke_width_mm"), (int, float)):
        expansion = max(0.0, float(value["stroke_width_mm"]) / 2.0)
    return (
        min(xs) - expansion,
        min(ys) - expansion,
        max(xs) + expansion,
        max(ys) + expansion,
    )


def _validate_drawing(value: Any, path: str, issues: list[dict[str, Any]]) -> None:
    if not isinstance(value, dict):
        issues.append(
            _issue("CATALOG_DRAWING_INVALID", "error", "Описание чертежа должно быть объектом.", path)
        )
        return
    _strict_keys(value, {"view_box", "primitives"}, {"view_box", "primitives"}, path, issues)
    view_box = value.get("view_box")
    valid_view_box = isinstance(view_box, list) and len(view_box) == 4
    if not valid_view_box:
        issues.append(
            _issue(
                "CATALOG_VIEW_BOX_INVALID",
                "error",
                "view_box должен содержать четыре числа [x, y, width, height].",
                f"{path}/view_box",
            )
        )
    else:
        for index, item in enumerate(view_box):
            _valid_number(
                item,
                f"{path}/view_box/{index}",
                issues,
                positive=index >= 2,
            )
    primitives = value.get("primitives")
    if not isinstance(primitives, list) or len(primitives) > MAX_PRIMITIVES_PER_NODE:
        issues.append(
            _issue(
                "CATALOG_PRIMITIVES_INVALID",
                "error",
                "Список примитивов отсутствует или превышает допустимый предел.",
                f"{path}/primitives",
                details={"maximum_items": MAX_PRIMITIVES_PER_NODE},
            )
        )
    else:
        for index, primitive in enumerate(primitives):
            primitive_path = f"{path}/primitives/{index}"
            _validate_primitive(primitive, primitive_path, issues)
            if valid_view_box and all(
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and isfinite(float(item))
                for item in view_box
            ):
                box_x, box_y, box_width, box_height = map(float, view_box)
                bounds = _primitive_bounds(primitive)
                if (
                    bounds is not None
                    and box_width > 0.0
                    and box_height > 0.0
                    and (
                        bounds[0] < box_x - 1e-9
                        or bounds[1] < box_y - 1e-9
                        or bounds[2] > box_x + box_width + 1e-9
                        or bounds[3] > box_y + box_height + 1e-9
                    )
                ):
                    issues.append(
                        _issue(
                            "CATALOG_PRIMITIVE_OUTSIDE_VIEW_BOX",
                            "error",
                            "Геометрия примитива выходит за пределы view_box.",
                            primitive_path,
                            details={
                                "primitive_bounds": list(map(_number, bounds)),
                                "view_box": list(map(_number, map(float, view_box))),
                            },
                        )
                    )


def validate_catalog(catalog: dict[str, Any] | None) -> dict[str, Any]:
    """Validate an untrusted node catalog without evaluating external content.

    The returned ``normalized`` value is a detached copy and is present only
    when the whole catalog is valid.  Unknown fields are rejected instead of
    being silently discarded.
    """

    issues: list[dict[str, Any]] = []
    if not isinstance(catalog, dict):
        issues.append(
            _issue(
                "CATALOG_ROOT_INVALID",
                "error",
                "Корень каталога узлов должен быть объектом JSON.",
                "/catalog",
            )
        )
        return {"valid": False, "issues": issues, "normalized": None}

    allowed = {"schema_version", "catalog_id", "name", "description", "revision", "sample", "nodes"}
    required = {"schema_version", "catalog_id", "name", "revision", "sample", "nodes"}
    _strict_keys(catalog, allowed, required, "/catalog", issues)
    if catalog.get("schema_version") != CATALOG_SCHEMA_VERSION:
        issues.append(
            _issue(
                "CATALOG_VERSION_UNSUPPORTED",
                "error",
                "Версия схемы каталога узлов не поддерживается.",
                "/catalog/schema_version",
                details={"supported": CATALOG_SCHEMA_VERSION, "actual": catalog.get("schema_version")},
            )
        )
    if "catalog_id" in catalog:
        _valid_text(catalog["catalog_id"], "/catalog/catalog_id", issues, identifier=True)
    if "name" in catalog:
        _valid_text(catalog["name"], "/catalog/name", issues, maximum=300)
    if "description" in catalog:
        _valid_text(catalog["description"], "/catalog/description", issues, nonempty=False, maximum=2_000)
    revision = catalog.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        issues.append(
            _issue(
                "CATALOG_REVISION_INVALID",
                "error",
                "Ревизия каталога должна быть неотрицательным целым числом.",
                "/catalog/revision",
            )
        )
    if "sample" in catalog and not isinstance(catalog["sample"], bool):
        issues.append(
            _issue(
                "CATALOG_INVALID_BOOLEAN",
                "error",
                "Поле sample должно быть логическим.",
                "/catalog/sample",
            )
        )

    nodes = catalog.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > MAX_CATALOG_NODES:
        issues.append(
            _issue(
                "CATALOG_NODES_INVALID",
                "error",
                "Список узлов отсутствует или превышает допустимый предел.",
                "/catalog/nodes",
                details={"maximum_items": MAX_CATALOG_NODES},
            )
        )
    else:
        node_ids: set[str] = set()
        for index, node in enumerate(nodes):
            path = f"/catalog/nodes/{index}"
            if not isinstance(node, dict):
                issues.append(_issue("CATALOG_NODE_INVALID", "error", "Узел должен быть объектом.", path))
                continue
            node_allowed = {"id", "name", "description", "version", "status", "contexts", "rules", "drawing"}
            node_required = {"id", "name", "version", "status", "contexts", "rules", "drawing"}
            _strict_keys(node, node_allowed, node_required, path, issues)
            if "id" in node and _valid_text(node["id"], f"{path}/id", issues, identifier=True):
                if node["id"] in node_ids:
                    issues.append(
                        _issue(
                            "CATALOG_NODE_ID_DUPLICATE",
                            "error",
                            "Идентификатор узла повторяется.",
                            f"{path}/id",
                            entity_ids=[node["id"]],
                        )
                    )
                node_ids.add(node["id"])
            if "name" in node:
                _valid_text(node["name"], f"{path}/name", issues, maximum=300)
            if "description" in node:
                _valid_text(node["description"], f"{path}/description", issues, nonempty=False, maximum=2_000)
            if "version" in node:
                _valid_text(node["version"], f"{path}/version", issues, maximum=100)
            if node.get("status") not in _NODE_STATUSES:
                issues.append(
                    _issue(
                        "CATALOG_NODE_STATUS_INVALID",
                        "error",
                        "Статус узла не поддерживается.",
                        f"{path}/status",
                        details={"value": node.get("status")},
                    )
                )
            if "contexts" in node:
                _validate_string_list(node["contexts"], f"{path}/contexts", issues, allowed=_NODE_CONTEXTS)
            if "rules" in node:
                _validate_rules(node["rules"], f"{path}/rules", issues)
            if "drawing" in node:
                _validate_drawing(node["drawing"], f"{path}/drawing", issues)

    valid = not any(issue["severity"] == "error" for issue in issues)
    normalized: dict[str, Any] | None = None
    if valid:
        normalized = deepcopy(catalog)
        normalized.setdefault("description", "")
        for node in normalized["nodes"]:
            node.setdefault("description", "")
            for primitive in node["drawing"]["primitives"]:
                if primitive["type"] == "text":
                    primitive.setdefault("fill", "#000000")
                    primitive.setdefault("anchor", "start")
                else:
                    primitive.setdefault("stroke", "#000000")
                    primitive.setdefault("stroke_width_mm", 0.25)
                    if primitive["type"] in {"polygon", "rect", "circle"}:
                        primitive.setdefault("fill", "none")
    return {"valid": valid, "issues": issues, "normalized": normalized}


def catalog_schema() -> dict[str, Any]:
    """Return the standalone JSON Schema for node catalogs."""

    coordinate = {"type": "number", "minimum": -MAX_VECTOR_COORDINATE_MM, "maximum": MAX_VECTOR_COORDINATE_MM}
    positive = {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_VECTOR_COORDINATE_MM}
    color = {"type": "string", "pattern": r"^(#[0-9A-Fa-f]{6}|none)$"}
    style = {
        "stroke": color,
        "stroke_width_mm": {"type": "number", "exclusiveMinimum": 0, "maximum": 1_000},
        "fill": color,
    }

    def primitive_schema(
        primitive_type: str,
        properties: dict[str, Any],
        required: list[str],
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", *required],
            "properties": {"type": {"const": primitive_type}, **deepcopy(style), **properties},
        }

    point = {
        "type": "array",
        "minItems": 2,
        "maxItems": 2,
        "prefixItems": [coordinate, coordinate],
    }
    primitives = [
        primitive_schema(
            "line",
            {key: coordinate for key in ("x1", "y1", "x2", "y2")},
            ["x1", "y1", "x2", "y2"],
        ),
        primitive_schema(
            "polyline",
            {"points": {"type": "array", "minItems": 2, "maxItems": MAX_POINTS_PER_PRIMITIVE, "items": point}},
            ["points"],
        ),
        primitive_schema(
            "polygon",
            {"points": {"type": "array", "minItems": 3, "maxItems": MAX_POINTS_PER_PRIMITIVE, "items": point}},
            ["points"],
        ),
        primitive_schema(
            "rect",
            {"x": coordinate, "y": coordinate, "width": positive, "height": positive},
            ["x", "y", "width", "height"],
        ),
        primitive_schema(
            "circle",
            {"cx": coordinate, "cy": coordinate, "r": positive},
            ["cx", "cy", "r"],
        ),
        primitive_schema(
            "text",
            {
                "x": coordinate,
                "y": coordinate,
                "text": {"type": "string", "minLength": 1, "maxLength": 500},
                "font_size": positive,
                "anchor": {"enum": ["start", "middle", "end"]},
            },
            ["x", "y", "text", "font_size"],
        ),
    ]
    rule_string_list = {"type": "array", "minItems": 1, "maxItems": 256, "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 1_000}}
    rules_properties: dict[str, Any] = {
        "panel_system_ids": deepcopy(rule_string_list),
        "adjacent_panel_system_ids": deepcopy(rule_string_list),
        "substrates": deepcopy(rule_string_list),
        "opening_kinds": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": sorted(_OPENING_KINDS)}},
        "face_sides": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": sorted(_FACE_SIDES)}},
        "corner_turns": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": sorted(_CORNER_TURNS)}},
        "profile_codes": deepcopy(rule_string_list),
        "face_finishes": deepcopy(rule_string_list),
        "reveal_finishes": deepcopy(rule_string_list),
        "minimum_panel_thickness_mm": {"type": "number", "minimum": 0, "maximum": MAX_VECTOR_COORDINATE_MM},
        "maximum_panel_thickness_mm": {"type": "number", "minimum": 0, "maximum": MAX_VECTOR_COORDINATE_MM},
        "minimum_joint_mm": {"type": "number", "minimum": 0, "maximum": MAX_VECTOR_COORDINATE_MM},
        "maximum_joint_mm": {"type": "number", "minimum": 0, "maximum": MAX_VECTOR_COORDINATE_MM},
        "requires_reveal_finish": {"type": "boolean"},
    }
    node = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "name", "version", "status", "contexts", "rules", "drawing"],
        "properties": {
            "id": {"type": "string", "pattern": _IDENTIFIER.pattern},
            "name": {"type": "string", "minLength": 1, "maxLength": 300},
            "description": {"type": "string", "maxLength": 2_000},
            "version": {"type": "string", "minLength": 1, "maxLength": 100},
            "status": {"enum": sorted(_NODE_STATUSES)},
            "contexts": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": sorted(_NODE_CONTEXTS)}},
            "rules": {"type": "object", "additionalProperties": False, "properties": rules_properties},
            "drawing": {
                "type": "object",
                "additionalProperties": False,
                "required": ["view_box", "primitives"],
                "properties": {
                    "view_box": {
                        "type": "array",
                        "minItems": 4,
                        "maxItems": 4,
                        "prefixItems": [coordinate, coordinate, positive, positive],
                    },
                    "primitives": {"type": "array", "maxItems": MAX_PRIMITIVES_PER_NODE, "items": {"oneOf": primitives}},
                },
            },
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://violet.local/schemas/node-catalog-1.0.json",
        "title": "Каталог узлов РД",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "catalog_id", "name", "revision", "sample", "nodes"],
        "properties": {
            "schema_version": {"const": CATALOG_SCHEMA_VERSION},
            "catalog_id": {"type": "string", "pattern": _IDENTIFIER.pattern},
            "name": {"type": "string", "minLength": 1, "maxLength": 300},
            "description": {"type": "string", "maxLength": 2_000},
            "revision": {"type": "integer", "minimum": 0},
            "sample": {"type": "boolean"},
            "nodes": {"type": "array", "maxItems": MAX_CATALOG_NODES, "items": node},
        },
    }


@dataclass
class _ElevationContext:
    report: dict[str, Any]
    building: Building
    floor: Floor
    space: Space
    boundary: BoundaryEdge
    wall: Wall
    face: WallFace
    path: str


@dataclass
class _ZoneContext:
    zone: CladdingZone
    zone_path: str
    elevation: _ElevationContext
    rect: tuple[float, float, float, float]
    source_offset: float
    source_base: float


def _rect_dict(rect: tuple[float, float, float, float]) -> dict[str, float]:
    return {
        "x_mm": _number(rect[0]),
        "y_mm": _number(rect[1]),
        "width_mm": _number(rect[2]),
        "height_mm": _number(rect[3]),
    }


def _clip_rect(
    rect: tuple[float, float, float, float],
    clip: tuple[float, float, float, float],
    tolerance: float,
) -> tuple[float, float, float, float] | None:
    x0 = max(rect[0], clip[0])
    y0 = max(rect[1], clip[1])
    x1 = min(rect[0] + rect[2], clip[0] + clip[2])
    y1 = min(rect[1] + rect[3], clip[1] + clip[3])
    if x1 - x0 <= tolerance or y1 - y0 <= tolerance:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def _merge_intervals(intervals: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    if not intervals:
        return []
    result: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if not result or start > result[-1][1] + tolerance:
            result.append((start, end))
        else:
            result[-1] = (result[-1][0], max(result[-1][1], end))
    return result


def _rect_union_area(
    rectangles: list[tuple[float, float, float, float]], tolerance: float
) -> float:
    if not rectangles:
        return 0.0
    x_values = sorted({rect[0] for rect in rectangles} | {rect[0] + rect[2] for rect in rectangles})
    area = 0.0
    for left, right in zip(x_values, x_values[1:]):
        if right - left <= tolerance:
            continue
        vertical = [
            (rect[1], rect[1] + rect[3])
            for rect in rectangles
            if rect[0] < right - tolerance and rect[0] + rect[2] > left + tolerance
        ]
        covered = sum(end - start for start, end in _merge_intervals(vertical, tolerance))
        area += (right - left) * covered
    return _number(area)


def _subtract_rectangles(
    panel: tuple[float, float, float, float],
    cutters: list[tuple[float, float, float, float]],
    tolerance: float,
) -> list[tuple[float, float, float, float]]:
    clipped = [candidate for cutter in cutters if (candidate := _clip_rect(cutter, panel, tolerance))]
    if not clipped:
        return [panel]
    x_values = sorted({panel[0], panel[0] + panel[2]} | {r[0] for r in clipped} | {r[0] + r[2] for r in clipped})
    pieces: list[tuple[float, float, float, float]] = []
    active_by_band: dict[tuple[float, float], int] = {}
    panel_bottom, panel_top = panel[1], panel[1] + panel[3]
    for left, right in zip(x_values, x_values[1:]):
        if right - left <= tolerance:
            continue
        blocked = _merge_intervals(
            [
                (max(panel_bottom, r[1]), min(panel_top, r[1] + r[3]))
                for r in clipped
                if r[0] < right - tolerance and r[0] + r[2] > left + tolerance
            ],
            tolerance,
        )
        cursor = panel_bottom
        free: list[tuple[float, float]] = []
        for start, end in blocked:
            if start - cursor > tolerance:
                free.append((cursor, start))
            cursor = max(cursor, end)
        if panel_top - cursor > tolerance:
            free.append((cursor, panel_top))
        next_active: dict[tuple[float, float], int] = {}
        for bottom, top in free:
            key = (_number(bottom), _number(top - bottom))
            existing_index = active_by_band.get(key)
            if existing_index is not None:
                existing = pieces[existing_index]
                if abs(existing[0] + existing[2] - left) <= tolerance:
                    pieces[existing_index] = (existing[0], existing[1], right - existing[0], existing[3])
                    next_active[key] = existing_index
                    continue
            pieces.append((left, bottom, right - left, top - bottom))
            next_active[key] = len(pieces) - 1
        active_by_band = next_active
    return sorted(pieces, key=lambda item: (item[0], item[1], item[2], item[3]))


def _candidate_stock_counts(
    count: int,
    target_width: float,
    stock_widths: list[float],
    tolerance: float,
) -> tuple[tuple[int, ...], float] | None:
    """Find the best bounded one/two-stock-width capacity for a panel count."""

    candidates: set[tuple[int, ...]] = set()
    for index, width in enumerate(stock_widths):
        if count * width >= target_width - tolerance:
            values = [0] * len(stock_widths)
            values[index] = count
            candidates.add(tuple(values))
    for low_index, low in enumerate(stock_widths):
        for high_index in range(low_index + 1, len(stock_widths)):
            high = stock_widths[high_index]
            denominator = high - low
            if denominator <= tolerance:
                continue
            estimate = ceil((target_width - count * low - tolerance) / denominator)
            for high_count in range(max(0, estimate - 2), min(count, estimate + 2) + 1):
                values = [0] * len(stock_widths)
                values[low_index] = count - high_count
                values[high_index] = high_count
                capacity = sum(number * stock_widths[i] for i, number in enumerate(values))
                if capacity >= target_width - tolerance:
                    candidates.add(tuple(values))
    if not candidates:
        return None
    return min(
        ((counts, sum(number * stock_widths[i] for i, number in enumerate(counts))) for counts in candidates),
        key=lambda item: (
            _number(item[1] - target_width),
            sum(1 for number in item[0] if number),
            tuple(reversed(item[0])),
        ),
    )


def _allocate_actual_widths(
    stock_sequence: list[float],
    target_width: float,
    minimum_cut: float,
    tolerance: float,
) -> list[float] | None:
    actual = list(stock_sequence)
    excess = sum(actual) - target_width
    if excess < -tolerance:
        return None
    excess = max(0.0, excess)
    order: list[int] = []
    left, right = 0, len(actual) - 1
    while left <= right:
        order.append(right)
        if left != right:
            order.append(left)
        left += 1
        right -= 1
    for index in order:
        removable = max(0.0, actual[index] - minimum_cut)
        cut = min(excess, removable)
        actual[index] -= cut
        excess -= cut
        if excess <= tolerance:
            break
    if excess > tolerance:
        return None
    residual = target_width - sum(actual)
    if actual and abs(residual) > 0.0:
        actual[-1] += residual
    if any(width < minimum_cut - tolerance or width > stock + tolerance
           for width, stock in zip(actual, stock_sequence)):
        return None
    return [_number(width) for width in actual]


def _choose_panel_widths(
    zone_width: float,
    joint: float,
    stock_widths: list[float],
    minimum_cut: float,
    tolerance: float,
) -> tuple[list[float], list[float], dict[str, Any]] | tuple[None, None, dict[str, Any]]:
    # Room-topology tolerance is not a manufacturing allowance. In particular,
    # closing a 7500.024 mm zone must never stretch a 900 mm stock strip.
    tolerance = min(tolerance, 1e-8)
    largest = max(stock_widths)
    minimum_count = max(1, ceil((zone_width + joint - tolerance) / (largest + joint)))
    maximum_count = floor((zone_width + joint + tolerance) / (minimum_cut + joint))
    details = {
        "estimated_minimum_panel_count": minimum_count,
        "maximum_feasible_panel_count": maximum_count,
        "maximum_generated_panel_count": MAX_LAYOUT_PANELS,
        "searched_panel_counts": 0,
        "search_truncated": False,
    }
    if minimum_count > MAX_LAYOUT_PANELS:
        return None, None, details
    if maximum_count < minimum_count:
        return None, None, details
    search_end = min(maximum_count, MAX_LAYOUT_PANELS, minimum_count + MAX_LAYOUT_SEARCH_COUNTS - 1)
    details["search_truncated"] = search_end < maximum_count
    best: tuple[Any, list[float], list[float]] | None = None
    for count in range(minimum_count, search_end + 1):
        details["searched_panel_counts"] += 1
        target = zone_width - joint * (count - 1)
        if target < count * minimum_cut - tolerance:
            continue
        candidate = _candidate_stock_counts(count, target, stock_widths, tolerance)
        if candidate is None:
            continue
        counts, capacity = candidate
        stocks: list[float] = []
        # Wider strips first makes the sequence stable; cutting is then
        # concentrated at the two visible ends by _allocate_actual_widths.
        for width, number in reversed(list(zip(stock_widths, counts))):
            stocks.extend([width] * number)
        actual = _allocate_actual_widths(stocks, target, minimum_cut, tolerance)
        if actual is None:
            continue
        waste = capacity - target
        cut_panels = sum(abs(stock - width) > tolerance for stock, width in zip(stocks, actual))
        score = (_number(waste), count, cut_panels, tuple(stocks))
        if best is None or score < best[0]:
            best = (score, stocks, actual)
    if best is None:
        return None, None, details
    return best[1], best[2], details


def _opening_rect(opening: Opening, wall: Wall, face: WallFace) -> tuple[float, float, float, float]:
    length = _wall_length(wall)
    x = _view_offset(face.side, length, float(opening.offset_mm), float(opening.width_mm))
    # Canonical semantics: sill is relative to the wall bottom.
    y = float(wall.base_mm + opening.sill_mm)
    return (x, y, float(opening.width_mm), float(opening.height_mm))


def _build_layout(
    context: _ZoneContext,
    panel_systems: dict[str, PanelSystem],
    openings: dict[str, Opening],
    issues: list[dict[str, Any]],
    tolerance: float,
    overlapping_zone_ids: set[str],
) -> dict[str, Any]:
    zone = context.zone
    elevation = context.elevation
    wall = elevation.wall
    face = elevation.face
    layout_id = f"layout:{zone.id}"
    local_issue_codes: list[str] = []

    def add_issue(code: str, message: str, details: dict[str, Any] | None = None) -> None:
        issues.append(_issue(code, "error", message, context.zone_path, [zone.id], details))
        local_issue_codes.append(code)

    panel_system = panel_systems.get(zone.panel_system_id)
    if panel_system is None:
        add_issue("LAYOUT_PANEL_SYSTEM_NOT_FOUND", "Для зоны не найдена система панелей.")
    elif panel_system.status != "approved":
        add_issue(
            "PANEL_SYSTEM_NOT_APPROVED",
            "Система панелей не имеет статуса approved и не готова к производственному выпуску.",
            {"panel_system_id": panel_system.id, "status": panel_system.status},
        )
    if zone.id in overlapping_zone_ids:
        local_issue_codes.append("CLADDING_ZONE_OVERLAP")

    zone_rect = context.rect
    clipped_openings: list[tuple[float, float, float, float]] = []
    excluded_openings: list[dict[str, Any]] = []
    excluded_opening_ids = set(zone.excluded_opening_ids)
    for opening in sorted(openings.values(), key=lambda item: item.id):
        if opening.wall_id != wall.id or opening.id in excluded_opening_ids:
            continue
        source_rect = _opening_rect(opening, wall, face)
        clipped = _clip_rect(source_rect, zone_rect, tolerance)
        if clipped is None:
            continue
        add_issue(
            "OPENING_NOT_EXCLUDED_FROM_ZONE",
            "Проём пересекает зону облицовки, но не указан в excluded_opening_ids. "
            "Нельзя считать материал сплошным или автоматически вырезать проём; "
            "для скрытого проёма или облицованной двери требуется отдельное явное решение.",
            {
                "opening_id": opening.id,
                "opening_kind": opening.kind,
                "source_rect": _rect_dict(source_rect),
                "clipped_rect": _rect_dict(clipped),
            },
        )
    if len(zone.excluded_opening_ids) > MAX_EXCLUDED_OPENINGS_PER_ZONE:
        add_issue(
            "ZONE_OPENING_LIMIT_EXCEEDED",
            "Число исключаемых проёмов превышает безопасный предел расчёта.",
            {"count": len(zone.excluded_opening_ids), "maximum": MAX_EXCLUDED_OPENINGS_PER_ZONE},
        )
    else:
        seen: set[str] = set()
        for opening_id in zone.excluded_opening_ids:
            if opening_id in seen:
                continue
            seen.add(opening_id)
            opening = openings.get(opening_id)
            if opening is None or opening.wall_id != wall.id:
                continue
            source_rect = _opening_rect(opening, wall, face)
            clipped = _clip_rect(source_rect, zone_rect, tolerance)
            if clipped is None:
                issues.append(
                    _issue(
                        "EXCLUDED_OPENING_OUTSIDE_ZONE",
                        "warning",
                        "Исключаемый проём не пересекает зону облицовки.",
                        context.zone_path,
                        [zone.id, opening.id],
                    )
                )
            else:
                clipped_openings.append(clipped)
            excluded_openings.append(
                {
                    "id": opening.id,
                    "kind": opening.kind,
                    "source_rect": _rect_dict(source_rect),
                    "clipped_rect": _rect_dict(clipped) if clipped else None,
                }
            )

    envelope_opening_area = _rect_union_area(clipped_openings, tolerance)
    empty_metrics = {
        "zone_envelope_area_mm2": _number(zone_rect[2] * zone_rect[3]),
        "joint_gap_area_mm2": 0.0,
        "panel_gross_area_mm2": 0.0,
        "excluded_opening_envelope_area_mm2": envelope_opening_area,
        "opening_cutout_area_mm2": 0.0,
        "net_panel_area_mm2": 0.0,
        "nominal_stock_strip_area_mm2": 0.0,
        "width_cut_waste_area_mm2": 0.0,
    }
    base = {
        "id": layout_id,
        "zone_id": zone.id,
        "zone_name": zone.name,
        "panel_system_id": zone.panel_system_id,
        "elevation_id": elevation.report["id"],
        "zone_rect": _rect_dict(zone_rect),
        "strategy": "bounded_minimum_width_waste_then_panel_count",
        "strategy_details": {
            "stock_mix": "one_or_two_standard_widths",
            "direction": "elevation_left_to_right",
            "global_optimum_claimed": False,
        },
        "constraints": {
            "joint_mm": _number(float(panel_system.joint_mm)) if panel_system else None,
            "minimum_cut_width_mm": _number(float(panel_system.minimum_cut_width_mm)) if panel_system else None,
            "maximum_height_mm": _number(float(panel_system.maximum_height_mm))
            if panel_system and panel_system.maximum_height_mm is not None
            else None,
            "standard_widths_mm": sorted({_number(float(value)) for value in panel_system.standard_widths_mm})
            if panel_system
            else [],
        },
        "panels": [],
        "joints": [],
        "excluded_openings": excluded_openings,
        "closure": None,
        "metrics": empty_metrics,
        "issue_codes": local_issue_codes,
        "valid": False,
    }
    if panel_system is None:
        return base
    if panel_system.maximum_height_mm is not None and zone.height_mm > panel_system.maximum_height_mm + 1e-8:
        add_issue(
            "PANEL_MAXIMUM_HEIGHT_EXCEEDED",
            "Высота зоны превышает максимальную высоту панели выбранной системы.",
            {"zone_height_mm": float(zone.height_mm), "maximum_height_mm": float(panel_system.maximum_height_mm)},
        )
    widths = sorted({_number(float(value)) for value in panel_system.standard_widths_mm})
    if len(widths) > MAX_STOCK_WIDTH_OPTIONS:
        add_issue(
            "PANEL_STOCK_WIDTH_OPTION_LIMIT_EXCEEDED",
            "Число стандартных ширин превышает безопасный предел расчёта.",
            {"count": len(widths), "maximum": MAX_STOCK_WIDTH_OPTIONS},
        )
        return base
    usable_widths = [width for width in widths if width + tolerance >= float(panel_system.minimum_cut_width_mm)]
    if not usable_widths:
        add_issue(
            "PANEL_STANDARD_WIDTHS_REQUIRED",
            "Нет стандартной ширины панели, пригодной с учётом минимальной подрезки.",
            {"standard_widths_mm": widths, "minimum_cut_width_mm": float(panel_system.minimum_cut_width_mm)},
        )
        return base

    stocks, actual, search = _choose_panel_widths(
        zone_rect[2],
        float(panel_system.joint_mm),
        usable_widths,
        float(panel_system.minimum_cut_width_mm),
        tolerance,
    )
    base["strategy_details"]["search"] = search
    if stocks is None or actual is None:
        code = (
            "PANEL_COUNT_LIMIT_EXCEEDED"
            if search["estimated_minimum_panel_count"] > MAX_LAYOUT_PANELS
            else "PANEL_LAYOUT_NOT_FEASIBLE"
        )
        add_issue(
            code,
            "Зону нельзя разложить в пределах ограничений ширины, шва, подрезки и безопасного числа панелей.",
            search,
        )
        return base

    panels: list[dict[str, Any]] = []
    joints: list[dict[str, Any]] = []
    cursor = zone_rect[0]
    piece_count = 0
    panel_opening_operations = 0
    piece_limit_hit = False
    operation_limit_hit = False
    net_area = 0.0
    gross_area = 0.0
    stock_area = 0.0
    width_waste_area = 0.0
    for index, (stock_width, panel_width) in enumerate(zip(stocks, actual)):
        panel_rect = (cursor, zone_rect[1], panel_width, zone_rect[3])
        if operation_limit_hit or piece_limit_hit:
            raw_pieces = []
        else:
            panel_cutters = [
                opening
                for opening in clipped_openings
                if opening[0] < panel_rect[0] + panel_rect[2] - tolerance
                and opening[0] + opening[2] > panel_rect[0] + tolerance
            ]
            # _subtract_rectangles builds x slabs and vertical unions.  The
            # square term bounds pathological overlapping input explicitly.
            panel_opening_operations += max(1, len(panel_cutters) ** 2)
            if panel_opening_operations > MAX_PANEL_OPENING_OPERATIONS:
                raw_pieces = []
                add_issue(
                    "PANEL_OPENING_OPERATION_LIMIT_EXCEEDED",
                    "Разбиение панелей вокруг проёмов остановлено на безопасном пределе операций.",
                    {"maximum_operations": MAX_PANEL_OPENING_OPERATIONS},
                )
                operation_limit_hit = True
            else:
                raw_pieces = _subtract_rectangles(panel_rect, panel_cutters, tolerance)
        if not piece_limit_hit and piece_count + len(raw_pieces) > MAX_MATERIAL_PIECES_PER_LAYOUT:
            add_issue(
                "MATERIAL_PIECE_LIMIT_EXCEEDED",
                "Число прямоугольных частей после вычитания проёмов превышает безопасный предел.",
                {"maximum": MAX_MATERIAL_PIECES_PER_LAYOUT},
            )
            raw_pieces = []
            piece_limit_hit = True
        pieces: list[dict[str, Any]] = []
        for piece_index, piece in enumerate(raw_pieces):
            area = _number(piece[2] * piece[3])
            pieces.append(
                {
                    "id": f"piece:{zone.id}:{index:04d}:{piece_index:04d}",
                    **_rect_dict(piece),
                    "area_mm2": area,
                }
            )
            net_area += area
        piece_count += len(pieces)
        panel_gross = panel_width * zone_rect[3]
        gross_area += panel_gross
        stock_area += stock_width * zone_rect[3]
        cut_width = max(0.0, stock_width - panel_width)
        width_waste_area += cut_width * zone_rect[3]
        panels.append(
            {
                "id": f"panel:{zone.id}:{index:04d}",
                "index": index,
                "x_mm": _number(cursor),
                "width_mm": _number(panel_width),
                "height_mm": _number(zone_rect[3]),
                "stock_width_mm": _number(stock_width),
                "cut_width_mm": _number(cut_width),
                "waste_width_mm": _number(cut_width),
                "gross_area_mm2": _number(panel_gross),
                "net_area_mm2": _number(sum(piece["area_mm2"] for piece in pieces)),
                "material_pieces": pieces,
            }
        )
        cursor += panel_width
        if index < len(actual) - 1:
            joints.append(
                {
                    "index": index,
                    "x_mm": _number(cursor),
                    "width_mm": _number(float(panel_system.joint_mm)),
                    "profile_code": None,
                    "quantity_status": "not_calculated",
                }
            )
            cursor += float(panel_system.joint_mm)

    panel_sum = sum(actual)
    joint_sum = float(panel_system.joint_mm) * max(0, len(actual) - 1)
    closure_error = zone_rect[2] - panel_sum - joint_sum
    if abs(closure_error) <= tolerance:
        closure_error = 0.0
    else:
        add_issue(
            "PANEL_LAYOUT_CLOSURE_ERROR",
            "Раскладка не замыкает ширину зоны в пределах геометрического допуска.",
            {"closure_error_mm": closure_error},
        )
    opening_cutout = max(0.0, gross_area - net_area)
    base.update(
        {
            "panels": panels,
            "joints": joints,
            "closure": {
                "panel_width_sum_mm": _number(panel_sum),
                "joint_width_sum_mm": _number(joint_sum),
                "zone_width_mm": _number(zone_rect[2]),
                "error_mm": _number(closure_error),
            },
            "metrics": {
                "zone_envelope_area_mm2": _number(zone_rect[2] * zone_rect[3]),
                "joint_gap_area_mm2": _number(joint_sum * zone_rect[3]),
                "panel_gross_area_mm2": _number(gross_area),
                "excluded_opening_envelope_area_mm2": envelope_opening_area,
                "opening_cutout_area_mm2": _number(opening_cutout),
                "net_panel_area_mm2": _number(net_area),
                "nominal_stock_strip_area_mm2": _number(stock_area),
                "width_cut_waste_area_mm2": _number(width_waste_area),
            },
        }
    )
    base["issue_codes"] = local_issue_codes
    base["valid"] = not local_issue_codes
    return base


def _catalog_candidate(node: dict[str, Any], requirement: dict[str, Any]) -> dict[str, Any] | None:
    if node["status"] != "approved" or requirement["kind"] not in node["contexts"]:
        return None
    context = requirement["context"]
    rules = node["rules"]
    explanations = [f"Узел поддерживает контекст {requirement['kind']}."]
    matched: list[str] = []

    scalar_lists = {
        "panel_system_ids": "panel_system_id",
        "adjacent_panel_system_ids": "adjacent_panel_system_id",
        "substrates": "wall_substrate",
        "opening_kinds": "opening_kind",
        "face_sides": "face_side",
        "corner_turns": "corner_turn",
        "face_finishes": "face_finish",
        "reveal_finishes": "reveal_finish",
    }
    for rule_key, context_key in scalar_lists.items():
        if rule_key not in rules:
            continue
        value = context.get(context_key)
        if value not in rules[rule_key]:
            return None
        matched.append(rule_key)
        explanations.append(f"{context_key}={value!r} соответствует правилу {rule_key}.")
    if "profile_codes" in rules:
        intersection = sorted(set(rules["profile_codes"]) & set(context.get("profile_codes", [])))
        if not intersection:
            return None
        matched.append("profile_codes")
        explanations.append(f"Совпали коды профилей: {', '.join(intersection)}.")

    numeric = {
        "minimum_panel_thickness_mm": ("panel_thickness_mm", lambda actual, limit: actual >= limit),
        "maximum_panel_thickness_mm": ("panel_thickness_mm", lambda actual, limit: actual <= limit),
        "minimum_joint_mm": ("joint_mm", lambda actual, limit: actual >= limit),
        "maximum_joint_mm": ("joint_mm", lambda actual, limit: actual <= limit),
    }
    for rule_key, (context_key, predicate) in numeric.items():
        if rule_key not in rules:
            continue
        actual = context.get(context_key)
        if not isinstance(actual, (int, float)) or not predicate(float(actual), float(rules[rule_key])):
            return None
        matched.append(rule_key)
        explanations.append(f"{context_key}={actual} мм соответствует ограничению {rule_key}={rules[rule_key]} мм.")
    if "requires_reveal_finish" in rules:
        if bool(context.get("has_reveal_finish")) != rules["requires_reveal_finish"]:
            return None
        matched.append("requires_reveal_finish")
        explanations.append("Наличие отделки откоса соответствует правилу узла.")

    return {
        "node_id": node["id"],
        "node_name": node["name"],
        "node_version": node["version"],
        "rank": 0,
        "specificity": len(matched),
        "matched_rules": matched,
        "explanation": explanations,
    }


def _assign_candidates(
    requirements: list[dict[str, Any]],
    catalog: dict[str, Any] | None,
    issues: list[dict[str, Any]],
) -> None:
    if catalog is None:
        return
    for requirement in requirements:
        candidates = [
            candidate
            for node in catalog["nodes"]
            if (candidate := _catalog_candidate(node, requirement)) is not None
        ]
        candidates.sort(key=lambda item: (-item["specificity"], item["node_id"], item["node_version"]))
        for rank, candidate in enumerate(candidates, start=1):
            candidate["rank"] = rank
        requirement["candidates"] = candidates
        if not candidates:
            issues.append(
                _issue(
                    "NODE_CANDIDATE_NOT_FOUND",
                    "error",
                    "Для инженерного контекста не найден подходящий утверждённый узел.",
                    f"/node_requirements/{requirement['id']}",
                    [requirement["id"]],
                    {"kind": requirement["kind"]},
                )
            )


def _common_node_context(zone: _ZoneContext, panel: PanelSystem | None) -> dict[str, Any]:
    elevation = zone.elevation
    return {
        "zone_id": zone.zone.id,
        "panel_system_id": panel.id if panel else zone.zone.panel_system_id,
        "panel_thickness_mm": _number(float(panel.panel_thickness_mm)) if panel else None,
        "backing_thickness_mm": _number(float(panel.backing_thickness_mm)) if panel else None,
        "joint_mm": _number(float(panel.joint_mm)) if panel else None,
        "profile_codes": list(panel.profile_codes) if panel else [],
        "wall_substrate": elevation.wall.substrate,
        "face_side": elevation.face.side,
        "face_finish": elevation.face.finish,
    }


def _build_node_requirements(
    elevations_by_space: dict[str, list[_ElevationContext]],
    zone_contexts: dict[str, _ZoneContext],
    layouts_by_zone: dict[str, dict[str, Any]],
    panel_systems: dict[str, PanelSystem],
    tolerance: float,
) -> list[dict[str, Any]]:
    counters: dict[tuple[str, str], int] = defaultdict(int)
    requirements: list[dict[str, Any]] = []
    connected_ends: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    zone_voids = {
        zone_id: [
            (o["source_rect"]["x_mm"], o["source_rect"]["y_mm"],
             o["source_rect"]["width_mm"], o["source_rect"]["height_mm"])
            for o in layout["excluded_openings"]
        ]
        for zone_id, layout in layouts_by_zone.items()
    }

    def append(
        kind: str,
        zone: _ZoneContext,
        location: dict[str, Any],
        extra_context: dict[str, Any] | None = None,
    ) -> None:
        elevation_id = zone.elevation.report["id"]
        sequence = counters[(elevation_id, kind)]
        counters[(elevation_id, kind)] += 1
        context = _common_node_context(zone, panel_systems.get(zone.zone.panel_system_id))
        if extra_context:
            context.update(extra_context)
        requirements.append(
            {
                "id": f"node-requirement:{elevation_id}:{kind}:{sequence:04d}",
                "kind": kind,
                "elevation_id": elevation_id,
                "zone_id": zone.zone.id,
                "location": location,
                "context": context,
                "candidates": [],
                "selection": {
                    "status": "unresolved",
                    "selected_node_id": None,
                    "implicit_approval": False,
                },
            }
        )

    # Corners are derived from room-boundary adjacency, not from drawing order.
    for space_id, elevation_contexts in elevations_by_space.items():
        if len(elevation_contexts) < 2:
            continue
        for index, current in enumerate(elevation_contexts):
            following = elevation_contexts[(index + 1) % len(elevation_contexts)]
            current_start = (float(current.wall.start.x), float(current.wall.start.y))
            current_end = (float(current.wall.end.x), float(current.wall.end.y))
            next_start = (float(following.wall.start.x), float(following.wall.start.y))
            next_end = (float(following.wall.end.x), float(following.wall.end.y))
            if current.boundary.reversed:
                current_start, current_end = current_end, current_start
            if following.boundary.reversed:
                next_start, next_end = next_end, next_start
            if hypot(current_end[0] - next_start[0], current_end[1] - next_start[1]) > tolerance:
                continue
            current_length = _wall_length(current.wall)
            next_length = _wall_length(following.wall)
            current_source_endpoint = 0.0 if current.boundary.reversed else current_length
            next_source_endpoint = next_length if following.boundary.reversed else 0.0
            current_zones = [
                zone
                for zone in zone_contexts.values()
                if zone.elevation is current
                and zone.source_offset - tolerance <= current_source_endpoint <= zone.source_offset + float(zone.zone.width_mm) + tolerance
            ]
            next_zones = [
                zone
                for zone in zone_contexts.values()
                if zone.elevation is following
                and zone.source_offset - tolerance <= next_source_endpoint <= zone.source_offset + float(zone.zone.width_mm) + tolerance
            ]
            incoming = (current_end[0] - current_start[0], current_end[1] - current_start[1])
            outgoing = (next_end[0] - next_start[0], next_end[1] - next_start[1])
            cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
            dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
            signed_turn = degrees(atan2(cross, dot))
            corner_turn = "straight" if abs(signed_turn) < 1e-9 else ("left" if signed_turn > 0 else "right")
            for current_zone in current_zones:
                for next_zone in next_zones:
                    bottom = max(current_zone.rect[1], next_zone.rect[1])
                    top = min(current_zone.rect[1] + current_zone.rect[3], next_zone.rect[1] + next_zone.rect[3])
                    if top - bottom <= tolerance:
                        continue
                    current_view_x = _view_offset(
                        current.face.side, current_length, current_source_endpoint, 0.0
                    )
                    next_view_x = _view_offset(
                        following.face.side, next_length, next_source_endpoint, 0.0
                    )
                    current_edge = "start" if abs(current_view_x) <= tolerance else "end"
                    next_edge = "start" if abs(next_view_x) <= tolerance else "end"
                    adjacent_panel = panel_systems.get(next_zone.zone.panel_system_id)
                    # A corner exists only where finish survives on both faces.
                    # An opening reaching one endpoint leaves the other face's
                    # exposed end, not a fictitious corner running across a void.
                    for start, end in visible_finish_segments(
                        (current_view_x, bottom), (current_view_x, top),
                        zone_voids.get(current_zone.zone.id, []), tolerance,
                    ):
                        for adjacent_start, adjacent_end in visible_finish_segments(
                            (next_view_x, start[1]), (next_view_x, end[1]),
                            zone_voids.get(next_zone.zone.id, []), tolerance,
                        ):
                            span_bottom, span_top = adjacent_start[1], adjacent_end[1]
                            connected_ends[(current_zone.zone.id, current_edge)].append((span_bottom, span_top))
                            connected_ends[(next_zone.zone.id, next_edge)].append((span_bottom, span_top))
                            append(
                                "corner", current_zone,
                                {
                                    "plan_point": {"x_mm": _number(current_end[0]), "y_mm": _number(current_end[1])},
                                    "primary_elevation_id": current.report["id"],
                                    "primary_x_mm": _number(current_view_x),
                                    "adjacent_elevation_id": following.report["id"],
                                    "adjacent_x_mm": _number(next_view_x),
                                    "bottom_mm": _number(span_bottom),
                                    "height_mm": _number(span_top - span_bottom),
                                },
                                {
                                    "space_id": space_id,
                                    "corner_turn": corner_turn,
                                    "signed_turn_deg": _number(signed_turn),
                                    "adjacent_zone_id": next_zone.zone.id,
                                    "adjacent_panel_system_id": next_zone.zone.panel_system_id,
                                    "adjacent_panel_thickness_mm": _number(float(adjacent_panel.panel_thickness_mm))
                                    if adjacent_panel else None,
                                },
                            )

    for zone_id, zone in zone_contexts.items():
        layout = layouts_by_zone.get(zone_id)
        if layout is None:
            continue
        rect = zone.rect
        elevation = zone.elevation
        void_rects = zone_voids[zone_id]
        for start,end in visible_finish_segments((rect[0],rect[1]),(rect[0]+rect[2],rect[1]),void_rects,tolerance):
            append("floor", zone, {"x_mm":_number((start[0]+end[0])/2),"y_mm":_number(start[1]),"width_mm":_number(end[0]-start[0])}, {
                "distance_to_floor_mm": _number(rect[1]),
                "distance_to_wall_bottom_mm": _number(rect[1] - float(elevation.wall.base_mm)),
            })
        for start,end in visible_finish_segments((rect[0],rect[1]+rect[3]),(rect[0]+rect[2],rect[1]+rect[3]),void_rects,tolerance):
            append("ceiling",zone,{"x_mm":_number((start[0]+end[0])/2),"y_mm":_number(start[1]),"width_mm":_number(end[0]-start[0])},
                   {"distance_to_room_ceiling_mm": _number(float(elevation.space.height_mm) - rect[1] - rect[3])})
        for edge, x in (("start", rect[0]), ("end", rect[0] + rect[2])):
            for start, end in visible_finish_segments((x, rect[1]), (x, rect[1] + rect[3]), void_rects, tolerance):
                for bottom, top in subtract_intervals(start[1], end[1], connected_ends[(zone_id, edge)], tolerance):
                    append("wall_" + edge, zone, {"x_mm": _number(x), "y_mm": _number(bottom), "height_mm": _number(top - bottom)})
        for excluded in layout["excluded_openings"]:
            clipped = excluded["clipped_rect"]
            if clipped is None:
                continue
            source = excluded["source_rect"]
            source_rect = (source["x_mm"],source["y_mm"],source["width_mm"],source["height_mm"])
            opening = next(
                (item for item in elevation.report["openings"] if item["id"] == excluded["id"]),
                None,
            )
            reveal = opening.get("reveal_finish", "") if opening else ""
            extra = {
                "opening_id": excluded["id"],
                "opening_kind": excluded["kind"],
                "reveal_finish": reveal,
                "has_reveal_finish": bool(reveal),
                "reveal_depth_mm": None,
                "door": opening.get("door") if opening else None,
                "view_door": opening.get("view_door") if opening else None,
                "detail_scope": "cladding_edges_only",
            }
            for kind,start,end in visible_opening_edges(source_rect,rect,void_rects,tolerance):
                extent={"height_mm":_number(end[1]-start[1])} if start[0]==end[0] else {"width_mm":_number(end[0]-start[0])}
                append(kind,zone,{"x_mm":_number(start[0]),"y_mm":_number(start[1]),**extent},extra)

    elevation_order = {
        context.report["id"]: order
        for order, context in enumerate(
            context
            for contexts in elevations_by_space.values()
            for context in contexts
        )
    }
    kind_order = {kind: index for index, kind in enumerate(sorted(_NODE_CONTEXTS))}
    requirements.sort(
        key=lambda item: (
            elevation_order.get(item["elevation_id"], 10**9),
            kind_order[item["kind"]],
            item["id"],
        )
    )
    return requirements


def _build_schedule(
    project: Project,
    layouts: list[dict[str, Any]],
    panel_systems: dict[str, PanelSystem],
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for layout in layouts:
        grouped[layout["panel_system_id"]].append(layout)
    systems: list[dict[str, Any]] = []
    profiles: list[dict[str, Any]] = []
    for panel_system in project.panel_systems:
        system_layouts = grouped.get(panel_system.id, [])
        if not system_layouts:
            continue
        stock_counts: Counter[float] = Counter()
        for layout in system_layouts:
            stock_counts.update(panel["stock_width_mm"] for panel in layout["panels"])
        metric_keys = [
            "zone_envelope_area_mm2",
            "joint_gap_area_mm2",
            "panel_gross_area_mm2",
            "excluded_opening_envelope_area_mm2",
            "opening_cutout_area_mm2",
            "net_panel_area_mm2",
            "nominal_stock_strip_area_mm2",
            "width_cut_waste_area_mm2",
        ]
        systems.append(
            {
                "panel_system_id": panel_system.id,
                "name": panel_system.name,
                "version": panel_system.version,
                "status": panel_system.status,
                "zone_count": len(system_layouts),
                "valid_layout_count": sum(layout["valid"] for layout in system_layouts),
                "panel_count": sum(len(layout["panels"]) for layout in system_layouts),
                "material_piece_count": sum(
                    len(panel["material_pieces"])
                    for layout in system_layouts
                    for panel in layout["panels"]
                ),
                "stock_width_counts": [
                    {"stock_width_mm": width, "count": stock_counts[width]}
                    for width in sorted(stock_counts)
                ],
                "metrics": {
                    key: _number(sum(layout["metrics"][key] for layout in system_layouts))
                    for key in metric_keys
                },
            }
        )
        for code in dict.fromkeys(panel_system.profile_codes):
            profiles.append(
                {
                    "panel_system_id": panel_system.id,
                    "profile_code": code,
                    "quantity": None,
                    "quantity_status": "not_calculated",
                    "reason": "Модель не содержит правил длины, нахлёстов и комплектации профиля.",
                }
            )
    opening_counts: Counter[str] = Counter()
    unique_openings: set[str] = set()
    for building in project.buildings:
        for floor_item in building.floors:
            for opening in floor_item.openings:
                if opening.id not in unique_openings:
                    unique_openings.add(opening.id)
                    opening_counts[opening.kind] += 1
    return {
        "basis": {
            "quantities": "derived_from_cladding_zone_elevation_geometry",
            "procurement_ready": False,
            "profile_and_fastener_quantities_calculated": False,
        },
        "panel_systems": systems,
        "profiles": profiles,
        "openings": {
            "unique_count": len(unique_openings),
            "by_kind": [
                {"kind": kind, "count": opening_counts[kind]}
                for kind in sorted(opening_counts)
            ],
        },
        "zones": [
            {
                "zone_id": layout["zone_id"],
                "layout_id": layout["id"],
                "panel_system_id": layout["panel_system_id"],
                "valid": layout["valid"],
                "panel_count": len(layout["panels"]),
                "material_piece_count": sum(len(panel["material_pieces"]) for panel in layout["panels"]),
                "metrics": deepcopy(layout["metrics"]),
            }
            for layout in layouts
        ],
    }


def build_engineering(project: Project, *, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a deterministic, JSON-safe engineering derivation.

    ``project`` remains the canonical source.  The output uses wall reference
    lines and rectangular elevation geometry; it does not modify the project,
    approve node candidates, or calculate unmodelled fixings/profile lengths.
    """

    if not isinstance(project, Project):
        raise TypeError("project must be backend.domain.models.Project")
    if catalog is not None and not isinstance(catalog, dict):
        raise TypeError("catalog must be dict or None")

    geometry_report = validate_project(project)
    issues: list[dict[str, Any]] = [item.model_dump(mode="json") for item in geometry_report.issues]
    tolerance = float(project.settings.geometry_tolerance_mm)
    panel_systems = {item.id: item for item in project.panel_systems}

    catalog_result = validate_catalog(catalog) if catalog is not None else None
    normalized_catalog: dict[str, Any] | None = None
    if catalog_result is None:
        issues.append(
            _issue(
                "NODE_CATALOG_REQUIRED",
                "error",
                "Для производственного выпуска требуется реальный проверенный каталог узлов.",
                "/catalog",
            )
        )
    elif not catalog_result["valid"]:
        issues.extend(catalog_result["issues"])
        issues.append(
            _issue(
                "NODE_CATALOG_INVALID",
                "error",
                "Каталог узлов не прошёл строгую проверку.",
                "/catalog",
            )
        )
    else:
        normalized_catalog = catalog_result["normalized"]
        if normalized_catalog["sample"]:
            issues.append(
                _issue(
                    "NODE_CATALOG_SAMPLE_ONLY",
                    "error",
                    "Синтетический пример каталога нельзя использовать для производственного выпуска.",
                    "/catalog/sample",
                )
            )

    elevations: list[dict[str, Any]] = []
    elevation_contexts: list[_ElevationContext] = []
    elevations_by_space: dict[str, list[_ElevationContext]] = defaultdict(list)
    elevation_by_face: dict[str, _ElevationContext] = {}
    floor_openings: dict[str, dict[str, Opening]] = {}
    floor_zone_paths: dict[str, str] = {}
    zones_by_id: dict[str, CladdingZone] = {}
    obstacle_count = 0

    for building_index, building in enumerate(project.buildings):
        for floor_index, floor_item in enumerate(building.floors):
            floor_path = f"/buildings/{building_index}/floors/{floor_index}"
            for opening_index, opening in enumerate(floor_item.openings):
                for problem in door_parameter_issues(opening):
                    issues.append(_issue(problem["code"], "warning", problem["message"],
                                         f"{floor_path}/openings/{opening_index}/door", [opening.id], problem["details"]))
            for obstacle_index, obstacle in enumerate(floor_item.obstacles):
                obstacle_count += 1
                issues.append(
                    _issue(
                        "OBSTACLE_CLADDING_NOT_RESOLVED",
                        "warning",
                        "Проекция препятствия на развёртки и её влияние на облицовку ещё не рассчитываются; "
                        "требуется отдельная проверка проектировщиком.",
                        f"{floor_path}/obstacles/{obstacle_index}",
                        [obstacle.id],
                        {
                            "obstacle_kind": obstacle.kind,
                            "obstacle_name": obstacle.name,
                            "space_id": obstacle.space_id,
                        },
                    )
                )
            walls = {wall.id: wall for wall in floor_item.walls}
            openings = {opening.id: opening for opening in floor_item.openings}
            floor_openings[floor_item.id] = openings
            zones_for_face: dict[str, list[CladdingZone]] = defaultdict(list)
            for zone_index, zone in enumerate(floor_item.cladding_zones):
                zones_for_face[zone.wall_face_id].append(zone)
                zones_by_id[zone.id] = zone
                floor_zone_paths[zone.id] = f"{floor_path}/cladding_zones/{zone_index}"
            for space_index, space in enumerate(floor_item.spaces):
                space_path = f"{floor_path}/spaces/{space_index}"
                for sequence_index, boundary in enumerate(space.boundary):
                    wall = walls.get(boundary.wall_id)
                    if wall is None:
                        continue
                    faces = [face for face in wall.faces if face.space_id == space.id]
                    if len(faces) != 1:
                        issues.append(
                            _issue(
                                "ENGINEERING_WALL_FACE_UNRESOLVED",
                                "error",
                                "Нельзя однозначно определить сторону стены для развёртки помещения.",
                                f"{space_path}/boundary/{sequence_index}",
                                [space.id, wall.id, *[face.id for face in faces]],
                                {"matching_face_count": len(faces)},
                            )
                        )
                        continue
                    face = faces[0]
                    length = _wall_length(wall)
                    elevation_id = f"elevation:{space.id}:{sequence_index:04d}:{wall.id}:{face.id}"
                    opening_entries: list[dict[str, Any]] = []
                    for opening in floor_item.openings:
                        if opening.wall_id != wall.id:
                            continue
                        source_offset = float(opening.offset_mm)
                        bottom = float(wall.base_mm + opening.sill_mm)
                        opening_entries.append(
                            {
                                "id": opening.id,
                                "name": opening.name,
                                "kind": opening.kind,
                                "source_offset_mm": _number(source_offset),
                                "view_offset_mm": _view_offset(face.side, length, source_offset, float(opening.width_mm)),
                                "width_mm": _number(float(opening.width_mm)),
                                "source_sill_mm": _number(float(opening.sill_mm)),
                                "bottom_mm": _number(bottom),
                                "height_mm": _number(float(opening.height_mm)),
                                "top_mm": _number(bottom + float(opening.height_mm)),
                                "absolute_bottom_mm": _number(float(floor_item.elevation_mm) + bottom),
                                "absolute_top_mm": _number(
                                    float(floor_item.elevation_mm) + bottom + float(opening.height_mm)
                                ),
                                **opening_details(opening, face.side),
                            }
                        )
                    opening_entries.sort(key=lambda item: (item["view_offset_mm"], item["id"]))
                    zone_entries: list[dict[str, Any]] = []
                    for zone in zones_for_face.get(face.id, []):
                        source_offset = float(zone.offset_mm)
                        bottom = float(wall.base_mm + zone.base_mm)
                        zone_entries.append(
                            {
                                "id": zone.id,
                                "name": zone.name,
                                "source_offset_mm": _number(source_offset),
                                "view_offset_mm": _view_offset(face.side, length, source_offset, float(zone.width_mm)),
                                "width_mm": _number(float(zone.width_mm)),
                                "source_base_mm": _number(float(zone.base_mm)),
                                "bottom_mm": _number(bottom),
                                "height_mm": _number(float(zone.height_mm)),
                                "top_mm": _number(bottom + float(zone.height_mm)),
                                "layout_id": f"layout:{zone.id}",
                            }
                        )
                    zone_entries.sort(key=lambda item: (item["view_offset_mm"], item["id"]))
                    report = {
                        "id": elevation_id,
                        "building_id": building.id,
                        "building_name": building.name,
                        "floor_id": floor_item.id,
                        "floor_name": floor_item.name,
                        "floor_elevation_mm": _number(float(floor_item.elevation_mm)),
                        "floor_levels": [
                            {
                                "id": level.id,
                                "name": level.name,
                                "kind": level.kind,
                                "relative_elevation_mm": _number(float(level.elevation_mm)),
                                "absolute_elevation_mm": _number(
                                    float(floor_item.elevation_mm) + float(level.elevation_mm)
                                ),
                            }
                            for level in floor_item.levels
                        ],
                        "space_id": space.id,
                        "space_code": space.code,
                        "space_name": space.name,
                        "room_height_mm": _number(float(space.height_mm)),
                        "sequence_index": sequence_index,
                        "wall_id": wall.id,
                        "wall_name": wall.name,
                        "wall_face_id": face.id,
                        "face_side": face.side,
                        "face_finish": face.finish,
                        "boundary_reversed": boundary.reversed,
                        "view_direction": "wall_end_to_start" if face.side == "right" else "wall_start_to_end",
                        "width_mm": _number(length),
                        "width_basis": "wall_reference_line",
                        "wall_base_mm": _number(float(wall.base_mm)),
                        "wall_height_mm": _number(float(wall.height_mm)),
                        "absolute_bottom_mm": _number(float(floor_item.elevation_mm + wall.base_mm)),
                        "absolute_top_mm": _number(float(floor_item.elevation_mm + wall.base_mm + wall.height_mm)),
                        "openings": opening_entries,
                        "zones": zone_entries,
                    }
                    context = _ElevationContext(
                        report=report,
                        building=building,
                        floor=floor_item,
                        space=space,
                        boundary=boundary,
                        wall=wall,
                        face=face,
                        path=f"{space_path}/boundary/{sequence_index}",
                    )
                    elevations.append(report)
                    elevation_contexts.append(context)
                    elevations_by_space[space.id].append(context)
                    if face.id in elevation_by_face:
                        issues.append(
                            _issue(
                                "ENGINEERING_FACE_HAS_MULTIPLE_ELEVATIONS",
                                "error",
                                "Одна сторона стены попала в несколько развёрток.",
                                context.path,
                                [face.id, elevation_by_face[face.id].space.id, space.id],
                            )
                        )
                    else:
                        elevation_by_face[face.id] = context

    zone_contexts: dict[str, _ZoneContext] = {}
    for zone_id, zone in zones_by_id.items():
        elevation = elevation_by_face.get(zone.wall_face_id)
        if elevation is None:
            issues.append(
                _issue(
                    "ZONE_ELEVATION_NOT_FOUND",
                    "error",
                    "Для зоны облицовки не найдена развёртка стороны стены помещения.",
                    floor_zone_paths[zone.id],
                    [zone.id, zone.wall_face_id],
                )
            )
            continue
        length = _wall_length(elevation.wall)
        x = _view_offset(elevation.face.side, length, float(zone.offset_mm), float(zone.width_mm))
        y = float(elevation.wall.base_mm + zone.base_mm)
        zone_contexts[zone.id] = _ZoneContext(
            zone=zone,
            zone_path=floor_zone_paths[zone.id],
            elevation=elevation,
            rect=(x, y, float(zone.width_mm), float(zone.height_mm)),
            source_offset=float(zone.offset_mm),
            source_base=float(zone.base_mm),
        )

    overlapping_zone_ids: set[str] = set()
    comparisons = 0
    zones_by_face: dict[str, list[_ZoneContext]] = defaultdict(list)
    for zone_context in zone_contexts.values():
        zones_by_face[zone_context.elevation.face.id].append(zone_context)
    overlap_limit_hit = False
    for face_id, face_zones in zones_by_face.items():
        ordered = sorted(face_zones, key=lambda item: (item.rect[0], item.rect[1], item.zone.id))
        for index, first in enumerate(ordered):
            for second in ordered[index + 1 :]:
                if second.rect[0] >= first.rect[0] + first.rect[2] - tolerance:
                    break
                comparisons += 1
                if comparisons > MAX_ZONE_OVERLAP_COMPARISONS:
                    overlap_limit_hit = True
                    break
                if _clip_rect(first.rect, second.rect, tolerance) is not None:
                    overlapping_zone_ids.update({first.zone.id, second.zone.id})
                    issues.append(
                        _issue(
                            "CLADDING_ZONE_OVERLAP",
                            "error",
                            "Зоны облицовки на одной стороне стены перекрываются.",
                            second.zone_path,
                            [first.zone.id, second.zone.id, face_id],
                        )
                    )
            if overlap_limit_hit:
                break
        if overlap_limit_hit:
            break
    if overlap_limit_hit:
        issues.append(
            _issue(
                "ZONE_OVERLAP_CHECK_LIMIT_EXCEEDED",
                "error",
                "Проверка перекрытия зон остановлена на безопасном пределе операций.",
                "/buildings",
                details={"maximum_comparisons": MAX_ZONE_OVERLAP_COMPARISONS},
            )
        )

    layouts: list[dict[str, Any]] = []
    for zone_id, context in zone_contexts.items():
        layout = _build_layout(
            context,
            panel_systems,
            floor_openings[context.elevation.floor.id],
            issues,
            tolerance,
            overlapping_zone_ids,
        )
        layouts.append(layout)
    layouts_by_zone = {layout["zone_id"]: layout for layout in layouts}

    if not layouts:
        issues.append(
            _issue(
                "ENGINEERING_SCOPE_EMPTY",
                "warning",
                "В проекте нет разрешённых зон облицовки для расчёта раскладки.",
                "/buildings",
            )
        )

    requirements = _build_node_requirements(
        elevations_by_space,
        zone_contexts,
        layouts_by_zone,
        panel_systems,
        tolerance,
    )
    _assign_candidates(requirements, normalized_catalog, issues)
    if requirements:
        issues.append(
            _issue(
                "NODE_SELECTIONS_REQUIRED",
                "error",
                "Кандидаты узлов требуют явного выбора и утверждения проектировщиком.",
                "/node_requirements",
                details={"unresolved_count": len(requirements)},
            )
        )

    # Supplier document provenance is independent from the drawable node
    # catalog. A manual reference is never counted as an approved node.
    from backend.vendor_design import build_vendor_report
    vendor_reference = build_vendor_report(project, requirements) if project.properties.get("vendor_design") is not None else None
    if vendor_reference is not None:
        issues.extend(vendor_reference["issues"])
        references = {r["node_requirement_id"]: r for r in vendor_reference["node_references"]}
        for requirement in requirements:
            if requirement["id"] in references:
                requirement["manual_reference"] = references[requirement["id"]]

    from ..cad_sections import section_readiness
    cad_sections = section_readiness(project)
    issues.extend(cad_sections["issues"])
    from ..wall_mount import wall_mount_report
    wall_mounting = wall_mount_report(project) if "wall_mounting" in project.properties else None
    if wall_mounting is not None:
        issues.extend(wall_mounting["issues"])
    schedule = _build_schedule(project, layouts, panel_systems)
    readiness_blocking_codes = {item["code"] for item in issues if item["severity"] == "error"}
    obstacles_resolved = obstacle_count == 0
    if not obstacles_resolved:
        readiness_blocking_codes.add("OBSTACLE_CLADDING_NOT_RESOLVED")
    blocking_codes = sorted(readiness_blocking_codes)
    layouts_valid = bool(layouts) and len(layouts) == len(zones_by_id) and all(layout["valid"] for layout in layouts)
    catalog_present = catalog is not None
    catalog_valid = bool(catalog_result and catalog_result["valid"])
    catalog_is_sample = bool(normalized_catalog and normalized_catalog["sample"])
    all_nodes_resolved = all(item["selection"]["status"] == "approved" for item in requirements)
    candidate_coverage = all(bool(item["candidates"]) for item in requirements)
    production_ready = (
        geometry_report.valid
        and layouts_valid
        and catalog_present
        and catalog_valid
        and not catalog_is_sample
        and candidate_coverage
        and all_nodes_resolved
        and obstacles_resolved
        and not blocking_codes
    )
    issue_counts: Counter[tuple[str, str]] = Counter(
        (item["code"], item["severity"]) for item in issues
    )
    issue_summary = {
        "error_count": sum(item["severity"] == "error" for item in issues),
        "warning_count": sum(item["severity"] == "warning" for item in issues),
        "by_code": [
            {"code": code, "severity": severity, "count": issue_counts[(code, severity)]}
            for code, severity in sorted(issue_counts)
        ],
    }

    return {
        "schema_version": ENGINEERING_SCHEMA_VERSION,
        "project_id": project.id,
        "project_name": project.metadata.name,
        "project_revision": project.revision,
        "source_snapshot_sha256": sha256(project.model_dump_json().encode()).hexdigest(),
        "units": "mm",
        "basis": {
            "horizontal": "wall_reference_line",
            "vertical": "floor_elevation_plus_wall_local_z",
            "area": "cladding_zone_elevation_envelope",
            "finished_dimensions_available": False,
            "notes": [
                "Ширины развёрток получены по опорным линиям стен, а не по чистовым граням.",
                "Толщина облицовки не преобразует размеры помещения в чистовые размеры.",
                "Opening.sill_mm и CladdingZone.base_mm интерпретируются относительно низа стены.",
            ],
        },
        "elevations": elevations,
        "layouts": layouts,
        "schedule": schedule,
        "node_requirements": requirements,
        "cad_sections": cad_sections,
        **({"wall_mounting": wall_mounting} if wall_mounting is not None else {}),
        **({"vendor_reference": vendor_reference} if vendor_reference is not None else {}),
        "issues": issues,
        "issue_summary": issue_summary,
        "readiness": {
            "geometry_valid": geometry_report.valid,
            "elevations_complete": len(elevation_contexts)
            == sum(len(space.boundary) for building in project.buildings for floor_item in building.floors for space in floor_item.spaces),
            "layouts_valid": layouts_valid,
            "catalog_present": catalog_present,
            "catalog_valid": catalog_valid,
            "catalog_is_sample": catalog_is_sample,
            "node_candidate_coverage": candidate_coverage,
            "all_node_requirements_resolved": all_nodes_resolved,
            "obstacle_cladding_resolved": obstacles_resolved,
            "production_ready": production_ready,
            "blocking_issue_codes": blocking_codes,
        },
    }
