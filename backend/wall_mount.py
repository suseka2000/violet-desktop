"""Evidence-bound wall finish previews. Geometry is not an installation approval.

Coordinates are elevation-local millimetres: X follows the selected face's view,
Y is up from the wall bottom. Layout pieces are not fabrication blanks. Manual
rules are immutable snapshots; filenames only suggest STEP components.
"""
from __future__ import annotations

import math
import re
import unicodedata
from copy import deepcopy
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_serializer, model_validator

from .domain.models import Identifier, Model, Project
from .vendor_design import _context_hash, _source, is_assembly_rule
from .vendor_library import Digest, ReferenceSnapshot

Dimension = Annotated[float, Field(strict=True, allow_inf_nan=False, gt=0, le=100_000)]
Gap = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0, le=10_000)]


class MountConfiguration(Model):
    installation_zone: Literal["interior", "exterior"] = "interior"
    material: Literal["veneer", "hpl", "painted", "unspecified"] = "unspecified"
    panel_thickness_mm: Annotated[Dimension, Field(le=200)] = 12
    panel_width_mm: Dimension = 600
    panel_height_mm: Dimension = 3000
    joint_mm: Annotated[Gap, Field(le=100)] = 2
    mounting_system: Literal["open_mount", "concealed_mount"] = "open_mount"
    assembly_rule_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    node_rule_ids: dict[Literal["joints", "floor", "ceiling", "corners", "openings"], Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=dict, max_length=5)
    panel_edge: Literal["V-0", "Va", "Vb", "Vc", "Vd"] | None = None
    floor_gap_mm: Gap = 0
    ceiling_gap_mm: Gap = 0
    allow_project_assumptions: bool = Field(default=False, strict=True)

    @field_validator("node_rule_ids", mode="before")
    @classmethod
    def normalize_node_rules(cls, value):
        if value is None:
            return {}
        if isinstance(value, dict):
            allowed = {"joints", "floor", "ceiling", "corners", "openings"}
            if set(value) - allowed:
                raise ValueError("Неизвестная группа монтажных узлов")
            return {key: item for key, item in value.items() if item is not None and item != ""}
        return value

    @model_serializer(mode="wrap")
    def serialize_configuration(self, handler):
        result = handler(self)
        # Historical plans hash the complete configuration. An absent override
        # must remain byte-for-byte equivalent, including nested serialization.
        if not result.get("node_rule_ids"):
            result.pop("node_rule_ids", None)
        if result.get("installation_zone") == "interior":
            result.pop("installation_zone", None)
        return result


class WallMountBinding(Model):
    wall_id: Identifier
    face_id: Identifier
    configuration: MountConfiguration
    geometry_signature: Digest | None = None


