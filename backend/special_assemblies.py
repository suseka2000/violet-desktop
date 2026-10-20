"""Reserved, portable special-product installations and solid assembly records.

Configuration and derived geometry are separate: edits invalidate geometry,
never silently re-use a different door or light. Source geometry is pinned by
SHA, and generated product parts remain explicit project assumptions.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
import math
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from .cad_sections import SHA256, SectionSnapshot
from .domain.models import Identifier, Model, Name, Project
from .mount_assemblies import AssemblyStep, SourceModel, digest
from .special_recipes import ANGLE, recipe

POLICY="special-project-solids/1.0"
Length=Annotated[float,Field(gt=0,le=1000,allow_inf_nan=False,strict=True)]
Gap=Annotated[float,Field(ge=0,le=100,allow_inf_nan=False,strict=True)]


def _validate_loops(loops,component):
    if not isinstance(loops,list) or len(loops)!=component.closed_wire_count:raise ValueError("Контуры штриховки не соответствуют сечению")
    for loop in loops:
        if not isinstance(loop,list) or len(loop)<4:raise ValueError("Некорректный замкнутый контур")
        for point in loop:
            if not isinstance(point,list) or len(point)!=2 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or abs(v)>1e9 for v in point):raise ValueError("Некорректная точка сечения")
        if loop[0]!=loop[-1]:raise ValueError("Контур штриховки не замкнут")
    def segments(paths):
        return Counter(tuple(sorted((tuple(round(v,4) for v in a),tuple(round(v,4) for v in b)))) for path in paths for a,b in zip(path,path[1:]) if a!=b)
    if segments(loops)!=segments(component.paths):raise ValueError("Штриховка не соответствует реальным рёбрам сечения")


class DoorConfig(Model):
    frame_depth_mm:Length=100
    frame_section_mm:Length=35
    frame_wall_mm:Length=2
    leaf_thickness_mm:Length=40
    side_gap_mm:Length=3
    bottom_gap_mm:Gap=10
    hinge_count:Annotated[int,Field(strict=True,ge=2,le=4)]=3


class HatchConfig(Model):
    frame_depth_mm:Length=60
    frame_section_mm:Length=25
    leaf_thickness_mm:Length=20
    gap_mm:Length=3


class LightConfig(Model):
    location:Literal["above_doors","wall_top"]="above_doors"
    housing_width_mm:Length=18
    housing_depth_mm:Length=12
    wall_mm:Length=1.5
    diffuser_thickness_mm:Length=1.5
    above_door_offset_mm:Gap=20


class SpecialConfiguration(Model):
    allow_project_assumptions:Literal[True]
    kinds:list[Literal["concealed_door","hatch","lighting"]]=Field(default_factory=lambda:["concealed_door","hatch"],min_length=1,max_length=3)
    door:DoorConfig=Field(default_factory=DoorConfig)
    hatch:HatchConfig=Field(default_factory=HatchConfig)
    lighting:LightConfig=Field(default_factory=LightConfig)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.kinds))!=len(self.kinds):raise ValueError("Типы специальных изделий не должны повторяться")
        return self

    @model_validator(mode="before")
    @classmethod
    def explicit_consent(cls,value):
        if isinstance(value,dict) and value.get("allow_project_assumptions") is not True:
            raise ValueError("Подтвердите применение проектных параметрических деталей")
        return value


class Installation(Model):
    id:SHA256
    kind:Literal["concealed_door","hatch","lighting"]
    label:Name
    wall_id:Identifier
    face_id:Identifier
    opening_id:Identifier|None=None
    floor_id:Identifier
    space_id:Identifier|None=None
    configuration:dict[str,JsonValue]
    target_geometry:dict[str,JsonValue]
    geometry_signature:SHA256
    source_models:list[SourceModel]=Field(default_factory=list,max_length=8)
    source_refs:list[dict[str,JsonValue]]=Field(default_factory=list,max_length=20)

    @model_validator(mode="after")
    def coherent(self):
        SpecialConfiguration.model_validate(self.configuration)
        expected=installation_id(self.kind,self.wall_id,self.face_id,self.opening_id)
        if self.id!=expected:raise ValueError("Идентификатор изделия не соответствует его месту")
        if len({source.source_id for source in self.source_models})!=len(self.source_models):raise ValueError("Исходники специальной сборки повторяются")
        if any(source.source_id!=ANGLE["source_id"] for source in self.source_models):raise ValueError("Для специальной сборки нет калибровки этого STEP")
        return self


class Installations(Model):
    schema_version:Literal["special-installations/1.0"]="special-installations/1.0"
    items:list[Installation]=Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        if len({item.id for item in self.items})!=len(self.items):raise ValueError("Место установки специального изделия повторено")
        return self


class ProductSection(Model):
    id:Identifier
    label:Name
    geometry_snapshot:SectionSnapshot
    bodies:list[dict[str,JsonValue]]=Field(default_factory=list,max_length=32)


class SpecialAssembly(Model):
    id:SHA256
    installation_id:SHA256
    label:Name
    kind:Literal["concealed_door","hatch","lighting"]
    configuration:dict[str,JsonValue]
    target_signature:SHA256
    source_models:list[SourceModel]=Field(default_factory=list,max_length=8)
    source_refs:list[dict[str,JsonValue]]=Field(default_factory=list,max_length=20)
    assumptions:list[Annotated[str,Field(min_length=1,max_length=3000)]]=Field(min_length=1,max_length=40)
    spec:dict[str,JsonValue]
    bodies:list[dict[str,JsonValue]]=Field(min_length=1,max_length=32)
    materials:list[dict[str,JsonValue]]=Field(min_length=1,max_length=32)
    assembled_step:AssemblyStep
    sections:list[ProductSection]=Field(min_length=1,max_length=4)
    collisions:list[dict[str,JsonValue]]=Field(default_factory=list,max_length=496)
    approval_status:Literal["unreviewed"]="unreviewed"
    fingerprint:SHA256

    @model_validator(mode="after")
    def coherent(self):
        from .assembly_geometry import validate_assembly_spec
        spec=validate_assembly_spec(self.spec)
        if self.collisions:raise ValueError("Сборка с объёмными коллизиями не сохраняется")
        if spec["collision_tolerance_mm3"]!=.001:raise ValueError("Допуск проверки специальных сборок не изменяется")
        sources={s.source_id for s in self.source_models}
        if {p["source_id"] for p in spec["placements"]}!=sources:raise ValueError("Состав исходных STEP не соответствует рецепту")
        expected=[(item,component) for item in spec["placements"] for component in item["component_ids"]]+[(item,None) for item in spec["boxes"]]
        if len(expected)!=len(self.bodies):raise ValueError("Состав тел не соответствует рецепту специальной сборки")
        for index,(body,(item,component)) in enumerate(zip(self.bodies,expected),1):
            if body.get("id")!=index or body.get("instance_id")!=item["id"] or body.get("rotation")!=item["rotation"] or body.get("translation_mm")!=item["translation_mm"]:
                raise ValueError("Положение тела не соответствует рецепту")
            if component is not None:
                if body.get("source_id")!=item["source_id"] or body.get("source_component_id")!=component or body.get("kind")!="step":raise ValueError("STEP-тело не соответствует исходнику")
            elif body.get("kind")!="generated_box" or body.get("size_mm")!=item["size_mm"] or body.get("cutouts",[])!=item.get("cutouts",[]) or body.get("role")!=item["role"]:
                raise ValueError("Параметрическое тело не соответствует рецепту")
        ids=[body["id"] for body in self.bodies]
        primary=self.sections[0].geometry_snapshot.request
        if primary.axis!=spec["section"]["axis"] or primary.offset_mm!=spec["section"]["offset_mm"]:raise ValueError("Основное сечение не соответствует рецепту сборки")
        if len({s.id for s in self.sections})!=len(self.sections):raise ValueError("Сечения специального изделия повторяются")
        for section in self.sections:
            if section.geometry_snapshot.source_id!=self.assembled_step.source_id:raise ValueError("Разрез построен от другого STEP")
            if section.geometry_snapshot.result.byte_length!=self.assembled_step.byte_length:raise ValueError("Размер STEP разреза не совпадает")
            if [c.id for c in section.geometry_snapshot.result.components]!=ids:raise ValueError("Состав разреза не соответствует сборке")
            if section.geometry_snapshot.result.point_count==0:raise ValueError("Сохранён пустой разрез специального изделия")
            if any(c.open_wire_count for c in section.geometry_snapshot.result.components):raise ValueError("Разрез содержит незамкнутую геометрию")
            if section.bodies:
                if [b.get("id") for b in section.bodies]!=ids:raise ValueError("Тела штриховки не соответствуют разрезу")
                for body,component in zip(section.bodies,section.geometry_snapshot.result.components):_validate_loops(body.get("section_loops"),component)
        for body,component in zip(self.bodies,self.sections[0].geometry_snapshot.result.components):_validate_loops(body.get("section_loops"),component)
        if {row.get("instance_id") for row in self.materials}!={body["instance_id"] for body in self.bodies}:raise ValueError("Ведомость материалов не соответствует составу")
        expected_id=digest({"installation_id":self.installation_id,"target_signature":self.target_signature,"spec":self.spec,"source_models":[s.model_dump(mode="json") for s in self.source_models],"assumptions":self.assumptions})
        if self.id!=expected_id:raise ValueError("Идентификатор специальной сборки не соответствует составу и месту")
        value=self.model_dump(mode="json",by_alias=True);fingerprint=value.pop("fingerprint")
        if fingerprint!=digest(value):raise ValueError("Контрольная сумма специальной сборки не совпадает")
        return self


class SpecialAssemblies(Model):
    schema_version:Literal["special-assemblies/1.0"]="special-assemblies/1.0"
    # Building size must not change the validity of an individual product.
    # STEP, section integrity and recipe bounds are checked on every item;
    # public request/project byte budgets remain enforced at their boundaries.
    items:list[SpecialAssembly]=Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        if len({i.installation_id for i in self.items})!=len(self.items):raise ValueError("Для одной установки сохранено несколько сборок")
        return self


def installation_id(kind,wall_id,face_id,opening_id):
    # A physical opening owns one door/hatch, regardless of the room used to
    # select it. Lighting at a face is separate on the two sides of a wall.
    return digest({"kind":kind,"wall_id":wall_id,"face_id":face_id if kind=="lighting" else None,"opening_id":opening_id})


def find_target(project,installation):
    for building in project.buildings:
        for floor in building.floors:
            wall=next((w for w in floor.walls if w.id==installation.wall_id),None)
            if wall is None:continue
            face=next((f for f in wall.faces if f.id==installation.face_id),None)
            opening=next((o for o in floor.openings if o.id==installation.opening_id and o.wall_id==wall.id),None) if installation.opening_id else None
            if face is None or installation.opening_id and opening is None:raise ValueError("Место специальной установки удалено")
            if floor.id!=installation.floor_id or face.space_id!=installation.space_id:raise ValueError("Привязка специальной установки к этажу/помещению не совпадает")
            if installation.kind in {"concealed_door","hatch"}:
                if opening is None or opening.kind!={"concealed_door":"door","hatch":"hatch"}[installation.kind]:raise ValueError("Тип специального изделия не соответствует проёму")
                if installation.kind=="concealed_door" and (opening.door is None or not opening.door.concealed_frame):raise ValueError("Для двери сначала выберите скрытую коробку в свойствах проёма")
            elif installation.configuration["lighting"]["location"]=="above_doors":
                if opening is None or opening.kind!="door":raise ValueError("Подсветка над дверью требует дверного проёма")
            elif opening is not None:raise ValueError("Подсветка верха стены не привязывается к проёму")
            return floor,wall,face,opening
    raise ValueError("Стена специальной установки удалена")


def target_geometry(floor,wall,face,opening,kind,configuration,project=None):
    length=math.hypot(wall.end.x-wall.start.x,wall.end.y-wall.start.y)
    width=float(opening.width_mm if opening else length)
    height=float(opening.height_mm if opening else wall.height_mm)
    if opening and (opening.offset_mm+width>length+.001 or opening.sill_mm+height>wall.height_mm+.001):raise ValueError("Проём выходит за границы стены; исправьте его размеры до построения изделия")
    if width>30000 or height>6000:raise ValueError("Специальное изделие ограничено шириной 30 м и высотой 6 м")
    if kind in {"concealed_door","hatch"} and width>3000:raise ValueError("Полотно двери/люка ограничено шириной 3000 мм")
    if kind=="lighting" and width<60:raise ValueError("Линия подсветки должна быть не короче 60 мм")
    result={"width_mm":width,"height_mm":height,"wall_thickness_mm":wall.thickness_mm,
            "wall_base_mm":wall.base_mm,"view_side":face.side,
            "offset_mm":float(opening.offset_mm) if opening else 0.,"sill_mm":float(opening.sill_mm) if opening else 0.}
    if opening and opening.door:
        result.update(opening.door.model_dump(mode="json"))
        if face.side=="right" and result["handing"] in {"left","right"}:result["handing"]="right" if result["handing"]=="left" else "left"
    if kind=="lighting":
        depth=configuration["lighting"]["housing_depth_mm"]
        offset=configuration["lighting"].get("above_door_offset_mm",20)
        if opening and offset<20:raise ValueError("Для текущего накладного корпуса над дверью нужен отступ не менее 20 мм под кронштейн")
        bottom=opening.sill_mm+opening.height_mm+offset if opening else wall.height_mm-depth
        if bottom+depth>wall.height_mm+.001:raise ValueError("Над проёмом нет места для корпуса подсветки")
        result.update(installation_bottom_mm=bottom,front_projection_mm=max(20.,configuration["lighting"]["housing_width_mm"]))
    if project is not None and kind in {"concealed_door","hatch"}:
        mounting=project.properties.get("wall_mounting",{})
        bindings=mounting.get("walls",[]) if isinstance(mounting,dict) else []
        binding=next((b for b in bindings if isinstance(b,dict) and b.get("wall_id")==wall.id and b.get("face_id")==face.id),None)
        if binding:
            from .wall_mount import MountConfiguration
            panel=MountConfiguration.model_validate(binding["configuration"])
            offset=opening.offset_mm if face.side=="left" else length-opening.offset_mm-opening.width_mm
            left=max(0.,min(150.,offset));right=max(0.,min(150.,length-offset-width))
            head=max(0.,min(150.,wall.height_mm-opening.sill_mm-height))
            # Context fragments must never fill a neighbouring real opening.
            for other in floor.openings:
                if other.id==opening.id or other.wall_id!=wall.id:continue
                ox=other.offset_mm if face.side=="left" else length-other.offset_mm-other.width_mm
                if max(opening.sill_mm,other.sill_mm)<min(opening.sill_mm+height,other.sill_mm+other.height_mm):
                    if ox+other.width_mm<=offset:left=min(left,offset-ox-other.width_mm)
                    elif ox>=offset+width:right=min(right,ox-offset-width)
                    else:raise ValueError("Проёмы пересекаются; исправьте их до построения изделия")
                if max(offset,ox)<min(offset+width,ox+other.width_mm) and other.sill_mm>=opening.sill_mm+height:
                    head=min(head,other.sill_mm-opening.sill_mm-height)
            result["panel_context"]={"thickness_mm":panel.panel_thickness_mm,"material":panel.material,
                "left_width_mm":left,"right_width_mm":right,"head_height_mm":head}
    return result


def target_signature(project,installation):
    floor,wall,face,opening=find_target(project,installation)
    geometry=target_geometry(floor,wall,face,opening,installation.kind,installation.configuration,project)
    return digest({"policy":POLICY,"configuration":installation.configuration,"target_geometry":geometry,
                   "wall":wall.model_dump(mode="json",exclude={"name","properties"}),"opening":opening.model_dump(mode="json",exclude={"name","properties"}) if opening else None,
                   "floor_elevation_mm":floor.elevation_mm,"floor_origin":floor.origin.model_dump(mode="json"),
                   "source_models":[s.model_dump(mode="json") for s in installation.source_models]})


def validate_special(project, *, derived_assemblies=False):
    from .workspace import validate_project_properties_budget
    validate_project_properties_budget(project,derived_assemblies=derived_assemblies)
    installations=Installations.model_validate(project.properties.get("special_installations",{}))
    assemblies=SpecialAssemblies.model_validate(project.properties.get("special_assemblies",{}))
    for item in installations.items:find_target(project,item)
    by_id={item.id:item for item in installations.items}
    for item in assemblies.items:
        if item.installation_id not in by_id:raise ValueError("Сборка ссылается на удалённую специальную установку")
    return installations,assemblies


def special_document_entries(project, *, derived_assemblies=False):
    installations,assemblies=validate_special(project,derived_assemblies=derived_assemblies)
    result=[]
    for installation in installations.items:
        record=next((r for r in assemblies.items if r.installation_id==installation.id),None)
        try:
            current=target_signature(project,installation)==installation.geometry_signature
            current=current and installation.target_geometry==target_geometry(*find_target(project,installation),installation.kind,installation.configuration,project)
            if record and current and record.target_signature==installation.geometry_signature:
                descriptor=recipe(installation)
                current=(record.spec==descriptor["spec"] and record.assumptions==descriptor["assumptions"]
                         and record.materials==descriptor["materials"] and record.configuration==installation.configuration
                         and record.source_models==installation.source_models and record.source_refs==installation.source_refs
                         and [{"id":s.id,"label":s.label,"axis":s.geometry_snapshot.request.axis,"offset_mm":s.geometry_snapshot.request.offset_mm} for s in record.sections]==descriptor["sections"])
        except ValueError:current=False
        status="ready" if record and current and record.target_signature==installation.geometry_signature else "stale" if record else "not_built"
        result.append({"id":installation.id,"label":installation.label,"kind":installation.kind,"wall_id":installation.wall_id,"face_id":installation.face_id,
                       "opening_id":installation.opening_id,"floor_id":installation.floor_id,"space_id":installation.space_id,
                       "status":status,"installation":installation.model_dump(mode="json"),
                       "assembly":record.model_dump(mode="json",by_alias=True) if status=="ready" else None,
                       "message":"" if status=="ready" else "Геометрия или параметры изменились; постройте специальное изделие заново." if record else "Установка настроена, твёрдотельная сборка ещё не построена."})
    return result


def special_report(project, *, derived_assemblies=False):
    entries=special_document_entries(project,derived_assemblies=derived_assemblies)
    return {"schema_version":"special-report/1.0","entries":entries,"production_ready":False,
            "ready_count":sum(e["status"]=="ready" for e in entries),"pending_count":sum(e["status"]!="ready" for e in entries),
            "limitations":["Параметрические изделия являются проектными решениями, не продукцией утверждённого производителя.","Фурнитура представлена габаритами: кинематика, крепёж, расчёт нагрузок и электротехнические требования не подтверждены."]}


def clone_special_properties(original,cloned,mapping):
    """Remap model references; exact STEP bytes stay unchanged, stale stays stale."""
    installations,records=validate_special(original)
    ready={e["id"] for e in special_document_entries(original) if e["status"]=="ready"}
    remapped={}
    for item in installations.items:
        value=item.model_dump(mode="json")
        for key in ("wall_id","face_id","opening_id","floor_id","space_id"):
            if value[key] is not None:value[key]=mapping.get(value[key],value[key])
        value["id"]=installation_id(value["kind"],value["wall_id"],value["face_id"],value["opening_id"])
        new=Installation.model_validate(value)
        # A configured but unbuilt, unchanged installation is also portable.
        try:
            if target_signature(original,item)==item.geometry_signature:new.geometry_signature=target_signature(cloned,new)
        except ValueError:pass  # Preserve an already stale installation in a copy.
        remapped[item.id]=new
    cloned.properties["special_installations"]=Installations(items=list(remapped.values())).model_dump(mode="json")
    result=[]
    for record in records.items:
        value=record.model_dump(mode="json",by_alias=True)
        new=remapped[record.installation_id]
        value["installation_id"]=new.id
        if record.installation_id in ready:value["target_signature"]=new.geometry_signature
        value["id"]=digest({key:value[key] for key in ("installation_id","target_signature","spec","source_models","assumptions")})
        value.pop("fingerprint",None);value["fingerprint"]=digest(value)
        result.append(value)
    if "special_assemblies" in original.properties:
        cloned.properties["special_assemblies"]={"schema_version":"special-assemblies/1.0","items":result}
    validate_special(cloned)
