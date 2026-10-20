"""Whole-wall exact solids. Parametric supports and anchors are project parts.

Unlike local node recipes this engine shares native definitions between rigid
occurrences. The installed screw is never scaled; the WT member is derived by
extruding an exact cross-section of the hash-pinned supplied profile.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import io
import json
import math
import re

from . import assembly_geometry as ag
from . import step_geometry as step
from .assembly_fasteners import CALIBRATIONS, PROFILE_SOURCES

REQUIRED_SOURCE_IDS = tuple(CALIBRATIONS[key]["source_id"] for key in ("screw", "frame")) + (PROFILE_SOURCES["t1"],)
SOURCE_LABELS = {CALIBRATIONS[key]["source_id"]: CALIBRATIONS[key]["filename"] for key in ("screw", "frame")}
SOURCE_LABELS[PROFILE_SOURCES["t1"]] = "V_038-Т-профиль-гвоздь_V-T-1.stp"
MAX_PREVIEW_POINTS = 6000
IDENTITY = [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]


def _error(message, code="WALL_ASSEMBLY_INVALID", details=None):
    raise step.StepGeometryError(message, code=code, details=details)


def _finite(value, lo=-1e9, hi=1e9):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi or not math.isfinite(value):
        _error("Некорректный конечный размер в сборке стены")
    return float(value)


def validate_wall_plan(plan):
    """Independent bounded preflight; the planner also checks design rules."""
    if not isinstance(plan, dict) or plan.get("schema_version",plan.get("schema")) not in ("wall-framing-plan/1.0", "wall-framing-plan/1"):
        _error("Неизвестный формат раскладки каркаса")
    if plan.get("status") != "ready":
        _error("Для объёмной сборки нужна допустимая раскладка каркаса")
    for key in ("panels", "posts", "rails", "screws", "anchors", "openings"):
        values = plan.get(key, [])
        if not isinstance(values, list):
            _error(f"Некорректный список элементов {key}")
        ids = set()
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in ids:
                _error(f"Некорректный идентификатор {key}")
            ids.add(item["id"])
            for field, value in item.items():
                if field.endswith("_mm") and value is not None:
                    _finite(value)
    if not plan.get("panels") or not plan.get("posts") or not plan.get("anchors"):
        _error("Раскладка стены не содержит панелей, каркаса или анкеров")
    members={item["id"] for group in ("posts","rails") for item in plan.get(group,[])}
    for group in ("screws","anchors"):
        for item in plan[group]:
            if item.get("post_id") not in members:_error("Крепёж не связан с существующей опорой")
    joints=plan.get("joint_lines",[])
    if not isinstance(joints,list):_error("Некорректный список стыков панелей")
    for joint in joints:
        if not isinstance(joint,dict) or joint.get("kind") not in ("vertical_joint","horizontal_joint"):_error("Некорректный стык панелей")
        for point in (joint.get("from"),joint.get("to")):
            if not isinstance(point,dict):_error("Некорректная координата стыка")
            _finite(point.get("x_mm"));_finite(point.get("y_mm"))
    hidden = plan.get("system") is not None
    layers = plan.get("layers", {})
    for field in ("panel_thickness_mm", "adhesive_thickness_mm", "backing_thickness_mm", "frame_depth_mm", "substrate_front_mm"):
        _finite(layers.get(field), 0. if hidden and field == "adhesive_thickness_mm" else .1, 10000)
    if hidden:
        from .wall_assembly_concealed import validate_concealed_plan
        validate_concealed_plan(plan)
    elif abs(layers["adhesive_thickness_mm"] - 3) > 1e-6 or abs(layers["backing_thickness_mm"] - 12) > 1e-6 or abs(layers["frame_depth_mm"] - 27) > 1e-6:
        _error("Сборка поддерживает проверенную цепочку: клей 3, подоснова 12, V-WT 27 мм")
    wall = plan.get("wall", {})
    _finite(wall.get("length_mm"), 1., 1e9)
    _finite(wall.get("height_mm"), 1., 1e9)
    cfg = plan.get("configuration", {})
    from .wall_framing import substrate_interval, substrate_openings
    try:
        substrate_start, substrate_end = substrate_interval(wall)
        substrate_openings(plan)
    except ValueError as error:
        _error(str(error))
    anchor_edge = _finite(cfg.get("anchor_edge_mm"), 40., 300.)
    for anchor in plan["anchors"]:
        x, z = anchor["x_mm"], anchor["z_mm"]
        if min(x-substrate_start, substrate_end-x, z, wall["height_mm"]-z) < anchor_edge-1e-6:
            _error("Анкер нарушает отступ от фактического края основания")
    if not hidden and abs(plan.get("mount_configuration",{}).get("joint_mm",2.)-2.)>1e-6:
        _error("Измеренный V-T1 требует межпанельный шов 2 мм")
    _finite(cfg.get("substrate_thickness_mm"), 60., 1000.)
    _finite(cfg.get("anchor_embedment_mm"), 30., 200.)
    if cfg["anchor_embedment_mm"] + 10 > cfg["substrate_thickness_mm"]:
        _error("Анкер выходит через обратную поверхность основания")
    return deepcopy(plan)


def required_source_ids(plan=None):
    if plan and plan.get("system") is not None:
        from .wall_assembly_concealed import source_ids
        return source_ids(plan)
    return REQUIRED_SOURCE_IDS


def validate_wall_sources(sources, plan=None):
    if not isinstance(sources, dict) or set(sources) != set(required_source_ids(plan)):
        _error("Нужен точный комплект исходных STEP выбранной системы стены")
    if sum(len(x) for x in sources.values() if isinstance(x, bytes)) > 64 * 1024**2:
        _error("Слишком большой комплект STEP", "WALL_ASSEMBLY_LIMIT")
    for identifier, content in sources.items():
        if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != identifier:
            _error("Хеш исходного STEP не соответствует байтам")
        step.validate_step_content(content)


def _move(shape, xyz, rotation=None):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf
    trsf = gp_Trsf()
    trsf.SetValues(*[value for row, shift in zip(rotation or IDENTITY, xyz) for value in (*row, shift)])
    return BRepBuilderAPI_Transform(shape, trsf, False, False).Shape()


def _box(size, xyz=(0., 0., 0.)):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    return BRepPrimAPI_MakeBox(gp_Pnt(*xyz), *size).Shape()


def _cylinder(radius, length, xyz=(0., 0., 0.), axis=(0., 1., 0.)):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*xyz), gp_Dir(*axis)), radius, length).Shape()


def _cut(shape, tool):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    return ag._boolean_shape(BRepAlgoAPI_Cut, shape, tool)


def _fuse(shapes):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    shape = shapes[0]
    for other in shapes[1:]:
        shape = ag._boolean_shape(BRepAlgoAPI_Fuse, shape, other)
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    unify = ShapeUpgrade_UnifySameDomain(shape, True, True, True)
    unify.Build()
    return unify.Shape()


def _bore(shape, holes, front, depth, radius):
    # Cut the same union of cylinders once. Sequential drilling repeatedly
    # rebuilds every preceding hole; perimeter films also used to process
    # every screw on the wall even when it is metres away from the film.
    oc=step._occt();bounds=step._bbox(shape,oc)
    if bounds is None:return shape
    if min(front+depth+.01,bounds["max"][1])<=max(front-.01,bounds["min"][1]):return shape
    selected=[];seen=set()
    for x,z in holes:
        if (x,z) in seen:continue
        seen.add((x,z))
        if (min(x+radius,bounds["max"][0])<=max(x-radius,bounds["min"][0])
                or min(z+radius,bounds["max"][2])<=max(z-radius,bounds["min"][2])):continue
        selected.append((x,z,_cylinder(radius,depth+.02,[x,front-.01,z])))
    if not selected:return shape
    if len(selected)==1:return _cut(shape,selected[0][2])
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    # A compound is a valid Boolean union only for disjoint tools. Keep
    # overlapping/tangent bores in separate batches so their union still has
    # exactly the same meaning as sequential drilling (including close holes).
    batches=[]
    for x,z,cutter in selected:
        batch=next((items for items in batches if all((x-a)**2+(z-b)**2>(2*radius+1e-7)**2 for a,b,_ in items)),None)
        if batch is None:batch=[];batches.append(batch)
        batch.append((x,z,cutter))
    for batch in batches:
        cutters=TopoDS_Compound();builder=BRep_Builder();builder.MakeCompound(cutters)
        for _,_,cutter in batch:builder.Add(cutters,cutter)
        shape=_cut(shape,cutters)
    return shape


def _wt_section(source, oc):
    """Exact material section, not a bounding-box replacement or scaled STEP."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
    plane = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0)), -100, 100, -100, 100).Face()
    face = ag._boolean_shape(BRepAlgoAPI_Common, source, plane)
    area = ag._mass(face, oc, surface=True)
    bounds = step._bbox(source, oc)
    # The supplied 240 mm sample has a trimmed end. Its central material
    # section is the profile definition; that sample-specific end cut is not
    # stretched across an entire wall member.
    if len(step._shapes(face, oc["TopAbs_FACE"], oc)) != 1 or not 96.42 < area < 96.43 or any(abs(a-b) > 1e-4 for a,b in zip(bounds["size"], [28., 240., 27.])):
        _error("Исходный V-WT не соответствует постоянному измеренному сечению")
    return face, area