class WallMounting(Model):
    schema_version: Literal["wall-mounting/1.0"] = "wall-mounting/1.0"
    snapshot: ReferenceSnapshot
    walls: list[WallMountBinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_faces(self):
        if len({(w.wall_id, w.face_id) for w in self.walls}) != len(self.walls):
            raise ValueError("Для стороны стены допускается одна настройка монтажа")
        return self


class WallMountRequest(Model):
    project: Project
    wall_id: Identifier
    face_id: Identifier | None = None
    configuration: MountConfiguration = Field(default_factory=MountConfiguration)


class MountScope(Model):
    kind: Literal["space", "floor", "exterior_floor", "exterior_building"]
    id: Identifier


class BatchMountRequest(Model):
    project: Project
    scope: MountScope
    configuration: MountConfiguration = Field(default_factory=MountConfiguration)
    replace_existing: bool = Field(default=False, strict=True)


def find_wall(project, wall_id, face_id=None):
    found = [(f, w) for b in project.buildings for f in b.floors for w in f.walls if w.id == wall_id]
    if len(found) != 1:
        raise ValueError("Стена не найдена или её идентификатор повторяется")
    floor, wall = found[0]
    face = next((f for f in wall.faces if f.id == face_id), None) if face_id else next((f for f in wall.faces if f.space_id), wall.faces[0])
    if face is None:
        raise ValueError("Выбранная сторона не принадлежит стене")
    return floor, wall, face


def validate_wall_mounting(project):
    value = project.properties.get("wall_mounting")
    if value is None:
        return None
    saved = WallMounting.model_validate(value)
    rules = {r.id: r for r in saved.snapshot.rules}
    for binding in saved.walls:
        find_wall(project, binding.wall_id, binding.face_id)
        chosen = binding.configuration.assembly_rule_id
        if chosen and (chosen not in rules or not is_assembly_rule(rules[chosen])):
            raise ValueError("Сборка монтажа отсутствует в зафиксированной методичке")
        from .mount_node_groups import validate_group_rule_ids
        validate_group_rule_ids(binding.configuration.node_rule_ids, saved.snapshot)
    return saved


def wall_signature(project, floor, wall, face):
    # Neighbours change edge/corner context. IDs are deliberately included: a
    # clone or changed wall requires explicit rechecking, never implicit trust.
    return _context_hash({"floor_id": floor.id, "origin": floor.origin.model_dump(mode="json"),
                          "elevation_mm": floor.elevation_mm, "face_id": face.id,
                          "geometry_tolerance_mm":project.settings.geometry_tolerance_mm,
                          "walls": [w.model_dump(mode="json", exclude={"name", "properties"}) for w in floor.walls],
                          "openings": [o.model_dump(mode="json", exclude={"name", "properties"}) for o in floor.openings],
                          "spaces": [s.model_dump(mode="json", exclude={"name", "properties"}) for s in floor.spaces]})


def _issue(code, message, severity="warning"):
    return {"code": code, "message": message, "severity": severity,
            "path": "/properties/wall_mounting", "entity_ids": [], "details": {}}


def _rect(x, y, w, h):
    return {"x_mm": round(x, 6), "y_mm": round(y, 6), "width_mm": round(w, 6), "height_mm": round(h, 6)}


def _point(x, y):
    return {"x_mm": round(x, 6), "y_mm": round(y, 6)}


def _local_panel_corner(project, floor, wall, face, point, opposite, neighbours):
    """Identify one mounted panel pair at a T junction, not the whole junction.

    Room winding cannot classify a standalone partition. The selected face's
    normal does, however, identify its inside quarter-plane. Exactly one
    perpendicular neighbour ray and its inward-facing mounted face must occupy
    that quarter-plane. A third collinear branch is not treated as a panel.
    """
    if len(neighbours) < 2 and not any(n.get("contact_kind") == "wall_interior" for n in neighbours):
        return None, []
    tolerance = float(project.settings.geometry_tolerance_mm)
    ux, uy = opposite.x-point.x, opposite.y-point.y
    length = math.hypot(ux, uy)
    if length <= tolerance:
        return None, []
    ux, uy = ux/length, uy/length
    dx, dy = wall.end.x-wall.start.x, wall.end.y-wall.start.y
    side = 1 if face.side == "left" else -1
    nx, ny = -dy/length*side, dx/length*side
    candidates = []
    neighbour_ids = {n["wall_id"] for n in neighbours}
    for adjacent in floor.walls:
        if adjacent.id not in neighbour_ids:
            continue
        vx, vy = adjacent.end.x-adjacent.start.x, adjacent.end.y-adjacent.start.y
        adjacent_length = math.hypot(vx, vy)
        if adjacent_length <= tolerance:
            continue
        vx, vy = vx/adjacent_length, vy/adjacent_length
        # At an unsplit wall's middle both rays exist; only the ray facing the
        # selected partition side belongs to this local panel pair.
        rays = []
        if math.hypot(adjacent.start.x-point.x, adjacent.start.y-point.y) <= tolerance:
            rays.append((vx, vy))
        elif math.hypot(adjacent.end.x-point.x, adjacent.end.y-point.y) <= tolerance:
            rays.append((-vx, -vy))
        else:
            rays.extend(((vx, vy), (-vx, -vy)))
        for rx, ry in rays:
            angle = math.degrees(math.acos(max(-1, min(1, ux*rx+uy*ry))))
            if nx*rx+ny*ry <= 1e-10:
                continue
            for adjacent_face in adjacent.faces:
                sign = 1 if adjacent_face.side == "left" else -1
                if (-vy*sign)*ux+(vx*sign)*uy > 0:
                    candidates.append((adjacent, adjacent_face, angle))
    if not candidates:
        return None, []
    if len(candidates) != 1:
        return {"kind": "ambiguous", "basis": "mounted_face_normals", "right_angle": False}, []
    adjacent, adjacent_face, angle = candidates[0]
    junction = {"kind": "internal_corner", "angle_deg": angle,
                "right_angle": math.isclose(angle, 90., abs_tol=1e-6),
                "basis": "mounted_face_normals", "current_face_id": face.id,
                "adjacent_wall_id": adjacent.id, "adjacent_face_id": adjacent_face.id}
    raw = project.properties.get("wall_mounting", {})
    bindings = raw.get("walls", []) if isinstance(raw, dict) else []
    matches = [b for b in bindings if isinstance(b, dict) and
               (b.get("wall_id"), b.get("face_id")) == (adjacent.id, adjacent_face.id)]
    if len(matches) != 1 or matches[0].get("geometry_signature") != wall_signature(project, floor, adjacent, adjacent_face):
        return junction, []
    try:
        config = MountConfiguration.model_validate(matches[0].get("configuration", {})).model_dump(mode="json")
    except ValueError:
        return junction, []
    mounting = {"wall_id": adjacent.id, "face_id": adjacent_face.id, "configuration": config,
                "wall_base_mm": adjacent.base_mm, "wall_height_mm": adjacent.height_mm,
                "geometry_signature_verified": True}
    return junction, [mounting]


def _local_panel_continuation(project, floor, wall, face, point, opposite, neighbours):
    """Resolve the uninterrupted back of a T as a same-facing straight pair.

    This also covers collinear standalone partitions without a room boundary.
    The perpendicular branch must not occupy the selected face's half-plane;
    its inside quarter is handled by _local_panel_corner before this resolver.
    A matching centreline alone is insufficient: the substrate face planes,
    direction and uniquely mounted neighbouring face must agree as well.
    """
    tolerance = float(project.settings.geometry_tolerance_mm)
    ux, uy = opposite.x-point.x, opposite.y-point.y
    length = math.hypot(ux, uy)
    if length <= tolerance:
        return None, []
    ux, uy = ux/length, uy/length
    dx, dy = wall.end.x-wall.start.x, wall.end.y-wall.start.y
    sign = 1 if face.side == "left" else -1
    nx, ny = -dy/length*sign, dx/length*sign
    neighbour_ids = {n["wall_id"] for n in neighbours if n.get("contact_kind") != "wall_interior"}
    candidates = []
    for adjacent in floor.walls:
        if adjacent.id not in neighbour_ids:
            continue
        vx, vy = adjacent.end.x-adjacent.start.x, adjacent.end.y-adjacent.start.y
        adjacent_length = math.hypot(vx, vy)
        if adjacent_length <= tolerance:
            continue
        vx, vy = vx/adjacent_length, vy/adjacent_length
        if math.hypot(adjacent.start.x-point.x, adjacent.start.y-point.y) <= tolerance:
            rx, ry = vx, vy
        elif math.hypot(adjacent.end.x-point.x, adjacent.end.y-point.y) <= tolerance:
            rx, ry = -vx, -vy
        else:
            continue
        # Collinearity is not rounded to the project's endpoint tolerance.
        if not math.isclose(ux*rx+uy*ry, -1., abs_tol=1e-12) or abs(ux*ry-uy*rx) > 1e-8:
            continue
        for adjacent_face in adjacent.faces:
            other_sign = 1 if adjacent_face.side == "left" else -1
            if not math.isclose(nx*(-vy*other_sign)+ny*(vx*other_sign), 1., abs_tol=1e-12):
                continue
            candidates.append((adjacent, adjacent_face))
    if not candidates:
        return None, []
    if len(candidates) != 1:
        return {"kind": "ambiguous", "basis": "mounted_face_normals"}, []
    adjacent, adjacent_face = candidates[0]
    continuation = {"kind": "straight", "angle_deg": 180., "basis": "mounted_face_normals",
                    "current_face_id": face.id, "adjacent_wall_id": adjacent.id,
                    "adjacent_face_id": adjacent_face.id,
                    "substrate_offset_delta_mm": (adjacent.thickness_mm-wall.thickness_mm)/2}
    raw = project.properties.get("wall_mounting", {})
    bindings = raw.get("walls", []) if isinstance(raw, dict) else []
    matches = [b for b in bindings if isinstance(b, dict) and
               (b.get("wall_id"), b.get("face_id")) == (adjacent.id, adjacent_face.id)]
    if len(matches) != 1 or matches[0].get("geometry_signature") != wall_signature(project, floor, adjacent, adjacent_face):
        return continuation, []
    try:
        config = MountConfiguration.model_validate(matches[0].get("configuration", {})).model_dump(mode="json")
    except ValueError:
        return continuation, []
    return continuation, [{"wall_id": adjacent.id, "face_id": adjacent_face.id, "configuration": config,
                           "wall_base_mm": adjacent.base_mm, "wall_height_mm": adjacent.height_mm,
                           "geometry_signature_verified": True}]


def _edge_contexts(project, floor, wall, face, *, exterior=False):
    """Detect coincident endpoints, not a guessed inside/outside corner.

    An adjacent wall may have no room assignment, a T junction or several
    neighbours. A selected face alone is not enough to approve corner geometry.
    """
    from .mount_defaults import classified_corner, room_geometry
    from .geometry.validation import _point_segment_distance
    tolerance = float(project.settings.geometry_tolerance_mm)
    room = None
    if face.space_id:
        space = next((s for s in floor.spaces if s.id==face.space_id),None)
        if space is not None:
            try:
                room=room_geometry(floor,space,tolerance)
                if not any(w.id==wall.id and f.id==face.id for w,f in zip(room["walls"],room["faces"])):
                    room=None
            except ValueError:
                pass  # Standalone wall preview remains available, corners unresolved.
    ends = [("left_edge", "start", wall.start, wall.end), ("right_edge", "end", wall.end, wall.start)]
    if face.side == "right":
        ends = [("left_edge", "end", wall.end, wall.start), ("right_edge", "start", wall.start, wall.end)]
    result = []
    for kind, physical_end, point, opposite in ends:
        neighbours = []
        ux, uy = opposite.x-point.x, opposite.y-point.y
        for adjacent in sorted(floor.walls, key=lambda w:w.id):
            if adjacent.id == wall.id:
                continue
            endpoint_hit=False
            for at, other in ((adjacent.start,adjacent.end),(adjacent.end,adjacent.start)):
                if math.hypot(at.x-point.x,at.y-point.y) > tolerance:
                    continue
                vx,vy = other.x-at.x,other.y-at.y
                denominator = math.hypot(ux,uy)*math.hypot(vx,vy)
                angle = math.degrees(math.acos(max(-1,min(1,(ux*vx+uy*vy)/denominator)))) if denominator > 1e-9 else None
                neighbours.append({"wall_id":adjacent.id,"angle_deg":round(angle,6) if angle is not None else None})
                endpoint_hit=True
            if not endpoint_hit:
                # Imported or not-yet-split T junction: touching a wall's middle
                # is not a free end, and does not define an inside/outside corner.
                a,b=(adjacent.start.x,adjacent.start.y),(adjacent.end.x,adjacent.end.y)
                vx,vy=b[0]-a[0],b[1]-a[1];length=math.hypot(vx,vy)
                if length>tolerance and _point_segment_distance((point.x,point.y),a,b)<=tolerance:
                    position=((point.x-a[0])*vx+(point.y-a[1])*vy)/length
                    if tolerance<position<length-tolerance:
                        denominator=math.hypot(ux,uy)*length
                        angle=math.degrees(math.acos(max(-1,min(1,abs(ux*vx+uy*vy)/denominator)))) if denominator>1e-9 else None
                        neighbours.append({"wall_id":adjacent.id,"angle_deg":round(angle,6) if angle is not None else None,"contact_kind":"wall_interior"})
        corner=classified_corner(room,point,tolerance) if room else None
        exterior_adjacent=[]
        if room is None and exterior:
            from .exterior_mount import exterior_corner_context
            corner,exterior_adjacent=exterior_corner_context(floor,wall,face,point,tolerance)
        adjacent_mounting=[]
        if corner:
            raw_mounting=project.properties.get("wall_mounting")
            raw_bindings=raw_mounting.get("walls",[]) if isinstance(raw_mounting,dict) else []
            by_binding={(b.get("wall_id"),b.get("face_id")):b for b in raw_bindings if isinstance(b,dict)}
            pair_context=zip(room["walls"],room["faces"]) if room else [(item["wall"],item["face"]) for item in exterior_adjacent]
            for adjacent,adjacent_face in pair_context:
                if adjacent.id==wall.id or adjacent.id not in corner["boundary_wall_ids"]:
                    continue
                binding=by_binding.get((adjacent.id,adjacent_face.id))
                if binding:
                    try:
                        config=MountConfiguration.model_validate(binding.get("configuration",{})).model_dump(mode="json")
                    except ValueError:
                        config=None
                    adjacent_mounting.append({"wall_id":adjacent.id,"face_id":adjacent_face.id,"configuration":config,
                                              "wall_base_mm":adjacent.base_mm,"wall_height_mm":adjacent.height_mm})
        local_junction = local_continuation = None
        if not corner or corner.get("corner_type") == "straight":
            local_junction, local_mounting = _local_panel_corner(project, floor, wall, face, point, opposite, neighbours)
            if local_junction:
                adjacent_mounting = local_mounting
            else:
                local_continuation, local_mounting = _local_panel_continuation(project, floor, wall, face, point, opposite, neighbours)
                if local_continuation:
                    adjacent_mounting = local_mounting
                    if local_continuation.get("kind") == "straight":
                        corner = {**(corner or {}), "corner_type": "straight", "interior_angle_deg": 180.,
                                  "right_angle": False, "classification_basis": "mounted_face_normals"}
        adjacent_voids = []
        if corner or local_junction or local_continuation:
            pair = local_junction or local_continuation
            relevant_walls = [pair.get("adjacent_wall_id")] if pair else corner["boundary_wall_ids"]
            for adjacent in floor.walls:
                if adjacent.id == wall.id or adjacent.id not in relevant_walls:
                    continue
                dx, dy = adjacent.end.x-adjacent.start.x, adjacent.end.y-adjacent.start.y
                length = math.hypot(dx, dy)
                endpoint = ((point.x-adjacent.start.x)*dx+(point.y-adjacent.start.y)*dy)/length if length else 0
                for opening in floor.openings:
                    if opening.wall_id == adjacent.id and opening.offset_mm-tolerance <= endpoint <= opening.offset_mm+opening.width_mm+tolerance:
                        bottom = float(adjacent.base_mm + opening.sill_mm - wall.base_mm)
                        start_distance = abs(endpoint-opening.offset_mm)
                        end_distance = abs(endpoint-opening.offset_mm-opening.width_mm)
                        jamb_distance = min(start_distance, end_distance)
                        adjacent_voids.append({"opening_id":opening.id,"wall_id":adjacent.id,
                                               "bottom_mm":bottom,"top_mm":bottom+opening.height_mm,
                                               "opening_kind":opening.kind,"width_mm":opening.width_mm,
                                               "jamb_side":("start" if start_distance <= end_distance else "end") if jamb_distance <= 1e-6 else None,
                                               "distance_to_jamb_mm":jamb_distance})
        result.append({"kind":kind,"wall_endpoint":physical_end,"world_point":point.model_dump(mode="json"),
                       "connection":"adjacent_walls" if neighbours else "free_end",
                       "corner_type":"unresolved" if neighbours else None,"adjacent_walls":neighbours,"adjacent_mounting":adjacent_mounting,
                       **({"adjacent_voids":adjacent_voids} if adjacent_voids else {}),
                       **({"local_panel_junction":local_junction} if local_junction else {}),
                       **({"local_panel_continuation":local_continuation} if local_continuation else {}),**(corner or {})})
    return result


@lru_cache(maxsize=256)
def _catalogue_filename_pattern(code):
    token = re.sub(r"[-\s]", "", code)
    return re.compile(r"(?<![A-Za-z0-9])" + r"[-\s]*".join(re.escape(c) for c in token) + r"$", re.I)


@lru_cache(maxsize=16384)
def _catalogue_filename_matches(code, filename):
    # Cache only a pure string predicate. Asset IDs, aliases, provenance and
    # decisions remain fresh for each caller; a renamed/replaced asset cannot
    # inherit another asset's identity or become approved through this cache.
    stem = re.sub(r"\.(?:step|stp)$", "", filename, flags=re.I)
    stem = re.sub(r"_\d+$", "", stem)
    stem = re.sub(r"\([^()]*\)$", "", stem).rstrip()
    return bool(_catalogue_filename_pattern(code).search(unicodedata.normalize("NFC", stem)))


def _filename_matches(code, assets):
    # Match a full terminal catalogue token; V-Base light is NOT V-Base, and
    # V-Start-1 is NOT V-Start. Even exact spelling remains only a suggestion.
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 -]+", code):
        return []
    matches = []
    for asset in assets:
        aliases = asset.get("aliases") or [{"filename": asset["filename"]}]
        for alias in aliases:
            if _catalogue_filename_matches(code, alias["filename"]):
                matches.append({"source_id": asset.get("source_id") or asset.get("asset_id") or asset.get("id"),
                                "filename": alias["filename"], "component_code": code,
                                "status": "filename_candidate", "confirmed": False})
                break
    return matches


