"""Pure installation planning and explicitly requested, isolated solid builds."""
from __future__ import annotations

import base64
import hashlib

from pydantic import Field

from ..cad_sections import SHA256, make_section_snapshot
from ..domain.models import Model, Project
from ..mount_assemblies import digest
from ..special_assemblies import (ANGLE, Installation, Installations,
    SpecialAssemblies, SpecialAssembly, SpecialConfiguration, find_target, installation_id,
    recipe, special_document_entries, target_geometry, target_signature, validate_special)
from ..wall_mount import MountScope

WARNING="Специальные изделия построены как реальные твёрдые тела. Недостающие детали и фурнитура - явно параметрические проектные решения; механизмы, крепёж, нагрузки и безопасность эксплуатации требуют проверки."


class SpecialPlanRequest(Model):
    project:Project
    scope:MountScope
    configuration:SpecialConfiguration
    replace_existing:bool=Field(default=False,strict=True)


class SpecialBuildRequest(Model):
    project:Project
    installation_id:SHA256


def _scope_faces(project,scope):
    from .. import wall_mount
    if hasattr(wall_mount,"mount_scope_targets"):
        selected=wall_mount.mount_scope_targets(project,scope)
        return selected["floor"],[(t["wall"],t["face"]) for t in selected["targets"]]
    from .mount_sections import _targets
    keys=_targets(project,scope)
    rows=[(floor,wall,face) for b in project.buildings for floor in b.floors for wall in floor.walls for face in wall.faces if (wall.id,face.id) in keys]
    return rows[0][0],[(w,f) for _,w,f in rows]


def _refs(kind):
    return [{"document_id":"armory-reference-rd","filename":"РД_Оружейная_палата_Виолет_элит_винтерио_на_доработке_1.pdf",
             "sha256":"87c31566f9baf0f2cef78e33f04457d2269e2123509eed48e7f1a12bae65f933",
             "page":12 if kind=="concealed_door" else 10 if kind=="hatch" else 13,
             "role":"design_reference_not_manufacturer_approval",
             "note":"Конструктивный контекст образца; параметрическая геометрия и фурнитура выбраны проектно, не сняты как заводское изделие."}]