def _panel_groups(panels):
    """Join geometry rectangles that form one connected manufactured panel."""
    remaining = list(panels)
    while remaining:
        group = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            for item in list(remaining):
                if item.get("panel_id", item["id"]) != group[0].get("panel_id", group[0]["id"]):
                    continue
                for other in group:
                    dx = min(item["x_mm"]+item["width_mm"], other["x_mm"]+other["width_mm"])-max(item["x_mm"],other["x_mm"])
                    dz = min(item["y_mm"]+item["height_mm"], other["y_mm"]+other["height_mm"])-max(item["y_mm"],other["y_mm"])
                    if dx >= -1e-7 and dz >= -1e-7 and max(dx,dz) > 1e-7:
                        group.append(item); remaining.remove(item); changed=True; break
        yield group


def build_wall_assembly(sources, plan):
    with step.rigid_bounds_cache() as bounds:
        result=_build_wall_assembly(sources,plan)
        result["performance"]={"exact_bounds_computed":bounds["misses"],"rigid_bounds_reused":bounds["hits"]}
        return result


def _build_wall_assembly(sources, plan):
    plan = validate_wall_plan(plan)
    validate_wall_sources(sources, plan)
    if plan.get("system") is not None:
        from .wall_assembly_concealed import build_concealed_wall
        return build_concealed_wall(sources, plan)
    oc = step._occt()
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec
    loaded = {key: step._load(value, oc)[1][0] for key, value in sources.items()}
    section, section_area = _wt_section(loaded[REQUIRED_SOURCE_IDS[1]], oc)
    definitions, instances, solids, bodies, fastenings, contacts, anchors = {}, [], [], [], [], [], []

    def define(key, shape, role, label, provenance=None):
        if key not in definitions:
            if not oc["BRepCheck_Analyzer"](shape).IsValid() or len(step._shapes(shape, oc["TopAbs_SOLID"], oc)) != 1:
                _error("Элемент стены не является одним корректным твёрдым телом", details={"definition": key})
            definitions[key] = {"shape": shape, "role": role, "label": label, "provenance": provenance or {"kind": "project_parametric"}}
        return key

    def add(identifier, definition, xyz=(0.,0.,0.), rotation=None):
        item = definitions[definition]
        shape = _move(item["shape"], xyz, rotation)
        index = len(bodies)+1
        body = {"id": index, "instance_id": identifier, "definition_id": definition, "role": item["role"], "label": item["label"],
                "bounds_mm": step._bbox(shape, oc), "volume_mm3": ag._mass(shape, oc), "provenance": item["provenance"]}
        solids.append(shape); bodies.append(body)
        instances.append({"id": identifier, "definition_id": definition, "rotation": rotation or IDENTITY, "translation_mm": list(xyz), "role": item["role"]})
        return identifier

    def contact(a, b):
        if a != b:
            contacts.append({"id": f"contact-{len(contacts)+1}", "body_a": a, "body_b": b, "min_area_mm2": .1})

    layers, cfg = plan["layers"], plan["configuration"]
    t, glue, backing = layers["panel_thickness_mm"], 3., 12.
    frame_front = t+glue+backing
    substrate_front = layers["substrate_front_mm"]
    length, height = plan["wall"]["length_mm"], plan["wall"]["height_mm"]
    substrate_depth, embedment = cfg["substrate_thickness_mm"], cfg["anchor_embedment_mm"]
    if substrate_front < frame_front+27.:
        _error("V-WT не помещается между панелью и основанием")
    from .wall_framing import substrate_interval
    substrate_start, substrate_end = substrate_interval(plan["wall"])
    substrate = _box([substrate_end-substrate_start, substrate_depth, height], [substrate_start, substrate_front, 0.])
    from .wall_framing import substrate_openings
    for opening in substrate_openings(plan):
        from .wall_opening_bounds import opening_cut_bounds
        x,z,right,top=opening_cut_bounds(opening,length,height,outside=.01)
        substrate = _cut(substrate, _box([right-x, substrate_depth+.02, top-z], [x, substrate_front-.01, z]))
    substrate = _bore(substrate, [(a["x_mm"],a["z_mm"]) for a in plan["anchors"]], substrate_front, embedment, 4.)
    add("substrate", define("substrate", substrate, "substrate", "Основание с монтажными отверстиями"))
    panel_rects = []
    for number, group in enumerate(_panel_groups(plan["panels"]), 1):
        shape = _fuse([_box([p["width_mm"], t, p["height_mm"]], [p["x_mm"],0.,p["y_mm"]]) for p in group])
        pid = add(f"panel-{number}", define(f"panel-{number}", shape, "panel", f"Панель {group[0].get('panel_id', group[0]['id'])}"))
        panel_rects.extend((pid, p) for p in group)
    # Full-length real T1 sections close the specified 2 mm V-0 joints.
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
    t1_source=loaded[PROFILE_SOURCES["t1"]]
    t1_face=ag._boolean_shape(BRepAlgoAPI_Common,t1_source,BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0,0,0),gp_Dir(0,1,0)),-100,100,-100,100).Face())
    if len(step._shapes(t1_face,oc["TopAbs_FACE"],oc))!=1:_error("Не удалось получить точное сечение V-T1")
    for number,joint in enumerate(plan.get("joint_lines",[]),1):
        vertical=joint.get("kind")=="vertical_joint"
        if not vertical and joint.get("kind")!="horizontal_joint":continue
        a,b=joint["from"],joint["to"]
        start,end=(sorted([a["y_mm"],b["y_mm"]]) if vertical else sorted([a["x_mm"],b["x_mm"]]))
        center=a["x_mm"] if vertical else a["y_mm"]
        spans=[(start,end)]
        if not vertical:
            for other in plan.get("joint_lines",[]):
                if other.get("kind")!="vertical_joint":continue
                oy1,oy2=sorted([other["from"]["y_mm"],other["to"]["y_mm"]])
                if not oy1-3.<=center<=oy2+3.:continue
                cut=other["from"]["x_mm"]
                spans=[span for lo,hi in spans for span in ((lo,min(hi,cut-3.01)),(max(lo,cut+3.01),hi)) if span[1]-span[0]>.01]
        for part,(lo,hi) in enumerate(spans,1):
            if hi-lo<=.01:continue
            rot=[[1.,0.,0.],[0.,0.,-1.],[0.,1.,0.]] if vertical else [[0.,1.,0.],[0.,0.,-1.],[-1.,0.,0.]]
            xyz=[center,-1.2,lo] if vertical else [lo,-1.2,center]
            shape=_move(BRepPrimAPI_MakePrism(t1_face,gp_Vec(0.,hi-lo,0.)).Shape(),xyz,rot)
            key=f"t1-{number}-{part}"
            profile_id=add(key,define(key,shape,"profile","V-T1 / точный профиль стыка",{"kind":"exact_section_extrusion","source_id":PROFILE_SOURCES["t1"],"section_axis":"y","section_offset_mm":0.,"length_mm":hi-lo}))
            neighbors=set()
            for panel_id,p in panel_rects:
                low=p["y_mm"] if vertical else p["x_mm"]
                high=low+(p["height_mm"] if vertical else p["width_mm"])
                edge_low=p["x_mm"] if vertical else p["y_mm"]
                edge_high=edge_low+(p["width_mm"] if vertical else p["height_mm"])
                if min(hi,high)>max(lo,low)+.01 and (abs(edge_high-(center-1))<1e-5 or abs(edge_low-(center+1))<1e-5):neighbors.add(panel_id)
            for panel_id in sorted(neighbors):contact(profile_id,panel_id)
    define("screw-v04", loaded[REQUIRED_SOURCE_IDS[0]], "fastener", "Саморез V_04 / исходный STEP", {"kind":"source_step", "source_id":REQUIRED_SOURCE_IDS[0], "component_id":1})
    sleeve = _cut(_cylinder(4., embedment), _cylinder(3., embedment+.02, [0.,-.01,0.]))
    define("anchor-sleeve", sleeve, "anchor_sleeve", "Проектная анкерная втулка 8/6 мм", {"kind":"project_parametric", "outer_diameter_mm":8., "inner_diameter_mm":6., "embedment_mm":embedment})
    bolt_length = substrate_front-frame_front+embedment
    bolt = _fuse([_cylinder(3., bolt_length), _cylinder(5., 4., [0.,-4.,0.])])
    define("anchor-bolt", bolt, "anchor_bolt", "Проектный анкерный болт 6 мм", {"kind":"project_parametric", "shaft_diameter_mm":6., "shaft_length_mm":bolt_length})
    spacer_depth = substrate_front-frame_front-1.2
    spacer = _bore(_box([12.,spacer_depth,12.],[-6.,0.,-6.]), [(0.,0.)],0.,spacer_depth,3.)
    for post in [*plan.get("posts", []), *plan.get("rails", [])]:
        horizontal = post.get("orientation") == "horizontal"
        if horizontal:
            start,end,center=post["x_start_mm"],post["x_end_mm"],post["z_mm"]
            rotation=[[0.,1.,0.],[0.,0.,-1.],[-1.,0.,0.]]
            translation=[start,frame_front,center]
            width=post.get("backing_width_mm",60.)
            rect=(start,center-width/2,end-start,width)
        else:
            start,end,center=post["z_start_mm"],post["z_end_mm"],post["x_mm"]
            rotation=[[1.,0.,0.],[0.,0.,-1.],[0.,1.,0.]]
            translation=[center,frame_front,start]
            width=post.get("backing_width_mm",60.)
            rect=(center-width/2,start,width,end-start)
        if end-start < 1:
            _error("Слишком короткий элемент каркаса")
        screws=[s for s in plan["screws"] if s["post_id"]==post["id"]]
        aps=[a for a in plan["anchors"] if a["post_id"]==post["id"]]
        anchor_holes=[(a["x_mm"],a["z_mm"]) for a in aps]
        clearance_holes=anchor_holes+[(s["x_mm"],s["z_mm"]) for s in screws]
        frame=_move(BRepPrimAPI_MakePrism(section,gp_Vec(0.,end-start,0.)).Shape(),translation,rotation)
        frame=_bore(frame,anchor_holes,frame_front,1.2,3.)
        fid=add("frame-"+post["id"],define("frame-"+post["id"],frame,"frame","V-WT / отрезок точного сечения",{"kind":"exact_section_extrusion","source_id":REQUIRED_SOURCE_IDS[1],"source_component_id":1,"section_axis":"y","section_offset_mm":0.,"section_area_mm2":section_area,"length_mm":end-start,"source_profile_length_mm":240.,"anchor_drill_diameter_mm":6.}))
        rx,rz,rw,rh=rect
        back=_bore(_box([rw,backing,rh],[rx,t+glue,rz]),anchor_holes,t+glue,backing,6.)
        bid=add("backing-"+post["id"],define("backing-"+post["id"],back,"backing","Подоснова 12 мм с доступом к анкерам"))
        contact(bid,fid)
        for panel_id,p in panel_rects:
            x,z=max(rx,p["x_mm"]),max(rz,p["y_mm"])
            w,h=min(rx+rw,p["x_mm"]+p["width_mm"])-x,min(rz+rh,p["y_mm"]+p["height_mm"])-z
            if min(w,h)<=.01: continue
            shape=_bore(_box([w,glue,h],[x,t,z]),clearance_holes,t,glue,6.)
            # A clearance pocket may split a narrow adhesive strip. Preserve
            # each real remaining island instead of exporting a false solid.
            for island in step._shapes(shape,oc["TopAbs_SOLID"],oc):
                if ag._mass(island,oc)<=.001:continue
                gid="glue-"+str(len(bodies)+1)
                add(gid,define(gid,island,"adhesive","Клеевой слой 3 мм с доступом к крепежу"))
                contact(panel_id,gid);contact(gid,bid)
        for screw in screws:
            x,z=screw["x_mm"],screw["z_mm"]
            sid=add("screw-"+screw["id"],"screw-v04",[x,t+glue-2.45,z],[[0.,-1.,0.],[1.,0.,0.],[0.,0.,1.]])
            fastenings.append({"id":"fix-"+screw["id"],"fastener_id":sid,"seat_id":bid,"axis_origin_mm":[x,t+glue,z],"axis_direction":[0.,1.,0.],"shaft_radius_mm":2.11,"head_radius_mm":5.4,"head_height_mm":2.45,"shaft_length_mm":25.,"min_engagement_mm":1.,"engagement_targets":[bid,fid]})
        for anchor in aps:
            x,z=anchor["x_mm"],anchor["z_mm"]
            aid=anchor["id"]
            bolt_id=add("bolt-"+aid,"anchor-bolt",[x,frame_front,z])
            sleeve_id=add("sleeve-"+aid,"anchor-sleeve",[x,substrate_front,z])
            # WT has genuine formed ribs rather than an ideal flat 1.2 mm
            # sheet. Machine the support's saddle from that actual surface.
            spacer_key="anchor-spacer-horizontal" if horizontal else "anchor-spacer-vertical"
            spacer_origin=[x,frame_front+1.2,z]
            if spacer_key not in definitions:
                spacer_world=_cut(_move(spacer,spacer_origin),frame)
                local_spacer=_move(spacer_world,[-v for v in spacer_origin])
                define(spacer_key,local_spacer,"spacer","Проектная дистанционная опора / седло V-WT",{"kind":"project_parametric","saddle_source_id":REQUIRED_SOURCE_IDS[1],"orientation":"horizontal" if horizontal else "vertical","depth_mm":spacer_depth})
            # Identical saddles are true STEP occurrences, not hundreds of
            # copied BReps. Every occurrence still undergoes real contact and
            # collision checks against its own frame and foundation below.
            spacer_id=add("spacer-"+aid,spacer_key,spacer_origin)
            for a,b in ((bolt_id,fid),(bolt_id,spacer_id),(bolt_id,sleeve_id),(spacer_id,fid),(spacer_id,"substrate"),(sleeve_id,"substrate")):
                contact(a,b)
            anchors.append({"id":aid,"bolt_id":bolt_id,"sleeve_id":sleeve_id,"spacer_id":spacer_id,"frame_id":fid,"substrate_id":"substrate","axis_origin_mm":[x,frame_front,z],"axis_direction":[0.,1.,0.],"embedment_mm":embedment,"min_embedment_mm":cfg["anchor_embedment_mm"]})
    return _finish(sources,plan,definitions,instances,solids,bodies,fastenings,contacts,anchors,oc)