def pinned_step_assets(snapshot):
    """Use immutable catalogue entries, not a mutable live directory listing."""
    snapshot = snapshot if isinstance(snapshot,ReferenceSnapshot) else ReferenceSnapshot.model_validate(snapshot)
    grouped={}
    for asset in snapshot.assets:
        if asset.format not in {"step","stp"}:
            continue
        item=grouped.setdefault(asset.sha256,{"source_id":asset.sha256,"filename":asset.filename,"aliases":[]})
        alias={"filename":asset.filename,"relative_path":asset.relative_path}
        if alias not in item["aliases"]:
            item["aliases"].append(alias)
    return [grouped[key] for key in sorted(grouped)]


def _candidates(config, snapshot, assets):
    documents = {d.id: d for d in snapshot.documents}
    assemblies = sorted((r for r in snapshot.rules if is_assembly_rule(r)), key=lambda r: (r.source.document_id, r.source.page, r.id))
    codes = {code for rule in assemblies for code in rule.component_codes}
    if len(snapshot.rules) > 500 or len(assemblies) > 64 or len(codes) > 256 or len(assets) > 1000:
        raise ValueError("Снимок превышает предел подбора: 500 правил, 64 сборки, 256 обозначений и 1000 STEP-моделей")
    groups = {}
    for rule in snapshot.rules:
        key = (rule.assembly_id,rule.system,rule.source.document_id)
        groups.setdefault(key,[]).append(rule)
    if any(len(groups[(r.assembly_id,r.system,r.source.document_id)]) > 50 for r in assemblies):
        raise ValueError("Одна монтажная сборка содержит больше 50 условий; разделите набор правил")
    matches_by_code = {code:_filename_matches(code,assets) for code in codes}
    result = []
    for rule in assemblies:
        related = groups[(rule.assembly_id,rule.system,rule.source.document_id)]
        checks = []
        checks.append({"parameter": "mounting_system", "status": "match" if rule.system == config.mounting_system else "mismatch",
                       "message": "Система монтажа", "actual": config.mounting_system, "expected": rule.system})
        # Straight wall preview cannot invent a curved substrate or apply a
        # corner drawing to every seam. Those assemblies remain discoverable.
        flat = "flat_wall" in rule.contexts
        special_context = bool(set(rule.contexts).intersection({"outer_radius", "inner_radius", "removable_panel", "internal_corner", "external_corner"}))
        unknown_context = rule.id == "martynova-assembly" or not special_context
        checks.append({"parameter": "wall_context", "status": "match" if flat else "unknown" if unknown_context else "mismatch",
                       "message": "Плоская стена" if flat else "Нужно подтвердить основание и последовательность специальной сборки" if unknown_context else "Эта сборка относится к углу, радиусу или съёмной панели, не к обычному полю плоской стены"})
        if rule.source_status != "verified":
            checks.append({"parameter": "source", "status": "unknown", "message": "Первоисточник страницы не подтверждён"})
        joint = [r for r in related if r.kind == "numeric" and r.parameter == "panel_joint_width_mm" and r.unit == "mm"]
        for r in joint:
            low = r.value if r.value is not None else r.value_min
            high = r.value if r.value is not None else r.value_max
            checks.append({"parameter": "joint_mm", "actual_mm": config.joint_mm, "minimum_mm": low, "maximum_mm": high,
                           "status": "unknown" if r.source_status != "verified" else "match" if low - 1e-6 <= config.joint_mm <= high + 1e-6 else "mismatch",
                           "message": r.requirement, "source": _source(r, documents), "rule_id": r.id})
        if not joint:
            checks.append({"parameter": "joint_mm", "status": "unknown", "message": "В этой сборке размер шва не задан в проверенной выписке"})
        if config.panel_edge == "V-0":
            edge = next((r for r in snapshot.rules if r.id == "open-v0-thickness"), None)
            if rule.system != "open_mount":
                checks.append({"parameter": "panel_edge", "status": "mismatch", "message": "Кромка V-0 относится к открытому монтажу"})
            elif edge and edge.source_status == "verified":
                checks.append({"parameter": "panel_thickness_mm", "actual_mm": config.panel_thickness_mm,
                               "minimum_mm": edge.value_min, "maximum_mm": edge.value_max,
                               "status": "match" if edge.value_min <= config.panel_thickness_mm <= edge.value_max else "mismatch",
                               "message": edge.requirement, "source": _source(edge, documents), "rule_id": edge.id})
            else:
                checks.append({"parameter":"panel_thickness_mm","status":"unknown","message":"Нет подтверждённой выписки для толщины панели V-0"})
        elif config.panel_edge in {"Va", "Vb", "Vc", "Vd"} and rule.system != "concealed_mount":
            checks.append({"parameter": "panel_edge", "status": "mismatch", "message": "Выбранный паз относится к скрытому монтажу"})
        statuses = {c["status"] for c in checks}
        status = "incompatible" if "mismatch" in statuses else "unknown" if "unknown" in statuses else "candidate"
        result.append({"rule_id": rule.id, "title": rule.title, "assembly_id": rule.assembly_id, "system": rule.system,
                       "status": status, "checks": checks, "source": _source(rule, documents),
                       "component_codes": rule.component_codes, "contexts": rule.contexts,
                       "step_matches": [m for code in rule.component_codes for m in matches_by_code[code][:20]],
                       "step_matches_omitted":sum(max(0,len(matches_by_code[code])-20) for code in rule.component_codes),
                       "requirements": [{"rule_id": r.id, "title": r.title, "requirement": r.requirement, "scope": r.scope,
                                         "source": _source(r, documents)} for r in related],
                       "approval_status": "unreviewed"})
    return result


