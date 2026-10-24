"""Visible joint faces and opaque floor slabs for the customer view.

Small prisms only: no STEP reads, hardware or CAD jobs. Joint axes come from
the same setting-out as panels; these are appearance envelopes, not a second
engineering assembly. Floor depth is a display setting, not a slab design.
"""
from __future__ import annotations

import math

from shapely.affinity import translate
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from .finish_geometry import outline, polygons
from .documents.plan_walls import wall_footprint

PROFILE_COLOR = "#d7d9dc"
FLOOR_COLOR = "#bdb9b0"


def joint_face(config):
    """Visible face only, in mm relative to the front surface of the panel.

    V-T1's 6 x 1.2 cap and the concealed 5.5 mm face recess are taken from
    the existing calibrated recipes. Never expand a real panel gap for visual
    clarity or substitute an open T cap for an unknown concealed system.
    """
    gap = config["joint_mm"]
    rule = config.get("node_rule_ids", {}).get("joints") or config.get("assembly_rule_id")
    if rule in {"open-1-1", "open-1-3", "open-1-4"} or (
            not rule and config["mounting_system"] == "open_mount"
            and config.get("panel_edge") in (None, "V-0") and abs(gap-2.) < .001):
        return {"family": "V-T1", "width_mm": 6., "front_mm": 1.2, "depth_mm": 1.2,
                "basis": "calibrated_visible_cap"}
    if rule in {"concealed-2-1", "concealed-2-2", "concealed-2-3", "concealed-2-6", "concealed-2-7",
                "concealed-2-4", "concealed-2-5"}:
        family = "VH-2" if rule in {"concealed-2-4", "concealed-2-5"} else "VH-3"
        return {"family": family, "width_mm": gap, "front_mm": -5.5, "depth_mm": 1.2,
                "basis": "calibrated_recess_visible_through_joint"}
    # A neutral face in the *actual* gap, not a fabricated SKU/large cover.
    # Clipped to the gap so it never needs a hidden groove in the panel model.
    return {"family": "Межпанельный профиль", "width_mm": gap,
            "front_mm": -min(5.5, config["panel_thickness_mm"]*.5)
                         if config["mounting_system"] == "concealed_mount" else 0.,
            "depth_mm": 1.2, "basis": "presentation_joint_face"}


def joint_rectangles(view, columns, rows):
    """Disjoint vertical/horizontal strips, cut at openings and intersections.

    Panel fragments made by an opening are NOT additional panel joints.
    Horizontal caps butt against vertical caps rather than intersect them.
    """
    from .engineering import _subtract_rectangles
    face = joint_face(view["configuration"])
    if view["configuration"]["joint_mm"] <= 1e-6 or not columns or not rows:
        return face, []
    half = face["width_mm"]/2
    left, right = columns[0][0], sum(columns[-1])
    low, high = rows[0][0], sum(rows[-1])
    vertical, horizontal = [], []
    for (x, width), (next_x, _) in zip(columns, columns[1:]):
        if next_x-x-width > 1e-6:
            center = (x+width+next_x)/2
            a, b = max(left, center-half), min(right, center+half)
            vertical.append((a, low, b-a, high-low))
    for (y, height), (next_y, _) in zip(rows, rows[1:]):
        if next_y-y-height > 1e-6:
            center = (y+height+next_y)/2
            a, b = max(low, center-half), min(high, center+half)
            horizontal.extend(_subtract_rectangles((left, a, right-left, b-a), vertical, 1e-7))
    cutters = [(o["x_mm"], o["y_mm"], o["width_mm"], o["height_mm"]) for o in view["openings"]]
    result = []
    for orientation, rectangles in (("vertical", vertical), ("horizontal", horizontal)):
        for rectangle in rectangles:
            for x, y, width, height in _subtract_rectangles(rectangle, cutters, 1e-7):
                if min(width, height) > 1e-6:
                    result.append({"orientation": orientation, "x_mm": x, "y_mm": y,
                                   "width_mm": width, "height_mm": height})
    return face, result


