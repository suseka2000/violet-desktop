"""Opt-in automatic component cuts, never fictitious 3D mounting assemblies.

The pure plan uses pinned supplier/model hashes. Building one selected cut may
cache its exact original in the content-addressed store, but never saves a
project. The caller applies all returned assets in a single guarded edit.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re
import time
import unicodedata

from ..cad_sections import MAX_PROJECT_SECTION_POINTS, make_section_snapshot
from ..domain.models import Model, Project
from ..mount_defaults import room_geometry
from ..vendor_design import _context_hash
from ..vendor_library import Digest
from ..wall_mount import MountScope, _filename_matches, pinned_step_assets, preview_wall_mount, validate_wall_mounting
from ..workspace import validate_cad_assets

POLICY = "profile-midsection/1.0"
MAX_MODELS = 8
# Explicit profile names, not hardware, panel-edge codes or generic L/F parts.
# Presence here only permits a geometric trial; the source must still match a
# selected rule, a pinned filename and the inspected single-solid conditions.
PROFILE_CODES = frozenset({
    "V-Ang", "V-Ang 45", "V-Ang-Start", "V-Base", "V-Base light",
    "V-Cap1", "V-Cap2", "V-Corn-10", "V-Corn-15", "V-Corn-20", "V-Corn-25",
    "V-Facet", "V-HSIF", "V-L-Start", "V-L8", "V-L10", "V-L13", "V-L13-2", "V-L13-Smart",
    "V-LED", "V-PW", "V-SH", "V-Sky", "V-Spike", "V-Start", "V-Start 0-1", "V-Start O",
    "V-Start O-1", "V-Start O-2", "V-Start-0-2", "V-Start-1", "V-Start-2", "V-Start-3",
    "V-Start-4", "V-Start-5", "V-T-Ang 90", "V-T1", "V-T2", "V-T3", "V-WS", "V-WT",
    "VH-1", "VH-2", "VH-3", "VH-4",
})
WARNING = "Это реальное геометрическое сечение отдельного STEP-компонента, не сборка узла. Имя файла, вариант и автоматическую плоскость должен проверить проектировщик."


class SectionPlanRequest(Model):
    project: Project
    scope: MountScope


class SectionBuildRequest(SectionPlanRequest):
    source_id: Digest


def automatic_section_id(source_id):
    return "auto-profile-" + source_id


def _targets(project, scope):
    from ..wall_mount import mount_scope_targets
    selected=mount_scope_targets(project,scope)
    return {(target["wall"].id,target["face"].id) for target in selected["targets"]}


def _filename_rank(match):
    stem=unicodedata.normalize("NFC",Path(match["filename"]).stem)
    variant=re.search(r"_(\d+)$",stem)
    return (variant is not None,int(variant[1]) if variant else 0,stem.casefold(),match["source_id"])


def section_plan(project, scope):
    scope=scope if isinstance(scope,MountScope) else MountScope.model_validate(scope)
    saved=validate_wall_mounting(project)
    if saved is None:
        raise ValueError("Сначала примените монтаж стен к проекту")
    targets=_targets(project,scope)
    cad=validate_cad_assets(project.properties.get("cad_assets",[]))
    complete={(a.source_id,s.id) for a in cad for s in a.sections if s.geometry_snapshot and s.geometry_snapshot.result.point_count}
    assets=pinned_step_assets(saved.snapshot)
    # The matching helper returns the first matching alias. Prefer the explicit
    # unsuffixed alias before matching; never conflate two different digests.
    for asset in assets:
        asset["aliases"].sort(key=lambda a:_filename_rank({**a,"source_id":asset["source_id"]}))
    refs={};skipped=[]
    for binding in saved.walls:
        if (binding.wall_id,binding.face_id) not in targets:
            continue
        try:
            view=preview_wall_mount(project,binding.wall_id,binding.face_id,binding.configuration,saved.snapshot,assets)
        except ValueError as error:
            skipped.append({"wall_id":binding.wall_id,"face_id":binding.face_id,"reason":"layout_invalid","message":str(error)})
            continue
        if binding.geometry_signature!=view["geometry_signature"]:
            skipped.append({"wall_id":binding.wall_id,"face_id":binding.face_id,"reason":"mounting_stale","message":"Монтаж изменённой стены надо пересчитать перед выбором сечений."})
            continue
        chosen=next((c for c in view["candidates"] if c["rule_id"]==view["selected_rule_id"]),None)
        if not chosen:
            skipped.append({"wall_id":binding.wall_id,"reason":"assembly_not_selected","message":"Нет выбранной совместимой сборки."})
            continue
        decisions=[{"component_codes":chosen["component_codes"],"rule_id":chosen["rule_id"]},*view["nodes"]]
        for decision in decisions:
            for code in decision.get("component_codes",[]):
                entry=refs.setdefault(code,{"rule_ids":set(),"wall_ids":set(),"face_ids":set()})
                if decision.get("rule_id"):
                    entry["rule_ids"].add(decision["rule_id"])
                entry["wall_ids"].add(binding.wall_id);entry["face_ids"].add(binding.face_id)
    proposed={}
    for code,refs_for_code in sorted(refs.items()):
        if code not in PROFILE_CODES:
            skipped.append({"component_code":code,"reason":"not_profile","message":"Не подтверждён конкретный профиль для автоматического поперечного сечения."})
            continue
        matches=sorted(_filename_matches(code,assets),key=_filename_rank)
        if not matches:
            skipped.append({"component_code":code,"reason":"no_pinned_model","message":"В зафиксированной библиотеке нет STEP с точным обозначением профиля."})
            continue
        best=matches[0]
        for variant in matches[1:]:
            skipped.append({"component_code":code,"source_id":variant["source_id"],"filename":variant["filename"],"reason":"alternate_variant","message":"Другой STEP-вариант оставлен для ручного выбора; взаимозаменяемость не подтверждена."})
        entry=proposed.setdefault(best["source_id"],{"source_id":best["source_id"],"filename":best["filename"],"component_codes":set(),"rule_ids":set(),"wall_ids":set(),"face_ids":set(),
            "selection_reason":"По точному обозначению в зафиксированной библиотеке; предпочтено имя без суффикса варианта, затем меньший номер и лексикографический порядок.","assumption":True,"manufacturer_confirmed":False})
        entry["component_codes"].add(code)
        for key in ("rule_ids","wall_ids","face_ids"):entry[key].update(refs_for_code[key])
    items=[]
    for source_id,entry in sorted(proposed.items(),key=lambda pair:(pair[1]["filename"].casefold(),pair[0])):
        if (source_id,automatic_section_id(source_id)) in complete:
            skipped.append({"source_id":source_id,"filename":entry["filename"],"reason":"already_present","message":"Автоматическое сечение этой точной модели уже сохранено."})
        elif len(items)>=MAX_MODELS:
            skipped.append({"source_id":source_id,"filename":entry["filename"],"reason":"model_limit","message":"За один запуск готовится не более 8 разных моделей."})
        else:
            items.append({k:sorted(v) if isinstance(v,set) else v for k,v in entry.items()})
    return {"schema_version":"mount-section-plan/1.0","policy":POLICY,"scope":scope.model_dump(mode="json"),
            "context_signature":_context_hash({"project":project.model_dump(mode="json"),"scope":scope.model_dump(mode="json"),"policy":POLICY}),
            "snapshot_id":saved.snapshot.snapshot_id,"items":items,"skipped":skipped,"warnings":[WARNING],
            "limits":{"max_models":MAX_MODELS,"max_project_points":MAX_PROJECT_SECTION_POINTS,"max_request_seconds":60},"approval_status":"unreviewed"}


def section_build_one(project, scope, source_id, model_library, source_store):
    deadline=time.monotonic()+60
    plan=section_plan(project,scope)
    item=next((item for item in plan["items"] if item["source_id"]==source_id),None)
    if item is None:
        already=next((s for s in plan["skipped"] if s.get("source_id")==source_id and s["reason"]=="already_present"),None)
        if already:
            return {"status":"skipped","source_id":source_id,"reason":"already_present","warnings":[already["message"]]}
        raise ValueError("Модель не входит в текущий план сечений выбранной области")
    try:
        content=source_store.get_bytes(source_id)
    except FileNotFoundError:
        content,_=model_library.original(source_id)
    if hashlib.sha256(content).hexdigest()!=source_id:
        raise ValueError("SHA исходной модели не совпадает с зафиксированным снимком")
    from .cad_assets import CAD_SLOTS
    from .server import HttpError
    from ..step_worker import run_step_inspect, run_step_section
    if not CAD_SLOTS.acquire(blocking=False):
        raise HttpError(429,"cad_busy","Заняты обработчики CAD. Дождитесь завершения текущей операции.")
    try:
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise ValueError("Исчерпан минутный бюджет подготовки сечения")
        inspection=run_step_inspect(content,timeout_seconds=min(30,remaining))
        components=inspection.get("components",[])
        if inspection.get("source_id")!=source_id:
            raise ValueError("Инспекция STEP относится к другому исходнику")
        reason=None
        if inspection.get("component_count")!=1 or len(components)!=1 or components[0].get("valid") is not True:
            reason="Автоматически разрезается только одно корректное твёрдое тело; сборку или проблемную модель откройте вручную."
        bounds=components[0].get("bounds_mm") if len(components)==1 else None
        sizes=bounds.get("size",[]) if bounds else []
        if not reason and (len(sizes)!=3 or not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>0 for v in sizes)):
            reason="Нет корректного трёхмерного габарита для автоматической плоскости."
        if not reason:
            axis_index=max(range(3),key=lambda i:sizes[i])
            if sizes[axis_index]<3*max(v for i,v in enumerate(sizes) if i!=axis_index):
                reason="Нет явно вытянутой оси профиля (требуется отношение не меньше 3:1); выберите плоскость вручную."
        if reason:
            return {"status":"skipped","source_id":source_id,"reason":"manual_plane_required","warnings":[reason,WARNING]}
        midpoint=(bounds["min"][axis_index]+bounds["max"][axis_index])/2
        if abs(midpoint)<1e-9:
            midpoint=0.0  # suppress numeric fuzz around the actual origin plane
        request={"axis":"xyz"[axis_index],"offset_mm":midpoint,"component_ids":[components[0]["id"]]}
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise ValueError("Исчерпан минутный бюджет построения сечения")
        result=run_step_section(content,**request,timeout_seconds=min(30,remaining))
        snapshot=make_section_snapshot(source_id,request,result)
        if not result["point_count"]:
            return {"status":"skipped","source_id":source_id,"reason":"empty_section","warnings":["Автоматическая плоскость дала пустое сечение; выберите её вручную.",WARNING]}
        if any(c["open_wire_count"] for c in result["components"]):
            return {"status":"skipped","source_id":source_id,"reason":"open_section","warnings":["Получены незамкнутые контуры; сечение требует ручной проверки.",WARNING]}
        scope=MountScope.model_validate(plan["scope"])
        section={"id":automatic_section_id(source_id),"label":"Профиль "+" / ".join(item["component_codes"])+" — автоматическое сечение",
                 **request,"note":WARNING+" "+item["selection_reason"]+" Плоскость через середину длинной оси габарита; это геометрическая эвристика, а не подтверждение постоянного сечения. Правила: "+", ".join(item["rule_ids"]),
                 "geometry_snapshot":snapshot,"target_space_id":scope.id if scope.kind=="space" else None}
        raw=deepcopy(project.properties.get("cad_assets",[]))
        asset=next((a for a in raw if a["source_id"]==source_id),None)
        if asset is None:
            asset={"source_id":source_id,"filename":item["filename"],"format":"step","sections":[]};raw.append(asset)
        sections=asset.setdefault("sections",[])
        if any(s["id"]==section["id"] for s in sections):
            # Never overwrite an incomplete or manually edited same-ID section.
            return {"status":"skipped","source_id":source_id,"reason":"existing_section_id","warnings":["Этот ID сечения уже есть; удалите или обновите его вручную."]}
        sections.append(section)
        validate_cad_assets(raw)
        # Only exact immutable original bytes are cached, never the project.
        source_store.put(item["filename"],content)
        return {"status":"ready","source_id":source_id,"asset":asset,"section":section,"scope":plan["scope"],"policy":POLICY,
                "selection":item,"warnings":[WARNING],"approval_status":"unreviewed"}
    finally:
        CAD_SLOTS.release()


def handle_mount_sections(handler, method, path, model_library, source_store):
    if path not in {"/api/v1/wall-mount/section-plan","/api/v1/wall-mount/section-build-one"}:
        return False
    from .server import HttpError
    if method!="POST":
        raise HttpError(405,"method_not_allowed","Подготовка сечений принимает POST")
    if path.endswith("/section-plan"):
        request=SectionPlanRequest.model_validate(handler.body())
        result=section_plan(request.project,request.scope)
    else:
        request=SectionBuildRequest.model_validate(handler.body())
        result=section_build_one(request.project,request.scope,request.source_id,model_library,source_store)
    handler.send_json(result)
    return True
