"""Source-bound full-height corner/straight-joint parts for room assemblies.

No panel samples are generated here.  The caller trims the actual wall panels,
then verifies their positive-area contact with ``panel_contacts``.  New corner
supports have real V04 screws and explicit foundation anchors, not free posts.
All shapes are returned in world coordinates; local IDs are prefixed by the
room worker.  Source bytes are the only accepted CAD input (never paths).
"""
from __future__ import annotations

import hashlib
import math

from .assembly_fasteners import CALIBRATIONS as HARDWARE, PROFILE_SOURCES
from .assembly_recipes import CALIBRATIONS
from .assembly_recipes_corners import CALIBRATIONS_EXTRA
from .native_definition_cache import definition_is_valid

ANGLE = CALIBRATIONS["angle20"]["source_id"]
CUB = CALIBRATIONS_EXTRA["external-V-Cub"]["source_id"]
SCREW = HARDWARE["screw"]["source_id"]
FRAME = HARDWARE["frame"]["source_id"]
CUB_PANEL_END = 1.8-CALIBRATIONS_EXTRA["external-V-Cub"]["panel_face_xy_mm"]
POLICY = "room-corner-parts/1.1"


def _number(value, low=-1e8, high=1e8):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Некорректный размер сборки угла")
    return float(value)


def _vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("Направление угла должно содержать три координаты")
    return [_number(x) for x in value]


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _matrix(*columns):
    return [[columns[j][i] for j in range(3)] for i in range(3)]


def _positions(length, pitch, edge):
    edge = min(edge, length/2)
    count = max(1, math.ceil(max(0, length-2*edge)/pitch))
    return [length/2] if length <= 2*edge+50. else [edge+(length-2*edge)*i/count for i in range(count+1)]


def _internal_support_x(thickness):
    return -thickness/2.


def _anchor_positions(length, pitch, screw_positions):
    edge = min(100., length/4)
    initial = max(1,math.ceil((length-2*edge)/pitch))
    for count in range(initial,initial+8):
        result=[]
        for position in [edge+(length-2*edge)*i/count for i in range(count+1)]:
            candidates = [position, *[value+shift for value in screw_positions for shift in (-25.,25.)]]
            candidates = [value for value in candidates if edge-1e-7 <= value <= length-edge+1e-7
                          and all(abs(value-screw) >= 25.-1e-7 for screw in screw_positions)
                          and all(abs(value-old) >= 25.-1e-7 for old in result)]
            if not candidates:break
            result.append(min(candidates,key=lambda value:(abs(position-value),value)))
        result.sort()
        if len(result)==count+1 and all(b-a <= pitch+1e-7 for a,b in zip(result,result[1:])):
            return result
    raise ValueError("Нельзя расставить угловые анкеры без пересечений и превышения заданного шага")


