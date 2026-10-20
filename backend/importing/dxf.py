"""Strict DXF inspection and explicit room-contour conversion."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from pathlib import Path
import re
import tempfile
from typing import Iterable

import ezdxf
from ezdxf import units
from ezdxf.lldxf.const import DXFError

from backend.domain.models import (
    BoundaryEdge,
    Building,
    Floor,
    Point2,
    Project,
    ProjectMetadata,
    Space,
    Wall,
    WallFace,
    new_id,
)
from backend.geometry.validation import validate_project

from ._common import (
    MAX_CONVERSION_SEGMENTS,
    MAX_DXF_CANDIDATES,
    MAX_DXF_ENTITIES,
    MAX_DXF_VERTICES,
    MAX_DXF_VERTICES_PER_ENTITY,
    MAX_PREVIEW_SEGMENTS,
    bounded_content,
    content_id,
    safe_filename,
)
from .errors import (
    AmbiguousTopologyError,
    ConvertedGeometryError,
    ImportSourceError,
    MalformedSourceError,
    RepairRequiredError,
    SelectionError,
    SourceLimitError,
    UnitError,
    UnsupportedGeometryError,
)
from .records import ImportReport, ImportResult
from .cad_preview import display_geometry, fitted_polyline, planar, polyline_points


DEFAULT_LAYER = "ROOMS"
_HANDLE_PATTERN = re.compile(r"^[0-9A-F]+$")
_UNIT_OVERRIDES = {
    "mm": units.MM,
    "millimeter": units.MM,
    "millimetre": units.MM,
    "cm": units.CM,
    "centimeter": units.CM,
    "centimetre": units.CM,
    "m": units.M,
    "meter": units.M,
    "metre": units.M,
    "in": units.IN,
    "inch": units.IN,
    "ft": units.FT,
    "foot": units.FT,
}
_LINEAR_EPSILON_MM = 1e-6
_SOURCE_EPSILON = 1e-9
_POINT_DECIMALS = 6


@dataclass(frozen=True)
class _Units:
    declared_code: int
    declared_name: str
    effective_code: int | None
    effective_name: str | None
    millimetre_factor: float | None
    override: str | None
    requires_override: bool

    @property
    def coordinate_units(self) -> str:
        return "mm" if self.millimetre_factor is not None else "source"

    def as_dict(self) -> dict:
        return {
            "declared_code": self.declared_code,
            "declared_name": self.declared_name,
            "effective_code": self.effective_code,
            "effective_name": self.effective_name,
            "override": self.override,
            "requires_override": self.requires_override,
            "millimetre_factor": self.millimetre_factor,
            "coordinate_units": self.coordinate_units,
        }


@dataclass(frozen=True)
class _RoomInput:
    handle: str
    layer: str
    points: tuple[tuple[float, float], ...]
    space_id: str


@dataclass(frozen=True)
class _RawSegment:
    room_index: int
    segment_index: int
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass
class _AtomOccurrence:
    room_index: int
    segment_index: int
    atom_index: int
    start: tuple[float, float]
    end: tuple[float, float]
    wall_id: str | None = None
    reversed: bool = False


def _read_document(content: bytes, filename: str):
    payload = bounded_content(content)
    safe_name = safe_filename(filename)
    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf") as source:
            source.write(payload)
            source.flush()
            document = ezdxf.readfile(source.name)
    except (OSError, DXFError, UnicodeError, ValueError, TypeError, OverflowError) as exc:
        raise MalformedSourceError(f"Не удалось прочитать DXF {safe_name!r}: файл повреждён или имеет неверный формат") from exc

    try:
        auditor = document.audit()
    except (DXFError, UnicodeError, ValueError, TypeError, OverflowError) as exc:
        raise MalformedSourceError(f"Не удалось проверить структуру DXF {safe_name!r}") from exc
    if auditor.has_errors:
        raise MalformedSourceError(
            f"Проверка DXF выявила неисправимых ошибок: {len(auditor.errors)}",
            details={"audit_error_count": len(auditor.errors)},
        )
    if auditor.fixes:
        raise RepairRequiredError(
            f"DXF требует исправлений: {len(auditor.fixes)}. Автоматическое исправление отключено",
            details={"audit_fix_count": len(auditor.fixes)},
        )

    entities = []
    vertex_count = 0
    try:
        for entity in document.modelspace():
            entities.append(entity)
            if len(entities) > MAX_DXF_ENTITIES:
                raise SourceLimitError(
                    f"В DXF больше {MAX_DXF_ENTITIES} объектов пространства модели",
                    details={"max_entities": MAX_DXF_ENTITIES},
                )
            if entity.dxftype() == "LWPOLYLINE":
                count = len(entity)
                if count > MAX_DXF_VERTICES_PER_ENTITY:
                    raise SourceLimitError(
                        f"В объекте DXF {entity.dxf.get('handle', '?')} слишком много вершин",
                        details={"max_vertices_per_entity": MAX_DXF_VERTICES_PER_ENTITY},
                    )
                vertex_count += count
            elif entity.dxftype() == "POLYLINE":
                count = len(entity.vertices)
                if count > MAX_DXF_VERTICES_PER_ENTITY:
                    raise SourceLimitError(
                        f"В объекте DXF {entity.dxf.get('handle', '?')} слишком много вершин",
                        details={"max_vertices_per_entity": MAX_DXF_VERTICES_PER_ENTITY},
                    )
                vertex_count += count
            if vertex_count > MAX_DXF_VERTICES:
                raise SourceLimitError(
                    f"В полилиниях DXF больше {MAX_DXF_VERTICES} вершин",
                    details={"max_vertices": MAX_DXF_VERTICES},
                )
    except ImportSourceError:
        raise
    except (DXFError, ValueError, TypeError, OverflowError) as exc:
        raise MalformedSourceError("Не удалось прочитать пространство модели DXF") from exc
    return document, entities


def _unit_name(code: int) -> str:
    if code == 0:
        return "unitless"
    try:
        name = str(units.unit_name(code))
    except (TypeError, ValueError, IndexError):
        name = f"code-{code}"
    return name or f"code-{code}"


def _resolve_units(document, override: str | None, *, require_mm: bool) -> _Units:
    declared = int(document.units)
    normalised_override = override.strip().casefold() if isinstance(override, str) else None
    if override is not None and (not isinstance(override, str) or not normalised_override):
        raise UnitError("units_override должен содержать название единицы измерения")
    override_code = _UNIT_OVERRIDES.get(normalised_override) if normalised_override else None
    if normalised_override and override_code is None:
        raise UnitError(
            f"Единица измерения {override!r} не поддерживается",
            details={"accepted": ["mm", "cm", "m", "in", "ft"]},
        )

    if declared == 0 and override_code is None:
        if require_mm:
            raise UnitError(
                "В DXF не задана $INSUNITS; укажите units_override явно"
            )
        return _Units(0, "unitless", None, None, None, None, True)

    selected = int(override_code if declared == 0 else declared)
    if declared != 0 and override_code is not None and selected != int(override_code):
        raise UnitError(
            "units_override противоречит $INSUNITS, указанной в DXF; "
            "импорт не будет молча переопределять единицы файла",
            details={"declared_code": declared, "override": normalised_override},
        )
    try:
        factor = float(units.conversion_factor(selected, units.MM))
    except (TypeError, ValueError, ZeroDivisionError, IndexError) as exc:
        raise UnitError(
            f"Единицы DXF {_unit_name(selected)!r} нельзя перевести в миллиметры"
        ) from exc
    if not math.isfinite(factor) or factor <= 0.0:
        raise UnitError("При переводе единиц DXF получен некорректный коэффициент")
    return _Units(
        declared_code=declared,
        declared_name=_unit_name(declared),
        effective_code=selected,
        effective_name=_unit_name(selected),
        millimetre_factor=factor,
        override=normalised_override,
        requires_override=False,
    )


def _normal_vector(entity) -> tuple[float, float, float]:
    extrusion = entity.dxf.get("extrusion", (0.0, 0.0, 1.0))
    try:
        return tuple(float(value) for value in extrusion)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MalformedSourceError("У объекта DXF некорректный вектор extrusion") from exc


def _lwpolyline_assessment(entity) -> dict:
    handle = str(entity.dxf.get("handle", "")).upper()
    layer = str(entity.dxf.get("layer", "0"))
    closed = bool(entity.closed)
    straight = not bool(entity.has_arc)
    count = len(entity)
    reasons: list[str] = []
    if not handle:
        reasons.append("у объекта нет устойчивого handle")
    if not closed:
        reasons.append("полилиния не замкнута")
    if not straight:
        reasons.append("полилиния содержит дуги или ненулевой bulge")
    if bool(entity.has_width):
        reasons.append("у полилинии задана ненулевая ширина")
    try:
        elevation = float(entity.dxf.get("elevation", 0.0))
    except (TypeError, ValueError, OverflowError):
        elevation = math.nan
    if not math.isfinite(elevation) or not math.isclose(
        elevation, 0.0, abs_tol=_SOURCE_EPSILON
    ):
        reasons.append("полилиния находится не на нулевой отметке")
    try:
        extrusion = _normal_vector(entity)
    except MalformedSourceError:
        extrusion = (math.nan, math.nan, math.nan)
    if len(extrusion) != 3 or any(
        not math.isfinite(actual)
        or not math.isclose(actual, expected, rel_tol=0.0, abs_tol=_SOURCE_EPSILON)
        for actual, expected in zip(extrusion, (0.0, 0.0, 1.0))
    ):
        reasons.append("полилиния использует наклонную OCS")
    if count < 3:
        reasons.append("у полилинии меньше трёх вершин")
    if count > MAX_DXF_VERTICES_PER_ENTITY:
        reasons.append("полилиния превышает лимит вершин")
    return {
        "handle": handle,
        "layer": layer,
        "entity_type": "LWPOLYLINE",
        "closed": closed,
        "straight": straight,
        "vertex_count": count,
        "supported": not reasons,
        "reason": "; ".join(reasons) if reasons else None,
    }


def _polyline_assessment(entity) -> dict:
    if entity.dxftype() == "LWPOLYLINE":
        return _lwpolyline_assessment(entity)
    reasons = []
    vertices = entity.vertices
    if not entity.is_2d_polyline or not planar(entity):
        reasons.append("поддерживается только плоская 2D POLYLINE на нулевой отметке")
    closed = bool(entity.is_closed)
    straight = not bool(entity.has_arc)
    if not closed:
        reasons.append("полилиния не замкнута")
    if not straight:
        reasons.append("полилиния содержит дуги или ненулевой bulge")
    if bool(entity.has_width):
        reasons.append("у полилинии задана ненулевая ширина")
    if fitted_polyline(entity):
        reasons.append("POLYLINE с curve-fit/spline-fit не является прямолинейным контуром")
    if any(not math.isfinite(float(v.dxf.location[2])) or abs(float(v.dxf.location[2])) > _SOURCE_EPSILON for v in vertices):
        reasons.append("вершины находятся не на нулевой отметке")
    if len(vertices) < 3:
        reasons.append("у полилинии меньше трёх вершин")
    return {"handle": str(entity.dxf.get("handle", "")).upper(), "layer": str(entity.dxf.get("layer", "0")),
            "entity_type": "POLYLINE", "closed": closed, "straight": straight, "vertex_count": len(vertices),
            "supported": not reasons, "reason": "; ".join(reasons) if reasons else None}


def _finite_xy(point) -> tuple[float, float] | None:
    try:
        x, y = float(point[0]), float(point[1])
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    return x, y


def _preview_segments_for_entity(entity) -> tuple[list[tuple[tuple[float, float], tuple[float, float]]], bool]:
    """Return deliberately shallow linework; never expand INSERT references."""

    kind = entity.dxftype()
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    skipped = False
    try:
        if kind == "LINE":
            start = _finite_xy(entity.dxf.start)
            end = _finite_xy(entity.dxf.end)
            start_z = float(entity.dxf.start[2])
            end_z = float(entity.dxf.end[2])
            if (
                start is not None
                and end is not None
                and math.isclose(start_z, 0.0, abs_tol=_SOURCE_EPSILON)
                and math.isclose(end_z, 0.0, abs_tol=_SOURCE_EPSILON)
            ):
                segments.append((start, end))
            else:
                skipped = True
        elif kind == "LWPOLYLINE":
            elevation = float(entity.dxf.get("elevation", 0.0))
            normal = _normal_vector(entity)
            if not math.isclose(elevation, 0.0, abs_tol=_SOURCE_EPSILON) or any(
                not math.isclose(actual, expected, abs_tol=_SOURCE_EPSILON)
                for actual, expected in zip(normal, (0.0, 0.0, 1.0))
            ):
                return [], True
            points = [(float(x), float(y), float(bulge)) for x, y, bulge in entity.get_points("xyb")]
            pairs = list(zip(points, points[1:]))
            if entity.closed and points:
                pairs.append((points[-1], points[0]))
            for first, second in pairs:
                if not all(math.isfinite(value) for value in (*first, *second)):
                    skipped = True
                    continue
                if not math.isclose(first[2], 0.0, abs_tol=_SOURCE_EPSILON):
                    skipped = True
                    continue
                segments.append(((first[0], first[1]), (second[0], second[1])))
        elif kind == "POLYLINE":
            vertices = list(entity.vertices)
            pairs = list(zip(vertices, vertices[1:]))
            if entity.is_closed and vertices:
                pairs.append((vertices[-1], vertices[0]))
            for first, second in pairs:
                start = _finite_xy(first.dxf.location)
                end = _finite_xy(second.dxf.location)
                start_z = float(first.dxf.location[2])
                end_z = float(second.dxf.location[2])
                bulge = float(first.dxf.get("bulge", 0.0))
                if (
                    start is None
                    or end is None
                    or not math.isclose(start_z, 0.0, abs_tol=_SOURCE_EPSILON)
                    or not math.isclose(end_z, 0.0, abs_tol=_SOURCE_EPSILON)
                    or not math.isclose(bulge, 0.0, abs_tol=_SOURCE_EPSILON)
                ):
                    skipped = True
                    continue
                segments.append((start, end))
        else:
            skipped = True
    except (DXFError, ValueError, TypeError, OverflowError, AttributeError):
        return [], True
    return segments, skipped


def inspect_dxf(
    filename: str,
    content: bytes,
    *,
    units_override: str | None = None,
) -> dict:
    """Inspect bounded DXF bytes without recovering or mutating the source."""

    safe_name = safe_filename(filename)
    payload = bounded_content(content)
    document, entities = _read_document(payload, safe_name)
    unit_info = _resolve_units(document, units_override, require_mm=False)
    factor = unit_info.millimetre_factor or 1.0
    coordinate_units = unit_info.coordinate_units

    type_counts: Counter[str] = Counter()
    layer_counts: dict[str, Counter[str]] = {}
    candidates: list[dict] = []
    candidate_count_by_layer: Counter[str] = Counter()
    preview_segments: list[dict] = []
    preview_truncated = False
    preview_skipped_types: Counter[str] = Counter()
    selectable_entities: list[dict] = []
    omitted_entities: list[dict] = []
    shallow_bounds: list[float] | None = None

    known_layers = {str(layer.dxf.name) for layer in document.layers}
    for entity in entities:
        entity_type = entity.dxftype()
        layer_name = str(entity.dxf.get("layer", "0"))
        known_layers.add(layer_name)
        type_counts[entity_type] += 1
        layer_counts.setdefault(layer_name, Counter())[entity_type] += 1
        assessment = None
        if entity_type in {"LWPOLYLINE", "POLYLINE"}:
            candidate_count_by_layer[layer_name] += 1
            assessment = _polyline_assessment(entity)
            if len(candidates) < MAX_DXF_CANDIDATES:
                candidates.append(assessment)

        remaining = MAX_PREVIEW_SEGMENTS - len(preview_segments)
        raw_segments, exact_geometry, reason = display_geometry(entity, budget=min(512, remaining))
        handle = str(entity.dxf.get("handle", "")).upper()
        if reason:
            preview_skipped_types[entity_type] += 1
            if len(omitted_entities) < MAX_DXF_CANDIDATES:
                omitted_entities.append({"handle": handle, "entity_type": entity_type, "layer": layer_name, "reason": reason})
        if remaining <= 0 or (reason and "лимит" in reason):
            preview_truncated = True
        scaled_segments = []
        for start, end in raw_segments:
            for x, y in (start, end):
                if shallow_bounds is None:
                    shallow_bounds = [x, y, x, y]
                else:
                    shallow_bounds[0] = min(shallow_bounds[0], x)
                    shallow_bounds[1] = min(shallow_bounds[1], y)
                    shallow_bounds[2] = max(shallow_bounds[2], x)
                    shallow_bounds[3] = max(shallow_bounds[3], y)
            if len(preview_segments) >= MAX_PREVIEW_SEGMENTS:
                preview_truncated = True
                continue
            segment = {
                    "start": {"x": start[0] * factor, "y": start[1] * factor},
                    "end": {"x": end[0] * factor, "y": end[1] * factor},
                    "handle": handle,
                    "layer": layer_name,
                    "entity_type": entity_type,
                }
            if not all(math.isfinite(v) for p in (segment["start"], segment["end"]) for v in p.values()):
                raise MalformedSourceError("Координаты DXF переполняются при переводе в миллиметры")
            preview_segments.append(segment)
            scaled_segments.append({"start": segment["start"], "end": segment["end"]})
        if scaled_segments:
            closed = bool(assessment["closed"]) if assessment else entity_type == "CIRCLE"
            points = [segment["start"] for segment in scaled_segments]
            if not closed:
                points.append(scaled_segments[-1]["end"])
            selectable_entities.append({
                "handle": handle, "entity_type": entity_type, "layer": layer_name,
                "block_name": str(entity.dxf.name) if entity_type == "INSERT" else None,
                "points": points, "segments": scaled_segments, "closed": closed,
                "exact_geometry": exact_geometry, "room_supported": bool(assessment and assessment["supported"] and exact_geometry),
                "reason": reason or (assessment["reason"] if assessment else None),
            })
    bounds = None
    if shallow_bounds is not None:
        bounds = {
            "min_x": shallow_bounds[0] * factor,
            "min_y": shallow_bounds[1] * factor,
            "max_x": shallow_bounds[2] * factor,
            "max_y": shallow_bounds[3] * factor,
            "units": coordinate_units,
            "scope": "bounded_2d_preview",
        }

    warnings: list[str] = []
    layouts = [{"name": layout.name, "entity_count": len(layout)} for layout in document.layouts if layout.name != "Model"]
    if any(layout["entity_count"] for layout in layouts):
        warnings.append("В файле есть объекты на листах (Paper Space). Пока показывается только Model Space; экспортируйте нужный 2D-вид в пространство модели с масштабом 1:1.")
    if unit_info.requires_override:
        warnings.append(
            "В DXF не заданы единицы: координаты предпросмотра оставлены как в исходнике, "
            "а для преобразования нужно указать units_override."
        )
    if preview_truncated:
        warnings.append(f"Предпросмотр ограничен {MAX_PREVIEW_SEGMENTS} отрезками.")
    if preview_skipped_types:
        warnings.append(
            "Предпросмотр пропускает или упрощает неподдерживаемые и неплоские объекты; "
            "полный состав смотрите в перечне объектов."
        )
    if len(candidates) < sum(candidate_count_by_layer.values()):
        warnings.append(f"Список подходящих контуров ограничен {MAX_DXF_CANDIDATES} записями.")

    layers = [
        {
            "name": layer_name,
            "entity_count": sum(layer_counts.get(layer_name, {}).values()),
            "entity_types": dict(sorted(layer_counts.get(layer_name, {}).items())),
            "candidate_count": int(candidate_count_by_layer[layer_name]),
        }
        for layer_name in sorted(known_layers, key=str.casefold)
    ]
    return {
        "schema_version": "1.0",
        "source_id": content_id(payload),
        "filename": safe_name,
        "format": "dxf",
        "media_type": "image/vnd.dxf",
        "byte_length": len(payload),
        "units": unit_info.as_dict(),
        "bounds": bounds,
        "entity_count": len(entities),
        "entity_types": dict(sorted(type_counts.items())),
        "layers": layers,
        "candidates": candidates,
        "candidates_truncated": len(candidates) < sum(candidate_count_by_layer.values()),
        "preview_segments": preview_segments,
        "preview_truncated": preview_truncated,
        "preview_skipped_entity_types": dict(sorted(preview_skipped_types.items())),
        "selectable_entities": selectable_entities,
        "omitted_entities": omitted_entities,
        "omitted_entities_truncated": sum(preview_skipped_types.values()) > len(omitted_entities),
        "layouts": layouts,
        "warnings": warnings,
        "missing_semantics": [
            "Сами по себе объекты DXF не определяют этажи, помещения, стены, проёмы и строительные узлы.",
            "Контуры помещений нужно выбрать явно по handle; произвольные линии не превращаются в помещения автоматически.",
            "Толщина стен, высота помещений, названия и коды не определяются по геометрии DXF.",
            "Обрабатывается пространство модели. Листы перечислены отдельно; внешние ссылки, proxy/AEC и 3D не преобразуются в помещения.",
        ],
    }


def _normalise_handles(handles: Iterable[str]) -> tuple[str, ...]:
    if isinstance(handles, (str, bytes)):
        raise SelectionError("handles должен быть списком идентификаторов объектов DXF")
    try:
        values = tuple(str(item).strip().upper() for item in handles)
    except TypeError as exc:
        raise SelectionError("handles должен быть перебираемым набором идентификаторов объектов DXF") from exc
    if not values or any(not value for value in values):
        raise SelectionError("Нужно выбрать хотя бы один непустой handle DXF")
    invalid = [value for value in values if len(value) > 32 or not _HANDLE_PATTERN.fullmatch(value)]
    if invalid:
        raise SelectionError(
            "Handle DXF может содержать только шестнадцатеричные символы",
            details={"invalid_handles": invalid[:10]},
        )
    duplicates = sorted(handle for handle, count in Counter(values).items() if count > 1)
    if duplicates:
        raise SelectionError(
            "Повторяющиеся handle DXF не допускаются",
            details={"duplicate_handles": duplicates},
        )
    return values


def _straight_vertices(entity, factor: float) -> tuple[tuple[float, float], ...]:
    assessment = _polyline_assessment(entity)
    if not assessment["supported"]:
        raise UnsupportedGeometryError(
            f"Выбранная LWPOLYLINE {assessment['handle']} не поддерживается: {assessment['reason']}",
            details=assessment,
        )
    raw: list[tuple[float, float]] = []
    try:
        for x, y, bulge in polyline_points(entity):
            values = float(x), float(y), float(bulge)
            if not all(math.isfinite(value) for value in values):
                raise UnsupportedGeometryError(
                    f"У выбранной LWPOLYLINE {assessment['handle']} есть нечисловые или бесконечные координаты"
                )
            if not math.isclose(values[2], 0.0, abs_tol=_SOURCE_EPSILON):
                raise UnsupportedGeometryError(
                    f"Выбранная LWPOLYLINE {assessment['handle']} содержит ненулевой bulge"
                )
            raw.append((values[0] * factor, values[1] * factor))
    except ImportSourceError:
        raise
    except (DXFError, ValueError, TypeError, OverflowError) as exc:
        raise MalformedSourceError(
            f"Не удалось прочитать вершины LWPOLYLINE {assessment['handle']}"
        ) from exc

    if len(raw) > 1 and _distance(raw[0], raw[-1]) <= _LINEAR_EPSILON_MM:
        raw.pop()
    if len(raw) < 3:
        raise UnsupportedGeometryError(
            f"У выбранной LWPOLYLINE {assessment['handle']} меньше трёх различных вершин"
        )
    for index, (start, end) in enumerate(zip(raw, raw[1:] + raw[:1]), start=1):
        if _distance(start, end) <= _LINEAR_EPSILON_MM:
            raise UnsupportedGeometryError(
                f"В выбранной LWPOLYLINE {assessment['handle']} отрезок {index} имеет нулевую длину"
            )
    area2 = _signed_area2(raw)
    if math.isclose(area2, 0.0, abs_tol=_LINEAR_EPSILON_MM**2):
        raise UnsupportedGeometryError(
            f"У выбранной LWPOLYLINE {assessment['handle']} нулевая площадь"
        )
    if area2 < 0.0:
        raw.reverse()
    return tuple(raw)


def _signed_area2(points: list[tuple[float, float]]) -> float:
    origin = points[0]
    translated = [(point[0] - origin[0], point[1] - origin[1]) for point in points]
    return sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(translated, translated[1:] + translated[:1])
    )


def _distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])


def _sub(first: tuple[float, float], second: tuple[float, float]) -> tuple[float, float]:
    return first[0] - second[0], first[1] - second[1]


def _dot(first: tuple[float, float], second: tuple[float, float]) -> float:
    return first[0] * second[0] + first[1] * second[1]


def _cross(first: tuple[float, float], second: tuple[float, float]) -> float:
    return first[0] * second[1] - first[1] * second[0]


def _point_parameter(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    direction = _sub(end, start)
    length_sq = _dot(direction, direction)
    if length_sq == 0.0:
        return 0.0
    return _dot(_sub(point, start), direction) / length_sq


def _point_on_segment(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> bool:
    direction = _sub(end, start)
    length = _distance(start, end)
    if length <= _LINEAR_EPSILON_MM:
        return False
    distance_to_line = abs(_cross(direction, _sub(point, start))) / length
    if distance_to_line > _LINEAR_EPSILON_MM:
        return False
    parameter = _point_parameter(point, start, end)
    parameter_epsilon = _LINEAR_EPSILON_MM / length
    return -parameter_epsilon <= parameter <= 1.0 + parameter_epsilon


def _canonical_point(point: tuple[float, float]) -> tuple[float, float]:
    x = round(point[0], _POINT_DECIMALS)
    y = round(point[1], _POINT_DECIMALS)
    return (0.0 if x == -0.0 else x, 0.0 if y == -0.0 else y)


def _segment_key(
    start: tuple[float, float], end: tuple[float, float]
) -> tuple[tuple[float, float], tuple[float, float]]:
    first = _canonical_point(start)
    second = _canonical_point(end)
    return (first, second) if first < second else (second, first)


def _split_boundaries(rooms: list[_RoomInput]):
    raw_segments: list[_RawSegment] = []
    for room_index, room in enumerate(rooms):
        for segment_index, (start, end) in enumerate(
            zip(room.points, room.points[1:] + room.points[:1]), start=1
        ):
            raw_segments.append(_RawSegment(room_index, segment_index, start, end))
    if len(raw_segments) > MAX_CONVERSION_SEGMENTS:
        raise SourceLimitError(
            f"В выбранных контурах больше {MAX_CONVERSION_SEGMENTS} граничных отрезков",
            details={"max_conversion_segments": MAX_CONVERSION_SEGMENTS},
        )

    split_parameters: list[list[float]] = [[0.0, 1.0] for _ in raw_segments]
    for first_index, first in enumerate(raw_segments):
        for second_index in range(first_index + 1, len(raw_segments)):
            second = raw_segments[second_index]
            for point in (second.start, second.end):
                if _point_on_segment(point, first.start, first.end):
                    split_parameters[first_index].append(
                        min(1.0, max(0.0, _point_parameter(point, first.start, first.end)))
                    )
            for point in (first.start, first.end):
                if _point_on_segment(point, second.start, second.end):
                    split_parameters[second_index].append(
                        min(1.0, max(0.0, _point_parameter(point, second.start, second.end)))
                    )

    occurrences: list[_AtomOccurrence] = []
    by_room: list[list[_AtomOccurrence]] = [[] for _ in rooms]
    for raw_index, segment in enumerate(raw_segments):
        length = _distance(segment.start, segment.end)
        parameter_epsilon = _LINEAR_EPSILON_MM / max(length, _LINEAR_EPSILON_MM)
        ordered: list[float] = []
        for value in sorted(split_parameters[raw_index]):
            if not ordered or abs(value - ordered[-1]) > parameter_epsilon:
                ordered.append(value)
        direction = _sub(segment.end, segment.start)
        for atom_index, (start_parameter, end_parameter) in enumerate(
            zip(ordered, ordered[1:]), start=1
        ):
            start = (
                segment.start[0] + direction[0] * start_parameter,
                segment.start[1] + direction[1] * start_parameter,
            )
            end = (
                segment.start[0] + direction[0] * end_parameter,
                segment.start[1] + direction[1] * end_parameter,
            )
            start = _canonical_point(start)
            end = _canonical_point(end)
            if _distance(start, end) <= _LINEAR_EPSILON_MM:
                continue
            occurrence = _AtomOccurrence(
                room_index=segment.room_index,
                segment_index=segment.segment_index,
                atom_index=atom_index,
                start=start,
                end=end,
            )
            occurrences.append(occurrence)
            by_room[segment.room_index].append(occurrence)
    return raw_segments, occurrences, by_room


def _build_project(
    rooms: list[_RoomInput],
    *,
    source_id: str,
    filename: str,
    unit_info: _Units,
    project_name: str,
    building_name: str,
    floor_name: str,
    room_height_mm: float,
    wall_thickness_mm: float,
):
    raw_segments, occurrences, by_room = _split_boundaries(rooms)
    grouped: dict[
        tuple[tuple[float, float], tuple[float, float]], list[_AtomOccurrence]
    ] = {}
    for occurrence in occurrences:
        grouped.setdefault(_segment_key(occurrence.start, occurrence.end), []).append(occurrence)

    floor = Floor(
        name=floor_name,
        elevation_mm=0.0,
        default_height_mm=room_height_mm,
    )
    shared_wall_count = 0
    for wall_number, (key, owners) in enumerate(grouped.items(), start=1):
        unique_rooms = {owner.room_index for owner in owners}
        if len(owners) > 2 or len(unique_rooms) != len(owners):
            raise AmbiguousTopologyError(
                "Один разделённый отрезок границы принадлежит одному помещению повторно "
                "или более чем двум помещениям",
                details={
                    "segment": [list(key[0]), list(key[1])],
                    "source_handles": sorted({rooms[item.room_index].handle for item in owners}),
                },
            )
        first = owners[0]
        first_start = _canonical_point(first.start)
        first_end = _canonical_point(first.end)
        left_space_id = rooms[first.room_index].space_id
        right_space_id = None
        if len(owners) == 2:
            second = owners[1]
            second_start = _canonical_point(second.start)
            second_end = _canonical_point(second.end)
            if first_start == second_start and first_end == second_end:
                raise AmbiguousTopologyError(
                    "Два выбранных контура проходят совпадающую границу в одном направлении",
                    details={
                        "segment": [list(key[0]), list(key[1])],
                        "source_handles": [
                            rooms[first.room_index].handle,
                            rooms[second.room_index].handle,
                        ],
                    },
                )
            if not (first_start == second_end and first_end == second_start):
                raise AmbiguousTopologyError(
                    "Совпадающие границы помещений не удалось свести к одной общей стене"
                )
            second.reversed = True
            right_space_id = rooms[second.room_index].space_id
            shared_wall_count += 1

        wall = Wall(
            name=f"DXF / {wall_number}",
            start=Point2(x=first_start[0], y=first_start[1]),
            end=Point2(x=first_end[0], y=first_end[1]),
            thickness_mm=wall_thickness_mm,
            height_mm=room_height_mm,
            faces=[
                WallFace(side="left", space_id=left_space_id),
                WallFace(side="right", space_id=right_space_id),
            ],
            properties={
                "source_format": "DXF",
                "source_id": source_id,
                "source_filename": filename,
                "source_handles": [rooms[item.room_index].handle for item in owners],
                "source_layers": [rooms[item.room_index].layer for item in owners],
                "source_segment_indices": [item.segment_index for item in owners],
            },
        )
        floor.walls.append(wall)
        for owner in owners:
            owner.wall_id = wall.id

    for room_index, room in enumerate(rooms, start=1):
        room_occurrences = by_room[room_index - 1]
        if any(item.wall_id is None for item in room_occurrences):
            raise AmbiguousTopologyError("Внутренняя ошибка импорта: для участка границы не создана стена")
        floor.spaces.append(
            Space(
                id=room.space_id,
                code=f"DXF-{room_index:02d}",
                name=f"Импортированное помещение {room_index}",
                height_mm=room_height_mm,
                boundary=[
                    BoundaryEdge(wall_id=str(item.wall_id), reversed=item.reversed)
                    for item in room_occurrences
                ],
                properties={
                    "source_format": "DXF",
                    "source_id": source_id,
                    "source_filename": filename,
                    "source_handle": room.handle,
                    "source_layer": room.layer,
                    "source_units": unit_info.effective_name or "unknown",
                },
            )
        )

    floor.properties["source_import"] = {
        "source_id": source_id,
        "source_filename": filename,
        "source_format": "DXF",
        "selected_handles": [room.handle for room in rooms],
        "source_units": unit_info.effective_name,
        "millimetre_factor": unit_info.millimetre_factor,
        "scope": "explicit closed straight LWPOLYLINE room reference contours only",
    }
    project = Project(
        metadata=ProjectMetadata(
            name=project_name,
            description=(
                "Импорт явно выбранных замкнутых прямолинейных контуров DXF. "
                "Высоты, толщины и семантика не были получены из чертежа."
            ),
            author="Модуль импорта DXF",
        ),
        buildings=[Building(name=building_name, floors=[floor])],
    )
    return project, len(raw_segments), len(occurrences), shared_wall_count


def _positive_number(value: float, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SelectionError(f"{name} должен быть положительным конечным числом") from exc
    if not math.isfinite(result) or result <= 0.0:
        raise SelectionError(f"{name} должен быть положительным конечным числом")
    return result


def convert_dxf(
    content: bytes,
    *,
    filename: str,
    handles: list[str],
    layer: str | None = None,
    units_override: str | None = None,
    project_name: str | None = None,
    building_name: str = "Импортированное здание",
    floor_name: str = "Импортированный этаж",
    room_height_mm: float = 3000.0,
    wall_thickness_mm: float = 150.0,
) -> ImportResult:
    """Convert only explicitly selected room contours into a new Project.

    Unselected entities are inventoried in the report but never promoted to
    walls, rooms or openings.  Full and partial collinear common boundaries
    are split into atomic segments and reused by adjacent rooms.
    """

    safe_name = safe_filename(filename)
    payload = bounded_content(content)
    selected_handles = _normalise_handles(handles)
    if layer is not None:
        if not isinstance(layer, str) or not layer.strip() or len(layer.strip()) > 255:
            raise SelectionError("layer должен быть непустой строкой длиной не более 255 символов")
        selected_layer = layer.strip()
    else:
        selected_layer = None
    height = _positive_number(room_height_mm, "room_height_mm")
    thickness = _positive_number(wall_thickness_mm, "wall_thickness_mm")

    document, entities = _read_document(payload, safe_name)
    unit_info = _resolve_units(document, units_override, require_mm=True)
    assert unit_info.millimetre_factor is not None
    source_id = content_id(payload)

    entities_by_handle: dict[str, object] = {}
    for entity in entities:
        handle = str(entity.dxf.get("handle", "")).upper()
        if not handle:
            continue
        if handle in entities_by_handle:
            raise MalformedSourceError(
                f"В пространстве модели DXF повторяется handle {handle}"
            )
        entities_by_handle[handle] = entity

    selected_entities = []
    rooms: list[_RoomInput] = []
    for handle in selected_handles:
        entity = entities_by_handle.get(handle)
        if entity is None:
            raise SelectionError(
                f"Выбранный handle {handle} отсутствует в пространстве модели",
                details={"handle": handle},
            )
        entity_layer = str(entity.dxf.get("layer", "0"))
        if selected_layer is not None and entity_layer.casefold() != selected_layer.casefold():
            raise SelectionError(
                f"Выбранный handle {handle} находится на слое {entity_layer!r}, а не {selected_layer!r}",
                details={"handle": handle, "actual_layer": entity_layer},
            )
        if entity.dxftype() not in {"LWPOLYLINE", "POLYLINE"}:
            raise UnsupportedGeometryError(
                f"Выбранный handle {handle} имеет тип {entity.dxftype()}, а нужен LWPOLYLINE или 2D POLYLINE",
                details={"handle": handle, "entity_type": entity.dxftype()},
            )
        selected_entities.append(entity)
        rooms.append(
            _RoomInput(
                handle=handle,
                layer=entity_layer,
                points=_straight_vertices(entity, unit_info.millimetre_factor),
                space_id=new_id(),
            )
        )

    name = project_name.strip() if isinstance(project_name, str) and project_name.strip() else None
    if project_name is not None and name is None:
        raise SelectionError("project_name не может быть пустым")
    if name is None:
        name = f"Импорт {Path(safe_name).stem}"
    project, raw_segment_count, atomic_segment_count, shared_wall_count = _build_project(
        rooms,
        source_id=source_id,
        filename=safe_name,
        unit_info=unit_info,
        project_name=name,
        building_name=building_name,
        floor_name=floor_name,
        room_height_mm=height,
        wall_thickness_mm=thickness,
    )
    validation = validate_project(project)
    if not validation.valid:
        errors = [issue for issue in validation.issues if issue.severity == "error"]
        first_message = errors[0].message if errors else "неизвестная ошибка геометрии"
        raise ConvertedGeometryError(
            f"Преобразованный проект не прошёл проверку геометрии: {first_message}",
            report=validation,
            details={"error_count": len(errors)},
        )

    ignored_types: Counter[str] = Counter()
    selected_identity = {id(entity) for entity in selected_entities}
    for entity in entities:
        if id(entity) not in selected_identity:
            ignored_types[entity.dxftype()] += 1
    ignored_count = sum(ignored_types.values())
    warnings = []
    if ignored_count:
        warnings.append(
            f"Невыбранные объекты пространства модели намеренно пропущены: {ignored_count}."
        )
    report = ImportReport(
        source_id=source_id,
        source_filename=safe_name,
        selected_handles=selected_handles,
        imported_space_count=len(rooms),
        imported_wall_count=len(project.buildings[0].floors[0].walls),
        shared_wall_count=shared_wall_count,
        split_segment_count=max(0, atomic_segment_count - raw_segment_count),
        ignored_entity_count=ignored_count,
        ignored_entity_types=dict(sorted(ignored_types.items())),
        warnings=tuple(warnings),
        missing_semantics=(
            "Названия и коды помещений созданы автоматически и требуют проверки.",
            "Высота помещений и толщина стен заданы параметрами импорта, а не прочитаны из DXF.",
            "Проёмы, окна, двери, отметки, материалы и отделка не импортированы.",
            "Контуры являются осями/референсными линиями; чистые размеры не установлены.",
        ),
        limitations=(
            "Импортированы только явно выбранные замкнутые прямолинейные LWPOLYLINE / 2D POLYLINE в плоскости XY.",
            "Дуги, bulge, ширина полилинии, блоки и произвольные линии не преобразуются в помещения.",
        ),
    )
    floor = project.buildings[0].floors[0]
    floor.properties["source_import_report"] = report.model_dump(mode="json")
    return ImportResult(
        project=project,
        report=report,
        validation=validation,
        source_id=source_id,
        imported_handles=selected_handles,
        source_unit_code=int(unit_info.effective_code),
        source_units=str(unit_info.effective_name),
        millimetre_factor=unit_info.millimetre_factor,
    )