def _wall_contacts(solids,bodies,contacts,oc):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    by_id={body["instance_id"]:(body,solid) for body,solid in zip(bodies,solids)}
    faces={};face_bounds={}
    def body_faces(body,shape):
        key=body["instance_id"]
        if key not in faces:faces[key]=step._shapes(shape,oc["TopAbs_FACE"],oc)
        return faces[key]
    result=[]
    for contact in contacts:
        a,sa=by_id[contact["body_a"]];b,sb=by_id[contact["body_b"]]
        fa,fb=body_faces(a,sa),body_faces(b,sb)
        small,small_shape,candidates,other,other_faces,ob=(a,sa,fa,sb,fb,b["bounds_mm"]) if len(fa)<=len(fb) else (b,sb,fb,sa,fa,a["bounds_mm"])
        # Exact set identity: every face under test lies within the small
        # body's AABB, hence F∩B == F∩(B∩box(A)). Cropping a perforated backing
        # around this one sleeve/head avoids rechecking hundreds of unrelated
        # holes for each face. Every actual contact is still measured anew.
        if len(other_faces)>=24 and len(other_faces)>len(candidates)*3:
            box=small["bounds_mm"]
            origin=[v-.01 for v in box["min"]]
            size=[hi-lo+.02 for lo,hi in zip(box["min"],box["max"])]
            other=ag._boolean_shape(BRepAlgoAPI_Common,other,_box(size,origin))
        key=small["instance_id"]
        if key not in face_bounds:face_bounds[key]=[(face,step._bbox(face,oc)) for face in candidates]
        area=0.
        for face,box in face_bounds[key]:
            spans=[min(box["max"][k],ob["max"][k])-max(box["min"][k],ob["min"][k]) for k in range(3)]
            # Two zero extents can only be a line/point, never bearing area.
            if min(spans)<-1e-6 or sum(span>1e-6 for span in spans)<2:continue
            area+=ag._mass(ag._boolean_shape(BRepAlgoAPI_Common,face,other),oc,surface=True)
        ba,bb=a["bounds_mm"],b["bounds_mm"]
        overlap=0. if _overlap_upper_bound(ba,bb)<=.001 else ag._mass(ag._boolean_shape(BRepAlgoAPI_Common,small_shape,other),oc)
        issues=[]
        if area+1e-5<contact["min_area_mm2"]:issues.append("ASSEMBLY_CONTACT_NOT_SEATED")
        if overlap>.001:issues.append("ASSEMBLY_CONTACT_HAS_OVERLAP")
        check={"status":"invalid" if issues else "valid","issues":issues,"contact_area_mm2":area,"overlap_volume_mm3":overlap}
        check.update(**contact,body_a_id=a["id"],body_b_id=b["id"])
        result.append(check)
    return result


