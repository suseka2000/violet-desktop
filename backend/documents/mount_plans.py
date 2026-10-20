"""Local mounting plans placed in spare space beside the related elevations."""
from __future__ import annotations

import math

from .scene import BLUE, GRAY, INK, LIGHT, Sheet, width as text_width
from .label_layout import collision, contains, obstacles, text_box
from .plan_walls import draw_wall_footprint, wall_footprint


def _context(project, views):
    if project is None or not views:
        return None
    identifier = views[0][1]["wall"]["floor_id"]
    floor = next((f for b in project.buildings for f in b.floors if f.id == identifier), None)
    if floor is None:
        return None
    selected = {v["wall"]["id"] for _, v in views}
    room_ids = {v["wall"].get("space_id") for _, v in views}
    for space in floor.spaces:
        if space.id in room_ids:
            selected.update(edge.wall_id for edge in space.boundary)
    local = floor.model_copy(update={"walls": [w for w in floor.walls if w.id in selected],
        "openings": [o for o in floor.openings if o.wall_id in selected],
        "spaces": [s for s in floor.spaces if s.id in room_ids]})
    return local


def draw_mount_plan(sheet, project, views, opening_marks, box):
    """Show real wall faces, opening voids and the X=0 side of every view.

    This is a plan/setting-out locator, not an invented horizontal section of
    a finish at an unspecified height. No guessed coating depth is drawn.
    """
    from . import scale_for
    from .openings import draw_plan_opening
    local = _context(project, views)
    if local is None or not local.walls:
        return False
    x, y, w, h = box
    outer = wall_footprint(local, openings=False)
    minx, miny, maxx, maxy = outer.bounds
    scale = scale_for(maxx-minx, maxy-miny, w-48, h-48)
    ox = x+24+(w-48-(maxx-minx)/scale)/2
    oy = y+20+(h-48-(maxy-miny)/scale)/2
    transform = lambda a, b: (ox+(a-minx)/scale, oy+(maxy-b)/scale)
    exterior = any(v["wall"].get("installation_zone") == "exterior" for _, v in views)
    sheet.text(x+3, y+6, "Монтажный план / снаружи" if exterior else "Монтажный план / привязки развёрток", 3, INK, True)
    sheet.text(x+w-3, y+12, f"М 1:{scale}", 2.5, BLUE, anchor="end")
    draw_wall_footprint(sheet, wall_footprint(local), transform, INK, LIGHT)
    by_id = {wall.id: wall for wall in local.walls}
    for opening in local.openings:
        draw_plan_opening(sheet, opening, by_id[opening.wall_id], transform, scale,
                          opening_marks.get(opening.id, ""), void_already_cut=True, draw_label=False)
    labels = []
    for mark, view in views:
        wall = by_id[view["wall"]["id"]]
        dx, dy = wall.end.x-wall.start.x, wall.end.y-wall.start.y
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        direction = (dx/length, dy/length)
        side = view["wall"]["side"]
        sign = 1 if side == "left" else -1
        normal = (-direction[1]*sign, direction[0]*sign)
        half = float(wall.thickness_mm)/2
        base = wall.start if side == "left" else wall.end
        travel = (direction[0]*sign, direction[1]*sign)
        point = lambda along: transform(base.x+travel[0]*along+normal[0]*half,
                                        base.y+travel[1]*along+normal[1]*half)
        # Dashed location of the selected mounting face; not a finish outline.
        cuts = sorted((o["x_mm"], o["x_mm"]+o["width_mm"]) for o in view.get("openings", []))
        cursor, spans = 0., []
        for low, high in cuts:
            if low > cursor: spans.append((cursor, low))
            cursor = max(cursor, high)
        if cursor < length: spans.append((cursor, length))
        for a, b in spans:
            sheet.line(*point(a), *point(b), BLUE, .45)
        p0 = point(0)
        arrow_at = min(length*.2, scale*8)
        p1 = point(arrow_at)
        sheet.circle(*p0, .65, BLUE, "#ffffff", .18)
        sheet.line(*p0, *p1, BLUE, .25)
        tx, ty = travel[0], -travel[1]
        sheet.line(*p1, p1[0]-tx*1.7-ty*.8, p1[1]-ty*1.7+tx*.8, BLUE, .25)
        sheet.line(*p1, p1[0]-tx*1.7+ty*.8, p1[1]-ty*1.7-tx*.8, BLUE, .25)
        middle = sum(max(spans, key=lambda value: value[1]-value[0]))/2 if spans else length/2
        anchor = point(middle)
        labels.append((mark, anchor, (normal[0], -normal[1]), wall.id, side,
                       (float(base.x), float(base.y)), travel))
    boxes, segments = obstacles(sheet.primitives)
    label_bounds = (x+2, y+15, w-4, h-33)
    for mark, anchor, normal, wall_id, side, base, travel in labels:
        choices = []
        for distance in (4., 8., 13., 19., 26., 34.):
            for shift in (0., -6., 6., -13., 13., -22., 22.):
                px = anchor[0]+normal[0]*distance-normal[1]*shift
                py = anchor[1]+normal[1]*distance+normal[0]*shift
                rect = text_box(mark, 2.5, px, py, bold=True)
                if not contains(label_bounds, rect): continue
                covered, strokes = collision(rect, boxes, segments)
                if not covered and not strokes:
                    choices.append((distance+abs(shift), px, py, rect))
        if not choices:
            raise ValueError("Монтажный план не помещается с читаемыми марками")
        _, px, py, rect = min(choices, key=lambda item: item[0])
        sheet.line(*anchor, px, py-1, GRAY, .1)
        sheet.rect(*rect, "none", "#ffffff", 0)
        sheet.text(px, py, mark, 2.5, BLUE, True, "middle")
        boxes.append(rect)
        sheet.layout.setdefault("mount_plan_faces", []).append({"mark": mark, "wall_id": wall_id, "side": side,
            "base_xy_mm": list(base), "direction_xy": list(travel), "label_box": list(rect)})
    sheet.text(x+3, y+h-10, "Синим - сторона монтажа; кружок - база X=0; стрелка - X раскладки.", 2.5, GRAY)
    sheet.text(x+3, y+h-4, "Контур стен - без отделки. Марки совпадают с развёртками.", 2.5, GRAY)
    sheet.references.extend(by_id)
    return True