def special_plan(project,scope,configuration,model_library=None,replace_existing=False):
    scope=scope if isinstance(scope,MountScope) else MountScope.model_validate(scope)
    config=configuration if isinstance(configuration,SpecialConfiguration) else SpecialConfiguration.model_validate(configuration)
    normalized=config.model_dump(mode="json")
    old,records=validate_special(project)
    floor,targets=_scope_faces(project,scope)
    existing={i.id:i for i in old.items}
    sources=[]
    if model_library is not None:
        exact=next((a for a in model_library.inventory()["assets"] if a["source_id"]==ANGLE["source_id"]),None)
        if exact:sources=[ANGLE]
    # Keep the same source bytes on re-plan, even if the live folder changed.
    source_from_saved=next((i.source_models for i in old.items if i.source_models),None)
    if source_from_saved:sources=[s.model_dump(mode="json") for s in source_from_saved]
    proposals={};skipped=[]
    for wall,face in targets:
        candidates=[]
        for opening in floor.openings:
            if opening.wall_id!=wall.id:continue
            if opening.kind=="door" and "concealed_door" in config.kinds:
                if opening.door and opening.door.concealed_frame:candidates.append(("concealed_door",opening))
                else:skipped.append({"label":opening.name,"message":"Для сборки скрытой двери сначала включите скрытую коробку в свойствах проёма."})
            if opening.kind=="hatch" and "hatch" in config.kinds:candidates.append(("hatch",opening))
            if opening.kind=="door" and "lighting" in config.kinds and config.lighting.location=="above_doors":candidates.append(("lighting",opening))
        if "lighting" in config.kinds and config.lighting.location=="wall_top":candidates.append(("lighting",None))
        for kind,opening in candidates:
            identifier=installation_id(kind,wall.id,face.id,opening.id if opening else None)
            if identifier in proposals:continue
            if identifier in existing and not replace_existing:
                saved=existing[identifier]
                try:
                    # Preserve chosen settings and exact sources, but refresh
                    # derived dimensions after an ordinary room edit. Re-plan
                    # must actually unblock rebuilding stale saved products.
                    value=saved.model_copy(deep=True)
                    value.target_geometry=target_geometry(*find_target(project,saved),saved.kind,saved.configuration,project)
                    value.geometry_signature=target_signature(project,value)
                    recipe(value)
                    proposals[identifier]=value
                    skipped.append({"label":saved.label,"message":"Существующие параметры и исходники сохранены; габариты сверены с текущим проектом."})
                except ValueError as error:skipped.append({"label":saved.label,"message":str(error)})
                continue
            label={"concealed_door":"Скрытая дверь","hatch":"Ревизионный люк","lighting":"Линейная подсветка"}[kind]+" / "+(opening.name if opening else wall.name)
            try:
                value=Installation(id=identifier,kind=kind,label=label,wall_id=wall.id,face_id=face.id,floor_id=floor.id,space_id=face.space_id,
                                   opening_id=opening.id if opening else None,configuration=normalized,
                                   target_geometry=target_geometry(floor,wall,face,opening,kind,normalized,project),geometry_signature="0"*64,
                                   source_models=sources,source_refs=_refs(kind))
                value.geometry_signature=target_signature(project,value)
                recipe(value)  # All dimensions and allocation limits before native work.
                proposals[identifier]=value
            except ValueError as error:skipped.append({"label":label,"message":str(error)})
    merged={**existing,**proposals}
    full=Installations(items=list(merged.values()))
    projected=project.model_copy(deep=True)
    projected.properties["special_installations"]=full.model_dump(mode="json")
    states={e["id"]:e for e in special_document_entries(projected)}
    items=[];saved=[]
    for item in proposals.values():
        state=states[item.id]["status"]
        entry={"id":item.id,"label":item.label,"kind":item.kind,"status":state,
               "wall_id":item.wall_id,"face_id":item.face_id,"opening_id":item.opening_id,
               "locations":[floor.name+" / "+item.label],"source_models":[s.model_dump(mode="json") for s in item.source_models]}
        if state=="ready":saved.append(item.id)
        else:items.append(entry)
    return {"schema_version":"special-assembly-plan/1.0","scope":scope.model_dump(mode="json"),"items":items,"saved_ids":saved,
            "proposed_installations":full.model_dump(mode="json"),"skipped":skipped,"warnings":[WARNING],
            "context_signature":digest({"project":project.model_dump(mode="json"),"scope":scope.model_dump(mode="json"),"configuration":normalized}),
            "limits":{"items":None,"points":None,"step_base64_bytes":None},"approval_status":"unreviewed"}