def corner_spec(joint):
    """Validate the domain joint and describe the only calibrated solutions.

    ``front_intersection_mm`` is the intersection of finished panel faces.
    Rays ``axes_a/b`` point away from the junction into their respective wall.
    Internal panels have depth -b/-a; external panels have depth +b/+a.
    ``segments_z_mm`` contains absolute world elevations, not local offsets.
    """
    if not isinstance(joint, dict) or joint.get("kind") not in {"internal_corner", "external_corner", "straight"}:
        raise ValueError("Нет проверенного рецепта для этого типа сопряжения")
    kind = joint["kind"]
    origin = _vector(joint.get("front_intersection_mm"))
    a, b = _vector(joint.get("axes_a")), _vector(joint.get("axes_b"))
    if any(abs(_dot(v,v)-1)>1e-7 or abs(v[2])>1e-7 for v in (a,b)):
        raise ValueError("Направления панелей должны быть единичными горизонтальными векторами")
    expected_dot = -1. if kind == "straight" else 0.
    if abs(_dot(a,b)-expected_dot)>1e-7:
        raise ValueError("Жёсткий STEP-профиль нельзя согнуть под непрямой угол")
    configs = [joint.get("configuration_a"), joint.get("configuration_b")]
    if any(not isinstance(value,dict) for value in configs):
        raise ValueError("Нужны назначения монтажа обеих сторон угла")
    thickness = [_number(value.get("panel_thickness_mm"), 8., 13.) for value in configs]
    systems = [value.get("mounting_system") for value in configs]
    if systems[0] != systems[1] or systems[0] not in {"open_mount","concealed_mount"}:
        raise ValueError("Смешанные системы угла требуют отдельной проверенной посадки")
    hidden = systems[0] == "concealed_mount"
    edges = [value.get("panel_edge") or ("Vb" if hidden else "V-0") for value in configs]
    if edges[0] != edges[1] or (hidden and edges[0] not in {"Vb","Vc","Vd"}) or (not hidden and edges[0] != "V-0"):
        raise ValueError("Кромки соседних панелей несовместимы с угловым рецептом")
    segments = joint.get("segments_z_mm")
    if not isinstance(segments,list):
        raise ValueError("Нужны высотные участки сопряжения")
    for interval in segments:
        if not isinstance(interval,(list,tuple)) or len(interval)!=2 or _number(interval[1])-_number(interval[0]) < 100 or interval[1]-interval[0]>10000:
            raise ValueError("Высота непрерывного углового участка должна быть 100–10000 мм")
    if any(right[0]<left[1]-1e-7 for left,right in zip(sorted(segments),sorted(segments)[1:])):
        raise ValueError("Высотные участки угла пересекаются")
    indices = joint.get("wall_indices")
    if not isinstance(indices,list) or len(indices)!=2 or any(type(i) is not int or i<0 for i in indices) or indices[0]==indices[1]:
        raise ValueError("Нужны две различные стороны стены")
    if not segments:
        return {"schema_version":POLICY,"kind":kind,"family":None,"origin_mm":origin,
                "axes_a":a,"axes_b":b,"segments_z_mm":[],"wall_indices":indices,
                "source_ids":[],"support":False,"assumptions":[],
                "skipped_reason":"Нет общей высоты двух облицованных панелей; угловой профиль не требуется, существующие торцы сохраняются."}
    family = "angle20"
    offsets = [0., 2.] if kind == "internal_corner" else [0., thickness[0]+2.]
    thin_bond = 3. if kind == "internal_corner" and thickness[1] < 10.8 and not hidden else 0.
    if kind == "internal_corner" and thickness[1] < 10.8 and hidden:
        raise ValueError("Реальная головка самореза 10,8 мм не помещается в угловом кармане второй панели")
    if thin_bond and min(19.-thin_bond-thickness[1],17.-thin_bond-thickness[0])<2.-1e-7:
        raise ValueError("Тонкопанельный клеевой угол не оставляет 2 мм нахлёста на обеих панелях")
    if kind == "external_corner" and hidden:
        minimum = 9.6 if edges[0] == "Vc" else 9.1
        if min(thickness)<minimum:
            raise ValueError("За пазом V-Cub не остаётся требуемого материала панели")
        family, offsets = "cub", [CUB_PANEL_END,CUB_PANEL_END]
    if kind == "straight":
        gap = [_number(value.get("joint_mm"),0,100) for value in configs]
        if abs(gap[0]-gap[1])>1e-7:
            raise ValueError("На прямом стыке заданы разные швы")
        family = "vh2" if hidden and 3<=gap[0]<=5 else "vh3" if hidden and 16<=gap[0]<=18 else "t1" if not hidden and abs(gap[0]-2)<1e-7 else None
        if family is None:
            raise ValueError("Нет калиброванного профиля прямого стыка для этого шва")
        keys = [(side.get("plan", {}).get("system") or {}).get("key") for side in joint.get("sides", [])]
        expected_keys={"v0","legacy_v0"} if family == "t1" else {family}
        if any(key is not None and key not in expected_keys for key in keys):
            raise ValueError("Профиль стыка не соответствует реально назначенной стеновой системе")
        if hidden and (abs(thickness[0]-thickness[1])>1e-7 or min(thickness)<(9.6 if edges[0]=="Vc" else 9.1) or max(thickness)>12.2):
            raise ValueError("Толщины панелей не соответствуют измеренной скрытой посадке стыка")
        offsets = [gap[0]/2,gap[0]/2]
    allowed_manual = {"profile-19"} if family == "cub" else {"profile-28"} if family == "angle20" else set()
    manual_group="corners"
    if kind == "straight":
        from .wall_systems import HIDDEN_RULES
        manual_group="joints"
        allowed_manual = {"open-1-1","open-1-3","open-1-4","profile-8"} if family == "t1" else {
            "profile-13" if family == "vh2" else "profile-14",
            *[rule for rule,key in HIDDEN_RULES.items() if key==family],
        }
    for cfg in configs:
        requested=(cfg.get("node_rule_ids") or {}).get(manual_group)
        if requested and requested not in allowed_manual:
            raise ValueError(f"Вручную назначен узел группы {manual_group}: {requested}; его нельзя молча заменить калиброванным {family}")
        if cfg.get("allow_project_assumptions") is not True and requested not in allowed_manual:
            raise ValueError("Проектный угловой комплект не разрешён для обеих сторон")
    given = joint.get("panel_end_offsets_mm")
    if given is not None and (not isinstance(given,(list,tuple)) or len(given)!=2 or any(abs(_number(x)-target)>1e-6 for x,target in zip(given,offsets))):
        raise ValueError("Обрезка панелей не соответствует реальному угловому профилю")
    sources = {"angle20":ANGLE,"cub":CUB,**PROFILE_SOURCES}
    ids = [sources[family]]
    support = kind == "internal_corner" or family in {"vh2","vh3"}
    if support: ids += [SCREW,FRAME]
    result = {"schema_version":POLICY,"kind":kind,"family":family,"origin_mm":origin,"axes_a":a,"axes_b":b,
              "segments_z_mm":[list(x) for x in segments],"wall_indices":indices,"thickness_mm":thickness,
              "panel_edge":edges[0],"panel_end_offsets_mm":offsets,"source_ids":ids,"support":support,
              "corner_gap_mm":2. if kind!="straight" else None,"assumptions":[]}
    if kind == "internal_corner":
        result["seat_variant"]="v0-thin-panel-bonded-rear-angle" if thin_bond else "direct-rear-angle"
        result["internal_bond_thickness_mm"]=thin_bond
        if thin_bond:
            result["assumptions"].append("Для тонких V-0 внутренний уголок отодвинут за обе панели на проектные 3 мм клеевого слоя; исходная головка не уменьшена. Полная стойка с саморезами и анкерами перенесена вместе с уголком, прочность клея не рассчитывается.")
    if kind == "external_corner" and family == "angle20":
        bond=_number(joint.get("corner_bond_thickness_mm",.5),.1,1.)
        if 19.-bond-offsets[1] < 2.-1e-7:
            raise ValueError("Клеевая наружная накладка не оставляет 2 мм нахлёста на второй панели")
        result["corner_bond_thickness_mm"]=bond
        result["assumptions"].append(f"Наружная накладка приклеена к двум панелям отдельными слоями {bond:g} мм: это проектная толщина, не значение производителя; прочность клея не рассчитывается.")
    if support:
        sides = joint.get("sides")
        if not isinstance(sides,list) or len(sides)!=2:
            raise ValueError("Для анкеровки нужна исходная стена и её слои")
        side=sides[0]; plan=side.get("plan",{}); layers=side.get("layers",plan.get("layers",{})); cfg=side.get("configuration",plan.get("configuration",{}))
        depth=_number(side.get("substrate_front_mm",layers.get("substrate_front_mm")),40,10000)
        embed=_number(side.get("anchor_embedment_mm",cfg.get("anchor_embedment_mm",50)),30,200)
        base=_number(side.get("substrate_thickness_mm",cfg.get("substrate_thickness_mm",160)),60,1000)
        if embed+10>base:raise ValueError("Анкер угловой опоры выходит через основание")
        result["support_configuration"]={"substrate_front_mm":depth,"substrate_thickness_mm":base,"embedment_mm":embed,
            "screw_pitch_mm":_number(cfg.get("screw_pitch_mm",400),100,1000),"anchor_pitch_mm":_number(cfg.get("anchor_pitch_mm",600),100,1500)}
    if kind == "straight":
        sides=joint.get("sides")
        if not isinstance(sides,list) or len(sides)!=2:raise ValueError("Нужны нормали обеих сторон прямого стыка")
        normal=sides[0].get("normal")
        normal=_vector([*normal,0.] if isinstance(normal,(list,tuple)) and len(normal)==2 else normal)
        depth=[-x for x in normal]
        if abs(_dot(depth,depth)-1)>1e-7 or abs(depth[2])>1e-7 or abs(_dot(depth,b))>1e-7:
            raise ValueError("Не определена глубина прямого стыка")
        result["axes_a"],result["axes_b"]=b,depth
    if family == "cub":result["assumptions"].append("Точный V-Cub использован как проектная угловая посадка с пазами; тождество V-Ang/V-T-Ang не утверждается.")
    if family == "angle20":result["assumptions"].append("Калиброванный уголок 20×20×1 — отдельная проектная адаптация, не выдуманный V-Ang/V-T-Ang.")
    if kind == "internal_corner":result["assumptions"].append("Угловой торцевой зазор 2 мм независим от шва плоского поля; дополнительная стойка анкеруется к существующему основанию.")
    return result


