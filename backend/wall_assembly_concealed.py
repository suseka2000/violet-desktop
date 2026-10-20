"""Exact-section VH wall adaptation; no substitutions for unsupported systems.

Panels are actually machined, not glued to the V0 frame under a hidden label.
The continuous backing, WT frame and anchors are explicit project parts; the
supplier section and V04 screw remain pinned to the supplied source bytes.
"""
from __future__ import annotations

from . import assembly_geometry as ag, step_geometry as step
from .assembly_fasteners import CALIBRATIONS, PROFILE_SOURCES
from .wall_assembly_geometry import (_error, _finite, _move, _box, _cylinder,
    _cut, _fuse, _bore, _wt_section, _panel_groups, _finish, IDENTITY)


def source_ids(plan):
    system=plan.get("system")
    if not isinstance(system,dict):_error("Некорректное описание скрытой системы")
    key = system.get("key")
    if key not in {"vh2", "vh3"}:
        _error("Неизвестная скрытая система полной стены")
    perimeter=system.get("perimeter_families", [])
    if not isinstance(perimeter,list) or any(not isinstance(p,str) or p not in {"sky","start"} for p in perimeter) or perimeter != sorted(set(perimeter)):
        _error("Неизвестный комплект профилей периметра")
    return (CALIBRATIONS["screw"]["source_id"], CALIBRATIONS["frame"]["source_id"],
            PROFILE_SOURCES[key], PROFILE_SOURCES["lstart"],
            *(PROFILE_SOURCES[name] for name in perimeter))