def preview_wall_mount(project, wall_id, face_id, configuration, snapshot, assets=None, *, terminal_infill_mm=0.):
    from .engineering import _subtract_rectangles
    from .engineering.openings import opening_details, visible_opening_edges
    from .mount_defaults import POLICY, node_decisions, select_assembly
    config = configuration if isinstance(configuration, MountConfiguration) else MountConfiguration.model_validate(configuration)
    snapshot = snapshot if isinstance(snapshot, ReferenceSnapshot) else ReferenceSnapshot.model_validate(snapshot)
    floor, wall, face = find_wall(project, wall_id, face_id)
    length = math.hypot(wall.end.x-wall.start.x, wall.end.y-wall.start.y)
    height = float(wall.height_mm)
    space_height = next((s.height_mm for s in floor.spaces if s.id == face.space_id), None)
    height_basis = "room_owner" if space_height is not None else "unknown"
    ceiling_space_id = face.space_id if space_height is not None else None
    if face.space_id is None and not any(edge.wall_id == wall.id for space in floor.spaces for edge in space.boundary):
        # Batch mounting includes an interior partition without assigning it
        # to a room. Recover only its vertical context from the same complete-
        # segment containment test, not from an arbitrary nearest room/height.
        from .mount_defaults import room_geometry, wall_room_relation
        containing = []
        tolerance = float(project.settings.geometry_tolerance_mm)
        for space in floor.spaces:
            try:
                geometry = room_geometry(floor, space, tolerance)
            except ValueError:
                continue
            if wall_room_relation(wall, geometry, tolerance) == "inside":
                containing.append(space)
        if len(containing) == 1:
            space_height = containing[0].height_mm
            ceiling_space_id = containing[0].id
            height_basis = "containing_room"
        elif containing:
            height_basis = "ambiguous_rooms"
    if length <= 1e-6:
        raise ValueError("Нельзя разложить панели на стене нулевой длины")
    usable_height = height - config.floor_gap_mm - config.ceiling_gap_mm
    if usable_height <= 0:
        raise ValueError("Зазоры у пола и потолка не оставляют места для панелей")
    # Validate arithmetic, not a product quota. Positive subnormal input may
    # overflow even though the individual JSON coordinates are finite.
    ratios=(length / (config.panel_width_mm + config.joint_mm),usable_height / (config.panel_height_mm + config.joint_mm))
    if not all(math.isfinite(value) for value in ratios):
        raise ValueError("Размер панели не представим с точностью расчёта; проверьте единицы размеров")
    columns,rows=(math.ceil(value) for value in ratios)
    openings = []
    for o in floor.openings:
        if o.wall_id != wall.id:
            continue
        if o.offset_mm + o.width_mm > length + .001 or o.sill_mm + o.height_mm > height + .001:
            raise ValueError("Проём выходит за границы выбранной стены; исправьте его размеры")
        x = length - o.offset_mm - o.width_mm if face.side == "right" else o.offset_mm
        openings.append({"id": o.id, "kind": o.kind, "name": o.name, **_rect(x, o.sill_mm, o.width_mm, o.height_mm),
                         **opening_details(o, face.side)})
    from .panel_layout import concealed_layout
    column_spans, row_spans, layout = concealed_layout(length, usable_height, config.floor_gap_mm, config, openings,
                                                    terminal_infill_mm=terminal_infill_mm)
    columns, rows = len(column_spans), len(row_spans)
    cutters = [(o["x_mm"], o["y_mm"], o["width_mm"], o["height_mm"]) for o in openings]
    panels, joint_lines = [], []
    for row, (y, h) in enumerate(row_spans):
        for col, (x, w) in enumerate(column_spans):
            if min(w, h) <= 1e-6:
                continue
            cell_id = f"panel-{row+1}-{col+1}"
            pieces = _subtract_rectangles((x, y, w, h), cutters, 1e-7)
            for part, (px, py, pw, ph) in enumerate(pieces, 1):
                # Output dimensions are stored to six decimal places. A
                # floating-point sliver left at an open wall boundary must
                # not become a zero-width panel after that rounding.
                if min(round(pw, 6), round(ph, 6)) <= 0:
                    continue
                panels.append({"id": f"{cell_id}-{part}", "panel_id": cell_id, "index": len(panels)+1,
                               **_rect(px, py, pw, ph), "polygon": [[px, py], [px+pw, py], [px+pw, py+ph], [px, py+ph]],
                               "holes": [], "area_m2": round(pw*ph/1e6, 9), "is_cut": pw != config.panel_width_mm or ph != config.panel_height_mm,
                               "thickness_mm": config.panel_thickness_mm})
    # Seams are elevation guides, not fastener axes or quantities. Clip them at
    # every opening, including overlaps, so no mounting line crosses a void.
    def segments(kind, a, b):
        vertical = a[0] == b[0]
        low, high = (a[1], b[1]) if vertical else (a[0], b[0])
        blocked = []
        for x, y, w, h in cutters:
            # Openings are serialized to six decimal places while this wall
            # endpoint can retain its full diagonal/corner-fit precision.
            # A sub-micrometre rounding difference is not a real jamb: do not
            # place a perimeter profile and screws across an open boundary.
            # Keep this at output precision, not the much larger CAD snap
            # tolerance, and never turn a zero-area cutter into a void.
            if w <= 0 or h <= 0:
                continue
            if (x-1e-6 <= a[0] <= x+w+1e-6) if vertical else (y-1e-6 <= a[1] <= y+h+1e-6):
                blocked.append((y, y+h) if vertical else (x, x+w))
        cursor = low
        for lo, hi in sorted(blocked):
            if hi <= cursor or lo >= high:
                continue
            if lo > cursor:
                yield kind, (a[0], cursor) if vertical else (cursor, a[1]), (a[0], min(lo, high)) if vertical else (min(lo, high), a[1])
            cursor = max(cursor, hi)
        if cursor < high:
            yield kind, (a[0], cursor) if vertical else (cursor, a[1]), b
    for c in range(1, columns):
        x = column_spans[c][0] - config.joint_mm/2
        if x < length:
            joint_lines.extend({"kind": k, "from": _point(*a), "to": _point(*b)} for k,a,b in segments("vertical_joint", (x, config.floor_gap_mm), (x, height-config.ceiling_gap_mm)))
    for r in range(1, rows):
        y = row_spans[r][0] - config.joint_mm/2
        joint_lines.extend({"kind": k, "from": _point(*a), "to": _point(*b)} for k,a,b in segments("horizontal_joint", (0,y), (length,y)))
    assets = assets if assets is not None else pinned_step_assets(snapshot)
    effective=config.model_copy(deep=True)
    if config.allow_project_assumptions and not config.panel_edge and config.mounting_system=="open_mount":
        effective.panel_edge="V-0"
    candidates = _candidates(effective, snapshot, assets)
    chosen,selection_status,selection_reason = select_assembly(config,candidates)
    issues = [_issue("MOUNTING_REVIEW_REQUIRED", "Схема предварительная. Основание, крепёж, нагрузки и применимость отделки не подтверждены; автоматического допуска к монтажу нет.", "error")]
    if config.installation_zone == "exterior":
        issues.append(_issue("MOUNTING_EXTERIOR_APPLICABILITY", "Наружная облицовка. Наружная применимость выбранной системы не подтверждена: уточнить влагостойкость, коррозионную защиту и ветровую нагрузку."))
    if layout["adjusted_axes"]:
        issues.append(_issue("MOUNTING_LAYOUT_ADJUSTED",
            "Швы смещены и крайние доборы перераспределены, чтобы возле проёмов и краёв не оставались узкие полосы. "
            f"Целевой добор при переразбивке — от {layout['minimum_infill_mm']:g} мм; номинальный формат "
            f"{config.panel_width_mm:g}×{config.panel_height_mm:g} мм сохранён как максимальный. "
            "Фактические размеры одинаковы на развёртке, в спецификации и в 3D; размеры стены и проёмов не менялись.", "info"))
    if layout.get("unresolved_axes"):
        issues.append(_issue("MOUNTING_LAYOUT_NO_SAFE_PARTITION",
            "Не удалось перераспределить все узкие доборы без превышения выбранного формата. "
            "Исходная геометрия сохранена; проверка пазов и профилей определит возможность 3D-сборки."))
    issues.append(_issue("MOUNTING_MATERIAL_UNKNOWN", "Методички не задают соответствие «шпон / HPL / окраска - сборка». Материал записан, но его применимость не подтверждена."))
    issues.append(_issue("MOUNTING_EDGE_ALLOWANCES_UNREVIEWED", "Зазоры у пола/потолка заданы пользователем. Отметка полки плинтуса из методички не приравнивается к зазору панели; примыкания и крайние подрезки требуют проверки."))
    if not config.panel_edge:
        issues.append(_issue("MOUNTING_EDGE_ASSUMED" if effective.panel_edge else "MOUNTING_EDGE_UNKNOWN",
                             "Кромка V-0 принята предварительно; толщина сверена с её выпиской, но сама кромка требует подтверждения." if effective.panel_edge else
                             "Кромка/паз панели не выбраны; толщина не проверена по конкретному сечению панели."))
    elif config.panel_edge != "V-0":
        issues.append(_issue("MOUNTING_THICKNESS_UNKNOWN", "Для скрытого монтажа общая допустимая толщина панели в методичке не задана; размеры паза требуют отдельной проверки."))
    if not chosen:
        issues.append(_issue("MOUNTING_ASSEMBLY_CHOICE_REQUIRED", "Нет единственного варианта: выберите подходящую сборку методички. Непроверенные значения не считаются совпадением."))
    elif chosen["status"] != "candidate":
        issues.append(_issue("MOUNTING_ASSEMBLY_INCOMPATIBLE" if chosen["status"] == "incompatible" else "MOUNTING_ASSEMBLY_UNKNOWN", "Выбранная сборка не проходит все известные проверки. См. условия её применимости.", "error" if chosen["status"] == "incompatible" else "warning"))
    if rows > 1:
        issues.append(_issue("MOUNTING_HORIZONTAL_JOINT_UNKNOWN", "Панели разделены по высоте. Деталь горизонтального стыка не определена альбомом вертикальных панелей."))
    if openings:
        issues.append(_issue("MOUNTING_OPENINGS_UNKNOWN", "Проёмы вычтены из панелей. Применимость решений откосов и перемычек к выбранной сборке не подтверждена; угловой узел не подставляется вместо откоса."))
        issues.append(_issue("MOUNTING_OPENING_COMPONENTS_UNDEFINED", "Монтажная раскладка показывает края облицовки вокруг проёмов. Коробки и полотна включаются в альбом из отдельных актуальных сборок дверей и люков, если они созданы. Фурнитура таких сборок представлена габаритами, не механизмом. Конструкции остекления, порогов и отделки откосов отдельно не назначены."))
        for opening in openings:
            for problem in opening["parameter_issues"]:
                issues.append({**_issue(problem["code"],problem["message"]),"entity_ids":[opening["id"]],"details":problem["details"]})
    relevant_obstacles = [o for o in floor.obstacles if o.space_id in {None, face.space_id}]
    if relevant_obstacles:
        issue = _issue("MOUNTING_OBSTACLES_NOT_PROJECTED", "Ниши и другие препятствия не вычтены из стены автоматически: у них нет привязки глубины и границ к её стороне. Требуется отдельная геометрия облицовки.")
        issue["entity_ids"] = [o.id for o in relevant_obstacles]
        issues.append(issue)
    edge_contexts = _edge_contexts(project, floor, wall, face, exterior=config.installation_zone=="exterior")
    # Reference locations: never repeat a marker as though it were hardware.
    regions = [dict(line) for line in joint_lines]
    regions.extend({"kind": k, "from": _point(*a), "to": _point(*b)} for k,a,b in segments("floor", (0,config.floor_gap_mm), (length,config.floor_gap_mm)))
    regions.extend({"kind":k,"from":_point(*a),"to":_point(*b)} for k,a,b in segments("ceiling",(0,height-config.ceiling_gap_mm),(length,height-config.ceiling_gap_mm)))
    for edge in edge_contexts:
        x = 0 if edge["kind"] == "left_edge" else length
        for kind, a, b in segments(edge["kind"], (x,config.floor_gap_mm), (x,height-config.ceiling_gap_mm)):
            adjacent_voids = edge.get("adjacent_voids", [])
            breaks = sorted({a[1], b[1], *(value for void in adjacent_voids for value in (void["bottom_mm"],void["top_mm"]) if a[1] < value < b[1])})
            for bottom, top in zip(breaks, breaks[1:]):
                blocked = [void for void in adjacent_voids if void["bottom_mm"] < top and void["top_mm"] > bottom]
                span_context = {**edge,"corner_type":"unresolved","right_angle":False,
                                "detail_reason":"На соседней стене на этой высоте находится проём: это не стык двух сплошных облицовок.",
                                "adjacent_opening_ids":[void["opening_id"] for void in blocked]} if blocked else edge
                regions.append({"kind":kind,"from":_point(x,bottom),"to":_point(x,top),"edge_context":span_context})
    if any(r.get("edge_context",{}).get("adjacent_opening_ids") for r in regions):
        issues.append(_issue("MOUNTING_CORNER_INTERRUPTED_BY_OPENING", "Проём соседней стены прерывает угловое примыкание. На этом участке нужен отдельный торцевой узел; обычная сборка угла не назначается."))
    finish_rect = (0, config.floor_gap_mm, length, usable_height)
    for o in openings:
        x,y,w,h = o["x_mm"],o["y_mm"],o["width_mm"],o["height_mm"]
        if w <= 0 or h <= 0:
            continue
        # Use the same serialized-coordinate tolerance as finish segments;
        # otherwise a flush opening acquires a fictitious exterior jamb.
        for kind,a,b in visible_opening_edges((x,y,w,h), finish_rect, cutters, tolerance=1e-6):
            regions.append({"kind":kind,"opening_id":o["id"],"opening_kind":o["kind"],"from":_point(*a),"to":_point(*b)})
    labels = {"vertical_joint":"Стык панелей", "horizontal_joint":"Горизонтальный стык", "floor":"Примыкание к полу", "ceiling":"Примыкание к потолку", "left_edge":"Левый край / угол", "right_edge":"Правый край / угол", "opening_head":"Верх проёма", "opening_sill":"Низ проёма", "opening_jamb_left":"Левый откос", "opening_jamb_right":"Правый откос"}
    if config.installation_zone=="exterior":
        labels.update(floor="Низ наружной облицовки",ceiling="Верх наружной облицовки")
    nodes = []
    for n, region in enumerate(regions, 1):
        covered = bool(chosen and chosen["status"] == "candidate" and (region["kind"] == "vertical_joint" or (region["kind"] == "floor" and "floor_junction" in chosen["contexts"]) or (region["kind"] == "ceiling" and "ceiling_junction" in chosen["contexts"])))
        node = {"id":f"mount-{n}","kind":region["kind"],"label":labels[region["kind"]],
                "x_mm":(region["from"]["x_mm"]+region["to"]["x_mm"])/2,"y_mm":(region["from"]["y_mm"]+region["to"]["y_mm"])/2,
                "status":"reference_candidate" if covered else "unresolved", "rule_id":chosen["rule_id"] if covered else None,
                "source":chosen["source"] if covered else None,"approval_status":"unreviewed"}
        if region.get("edge_context"):
            node["edge_context"] = region["edge_context"]
        if config.installation_zone=="exterior" and node["kind"] in {"floor","ceiling"}:
            node["boundary_basis"]="exterior_cladding_limits"
        if region.get("opening_id"):
            opening = next(o for o in openings if o["id"] == region["opening_id"])
            node.update(opening_id=region["opening_id"],opening_kind=region["opening_kind"],
                        reveal_finish=opening["reveal_finish"],reveal_depth_mm=None,door=opening["door"],view_door=opening["view_door"],
                        detail_scope="cladding_edges_only")
        nodes.append(node)
        region["node_id"] = node["id"]
    assumptions=node_decisions(config,snapshot,chosen,nodes,selection_status)
    from .mount_node_groups import apply_node_groups
    node_groups = apply_node_groups(config, effective, snapshot, assets, chosen, nodes, assumptions)
    # A local pair at a T junction remains distinct from a whole room corner,
    # but must not be reported as unidentified once an exact recipe exists.
    # Test actual node spans: a neighbouring opening may interrupt only part
    # of an otherwise usable pair. No native CAD build or live-library read.
    from .assembly_recipes import recipe_for
    local_view = {"configuration":config.model_dump(mode="json"),"candidates":candidates,
                  "selected_rule_id":chosen["rule_id"] if chosen else None,"selection_status":selection_status,
                  "snapshot":snapshot.model_dump(mode="json"),
                  "wall":{"base_mm":wall.base_mm,"height_mm":height,"space_height_mm":space_height,"space_height_basis":height_basis}}
    unknown_edges, known_local_pairs, known_continuations = [], [], []
    for node in nodes:
        edge = node.get("edge_context")
        if not edge:
            continue
        if edge.get("local_panel_junction") or edge.get("local_panel_continuation"):
            if recipe_for(local_view,node,assets).get("spec"):
                (known_local_pairs if edge.get("local_panel_junction") else known_continuations).append(node)
            else:
                unknown_edges.append(node)
        elif edge.get("corner_type") in {None,"unresolved"}:
            unknown_edges.append(node)
    if unknown_edges:
        issues.append(_issue("MOUNTING_EDGE_DETAILS_UNKNOWN", "Для части краёв не определена применимая конструкция сопряжения. Проверьте смежные стороны, проёмы и монтажные параметры; совпадение концов стен само по себе не задаёт узел."))
    if known_local_pairs:
        issues.append(_issue("MOUNTING_T_JUNCTION_LOCAL_PAIR", "Т-примыкание разделено на локальные углы выбранных облицовок. Для каждой пары проверены стороны, угол, параметры и актуальность монтажа; применяется реальная STEP-посадка с проектными допущениями. Соединение оснований и весь Т-каркас этим разрезом не моделируются."))
    if known_continuations:
        issues.append(_issue("MOUNTING_STRAIGHT_PANEL_CONTINUATION", "На прямом продолжении выбранных сторон проверены общая плоскость, актуальность и параметры соседней облицовки. Применяется реальный стыковой профиль; ответвление с обратной стороны стены не превращает этот стык в угол."))
    if assumptions:
        issues.append(_issue("MOUNTING_PROJECT_ASSUMPTIONS",f"Принято проектных допущений: {len(assumptions)}. Они включены в реестр и не являются подтверждением изготовителя."))
    if selection_status=="incompatible":
        issues.append(_issue("MOUNTING_ASSEMBLY_INCOMPATIBLE",selection_reason,"error"))
    if config.allow_project_assumptions:
        for issue in issues:
            if issue["code"]=="MOUNTING_OPENINGS_UNKNOWN":
                issue["message"]="Проёмы вычтены из панелей. Решения имеют построчный статус: ссылка на источник либо проектная адаптация/принципиальная схема; требуется проверка применимости."
    return {"schema_version":"wall-mount-preview/1.0", "wall":{"id":wall.id,"face_id":face.id,"name":wall.name,**({"installation_zone":"exterior","exterior_applicability":"unverified"} if config.installation_zone=="exterior" else {}),"floor_id":floor.id,"side":face.side,"space_id":face.space_id,"space_height_mm":space_height,"space_height_basis":height_basis,"ceiling_space_id":ceiling_space_id,"length_mm":length,"height_mm":height,"base_mm":wall.base_mm,"start":wall.start.model_dump(mode="json"),"end":wall.end.model_dump(mode="json"),"view_direction":"wall_end_to_start" if face.side=="right" else "wall_start_to_end"},
            "configuration":config.model_dump(mode="json"),"effective_configuration":effective.model_dump(mode="json"),"decision_policy":POLICY,"assumptions":assumptions,"geometry_signature":wall_signature(project,floor,wall,face),"snapshot":snapshot.model_dump(mode="json"),
            "panels":panels,"openings":openings,"layout":layout,"joint_lines":joint_lines,"mounting_regions":regions,"nodes":nodes,"node_groups":node_groups,"edge_contexts":edge_contexts,"candidates":candidates,
            "selected_rule_id":chosen["rule_id"] if chosen else None,"requested_rule_id":config.assembly_rule_id,"selection_status":selection_status,"selection_reason":selection_reason,
            "requirements":chosen["requirements"] if chosen else [],"issues":issues,"warnings":[i["message"] for i in issues],
            "approval_status":"unreviewed","production_ready":False,
            "summary":{"panel_count":len({p["panel_id"] for p in panels}),"piece_count":len(panels),"area_m2":round(sum(p["area_m2"] for p in panels),9)},
            "basis":{"horizontal":"wall_reference_line","vertical":"from_wall_bottom","pieces":"geometric_rectangles_not_cutting_plan","fastener_spacing":None},
            "options":{"materials":[{"id":k,"label":v} for k,v in [("veneer","Шпон"),("hpl","HPL"),("painted","Окраска"),("unspecified","Не выбран")]],"mounting_systems":[{"id":"open_mount","label":"Открытый монтаж"},{"id":"concealed_mount","label":"Скрытый монтаж"}]}}