def disjoint_profiles(profiles):
    """Butt the projecting metal caps at cross-wall/T intersections too.

    Wall clipping uses panel front planes, so 1.2 mm proud caps from two walls
    can still overlap. Resolve only intersecting candidates and only their
    shared height bands. One immutable spatial index, not an all-pairs pass.
    """
    parts = [(owner, mesh) for owner in profiles for mesh in owner["meshes"]]
    shapes = [Polygon(m["footprint_mm"], m.get("holes_mm", [])) for _, m in parts]
    tree = STRtree(shapes)
    result = {p["id"]: [] for p in profiles}
    for i, (owner, mesh) in enumerate(parts):
        low, high = mesh["bottom_mm"], mesh["bottom_mm"]+mesh["height_mm"]
        candidates = [int(j) for j in tree.query(shapes[i], predicate="intersects")
                      if j < i and parts[j][0]["id"] != owner["id"]
                      and min(high, parts[j][1]["bottom_mm"]+parts[j][1]["height_mm"])
                          -max(low, parts[j][1]["bottom_mm"]) > 1e-7
                      and shapes[i].intersection(shapes[j]).area > 1e-7]
        if not candidates:
            result[owner["id"]].append(mesh)
            continue
        levels = sorted({low, high, *(z for j in candidates for z in
            (parts[j][1]["bottom_mm"], parts[j][1]["bottom_mm"]+parts[j][1]["height_mm"]) if low < z < high)})
        for a, b in zip(levels, levels[1:]):
            z = (a+b)/2
            cutters = [shapes[j] for j in candidates if parts[j][1]["bottom_mm"] < z <
                       parts[j][1]["bottom_mm"]+parts[j][1]["height_mm"]]
            shape = shapes[i].difference(unary_union(cutters)) if cutters else shapes[i]
            result[owner["id"]].extend({**outline(poly), "bottom_mm": a, "height_mm": b-a}
                                        for poly in polygons(shape))
    return [{**p, "meshes": result[p["id"]], "footprint_mm": result[p["id"]][0]["footprint_mm"]}
            for p in profiles if result[p["id"]]]


def floor_slabs(floor, position, style, warnings, *, space_id=None, tolerance=.1):
    """Closed room footprints, unioned once per floor, with shafts kept open.

    No bounding rectangle/hull: concave buildings and separated wings stay
    concave/separate. Use named rooms when present, so an unassigned courtyard
    is not inferred to be a room. A wall-only closed plan is a fallback.
    """
    rooms, boundary_ids = [], set()
    walls = {w.id: w for w in floor.walls}
    for space in floor.spaces:
        if space_id is not None and space.id != space_id:
            continue
        segments = []
        for edge in space.boundary:
            wall = walls.get(edge.wall_id)
            if wall is None:
                break
            a, b = (wall.end, wall.start) if edge.reversed else (wall.start, wall.end)
            segments.append(((a.x, a.y), (b.x, b.y)))
        closed = (len(segments) == len(space.boundary) and len(segments) >= 3
                  and all(math.dist(a[1], b[0]) <= tolerance for a, b in
                          zip(segments, segments[1:]+segments[:1])))
        poly = Polygon([a for a, _ in segments]) if closed else None
        if poly is None or not poly.is_valid or poly.area <= tolerance*tolerance:
            warnings.append(f"Пол помещения «{space.name}» не показан: проверьте замкнутость контура.")
            continue
        rooms.append(poly)
        boundary_ids.update(e.wall_id for e in space.boundary)
    if not floor.spaces and space_id is None:
        lines = [LineString([(w.start.x, w.start.y), (w.end.x, w.end.y)]) for w in floor.walls]
        rooms = list(polygonize(unary_union(lines)))
        # Preserve holes of nested rings, rather than filling the courtyard
        # cell which polygonize also returns as a standalone polygon.
        rooms = [p for p in rooms if not any(p != q and Polygon(q.exterior).contains(p)
                                           for q in rooms)]
        area = unary_union(rooms)
        boundary_ids = {w.id for w, line in zip(floor.walls, lines) if area.intersects(line)}
    if not rooms:
        return []
    boundary_floor = floor.model_copy(update={"walls": [walls[k] for k in boundary_ids], "openings": []})
    shape = unary_union([*rooms, wall_footprint(boundary_floor, openings=False)])
    levels = {float(level.elevation_mm) for level in floor.levels if level.kind == "finished_floor"}
    local_z = next(iter(levels)) if len(levels) == 1 else 0.
    if len(levels) > 1:
        warnings.append(f"На этаже «{floor.name}» несколько отметок чистого пола; в 3D использована отметка этажа.")
    holes = [Polygon([(p.x, p.y) for p in o.footprint]) for o in floor.obstacles
             if o.kind == "shaft" and o.base_mm <= local_z+.001
             and o.base_mm+o.height_mm > local_z
             and (space_id is None or o.space_id in (None, space_id))]
    if holes:
        shape = shape.difference(unary_union([p for p in holes if p.is_valid]))
    shape = translate(shape, floor.origin.x+position["presentation_offset_mm"][0],
                      floor.origin.y+position["presentation_offset_mm"][1])
    depth = style.floor_thickness_mm
    z = position["elevation_mm"]+local_z
    return [{"id": f"floor:{floor.id}:{i}", "floor_id": floor.id, "kind": "floor",
             "label": f"Пол · {floor.name}", "material": "floor",
             "color": style.floor_colors.get(floor.id, style.floor_color),
             "bottom_mm": z-depth, "height_mm": depth, "thickness_mm": depth,
             "top_mm": z, "thickness_basis": "presentation", **outline(poly)}
            for i, poly in enumerate(polygons(shape), 1)]
