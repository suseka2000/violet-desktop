"""Small, closed presentation solids. No BRep/STEP or fastener instances.

All clipping takes place in plan, in height bands at opening/wall boundaries.
Caps are constrained triangulations, including concavities and inner rings.
This is display geometry; the canonical drawing is never silently trimmed.
"""
from __future__ import annotations

import math
from collections import defaultdict

from shapely import constrained_delaunay_triangles
from shapely.affinity import translate
from shapely.geometry import GeometryCollection, LineString, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .documents.plan_walls import wall_footprint
from .exterior_mount import _envelopes


MATERIALS = {
    "concrete": ("Бетон", "#afb3b2"),
    "solid_masonry": ("Полнотелая кладка", "#b8876e"),
    "hollow_masonry": ("Пустотелая кладка", "#b8876e"),
    "gypsum": ("Гипсовое основание", "#dfded6"),
    "unknown": ("Материал основания не указан", "#b8bcc0"),
}


def polygons(geometry):
    if geometry.geom_type == "Polygon":
        if geometry.area > 1e-6:
            yield geometry
    elif hasattr(geometry, "geoms"):
        for item in geometry.geoms:
            yield from polygons(item)


def outline(polygon):
    """An extrusion ring and watertight cap tessellation, never a convex hull."""
    def coords(ring):
        return [[float(x), float(y)] for x, y in list(ring.coords)[:-1]]
    return {"footprint_mm": coords(polygon.exterior),
            "holes_mm": [coords(ring) for ring in polygon.interiors],
            "cap_triangles_mm": [coords(t.exterior) for t in
                                 constrained_delaunay_triangles(polygon).geoms]}


def strip(wall, left, right, shift=(0., 0.), lo=0., hi=None):
    length = math.hypot(wall.end.x-wall.start.x, wall.end.y-wall.start.y)
    if length <= 1e-8:
        return GeometryCollection()
    hi = length if hi is None else hi
    t = ((wall.end.x-wall.start.x)/length, (wall.end.y-wall.start.y)/length)
    n = (-t[1], t[0])
    return Polygon([(wall.start.x+shift[0]+t[0]*x+n[0]*y,
                     wall.start.y+shift[1]+t[1]*x+n[1]*y)
                    for x, y in ((lo, left), (hi, left), (hi, -right), (lo, -right))])