def mount_scope_targets(project, scope, *, target_limit=None):
    """Resolve boundary faces and complete partitions without mutating ownership.

    Whole-floor scope additionally includes explicitly drawn partitions outside
    rooms. Unclassified outside walls and partially crossing walls remain
    explicit exclusions; applying one binding can never clip just half a wall.
    """
    from .mount_defaults import room_geometry, wall_room_relation
    scope=scope if isinstance(scope,MountScope) else MountScope.model_validate(scope)
    floors=[f for b in project.buildings for f in b.floors]
    if scope.kind in {"exterior_floor","exterior_building"}:
        from .exterior_mount import exterior_floor_context
        if scope.kind == "exterior_floor":
            selected=[f for f in floors if f.id==scope.id]
            if len(selected)!=1:raise ValueError("Этаж наружного монтажа не найден")
        else:
            buildings=[b for b in project.buildings if b.id==scope.id]
            if len(buildings)!=1:raise ValueError("Здание наружного монтажа не найдено")
            selected=list(buildings[0].floors)
        contexts=[exterior_floor_context(f,float(project.settings.geometry_tolerance_mm)) for f in selected]
        targets=[t for context in contexts for t in context["targets"]]
        if target_limit is not None and len(targets)>target_limit:raise ValueError("Область превышает заданный предел сторон стен")
        return {"floor":selected[0] if len(selected)==1 else None,"floors":selected,"targets":targets,
                "excluded":[e for context in contexts for e in context["excluded"]],"excluded_boundary_faces":0}
    if scope.kind=="floor":
        matches=[f for f in floors if f.id==scope.id]
        if len(matches)!=1:
            raise ValueError("Этаж области монтажа не найден или имеет повторяющийся идентификатор")
        floor=matches[0];rooms=list(floor.spaces)
    else:
        matches=[(f,s) for f in floors for s in f.spaces if s.id==scope.id]
        if len(matches)!=1:
            raise ValueError("Помещение области монтажа не найдено или имеет повторяющийся идентификатор")
        floor,space=matches[0];rooms=[space]
    if len({w.id for w in floor.walls}) != len(floor.walls):
        raise ValueError("Повторяются идентификаторы стен этажа")
    if target_limit is not None and sum(len(s.boundary) for s in rooms)>target_limit:
        raise ValueError("Область превышает явно заданный предел сторон стен")
    tolerance=float(project.settings.geometry_tolerance_mm)
    geometries=[(space,room_geometry(floor,space,tolerance)) for space in rooms]
    targets=[];seen=set();face_ids=set();excluded=[]
    def add(wall,face,space,reason):
        key=(wall.id,face.id)
        if key in seen:return
        if face.id in face_ids:raise ValueError("Идентификатор стороны повторяется у разных стен")
        if target_limit is not None and len(targets)>=target_limit:raise ValueError("Область превышает явно заданный предел сторон стен")
        seen.add(key);face_ids.add(face.id)
        targets.append({"wall":wall,"face":face,"space":space,"inclusion_reason":reason})
    for space,geometry in geometries:
        for wall,face in zip(geometry["walls"],geometry["faces"]):
            add(wall,face,space,"room_boundary")
    boundary_ids={edge.wall_id for space in floor.spaces for edge in space.boundary}
    for wall in floor.walls:
        if wall.id in boundary_ids:continue
        relations=[(space,wall_room_relation(wall,geometry,tolerance)) for space,geometry in geometries]
        inside=[space for space,relation in relations if relation=="inside"]
        for face in wall.faces:
            eligible=[space for space in inside if face.space_id in {None,space.id}]
            if len(eligible)==1:
                add(wall,face,eligible[0],"partition_inside_room");continue
            # A known owner resolves overlapping-room ambiguity, otherwise the
            # assignment is deliberately left to the user.
            if len(eligible)>1:reason="ambiguous_room"
            elif face.space_id and inside:reason="space_assignment_conflict"
            elif any(relation!="outside" for _,relation in relations):reason="crosses_room_boundary"
            elif math.hypot(wall.end.x-wall.start.x,wall.end.y-wall.start.y)<=tolerance:reason="degenerate_wall"
            elif scope.kind=="floor" and wall.properties.get("editor_kind")=="partition" and face.space_id is None:
                add(wall,face,None,"standalone_partition");continue
            else:reason="outside_scope" if scope.kind=="space" else "unclassified_outside_wall"
            excluded.append({"wall_id":wall.id,"face_id":face.id,"side":face.side,"reason":reason})
    return {"floor":floor,"targets":targets,"excluded":excluded,
            "excluded_boundary_faces":sum((w.id,f.id) not in seen for w in floor.walls if w.id in boundary_ids for f in w.faces)}


