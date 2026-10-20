"""Transparent preliminary-design decisions, separate from supplier evidence.

No rule here approves a fixing, infers a fabrication dimension, or places STEP
solids as an assembly. Project assumptions are opt-in and remain exportable.
"""
from __future__ import annotations

import math

from .geometry.validation import _point_in_polygon, _point_segment_distance, _segment_intersection_kind, _signed_polygon_area
from .vendor_design import _source

POLICY = "preliminary-room/1.0"
FAMILIES = {"V-T1", "V-T2", "V-Base", "VH-1", "VH-2", "VH-3", "VH-4", "V-Spike"}


def room_geometry(floor, space, tolerance):
    """Resolve a simple, ordered room and its interior faces, CW or CCW.

    Requiring boundary membership and the correct face prevents accidentally
    finishing the exterior side. Additional partitions are resolved separately.
    """
    if not 3 <= len(space.boundary) <= 1000:
        raise ValueError("Контур помещения должен содержать 3–1000 сторон")
    by_id = {w.id:w for w in floor.walls}
    if len(by_id) != len(floor.walls):
        raise ValueError("Повторяются идентификаторы стен этажа")
    segments, walls = [], []
    for edge in space.boundary:
        wall = by_id.get(edge.wall_id)
        if wall is None:
            raise ValueError("В контуре помещения отсутствует стена")
        a,b = (wall.end,wall.start) if edge.reversed else (wall.start,wall.end)
        if math.hypot(b.x-a.x,b.y-a.y) <= tolerance:
            raise ValueError("Контур содержит стену нулевой длины")
        segments.append(((a.x,a.y),(b.x,b.y)));walls.append(wall)
    if len({w.id for w in walls}) != len(walls):
        raise ValueError("Одна стена повторяется в контуре помещения")
    for (_,end),(start,_) in zip(segments,segments[1:]+segments[:1]):
        if math.hypot(end[0]-start[0],end[1]-start[1]) > tolerance:
            raise ValueError("Контур помещения не замкнут по порядку стен")
    vertices = [a for a,b in segments]
    area = _signed_polygon_area(vertices)
    if abs(area) <= tolerance*tolerance:
        raise ValueError("Контур помещения имеет нулевую площадь")
    for i,(a,b) in enumerate(segments):
        for j in range(i+1,len(segments)):
            if j in {i+1} or (i==0 and j==len(segments)-1):
                continue
            if _segment_intersection_kind(a,b,*segments[j],tolerance) != "none":
                raise ValueError("Контур помещения пересекает или касается сам себя")
    faces = []
    for edge,wall in zip(space.boundary,walls):
        side = "left" if (area > 0) != edge.reversed else "right"
        matches = [f for f in wall.faces if f.space_id==space.id and f.side==side]
        if len(matches)!=1:
            raise ValueError("Внутренняя сторона стены не согласована с направлением контура помещения")
        faces.append(matches[0])
    return {"vertices":vertices,"walls":walls,"faces":faces,"signed_area_mm2":area,"space_id":space.id}


def wall_room_relation(wall, room, tolerance):
    """Classify the entire reference segment, never just its centre.

    Endpoints may meet the room boundary (a T junction). Collinear duplicates
    and a segment which leaves a concave room are not internal partitions.
    No inside/outside *corner* or physical offset dimension is inferred here.
    """
    a, b = (wall.start.x, wall.start.y), (wall.end.x, wall.end.y)
    dx, dy = b[0]-a[0], b[1]-a[1]
    length = math.hypot(dx, dy)
    if length <= tolerance:
        return "degenerate"
    vertices = room["vertices"]
    parameters = {0., 1.}
    touching = False
    for c, d in zip(vertices, vertices[1:]+vertices[:1]):
        kind = _segment_intersection_kind(a, b, c, d, tolerance)
        if kind == "collinear_overlap":
            return "boundary_overlap"
        if kind == "proper":
            return "crosses_boundary"
        touching = touching or kind == "touch"
        # Vertex touches need interval checks: a concave notch can be crossed
        # through its vertices without a proper edge intersection.
        if _point_segment_distance(c, a, b) <= tolerance:
            parameters.add(max(0., min(1., ((c[0]-a[0])*dx+(c[1]-a[1])*dy)/(length*length))))
    ordered = sorted(parameters)
    inside = [_point_in_polygon((a[0]+dx*(lo+hi)/2, a[1]+dy*(lo+hi)/2), vertices, tolerance)
              for lo, hi in zip(ordered, ordered[1:]) if (hi-lo)*length > tolerance]
    endpoints = [_point_in_polygon(p, vertices, tolerance) for p in (a, b)]
    if inside and all(value == 1 for value in inside) and all(value != 0 for value in endpoints):
        return "inside"
    return "outside" if not touching and all(value == 0 for value in [*inside, *endpoints]) else "crosses_boundary"