def required_source_ids(joints):
    """Return exact source hashes for one joint or a collection of joints.

    Unsupported geometry raises its explanatory ValueError: callers must keep
    that junction as unresolved, never quietly omit it from a completed room.
    """
    if isinstance(joints,dict):joints=[joints]
    return {source_id for joint in joints for source_id in corner_spec(joint)["source_ids"]}


def build_corner_parts(joint, sources_bytes, oc=None, *, source_cache=None):
    """Create exact world solids/tools; native room integration must prove seats.

    Returned ``contacts``/``fastenings``/``anchors`` are evidence obligations,
    not an approval. The worker checks them against exported/reimported solids,
    and checks ``panel_contacts`` and substrate interfaces against actual walls.
    """
    spec=corner_spec(joint)
    if spec.get("skipped_reason"):
        return {"schema_version":POLICY,"spec":spec,"parts":[],"contacts":[],"fastenings":[],
                "anchors":[],"panel_contacts":[],"panel_tools":[],"substrate_tools":[],"clearance_tools":[],
                "native_checks":{"status":"not_applicable","reason":spec["skipped_reason"]},"assumptions":[]}
    from . import assembly_geometry as ag, step_geometry as step
    from .wall_assembly_geometry import _box,_cut,_fuse,_cylinder,_move,_wt_section
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pln,gp_Pnt,gp_Dir,gp_Vec
    oc=oc or step._occt()
    if not isinstance(sources_bytes,dict):raise ValueError("Нужны байты исходных STEP, а не пути к файлам")
    loaded={}
    source_cache={} if source_cache is None else source_cache
    for source_id in spec["source_ids"]:
        content=sources_bytes.get(source_id)
        if not isinstance(content,bytes) or hashlib.sha256(content).hexdigest()!=source_id:
            raise ValueError("Байты угловой детали не соответствуют зафиксированному SHA-256")
        if source_id not in source_cache:
            solids=step._load(content,oc,max_bytes=None)[1]
            if len(solids)!=1:raise ValueError("Калиброванная угловая деталь должна содержать ровно одно тело")
            source_cache[source_id]=solids[0]
        loaded[source_id]=source_cache[source_id]
    family=spec["family"];a,b=spec["axes_a"],spec["axes_b"]
    handed=_cross(a,b)[2]
    if abs(abs(handed)-1)>1e-7:raise ValueError("Система координат сопряжения не ортонормальна")
    rotation=_matrix(a,b,[0.,0.,1. if handed>0 else -1.])
    parts=[];contacts=[];fastenings=[];anchors=[];panel_contacts=[];panel_tools=[];substrate_tools=[];clearance_tools=[]
    def section(source_id,rotation_source,translation,offset=0.):
        face=BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0,offset,0),gp_Dir(0,1,0)),-200,200,-200,200).Face()
        face=ag._boolean_shape(BRepAlgoAPI_Common,loaded[source_id],face)
        if len(step._shapes(face,oc["TopAbs_FACE"],oc))!=1 or ag._mass(face,oc,surface=True)<=1:
            raise ValueError("Не удалось извлечь единое точное материальное сечение профиля")
        return _move(face,translation,rotation_source)
    if family=="angle20":
        profile_face=section(ANGLE,[[1,0,0],[0,0,-1],[0,1,0]],[0,0,0])
        if abs(ag._mass(profile_face,oc,surface=True)-39.)>1e-5:raise ValueError("Изменилось сечение уголка 20×20×1")
    elif family=="cub":
        c=math.sqrt(.5);f=CALIBRATIONS_EXTRA["external-V-Cub"]["panel_face_xy_mm"]
        profile_face=section(CUB,[[c,0,c],[-c,0,c],[0,-1,0]],[-f,-f,15],15.)
    elif family in {"vh2","vh3"}:
        from .wall_assembly_concealed import _profile_face
        profile_face=_profile_face(loaded[PROFILE_SOURCES[family]],family,oc)
    else:
        profile_face=section(PROFILE_SOURCES["t1"],[[1,0,0],[0,0,-1],[0,1,0]],[0,-1.2,0])
    for number,(low,high) in enumerate(spec["segments_z_mm"],1):
        height=high-low;translation=[spec["origin_mm"][0],spec["origin_mm"][1],low if handed>0 else high]
        def point(p):return [translation[i]+sum(rotation[i][j]*p[j] for j in range(3)) for i in range(3)]
        def direction(p):return [sum(rotation[i][j]*p[j] for j in range(3)) for i in range(3)]
        def world(shape):return _move(shape,translation,rotation)
        def add(local,shape,role,label,source_id=None,provenance=None):
            identifier=f"s{number}:{local}"
            # Validation is location invariant. OCCT's classifier loses some
            # precision for the supplied threaded screw at large world XY.
            from OCP.TopLoc import TopLoc_Location
            definition=shape.Located(TopLoc_Location())
            if not definition_is_valid(definition,oc) or len(step._shapes(definition,oc["TopAbs_SOLID"],oc))!=1:
                raise ValueError(f"Некорректное тело угловой детали {identifier}")
            shape=world(shape)
            parts.append({"id":identifier,"instance_id":identifier,"shape":shape,"role":role,"label":label,
                "source_id":source_id,"provenance":provenance or {"kind":"source_step" if source_id else "project_parametric","source_id":source_id}})
            return identifier
        t_a,t_b=spec["thickness_mm"]
        profile=BRepPrimAPI_MakePrism(profile_face,gp_Vec(0,0,height)).Shape()
        bond=spec.get("corner_bond_thickness_mm",0.)
        internal_bond=spec.get("internal_bond_thickness_mm",0.)
        profile_origin=[-t_b-1-internal_bond,-t_a-1-internal_bond,0] if spec["kind"]=="internal_corner" else [-1-bond,-1-bond,0] if family=="angle20" else [0,0,0]
        profile=_move(profile,profile_origin)
        # The receiving pocket must clear the complete original section,
        # including access to anchor bores. Subtracting the drilled section
        # would leave isolated plugs of old backing inside those holes.
        profile_seat=profile
        columns=[]
        if spec["kind"]=="internal_corner":columns=[{"x":_internal_support_x(t_b+internal_bond),"seat":-t_a-internal_bond,"sign":-1.,"backing_front":-t_a-1-internal_bond,"backing":12.}]
        elif family in {"vh2","vh3"}:
            columns=[{"x":x,"seat":5.5 if family=="vh3" else 14.7,"sign":1.,"backing_front":15.9,"backing":10. if family=="vh3" else 12.} for x in ([0.] if family=="vh3" else [-19.,20.])]
        scfg=spec.get("support_configuration",{})
        hardware=[]
        if columns:
            wt_face,_area=_wt_section(loaded[FRAME],oc)
            wt_face=_move(wt_face,[0,0,0],[[1,0,0],[0,0,1],[0,-1,0]])
            for ci,column in enumerate(columns,1):
                x,seat,sign,front,backing=(column[k] for k in ("x","seat","sign","backing_front","backing"))
                frame_front=front+sign*backing;depth=sign*scfg["substrate_front_mm"];embed=scfg["embedment_mm"]
                if sign*(depth-frame_front)<27.01:raise ValueError("Угловой каркас не помещается до основания")
                zs=_positions(height,scfg["screw_pitch_mm"],50.)
                azs=_anchor_positions(height,scfg["anchor_pitch_mm"],zs)
                frame=BRepPrimAPI_MakePrism(wt_face,gp_Vec(0,0,height)).Shape()
                if sign>0:frame=_move(frame,[0,0,height],[[1,0,0],[0,-1,0],[0,0,-1]])
                frame=_move(frame,[x,frame_front,0])
                back=_box([28.,backing,height],[x-14.,min(front,frame_front),0])
                for z in azs:
                    axis=(0,sign,0)
                    frame=_cut(frame,_cylinder(3.,1.22,[x,frame_front-sign*.01,z],axis))
                    back=_cut(back,_cylinder(6.,backing+.02,[x,front-sign*.01,z],axis))
                    # The anchor head is reached through a real opening in the
                    # profile. A bore never goes through a visible panel.
                    profile=_cut(profile,_cylinder(6.,abs(frame_front-seat)+.02,[x,seat-sign*.01,z],axis))
                hardware.append((ci,column,frame,back,zs,azs))
        profile_id=add("profile",profile,"profile",f"Угловой профиль {family} / точное сечение",spec["source_ids"][0],
            {"kind":"exact_section_extrusion","source_id":spec["source_ids"][0],"length_mm":height,"corner_policy":POLICY,
             "section_translation_mm":profile_origin,"corner_bond_thickness_mm":bond,
             "internal_bond_thickness_mm":internal_bond,"seat_variant":spec.get("seat_variant")})
        if spec["kind"]=="internal_corner":
            # V-0 has a continuous 3 mm adhesive bed at the panel rear. The
            # actual angle leg occupies 1 mm of that bed on BOTH walls; a
            # pocket for only the extra WT would leave overlapping materials.
            for wall_index in spec["wall_indices"]:
                clearance_tools.append({"id":f"s{number}:angle-leg-seat-w{wall_index}","kind":"corner_profile_seat",
                    "wall_index":wall_index,"roles":["backing","adhesive"],"shape":world(profile_seat)})
        if internal_bond:
            films=[_box([19.-internal_bond-t_b,internal_bond,height],[0.,-t_a-internal_bond,0.]),
                   _box([internal_bond,17.-internal_bond-t_a,height],[-t_b-internal_bond,2.,0.])]
            for index,shape in enumerate(films):
                identifier=add(f"thin-corner-bond-{index+1}",shape,"adhesive","Клей тыльного уголка 3 мм / проектный",
                    provenance={"kind":"project_parametric_bond","thickness_mm":internal_bond,
                                "corner_policy":POLICY,"seat_variant":spec["seat_variant"],"manufacturer_dimension":False})
                contacts.append({"id":f"s{number}:thin-angle-bond-{index+1}","body_a":profile_id,"body_b":identifier,"min_area_mm2":.1})
                panel_contacts.append({"body_id":identifier,"wall_index":spec["wall_indices"][index],"min_area_mm2":.1})
                clearance_tools.append({"id":f"s{number}:thin-bond-seat-{index+1}","kind":"corner_bond_replacement",
                    "wall_index":spec["wall_indices"][index],"roles":["adhesive","backing"],"shape":world(shape)})
        elif bond:
            # Two finite material films join the fixed, unchanged panels to
            # the rigidly translated source cap. Merely touching an unglued
            # loose front cap would not be a fastening connection.
            extent=19.-bond;second_start=spec["panel_end_offsets_mm"][1]
            bonds=[_box([extent,bond,height],[0.,-bond,0.]),
                   _box([bond,extent-second_start,height],[-bond,second_start,0.])]
            for index,shape in enumerate(bonds):
                bond_id=add(f"corner-bond-{index+1}",shape,"adhesive",f"Клей наружной накладки {bond:g} мм / проектный",
                            provenance={"kind":"project_parametric_bond","thickness_mm":bond,
                                        "corner_policy":POLICY,"manufacturer_dimension":False})
                contacts.append({"id":f"s{number}:cap-bond-{index+1}","body_a":profile_id,"body_b":bond_id,"min_area_mm2":.1})
                panel_contacts.append({"body_id":bond_id,"wall_index":spec["wall_indices"][index],"min_area_mm2":.1})
        else:
            for wall_index in spec["wall_indices"]:panel_contacts.append({"body_id":profile_id,"wall_index":wall_index,"min_area_mm2":.1})
        if family=="cub":
            edge=spec["panel_edge"];offset=CUB_PANEL_END;depth=11. if edge=="Vb" else 16.;width=3.1 if edge=="Vc" else 2.6
            for index in (0,1):
                cutter=_box([depth+.02,width,height+.02],[offset-.01,5.5,-.01])
                if index:cutter=_move(cutter,[0,0,height],[[0,1,0],[1,0,0],[0,0,-1]])
                panel_tools.append({"id":f"s{number}:cub-groove-{index}","kind":"corner_groove","wall_index":spec["wall_indices"][index],"roles":["panel"],"shape":world(cutter)})
                if edge=="Vc":
                    relief=_box([5.02,spec["thickness_mm"][index]-8.6+.01,height+.02],[offset-.01,8.6,-.01])
                    if index:relief=_move(relief,[0,0,height],[[0,1,0],[1,0,0],[0,0,-1]])
                    panel_tools.append({"id":f"s{number}:cub-relief-{index}","kind":"corner_rear_relief","wall_index":spec["wall_indices"][index],"roles":["panel"],"shape":world(relief)})
        if spec["kind"]=="straight" and family in {"vh2","vh3"}:
            edge=spec["panel_edge"];depth=11. if edge=="Vb" else 16.;width=3.1 if edge=="Vc" else 2.6
            for index,sign in ((0,-1),(1,1)):
                offset=sign*spec["panel_end_offsets_mm"][index]
                x=offset-.01 if sign>0 else offset-depth
                cutter=_box([depth+.01,width,height+.02],[x,5.5,-.01])
                panel_tools.append({"id":f"s{number}:straight-groove-{index}","kind":"straight_joint_groove","wall_index":spec["wall_indices"][index],"roles":["panel"],"shape":world(cutter)})
                if edge=="Vc":
                    x=offset-.01 if sign>0 else offset-5.
                    relief=_box([5.01,spec["thickness_mm"][index]-8.6+.01,height+.02],[x,8.6,-.01])
                    panel_tools.append({"id":f"s{number}:straight-relief-{index}","kind":"straight_joint_rear_relief","wall_index":spec["wall_indices"][index],"roles":["panel"],"shape":world(relief)})
        for ci,column,frame,back,zs,azs in hardware:
            x,seat,sign,front,backing=(column[k] for k in ("x","seat","sign","backing_front","backing"))
            frame_front=front+sign*backing;depth=sign*scfg["substrate_front_mm"];embed=scfg["embedment_mm"]
            frame_id=add(f"frame-{ci}",frame,"frame","Угловая опора V-WT / точное сечение",FRAME,{"kind":"exact_section_extrusion","source_id":FRAME,"length_mm":height})
            backing_id=add(f"backing-{ci}",back,"backing",f"Угловая подоснова {backing:g} мм")
            contacts.extend([{"id":f"s{number}:profile-backing-{ci}","body_a":profile_id,"body_b":backing_id,"min_area_mm2":.1},
                             {"id":f"s{number}:backing-frame-{ci}","body_a":backing_id,"body_b":frame_id,"min_area_mm2":.1}])
            pocket=world(_box([28.,backing+27.,height],[x-14.,min(front,frame_front+sign*27),0]))
            pocket_indices=spec["wall_indices"] if spec["kind"]=="straight" else spec["wall_indices"][:1]
            for wall_index in pocket_indices:
                clearance_tools.append({"id":f"s{number}:support-pocket-{ci}-w{wall_index}","kind":"corner_support_pocket","wall_index":wall_index,"roles":["backing","adhesive","frame"],"shape":pocket})
            screw_rotation=[[0,-sign,0],[sign,0,0],[0,0,1]]
            for si,z in enumerate(zs,1):
                screw=add(f"screw-{ci}-{si}",_move(loaded[SCREW],[x,seat-sign*2.45,z],screw_rotation),"fastener","Саморез V04 / исходный STEP",SCREW)
                fastenings.append({"id":f"s{number}:fix-{ci}-{si}","fastener_id":screw,"seat_id":profile_id,
                    "axis_origin_mm":point([x,seat,z]),"axis_direction":direction([0,sign,0]),"shaft_radius_mm":2.11,"head_radius_mm":5.4,"head_height_mm":2.45,"shaft_length_mm":25.,"min_engagement_mm":1.,"engagement_targets":[profile_id,backing_id,frame_id]})
            for ai,z in enumerate(azs,1):
                axis=(0,sign,0);length=sign*(depth-frame_front)+embed
                bolt=_fuse([_cylinder(3.,length,[x,frame_front,z],axis),_cylinder(5.,4.,[x,frame_front-sign*4,z],axis)])
                sleeve=_cut(_cylinder(4.,embed,[x,depth,z],axis),_cylinder(3.,embed+.02,[x,depth-sign*.01,z],axis))
                spacer_start=frame_front+sign*1.2;spacer_length=sign*(depth-spacer_start)
                spacer=_box([12.,spacer_length,12.],[x-6.,min(spacer_start,depth),z-6.])
                spacer=_cut(_cut(spacer,_cylinder(3.,spacer_length+.02,[x,spacer_start-sign*.01,z],axis)),frame)
                bolt_id=add(f"bolt-{ci}-{ai}",bolt,"anchor_bolt","Угловой анкерный болт 6 мм / проектный")
                sleeve_id=add(f"sleeve-{ci}-{ai}",sleeve,"anchor_sleeve","Анкерная втулка 8/6 мм / проектная")
                spacer_id=add(f"spacer-{ci}-{ai}",spacer,"spacer","Дистанционная опора / точное седло V-WT")
                for ca,cb in ((bolt_id,frame_id),(bolt_id,spacer_id),(bolt_id,sleeve_id),(spacer_id,frame_id)):
                    contacts.append({"id":f"contact-{ca}-{cb}","body_a":ca,"body_b":cb,"min_area_mm2":.1})
                anchor_id=f"s{number}:anchor-{ci}-{ai}"
                anchors.append({"id":anchor_id,"bolt_id":bolt_id,"sleeve_id":sleeve_id,"spacer_id":spacer_id,"frame_id":frame_id,
                    "substrate_wall_index":spec["wall_indices"][0],"axis_origin_mm":point([x,frame_front,z]),"axis_direction":direction([0,sign,0]),
                    "substrate_front_mm":point([x,depth,z]),"embedment_mm":embed,"min_embedment_mm":embed})
                substrate_tools.append({"id":anchor_id+":bore","anchor_id":anchor_id,"kind":"corner_anchor_bore","wall_index":spec["wall_indices"][0],"roles":["substrate"],"shape":world(_cylinder(4.,embed,[x,depth,z],axis))})
    return {"schema_version":POLICY,"spec":spec,"parts":parts,"contacts":contacts,"fastenings":fastenings,"anchors":anchors,
            "panel_contacts":panel_contacts,"panel_tools":panel_tools,"substrate_tools":substrate_tools,"clearance_tools":clearance_tools,
            "native_checks":{"status":"requires_room_validation","reason":"Actual panels and foundation, including exported STEP seats, are verified by the room worker."},
            "assumptions":spec["assumptions"]}