def special_build_one(project,installation_id,model_library,source_store):
    from ..assembly_worker import run_assembly_build
    from ..step_worker import run_step_section
    from .cad_assets import CAD_SLOTS
    from .server import HttpError
    installations,records=validate_special(project)
    item=next((i for i in installations.items if i.id==installation_id),None)
    if item is None:raise ValueError("Настройка специальной установки не найдена")
    if target_signature(project,item)!=item.geometry_signature:raise ValueError("Параметры места изменились: сначала пересчитайте план специальных изделий")
    if item.target_geometry!=target_geometry(*find_target(project,item),item.kind,item.configuration,project):raise ValueError("Габариты установки не совпадают с текущей моделью; пересчитайте план")
    fresh=next((e for e in special_document_entries(project) if e["id"]==installation_id and e["status"]=="ready"),None)
    if fresh:return {"status":"ready","assembly":fresh["assembly"],"reused":True,"warnings":[WARNING]}
    descriptor=recipe(item)
    sources={}
    for source in item.source_models:
        try:content=source_store.get_bytes(source.source_id)
        except FileNotFoundError:content,_=model_library.original(source.source_id)
        if hashlib.sha256(content).hexdigest()!=source.source_id:raise ValueError("Точный исходный STEP изменился")
        sources[source.source_id]=content
    if not CAD_SLOTS.acquire(blocking=False):raise HttpError(429,"cad_busy","Обработчик CAD занят. Дождитесь завершения текущей операции.")
    try:
        built=run_assembly_build(sources,descriptor["spec"],timeout_seconds=90)
        if built["collisions"]:
            return {"status":"skipped","message":"Тела специальной сборки пересекаются. Геометрия не добавлена; измените параметры изделия.","collisions":built["collisions"],"warnings":[WARNING]}
        content=base64.b64decode(built["assembled_step"]["base64"],validate=True)
        sections=[]
        for index,section in enumerate(descriptor["sections"]):
            result=built["result"] if index==0 else run_step_section(content,section["axis"],section["offset_mm"],timeout_seconds=45)
            labels={b["id"]:b["label"] for b in built["bodies"]}
            for component in result["components"]:component["label"]=labels[component["id"]]
            if not result["point_count"]:raise ValueError("Сечение специального изделия оказалось пустым")
            snapshot=make_section_snapshot(built["assembled_step"]["source_id"],{"axis":section["axis"],"offset_mm":section["offset_mm"],"component_ids":None},result)
            sections.append({"id":section["id"],"label":section["label"],"geometry_snapshot":snapshot,
                             "bodies":[{"id":b["id"],"section_loops":b["section_loops"]} for b in built["bodies"]] if index==0 else []})
    finally:CAD_SLOTS.release()
    materials={r["instance_id"]:r for r in descriptor["materials"]}
    for body in built["bodies"]:
        body.update(material=materials[body["instance_id"]]["material"],origin=materials[body["instance_id"]]["origin"])
    record={"installation_id":item.id,"label":item.label,"kind":item.kind,"configuration":item.configuration,"target_signature":item.geometry_signature,
            "source_models":[s.model_dump(mode="json") for s in item.source_models],"source_refs":item.source_refs,
            "assumptions":descriptor["assumptions"],"spec":descriptor["spec"],"bodies":built["bodies"],"materials":descriptor["materials"],
            "assembled_step":built["assembled_step"],"sections":sections,"collisions":[],"approval_status":"unreviewed"}
    record["id"]=digest({key:record[key] for key in ("installation_id","target_signature","spec","source_models","assumptions")})
    record["fingerprint"]=digest(record)
    record=SpecialAssembly.model_validate(record).model_dump(mode="json",by_alias=True)
    SpecialAssemblies.model_validate({"items":[*(r.model_dump(mode="json",by_alias=True) for r in records.items if r.installation_id!=item.id),record]})
    from ..workspace import validate_project_properties_budget
    projected=project.model_copy(deep=True)
    projected.properties["special_assemblies"]={"schema_version":"special-assemblies/1.0","items":[*(r.model_dump(mode="json",by_alias=True) for r in records.items if r.installation_id!=item.id),record]}
    validate_project_properties_budget(projected)
    for source in item.source_models:source_store.put(source.filename,sources[source.source_id])
    return {"status":"ready","assembly":record,"warnings":[WARNING]}


def handle_special_assemblies(handler,method,path,model_library,source_store):
    if path not in {"/api/v1/special-assemblies/plan","/api/v1/special-assemblies/build-one"}:return False
    from .server import HttpError
    if method!="POST":raise HttpError(405,"method_not_allowed","Сборки специальных изделий принимают POST")
    if path.endswith("/plan"):
        request=SpecialPlanRequest.model_validate(handler.body())
        result=special_plan(request.project,request.scope,request.configuration,model_library,request.replace_existing)
    else:
        request=SpecialBuildRequest.model_validate(handler.body())
        result=special_build_one(request.project,request.installation_id,model_library,source_store)
    handler.send_json(result)
    return True