def _overlap_upper_bound(a,b):
    return math.prod(max(0.,min(a["max"][k],b["max"][k])-max(a["min"][k],b["min"][k])) for k in range(3))


def _native_engagement(screw,target,spec,oc,cache):
    """Reuse a thread Boolean only after proving the real target is identical.

    Each exported occurrence is cropped independently upstream. In its screw
    frame, compare that exact material to a previously checked fragment using
    both native differences. Reuse requires BOTH differences to contain no
    solid at all; matching bounds, volume or visual appearance alone never
    establish equality. This avoids re-solving the same thread intersections
    hundreds of times while still detecting every local hole or missing edge.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common,BRepAlgoAPI_Cut
    origin=spec["axis_origin_mm"]
    local_screw=_move(screw,[-v for v in origin]);local_target=_move(target,[-v for v in origin])
    pose=local_screw.Location().Transformation()
    # Exact doubles and native TShape identity: no tolerance-based screw-pose
    # cache. Rounded bounds below select candidates only; CSG proves equality.
    key=(local_screw.TShape(),local_screw.Orientation(),
         tuple(pose.Value(i,j) for i in range(1,4) for j in range(1,5)),
         spec["shaft_radius_mm"],spec["shaft_length_mm"],tuple(spec["axis_direction"]))
    bounds=step._bbox(local_target,oc)
    signature=None if bounds is None else tuple(round(v,4) for k in ("min","max") for v in bounds[k])
    options=cache.get((key,signature),[])
    for known in options:
        if (not step._shapes(ag._boolean_shape(BRepAlgoAPI_Cut,local_target,known["target"]),oc["TopAbs_SOLID"],oc)
                and not step._shapes(ag._boolean_shape(BRepAlgoAPI_Cut,known["target"],local_target),oc["TopAbs_SOLID"],oc)):
            return known["metrics"]
    common=ag._boolean_shape(BRepAlgoAPI_Common,local_screw,local_target)
    volume=ag._mass(common,oc)
    metrics={"volume_mm3":volume}
    if volume>.001:
        local_spec={**spec,"axis_origin_mm":[0.,0.,0.]}
        shaft=ag._cylinder(local_spec,spec["shaft_radius_mm"],0.,spec["shaft_length_mm"]+.002)
        engaged=ag._boolean_shape(BRepAlgoAPI_Common,common,shaft)
        engaged_volume=ag._mass(engaged,oc)
        metrics.update(engaged_volume_mm3=engaged_volume,
            outside_shaft_volume_mm3=ag._mass(ag._boolean_shape(BRepAlgoAPI_Cut,common,shaft),oc),
            axial_span_mm=ag._axial_span(engaged,local_spec,oc) if engaged_volume>0 else [0.,0.],
            depth_mm=ag._engagement_depth(engaged,local_spec,oc) if engaged_volume>0 else 0.)
    # Bound the proof cache; a new/unusual fragment is always solved directly.
    if len(cache)<128 or (key,signature) in cache:
        values=cache.setdefault((key,signature),[])
        if len(values)<8:values.append({"target":local_target,"metrics":metrics})
    return metrics


def _wall_collisions(solids,bodies,fastenings,oc,*,localize_fasteners=False):
    """Check all locations, sharing only the identical native screw topology."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common,BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln,gp_Pnt,gp_Dir
    by_id={body["instance_id"]:(body,solid) for body,solid in zip(bodies,solids)}
    localized={}
    def target_near_fastener(fastener_id,target_id):
        target=by_id[target_id][1]
        if not localize_fasteners:return target
        key=(fastener_id,target_id)
        if key not in localized:
            bounds=by_id[fastener_id][0]["bounds_mm"]
            # This is an exact Boolean identity, not a shape-similarity
            # cache: A ∩ B == A ∩ (B ∩ box(A)). The box strictly encloses
            # the complete fastener including head and tip, and each real
            # target/occurrence gets its own cropped solid. Threads are not
            # approximated and no holes or neighbour material are ignored.
            origin=[v-.01 for v in bounds["min"]]
            size=[hi-lo+.02 for lo,hi in zip(bounds["min"],bounds["max"])]
            localized[key]=ag._boolean_shape(BRepAlgoAPI_Common,target,_box(size,origin))
        return localized[key]
    prototypes=[];prepared=[];checks=[];pairs={}
    for fastening in fastenings:
        body,shape=by_id[fastening["fastener_id"]]
        seat_body,seat=by_id[fastening["seat_id"]]
        origin=fastening["axis_origin_mm"]
        local=_move(shape,[-v for v in origin]);transform=local.Location().Transformation()
        signature=tuple(round(transform.Value(i,j),9) for i in range(1,4) for j in range(1,5))
        params=tuple(fastening[k] for k in ("shaft_radius_mm","head_radius_mm","head_height_mm","shaft_length_mm"))+tuple(fastening["axis_direction"])
        prototype=next((p for p in prototypes if p["shape"].IsPartner(shape) and p["signature"]==signature and p["params"]==params),None)
        if prototype is None:
            local_prepared,local_checks=ag._fastening_geometry(solids,bodies,[fastening],oc)
            item=local_prepared[0];check=local_checks[0]
            radius=fastening["head_radius_mm"]
            plane=BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(*origin),gp_Dir(*fastening["axis_direction"])), -radius*1.1,radius*1.1,-radius*1.1,radius*1.1).Face()
            bearing=ag._boolean_shape(BRepAlgoAPI_Common,shape,plane)
            bearing=ag._boolean_shape(BRepAlgoAPI_Cut,bearing,ag._cylinder(fastening,fastening["shaft_radius_mm"]+.05,-.01,.02))
            prototypes.append({"shape":shape,"signature":signature,"params":params,"check":deepcopy(check),"bearing":_move(bearing,[-v for v in origin])})
        else:
            check=deepcopy(prototype["check"])
            check.update(id=fastening["id"],fastener_id=fastening["fastener_id"],fastener_body_id=body["id"],seat_id=fastening["seat_id"],seat_body_id=seat_body["id"],axis_origin_mm=origin)
            # Only envelope/head shape proof is shared. The actual bearing on
            # this actual exported seat is measured independently every time.
            check["issues"]=[issue for issue in check["issues"] if issue!="FASTENER_HEAD_NOT_SEATED"]
            area=ag._mass(ag._boolean_shape(BRepAlgoAPI_Common,_move(prototype["bearing"],origin),target_near_fastener(fastening["fastener_id"],fastening["seat_id"])),oc,surface=True)
            check["bearing_contact_area_mm2"]=area
            if check["head_bearing_area_mm2"]<.1 or area+1e-5<check["required_bearing_area_mm2"]:check["issues"].append("FASTENER_HEAD_NOT_SEATED")
            item={"spec":fastening,"shaft":ag._cylinder(fastening,fastening["shaft_radius_mm"],0.,fastening["shaft_length_mm"]+.002),"check":check}
        prepared.append(item);checks.append(check)
        for target in fastening["engagement_targets"]:pairs[frozenset((fastening["fastener_id"],target))]=(item,target)
    collisions=[];engagements=[];material_proofs={}
    for i,a in enumerate(bodies):
        for j in range(i+1,len(bodies)):
            b=bodies[j];ba,bb=a["bounds_mm"],b["bounds_mm"]
            # A tiny thickness can still have material volume over a very
            # large area. Only the conservative AABB *volume* can prove a
            # pair below the 0.001 mm³ collision threshold without a Boolean.
            if _overlap_upper_bound(ba,bb)<=.001:continue
            if localize_fasteners and a["role"]=="fastener" and b["role"]!="fastener":
                left,right=solids[i],target_near_fastener(a["instance_id"],b["instance_id"])
            elif localize_fasteners and b["role"]=="fastener" and a["role"]!="fastener":
                left,right=target_near_fastener(b["instance_id"],a["instance_id"]),solids[j]
            else:left,right=solids[i],solids[j]
            declaration=pairs.get(frozenset((a["instance_id"],b["instance_id"])))
            proof=None
            if declaration and localize_fasteners:
                item,target=declaration
                screw=by_id[item["spec"]["fastener_id"]][1]
                proof=_native_engagement(screw,target_near_fastener(item["spec"]["fastener_id"],target),item["spec"],oc,material_proofs)
                volume=proof["volume_mm3"]
            else:
                common=ag._boolean_shape(BRepAlgoAPI_Common,left,right);volume=ag._mass(common,oc)
            if volume<=.001:continue
            illegal=volume
            if declaration:
                item,target=declaration;spec,check=item["spec"],item["check"]
                if proof is not None:
                    engaged_volume=proof["engaged_volume_mm3"];outside=proof["outside_shaft_volume_mm3"]
                    span=proof["axial_span_mm"];depth=proof["depth_mm"]
                else:
                    engaged=ag._boolean_shape(BRepAlgoAPI_Common,common,item["shaft"])
                    engaged_volume=ag._mass(engaged,oc)
                    outside=ag._mass(ag._boolean_shape(BRepAlgoAPI_Cut,common,item["shaft"]),oc)
                    span=ag._axial_span(engaged,spec,oc) if engaged_volume>0 else [0.,0.]
                    depth=ag._engagement_depth(engaged,spec,oc) if engaged_volume>0 else 0.
                valid=not check["issues"] and outside<=.001 and depth+1e-5>=spec["min_engagement_mm"] and engaged_volume>.001
                evidence={"fastening_id":spec["id"],"fastener_id":spec["fastener_id"],"target_id":target,"body_a":i+1,"body_b":j+1,"volume_mm3":engaged_volume,"axial_span_mm":span,"depth_mm":depth,"min_engagement_mm":spec["min_engagement_mm"],"outside_shaft_volume_mm3":outside,"status":"valid" if valid else "invalid"}
                check["engagements"].append(evidence)
                if outside>.001:check["issues"].append("FASTENER_HEAD_OR_OFF_AXIS_COLLISION")
                if depth+1e-5<spec["min_engagement_mm"] or engaged_volume<=.001:check["issues"].append("FASTENER_INSUFFICIENT_ENGAGEMENT")
                if valid:engagements.append(evidence);illegal=outside
            if illegal>.001:collisions.append({"body_a":i+1,"body_b":j+1,"volume_mm3":illegal})
    for item in prepared:
        check,spec=item["check"],item["spec"]
        if {e["target_id"] for e in check["engagements"]}!=set(spec["engagement_targets"]):check["issues"].append("FASTENER_TARGET_NOT_REACHED")
        check["issues"]=sorted(set(check["issues"]));check["status"]="invalid" if check["issues"] else "valid"
    return collisions,checks,engagements


