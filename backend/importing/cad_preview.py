"""Bounded, selectable 2D display geometry, never authoritative room geometry."""
from __future__ import annotations

from itertools import islice
import math

MAX_ENTITY_SEGMENTS = 512
MAX_BLOCK_DEPTH = 4
MAX_BLOCK_ENTITIES = 512


def planar(entity) -> bool:
    normal = entity.dxf.get("extrusion", (0, 0, 1)) if entity.dxf.is_supported("extrusion") else (0, 0, 1)
    if any(not math.isfinite(float(v)) or abs(float(v) - wanted) > 1e-9 for v, wanted in zip(normal, (0, 0, 1))):
        return False
    kind = entity.dxftype()
    if kind == "POLYLINE":
        if not entity.is_2d_polyline:
            return False
        elevation = entity.dxf.get("elevation", (0, 0, 0))[2]
    else:
        elevation = entity.dxf.get("elevation", 0) if entity.dxf.is_supported("elevation") else 0
    return math.isfinite(float(elevation)) and abs(float(elevation)) < 1e-9


def xy(point):
    values = tuple(float(v) for v in point)
    if len(values) < 2 or not all(math.isfinite(v) for v in values) or (len(values) > 2 and abs(values[2]) > 1e-9):
        raise ValueError("not finite XY")
    return values[0], values[1]


def polyline_points(entity):
    if entity.dxftype() == "LWPOLYLINE":
        return [(float(x), float(y), float(b)) for x, y, b in entity.get_points("xyb")]
    return [(float(v.dxf.location[0]), float(v.dxf.location[1]), float(v.dxf.get("bulge", 0))) for v in entity.vertices]


def fitted_polyline(entity):
    """Legacy spline/curve-fit vertices must not masquerade as straight edges."""
    return entity.dxftype() == "POLYLINE" and (
        bool(int(entity.dxf.get("flags", 0)) & 6)
        or any(int(vertex.dxf.get("flags", 0)) & 27 for vertex in entity.vertices)
    )


def display_geometry(entity, *, budget=MAX_ENTITY_SEGMENTS, depth=0, _visited=None):
    """Return segments, exact flag, omission reason. INSERT is one selectable object.

    Sample arcs with at most 72 edges. Expansion is capped per source entity and
    by recursion depth, never resolves XREF files or tessellates 3D geometry.
    """
    if _visited is None:
        _visited = [0]
    _visited[0] += 1
    if _visited[0] > MAX_BLOCK_ENTITIES:
        return [], False, "достигнут лимит вложенных объектов"
    if budget <= 0:
        return [], False, "достигнут лимит предпросмотра"
    kind = entity.dxftype()
    segments, exact, reasons = [], True, []
    try:
        if not planar(entity):
            return [], False, "неплоская геометрия не проецируется автоматически"
        if kind == "LINE":
            segments = [(xy(entity.dxf.start), xy(entity.dxf.end))]
        elif kind in {"LWPOLYLINE", "POLYLINE"}:
            if fitted_polyline(entity):
                return [], False, "POLYLINE с curve-fit/spline-fit требует ручной обводки"
            points = polyline_points(entity)
            closed = bool(entity.closed) if kind == "LWPOLYLINE" else bool(entity.is_closed)
            if not all(math.isfinite(v) for p in points for v in p):
                return [], False, "некорректные координаты"
            if kind == "POLYLINE" and any(not math.isfinite(float(v.dxf.location[2])) or abs(float(v.dxf.location[2])) > 1e-9 for v in entity.vertices):
                return [], False, "ненулевые Z-координаты"
            if any(abs(p[2]) > 1e-9 for p in points):
                exact = False
                reasons.append("дуговые участки показаны приближённо")
                for child in islice(entity.virtual_entities(), MAX_BLOCK_ENTITIES + 1):
                    child_segments, _, why = display_geometry(child, budget=budget - len(segments), depth=depth, _visited=_visited)
                    segments.extend(child_segments)
                    if why and why not in reasons:
                        reasons.append(why)
                    if len(segments) >= budget:
                        reasons.append("достигнут лимит предпросмотра")
                        break
            else:
                vertices = [(x, y) for x, y, _ in points]
                segments = list(zip(vertices, vertices[1:]))
                if closed and vertices:
                    segments.append((vertices[-1], vertices[0]))
            if bool(entity.has_width):
                exact = False
                reasons.append("показана ось полилинии без её ширины")
        elif kind in {"ARC", "CIRCLE"}:
            center = xy(entity.dxf.center)
            radius = float(entity.dxf.radius)
            if not math.isfinite(radius) or radius <= 0:
                return [], False, "некорректный радиус"
            start = float(entity.dxf.start_angle) if kind == "ARC" else 0
            span = ((float(entity.dxf.end_angle) - start) % 360) if kind == "ARC" else 360
            if not math.isfinite(start) or not math.isfinite(span):
                return [], False, "некорректные углы"
            count = min(72, max(4, math.ceil(span / 5)), budget)
            points = [(center[0] + radius * math.cos(math.radians(start + span * i / count)),
                       center[1] + radius * math.sin(math.radians(start + span * i / count))) for i in range(count + 1)]
            segments = list(zip(points, points[1:]))
            exact = False
            reasons.append("кривая показана приближённо; не контур помещения")
        elif kind == "INSERT":
            if depth >= MAX_BLOCK_DEPTH:
                return [], False, "достигнут лимит вложенности блоков"
            block = entity.block()
            if block is None or block.block.is_xref or block.block.is_xref_overlay:
                return [], False, "внешняя ссылка не загружается"
            exact = False
            reasons.append("блок показан как единый объект; не контур помещения")
            if int(entity.dxf.get("row_count", 1)) > 1 or int(entity.dxf.get("column_count", 1)) > 1:
                reasons.append("массив вставок: показан только первый экземпляр")
            for index, child in enumerate(islice(entity.virtual_entities(), MAX_BLOCK_ENTITIES + 1)):
                if index >= MAX_BLOCK_ENTITIES or len(segments) >= budget:
                    reasons.append("достигнут лимит предпросмотра блока")
                    break
                child_segments, _, why = display_geometry(child, budget=budget - len(segments), depth=depth + 1, _visited=_visited)
                segments.extend(child_segments)
                if why and why not in reasons:
                    reasons.append(why)
        else:
            return [], False, "тип объекта пока не отображается"
    except (ValueError, TypeError, AttributeError, OverflowError, ZeroDivisionError):
        return [], False, "геометрия объекта не может быть безопасно показана"
    if len(segments) > budget:
        segments = segments[:budget]
        exact = False
        reasons.append("достигнут лимит предпросмотра")
    return segments, exact, "; ".join(reasons) if reasons else None