def validate_concealed_plan(plan):
    from .wall_systems import HIDDEN_RULES, FAMILIES, PERIMETER_VERSION, source_models_for_system
    from .assembly_recipes_concealed import DOCUMENT_HASHES
    system, layers, mount = plan["system"], plan["layers"], plan.get("mount_configuration", {})
    if any(not isinstance(item,dict) for item in (system,layers,mount)):
        _error("Некорректное описание слоёв скрытой стены")
    key, edge = system.get("key"), system.get("panel_edge")
    source_ids(plan)
    selected=system.get("selected_rule_id")
    codes=system.get("component_codes")
    if not isinstance(selected,str) or not isinstance(codes,list) or any(not isinstance(code,str) for code in codes):
        _error("Некорректный перечень компонентов скрытой системы")
    if HIDDEN_RULES.get(selected)!=key or (set(codes) & FAMILIES)!={"VH-2" if key=="vh2" else "VH-3"}:
        _error("Выбранная страница методички не соответствует семейству скрытой стены")
    perimeter = system.get("perimeter_families", [])
    if not isinstance(perimeter, list) or any(not isinstance(p,str) or p not in {"sky", "start"} for p in perimeter) or perimeter != sorted(set(perimeter)):
        _error("Неизвестный профиль периметра скрытой системы")
    if perimeter and system.get("perimeter_version") != PERIMETER_VERSION:
        _error("Неизвестная версия измеренной посадки периметра")
    if system.get("source_models")!=source_models_for_system(key, perimeter):
        _error("Набор исходных моделей скрытой стены не соответствует калибровке")
    reference_list=system.get("source_refs")
    if not isinstance(reference_list,list) or len(reference_list)>20 or any(not isinstance(r,dict) or not isinstance(r.get("rule_id"),str) for r in reference_list):
        _error("Некорректные ссылки на исходные страницы скрытого монтажа")
    refs={r["rule_id"]:r for r in reference_list}
    required={selected,selected+"-joint","concealed-edges","concealed-profile-vh2-vh3","concealed-profile-angstart"}
    if not required.issubset(refs) or any(refs[r].get("document_id")!="violet-concealed-mount-2025" or refs[r].get("sha256")!=DOCUMENT_HASHES["violet-concealed-mount-2025"] for r in required):
        _error("Не подтверждены исходные страницы размеров скрытого монтажа")
    if system.get("supported") is not True or mount.get("mounting_system") != "concealed_mount" or mount.get("panel_edge") != edge:
        _error("Скрытая система не согласована с назначением стены")
    if mount.get("allow_project_assumptions") is not True or plan.get("configuration",{}).get("allow_project_assumptions") is not True:
        _error("Проектная адаптация скрытого каркаса не разрешена")
    if abs(_finite(mount.get("panel_thickness_mm"))-_finite(layers.get("panel_thickness_mm")))>1e-7:
        _error("Толщина скрытой панели не соответствует назначению")
    # VH3's seam alone permits12.5, but this whole-wall perimeter uses the
    # L-start rear screw head whose measured front is12.25. Do not silently
    # thin a12.5 panel merely to accommodate the boundary hardware.
    minimum, maximum = (9.6 if edge == "Vc" else 9.1), 12.2
    if edge not in {"Vb", "Vc", "Vd"} or not minimum <= _finite(layers.get("panel_thickness_mm")) <= maximum:
        _error("Панель не соответствует измеренной скрытой посадке")
    low, high = (3., 5.) if key == "vh2" else (16., 18.)
    if not low <= _finite(mount.get("joint_mm")) <= high:
        _error("Шов не соответствует реальному VH-профилю")
    expected = {"adhesive_thickness_mm":0., "backing_thickness_mm":12. if key == "vh2" else 10.,
                "backing_front_mm":15.9, "frame_depth_mm":27.}
    for name, value in expected.items():
        if abs(_finite(layers.get(name)) - value) > 1e-7:
            _error("Изменена измеренная цепочка скрытого крепления", details={"field":name})
    for name in ("profile_runs", "profile_screws"):
        items=plan.get(name)
        if not isinstance(items,list) or not items:
            _error("Некорректный список скрытых профилей или креплений")
        ids=set()
        for item in items:
            if not isinstance(item,dict) or not isinstance(item.get("id"),str) or not item["id"] or item["id"] in ids:
                _error("Повторяющийся идентификатор скрытой детали")
            ids.add(item["id"])
            for field,value in item.items():
                if field.endswith("_mm"):_finite(value)
    runs={run["id"]:run for run in plan["profile_runs"]}
    for run in runs.values():
        if run.get("family") not in {key,"lstart", *perimeter} or run.get("axis") not in {"vertical","horizontal"}:
            _error("Профиль не принадлежит выбранной скрытой системе")
        if run.get("inward_sign",1) not in {-1,1} or isinstance(run.get("inward_sign"),bool):
            _error("Некорректное направление кромочного профиля")
        if _finite(run.get("end_mm"))-_finite(run.get("start_mm"))<=1:
            _error("Слишком короткий профиль")
        _finite(run.get("edge_mm"))
        if run["family"] == "sky" and (run.get("context") != "ceiling" or run["axis"] != "horizontal" or run.get("inward_sign") != -1
                or _finite(mount.get("ceiling_gap_mm")) < 8 or abs(run["edge_mm"]-(plan["wall"]["height_mm"]-mount["ceiling_gap_mm"])) > 1e-6):
            _error("V-Sky не соответствует реальному верхнему торцу и зазору до потолка")
        if run["family"] == "start" and (run.get("context") != "floor" or run["axis"] != "horizontal" or run.get("inward_sign") != 1
                or abs(_finite(mount.get("floor_gap_mm"))-59) > 1e-6 or abs(run["edge_mm"]-59) > 1e-6):
            _error("Полка V-Start1 не соответствует отметке низа панели 59 мм")
    existing={item["id"] for item in plan["screws"]}
    for screw in plan["profile_screws"]:
        if screw["id"] in existing or screw.get("profile_id") not in runs:
            _error("Саморез скрытого профиля не связан с реальным прогоном")
        run=runs[screw["profile_id"]]
        expected_y=5.5 if run["family"]=="vh3" else layers["panel_thickness_mm"] if run["family"]=="start" else 14.7
        if abs(_finite(screw.get("seat_y_mm"))-expected_y)>1e-7:
            _error("Изменена измеренная посадка головки скрытого самореза")
        _finite(screw.get("x_mm"));_finite(screw.get("z_mm"))
    from .wall_framing import validate_hidden_layout
    try:validate_hidden_layout(plan)
    except (ValueError,KeyError,TypeError) as exc:
        _error("Расстановка скрытого крепежа не прошла независимую проверку",details={"reason":str(exc)})


