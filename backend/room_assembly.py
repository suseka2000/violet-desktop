"""Room-space placement of already verified, exact wall assemblies.

The domain wall line is its centreline. Wall assembly Y points from the panel
into the substrate; X runs A→B on a left face and B→A on a right face. Keeping
these conventions explicit prevents mirrored openings and floating floors.
"""
from __future__ import annotations

import math
from copy import deepcopy

from .assembly_jobs import input_signature
from .mount_assemblies import digest
from .wall_mount import mount_scope_targets, validate_wall_mounting
from .whole_wall_assemblies import validate_wall_settings
from .server_wall_records import validate_server_whole_walls, create_wall_planner

POLICY = "room-finish-assembly/3.0-streamed-unbounded-inventory"
IDENTITY = [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]


def matmul(a, b):
    return [[sum(a[i][k]*b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def transform_point(rotation, translation, point):
    return [sum(rotation[i][k]*point[k] for k in range(3))+translation[i] for i in range(3)]


def transform_bounds(bounds, rotation, translation):
    points = [transform_point(rotation, translation, [bounds[a][0], bounds[b][1], bounds[c][2]])
              for a in ("min", "max") for b in ("min", "max") for c in ("min", "max")]
    low = [min(p[k] for p in points) for k in range(3)]
    high = [max(p[k] for p in points) for k in range(3)]
    return {"min": low, "max": high, "size": [b-a for a, b in zip(low, high)]}


def wall_placement(floor, wall, face, plan):
    dx, dy = wall.end.x-wall.start.x, wall.end.y-wall.start.y
    length = math.hypot(dx, dy)
    if length <= 1e-6:
        raise ValueError("Нельзя собрать отделку стены нулевой длины")
    side = 1. if face.side == "left" else -1.
    tx, ty = dx/length*side, dy/length*side
    nx, ny = -ty, tx  # selected room-facing normal
    origin = wall.start if side == 1 else wall.end
    distance = wall.thickness_mm/2 + plan["layers"]["substrate_front_mm"]
    # This matrix has determinant -1. Native export bakes the handedness
    # change into definitions instead of illegal mirrored STEP occurrences.
    rotation = [[tx, -nx, 0.], [ty, -ny, 0.], [0., 0., 1.]]
    translation = [floor.origin.x+origin.x+nx*distance,
                   floor.origin.y+origin.y+ny*distance,
                   floor.elevation_mm+wall.base_mm]
    return {"rotation": rotation, "translation_mm": translation,
            "side": face.side, "basis": "wall_centreline_to_substrate_face", "depth_mm": distance}


def room_targets(project, space_id):
    resolved = mount_scope_targets(project, {"kind": "space", "id": space_id}, target_limit=None)
    space = next(space for space in resolved["floor"].spaces if space.id == space_id)
    mounting = validate_wall_mounting(project)
    mounted = {(item.wall_id, item.face_id) for item in mounting.walls} if mounting else set()
    targets = [item for item in resolved["targets"] if (item["wall"].id, item["face"].id) in mounted]
    if not targets:
        raise ValueError("В помещении ещё нет монтажа. Примените монтаж к помещению и сохраните проект.")
    return resolved, space, targets


def prepare_room(project, space_id, *, include_preview=True):
    """Return a source-bound manifest, preview, and exact source records.

    Never accept client-supplied CAD. The caller obtains this project from the
    repository + the exact-input AssemblyJobs overlay, not a request body.
    """
    resolved, space, targets = room_targets(project, space_id)
    seeds = {(item.wall_id,item.face_id):item for item in validate_server_whole_walls(project).items}
    settings = validate_wall_settings(project)
    make_plan=create_wall_planner(project)
    records, missing, walls = [], [], []
    for target in targets:
        wall, face = target["wall"], target["face"]
        record = seeds.get((wall.id, face.id))
        rebuild = False
        if record is not None:
            seed=record
            # An engine upgrade may change derived setting-out. Such a record
            # supplies verified source dimensions only; its solids must be
            # rebuilt before publication. Changed user inputs never qualify.
            try:
                current=make_plan(wall.id,face.id,settings.get((wall.id,face.id),seed.configuration))
                if digest(current)!=digest(seed.plan):
                    rebuild=current.get("status")=="ready" and all(current.get(key)==seed.plan.get(key)
                        for key in ("geometry_signature","mount_configuration","configuration","layers","wall"))
                    if not rebuild:record=None
            except ValueError:record=None
        if record is None:
            missing.append({"wall_id": wall.id, "face_id": face.id, "label": wall.name})
            continue
        transform = wall_placement(resolved["floor"], wall, face, record.plan)
        walls.append({"wall_id": wall.id, "face_id": face.id, "label": record.label,
                      "record_id": record.id, "fingerprint": record.fingerprint, "rebuild":rebuild,
                      "inclusion_reason": target["inclusion_reason"], **transform,
                      "original_base": {"rotation":deepcopy(transform["rotation"]),
                                        "translation_mm":deepcopy(transform["translation_mm"]),
                                        "plan":deepcopy(record.plan), "wall_thickness_mm":wall.thickness_mm}})
        records.append(record)
    if missing:
        return {"missing": missing, "wall_count": len(targets)}, []
    count = sum(sum(b["role"] != "substrate" for b in record.geometry["bodies"]) for record in records)
    definitions, instances, bodies, points = [], [], [], 0
    for index, (wall, record) in enumerate(zip(walls, records), 1):
        prefix = f"w{index}:"
        preview = record.geometry.get("preview", {})
        if preview.get("schema") != "wall-assembly-wire-preview/1":
            raise ValueError("В сборке стены отсутствует совместимый 3D-просмотр; пересчитайте стены")
        definitions_by_id = {item["id"]: item for item in preview["definitions"]}
        preview_instances = {item["id"]: item for item in preview["instances"]}
        used = {body["definition_id"] for body in record.geometry["bodies"] if body["role"] != "substrate"}
        for identifier in sorted(used) if include_preview else []:
            definition = deepcopy(definitions_by_id[identifier])
            definition["id"] = prefix+identifier
            definitions.append(definition)
            points += sum(len(path) for path in definition.get("paths", []))
        for body in record.geometry["bodies"]:
            if body["role"] == "substrate":
                continue  # architectural support is not a finish component
            item = preview_instances[body["instance_id"]]
            rotation = matmul(wall["rotation"], item["rotation"])
            translation = transform_point(wall["rotation"], wall["translation_mm"], item["translation_mm"])
            if include_preview:instances.append({**item, "id": prefix+item["id"], "definition_id": prefix+item["definition_id"],
                              "rotation": rotation, "translation_mm": translation,
                              "wall_id": wall["wall_id"], "face_id": wall["face_id"]})
            bodies.append({"id": prefix+body["instance_id"], "wall_id": wall["wall_id"], "face_id": wall["face_id"],
                           "role": body["role"], "label": body["label"], "volume_mm3": body["volume_mm3"],
                           "bounds_mm": transform_bounds(body["bounds_mm"], wall["rotation"], wall["translation_mm"])})
    low = [min(body["bounds_mm"]["min"][k] for body in bodies) for k in range(3)]
    high = [max(body["bounds_mm"]["max"][k] for body in bodies) for k in range(3)]
    signature = input_signature(project)
    identity = digest({"policy": POLICY, "source": signature, "space_id": space_id, "walls": walls})
    omitted = len(resolved["targets"])-len(targets)
    warnings = ([f"На {omitted} сторонах помещения монтаж не назначен; они не включены в сборку."] if omitted else [])
    result = {"schema_version": "room-assembly/1.0", "policy": POLICY, "id": identity,
            "space_id": space_id, "label": f"{space.code} · {space.name}", "source_revision": project.revision,
            "source_signature": signature, "walls": walls, "wall_count": len(walls), "body_count": len(bodies),
            "coverage": {"mounted_wall_count":len(targets),"total_wall_count":len(resolved["targets"]),"complete":omitted==0},
            "bounds_mm": {"min": low, "max": high, "size": [b-a for a,b in zip(low, high)]},
            "wire_preview": {"schema": "wall-assembly-wire-preview/1", "definitions": definitions,
                             "instances": instances, "point_count": points}, "bodies": bodies,
            "checks": {"individual_walls": "valid", "room_collisions": "not_checked"},
            "warnings": warnings, "missing": []}
    from .room_joints import plan_room_joints, physical_wall_topology
    setting_out = plan_room_joints(walls, records, float(project.settings.geometry_tolerance_mm),
                                  physical_walls=physical_wall_topology(resolved["floor"]))
    result["joints"] = setting_out["joints"]
    result["joint_setting_out"] = setting_out
    for wall, span in zip(walls, setting_out["wall_spans"]):
        wall["room_span"] = span
    return result, records


def refresh_room_manifest(result, walls, records):
    """Rebuild world-space evidence after coordinating the whole fixing rows."""
    if result.get("scope_kind") == "building" or (result.get("scope_kind") == "room" and "wire_preview" not in result):
        # These large inventories are replaced below, so copying them first
        # doubles transient memory without retaining any evidence. The initial
        # wire preview is read-only: the native building worker consumes the
        # fitted records and creates a new preview for publication. Retain the
        # existing field for callers, without walking every old contour again.
        previous = result
        result = deepcopy({key: value for key, value in previous.items()
                           if key not in {"walls", "bodies", "body_count", "bounds_mm", "wire_preview"}})
        if "wire_preview" in previous:
            result["wire_preview"] = previous["wire_preview"]
    else:
        result = deepcopy(result)
    result["walls"] = walls
    bodies = []
    for index, (wall, record) in enumerate(zip(walls, records), 1):
        for body in record.geometry["bodies"]:
            if body["role"] == "substrate":
                continue
            bodies.append({"id":f"w{index}:"+body["instance_id"], "wall_id":wall["wall_id"],
                           "face_id":wall["face_id"], "role":body["role"], "label":body["label"],
                           **({"floor_id":wall["floor_id"]} if result.get("scope_kind")=="building" else {}),
                           "volume_mm3":body["volume_mm3"],
                           "bounds_mm":transform_bounds(body["bounds_mm"], wall["rotation"], wall["translation_mm"])})
    result["bodies"] = bodies
    result["body_count"] = len(bodies)
    low = [min(item["bounds_mm"]["min"][axis] for item in bodies) for axis in range(3)]
    high = [max(item["bounds_mm"]["max"][axis] for item in bodies) for axis in range(3)]
    result["bounds_mm"] = {"min":low, "max":high, "size":[b-a for a,b in zip(low,high)]}
    identity={"policy":result.get("policy",POLICY), "source":result["source_signature"], "walls":walls,
              "joints":result.get("joints",[]), "space_id":result.get("space_id")}
    if result.get("scope_kind")=="building":
        identity.pop("space_id");identity["building_id"]=result["building_id"]
    result["id"]=digest(identity)
    return result


def prepare_building(project,building_id):
    from .building_assembly import prepare_building as prepare
    return prepare(project,building_id)