def batch_preview_wall_mount(project, scope, configuration, snapshot, replace_existing=False):
    """Atomic pure preview of scoped wall faces; no database or file writes."""
    scope=scope if isinstance(scope,MountScope) else MountScope.model_validate(scope)
    config=configuration if isinstance(configuration,MountConfiguration) else MountConfiguration.model_validate(configuration)
    if scope.kind.startswith("exterior_"):
        config=config.model_copy(update={"installation_zone":"exterior"})
    saved=validate_wall_mounting(project)
    snapshot=saved.snapshot if saved else snapshot if isinstance(snapshot,ReferenceSnapshot) else ReferenceSnapshot.model_validate(snapshot)
    resolved=mount_scope_targets(project,scope)
    targets=[(t["wall"],t["face"],t["space"]) for t in resolved["targets"]]
    target_context={(t["wall"].id,t["face"].id):{"inclusion_reason":t["inclusion_reason"],"space_id":t["space"].id if t["space"] else None} for t in resolved["targets"]}
    existing={(b.wall_id,b.face_id):b for b in saved.walls} if saved else {}
    changed=[(w,f,s) for w,f,s in targets if replace_existing or (w.id,f.id) not in existing]
    changed_keys={(w.id,f.id) for w,f,s in changed}
    # Validate through the typed model above, but retain the original JSON.
    # Normalizing a historical binding would add new defaults/nullable fields
    # and make an unrelated bulk operation look like an edit to that wall.
    raw_existing=project.properties.get("wall_mounting",{}).get("walls",[]) if saved else []
    retained=[deepcopy(b) for b in raw_existing if (b["wall_id"],b["face_id"]) not in changed_keys]
    previews=[];entries=[];assumptions=[];pieces=0
    assets=pinned_step_assets(snapshot)
    for wall,face,space in changed:
        result=preview_wall_mount(project,wall.id,face.id,config,snapshot,assets)
        result["scope_context"]=target_context[(wall.id,face.id)]
        pieces+=len(result["panels"])
        entry={"wall_id":wall.id,"face_id":face.id,"configuration":result["configuration"],"geometry_signature":result["geometry_signature"]}
        entries.append(entry)
        for item in result["assumptions"]:
            assumptions.append({**item,"id":f"{face.id}:{item['id']}","local_id":item["id"],"wall_id":wall.id,"face_id":face.id,"space_id":space.id if space else None})
        result.pop("snapshot")  # one common source snapshot, not 100 duplicates
        previews.append(result)
    skipped=[{"wall_id":w.id,"face_id":f.id,"space_id":s.id if s else None,"reason":"existing_preserved"} for w,f,s in targets if (w.id,f.id) not in changed_keys]
    return {"schema_version":"wall-mount-batch/1.0","scope":scope.model_dump(mode="json"),"configuration":config.model_dump(mode="json"),"replace_existing":replace_existing,
            "context_signature":_context_hash({"project":project.model_dump(mode="json"),"scope":scope.model_dump(mode="json"),"configuration":config.model_dump(mode="json"),"replace_existing":replace_existing}),
            "snapshot":snapshot.model_dump(mode="json"),"entries":entries,"retained_entries":retained,"previews":previews,"skipped":skipped,"assumptions":assumptions,"excluded":resolved["excluded"],
            "scope_targets":[{"wall_id":t["wall"].id,"face_id":t["face"].id,"inclusion_reason":t["inclusion_reason"]} for t in resolved["targets"]],
            "summary":{"target_count":len(targets),"changed_count":len(entries),"skipped_count":len(skipped),"total_assigned_count":len(entries)+len(retained),
                       "panel_count":sum(p["summary"]["panel_count"] for p in previews),"piece_count":pieces,"area_m2":round(sum(p["summary"]["area_m2"] for p in previews),9),
                       "node_count":sum(len(p["nodes"]) for p in previews),"assumption_count":len(assumptions),
                       "room_boundary_faces":sum(t["inclusion_reason"]=="room_boundary" for t in resolved["targets"]),
                       "interior_partition_faces":sum(t["inclusion_reason"]=="partition_inside_room" for t in resolved["targets"]),
                       "standalone_partition_faces":sum(t["inclusion_reason"]=="standalone_partition" for t in resolved["targets"]),
                       "exterior_faces":sum(t["inclusion_reason"]=="exterior_envelope" for t in resolved["targets"]),
                       "excluded_boundary_faces":resolved["excluded_boundary_faces"],"excluded_unbound_faces":len(resolved["excluded"])},
            "approval_status":"unreviewed","production_ready":False,
            "issues":[{**issue,"entity_ids":[p["wall"]["id"],p["wall"]["face_id"]]} for p in previews for issue in p["issues"]]}