def concealed_fastening_specs(plan, bodies):
    """Pure reconstruction used by the portable record validator as well.

Backing ownership is derived from geometric instance bounds, never copied
from claimed fastening checks. Native engagement subsequently proves that
the actual material (rather than a window/access hole in those bounds) exists.
"""
    backs=[b for b in bodies if b.get("role")=="backing"]
    def backing_at(x,z):
        matches=[b for b in backs if b["bounds_mm"]["min"][0]-1e-6<=x<=b["bounds_mm"]["max"][0]+1e-6
                 and b["bounds_mm"]["min"][2]-1e-6<=z<=b["bounds_mm"]["max"][2]+1e-6]
        if len(matches)!=1:_error("Крепёж не попадает в единственную часть подосновы")
        return matches[0]["instance_id"]
    specs=[]
    for profile,collection in ((False,plan["screws"]),(True,plan["profile_screws"])):
        for item in collection:
            x,z=item["x_mm"],item["z_mm"];back=backing_at(x,z)
            seat="profile-"+item["profile_id"] if profile else back
            targets=[seat,back] if profile else [back,"frame-"+item["post_id"]]
            specs.append({"id":"fix-"+item["id"],"fastener_id":"screw-"+item["id"],"seat_id":seat,
                "axis_origin_mm":[x,item["seat_y_mm"] if profile else 15.9,z],"axis_direction":[0.,1.,0.],
                "shaft_radius_mm":2.11,"head_radius_mm":5.4,"head_height_mm":2.45,
                "shaft_length_mm":25.,"min_engagement_mm":1.,"engagement_targets":targets})
    return specs


def hidden_evidence_spec(plan,bodies):
    """Portable expected identities, seats and mandatory connectivity."""
    validate_concealed_plan(plan)
    by_id={b["instance_id"]:b for b in bodies}
    roles={"substrate":"substrate"};pairs=[]
    backs=[b for b in bodies if b.get("role")=="backing"]
    if not backs or {b["instance_id"] for b in backs}!={f"backing-{i}" for i in range(1,len(backs)+1)}:
        _error("Неизвестные части сплошной подосновы")
    for b in backs:roles[b["instance_id"]]="backing"
    bond_profiles = {}
    for run in plan["profile_runs"]:
        if run["family"] not in {"sky", "start"}:
            continue
        prefix=f"bond-{run['id']}-"
        bonds=sorted((b for b in bodies if b.get("instance_id", "").startswith(prefix)),key=lambda b:b["instance_id"])
        if not bonds or {b["instance_id"] for b in bonds}!={prefix+str(i) for i in range(1,len(bonds)+1)}:
            _error("Нет частей проектного соединения профиля периметра")
        for bond in bonds:
            roles[bond["instance_id"]]="adhesive"
            bond_profiles[bond["instance_id"]]="profile-"+run["id"]
            pairs.append([bond["instance_id"],"profile-"+run["id"]])
    for index,_ in enumerate(_panel_groups(plan["panels"]),1):roles[f"panel-{index}"]="panel"
    for run in plan["profile_runs"]:
        pid="profile-"+run["id"];roles[pid]="profile"
        provenance=by_id.get(pid,{}).get("provenance",{})
        if provenance.get("kind")!="exact_section_extrusion" or provenance.get("source_id")!=PROFILE_SOURCES[run["family"]] or abs(provenance.get("length_mm",0)-(run["end_mm"]-run["start_mm"]))>1e-6:
            _error("Профиль стены не соответствует точному источнику и длине")
    for member in [*plan["posts"],*plan.get("rails",[])]:
        fid="frame-"+member["id"];roles[fid]="frame"
        if fid not in by_id:_error("Отсутствует реальный элемент каркаса")
        frame=by_id[fid]
        for back in backs:
            if all(min(frame["bounds_mm"]["max"][k],back["bounds_mm"]["max"][k])-max(frame["bounds_mm"]["min"][k],back["bounds_mm"]["min"][k])>.01 for k in (0,2)):
                pairs.append([fid,back["instance_id"]])
    fastenings=concealed_fastening_specs(plan,bodies)
    for spec in fastenings:roles[spec["fastener_id"]]="fastener"
    for anchor in plan["anchors"]:
        aid=anchor["id"]
        roles.update({"bolt-"+aid:"anchor_bolt","sleeve-"+aid:"anchor_sleeve","spacer-"+aid:"spacer"})
    return {"fastenings":fastenings,"required_roles":roles,"required_pairs":pairs,"perimeter_bond_profiles":bond_profiles,
            "frame_front_mm":15.9+plan["layers"]["backing_thickness_mm"]}