def classified_corner(room, point, tolerance):
    vertices = room["vertices"]
    indices = [i for i,p in enumerate(vertices) if math.hypot(p[0]-point.x,p[1]-point.y)<=tolerance]
    if len(indices)!=1:
        return None
    i=indices[0];a,b,c=vertices[i-1],vertices[i],vertices[(i+1)%len(vertices)]
    incoming=(b[0]-a[0],b[1]-a[1]);outgoing=(c[0]-b[0],c[1]-b[1])
    turn=math.degrees(math.atan2(incoming[0]*outgoing[1]-incoming[1]*outgoing[0],incoming[0]*outgoing[0]+incoming[1]*outgoing[1]))
    oriented=turn*(1 if room["signed_area_mm2"]>0 else -1)
    interior=180-oriented
    kind="internal_corner" if oriented>1e-7 else "external_corner" if oriented< -1e-7 else "straight"
    short_length=min(math.hypot(*incoming),math.hypot(*outgoing))
    angular_tolerance=math.degrees(math.atan2(tolerance,short_length))
    return {"corner_type":kind,"interior_angle_deg":round(interior,6),"right_angle":abs(abs(oriented)-90)<=angular_tolerance+1e-9,
            "classification_basis":"simple_room_winding","space_id":room["space_id"],
            "boundary_wall_ids":[room["walls"][i-1].id,room["walls"][i].id]}


def select_assembly(config, candidates):
    requested = next((c for c in candidates if c["rule_id"]==config.assembly_rule_id),None)
    if config.assembly_rule_id:
        if requested is None:
            raise ValueError("Выбранная сборка отсутствует в снимке методичек")
        if requested["status"]=="incompatible":
            return None,"incompatible","Явно выбранная сборка противоречит известным условиям; она не назначена."
        if requested["status"]=="unknown" and not config.allow_project_assumptions:
            return None,"unknown","Выбранной сборке не хватает подтверждённых данных; строгий режим не назначает её."
        return requested,"explicit","Сборка выбрана пользователем; известные противоречия не обнаружены."
    compatible = [c for c in candidates if c["status"]=="candidate"]
    if not config.allow_project_assumptions:
        return (compatible[0],"unique_candidate","Единственный вариант, совпадающий по известным параметрам.") if len(compatible)==1 else (None,"choice_required","Нет единственного подтверждённого по известным параметрам варианта; требуется выбор.")
    # Unknown geometry/substrate of a special/removable/curved system is not a
    # sensible default for a simple room. A flat-wall missing joint can remain
    # an explicitly recorded preliminary assumption, never a source value.
    pool = compatible or [c for c in candidates if c["status"]=="unknown" and "flat_wall" in c["contexts"] and c["source"]["source_status"]=="verified"]
    if not pool:
        return None,"no_compatible_candidate","Ни одна сборка не прошла известные условия; проектные допущения не отменяют несовместимость."
    def rank(c):
        return (c["status"]!="candidate","lighting" in c["contexts"],"removable_panel" in c["contexts"],len(c["component_codes"]),c["source"]["document_id"],c["source"]["page"],c["rule_id"])
    chosen = min(pool,key=rank)
    reason="Предварительный выбор: сначала совпадение известных размеров, затем вариант без подсветки/съёмных панелей и с меньшим составом; при равенстве - порядок документа и страницы."
    return chosen,"project_default",reason


def assumption(id, message, required_check, source_refs=None):
    return {"id":id,"kind":"project_assumption","message":message,"required_check":required_check,
            "source_refs":source_refs or [],"manufacturer_confirmed":False}


def source_reference(rule, documents, role="assembly"):
    return {"rule_id":rule.id,"title":rule.title,"scope":rule.scope,"requirement":rule.requirement,
            "reference_kind":role,**_source(rule,documents)}


def _profile_family(codes):
    return set(codes).intersection(FAMILIES)


