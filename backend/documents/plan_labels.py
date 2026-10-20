"""Plan identifiers placed after dimensions in independent readable lanes."""
from __future__ import annotations

import math

from .label_layout import collision, contains, obstacles, segment_hits, text_box
from .plan_dimensions import wall_dimension_reference
from .plan_walls import wall_footprint
from .scene import BLUE, GRAY


def draw_space_labels(sheet, floor, transform):
    """Place room bubbles after dimensions and face/opening marks.

    The saved room and every dimension stay untouched. Only the printed room
    identifier moves to the nearest clear point inside its own usable area.
    Wall-linked marks have less placement freedom, so they get their lanes
    first; the bubble must also clear all of their leaders and text.
    """
    from shapely.geometry import LineString, Point, Polygon, box
    from shapely.ops import nearest_points, transform as project_shape, unary_union
    from . import DocumentError

    walls = {wall.id: wall for wall in floor.walls}
    occupied_wall = wall_footprint(floor, openings=False)
    obstructions = [Polygon([(p.x, p.y) for p in item.footprint]) for item in floor.obstacles]
    for space in floor.spaces:
        vertices = [walls[e.wall_id].end if e.reversed else walls[e.wall_id].start for e in space.boundary]
        room = Polygon([(p.x, p.y) for p in vertices])
        initial = room.representative_point()
        original = Point(*transform(initial.x, initial.y))
        radius = max(5., len(space.code)*.9)
        usable = room.difference(occupied_wall)
        if obstructions:
            usable = usable.difference(unary_union(obstructions))
        paper = project_shape(lambda x,y: transform(x,y), usable)
        allowed = paper.buffer(-(radius+.6)).intersection(
            box(23+radius+.6, 43+radius+.6, sheet.width-8-radius-.6, sheet.height-77-radius-.6))
        boxes, segments = obstacles(sheet.primitives)
        blocked = [box(x, y, x+w, y+h).buffer(radius+.5) for x,y,w,h in boxes]
        blocked.extend(LineString([a,b]).buffer(radius+.5) for a,b in segments if a != b)
        if blocked:
            allowed = allowed.difference(unary_union(blocked))
        if allowed.is_empty:
            raise DocumentError(f"Номер помещения {space.code} не помещается без пересечений на плане. Используйте A2 или увеличенный фрагмент.")
        centre = original if allowed.covers(original) else nearest_points(allowed, original)[0]
        x, y = float(centre.x), float(centre.y)
        sheet.circle(x, y, radius, BLUE, "#ffffff")
        sheet.text(x, y+1, space.code, 3.1, BLUE, True, "middle")
        sheet.layout.setdefault("space_labels", []).append({"space_id": space.id, "code": space.code,
            "centre": [x,y], "radius": radius, "original_centre": [original.x,original.y]})


def _caption_box(lines, x, y):
    """Centre a multi-line caption as one collision box; keep font size fixed."""
    leading = 3.9
    baselines = [y+2.7/2-.175+(index-(len(lines)-1)/2)*leading for index in range(len(lines))]
    rectangles = [text_box(line,2.7,x,baseline,bold=True) for line,baseline in zip(lines,baselines)]
    left = min(rect[0] for rect in rectangles); top = min(rect[1] for rect in rectangles)
    right = max(rect[0]+rect[2] for rect in rectangles); bottom = max(rect[1]+rect[3] for rect in rectangles)
    return (left,top,right-left,bottom-top), baselines


def _route_around_captions(source, end, normal, tangent, along, depth, boxes):
    """Keep the leader anchored on its face, without crossing existing text."""
    nx,ny=normal;tx,ty=tangent
    knee=(source[0]+nx*1.1,source[1]+ny*1.1)
    # A diagonal can hit a dimension even when both ends are clear. The
    # alternative bends stay entirely in the selected wall's half-plane.
    routes=[[source,knee,end],
            [source,knee,(knee[0]+tx*along,knee[1]+ty*along),end],
            [source,(source[0]+nx*depth,source[1]+ny*depth),end]]
    return next((points for points in routes if not any(
        segment_hits(box,a,b) for a,b in zip(points,points[1:]) for box in boxes)),None)