class FloorGeometry:
    def __init__(self, floor, position, views, framing, warnings):
        self.floor, self.position = floor, position
        self.shift = [getattr(floor.origin, axis)+position["presentation_offset_mm"][i]
                      for i, axis in enumerate(("x", "y"))]
        self.z = position["elevation_mm"]
        self.walls = {w.id: w for w in floor.walls}
        self.views = defaultdict(list)
        for v in views:
            self.views[v["wall_id"]].append(v)
        self.framing = framing
        self.warnings = warnings
        self.openings = defaultdict(list)
        for opening in floor.openings:
            if opening.wall_id in self.walls:
                w = self.walls[opening.wall_id]
                length = math.hypot(w.end.x-w.start.x, w.end.y-w.start.y)
                if opening.offset_mm+opening.width_mm > length+.001 or opening.sill_mm+opening.height_mm > w.height_mm+.001:
                    raise ValueError(f"Проём «{opening.name}» выходит за границы стены «{w.name}»")
                self.openings[opening.wall_id].append(opening)
        segments = tuple((w.id, w.start.x, w.start.y, w.end.x, w.end.y) for w in floor.walls)
        envelopes = _envelopes(segments)
        self.envelope = translate(unary_union(envelopes), *self.shift) if envelopes else None
        self.boundary_ids = {e.wall_id for s in floor.spaces for e in s.boundary}
        self.cores, self.extents, self.ends, self.trimmed = {}, {}, {}, {}
        self.levels = set()
        for w in floor.walls:
            self.levels.update((self.z+w.base_mm, self.z+w.base_mm+w.height_mm))
            for o in self.openings[w.id]:
                self.levels.update((self.z+w.base_mm+o.sill_mm,
                                   self.z+w.base_mm+o.sill_mm+o.height_mm))
            half = w.thickness_mm/2
            left = max([half, *(v["front_offset_mm"] for v in self.views[w.id] if v["side"] == "left")])
            right = max([half, *(v["front_offset_mm"] for v in self.views[w.id] if v["side"] == "right")])
            self.cores[w.id] = strip(w, half, half, self.shift)
            self.extents[w.id] = strip(w, left, right, self.shift)
            self.ends[w.id] = (left, right)
            if w.properties.get("editor_kind") == "partition" and self.envelope is not None and not any(
                    v["configuration"].get("installation_zone") == "exterior" for v in self.views[w.id]):
                line = LineString([(w.start.x+self.shift[0], w.start.y+self.shift[1]),
                                   (w.end.x+self.shift[0], w.end.y+self.shift[1])])
                if line.intersection(self.envelope).length > .001:
                    extra = self.cores[w.id].difference(self.envelope)
                    if extra.area > 1e-6:
                        self.trimmed[w.id] = extra
                        self.cores[w.id] = self.cores[w.id].intersection(self.envelope)
                    if line.difference(self.envelope).length > .1:
                        warnings.append(f"Перегородка «{w.name}» ограничена контуром здания в 3D. Её опорная линия в чертеже выходит наружу.")
        self.order = list(self.walls)
        self.tree = STRtree([self.extents[k] for k in self.order])
        self._bands = {}

    def material(self, wall):
        aliases = {"бетон": "concrete", "кирпич": "solid_masonry", "brick": "solid_masonry",
                   "гипс": "gypsum", "гипсокартон": "gypsum"}
        modeled = aliases.get(wall.substrate.casefold(), wall.substrate)
        if modeled in MATERIALS and modeled != "unknown":
            return modeled, "wall"
        values = {c.get("substrate_type", "unknown") for key, c in self.framing.items() if key[0] == wall.id}
        if len(values) == 1 and next(iter(values)) in MATERIALS:
            return next(iter(values)), "mount_settings"
        if len(values) > 1:
            self.warnings.append(f"Для стены «{wall.name}» на двух сторонах указаны разные основания; показан нейтральный материал.")
        return "unknown", "unspecified"

    def band(self, z):
        # Cache by actual opening/wall band; dozens of panel rows share it.
        key = sum(level <= z for level in self.levels)
        if key in self._bands:
            return self._bands[key]
        core, extent, active_openings, active = {}, {}, [], []
        for w in self.floor.walls:
            if not self.z+w.base_mm < z < self.z+w.base_mm+w.height_mm:
                continue
            active.append(w)
            cutters, outer_cutters = [], []
            for o in self.openings[w.id]:
                if self.z+w.base_mm+o.sill_mm < z < self.z+w.base_mm+o.sill_mm+o.height_mm:
                    active_openings.append(o)
                    cutters.append(strip(w, w.thickness_mm/2+.001, w.thickness_mm/2+.001,
                                         self.shift, o.offset_mm, o.offset_mm+o.width_mm))
                    left, right = self.ends[w.id]
                    outer_cutters.append(strip(w, left+.001, right+.001, self.shift,
                                               o.offset_mm, o.offset_mm+o.width_mm))
            core[w.id] = self.cores[w.id].difference(unary_union(cutters)) if cutters else self.cores[w.id]
            extent[w.id] = self.extents[w.id].difference(unary_union(outer_cutters)) if cutters else self.extents[w.id]
        joined = wall_footprint(self.floor.model_copy(update={"walls": active, "openings": active_openings}))
        joined = translate(joined, *self.shift)
        trims = [self.trimmed[w.id] for w in active if w.id in self.trimmed]
        if trims:
            # Restore other walls (including their proper corner wedges), not
            # the part of a partition accidentally crossing the outer contour.
            untouched = [w for w in active if w.id not in self.trimmed]
            other = wall_footprint(self.floor.model_copy(update={"walls": untouched, "openings": active_openings}))
            joined = unary_union([joined.difference(unary_union(trims)), translate(other, *self.shift), *core.values()])
        value = {"core": core, "extent": extent, "joined": joined}
        self._bands[key] = value
        return value

    def panel_parts(self, view, footprint, bottom, height):
        """Trim against actual structure and adjacent finish depth at T joints.

        Do not apply an interior envelope to facade cladding or unrelated,
        separately drawn outside partitions. Only partitions whose centreline
        enters this building are bounded by its outline.
        """
        shape = Polygon(footprint)
        wall = self.walls[view["wall_id"]]
        interior = view["configuration"].get("installation_zone", "interior") == "interior"
        line = LineString([(wall.start.x+self.shift[0], wall.start.y+self.shift[1]),
                           (wall.end.x+self.shift[0], wall.end.y+self.shift[1])])
        if interior and self.envelope is not None and line.intersection(self.envelope).length > .001:
            shape = shape.intersection(self.envelope)
        if shape.is_empty:
            return []
        candidates = [self.order[int(i)] for i in self.tree.query(shape, predicate="intersects")
                      if self.order[int(i)] != wall.id]
        heights = sorted({bottom, bottom+height, *(h for h in self.levels if bottom < h < bottom+height)})
        result = []
        for low, high in zip(heights, heights[1:]):
            band = self.band((low+high)/2)
            # Own same-room miter neighbours already split the corner at the
            # bisector; cutting their full cavity again would leave a notch.
            obstacles = [band["core" if other in view.get("miter_wall_ids", ()) else "extent"].get(other)
                         for other in candidates]
            obstacles = [o for o in obstacles if o is not None]
            clipped = shape.difference(unary_union(obstacles)) if obstacles else shape
            # Added joined-wall wedges matter at acute/non-orthogonal corners.
            clipped = clipped.difference(band["joined"])
            for polygon in polygons(clipped):
                if result and result[-1][1].equals(polygon) and abs(result[-1][0][1]-low) < 1e-6:
                    result[-1] = ((result[-1][0][0], high), polygon)
                else:
                    result.append(((low, high), polygon))
        return result

    def structures(self, wall_ids=None):
        """Joined, solid walls, with true door/window voids and no panel seams.

        Assign disjoint pieces to walls so hiding a wall is still inexpensive.
        Do not draw internal triangulation/band edges. Adjacent matching
        materials use world coordinates so their texture has no artificial seam.
        """
        ordered = sorted(self.floor.walls, key=lambda w: (w.id not in self.boundary_ids, w.id))
        result, previous = [], {}
        materials = {w.id: self.material(w) for w in ordered}
        levels = sorted(self.levels)
        for low, high in zip(levels, levels[1:]):
            band = self.band((low+high)/2)
            remaining = band["joined"]
            owned = {}
            for wall in ordered:
                if wall.id not in band["core"]:
                    continue
                part = remaining.intersection(band["core"][wall.id])
                owned[wall.id] = part
                remaining = remaining.difference(part)
            for wedge in polygons(remaining):
                # Small endpoint mitres belong to the nearest active wall.
                wall_id = min(owned, key=lambda k: self.cores[k].distance(wedge.representative_point()))
                owned[wall_id] = owned[wall_id].union(wedge)
            for wall in ordered:
                if wall.id not in owned or wall_ids is not None and wall.id not in wall_ids:
                    continue
                material, basis = materials[wall.id]
                label, color = MATERIALS[material]
                for i, polygon in enumerate(polygons(owned[wall.id])):
                    key = (wall.id, polygon.normalize().wkb)
                    earlier = previous.get(key)
                    if earlier is not None and abs(earlier["bottom_mm"]+earlier["height_mm"]-low) < 1e-6:
                        earlier["height_mm"] = high-earlier["bottom_mm"]
                        continue
                    item = {"id": f"base:{wall.id}:{low:g}:{i}", "wall_id": wall.id,
                        "floor_id": self.floor.id, "kind": "substrate", "label": wall.name,
                        "material": material, "material_label": label, "material_basis": basis,
                        "color": color, "bottom_mm": low, "height_mm": high-low,
                        "thickness_mm": wall.thickness_mm, **outline(polygon)}
                    previous[key] = item
                    result.append(item)
        return result