def _finish(sources,plan,definitions,instances,solids,bodies,fastenings,contacts,anchors,oc):
    # Native checks use the very solids exported below. Pair checks include all
    # instances, including hardware not intersected by any drawing section.
    content=_export_instanced_step(definitions,instances,oc)
    solids=_portable_solids(content,solids,bodies,oc,reuse_rigid_definitions=True)
    collisions,fastening_checks,engagements=_wall_collisions(solids,bodies,fastenings,oc,localize_fasteners=True)
    contact_checks=_wall_contacts(solids,bodies,contacts,oc)
    by_pair={frozenset((c["body_a"],c["body_b"])):c for c in contact_checks}
    anchor_checks=[]
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common,BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln,gp_Pnt,gp_Dir
    by_id={body["instance_id"]:solid for body,solid in zip(bodies,solids)}
    for anchor in anchors:
        bolt,frame,sleeve,base=anchor["bolt_id"],anchor["frame_id"],anchor["sleeve_id"],anchor["substrate_id"]
        bearing=by_pair[frozenset((bolt,frame))]
        plane=BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(*anchor["axis_origin_mm"]),gp_Dir(*anchor["axis_direction"])), -6.,6.,-6.,6.).Face()
        head_face=ag._boolean_shape(BRepAlgoAPI_Common,by_id[bolt],plane)
        center=anchor["axis_origin_mm"]
        head_face=ag._boolean_shape(BRepAlgoAPI_Cut,head_face,_cylinder(3.05,.02,[center[0],center[1]-.01,center[2]]))
        head_area=ag._mass(head_face,oc,surface=True)
        bearing_area=ag._mass(ag._boolean_shape(BRepAlgoAPI_Common,head_face,by_id[frame]),oc,surface=True)
        embed=by_pair[frozenset((sleeve,base))]
        expected=2*math.pi*4*anchor["embedment_mm"]
        issues=[]
        if bearing["status"]!="valid" or head_area<1 or bearing_area+1e-5<head_area*.9:issues.append("ANCHOR_HEAD_NOT_SEATED")
        if embed["status"]!="valid" or embed["contact_area_mm2"]+1e-4<expected:issues.append("ANCHOR_INSUFFICIENT_MATERIAL_EMBEDMENT")
        anchor_checks.append({**anchor,"status":"invalid" if issues else "valid","bearing_area_mm2":bearing_area,"head_bearing_area_mm2":head_area,"required_bearing_area_mm2":head_area*.9,"sleeve_substrate_contact_mm2":embed["contact_area_mm2"],"required_sleeve_contact_mm2":expected,"issues":issues})
    edges={b["instance_id"]:set() for b in bodies}
    for c in contact_checks:
        if c["status"]=="valid":edges[c["body_a"]].add(c["body_b"]);edges[c["body_b"]].add(c["body_a"])
    for c in engagements:
        edges[c["fastener_id"]].add(c["target_id"]);edges[c["target_id"]].add(c["fastener_id"])
    visited=set();queue=["substrate"]
    while queue:
        current=queue.pop()
        if current in visited:continue
        visited.add(current);queue.extend(edges[current]-visited)
    disconnected=sorted(set(edges)-visited)
    issues=[]
    if collisions:issues.append("UNEXPECTED_SOLID_INTERSECTIONS")
    if any(c["status"]!="valid" for c in fastening_checks):issues.append("INVALID_SCREW_CONNECTION")
    if any(c["status"]!="valid" for c in contact_checks):issues.append("INVALID_BODY_CONTACT")
    if any(c["status"]!="valid" for c in anchor_checks):issues.append("INVALID_ANCHOR")
    if disconnected:issues.append("DISCONNECTED_WALL_COMPONENTS")
    bom=[]
    for key,item in definitions.items():
        count=sum(i["definition_id"]==key for i in instances)
        if not count:continue
        bom.append({"definition_id":key,"role":item["role"],"label":item["label"],"count":count,"volume_mm3":ag._mass(item["shape"],oc),"provenance":item["provenance"]})
    preview=_preview(definitions,instances,oc)
    from .mount_assemblies import digest
    return {"schema":"whole-wall-assembly-build/1","plan_id":plan.get("id"),"spec_sha256":digest(plan),
            "step":{"content_base64":base64.b64encode(content).decode(),"byte_length":len(content),"sha256":hashlib.sha256(content).hexdigest()},
            "bodies":bodies,"bom":bom,"collisions":collisions,"fastening_checks":fastening_checks,"intentional_engagements":engagements,"contact_checks":contact_checks,"anchor_checks":anchor_checks,
            "checks":{"status":"invalid" if issues else "valid","issues":issues,"body_count":len(bodies),"fastener_count":len(fastenings),"anchor_count":len(anchors),"contact_count":len(contacts),"connected_body_count":len(visited),"disconnected_instances":disconnected,"collision_pair_scope":"all_instances_aabb_then_exact_brep"},
            "preview":preview,"assumptions":["Анкерный болт, втулка и дистанционная опора — параметрические проектные детали, не паспорт изготовителя анкера.","Проверены реальные объёмы, посадки, контакт с основанием и глубина заложения; несущая способность, раскрытие анкера и усилие затяжки не вычисляются.","V-WT получен выдавливанием точного исходного сечения; длина исходного STEP не масштабировалась."]}


