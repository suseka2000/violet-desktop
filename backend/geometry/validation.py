"""Deterministic, dependency-free validation of project geometry.

The validator deliberately works with wall reference lines.  Consequently,
room metrics in the report are *reference-line* area and perimeter and must not
be presented as finished/clean room measurements.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from typing import Iterable, Literal

from backend.domain.models import (
    Building,
    CladdingZone,
    DesignDimension,
    Entity,
    Floor,
    Issue,
    LevelDatum,
    Measurement,
    Obstacle,
    Opening,
    PanelSystem,
    Point2,
    Project,
    Space,
    ValidationReport,
    Wall,
    WallFace,
)

Point = tuple[float, float]
SegmentKind = Literal["none", "touch", "proper", "collinear_overlap"]


@dataclass(frozen=True)
class _EntityContext:
    entity: Entity
    path: str
    floor: Floor | None = None
    floor_path: str | None = None
    owner_wall: Wall | None = None


@dataclass
class _RoomGeometry:
    space: Space
    path: str
    floor: Floor
    segments: list[tuple[Point, Point]]
    vertices: list[Point]
    perimeter_mm: float | None
    area_mm2: float | None
    valid: bool


def _point(point: Point2) -> Point:
    return (float(point.x), float(point.y))


def _distance(a: Point, b: Point) -> float:
    return hypot(b[0] - a[0], b[1] - a[1])


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def _dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    direction = _sub(end, start)
    length_sq = _dot(direction, direction)
    if length_sq == 0.0:
        return _distance(point, start)
    parameter = _dot(_sub(point, start), direction) / length_sq
    parameter = max(0.0, min(1.0, parameter))
    projection = (
        start[0] + parameter * direction[0],
        start[1] + parameter * direction[1],
    )
    return _distance(point, projection)


def _point_line_distance(point: Point, start: Point, end: Point) -> float:
    length = _distance(start, end)
    if length == 0.0:
        return _distance(point, start)
    return abs(_cross(_sub(end, start), _sub(point, start))) / length


def _segment_intersection_kind(
    a: Point,
    b: Point,
    c: Point,
    d: Point,
    tolerance: float,
) -> SegmentKind:
    """Classify two finite segments using a linear-distance tolerance."""

    ab = _sub(b, a)
    cd = _sub(d, c)
    length_ab = _distance(a, b)
    length_cd = _distance(c, d)
    if length_ab <= tolerance or length_cd <= tolerance:
        return "none"

    denominator = _cross(ab, cd)
    parallel_threshold = tolerance * max(length_ab, length_cd, 1.0)
    if abs(denominator) <= parallel_threshold:
        # Parallel is not enough: both segments must lie on the same line.
        if (
            _point_line_distance(c, a, b) > tolerance
            or _point_line_distance(d, a, b) > tolerance
        ):
            return "none"
        if (
            _point_line_distance(a, c, d) > tolerance
            or _point_line_distance(b, c, d) > tolerance
        ):
            return "none"

        unit = (ab[0] / length_ab, ab[1] / length_ab)
        c_parameter = _dot(_sub(c, a), unit)
        d_parameter = _dot(_sub(d, a), unit)
        overlap = min(length_ab, max(c_parameter, d_parameter)) - max(
            0.0, min(c_parameter, d_parameter)
        )
        if overlap > tolerance:
            return "collinear_overlap"
        if overlap >= -tolerance:
            return "touch"
        return "none"

    ac = _sub(c, a)
    t = _cross(ac, cd) / denominator
    u = _cross(ac, ab) / denominator
    t_epsilon = tolerance / max(length_ab, tolerance)
    u_epsilon = tolerance / max(length_cd, tolerance)
    if not (-t_epsilon <= t <= 1.0 + t_epsilon):
        return "none"
    if not (-u_epsilon <= u <= 1.0 + u_epsilon):
        return "none"

    ab_interior = t_epsilon < t < 1.0 - t_epsilon
    cd_interior = u_epsilon < u < 1.0 - u_epsilon
    if ab_interior and cd_interior:
        return "proper"
    return "touch"


def _polygon_area(vertices: list[Point]) -> float:
    return abs(_signed_polygon_area(vertices))


def _signed_polygon_area(vertices: list[Point]) -> float:
    if len(vertices) < 3:
        return 0.0
    # Translation avoids catastrophic cancellation for a small room located at
    # a large project coordinate (for example x=1e9 mm).
    origin = vertices[0]
    translated = [_sub(vertex, origin) for vertex in vertices]
    return 0.5 * sum(
        a[0] * b[1] - b[0] * a[1]
        for a, b in zip(translated, translated[1:] + translated[:1])
    )


def _polygon_edges(vertices: list[Point]) -> list[tuple[Point, Point]]:
    return list(zip(vertices, vertices[1:] + vertices[:1]))


def _point_in_polygon(point: Point, vertices: list[Point], tolerance: float) -> int:
    """Return 1 inside, 0 outside and -1 on the boundary."""

    for start, end in _polygon_edges(vertices):
        if _point_segment_distance(point, start, end) <= tolerance:
            return -1

    inside = False
    x, y = point
    for start, end in _polygon_edges(vertices):
        if (start[1] > y) == (end[1] > y):
            continue
        x_crossing = start[0] + (y - start[1]) * (end[0] - start[0]) / (
            end[1] - start[1]
        )
        if x_crossing > x:
            inside = not inside
    return 1 if inside else 0


def _cyclic_polygons_equal(
    first: list[Point], second: list[Point], tolerance: float
) -> bool:
    if len(first) != len(second):
        return False
    if not first:
        return True
    for start_index, candidate in enumerate(second):
        if _distance(first[0], candidate) > tolerance:
            continue
        forward = all(
            _distance(first[index], second[(start_index + index) % len(second)])
            <= tolerance
            for index in range(len(first))
        )
        reverse = all(
            _distance(first[index], second[(start_index - index) % len(second)])
            <= tolerance
            for index in range(len(first))
        )
        if forward or reverse:
            return True
    return False


def _polygons_overlap_interiors(
    first: list[Point], second: list[Point], tolerance: float
) -> bool:
    if _cyclic_polygons_equal(first, second, tolerance):
        return True

    first_edges = _polygon_edges(first)
    second_edges = _polygon_edges(second)
    for first_edge in first_edges:
        for second_edge in second_edges:
            if (
                _segment_intersection_kind(
                    first_edge[0],
                    first_edge[1],
                    second_edge[0],
                    second_edge[1],
                    tolerance,
                )
                == "proper"
            ):
                return True

    first_samples = list(first)
    first_samples.extend(
        ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        for a, b in first_edges
    )
    second_samples = list(second)
    second_samples.extend(
        ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        for a, b in second_edges
    )
    return any(
        _point_in_polygon(sample, second, tolerance) == 1
        for sample in first_samples
    ) or any(
        _point_in_polygon(sample, first, tolerance) == 1
        for sample in second_samples
    )


class _ProjectValidator:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.geometry_tolerance = float(project.settings.geometry_tolerance_mm)
        self.measurement_tolerance = float(project.settings.measurement_tolerance_mm)
        self.issues: list[Issue] = []
        self.contexts = list(self._iter_entities())
        self.context_by_object = {id(context.entity): context for context in self.contexts}
        self.contexts_by_id: dict[str, list[_EntityContext]] = {}
        for context in self.contexts:
            self.contexts_by_id.setdefault(context.entity.id, []).append(context)
        self.room_geometries: list[_RoomGeometry] = []

    def validate(self) -> ValidationReport:
        self._validate_unique_ids()
        for building_index, building in enumerate(self.project.buildings):
            building_path = f"/buildings/{building_index}"
            for floor_index, floor in enumerate(building.floors):
                floor_path = f"{building_path}/floors/{floor_index}"
                self._validate_floor(floor, floor_path)
        self._validate_measurements()
        self._validate_design_dimensions()

        self.issues.sort(
            key=lambda issue: (issue.path, issue.code, tuple(issue.entity_ids))
        )
        error_count = sum(issue.severity == "error" for issue in self.issues)
        warning_count = sum(issue.severity == "warning" for issue in self.issues)
        statistics = self._statistics()
        statistics["error_count"] = error_count
        statistics["warning_count"] = warning_count
        return ValidationReport(
            valid=error_count == 0,
            issues=self.issues,
            statistics=statistics,
        )

    def _iter_entities(self) -> Iterable[_EntityContext]:
        yield _EntityContext(self.project, "")
        for building_index, building in enumerate(self.project.buildings):
            building_path = f"/buildings/{building_index}"
            yield _EntityContext(building, building_path)
            for floor_index, floor in enumerate(building.floors):
                floor_path = f"{building_path}/floors/{floor_index}"
                yield _EntityContext(
                    floor,
                    floor_path,
                    floor=floor,
                    floor_path=floor_path,
                )
                for level_index, level in enumerate(floor.levels):
                    yield _EntityContext(
                        level,
                        f"{floor_path}/levels/{level_index}",
                        floor=floor,
                        floor_path=floor_path,
                    )
                for wall_index, wall in enumerate(floor.walls):
                    wall_path = f"{floor_path}/walls/{wall_index}"
                    yield _EntityContext(
                        wall,
                        wall_path,
                        floor=floor,
                        floor_path=floor_path,
                        owner_wall=wall,
                    )
                    for face_index, face in enumerate(wall.faces):
                        yield _EntityContext(
                            face,
                            f"{wall_path}/faces/{face_index}",
                            floor=floor,
                            floor_path=floor_path,
                            owner_wall=wall,
                        )
                for space_index, space in enumerate(floor.spaces):
                    yield _EntityContext(
                        space,
                        f"{floor_path}/spaces/{space_index}",
                        floor=floor,
                        floor_path=floor_path,
                    )
                for opening_index, opening in enumerate(floor.openings):
                    yield _EntityContext(
                        opening,
                        f"{floor_path}/openings/{opening_index}",
                        floor=floor,
                        floor_path=floor_path,
                    )
                for obstacle_index, obstacle in enumerate(floor.obstacles):
                    yield _EntityContext(
                        obstacle,
                        f"{floor_path}/obstacles/{obstacle_index}",
                        floor=floor,
                        floor_path=floor_path,
                    )
                for zone_index, zone in enumerate(floor.cladding_zones):
                    yield _EntityContext(
                        zone,
                        f"{floor_path}/cladding_zones/{zone_index}",
                        floor=floor,
                        floor_path=floor_path,
                    )
        for system_index, system in enumerate(self.project.panel_systems):
            yield _EntityContext(system, f"/panel_systems/{system_index}")
        for measurement_index, measurement in enumerate(self.project.measurements):
            yield _EntityContext(measurement, f"/measurements/{measurement_index}")
        for dimension_index, dimension in enumerate(self.project.design_dimensions):
            yield _EntityContext(
                dimension, f"/design_dimensions/{dimension_index}"
            )

    def _add(
        self,
        code: str,
        severity: Literal["error", "warning"],
        message: str,
        path: str,
        entity_ids: Iterable[str] = (),
        details: dict | None = None,
    ) -> None:
        self.issues.append(
            Issue(
                code=code,
                severity=severity,
                message=message,
                path=path or "/",
                entity_ids=list(entity_ids),
                details=details or {},
            )
        )

    def _validate_unique_ids(self) -> None:
        for identifier, contexts in self.contexts_by_id.items():
            if len(contexts) <= 1:
                continue
            first = contexts[0]
            for duplicate in contexts[1:]:
                self._add(
                    "DUPLICATE_ENTITY_ID",
                    "error",
                    f"Идентификатор «{identifier}» уже используется другой сущностью.",
                    f"{duplicate.path}/id",
                    [identifier],
                    {
                        "first_path": first.path or "/",
                        "duplicate_path": duplicate.path or "/",
                    },
                )

    def _validate_floor(self, floor: Floor, floor_path: str) -> None:
        walls = {wall.id: wall for wall in floor.walls}
        spaces = {space.id: space for space in floor.spaces}
        openings = {opening.id: opening for opening in floor.openings}
        faces: dict[str, tuple[WallFace, Wall]] = {
            face.id: (face, wall) for wall in floor.walls for face in wall.faces
        }

        for wall_index, wall in enumerate(floor.walls):
            wall_path = f"{floor_path}/walls/{wall_index}"
            length = _distance(_point(wall.start), _point(wall.end))
            if length <= self.geometry_tolerance:
                self._add(
                    "WALL_TOO_SHORT",
                    "error",
                    "Длина стены не превышает геометрический допуск.",
                    wall_path,
                    [wall.id],
                    {
                        "length_mm": length,
                        "tolerance_mm": self.geometry_tolerance,
                    },
                )
            for face_index, face in enumerate(wall.faces):
                if face.space_id is None:
                    continue
                self._resolve_same_floor_reference(
                    face.space_id,
                    spaces,
                    Space,
                    f"{wall_path}/faces/{face_index}/space_id",
                    "WALL_FACE_SPACE_NOT_FOUND",
                    "Сторона стены ссылается на отсутствующее помещение этажа.",
                    [face.id, face.space_id],
                )

        self._validate_wall_intersections(floor, floor_path)

        floor_rooms: list[_RoomGeometry] = []
        for space_index, space in enumerate(floor.spaces):
            geometry = self._validate_room(
                space,
                f"{floor_path}/spaces/{space_index}",
                floor,
                walls,
            )
            floor_rooms.append(geometry)
            self.room_geometries.append(geometry)
        self._validate_face_boundary_membership(floor, floor_path, spaces)
        self._validate_room_overlaps(floor_rooms)

        self._validate_openings(floor, floor_path, walls)
        self._validate_obstacles(floor, floor_path, spaces)
        self._validate_cladding_zones(
            floor,
            floor_path,
            faces,
            openings,
        )

    def _resolve_same_floor_reference(
        self,
        identifier: str,
        local: dict[str, object],
        expected_type: type,
        path: str,
        missing_code: str,
        missing_message: str,
        entity_ids: list[str],
    ) -> object | None:
        candidate = local.get(identifier)
        if isinstance(candidate, expected_type):
            return candidate
        global_candidates = self.contexts_by_id.get(identifier, [])
        if any(isinstance(context.entity, expected_type) for context in global_candidates):
            self._add(
                "REFERENCE_CROSS_FLOOR",
                "error",
                "Ссылка ведёт на сущность другого этажа.",
                path,
                entity_ids,
                {
                    "target_id": identifier,
                    "expected_type": expected_type.__name__,
                },
            )
        else:
            self._add(
                missing_code,
                "error",
                missing_message,
                path,
                entity_ids,
                {"target_id": identifier},
            )
        return None

    def _validate_wall_intersections(self, floor: Floor, floor_path: str) -> None:
        for first_index, first in enumerate(floor.walls):
            first_start, first_end = _point(first.start), _point(first.end)
            if _distance(first_start, first_end) <= self.geometry_tolerance:
                continue
            for second_index in range(first_index + 1, len(floor.walls)):
                second = floor.walls[second_index]
                second_start, second_end = _point(second.start), _point(second.end)
                if _distance(second_start, second_end) <= self.geometry_tolerance:
                    continue
                kind = _segment_intersection_kind(
                    first_start,
                    first_end,
                    second_start,
                    second_end,
                    self.geometry_tolerance,
                )
                second_path = f"{floor_path}/walls/{second_index}"
                details = {
                    "first_wall_id": first.id,
                    "second_wall_id": second.id,
                    "first_wall_path": f"{floor_path}/walls/{first_index}",
                    "second_wall_path": second_path,
                }
                if kind == "proper":
                    self._add(
                        "WALL_PROPER_INTERSECTION",
                        "error",
                        "Стены пересекаются внутренними участками; T-примыканием это не является.",
                        second_path,
                        [first.id, second.id],
                        details,
                    )
                elif kind == "collinear_overlap":
                    self._add(
                        "WALL_COLLINEAR_OVERLAP",
                        "error",
                        "Коллинеарные стены перекрываются; общую стену нужно переиспользовать по одному ID.",
                        second_path,
                        [first.id, second.id],
                        details,
                    )

    def _validate_room(
        self,
        space: Space,
        space_path: str,
        floor: Floor,
        walls: dict[str, Wall],
    ) -> _RoomGeometry:
        segments: list[tuple[Point, Point]] = []
        resolved_edges: list[tuple[Wall, bool]] = []
        wall_ids_seen: set[str] = set()
        height_checked: set[str] = set()
        valid = True
        for edge_index, edge in enumerate(space.boundary):
            edge_path = f"{space_path}/boundary/{edge_index}"
            if edge.wall_id in wall_ids_seen:
                valid = False
                self._add(
                    "SPACE_DUPLICATE_BOUNDARY_WALL",
                    "error",
                    "Одна стена повторно включена в границу помещения.",
                    f"{edge_path}/wall_id",
                    [space.id, edge.wall_id],
                )
            wall_ids_seen.add(edge.wall_id)
            wall = self._resolve_same_floor_reference(
                edge.wall_id,
                walls,
                Wall,
                f"{edge_path}/wall_id",
                "SPACE_BOUNDARY_WALL_NOT_FOUND",
                "Граница помещения ссылается на отсутствующую стену этажа.",
                [space.id, edge.wall_id],
            )
            if not isinstance(wall, Wall):
                valid = False
                continue
            start, end = _point(wall.start), _point(wall.end)
            if edge.reversed:
                start, end = end, start
            segments.append((start, end))
            resolved_edges.append((wall, edge.reversed))
            if _distance(start, end) <= self.geometry_tolerance:
                valid = False
            if wall.id not in height_checked:
                height_checked.add(wall.id)
                wall_top = float(wall.base_mm + wall.height_mm)
                room_top = float(space.height_mm)
                if abs(wall_top - room_top) > self.geometry_tolerance:
                    self._add(
                        "ROOM_WALL_HEIGHT_MISMATCH",
                        "warning",
                        "Верх граничной стены не совпадает с высотой помещения от уровня этажа.",
                        edge_path,
                        [space.id, wall.id],
                        {
                            "wall_top_mm": wall_top,
                            "room_top_mm": room_top,
                            "difference_mm": abs(wall_top - room_top),
                            "tolerance_mm": self.geometry_tolerance,
                        },
                    )

        perimeter: float | None = None
        area: float | None = None
        vertices: list[Point] = []
        if len(segments) == len(space.boundary):
            perimeter = sum(_distance(start, end) for start, end in segments)
            vertices = [segment[0] for segment in segments]
            connected = True
            for index, segment in enumerate(segments):
                next_segment = segments[(index + 1) % len(segments)]
                gap = _distance(segment[1], next_segment[0])
                if gap > self.geometry_tolerance:
                    valid = False
                    connected = False
                    next_index = (index + 1) % len(segments)
                    self._add(
                        "SPACE_BOUNDARY_DISCONNECTED",
                        "error",
                        "Последовательные стены границы помещения не соединяются.",
                        f"{space_path}/boundary/{next_index}",
                        [space.id],
                        {
                            "after_edge_index": index,
                            "before_edge_index": next_index,
                            "gap_mm": gap,
                            "tolerance_mm": self.geometry_tolerance,
                        },
                    )

            signed_area = _signed_polygon_area(vertices)
            area = abs(signed_area)
            if area <= self.geometry_tolerance * self.geometry_tolerance:
                valid = False
                self._add(
                    "SPACE_ZERO_REFERENCE_AREA",
                    "error",
                    "Площадь помещения по опорным линиям стен равна нулю.",
                    f"{space_path}/boundary",
                    [space.id],
                    {"reference_line_area_mm2": area},
                )

            edge_count = len(segments)
            self_intersects = False
            for first_index in range(edge_count):
                for second_index in range(first_index + 1, edge_count):
                    adjacent = second_index == first_index + 1 or (
                        first_index == 0 and second_index == edge_count - 1
                    )
                    kind = _segment_intersection_kind(
                        segments[first_index][0],
                        segments[first_index][1],
                        segments[second_index][0],
                        segments[second_index][1],
                        self.geometry_tolerance,
                    )
                    invalid_intersection = kind == "collinear_overlap" or (
                        not adjacent and kind in {"touch", "proper"}
                    )
                    if invalid_intersection:
                        valid = False
                        self_intersects = True
                        self._add(
                            "SPACE_BOUNDARY_SELF_INTERSECTION",
                            "error",
                            "Граница помещения самопересекается или самоперекрывается.",
                            f"{space_path}/boundary/{second_index}",
                            [space.id],
                            {
                                "first_edge_index": first_index,
                                "second_edge_index": second_index,
                                "intersection_kind": kind,
                            },
                        )

            if (
                connected
                and not self_intersects
                and area > self.geometry_tolerance * self.geometry_tolerance
                and len(resolved_edges) == len(space.boundary)
            ):
                interior_is_left_of_boundary = signed_area > 0.0
                for edge_index, (wall, reversed_edge) in enumerate(resolved_edges):
                    expected_side = (
                        "left"
                        if interior_is_left_of_boundary != reversed_edge
                        else "right"
                    )
                    expected_index = next(
                        index
                        for index, face in enumerate(wall.faces)
                        if face.side == expected_side
                    )
                    opposite_index = 1 - expected_index
                    expected_face = wall.faces[expected_index]
                    opposite_face = wall.faces[opposite_index]
                    wall_context = self.context_by_object[id(wall)]
                    expected_path = (
                        f"{wall_context.path}/faces/{expected_index}/space_id"
                    )
                    opposite_path = (
                        f"{wall_context.path}/faces/{opposite_index}/space_id"
                    )
                    if expected_face.space_id == space.id:
                        if opposite_face.space_id == space.id:
                            valid = False
                            self._add(
                                "ROOM_BOUNDARY_FACE_WRONG_SIDE",
                                "error",
                                "Обе стороны граничной стены назначены одному помещению.",
                                opposite_path,
                                [space.id, wall.id, opposite_face.id],
                                {"expected_interior_side": expected_side},
                            )
                        continue
                    valid = False
                    if opposite_face.space_id == space.id:
                        self._add(
                            "ROOM_BOUNDARY_FACE_WRONG_SIDE",
                            "error",
                            "Помещение назначено наружной, а не внутренней стороне граничной стены.",
                            opposite_path,
                            [space.id, wall.id, opposite_face.id],
                            {"expected_interior_side": expected_side},
                        )
                    elif expected_face.space_id is None:
                        self._add(
                            "ROOM_BOUNDARY_FACE_MISSING",
                            "error",
                            "Внутренняя сторона граничной стены не назначена помещению.",
                            expected_path,
                            [space.id, wall.id, expected_face.id],
                            {"expected_interior_side": expected_side},
                        )
                    else:
                        self._add(
                            "ROOM_BOUNDARY_FACE_WRONG_SPACE",
                            "error",
                            "Внутренняя сторона граничной стены назначена другому помещению.",
                            expected_path,
                            [
                                space.id,
                                wall.id,
                                expected_face.id,
                                expected_face.space_id,
                            ],
                            {
                                "expected_interior_side": expected_side,
                                "actual_space_id": expected_face.space_id,
                            },
                        )

        return _RoomGeometry(
            space=space,
            path=space_path,
            floor=floor,
            segments=segments,
            vertices=vertices,
            perimeter_mm=perimeter,
            area_mm2=area,
            valid=valid,
        )

    def _validate_face_boundary_membership(
        self,
        floor: Floor,
        floor_path: str,
        spaces: dict[str, Space],
    ) -> None:
        boundary_walls_by_space = {
            space.id: {edge.wall_id for edge in space.boundary}
            for space in floor.spaces
        }
        for wall_index, wall in enumerate(floor.walls):
            for face_index, face in enumerate(wall.faces):
                if face.space_id is None or face.space_id not in spaces:
                    continue
                if wall.id not in boundary_walls_by_space[face.space_id]:
                    self._add(
                        "WALL_FACE_SPACE_BOUNDARY_MISMATCH",
                        "error",
                        "Сторона стены назначена помещению, но стена отсутствует в его границе.",
                        f"{floor_path}/walls/{wall_index}/faces/{face_index}/space_id",
                        [wall.id, face.id, face.space_id],
                    )

    def _validate_room_overlaps(self, rooms: list[_RoomGeometry]) -> None:
        valid_rooms = [room for room in rooms if room.valid and room.vertices]
        for first_index, first in enumerate(valid_rooms):
            for second in valid_rooms[first_index + 1 :]:
                if _polygons_overlap_interiors(
                    first.vertices,
                    second.vertices,
                    self.geometry_tolerance,
                ):
                    self._add(
                        "SPACE_INTERIOR_OVERLAP",
                        "error",
                        "Внутренние области помещений перекрываются.",
                        second.path,
                        [first.space.id, second.space.id],
                        {"first_space_path": first.path, "second_space_path": second.path},
                    )

    def _validate_openings(
        self,
        floor: Floor,
        floor_path: str,
        walls: dict[str, Wall],
    ) -> None:
        resolved: dict[str, list[tuple[Opening, str]]] = {}
        for opening_index, opening in enumerate(floor.openings):
            opening_path = f"{floor_path}/openings/{opening_index}"
            wall = self._resolve_same_floor_reference(
                opening.wall_id,
                walls,
                Wall,
                f"{opening_path}/wall_id",
                "OPENING_WALL_NOT_FOUND",
                "Проём ссылается на отсутствующую стену этажа.",
                [opening.id, opening.wall_id],
            )
            if not isinstance(wall, Wall):
                continue
            resolved.setdefault(wall.id, []).append((opening, opening_path))
            wall_length = _distance(_point(wall.start), _point(wall.end))
            horizontal_end = float(opening.offset_mm + opening.width_mm)
            if horizontal_end > wall_length + self.geometry_tolerance:
                self._add(
                    "OPENING_HORIZONTAL_OUT_OF_BOUNDS",
                    "error",
                    "Проём выходит за длину стены.",
                    opening_path,
                    [opening.id, wall.id],
                    {
                        "opening_end_mm": horizontal_end,
                        "wall_length_mm": wall_length,
                    },
                )
            wall_bottom = float(wall.base_mm)
            wall_top = float(wall.base_mm + wall.height_mm)
            opening_bottom = float(wall.base_mm + opening.sill_mm)
            opening_top = float(wall.base_mm + opening.sill_mm + opening.height_mm)
            if (
                opening_bottom < wall_bottom - self.geometry_tolerance
                or opening_top > wall_top + self.geometry_tolerance
            ):
                self._add(
                    "OPENING_VERTICAL_OUT_OF_BOUNDS",
                    "error",
                    "Проём выходит за вертикальный диапазон стены.",
                    opening_path,
                    [opening.id, wall.id],
                    {
                        "opening_bottom_mm": opening_bottom,
                        "opening_top_mm": opening_top,
                        "wall_bottom_mm": wall_bottom,
                        "wall_top_mm": wall_top,
                    },
                )

        for wall_id, wall_openings in resolved.items():
            for first_index, (first, first_path) in enumerate(wall_openings):
                first_x = (float(first.offset_mm), float(first.offset_mm + first.width_mm))
                first_z = (float(first.sill_mm), float(first.sill_mm + first.height_mm))
                for second, second_path in wall_openings[first_index + 1 :]:
                    second_x = (
                        float(second.offset_mm),
                        float(second.offset_mm + second.width_mm),
                    )
                    second_z = (
                        float(second.sill_mm),
                        float(second.sill_mm + second.height_mm),
                    )
                    x_overlap = min(first_x[1], second_x[1]) - max(
                        first_x[0], second_x[0]
                    )
                    z_overlap = min(first_z[1], second_z[1]) - max(
                        first_z[0], second_z[0]
                    )
                    if (
                        x_overlap > self.geometry_tolerance
                        and z_overlap > self.geometry_tolerance
                    ):
                        self._add(
                            "OPENING_OVERLAP",
                            "error",
                            "Прямоугольники проёмов на одной стене перекрываются.",
                            second_path,
                            [first.id, second.id, wall_id],
                            {
                                "first_opening_path": first_path,
                                "second_opening_path": second_path,
                                "horizontal_overlap_mm": x_overlap,
                                "vertical_overlap_mm": z_overlap,
                            },
                        )

    def _validate_obstacles(
        self,
        floor: Floor,
        floor_path: str,
        spaces: dict[str, Space],
    ) -> None:
        for obstacle_index, obstacle in enumerate(floor.obstacles):
            obstacle_path = f"{floor_path}/obstacles/{obstacle_index}"
            if obstacle.space_id is not None:
                self._resolve_same_floor_reference(
                    obstacle.space_id,
                    spaces,
                    Space,
                    f"{obstacle_path}/space_id",
                    "OBSTACLE_SPACE_NOT_FOUND",
                    "Препятствие ссылается на отсутствующее помещение этажа.",
                    [obstacle.id, obstacle.space_id],
                )
            vertices = [_point(point) for point in obstacle.footprint]
            edges = _polygon_edges(vertices)
            for edge_index, (start, end) in enumerate(edges):
                if _distance(start, end) <= self.geometry_tolerance:
                    self._add(
                        "OBSTACLE_DEGENERATE_EDGE",
                        "error",
                        "Контур препятствия содержит ребро нулевой длины.",
                        f"{obstacle_path}/footprint/{edge_index}",
                        [obstacle.id],
                    )
            area = _polygon_area(vertices)
            if area <= self.geometry_tolerance * self.geometry_tolerance:
                self._add(
                    "OBSTACLE_ZERO_AREA",
                    "error",
                    "Площадь контура препятствия равна нулю.",
                    f"{obstacle_path}/footprint",
                    [obstacle.id],
                    {"area_mm2": area},
                )
            edge_count = len(edges)
            for first_index in range(edge_count):
                for second_index in range(first_index + 1, edge_count):
                    adjacent = second_index == first_index + 1 or (
                        first_index == 0 and second_index == edge_count - 1
                    )
                    kind = _segment_intersection_kind(
                        edges[first_index][0],
                        edges[first_index][1],
                        edges[second_index][0],
                        edges[second_index][1],
                        self.geometry_tolerance,
                    )
                    if kind == "collinear_overlap" or (
                        not adjacent and kind in {"touch", "proper"}
                    ):
                        self._add(
                            "OBSTACLE_SELF_INTERSECTION",
                            "error",
                            "Контур препятствия самопересекается или самоперекрывается.",
                            f"{obstacle_path}/footprint/{second_index}",
                            [obstacle.id],
                            {
                                "first_edge_index": first_index,
                                "second_edge_index": second_index,
                                "intersection_kind": kind,
                            },
                        )

    def _validate_cladding_zones(
        self,
        floor: Floor,
        floor_path: str,
        faces: dict[str, tuple[WallFace, Wall]],
        openings: dict[str, Opening],
    ) -> None:
        panel_systems = {system.id: system for system in self.project.panel_systems}
        for zone_index, zone in enumerate(floor.cladding_zones):
            zone_path = f"{floor_path}/cladding_zones/{zone_index}"
            face_entry = faces.get(zone.wall_face_id)
            if face_entry is None:
                global_faces = self.contexts_by_id.get(zone.wall_face_id, [])
                if any(isinstance(context.entity, WallFace) for context in global_faces):
                    self._add(
                        "REFERENCE_CROSS_FLOOR",
                        "error",
                        "Зона облицовки ссылается на сторону стены другого этажа.",
                        f"{zone_path}/wall_face_id",
                        [zone.id, zone.wall_face_id],
                    )
                else:
                    self._add(
                        "CLADDING_FACE_NOT_FOUND",
                        "error",
                        "Зона облицовки ссылается на отсутствующую сторону стены.",
                        f"{zone_path}/wall_face_id",
                        [zone.id, zone.wall_face_id],
                    )
            wall = face_entry[1] if face_entry is not None else None

            system = panel_systems.get(zone.panel_system_id)
            if system is None:
                self._add(
                    "CLADDING_SYSTEM_NOT_FOUND",
                    "error",
                    "Зона облицовки ссылается на отсутствующую систему панелей.",
                    f"{zone_path}/panel_system_id",
                    [zone.id, zone.panel_system_id],
                )

            if wall is not None:
                wall_length = _distance(_point(wall.start), _point(wall.end))
                zone_end = float(zone.offset_mm + zone.width_mm)
                if zone_end > wall_length + self.geometry_tolerance:
                    self._add(
                        "CLADDING_HORIZONTAL_OUT_OF_BOUNDS",
                        "error",
                        "Зона облицовки выходит за длину стороны стены.",
                        zone_path,
                        [zone.id, wall.id],
                        {"zone_end_mm": zone_end, "wall_length_mm": wall_length},
                    )
                zone_bottom = float(wall.base_mm + zone.base_mm)
                zone_top = float(wall.base_mm + zone.base_mm + zone.height_mm)
                wall_bottom = float(wall.base_mm)
                wall_top = float(wall.base_mm + wall.height_mm)
                if (
                    zone_bottom < wall_bottom - self.geometry_tolerance
                    or zone_top > wall_top + self.geometry_tolerance
                ):
                    self._add(
                        "CLADDING_VERTICAL_OUT_OF_BOUNDS",
                        "error",
                        "Зона облицовки выходит за вертикальный диапазон стены.",
                        zone_path,
                        [zone.id, wall.id],
                        {
                            "zone_bottom_mm": zone_bottom,
                            "zone_top_mm": zone_top,
                            "wall_bottom_mm": wall_bottom,
                            "wall_top_mm": wall_top,
                        },
                    )

            excluded_seen: set[str] = set()
            for excluded_index, opening_id in enumerate(zone.excluded_opening_ids):
                excluded_path = (
                    f"{zone_path}/excluded_opening_ids/{excluded_index}"
                )
                if opening_id in excluded_seen:
                    self._add(
                        "CLADDING_DUPLICATE_EXCLUDED_OPENING",
                        "error",
                        "Проём повторно указан в исключениях зоны облицовки.",
                        excluded_path,
                        [zone.id, opening_id],
                    )
                excluded_seen.add(opening_id)
                opening = openings.get(opening_id)
                if opening is None:
                    global_openings = self.contexts_by_id.get(opening_id, [])
                    if any(
                        isinstance(context.entity, Opening)
                        for context in global_openings
                    ):
                        self._add(
                            "REFERENCE_CROSS_FLOOR",
                            "error",
                            "Исключённый проём находится на другом этаже.",
                            excluded_path,
                            [zone.id, opening_id],
                        )
                    else:
                        self._add(
                            "CLADDING_EXCLUDED_OPENING_NOT_FOUND",
                            "error",
                            "Исключённый проём не найден на этаже.",
                            excluded_path,
                            [zone.id, opening_id],
                        )
                elif wall is not None and opening.wall_id != wall.id:
                    self._add(
                        "CLADDING_EXCLUDED_OPENING_WRONG_WALL",
                        "error",
                        "Исключённый проём относится к другой стене.",
                        excluded_path,
                        [zone.id, opening.id, wall.id],
                        {
                            "zone_wall_id": wall.id,
                            "opening_wall_id": opening.wall_id,
                        },
                    )

    def _validate_measurements(self) -> None:
        active_statuses = {"raw", "verified", "accepted"}
        groups: dict[tuple[str, str], list[tuple[Measurement, str]]] = {}
        for measurement_index, measurement in enumerate(self.project.measurements):
            path = f"/measurements/{measurement_index}"
            target = self._unique_context(measurement.target_id)
            if target is None:
                code = (
                    "MEASUREMENT_TARGET_AMBIGUOUS"
                    if measurement.target_id in self.contexts_by_id
                    else "MEASUREMENT_TARGET_NOT_FOUND"
                )
                self._add(
                    code,
                    "error",
                    "Цель измерения не найдена или имеет неоднозначный ID.",
                    f"{path}/target_id",
                    [measurement.id, measurement.target_id],
                )
                continue
            supported, _ = self._property_value(
                target, measurement.property_name
            )
            if not supported:
                self._add(
                    "MEASUREMENT_PROPERTY_UNSUPPORTED",
                    "error",
                    "Выбранное свойство неприменимо к цели измерения.",
                    f"{path}/property_name",
                    [measurement.id, measurement.target_id],
                    {
                        "property_name": measurement.property_name,
                        "target_type": type(target.entity).__name__,
                    },
                )
                continue
            if measurement.status in active_statuses:
                groups.setdefault(
                    (measurement.target_id, measurement.property_name), []
                ).append((measurement, path))

        for (target_id, property_name), measurements in groups.items():
            if len(measurements) < 2:
                continue
            minimum = min(measurements, key=lambda item: item[0].value_mm)
            maximum = max(measurements, key=lambda item: item[0].value_mm)
            delta = float(maximum[0].value_mm - minimum[0].value_mm)
            allowed = max(
                self.measurement_tolerance,
                float(minimum[0].tolerance_mm),
                float(maximum[0].tolerance_mm),
            )
            if delta > allowed:
                self._add(
                    "MEASUREMENT_CONFLICT",
                    "warning",
                    "Активные измерения одного свойства расходятся сверх допуска.",
                    maximum[1],
                    [minimum[0].id, maximum[0].id, target_id],
                    {
                        "target_id": target_id,
                        "property_name": property_name,
                        "minimum_value_mm": float(minimum[0].value_mm),
                        "maximum_value_mm": float(maximum[0].value_mm),
                        "difference_mm": delta,
                        "allowed_difference_mm": allowed,
                    },
                )

    def _validate_design_dimensions(self) -> None:
        measurement_by_id = {
            measurement.id: measurement for measurement in self.project.measurements
        }
        for dimension_index, dimension in enumerate(
            self.project.design_dimensions
        ):
            path = f"/design_dimensions/{dimension_index}"
            target = self._unique_context(dimension.target_id)
            if target is None:
                code = (
                    "DESIGN_DIMENSION_TARGET_AMBIGUOUS"
                    if dimension.target_id in self.contexts_by_id
                    else "DESIGN_DIMENSION_TARGET_NOT_FOUND"
                )
                self._add(
                    code,
                    "error",
                    "Цель проектного размера не найдена или имеет неоднозначный ID.",
                    f"{path}/target_id",
                    [dimension.id, dimension.target_id],
                )
                continue

            supported, geometry_value = self._property_value(
                target, dimension.property_name
            )
            if not supported:
                self._add(
                    "DESIGN_DIMENSION_PROPERTY_UNSUPPORTED",
                    "error",
                    "Выбранное свойство неприменимо к цели проектного размера.",
                    f"{path}/property_name",
                    [dimension.id, dimension.target_id],
                    {
                        "property_name": dimension.property_name,
                        "target_type": type(target.entity).__name__,
                    },
                )
                continue

            referenced: list[Measurement] = []
            seen: set[str] = set()
            for reference_index, measurement_id in enumerate(
                dimension.measurement_ids
            ):
                reference_path = f"{path}/measurement_ids/{reference_index}"
                if measurement_id in seen:
                    self._add(
                        "DESIGN_DIMENSION_DUPLICATE_MEASUREMENT_REFERENCE",
                        "error",
                        "Измерение повторно указано в проектном размере.",
                        reference_path,
                        [dimension.id, measurement_id],
                    )
                seen.add(measurement_id)
                measurement = measurement_by_id.get(measurement_id)
                if measurement is None:
                    self._add(
                        "DESIGN_DIMENSION_MEASUREMENT_NOT_FOUND",
                        "error",
                        "Связанное измерение не найдено.",
                        reference_path,
                        [dimension.id, measurement_id],
                    )
                    continue
                referenced.append(measurement)
                if (
                    measurement.target_id != dimension.target_id
                    or measurement.property_name != dimension.property_name
                ):
                    self._add(
                        "DESIGN_DIMENSION_MEASUREMENT_MISMATCH",
                        "error",
                        "Измерение относится к другой цели или свойству.",
                        reference_path,
                        [dimension.id, measurement.id],
                        {
                            "dimension_target_id": dimension.target_id,
                            "dimension_property_name": dimension.property_name,
                            "measurement_target_id": measurement.target_id,
                            "measurement_property_name": measurement.property_name,
                        },
                    )
                if measurement.status in {"rejected", "superseded"}:
                    self._add(
                        "DESIGN_DIMENSION_INACTIVE_MEASUREMENT",
                        "warning",
                        "Проектный размер ссылается на отклонённое или устаревшее измерение.",
                        reference_path,
                        [dimension.id, measurement.id],
                        {"measurement_status": measurement.status},
                    )

            if geometry_value is not None:
                tolerance = max(
                    self.geometry_tolerance,
                    self.measurement_tolerance,
                    *(float(item.tolerance_mm) for item in referenced),
                )
                difference = abs(float(dimension.value_mm) - geometry_value)
                if difference > tolerance:
                    self._add(
                        "DESIGN_DIMENSION_GEOMETRY_MISMATCH",
                        "error",
                        "Проектный размер не совпадает с канонической геометрией.",
                        f"{path}/value_mm",
                        [dimension.id, dimension.target_id],
                        {
                            "design_value_mm": float(dimension.value_mm),
                            "geometry_value_mm": geometry_value,
                            "difference_mm": difference,
                            "tolerance_mm": tolerance,
                        },
                    )

    def _unique_context(self, identifier: str) -> _EntityContext | None:
        contexts = self.contexts_by_id.get(identifier, [])
        return contexts[0] if len(contexts) == 1 else None

    def _property_value(
        self, context: _EntityContext, property_name: str
    ) -> tuple[bool, float | None]:
        entity = context.entity
        if isinstance(entity, Wall):
            values = {
                "length_mm": _distance(_point(entity.start), _point(entity.end)),
                "height_mm": float(entity.height_mm),
                "thickness_mm": float(entity.thickness_mm),
                "elevation_mm": float(entity.base_mm),
            }
        elif isinstance(entity, WallFace) and context.owner_wall is not None:
            values = {
                "length_mm": _distance(
                    _point(context.owner_wall.start), _point(context.owner_wall.end)
                ),
                "height_mm": float(context.owner_wall.height_mm),
            }
        elif isinstance(entity, Space):
            values = {"height_mm": float(entity.height_mm)}
        elif isinstance(entity, Opening):
            values = {
                "width_mm": float(entity.width_mm),
                "height_mm": float(entity.height_mm),
                "offset_mm": float(entity.offset_mm),
                "sill_mm": float(entity.sill_mm),
            }
        elif isinstance(entity, Obstacle):
            values = {
                "height_mm": float(entity.height_mm),
                "elevation_mm": float(entity.base_mm),
            }
        elif isinstance(entity, CladdingZone):
            values = {
                "width_mm": float(entity.width_mm),
                "height_mm": float(entity.height_mm),
                "offset_mm": float(entity.offset_mm),
                "elevation_mm": float(entity.base_mm),
            }
        elif isinstance(entity, Floor):
            values = {
                "height_mm": float(entity.default_height_mm),
                "elevation_mm": float(entity.elevation_mm),
            }
        elif isinstance(entity, LevelDatum):
            values = {"elevation_mm": float(entity.elevation_mm)}
        elif isinstance(entity, PanelSystem):
            values = {"thickness_mm": float(entity.panel_thickness_mm)}
        else:
            values = {}
        if property_name not in values:
            return False, None
        return True, values[property_name]

    def _statistics(self) -> dict:
        counts = {
            "buildings": len(self.project.buildings),
            "floors": 0,
            "levels": 0,
            "walls": 0,
            "wall_faces": 0,
            "spaces": 0,
            "openings": 0,
            "obstacles": 0,
            "cladding_zones": 0,
            "panel_systems": len(self.project.panel_systems),
            "measurements": len(self.project.measurements),
            "design_dimensions": len(self.project.design_dimensions),
            "entities": len(self.contexts),
        }
        for building in self.project.buildings:
            counts["floors"] += len(building.floors)
            for floor in building.floors:
                counts["levels"] += len(floor.levels)
                counts["walls"] += len(floor.walls)
                counts["wall_faces"] += sum(len(wall.faces) for wall in floor.walls)
                counts["spaces"] += len(floor.spaces)
                counts["openings"] += len(floor.openings)
                counts["obstacles"] += len(floor.obstacles)
                counts["cladding_zones"] += len(floor.cladding_zones)

        room_metrics = []
        for geometry in self.room_geometries:
            floor_context = self.context_by_object[id(geometry.floor)]
            room_metrics.append(
                {
                    "space_id": geometry.space.id,
                    "space_code": geometry.space.code,
                    "floor_id": geometry.floor.id,
                    "floor_path": floor_context.path,
                    "boundary_basis": "wall_reference_line",
                    "reference_line_perimeter_mm": geometry.perimeter_mm,
                    "reference_line_area_mm2": geometry.area_mm2,
                    "geometry_valid": geometry.valid,
                }
            )
        return {
            "counts": counts,
            "room_reference_line_metrics": room_metrics,
            "reference_line_metrics_note": (
                "Площадь и периметр рассчитаны по опорным линиям стен; "
                "это не чистая площадь и не чистый периметр помещения."
            ),
        }


def validate_project(project: Project) -> ValidationReport:
    """Validate references, planar geometry and measurement coherence.

    The function has no side effects and does not mutate ``project``.
    Warnings do not make the report invalid; any error does.
    """

    return _ProjectValidator(project).validate()
