"""Opening marks and drawing symbols driven by explicit model parameters."""
from __future__ import annotations

import math

from .scene import BLUE, GRAY, INK

KINDS = {"door": "Дверь", "window": "Окно", "passage": "Проход", "hatch": "Люк", "custom": "Проём"}
PREFIXES = {"door": "ДВ", "window": "ОК", "passage": "ПР", "hatch": "ЛК", "custom": "ПО"}


def opening_marks(project):
    counters, result = {}, {}
    for building in project.buildings:
        for floor in building.floors:
            wall_order = {wall.id: index for index, wall in enumerate(floor.walls)}
            for opening in sorted(floor.openings, key=lambda o: (wall_order.get(o.wall_id, -1), o.offset_mm, o.sill_mm, o.id)):
                prefix = PREFIXES[opening.kind]
                counters[prefix] = counters.get(prefix, 0) + 1
                result[opening.id] = f"{prefix}-{counters[prefix]:02d}"
    return result


def opening_schedule(project, num):
    marks, rows = opening_marks(project), []
    for building in project.buildings:
        for floor in building.floors:
            walls = {w.id: w for w in floor.walls}
            for opening in floor.openings:
                wall = walls[opening.wall_id]
                rooms = [space.code for space in floor.spaces if any(e.wall_id == wall.id for e in space.boundary)]
                bottom = float(floor.elevation_mm + wall.base_mm + opening.sill_mm)
                notes = []
                if opening.reveal_finish:
                    notes.append("Откосы: " + opening.reveal_finish)
                door = opening.door
                if opening.kind == "door":
                    if door:
                        notes.append("Скрытая коробка" if door.concealed_frame else "Обычная коробка")
                        if door.leaf_width_mm is not None:
                            notes.append("Полотно " + num(door.leaf_width_mm) + " мм")
                        if door.frame_width_mm is not None:
                            notes.append("Коробка " + num(door.frame_width_mm) + " мм")
                        if door.handing != "unspecified" and door.opening_side != "unspecified":
                            notes.append({"left": "Петли у начала проёма", "right": "Петли у конца проёма", "double": "Две створки"}[door.handing])
                            notes.append("Открывание " + ("слева" if door.opening_side == "left" else "справа") + " от направления стены")
                        else:
                            notes.append("Направление открывания не задано")
                    else:
                        notes.append("Параметры двери не заданы")
                rows.append([marks[opening.id], floor.name + " / " + (", ".join(rooms) or wall.name),
                             KINDS[opening.kind] + " / " + opening.name,
                             num(opening.width_mm) + " x " + num(opening.height_mm),
                             num(bottom), num(bottom + float(opening.height_mm)), "; ".join(notes) or "-"])
    return rows


def vertical_dimension(sheet, x, y1, y2, object_x, label, color=BLUE, size=2.5):
    """Dimension in paper mm, with an unrotated readable value."""
    if y2 - y1 < .01:
        return
    first = len(sheet.primitives)
    for y in (y1, y2):
        sheet.line(object_x, y, x - 1.2, y, color, .12)
        sheet.line(x - .7, y + .7, x + .7, y - .7, color, .2)
    sheet.line(x, y1, x, y2, color, .15)
    sheet.text(x - 1.5, (y1 + y2) / 2, label, size, color, anchor="end")
    sheet.register_dimension(range(first, len(sheet.primitives)), (object_x, y1),
                             (object_x, y2), (x, y1), label, direction=(0., 1.))


def draw_opening_height(sheet, opening, transform, scale, num):
    """Only place a jamb dimension when it clears the actual opening labels.

    Small openings retain both heights/ordinates in the setting-out schedule.
    Trying on a scratch sheet keeps a rejected dimension out of PDF and DXF.
    """
    from .scene import Sheet
    from .label_layout import obstacles, overlap, segment_hits
    if opening["height_mm"]/scale < 12 or opening["width_mm"]/scale < 16:
        return False
    a = transform(opening["x_mm"], opening["y_mm"]+opening["height_mm"])
    b = transform(opening["x_mm"], opening["y_mm"])
    existing, _ = obstacles([p for p in sheet.primitives if p["type"] == "text"])
    trial = Sheet("Размер проёма", "trial")
    vertical_dimension(trial, a[0]+6, a[1], b[1], a[0], num(opening["height_mm"]), INK, 2.5)
    boxes, segments = obstacles(trial.primitives)
    if any(overlap(box, other) for box in boxes for other in existing):
        return False
    if any(segment_hits(box, start, end) for box in existing for start, end in segments):
        return False
    sheet.primitives.extend(trial.primitives)
    return True