def wall_mount_report(project):
    saved = validate_wall_mounting(project)
    result = {"schema_version":"wall-mount-report/1.0","walls":[],"unavailable_walls":[],"issues":[],"approval_status":"unreviewed"}
    if saved is None:
        return result
    total_pieces = 0
    for binding in saved.walls:
        try:
            preview = preview_wall_mount(project, binding.wall_id, binding.face_id, binding.configuration, saved.snapshot)
        except ValueError as error:
            # A valid later wall edit can make a previously saved finish
            # impossible. Keep diagnostics/other walls usable, but do not draw
            # stale panels or replace the missing result with a fake empty one.
            issue = _issue("MOUNTING_LAYOUT_INVALID", "Схема монтажа стороны стены не построена: " + str(error), "error")
            issue["entity_ids"] = [binding.wall_id,binding.face_id]
            result["issues"].append(issue)
            result["unavailable_walls"].append({"wall_id":binding.wall_id,"face_id":binding.face_id,"status":"invalid","message":issue["message"]})
            continue
        total_pieces += len(preview["panels"])
        if binding.geometry_signature != preview["geometry_signature"]:
            preview["issues"].append(_issue("MOUNTING_CONTEXT_STALE", "Стена, проём или соседняя геометрия изменены после назначения монтажа. Обновите и сохраните предварительную схему.", "error"))
        for issue in preview["issues"]:
            issue["entity_ids"] = [binding.wall_id,binding.face_id]
        preview.pop("snapshot")
        # The immutable snapshot contains every alternative; derived reports
        # need the selected one only, avoiding quadratic document bloat on many
        # walls that share the same manual catalogue.
        preview["candidate_summary"] = {status:sum(c["status"]==status for c in preview["candidates"]) for status in ("candidate","incompatible","unknown")}
        for group in preview["node_groups"]:
            group["candidate_summary"] = {status: sum(item["status"] == status for item in group["candidates"]) for status in ("candidate", "incompatible", "unknown")}
            keep = {group["requested_rule_id"], group["selected_rule_id"]}
            group["candidates"] = [item for item in group["candidates"] if item["rule_id"] in keep]
        preview["candidates"] = [c for c in preview["candidates"] if c["rule_id"] == preview["selected_rule_id"]]
        result["walls"].append(preview)
        result["issues"].extend(preview["issues"])
    return result
