"""Model builders shared by CLI, API and the future visual editor."""
from __future__ import annotations

from .models import (BoundaryEdge, Building, CladdingZone, Floor, LevelDatum,
                     Measurement, Obstacle, Opening, PanelSystem, Point2,
                     Project, ProjectMetadata, Space, Wall, WallFace)


def add_polygon_space(floor: Floor, *, code: str, name: str,
                      points: list[Point2], height_mm: float | None = None,
                      thickness_mm: float = 150) -> Space:
    """Build a CCW room; input points are wall reference-line intersections.

    Shared walls must be represented once and referenced in reverse by the
    adjacent room. This convenience builder creates independent walls.
    """
    if len(points) < 3:
        raise ValueError("Контур помещения должен содержать минимум три точки")
    origin = points[0]
    area2 = sum((a.x-origin.x) * (b.y-origin.y) - (b.x-origin.x) * (a.y-origin.y)
                for a, b in zip(points, points[1:] + points[:1]))
    if area2 < 0:
        points = list(reversed(points))
    height = floor.default_height_mm if height_mm is None else height_mm
    walls = [Wall(name=f"{code} / {i+1}", start=a, end=b,
                  height_mm=height, thickness_mm=thickness_mm)
             for i, (a, b) in enumerate(zip(points, points[1:] + points[:1]))]
    space = Space(code=code, name=name, height_mm=height,
                  boundary=[BoundaryEdge(wall_id=wall.id) for wall in walls])
    for wall in walls:
        wall.faces = [WallFace(side="left", space_id=space.id), WallFace(side="right")]
    floor.walls.extend(walls)
    floor.spaces.append(space)
    return space


def add_rectangle(floor: Floor, *, code: str, name: str, x: float, y: float,
                  width_mm: float, depth_mm: float, height_mm: float | None = None,
                  thickness_mm: float = 150) -> Space:
    if width_mm <= 0 or depth_mm <= 0:
        raise ValueError("Длина и ширина помещения должны быть больше нуля")
    return add_polygon_space(floor, code=code, name=name, height_mm=height_mm,
                             thickness_mm=thickness_mm, points=[
                                 Point2(x=x, y=y), Point2(x=x+width_mm, y=y),
                                 Point2(x=x+width_mm, y=y+depth_mm), Point2(x=x, y=y+depth_mm)])


def make_demo_project() -> Project:
    """Synthetic fixture, not a reconstruction or engineering issue of the PDF."""
    project = Project(metadata=ProjectMetadata(name="Пример: два этажа",
        description="Учебные размеры. Не является рабочей документацией объекта.", author="Демонстрация"))
    building = Building(name="Демонстрационное здание")
    first = Floor(name="Первый этаж", elevation_mm=0, default_height_mm=3600,
                  levels=[LevelDatum(name="Чистый пол", kind="finished_floor", elevation_mm=0),
                          LevelDatum(name="Чистый потолок", kind="finished_ceiling", elevation_mm=3600)])
    second = Floor(name="Второй этаж", elevation_mm=4200, default_height_mm=3600)
    add_rectangle(first, code="1.01", name="Лекторий", x=0, y=0, width_mm=6000, depth_mm=4500)
    add_polygon_space(first, code="1.02", name="Г-образная комната", points=[
        Point2(x=7500,y=0), Point2(x=12000,y=0), Point2(x=12000,y=4000),
        Point2(x=9500,y=4000), Point2(x=9500,y=2500), Point2(x=7500,y=2500)])
    add_rectangle(second, code="2.01", name="Длинная стена", x=0, y=0, width_mm=20350, depth_mm=3000)
    first.openings.extend([
        Opening(name="Дверь лектория", kind="door", wall_id=first.walls[0].id,
                offset_mm=1200, width_mm=900, height_mm=2100),
        Opening(name="Окно лектория", kind="window", wall_id=first.walls[2].id,
                offset_mm=1500, width_mm=1400, sill_mm=900, height_mm=1400)])
    first.obstacles.append(Obstacle(name="Колонна", kind="column", space_id=first.spaces[0].id,
        height_mm=3600, footprint=[Point2(x=4900,y=3300), Point2(x=5300,y=3300),
                                  Point2(x=5300,y=3700), Point2(x=4900,y=3700)]))
    panel = PanelSystem(name="Учебная система (не для производства)", panel_thickness_mm=20,
                        backing_thickness_mm=4, standard_widths_mm=[600,900,1200])
    first.cladding_zones.append(CladdingZone(name="Учебная зона отделки",
        wall_face_id=first.walls[0].faces[0].id, panel_system_id=panel.id,
        width_mm=6000, base_mm=20, height_mm=3560, excluded_opening_ids=[first.openings[0].id]))
    project.panel_systems.append(panel)
    project.measurements.extend([
        Measurement(target_id=second.walls[0].id, property_name="length_mm", value_mm=20320.01,
                    source="Учебный обмер", tolerance_mm=1),
        Measurement(target_id=second.walls[0].id, property_name="length_mm", value_mm=20350,
                    source="Учебный проект", tolerance_mm=1)])
    building.floors = [first, second]
    project.buildings.append(building)
    return project