def add_mount_plan(sheets, first, views, project, opening_marks, title):
    """Prefer existing whitespace; only dense sheets need a separate plan."""
    from .album import _bounds_ok
    from .album_graphics import _append
    local = _context(project, views)
    if local is None:
        return
    from . import scale_for
    minx, miny, maxx, maxy = wall_footprint(local, openings=False).bounds
    full_scale = scale_for(maxx-minx, maxy-miny, 500, 260)
    def trial(box):
        if scale_for(maxx-minx, maxy-miny, box[2]-48, box[3]-48) > full_scale*2:
            return None
        sheet = Sheet(title, "mount_plan", 594, 420)
        try:
            ok = draw_mount_plan(sheet, project, views, opening_marks, box)
        except ValueError as error:
            if "помещ" in str(error): return None
            raise
        return sheet if ok and _bounds_ok(sheet, box) else None
    for page in sheets[first:]:
        cells = [c["box"] for c in page.layout.get("graphic_cells", [])]
        for width in (548., 270.):
            for height in (184., 154., 124.):
                for y in sorted({34., *(cy+ch+8 for _, cy, _, ch in cells)}):
                    for x in ([23., 301.] if width == 270 else [23.]):
                        box = (x, y, width, height)
                        if y+height > 342: continue
                        if any(min(x+width, cx+cw)>max(x, cx) and min(y+height, cy+ch)>max(y, cy)
                               for cx, cy, cw, ch in cells): continue
                        drawing = trial(box)
                        if drawing is None: continue
                        _append(page, drawing)
                        page.layout["mount_plan_faces"] = drawing.layout["mount_plan_faces"]
                        page.layout.setdefault("graphic_cells", []).append({"kind": "mount_plan", "mark": "Монтажный план", "marks": [], "box": list(box)})
                        page.layout["related_view_marks"] = list(dict.fromkeys([*page.layout.get("related_view_marks", []), *(m for m, _ in views)]))
                        return
    box = (23., 34., 548., 250.)
    drawing = trial(box)
    if drawing is None:
        drawing = trial((23., 34., 548., 308.))
    if drawing is None:
        # Split the locator by related walls, not a silent omitted room plan.
        if len(views) > 1:
            middle = len(views)//2
            add_mount_plan(sheets, len(sheets), views[:middle], project, opening_marks, title+" / часть 1")
            add_mount_plan(sheets, len(sheets), views[middle:], project, opening_marks, title+" / часть 2")
            return
        raise ValueError("Монтажный план не помещается на A2")
    drawing.layout.update({"related_view_marks": [m for m, _ in views], "detail_reference_marks": [], "view_marks": [], "detail_marks": []})
    sheets.insert(first, drawing)
