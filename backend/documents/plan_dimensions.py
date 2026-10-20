"""Dimensions of the depicted wall faces, not invisible model reference axes.

The model and elevations keep their reference-line contract. Only the plan
measures its joined, unfinished wall footprint, so a witness line always has
a visible geometric referent. Geometry is resolved in world millimetres before
the paper transform; rotation, unequal thicknesses and mitres are preserved.
"""
from __future__ import annotations

import math

from .plan_walls import wall_footprint
from .scene import BLUE, GRAY, width as text_width


def _num(value):
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def _rings(geometry):
    polygons = [geometry] if geometry.geom_type == "Polygon" else getattr(geometry, "geoms", [])
    for polygon in polygons:
        if polygon.geom_type == "Polygon":
            yield polygon.exterior.coords
            yield from (ring.coords for ring in polygon.interiors)


def wall_dimension_reference(floor, wall, footprint=None):
    """Return the visible straight face extent in wall-local world coordinates.

    The union boundary may extend past an axis endpoint at an outer mitre, or
    end before it at an inner corner/T-junction. Collinear neighbouring walls
    must not make this wall inherit the length of their entire shared face.
    A wholly obscured face has no honest visible dimension and returns None.
    """
    footprint = wall_footprint(floor, openings=False) if footprint is None else footprint
    origin = (float(wall.start.x), float(wall.start.y))
    length = math.hypot(wall.end.x-wall.start.x, wall.end.y-wall.start.y)
    if length <= 1e-8:
        return None
    tangent = ((wall.end.x-wall.start.x)/length, (wall.end.y-wall.start.y)/length)
    # Prefer the side opposite the occupied room. With two/no room assignments,
    # choose a stable world-facing normal, independent of endpoint ordering.
    left = any(face.side == "left" and face.space_id for face in wall.faces)
    right = any(face.side == "right" and face.space_id for face in wall.faces)
    normal = (tangent[1], -tangent[0])
    if right and not left:
        normal = (-normal[0], -normal[1])
    elif left == right and (normal[1] > 1e-8 or (abs(normal[1]) <= 1e-8 and normal[0] < 0)):
        normal = (-normal[0], -normal[1])
    half = float(wall.thickness_mm)/2
    epsilon = max(1e-5, length*1e-10)

    def extension_limit(endpoint):
        extent = 0.
        own_position = ((endpoint[0]-origin[0])*tangent[0]+(endpoint[1]-origin[1])*tangent[1])
        for other in floor.walls:
            if other.id == wall.id:
                continue
            dx, dy = other.end.x-other.start.x, other.end.y-other.start.y
            other_length = math.hypot(dx, dy)
            if other_length <= epsilon:
                continue
            direction = (dx/other_length, dy/other_length)
            delta = (endpoint[0]-other.start.x, endpoint[1]-other.start.y)
            along = delta[0]*direction[0]+delta[1]*direction[1]
            cross = abs(delta[0]*direction[1]-delta[1]*direction[0])
            parallel = abs(tangent[0]*direction[1]-tangent[1]*direction[0]) < 1e-8
            if parallel:
                if cross <= epsilon and -epsilon <= along <= other_length+epsilon:
                    # A subdivision of a straight wall ends at that visible
                    # face point, even when a perpendicular T also meets it.
                    positions = [((p.x-origin[0])*tangent[0]+(p.y-origin[1])*tangent[1])
                                 for p in (other.start, other.end)]
                    if (own_position < epsilon and min(positions) < -epsilon
                            or own_position > length-epsilon and max(positions) > length+epsilon):
                        return 0.
                continue
            if cross <= epsilon and -epsilon <= along <= other_length+epsilon:
                # Same bounded-mitre ceiling as the presentation footprint.
                extent = max(extent, 4*max(half, float(other.thickness_mm)/2))
        return extent

    limits = (-extension_limit(origin), length+extension_limit((wall.end.x, wall.end.y)))
    face_segments = []
    for ring in _rings(footprint):
        points = list(ring)
        for a, b in zip(points, points[1:]):
            local = []
            for point in (a, b):
                dx, dy = point[0]-origin[0], point[1]-origin[1]
                local.append((dx*tangent[0]+dy*tangent[1], dx*normal[0]+dy*normal[1]))
            if any(abs(point[1]-half) > epsilon for point in local):
                continue
            low, high = sorted(point[0] for point in local)
            if high-low > epsilon:
                face_segments.append((low, high))
    # GEOS legitimately keeps collinear vertices at strip/mitre boundaries.
    # Merge these segments first: the short corner extension can merely touch
    # t=0/L and would otherwise be discarded as belonging to another wall.
    merged = []
    for low, high in sorted(face_segments):
        if merged and low <= merged[-1][1]+epsilon:
            merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
        else:
            merged.append((low, high))
    intervals = []
    for low, high in merged:
        # Include only boundary pieces belonging to this wall, not a separate
        # collinear wall beyond either endpoint.
        if min(high, length)-max(low, 0.) <= epsilon:
            continue
        low, high = max(low, limits[0]), min(high, limits[1])
        if high-low > epsilon:
            intervals.append((low, high))
    if not intervals:
        return None
    low, high = min(item[0] for item in intervals), max(item[1] for item in intervals)
    return {"origin_mm": origin, "direction_mm": tangent, "normal_mm": normal,
            "offset_mm": half, "low_mm": low, "high_mm": high,
            "visible_intervals_mm": intervals}