def _profile_face(source,family,oc,thickness=12.):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln,gp_Pnt,gp_Dir
    axis=(1,0,0) if family in {"vh2", "sky", "start"} else (0,1,0)
    plane=BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0,0,0),gp_Dir(*axis)),-100,100,-100,100).Face()
    face=ag._boolean_shape(BRepAlgoAPI_Common,source,plane)
    if len(step._shapes(face,oc["TopAbs_FACE"],oc))!=1 or ag._mass(face,oc,surface=True)<=1:
        _error("Исходный скрытый профиль не имеет единого точного сечения")
    rotation,translation=({"vh2":([[0,0,1],[0,-1,0],[1,0,0]],[27.36,15.9,0]),
                          "vh3":([[1,0,0],[0,0,1],[0,-1,0]],[0,15.9,0]),
                          "lstart":([[-1,0,0],[0,0,1],[0,1,0]],[-2,15.9,0]),
                          "sky":([[0,1,0],[0,0,1],[1,0,0]],[0,thickness,0]),
                          "start":([[0,1,0],[0,0,1],[1,0,0]],[-59,thickness+1,0])})[family]
    return _move(face,translation,rotation)


def _run_transform(run):
    sign=run.get("inward_sign",1) if run["family"] in {"lstart", "sky", "start"} else 1
    edge,start,end=run["edge_mm"],run["start_mm"],run["end_mm"]
    if run["axis"]=="vertical":
        return ([edge,0,start],IDENTITY) if sign==1 else ([edge,0,end],[[-1,0,0],[0,1,0],[0,0,-1]])
    return ([end,0,edge],[[0,0,-1],[0,1,0],[1,0,0]]) if sign==1 else ([start,0,edge],[[0,0,1],[0,1,0],[-1,0,0]])


def machining_tools(plan):
    """Actual axis-aligned cutters in the wall's X/depth-Y/elevation-Z frame."""
    edge=plan["system"]["panel_edge"];depth=11. if edge=="Vb" else 16.;width=3.1 if edge=="Vc" else 2.6
    thickness=plan["layers"]["panel_thickness_mm"];gap=plan["mount_configuration"]["joint_mm"]
    result=[]
    for run in plan["profile_runs"]:
        if run["family"] in {"sky", "start"}:
            continue
        sides=[(0.,run.get("inward_sign",1))] if run["family"]=="lstart" else [(-gap/2,-1),(gap/2,1)]
        for offset,sign in sides:
            line=run["edge_mm"]+offset
            for kind,across,front,deep in [("edge_groove",depth,5.5,width)]+([("rear_edge_relief",5.,8.6,thickness-8.6+.01)] if edge=="Vc" else []):
                low=line-.01 if sign==1 else line-across
                if run["axis"]=="vertical":origin=[low,front,run["start_mm"]];size=[across+.01,deep,run["end_mm"]-run["start_mm"]]
                else:origin=[run["start_mm"],front,low];size=[run["end_mm"]-run["start_mm"],deep,across+.01]
                result.append({"profile_id":"profile-"+run["id"],"kind":kind,"origin_mm":origin,"size_mm":size})
    return result