def draw_plan_opening(sheet, opening, wall, transform, scale, mark, *, void_already_cut=False, draw_label=True):
    """Draw the wall void and a conventional symbol; never invent handing."""
    length = math.hypot(wall.end.x - wall.start.x, wall.end.y - wall.start.y)
    ux, uy = (wall.end.x-wall.start.x)/length, (wall.end.y-wall.start.y)/length
    nx, ny = -uy, ux
    ax, ay = wall.start.x + ux*opening.offset_mm, wall.start.y + uy*opening.offset_mm
    half = float(wall.thickness_mm)/2
    def point(along, normal=0):
        return transform(ax + ux*along + nx*normal, ay + uy*along + ny*normal)
    span = float(opening.width_mm)
    if void_already_cut:
        # The unified footprint has already subtracted only this host wall.
        # A white rectangle here would incorrectly erase a crossing wall.
        for edge in (0,span):sheet.line(*point(edge,half),*point(edge,-half),GRAY,.15)
    else:
        sheet.polygon([point(0, half), point(span, half), point(span, -half), point(0, -half)], GRAY, "#ffffff", .15)
    if opening.kind == "window":
        for offset in (-half*.3, half*.3):
            sheet.line(*point(0, offset), *point(span, offset), INK, .2)
    elif (opening.kind == "door" and opening.door and opening.door.handing != "unspecified"
          and opening.door.opening_side != "unspecified"
          and (opening.door.leaf_width_mm is None or opening.door.leaf_width_mm <= span)):
        door = opening.door
        direction = 1 if door.opening_side == "left" else -1
        leaves = [0, span] if door.handing == "double" else [0 if door.handing == "left" else span]
        # With no leaf measurement this is the conventional opening symbol,
        # not a dimensioned door-leaf fabrication model.
        radius = float(door.leaf_width_mm or span) / len(leaves)
        for hinge in leaves:
            along_sign = 1 if hinge == 0 else -1
            sheet.line(*point(hinge), *point(hinge, direction*radius), INK, .22)
            arc = [point(hinge + along_sign*radius*math.cos(i*math.pi/40), direction*radius*math.sin(i*math.pi/40)) for i in range(21)]
            sheet.polygon(arc, GRAY, weight=.12, closed=False)
    else:
        sheet.line(*point(0), *point(span), GRAY, .15, True)
    if draw_label:
        mx, my = point(span/2, -(half+160*min(scale/50, 2)))
        sheet.text(mx, my, mark, 2.7, BLUE, True, "middle")


def draw_elevation_opening(sheet, opening, transform, scale, mark):
    """A door/window outline, identified independently of the wall face."""
    x, y = float(opening["x_mm"]), float(opening["y_mm"])
    width, height = float(opening["width_mm"]), float(opening["height_mm"])
    px, py = transform(x, y + height)
    pw, ph = width/scale, height/scale
    sheet.rect(px, py, pw, ph, INK, "#ffffff", .25)
    if opening.get("kind") == "window" and min(pw, ph) >= 5:
        sheet.rect(px+1, py+1, pw-2, ph-2, GRAY, weight=.12)
    if opening.get("kind") == "hatch":
        sheet.line(px, py, px+pw, py+ph, GRAY, .1)
        sheet.line(px, py+ph, px+pw, py, GRAY, .1)
    if pw >= 10 and ph >= 9:
        sheet.text(px+pw/2, py+ph/2, mark, 2.6, BLUE, True, "middle")
        if pw >= 21 and ph >= 17:
            sheet.text(px+pw/2, py+ph/2+4, f"{width:g} x {height:g}", 2.5, INK, anchor="middle")