def draw_plan_dimensions(sheet, floor, transform, scale):
    """Draw face lengths and opening-jamb chains with actual contour anchors."""
    from . import DocumentError

    occupied, labels = [], []
    footprint = wall_footprint(floor, openings=False)
    records = sheet.layout.setdefault("plan_dimensions", [])
    for primitive in sheet.primitives:
        if primitive["type"] == "text":
            size = primitive["size"]
            length = text_width(primitive["value"], size, primitive["bold"])
            left = primitive["x"]-(length/2 if primitive["anchor"] == "middle" else length if primitive["anchor"] == "end" else 0)
            occupied.append((left-.7, primitive["y"]-size-.5, length+1.4, size+1.5))

    def overlap(a, b):
        return max(0, min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0]))*max(0, min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))

    for wall in floor.walls:
        reference = wall_dimension_reference(floor, wall, footprint)
        if reference is None:
            continue
        origin, tangent, normal = (reference[key] for key in ("origin_mm", "direction_mm", "normal_mm"))
        low, high, half = (reference[key] for key in ("low_mm", "high_mm", "offset_mm"))

        def point_at(position):
            return (origin[0]+tangent[0]*position+normal[0]*half,
                    origin[1]+tangent[1]*position+normal[1]*half)

        pa, pb = transform(*point_at(low)), transform(*point_at(high))
        paper_length = math.dist(pa, pb)
        ux, uy = (pb[0]-pa[0])/paper_length, (pb[1]-pa[1])/paper_length
        normal_point = transform(origin[0]+normal[0]*scale, origin[1]+normal[1]*scale)
        axis_point = transform(*origin)
        nx, ny = normal_point[0]-axis_point[0], normal_point[1]-axis_point[1]
        normal_length = math.hypot(nx, ny)
        nx, ny = nx/normal_length, ny/normal_length
        openings = [opening for opening in floor.openings if opening.wall_id == wall.id]
        cuts = sorted({low, high, *[min(high, max(low, float(value))) for opening in openings
                                   for value in (opening.offset_mm, opening.offset_mm+opening.width_mm)]})
        spans = [(start, end, 5.5, "wall_span") for start, end in zip(cuts, cuts[1:]) if end-start > .001]
        if openings:
            spans.append((low, high, 14., "wall_overall"))
        for start, end, offset, kind in spans:
            first = len(sheet.primitives)
            start_world, end_world = point_at(start), point_at(end)
            pa, pb = transform(*start_world), transform(*end_world)
            da, db = (pa[0]+nx*offset, pa[1]+ny*offset), (pb[0]+nx*offset, pb[1]+ny*offset)
            for point, dimension in ((pa, da), (pb, db)):
                # The small air gap starts at the actual face/corner, not at
                # a displaced axis. It cannot visually imply another wall.
                sheet.line(point[0]+nx*1.2, point[1]+ny*1.2,
                           dimension[0]+nx, dimension[1]+ny, BLUE, .12)
                sheet.line(dimension[0]-.65*(ux+nx), dimension[1]-.65*(uy+ny),
                           dimension[0]+.65*(ux+nx), dimension[1]+.65*(uy+ny), BLUE, .2)
            sheet.line(*da, *db, BLUE, .15)
            records.append({"wall_id": wall.id, "kind": kind, "basis": "wall_faces_without_finish",
                            "start_mm": list(start_world), "end_mm": list(end_world),
                            "direction_mm": list(tangent), "normal_mm": list(normal), "value_mm": end-start})
            label, size = _num(end-start), 2.6
            label_w = text_width(label, size)+1.5
            mx, my = (da[0]+db[0])/2, (da[1]+db[1])/2
            separation = abs(nx)*label_w/2+abs(ny)*2+1
            options = []
            for extra in (0, 4.5, 9, 13.5, 18):
                for along in (0, -4, 4, -8, 8):
                    cx, cy = mx+nx*(separation+extra)+ux*along, my+ny*(separation+extra)+uy*along
                    rectangle = (cx-label_w/2, cy-2, label_w, 4)
                    if rectangle[0] < 23 or rectangle[0]+label_w > sheet.width-8 or rectangle[1] < 43 or rectangle[1]+4 > sheet.height-77:
                        continue
                    options.append((sum(overlap(rectangle, other) for other in occupied), extra+abs(along), cx, cy, rectangle))
            if not options:
                raise DocumentError("Размеры плана не помещаются на лист. Разделите этаж на фрагменты или используйте A2.")
            collision, displacement, cx, cy, rectangle = min(options, key=lambda value: value[:2])
            occupied.append(rectangle)
            if displacement > 0 or (end-start)/scale < label_w+2:
                sheet.line(mx, my, cx, cy, BLUE, .1)
            labels.append((rectangle, cx, cy+.9, label, size,
                           list(range(first, len(sheet.primitives))), pa, pb, da, (ux, uy), end-start))
    for rectangle, cx, cy, label, size, indices, pa, pb, da, direction, value in labels:
        first = len(sheet.primitives)
        sheet.rect(*rectangle, "none", "#ffffff", 0)
        sheet.text(cx, cy, label, size, BLUE, anchor="middle")
        sheet.register_dimension([*indices, *range(first, len(sheet.primitives))], pa, pb, da,
                                 label, direction=direction, value_mm=value)