def node_decisions(config, snapshot, chosen, nodes, selection_status):
    """Resolve known contexts and label adaptations instead of inventing data."""
    documents={d.id:d for d in snapshot.documents}
    by_id={r.id:r for r in snapshot.rules}
    selected_rule=by_id.get(chosen["rule_id"]) if chosen else None
    family=_profile_family(chosen["component_codes"]) if chosen else set()
    assumptions=[]
    if config.allow_project_assumptions:
        assumptions.extend([
            assumption("material-applicability","Отделка и толщина приняты как проектное задание, а не как подтверждённая изготовителем совместимость.","Проверить паспорт панели, материал основания, пожарные требования и допустимую массу."),
            assumption("edge-allowances","Зазоры у пола/потолка и крайние подрезки приняты из параметров проекта.","Увязать их с реальным полом, потолком, опорной полкой и торцевыми деталями; отметку полки не считать зазором панели."),
        ])
        if not config.panel_edge:
            assumptions.append(assumption("panel-edge","Для проверки открытой системы предварительно принята кромка V-0; для скрытой системы форма паза пока не назначена.","Подтвердить кромку и обработку конкретной панели перед выпуском рабочих узлов."))
        if chosen and selection_status=="project_default":
            assumptions.append(assumption("assembly-choice",f"Для предварительной документации выбран вариант {chosen['title']} по прозрачному приоритету, не как обязательная норма.","Подтвердить внешний вид, состав, основание и применимость сборки.",[source_reference(selected_rule,documents)]))
        if chosen and chosen["status"]=="unknown":
            missing="; ".join(c["message"] for c in chosen["checks"] if c["status"]=="unknown")
            assumptions.append(assumption("assembly-unknown-conditions","Сборка принята предварительно при незаданных условиях: "+missing,"Подтвердить все перечисленные условия до рабочих монтажных чертежей.",[source_reference(selected_rule,documents)]))
    for node in nodes:
        kind=node["kind"];edge=node.get("edge_context",{})
        context={"vertical_joint":"panel_joint","floor":"floor_junction","ceiling":"ceiling_junction"}.get(kind,kind)
        if kind in {"left_edge","right_edge"}:
            context=edge.get("corner_type") or "wall_end"
        source_rule=None;adapted=False;reason=""
        if selected_rule and chosen["status"]=="candidate" and context in selected_rule.contexts:
            source_rule=selected_rule
        else:
            eligible=[]
            for rule in snapshot.rules:
                contexts=set(rule.contexts)
                matching=context in contexts or (context.startswith("opening_jamb") and "opening_jamb" in contexts)
                analogy=bool(not matching and config.allow_project_assumptions and node.get("opening_kind")=="window" and context in {"opening_head","opening_sill"} and "window_opening" in contexts)
                if not (matching or analogy) or rule.source_status!="verified" or rule.system not in {config.mounting_system,"general"}:
                    continue
                if rule.kind!="detail" or rule.topic not in {"mounting_assembly","junction_detail","junction_reference","opening_detail","profile_geometry","profile_section"}:
                    continue
                if context.startswith("opening_"):
                    opening_context="window_opening" if node.get("opening_kind")=="window" else "door_opening" if node.get("opening_kind") in {"door","passage"} else None
                    if contexts.intersection({"window_opening","door_opening"}) and opening_context not in contexts:
                        continue
                other_family=_profile_family(rule.component_codes)
                # A different profile family is a known mismatch, not a
                # missing-data assumption. Catalog-only generic profiles may
                # support a conceptual adaptation but never a finished node.
                if family and other_family and not family.intersection(other_family):
                    continue
                special="removable_panel" in contexts or rule.topic in {"profile_geometry","profile_section"}
                is_corner=context in {"internal_corner","external_corner"}
                if is_corner and not edge.get("right_angle"):
                    continue
                direct_corner=is_corner and bool(family.intersection(other_family)) and not special and "panel_joint" not in contexts
                # Cross-assembly corners with matching family are a cited
                # reference. Openings/ceilings from another assembly remain
                # project adaptation unless the chosen assembly itself says so.
                adaptation=not direct_corner
                if adaptation and not config.allow_project_assumptions:
                    continue
                eligible.append((special,not bool(family.intersection(other_family)),len(rule.component_codes),rule.source.document_id,rule.source.page,rule.id,rule,adaptation,analogy))
            if eligible:
                best=min(eligible,key=lambda item:item[:6]);source_rule,adapted=best[6],best[7]
                reason="Схема бокового оконного откоса перенесена на верх/низ окна по проектной аналогии; эти места прямо не определены источником." if best[8] else "Применена схема другого раздела к текущей стене; это проектная адаптация, не подтверждение изготовителя." if adapted else "Угловая схема для совпадающего семейства профилей и прямого угла замкнутого помещения."
        if source_rule is None and not config.allow_project_assumptions:
            node.update(decision_status="unresolved",detail_key=f"unresolved.{context}",source_refs=[],component_codes=[],assumption_ids=[],required_checks=["Выбрать и проверить решение этого примыкания."],manufacturer_confirmed=False)
            continue
        source_refs=[source_reference(source_rule,documents,"project_adaptation" if adapted else "assembly")] if source_rule else []
        is_assumption=adapted or source_rule is None or (chosen is not None and chosen["status"]!="candidate")
        node.update(status="project_assumption" if is_assumption else "reference_candidate",decision_status="project_assumption" if is_assumption else "source_reference",
                    rule_id=source_rule.id if source_rule else None,source=_source(source_rule,documents) if source_rule else None,source_refs=source_refs,
                    component_codes=list(source_rule.component_codes) if source_rule else [],detail_key=("project." if is_assumption else "source.")+context,
                    required_checks=["Проверить применимость по основанию, материалу панели, зазорам и креплению."],assumption_ids=[],manufacturer_confirmed=False)
        if edge.get("corner_type"):
            node["corner_type"]=edge["corner_type"]
            node["required_checks"].append("Согласовать системы и кромки панелей с обеих сторон угла; совпадение семейства на одной стене не утверждает их совместимость.")
        if is_assumption:
            ident="detail-"+node["id"]
            message=reason or f"Для места «{node['label']}» принята принципиальная проектная схема без назначения производственных размеров и крепежа."
            checks="Увязать с реальной конструкцией проёма/потолка/основания; подтвердить размеры профиля, крепление и возможность сборки."
            assumptions.append(assumption(ident,message,checks,source_refs));node["assumption_ids"]=[ident];node["required_checks"]=[checks]
        node["decision_reason"]=reason or "Схема примыкания указана на странице выбранной сборки."
    return assumptions
