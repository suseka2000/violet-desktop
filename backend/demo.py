"""Synthetic engineering fixture. No dimension is inferred from the user's PDF."""
from .domain.builders import make_demo_project
from .domain.models import CladdingZone


def make_documentation_demo():
    project = make_demo_project()
    project.metadata.name = "Учебный комплект / проверка генератора РД"
    project.metadata.description = (
        "Синтетическая геометрия для проверки программы. Не является проектом Оружейной палаты. "
        "Размеры, система панелей и зоны условные; производственные узлы не приложены."
    )
    system = project.panel_systems[0]
    system.name = "Учебная система / параметры требуют подтверждения"
    system.maximum_height_mm = 4000
    for floor in project.buildings[0].floors:
        selected = [floor.walls[2], floor.walls[4], floor.walls[5]] if len(floor.walls) > 6 else [floor.walls[0]]
        for wall in selected:
            import math
            face = next((f for f in wall.faces if f.space_id), None)
            if face is None:
                continue
            floor.cladding_zones.append(CladdingZone(
                name=f"Учебная зона / {wall.name}", wall_face_id=face.id, panel_system_id=system.id,
                width_mm=math.hypot(wall.end.x - wall.start.x, wall.end.y - wall.start.y),
                base_mm=20, height_mm=wall.height_mm - 40,
                excluded_opening_ids=[o.id for o in floor.openings if o.wall_id == wall.id]))
    return project