def draw_plan_overall_dimension(sheet, footprint, transform):
    """Horizontal envelope, with separate witnesses at real extreme vertices."""
    points = [point for ring in _rings(footprint) for point in ring]
    if not points:
        return
    minx, miny, maxx, _ = footprint.bounds
    for dimension in sheet.layout.get("plan_dimensions", []):
        # On an axis-aligned rectangular room the outside wall dimension is
        # already its full horizontal envelope. A second grey copy adds no
        # information. Oblique wall lengths are not horizontal envelopes.
        if abs(dimension["direction_mm"][1]) > 1e-8:
            continue
        xs = sorted((dimension["start_mm"][0], dimension["end_mm"][0]))
        if (abs(xs[0]-minx) < 1e-5 and abs(xs[1]-maxx) < 1e-5
                and abs(dimension["value_mm"]-(maxx-minx)) < 1e-5):
            return
    a = min(points, key=lambda point: (point[0], point[1]))
    b = min(points, key=lambda point: (-point[0], point[1]))
    pa, pb = transform(*a), transform(*b)
    _, bottom = transform(minx, miny)
    y = bottom+26
    first = len(sheet.primitives)
    for point in (pa, pb):
        sheet.line(point[0], point[1]+1.2, point[0], y+1.2, GRAY, .15)
        sheet.line(point[0]-.7, y+.7, point[0]+.7, y-.7, GRAY, .25)
    sheet.line(pa[0], y, pb[0], y, GRAY, .18)
    sheet.text((pa[0]+pb[0])/2, y-1.2, _num(maxx-minx), 2.8, GRAY, anchor="middle")
    sheet.register_dimension(range(first, len(sheet.primitives)), pa, pb, (pa[0], y),
                             _num(maxx-minx), value_mm=maxx-minx)
    sheet.layout.setdefault("plan_dimensions", []).append({"wall_id": None, "kind": "floor_overall",
        "basis": "wall_envelope_without_finish", "start_mm": list(a), "end_mm": list(b),
        "direction_mm": [1., 0.], "normal_mm": [0., -1.], "value_mm": maxx-minx})