def _export_instanced_step(definitions,instances,oc):
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TopoDS import TopoDS_Compound
    from OCP.BRep import BRep_Builder
    from OCP.TopLoc import TopLoc_Location
    from OCP.gp import gp_Trsf
    from OCP.Interface import Interface_Static
    from OCP.TDataStd import TDataStd_Name
    doc=TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    tool=XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    compound=TopoDS_Compound();BRep_Builder().MakeCompound(compound)
    root=tool.AddShape(compound,True)
    used={item["definition_id"] for item in instances}
    labels={key:tool.AddShape(item["shape"],False) for key,item in definitions.items() if key in used}
    for key,label in labels.items():TDataStd_Name.Set_s(label,TCollection_ExtendedString(definitions[key]["label"]))
    for item in instances:
        trsf=gp_Trsf();trsf.SetValues(*[v for row,shift in zip(item["rotation"],item["translation_mm"]) for v in (*row,shift)])
        label=tool.AddComponent(root,labels[item["definition_id"]],TopLoc_Location(trsf))
        TDataStd_Name.Set_s(label,TCollection_ExtendedString(item["id"]))
    tool.UpdateAssemblies()
    writer=STEPCAFControl_Writer()
    previous=Interface_Static.IVal_s("write.surfacecurve.mode")
    try:
        Interface_Static.SetIVal_s("write.surfacecurve.mode",0)
        if not writer.Transfer(doc,STEPControl_AsIs):_error("Не удалось подготовить STEP стены","WALL_ASSEMBLY_EXPORT_FAILED")
        output=io.BytesIO()
        if writer.Writer().WriteStream(output)!=oc["IFSelect_RetDone"]:_error("Не удалось записать STEP стены","WALL_ASSEMBLY_EXPORT_FAILED")
    finally:Interface_Static.SetIVal_s("write.surfacecurve.mode",previous)
    content=output.getvalue()
    header,separator,data=content.partition(b"ENDSEC;")
    header,count=re.subn(rb"(FILE_NAME\s*\(\s*'(?:''|[^'])*'\s*,\s*)'(?:''|[^'])*'",rb"\1'2000-01-01T00:00:00'",header,count=1)
    if count!=1 or not separator:_error("Некорректный заголовок STEP стены")
    return header+separator+data