def draw_plan_labels(sheet, floor, transform, scale, opening_marks, view_labels):
    """View labels stay on their wall side; door labels keep a jamb leader.

    ``view_labels`` contains (wall_id, side, text). Neither the building model
    nor any dimension/fastener coordinate is adjusted to make labels fit.
    """
    from . import DocumentError
    walls = {wall.id: wall for wall in floor.walls}
    footprint = wall_footprint(floor, openings=False)
    requests = []
    for opening in floor.openings:
        wall = walls[opening.wall_id]
        reference = wall_dimension_reference(floor, wall, footprint)
        length = math.hypot(wall.end.x-wall.start.x, wall.end.y-wall.start.y)
        tangent = ((wall.end.x-wall.start.x)/length, (wall.end.y-wall.start.y)/length)
        normal = reference["normal_mm"] if reference else (tangent[1], -tangent[0])
        centre = float(opening.offset_mm+opening.width_mm/2)
        # Nearby dimension text may cover the centre and both inner anchors.
        # Also try beside either jamb, always inside this SAME opening.
        requests.append((wall, normal, opening_marks[opening.id], "opening", opening.id,
                         [centre, centre-opening.width_mm*.22, centre+opening.width_mm*.22,
                          centre-opening.width_mm*.42, centre+opening.width_mm*.42], centre))
    for wall_id, side, label in view_labels:
        wall = walls[wall_id]
        length = math.hypot(wall.end.x-wall.start.x, wall.end.y-wall.start.y)
        tangent = ((wall.end.x-wall.start.x)/length, (wall.end.y-wall.start.y)/length)
        sign = 1 if side == "left" else -1
        normal = (-tangent[1]*sign, tangent[0]*sign)
        cuts = [(float(o.offset_mm), float(o.offset_mm+o.width_mm)) for o in floor.openings if o.wall_id == wall.id]
        solids, previous = [], 0.
        for lo, hi in sorted(cuts):
            if lo > previous: solids.append((previous, lo))
            previous = max(previous, hi)
        if previous < length: solids.append((previous, length))
        # A view mark belongs beside solid wall, not at the centre of a door.
        choices = [(a+b)/2 for a,b in sorted(solids, key=lambda item: (abs((item[0]+item[1])/2-length/2), -(item[1]-item[0])))]
        choices += [length*f for f in (.5,.25,.75,.12,.88)]
        requests.append((wall, normal, label, "view", wall.id, choices, length/2))
    boxes, segments = obstacles(sheet.primitives)
    bounds = (23., 43., sheet.width-31., sheet.height-120.)
    for wall, normal, label, kind, identifier, choices, target in requests:
        length = math.hypot(wall.end.x-wall.start.x, wall.end.y-wall.start.y)
        tangent = ((wall.end.x-wall.start.x)/length, (wall.end.y-wall.start.y)/length)
        half = float(wall.thickness_mm)/2
        centre = transform(wall.start.x, wall.start.y)
        normal_point = transform(wall.start.x+normal[0]*scale, wall.start.y+normal[1]*scale)
        nx, ny = normal_point[0]-centre[0], normal_point[1]-centre[1]
        magnitude = math.hypot(nx, ny); nx /= magnitude; ny /= magnitude
        # Centre the complete text box, not its baseline. In the narrow lane
        # between a wall and its 5.5-mm dimension chain even a .3-mm baseline
        # error can make the caption touch that chain.
        variants = [[label]]
        if " / " in label:
            variants.append(label.split(" / "))
        chosen = None
        tangent_point=transform(wall.start.x+tangent[0]*scale,wall.start.y+tangent[1]*scale)
        tx,ty=tangent_point[0]-centre[0],tangent_point[1]-centre[1]
        for fallback in (False,True):
            for lines in variants:
                prototype,_ = _caption_box(lines,0,0)
                clearance = abs(nx)*prototype[2]/2+abs(ny)*prototype[3]/2+.65
                options = []
                for position_index, position in enumerate(dict.fromkeys(choices)):
                    source = transform(wall.start.x+tangent[0]*position+normal[0]*half,
                                       wall.start.y+tangent[1]*position+normal[1]*half)
                    # A short, narrow room may not fit even a stacked view mark
                    # inside its walls. Extend a dedicated annotation lane past
                    # the wall end, with a dogleg tied to the same wall face.
                    tangential = (0.,) if not fallback else tuple(sign*distance for distance in (8.,14.,20.,28.,38.,50.) for sign in (-1,1))
                    for along in tangential:
                        for extra in (0.,4.5,9.,14.,20.,27.):
                            px,py=source[0]+nx*(clearance+extra)+tx*along,source[1]+ny*(clearance+extra)+ty*along
                            rect,baselines=_caption_box(lines,px,py)
                            if not contains(bounds,rect): continue
                            covered,strokes=collision(rect,boxes,segments)
                            if covered or strokes: continue
                            score=extra+position_index*1.2+abs(along)
                            needs_leader=fallback or score>1.2 or (kind=="opening" and abs(position-target)>.01)
                            route=[source,(px,py)] if needs_leader else []
                            if fallback:
                                route=_route_around_captions(source,(px,py),(nx,ny),(tx,ty),along,clearance+extra,boxes)
                                if route is None:continue
                            elif any(segment_hits(box,a,b) for a,b in zip(route,route[1:]) for box in boxes): continue
                            options.append((score,px,py,rect,source,position,lines,baselines,route))
                if options:
                    chosen=min(options,key=lambda item:item[0]);break
            if chosen is not None:break
        if chosen is None:
            raise DocumentError(f"Обозначение {label} не помещается без пересечений на плане. Используйте A2 или разделите этаж на фрагменты.")
        score,px,py,rect,source,position,lines,baselines,route=chosen
        if route:
            # The leader terminates at the relevant wall face. It never moves
            # a door mark onto another opening to find free space.
            for a,b in zip(route,route[1:]):
                sheet.line(*a,*b,GRAY,.1)
                segments.append((a,b))
        sheet.rect(*rect, "none", "#ffffff", 0)
        for line,baseline in zip(lines,baselines):
            sheet.text(px,baseline,line,2.7,BLUE,True,"middle")
        boxes.append(rect)
        sheet.layout.setdefault("plan_labels", []).append({"kind": kind, "entity_id": identifier, "label": label,
            "wall_id": wall.id, "normal_mm": list(normal), "box": rect, "anchor": source,
            "primitive_index": len(sheet.primitives)-1,"caption_lines":lines,"leader":route})
