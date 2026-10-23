"""Lightweight customer presentation derived from canonical panel layouts.

No assembly records, STEP decoding, BRep operations or fastener tessellation.
Millimetres, Z up. Technical assemblies and their exports remain independent.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Annotated, Literal

from pydantic import Field

from .domain.models import Identifier, Model, Project
from .panel_layout import layout_pieces
from .wall_mount import MountConfiguration, mount_scope_targets
from .finish_geometry import FloorGeometry, outline

Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
DEFAULT_COLOR = "#c4ab86"


class Appearance(Model):
    schema_version: Literal["finish-presentation/1.0"] = "finish-presentation/1.0"
    panel_color: Color = DEFAULT_COLOR
    face_colors: dict[Identifier, Color] = Field(default_factory=dict)
    panel_colors: dict[Identifier, Color] = Field(default_factory=dict)
    # Visual finish overrides do not change manufacturing materials or STEP.
    surface: Literal["auto", "veneer", "hpl", "painted"] = "auto"
    face_surfaces: dict[Identifier, Literal["auto", "veneer", "hpl", "painted"]] = Field(default_factory=dict)
    panel_surfaces: dict[Identifier, Literal["auto", "veneer", "hpl", "painted"]] = Field(default_factory=dict)
    align_floors: bool = True


class Scope(Model):
    kind: Literal["building", "floor", "space"]
    id: Identifier


class PreviewRequest(Model):
    project: Project
    scope: Scope | None = None


def appearance(project):
    return Appearance.model_validate(project.properties.get("finish_presentation", {}))


def floor_positions(project, *, align_xy=False):
    """Keep explicit heights. Separate legacy floors sharing one elevation.

    Automatic display offsets are returned explicitly and never written to the
    source. The UI offers applying them to the project with ordinary undo/save.
    Negative levels and distinct user elevations are never normalised away.
    """
    result = []
    for building in project.buildings:
        # Legacy plans may be drawn beside each other in their floor-local
        # coordinates. Align plan origins for presentation only, never rewrite
        # drawing coordinates or engineering/STEP transforms. Explicit switch
        # lets a setback building retain its true horizontal offsets.
        plan_origins = {f.id: [min(getattr(p, axis)+getattr(f.origin, axis)
                           for w in f.walls for p in (w.start, w.end)) for axis in ("x", "y")]
                        for f in building.floors if f.walls}
        reference = next(iter(plan_origins.values()), [0., 0.])
        reserved = {f.elevation_mm for f in building.floors}
        seen = set()
        top = None
        for floor in building.floors:
            height = max([float(floor.default_height_mm), *(w.base_mm+w.height_mm for w in floor.walls)])
            z = float(floor.elevation_mm)
            adjusted = z in seen
            if adjusted:
                z = max(z+height, top if top is not None else z+height)
                while z in reserved:
                    z += height
            reserved.add(z)
            seen.add(floor.elevation_mm)
            top = max(top if top is not None else z+height, z+height)
            result.append({"id": floor.id, "building_id": building.id, "name": floor.name,
                "elevation_mm": z, "source_elevation_mm": float(floor.elevation_mm),
                "height_mm": height, "auto_stacked": adjusted,
                "origin_mm": [floor.origin.x, floor.origin.y],
                "presentation_offset_mm": [reference[i]-plan_origins.get(floor.id, reference)[i]
                                           if align_xy else 0. for i in range(2)]})
    return result


def _clip(poly, normal, offset):
    """Convex half-plane clip; mitered panel ends remain solid prisms."""
    result = []
    for a, b in zip(poly, [*poly[1:], poly[0]]):
        da = sum(x*n for x, n in zip(a, normal))-offset
        db = sum(x*n for x, n in zip(b, normal))-offset
        if da >= -1e-7:
            result.append(a)
        if (da < -1e-7) != (db < -1e-7):
            fraction = da/(da-db)
            result.append([a[i]+fraction*(b[i]-a[i]) for i in range(2)])
    return result


def _miters(views):
    # Partition the overlap between two adjacent finishes at their bisector.
    # Only unambiguous same-room corners qualify; T junctions stay untrimmed.
    ends = defaultdict(list)
    for view in views:
        for p in (view["start"], view["end"]):
            ends[(view["floor_id"], view["space_id"], round(p[0], 4), round(p[1], 4))].append(view)
    for group in ends.values():
        if len(group) != 2 or not group[0]["space_id"]:
            continue
        a, b = group
        if abs(sum(x*y for x, y in zip(a["normal"], b["normal"]))) > .999:
            continue
        for current, other in ((a, b), (b, a)):
            n = [other["normal"][i]-current["normal"][i] for i in range(2)]
            d = (sum(other["normal"][i]*other["start"][i] for i in range(2))+other["front_offset_mm"]
                 -sum(current["normal"][i]*current["start"][i] for i in range(2))-current["front_offset_mm"])
            current["clips"].append((n, d))
            current.setdefault("miter_wall_ids", []).append(other["wall_id"])


def build_finish_presentation(project, scope=None):
    """Compute actual panel thickness/openings and only symbolic fixings."""
    scope = Scope.model_validate(scope) if scope is not None and not isinstance(scope, Scope) else scope
    style = appearance(project)
    floors = floor_positions(project, align_xy=style.align_floors)
    floor_map = {f["id"]: f for f in floors}
    targets = None
    if scope and scope.kind == "space":
        resolved = mount_scope_targets(project, {"kind": "space", "id": scope.id})
        targets = {(t["wall"].id, t["face"].id) for t in resolved["targets"]}
        allowed = {resolved["floor"].id}
    elif scope:
        allowed = {f["id"] for f in floors if (f["building_id"] if scope.kind == "building" else f["id"]) == scope.id}
        if not allowed:
            raise ValueError("Выбранная область 3D-просмотра не найдена или пока не содержит этажей")
    else:
        allowed = set(floor_map)
    walls = {(w.id, face.id): (f, w, face) for b in project.buildings for f in b.floors for w in f.walls for face in w.faces}
    # Validate only the inputs used by this view. Old derived STEP snapshots
    # and unconfirmed node choices cannot block a customer panel presentation.
    raw_mount = project.properties.get("wall_mounting", {})
    if not isinstance(raw_mount, dict) or not isinstance(raw_mount.get("walls", []), list):
        raise ValueError("Некорректная раскладка панелей")
    raw_framing = project.properties.get("whole_wall_settings", {})
    if not isinstance(raw_framing, dict) or not isinstance(raw_framing.get("walls", []), list):
        raise ValueError("Некорректные настройки глубины облицовки")
    framing = raw_framing.get("walls", [])
    if any(not isinstance(item, dict) or not isinstance(item.get("configuration", {}), dict) for item in framing):
        raise ValueError("Некорректные настройки глубины облицовки")
    cavities = {(item["wall_id"], item["face_id"]): item.get("configuration", {}).get("cavity_mm", 80.)
                for item in framing if isinstance(item, dict) and "wall_id" in item and "face_id" in item}
    framing_by_key = {(item["wall_id"], item["face_id"]): item.get("configuration", {})
                      for item in framing if "wall_id" in item and "face_id" in item}
    views, warnings, seen = [], [], set()
    for binding in raw_mount.get("walls", []):
        if not isinstance(binding, dict):
            raise ValueError("Некорректная сторона облицовки")
        key = (binding.get("wall_id"), binding.get("face_id"))
        if key in seen or key not in walls:
            raise ValueError("Сторона облицовки повторяется или не найдена")
        seen.add(key)
        floor, wall, face = walls[key]
        if floor.id not in allowed or targets is not None and key not in targets:
            continue
        config = MountConfiguration.model_validate(binding.get("configuration"))
        length = math.hypot(wall.end.x-wall.start.x, wall.end.y-wall.start.y)
        if length <= 1e-6:
            raise ValueError("Нулевая длина стены в раскладке")
        direction = 1 if face.side == "left" else -1
        tangent = [(wall.end.x-wall.start.x)/length*direction, (wall.end.y-wall.start.y)/length*direction]
        normal = [-tangent[1], tangent[0]]
        start, end = (wall.start, wall.end) if direction == 1 else (wall.end, wall.start)
        shift = floor_map[floor.id]["presentation_offset_mm"]
        start = [start.x+floor.origin.x+shift[0], start.y+floor.origin.y+shift[1]]
        end = [end.x+floor.origin.x+shift[0], end.y+floor.origin.y+shift[1]]
        openings = []
        for o in floor.openings:
            if o.wall_id != wall.id:
                continue
            if o.offset_mm+o.width_mm > length+.001 or o.sill_mm+o.height_mm > wall.height_mm+.001:
                raise ValueError(f"Проём «{o.name}» выходит за границы стены «{wall.name}»")
            openings.append({"id": o.id, "name": o.name, "kind": o.kind,
                "x_mm": round(o.offset_mm if direction == 1 else length-o.offset_mm-o.width_mm, 6),
                "y_mm": round(o.sill_mm, 6), "width_mm": round(o.width_mm, 6), "height_mm": round(o.height_mm, 6)})
        panels, _, _, layout = layout_pieces(length, wall.height_mm, config, openings)
        cavity = cavities.get(key, 80.)
        if isinstance(cavity, bool) or not isinstance(cavity, (int, float)) or not math.isfinite(cavity) or not 0 <= cavity <= 10000:
            raise ValueError("Некорректная глубина облицовки")
        front = wall.thickness_mm/2+cavity+config.panel_thickness_mm
        space = next((s for s in floor.spaces if s.id == face.space_id), None)
        views.append({"id": face.id, "wall_id": wall.id, "face_id": face.id, "floor_id": floor.id,
            "space_id": face.space_id, "name": wall.name, "floor_name": floor.name,
            "space_name": f"{space.code} · {space.name}" if space else "Перегородка / наружная сторона",
            "side": face.side, "start": start, "end": end, "tangent": tangent, "normal": normal,
            "length_mm": length, "height_mm": wall.height_mm, "base_mm": wall.base_mm,
            "elevation_mm": floor_map[floor.id]["elevation_mm"]+wall.base_mm,
            "front_offset_mm": front, "cavity_mm": cavity, "configuration": config.model_dump(mode="json"),
            "color": style.face_colors.get(face.id, style.panel_color).lower(),
            "panel_colors": {p["id"]: style.panel_colors.get(face.id+":"+p["id"],
                               style.face_colors.get(face.id, style.panel_color)).lower() for p in panels},
            "panels": panels, "openings": openings, "layout": layout, "clips": []})
    _miters(views)
    floor_geometry = {f.id: FloorGeometry(f, floor_map[f.id], [v for v in views if v["floor_id"] == f.id],
                                        framing_by_key, warnings)
                      for b in project.buildings for f in b.floors if f.id in allowed}
    structures = [part for geometry in floor_geometry.values()
                  for part in geometry.structures({key[0] for key in targets} if targets is not None else None)]
    solids, schematic, counts, area = [], [], Counter(), 0.
    for index, view in enumerate(views, 1):
        view["mark"] = f"П{index:03d}"
        t, n, origin = view["tangent"], view["normal"], view["start"]
        depth = view["front_offset_mm"]
        thickness = view["configuration"]["panel_thickness_mm"]
        for p in view["panels"]:
            x, y, w, h = (p[k] for k in ("x_mm", "y_mm", "width_mm", "height_mm"))
            footprint = [[origin[i]+t[i]*xx+n[i]*dd for i in range(2)]
                         for xx, dd in ((x, depth), (x+w, depth), (x+w, depth-thickness), (x, depth-thickness))]
            for normal, offset in view["clips"]:
                if footprint:
                    footprint = _clip(footprint, normal, offset)
            if len(footprint) < 3:
                continue
            identity = view["face_id"]+":"+p["id"]
            z = view["elevation_mm"]+y
            parts = floor_geometry[view["floor_id"]].panel_parts(view, footprint, z, h)
            if not parts:
                continue
            # One selectable panel may have several closed fragments after a
            # T junction or an adjacent opening trims its end. Keep its identity.
            meshes = [{**outline(poly), "bottom_mm": low, "height_mm": high-low}
                      for (low, high), poly in parts]
            solids.append({"id": identity, "face_id": view["face_id"], "floor_id": view["floor_id"],
                "wall_id": view["wall_id"], "kind": "panel",
                "label": f"{view['mark']} / панель {p['index']}",
                "color": style.panel_colors.get(identity, view["color"]),
                "material": view["configuration"]["material"],
                "front_normal": n, "tangent": t, "texture_origin_mm": origin+[view["elevation_mm"]],
                "footprint_mm": meshes[0]["footprint_mm"], "bottom_mm": z, "height_mm": h,
                "meshes": meshes, "width_mm": w, "thickness_mm": thickness})
            # Two symbols per visible piece, independent of real screw counts.
            for fraction in (.2, .8):
                a = [origin[i]+t[i]*(x+w*.5)+n[i]*(depth-thickness-2) for i in range(2)]
                schematic.append({"face_id": view["face_id"], "floor_id": view["floor_id"],
                    "points": [[a[i]-t[i]*min(25, w*.15) for i in range(2)]+[z+h*fraction],
                               [a[i]+t[i]*min(25, w*.15) for i in range(2)]+[z+h*fraction]]})
            counts[view["floor_id"]] += 1
            area += p["area_m2"]
        view.pop("clips")
        view.pop("miter_wall_ids", None)
    if any(f["auto_stacked"] for f in floors if f["id"] in allowed):
        warnings.append("Этажи с одинаковой отметкой разнесены по высоте только для просмотра. Можно применить эти высоты к проекту.")
    if any(any(abs(x) > .001 for x in f["presentation_offset_mm"]) for f in floors if f["id"] in allowed):
        warnings.append("Планы этажей совмещены по нижнему левому углу контура только в этом 3D-виде. Исходные координаты не изменены.")
    return {"schema_version": "finish-scene/1.0", "project_id": project.id, "source_revision": project.revision,
        "scope": scope.model_dump() if scope else None, "appearance": style.model_dump(),
        "floors": [{**f, "panel_count": counts[f["id"]]} for f in floors if f["id"] in allowed],
        "views": views, "panels": solids, "structures": structures, "schematic_fixings": schematic,
        "warnings": list(dict.fromkeys(warnings)), "summary": {"wall_count": len(views), "piece_count": len(solids), "area_m2": round(area, 3)},
        "representation": "solid_panels_schematic_fixings", "fixings_are_schematic": True}