def _portable_solids(content,original,bodies,oc,*,reuse_rigid_definitions=False):
    """Read exported occurrences back, preserving every repeated screw."""
    reader=oc["STEPControl_Reader"]()
    if reader.ReadStream("whole-wall.step",io.BytesIO(content))!=oc["IFSelect_RetDone"]:
        _error("Не удалось повторно прочитать STEP стены","WALL_ASSEMBLY_EXPORT_FAILED")
    reader.SetSystemLengthUnit(1.)
    if reader.TransferRoots()<=0:_error("Экспорт стены не содержит тел","WALL_ASSEMBLY_EXPORT_FAILED")
    imported=step._shapes(reader.OneShape(),oc["TopAbs_SOLID"],oc)
    if len(imported)!=len(original):_error("Экспорт изменил число реальных тел стены","WALL_ASSEMBLY_EXPORT_FAILED")
    # Each exported occurrence has a fixed exact bound. Compute it once,
    # rather than repeating native bounds during every candidate comparison
    # in this permutation search (quadratic native work on long walls).
    remaining=[(shape,step._bbox(shape,oc)) for shape in imported];ordered=[]
    topology=[]
    def measures(shape):
        # Native IsPartner means the SAME underlying TShape, not a similar
        # BRep signature. Its topology/volume are invariant under the rigid
        # unit-scale locations exported by this builder. Bounds are still
        # measured for every occurrence; bearing and engagements remain
        # independent per real location below.
        rigid=abs(shape.Location().Transformation().ScaleFactor()-1.)<1e-12
        if reuse_rigid_definitions and rigid:
            known=next((entry[1:] for entry in topology if entry[0].IsPartner(shape) and entry[0].Orientation()==shape.Orientation()),None)
            if known is not None:return known
        result=(oc["BRepCheck_Analyzer"](shape).IsValid(),len(step._shapes(shape,oc["TopAbs_FACE"],oc)),ag._mass(shape,oc))
        if reuse_rigid_definitions and rigid:topology.append((shape,*result))
        return result
    for body,source in zip(bodies,original):
        expected=body["bounds_mm"]
        match=None
        for i,(shape,bounds) in enumerate(remaining):
            if all(abs(bounds[k][j]-expected[k][j])<1e-4 for k in ("min","max") for j in range(3)):
                match=i;break
        if match is None:_error("Экспорт сместил деталь стены","WALL_ASSEMBLY_EXPORT_FAILED",{"instance_id":body["instance_id"]})
        shape,_=remaining.pop(match)
        valid,count,volume=measures(shape)
        if not valid or count!=len(step._shapes(source,oc["TopAbs_FACE"],oc)) or abs(volume-body["volume_mm3"])>max(.001,body["volume_mm3"]*1e-8):
            _error("Экспорт изменил точную геометрию детали","WALL_ASSEMBLY_EXPORT_FAILED",{"instance_id":body["instance_id"]})
        ordered.append(shape)
    return ordered


def _preview(definitions,instances,oc):
    # A bounded wire preview is explicitly not the machining model. Native
    # STEP always retains exact screw threads; repeated preview definitions
    # avoid emitting tens of thousands of duplicated thread coordinates.
    result=[];budget=0
    for key,item in definitions.items():
        shape=item["shape"];bounds=step._bbox(shape,oc);paths=[]
        if item["role"] not in ("fastener",) and budget<MAX_PREVIEW_POINTS-500:
            for edge in step._shapes(shape,oc["TopAbs_EDGE"],oc):
                points,_=step._sample_edge(edge,oc,preview=True)
                if budget+len(points)>MAX_PREVIEW_POINTS:break
                budget+=len(points);paths.append(points)
        result.append({"id":key,"role":item["role"],"label":item["label"],"bounds_mm":bounds,"paths":paths,"representation":"exact_edges" if paths else "bounds_only"})
    return {"schema":"wall-assembly-wire-preview/1","definitions":result,"instances":instances,"point_count":budget,"note":"Упрощённый каркасный просмотр; точная резьба крепежа сохранена в STEP."}