def build_concealed_wall(sources,plan):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec
    oc=step._occt();loaded={key:step._load(value,oc)[1][0] for key,value in sources.items()}
    definitions,instances,solids,bodies,contacts,anchors={ },[],[],[],[],[]
    def define(key,shape,role,label,provenance=None):
        if key not in definitions:
            if not oc["BRepCheck_Analyzer"](shape).IsValid() or len(step._shapes(shape,oc["TopAbs_SOLID"],oc))!=1:
                _error("Скрытая деталь не является одним корректным телом",details={"definition":key})
            definitions[key]={"shape":shape,"role":role,"label":label,"volume_mm3":ag._mass(shape,oc),"provenance":provenance or {"kind":"project_parametric"}}
        return key
    def add(identifier,key,xyz=(0.,0.,0.),rotation=None):
        item=definitions[key];shape=_move(item["shape"],xyz,rotation)
        bodies.append({"id":len(bodies)+1,"instance_id":identifier,"definition_id":key,"role":item["role"],"label":item["label"],
                       "bounds_mm":step._bbox(shape,oc),"volume_mm3":item["volume_mm3"],"provenance":item["provenance"]})
        solids.append(shape);instances.append({"id":identifier,"definition_id":key,"rotation":rotation or IDENTITY,"translation_mm":list(xyz),"role":item["role"]})
        return identifier
    contact_pairs=set()
    def contact(a,b):
        pair=tuple(sorted((a,b)))
        if a!=b and pair not in contact_pairs:
            contact_pairs.add(pair);contacts.append({"id":f"contact-{len(contacts)+1}","body_a":a,"body_b":b,"min_area_mm2":.1})
    layers,cfg=plan["layers"],plan["configuration"]
    t,backing=layers["panel_thickness_mm"],layers["backing_thickness_mm"]
    front=15.9;frame_front=front+backing;base_front=layers["substrate_front_mm"]
    length,height=plan["wall"]["length_mm"],plan["wall"]["height_mm"]
    base_depth,embedment=cfg["substrate_thickness_mm"],cfg["anchor_embedment_mm"]
    if base_front<frame_front+27:_error("Каркас скрытой стены не помещается в полости")
    def cut_openings(shape,y,depth,openings=None):
        from .wall_opening_bounds import opening_cut_bounds
        for opening in plan["openings"] if openings is None else openings:
            x,z,right,top=opening_cut_bounds(opening,length,height,outside=.01)
            shape=_cut(shape,_box([right-x,depth+.02,top-z],[x,y-.01,z]))
        return shape
    holes=[(a["x_mm"],a["z_mm"]) for a in plan["anchors"]]
    from .wall_framing import substrate_interval, substrate_openings
    base_start,base_end=substrate_interval(plan["wall"])
    base=_bore(cut_openings(_box([base_end-base_start,base_depth,height],[base_start,base_front,0]),base_front,base_depth,substrate_openings(plan)),holes,base_front,embedment,4.)
    add("substrate",define("substrate",base,"substrate","Основание с монтажными отверстиями"))
    back=_bore(cut_openings(_box([length,backing,height],[0,front,0]),front,backing),holes,front,backing,6.)
    backing_bodies=[]
    for i,shape in enumerate(sorted(step._shapes(back,oc["TopAbs_SOLID"],oc),key=lambda s:tuple(step._bbox(s,oc)["min"])),1):
        bid=add(f"backing-{i}",define(f"backing-{i}",shape,"backing",f"Сплошная проектная подоснова {backing:g} мм"))
        backing_bodies.append((bid,shape,step._bbox(shape,oc)))
    machining=[];panel_bodies=[];tools=machining_tools(plan)
    for number,group in enumerate(_panel_groups(plan["panels"]),1):
        shape=_fuse([_box([p["width_mm"],t,p["height_mm"]],[p["x_mm"],0,p["y_mm"]]) for p in group])
        pid=f"panel-{number}";bounds=step._bbox(shape,oc)
        for tool in tools:
            origin,size=tool["origin_mm"],tool["size_mm"]
            if all(min(bounds["max"][k],origin[k]+size[k])-max(bounds["min"][k],origin[k])>1e-6 for k in range(3)):
                cutter=_box(size,origin);before=ag._mass(shape,oc);shape=_cut(shape,cutter);removed=before-ag._mass(shape,oc)
                if removed>.001:machining.append({**tool,"panel_id":pid,"removed_volume_mm3":removed})
        add(pid,define(pid,shape,"panel",f"Панель {group[0].get('panel_id',group[0]['id'])} / пазы {plan['system']['panel_edge']}",{"kind":"project_machined_panel","edge":plan["system"]["panel_edge"]}))
        panel_bodies.append((pid,shape))
    sections={family:_profile_face(loaded[PROFILE_SOURCES[family]],family,oc,t) for family in {r["family"] for r in plan["profile_runs"]}}
    profile_bodies=[]
    for run in plan["profile_runs"]:
        extent=run["end_mm"]-run["start_mm"];xyz,rotation=_run_transform(run)
        definition=f"profile-section-{run['family']}-{extent:.6f}"
        shape=BRepPrimAPI_MakePrism(sections[run["family"]],gp_Vec(0,0,extent)).Shape()
        define(definition,shape,"profile",f"{run['family'].upper()} / точное исходное сечение",{"kind":"exact_section_extrusion","source_id":PROFILE_SOURCES[run["family"]],"section_axis":"x" if run["family"] in {"vh2", "sky", "start"} else "y","section_offset_mm":0.,"length_mm":extent})
        pid=add("profile-"+run["id"],definition,xyz,rotation);profile_bodies.append((pid,solids[-1]))
    # Exact V-Sky / Start rear flanges sit at a different depth than VH.
    # Bridge to the existing common backing with an explicit project bond,
    # without moving the panel face, stretching a profile or hiding an air gap.
    perimeter_bonds=[]
    for run in plan["profile_runs"]:
        if run["family"] not in {"sky", "start"}:
            continue
        rear=t+(1.2 if run["family"]=="sky" else 1.)
        if rear >= front-1e-6:
            _error("Нет места для проектного соединения профиля с подосновой")
        transverse=2. if run["family"]=="sky" else -42.
        width=30. if run["family"]=="sky" else 26.
        xyz,rotation=_run_transform(run)
        bond=_move(_box([width,front-rear,run["end_mm"]-run["start_mm"]],[transverse,rear,0]),xyz,rotation)
        # Existing frame-screw heads and anchor access holes stay open. The
        # Start clamp screw has only a shaft here, not a buried head.
        bond=_bore(bond,[(s["x_mm"],s["z_mm"]) for s in plan["screws"]],rear,front-rear,5.6)
        bond=_bore(bond,holes,rear,front-rear,6.)
        bond=_bore(bond,[(s["x_mm"],s["z_mm"]) for s in plan["profile_screws"] if s["profile_id"]==run["id"]],rear,front-rear,2.12)
        for number,shape in enumerate(step._shapes(bond,oc["TopAbs_SOLID"],oc),1):
            identifier=f"bond-{run['id']}-{number}"
            add(identifier,define(identifier,shape,"adhesive","Проектное соединение профиля периметра с подосновой",
                {"kind":"project_perimeter_bond","profile_id":"profile-"+run["id"],"source_id":PROFILE_SOURCES[run["family"]],
                 "depth_mm":front-rear}))
            perimeter_bonds.append((identifier,shape))
            contact("profile-"+run["id"],identifier)
    # These are candidate constraints, not approvals. Every declared contact
    # is checked with exact Common on the exported/reimported solids in
    # _finish; a wrong candidate makes the complete assembly invalid.
    from .wall_framing import _panel_profile_contact
    panel_groups=list(_panel_groups(plan["panels"]))
    for run in plan["profile_runs"]:
        for index,group in enumerate(panel_groups,1):
            if any(_panel_profile_contact(p,run,plan["mount_configuration"]["joint_mm"]) for p in group):
                contact("profile-"+run["id"],f"panel-{index}")
    def find_contacts(left,right):
        by_id={b["instance_id"]:b for b in bodies}
        for aid,ashape in left:
            for bid,bshape in right:
                a,b=by_id[aid],by_id[bid]
                if any(min(a["bounds_mm"]["max"][k],b["bounds_mm"]["max"][k])-max(a["bounds_mm"]["min"][k],b["bounds_mm"]["min"][k]) < -1e-6 for k in range(3)):continue
                contact(aid,bid)
    find_contacts(profile_bodies,[(a,b) for a,b,_ in backing_bodies])
    find_contacts(perimeter_bonds,[(a,b) for a,b,_ in backing_bodies])
    section,area=_wt_section(loaded[CALIBRATIONS["frame"]["source_id"]],oc)
    frame_bodies=[]
    for member in [*plan["posts"],*plan.get("rails",[])]:
        horizontal=member.get("orientation")=="horizontal"
        if horizontal:start,end,center=member["x_start_mm"],member["x_end_mm"],member["z_mm"];rot=[[0,1,0],[0,0,-1],[-1,0,0]];xyz=[start,frame_front,center]
        else:start,end,center=member["z_start_mm"],member["z_end_mm"],member["x_mm"];rot=[[1,0,0],[0,0,-1],[0,1,0]];xyz=[center,frame_front,start]
        aps=[a for a in plan["anchors"] if a["post_id"]==member["id"]]
        frame=_move(BRepPrimAPI_MakePrism(section,gp_Vec(0,end-start,0)).Shape(),xyz,rot)
        frame=_bore(frame,[(a["x_mm"],a["z_mm"]) for a in aps],frame_front,1.2,3.)
        fid=add("frame-"+member["id"],define("frame-"+member["id"],frame,"frame","V-WT / точное сечение проектного каркаса",{"kind":"exact_section_extrusion","source_id":CALIBRATIONS["frame"]["source_id"],"section_axis":"y","section_offset_mm":0.,"length_mm":end-start,"section_area_mm2":area}))
        frame_bodies.append((fid,frame))
    find_contacts(frame_bodies,[(a,b) for a,b,_ in backing_bodies])
    define("screw-v04",loaded[CALIBRATIONS["screw"]["source_id"]],"fastener","Саморез V04 / исходный STEP",{"kind":"source_step","source_id":CALIBRATIONS["screw"]["source_id"],"component_id":1})
    fastenings=concealed_fastening_specs(plan,bodies)
    for spec in fastenings:
        x,y,z=spec["axis_origin_mm"]
        add(spec["fastener_id"],"screw-v04",[x,y-2.45,z],[[0,-1,0],[1,0,0],[0,0,1]])
    sleeve=_cut(_cylinder(4,embedment),_cylinder(3,embedment+.02,[0,-.01,0]))
    define("anchor-sleeve",sleeve,"anchor_sleeve","Проектная анкерная втулка 8/6 мм")
    bolt=_fuse([_cylinder(3,base_front-frame_front+embedment),_cylinder(5,4,[0,-4,0])])
    define("anchor-bolt",bolt,"anchor_bolt","Проектный анкерный болт 6 мм")
    depth=base_front-frame_front-1.2
    spacer=_bore(_box([12,depth,12],[-6,0,-6]),[(0,0)],0,depth,3.)
    frame_by_id=dict(frame_bodies)
    members={m["id"]:m for m in [*plan["posts"],*plan.get("rails",[])]}
    for anchor in plan["anchors"]:
        aid,x,z=anchor["id"],anchor["x_mm"],anchor["z_mm"]
        fid="frame-"+anchor["post_id"];horizontal=members[anchor["post_id"]].get("orientation")=="horizontal"
        bid=add("bolt-"+aid,"anchor-bolt",[x,frame_front,z]);sid=add("sleeve-"+aid,"anchor-sleeve",[x,base_front,z])
        skey="anchor-spacer-horizontal" if horizontal else "anchor-spacer-vertical";origin=[x,frame_front+1.2,z]
        if skey not in definitions:
            saddle=_move(_cut(_move(spacer,origin),frame_by_id[fid]),[-v for v in origin])
            define(skey,saddle,"spacer","Проектная дистанционная опора / седло V-WT",{"kind":"project_parametric","saddle_source_id":CALIBRATIONS["frame"]["source_id"],"depth_mm":depth})
        spid=add("spacer-"+aid,skey,origin)
        for a,b in ((bid,fid),(bid,spid),(bid,sid),(spid,fid),(spid,"substrate"),(sid,"substrate")):contact(a,b)
        anchors.append({"id":aid,"bolt_id":bid,"sleeve_id":sid,"spacer_id":spid,"frame_id":fid,"substrate_id":"substrate","axis_origin_mm":[x,frame_front,z],"axis_direction":[0.,1.,0.],"embedment_mm":embedment,"min_embedment_mm":cfg["anchor_embedment_mm"]})
    result=_finish(sources,plan,definitions,instances,solids,bodies,fastenings,contacts,anchors,oc)
    result["machining"]=machining
    result["assumptions"].extend(["Сплошная подоснова и каркас V-WT — самостоятельная проектная адаптация, не воспроизведение заводского V-WS/V-SH.",
        "V-L-start применён как проектное обрамление с пазом; его тождество артикулу V-Ang-Start не утверждается.",
        "Пазы и профильные посадки построены реальными телами. Проверка объёмов не подтверждает свободный монтажный ход, съёмность или несущую способность панели."])
    return result
