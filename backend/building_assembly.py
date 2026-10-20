"""Whole-building finish placement from server-owned, validated wall records.

There is no wall/body/point quota here. Individual wall records retain their
strict validation; the isolated worker enforces actual memory/time/disk budgets.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .assembly_jobs import input_signature
from .mount_assemblies import digest
from .room_assembly import matmul, transform_point, transform_bounds, wall_placement
from .wall_mount import mount_scope_targets, validate_wall_mounting
from .whole_wall_assemblies import validate_wall_settings
from .server_wall_records import create_wall_planner, validate_server_whole_walls

BUILDING_POLICY = "building-finish-assembly/1.0-streamed"


def building_targets(project, building_id, *, mounting=None):
    building=next((item for item in project.buildings if item.id==building_id),None)
    if building is None:raise ValueError("Здание не найдено")
    mounting=mounting if mounting is not None else validate_wall_mounting(project)
    mounted={(item.wall_id,item.face_id) for item in mounting.walls} if mounting else set()
    targets=[];seen=set();eligible=set()
    for floor in building.floors:
        resolved=mount_scope_targets(project,{"kind":"floor","id":floor.id},target_limit=None)
        contexts={(item["wall"].id,item["face"].id):item for item in resolved["targets"]}
        eligible.update(contexts)
        for wall in floor.walls:
            for face in wall.faces:
                key=(wall.id,face.id)
                if key not in mounted or key in seen:continue
                seen.add(key);eligible.add(key)
                targets.append({**contexts.get(key,{"wall":wall,"face":face,"space":None,
                                "inclusion_reason":"explicitly_mounted_building_face"}),"floor":floor})
    if not targets:raise ValueError("В здании ещё нет монтажа. Примените монтаж и сохраните проект.")
    return building,targets,{"mounted_wall_count":len(targets),"total_wall_count":len(eligible),"complete":len(eligible)==len(targets)}


def _records(project, targets, *, mounting=None):
    # One strict validation of all stored wall records for this immutable build.
    # Rechecking the same collection once per room used to decompress it N times.
    collection=validate_server_whole_walls(project)
    by_target={(record.wall_id,record.face_id):record for record in collection.items}
    settings=validate_wall_settings(project)
    make_plan=create_wall_planner(project,mounting=mounting)
    walls=[];records=[];missing=[]
    for target in targets:
        wall,face,floor=target["wall"],target["face"],target["floor"]
        key=(wall.id,face.id);record=by_target.get(key);rebuild=False
        if record is not None:
            try:
                current=make_plan(wall.id,face.id,settings.get(key,record.configuration))
                if digest(current)!=digest(record.plan):
                    rebuild=(current.get("status")=="ready" and all(current.get(name)==record.plan.get(name)
                        for name in ("geometry_signature","mount_configuration","configuration","layers","wall")))
                    if not rebuild:record=None
            except ValueError:record=None
        if record is None:
            missing.append({"wall_id":wall.id,"face_id":face.id,"floor_id":floor.id,"label":wall.name})
            continue
        transform=wall_placement(floor,wall,face,record.plan)
        walls.append({"wall_id":wall.id,"face_id":face.id,"floor_id":floor.id,
            "space_id":face.space_id,"label":record.label,"record_id":record.id,
            "fingerprint":record.fingerprint,"rebuild":rebuild,
            "inclusion_reason":target["inclusion_reason"],**transform,
            "original_base":{"rotation":deepcopy(transform["rotation"]),
                "translation_mm":deepcopy(transform["translation_mm"]),
                "plan":deepcopy(record.plan),"wall_thickness_mm":wall.thickness_mm}})
        records.append(record)
    return walls,records,missing


def _preview(walls,records):
    definitions=[];instances=[];bodies=[];identical={};points=0
    for index,(wall,record) in enumerate(zip(walls,records),1):
        preview=record.geometry.get("preview",{})
        if preview.get("schema")!="wall-assembly-wire-preview/1":
            raise ValueError("В сборке стены отсутствует совместимый 3D-просмотр; пересчитайте стены")
        definitions_by_id={item["id"]:item for item in preview["definitions"]}
        preview_instances={item["id"]:item for item in preview["instances"]}
        used={body["definition_id"] for body in record.geometry["bodies"] if body["role"]!="substrate"}
        aliases={}
        for identifier in sorted(used):
            definition=definitions_by_id[identifier]
            # Only identical preview geometry/metadata is shared; STEP evidence
            # is deduplicated separately by source SHA in the native worker.
            encoded=json.dumps({key:value for key,value in definition.items() if key!="id"},
                               sort_keys=True,separators=(",",":"),allow_nan=False).encode()
            signature=hashlib.sha256(encoded).hexdigest()
            if signature not in identical:
                identity="d:"+signature;identical[signature]=identity
                definitions.append({**deepcopy(definition),"id":identity})
                points+=sum(len(path) for path in definition.get("paths",[]))
            aliases[identifier]=identical[signature]
        for body in record.geometry["bodies"]:
            if body["role"]=="substrate":continue
            item=preview_instances[body["instance_id"]];identity=f"w{index}:"+item["id"]
            instances.append({**item,"id":identity,"definition_id":aliases[item["definition_id"]],
                "rotation":matmul(wall["rotation"],item["rotation"]),
                "translation_mm":transform_point(wall["rotation"],wall["translation_mm"],item["translation_mm"]),
                "wall_id":wall["wall_id"],"face_id":wall["face_id"],"floor_id":wall["floor_id"]})
            bodies.append({"id":identity,"wall_id":wall["wall_id"],"face_id":wall["face_id"],"floor_id":wall["floor_id"],
                "role":body["role"],"label":body["label"],"volume_mm3":body["volume_mm3"],
                "bounds_mm":transform_bounds(body["bounds_mm"],wall["rotation"],wall["translation_mm"])})
    if not bodies:raise ValueError("В назначенных сборках нет деталей отделки")
    low=[min(body["bounds_mm"]["min"][axis] for body in bodies) for axis in range(3)]
    high=[max(body["bounds_mm"]["max"][axis] for body in bodies) for axis in range(3)]
    return {"schema":"wall-assembly-wire-preview/1","definitions":definitions,"instances":instances,"point_count":points},bodies,{"min":low,"max":high,"size":[b-a for a,b in zip(low,high)]}


def _building_joints(walls,records,tolerance,*,building=None):
    from .room_joints import plan_room_joints, physical_wall_topology
    joints=[];unresolved=[];spans=[None]*len(walls)
    floors={}
    for index,wall in enumerate(walls):floors.setdefault(wall["floor_id"],[]).append(index)
    for floor_id,indices in floors.items():
        floor=next((floor for floor in building.floors if floor.id==floor_id),None) if building else None
        result=plan_room_joints([walls[i] for i in indices],[records[i] for i in indices],tolerance,
                               physical_walls=physical_wall_topology(floor) if floor else None)
        for local_index,span in enumerate(result["wall_spans"]):spans[indices[local_index]]=span
        for item in result["joints"]:
            joint=deepcopy(item);joint.pop("id",None)
            joint["floor_id"]=floor_id
            joint["wall_indices"]=[indices[i] for i in joint["wall_indices"]]
            for side in joint["sides"]:side["wall_index"]=indices[side["wall_index"]]
            joint["id"]=digest(joint);joints.append(joint)
        for item in result["unresolved"]:
            unresolved.append({**item,"floor_id":floor_id,"wall_indices":[indices[i] for i in item["wall_indices"]]})
    return {"policy":"building-joints/1.0","joints":joints,"wall_spans":spans,"unresolved":unresolved}


def prepare_building(project,building_id):
    mounting=validate_wall_mounting(project)
    building,targets,coverage=building_targets(project,building_id,mounting=mounting)
    walls,records,missing=_records(project,targets,mounting=mounting)
    floors=[{"id":floor.id,"name":floor.name,"elevation_mm":floor.elevation_mm,
             "origin_mm":[floor.origin.x,floor.origin.y],
             "wall_count":sum(target["floor"].id==floor.id for target in targets)} for floor in building.floors]
    base={"scope_kind":"building","building_id":building_id,"label":building.name,"floors":floors,
          "floor_count":len(floors),"wall_count":len(targets),"coverage":coverage,"missing":missing}
    if missing:return base,[]
    preview,bodies,bounds=_preview(walls,records)
    setting_out=_building_joints(walls,records,float(project.settings.geometry_tolerance_mm),building=building)
    for wall,span in zip(walls,setting_out["wall_spans"]):wall["room_span"]=span
    signature=input_signature(project)
    return {**base,"schema_version":"building-assembly/1.0","policy":BUILDING_POLICY,
        "id":digest({"policy":BUILDING_POLICY,"source":signature,"building_id":building_id,"walls":walls}),
        "source_revision":project.revision,"source_signature":signature,"walls":walls,
        "body_count":len(bodies),"bounds_mm":bounds,"wire_preview":preview,"bodies":bodies,
        "joints":setting_out["joints"],"joint_setting_out":setting_out,
        "checks":{"individual_walls":"valid","room_collisions":"not_checked"},
        "warnings":[] if coverage["complete"] else ["Стороны без назначенного монтажа не включены в 3D-сборку здания."]},records


def refresh_building_manifest(result,walls,records):
    from .room_assembly import refresh_room_manifest
    if result.get("scope_kind")!="building":raise ValueError("Нужен реестр сборки здания")
    return refresh_room_manifest(result,walls,records)
