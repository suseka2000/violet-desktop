"""Exact, auditable machining at room junctions.

Wall frames and complete fastening cells are replanned upstream.  This module
only machines the explicitly named architectural/profile parts.  It never
clips a screw, moves a head off its seat, or declares an overlap harmless just
because it happens near a corner.
"""
from __future__ import annotations

import math

from . import assembly_geometry as ag, step_geometry as step
from .room_assembly import IDENTITY
from .native_definition_cache import definition_is_valid,definition_volume

MACHINABLE_ROLES = frozenset({"panel", "adhesive", "backing", "frame", "profile"})
HARDWARE_ROLES = frozenset({"fastener", "anchor_bolt", "anchor_sleeve", "spacer"})
MAX_TOOLS = 256
VOLUME_TOLERANCE = .001


def _error(message, details=None):
    raise step.StepGeometryError(message, code="ROOM_JOINT_INVALID", details=details)


def _native_call(context, operation, *args, **kwargs):
    """Preserve strict kernel failures with bounded, actionable identity."""
    try:
        return operation(*args, **kwargs)
    except step.StepGeometryError as error:
        previous=error.details if isinstance(error.details,dict) else {}
        raise step.StepGeometryError(str(error),code=error.code,
            details={**context,**previous}) from error


def _shift(shape, xyz):
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    transform = gp_Trsf(); transform.SetTranslation(gp_Vec(*xyz))
    return BRepBuilderAPI_Transform(shape, transform, False).Shape()


def _overlap(a, b, epsilon=1e-6):
    return all(min(a["max"][k], b["max"][k]) - max(a["min"][k], b["min"][k]) > epsilon for k in range(3))


class _BoundsIndex:
    """Per-build broad phase; queries preserve the original Boolean order."""
    def __init__(self,items):
        self.items=items
        def tree(indices):
            bounds={side:[operation(items[i]['bounds_mm'][side][k] for i in indices) for k in range(3)]
                    for side,operation in (('min',min),('max',max))}
            if len(indices)<=8:return bounds,indices,None
            axis=max(range(3),key=lambda k:bounds['max'][k]-bounds['min'][k])
            indices.sort(key=lambda i:items[i]['bounds_mm']['min'][axis]+items[i]['bounds_mm']['max'][axis])
            middle=len(indices)//2
            return bounds,None,(tree(indices[:middle]),tree(indices[middle:]))
        self.root=tree(list(range(len(items)))) if items else None

    def query(self,*bounds,epsilon):
        pending=[self.root] if self.root else [];indices=[]
        while pending:
            node_bounds,leaf,children=pending.pop()
            if not any(_overlap(box,node_bounds,epsilon) for box in bounds):continue
            if children is not None:
                pending.extend(children);continue
            indices.extend(i for i in leaf if any(_overlap(box,self.items[i]['bounds_mm'],epsilon) for box in bounds))
        return [self.items[i] for i in sorted(indices)]


def _body_groups(bodies):
    by_id={};by_prefix={};panels_by_wall={}
    for body in bodies:
        identifier=body['id'];by_id[identifier]=body
        parts=identifier.split(':',2)
        prefix=(':'.join(parts[:2]) if parts[0]=='corner' and len(parts)>2 else parts[0])+':'
        by_prefix.setdefault(prefix,[]).append(body)
        if body['role']=='panel':panels_by_wall.setdefault(parts[0]+':',[]).append(body)
    return by_id,by_prefix,panels_by_wall


def _localized_boolean(operation, a, b, bounds_a, bounds_b):
    """Keep source/world coordinates, but condition native booleans locally."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Common
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID
    center = [(max(bounds_a["min"][k], bounds_b["min"][k]) + min(bounds_a["max"][k], bounds_b["max"][k])) / 2 for k in range(3)]
    local_a,local_b=_shift(a,[-v for v in center]),_shift(b,[-v for v in center])
    result = ag._boolean_shape(operation, local_a, local_b)
    if operation is BRepAlgoAPI_Cut and not BRepCheck_Analyzer(result).IsValid():
        # At a stepped end the cutter may only touch the already open notch's
        # face. OCCT can return an invalid zero-thickness remnant from Cut in
        # this case. Do not heal/drop solids: prove exact volumetric disjoint-
        # ness independently and retain the original, fully validated body.
        common=ag._boolean_shape(BRepAlgoAPI_Common,local_a,local_b)
        if BRepCheck_Analyzer(common).IsValid() and not TopExp_Explorer(common,TopAbs_SOLID).More():
            return a
    return _shift(result, center)


def _valid_single(shape, oc, identifier, context=None):
    from OCP.TopLoc import TopLoc_Location
    solids = step._shapes(shape, oc["TopAbs_SOLID"], oc)
    if len(solids) != 1 or not definition_is_valid(solids[0].Located(TopLoc_Location()),oc):
        _error("Обработка угла разделила или повредила деталь", {"instance_id": identifier, "solid_count": len(solids), **(context or {})})
    return solids[0]


def _box(bounds, margin=0.):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    return BRepPrimAPI_MakeBox(gp_Pnt(*[v-margin for v in bounds["min"]]),
                              *[max(.001, b-a+2*margin) for a,b in zip(bounds["min"],bounds["max"])]).Shape()


def apply_joint_tools(bodies, tools, oc, *, _context=None):
    """Apply explicit cutout/common tools and return (new bodies, audit).

    Native tool contract: {id,joint_id,shape,kind:'cutout'|'halfspace',
    wall_index:0-based,roles:[...],instance_ids?:[room-prefixed IDs]}.
    A halfspace's shape is the *retained* region.  Tools are supplied by the
    trusted corner builder, not by the browser.  All coordinates are global.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
    if not isinstance(tools,list) or len(tools)>MAX_TOOLS:
        _error("Превышен лимит обработок углов")
    if not _context:
        result = [dict(body) for body in bodies]
        by_wall={}
        for body in result:by_wall.setdefault(body["id"].split(":",1)[0]+":",[]).append(body)
        hardware = [body for body in result if body["role"] in HARDWARE_ROLES]
        # Balanced 3D AABB broad phase, as used by cross-wall collision checks.
        # Hardware is never machined, so this index remains valid for all tools
        # and all batches. Query every wall/floor, not just the target wall.
        def tree(indices):
            bounds={"min":[min(hardware[i]["bounds_mm"]["min"][k] for i in indices) for k in range(3)],
                    "max":[max(hardware[i]["bounds_mm"]["max"][k] for i in indices) for k in range(3)]}
            if len(indices)<=8:return bounds,indices,None
            axis=max(range(3),key=lambda k:bounds["max"][k]-bounds["min"][k])
            indices.sort(key=lambda i:sum(hardware[i]["bounds_mm"][side][axis] for side in ("min","max")))
            middle=len(indices)//2
            return bounds,None,(tree(indices[:middle]),tree(indices[middle:]))
        hardware_tree=tree(list(range(len(hardware)))) if hardware else None
        if _context is not None:
            _context.update(bodies=result,by_wall=by_wall,hardware=hardware,hardware_tree=hardware_tree)
    else:
        # Private state belongs to one synchronous building operation. Reject
        # accidentally reusing it for a different body collection.
        if _context["bodies"] is not bodies:_error("Обработки углов относятся к другому составу здания")
        result=bodies;by_wall=_context["by_wall"];hardware=_context["hardware"];hardware_tree=_context["hardware_tree"]
    audits = []
    def nearby_hardware(bounds):
        pending=[hardware_tree] if hardware_tree else [];indices=[]
        while pending:
            node_bounds,leaf,children=pending.pop()
            # The same expanded support envelope as the original exhaustive
            # check, including near-touching hardware within 0.02 mm.
            if not _overlap(bounds,node_bounds,epsilon=-.02):continue
            if children is not None:pending.extend(children);continue
            indices.extend(index for index in leaf if _overlap(bounds,hardware[index]["bounds_mm"],epsilon=-.02))
        # Retain deterministic source order (and the same first error).
        return (hardware[index] for index in sorted(indices))
    for tool in tools:
        if not isinstance(tool,dict) or tool.get("kind") not in {"cutout","halfspace"}:
            _error("Неизвестная обработка угла")
        roles = set(tool.get("roles",["panel"]))
        if not roles or not roles <= MACHINABLE_ROLES:
            _error("Обрезка крепежа и анкеров запрещена")
        if type(tool.get("wall_index")) is not int or tool["wall_index"]<0:
            _error("Не задана сторона стены для обработки")
        if not isinstance(tool.get("id"),str) or not tool["id"]:
            _error("Не задан идентификатор обработки")
        if "shape" not in tool:
            _error("Отсутствует точное тело обработки")
        target_prefix = f"w{tool['wall_index']+1}:"
        allowed_ids = tool.get("instance_ids")
        if allowed_ids is not None and (not isinstance(allowed_ids,list) or any(not isinstance(i,str) or not i.startswith(target_prefix) for i in allowed_ids)):
            _error("Обработка указывает на другую стену")
        tool_bounds = step._bbox(tool["shape"],oc)
        for body in by_wall.get(target_prefix,[]):
            if body["role"] not in roles or (allowed_ids is not None and body["id"] not in allowed_ids):
                continue
            if tool["kind"]=="cutout" and not _overlap(body["bounds_mm"],tool_bounds):
                continue
            plane=tool.get("retained_plane")
            if plane is not None:
                minimum=sum(plane["normal"][k]*(body["bounds_mm"]["min" if plane["normal"][k]>=0 else "max"][k]-plane["point_mm"][k]) for k in range(3))
                if minimum>=plane["offset_mm"]-1e-7:continue
            original = body["shape"]
            operation = BRepAlgoAPI_Cut if tool["kind"]=="cutout" else BRepAlgoAPI_Common
            context={'phase':'joint_machining','instance_id':body['id'],'tool_id':tool['id'],
                     'joint_id':tool.get('joint_id'),'wall_index':tool['wall_index']}
            changed = _native_call({**context,'operation':'machining_boolean'},_localized_boolean,
                operation,original,tool["shape"],body["bounds_mm"],tool_bounds)
            volume = _native_call({**context,'operation':'machining_volume'},ag._mass,changed,oc)
            removed = body["volume_mm3"]-volume
            if removed < -VOLUME_TOLERANCE:
                _error("Обработка угла неожиданно добавила материал",{"instance_id":body["id"]})
            if removed <= VOLUME_TOLERANCE:
                continue
            if volume <= VOLUME_TOLERANCE:
                _error("Обработка угла удаляет целую деталь; требуется пересчёт её крепления",{"instance_id":body["id"],"tool_id":tool["id"]})
            changed = _valid_single(changed,oc,body["id"],{"tool_id":tool["id"],"joint_id":tool.get("joint_id")})
            removal = _native_call({**context,'operation':'removed_material'},_localized_boolean,
                BRepAlgoAPI_Cut,original,changed,body["bounds_mm"],step._bbox(changed,oc))
            removal_bounds = step._bbox(removal,oc)
            # Conservative finite-volume exclusion around each physical screw,
            # anchor and spacer.  This protects both the actual hardware and
            # the material immediately supporting it, not only its centreline.
            for fastener in nearby_hardware(removal_bounds):
                envelope = _box(fastener["bounds_mm"],.02)
                lost = _native_call({**context,'operation':'fastener_support','fastener_id':fastener['id']},
                    lambda:ag._mass(_localized_boolean(BRepAlgoAPI_Common,removal,envelope,removal_bounds,step._bbox(envelope,oc)),oc))
                if lost>VOLUME_TOLERANCE:
                    _error("Обработка угла затрагивает посадку крепежа; нужна новая крепёжная ячейка",
                           {"instance_id":body["id"],"fastener_id":fastener["id"],"tool_id":tool["id"],"removed_support_mm3":lost})
            audits.append({"kind":"native_machining","joint_id":tool.get("joint_id"),"tool_id":tool["id"],"instance_id":body["id"],
                           "role":body["role"],"before_volume_mm3":body["volume_mm3"],"after_volume_mm3":volume,
                           "removed_volume_mm3":removed,"hardware_clipped":False,
                           **{key:tool[key] for key in ("strategy","end_stop_local_x_mm","support_clearance_mm") if key in tool}})
            body.update(shape=changed,volume_mm3=volume,bounds_mm=step._bbox(changed,oc))
    return result,audits


def apply_building_joint_tools(bodies, tools, oc):
    """Trusted building batches retain every native per-tool safety check."""
    if not isinstance(tools, list):
        _error("Некорректные обработки углов здания")
    audit = [];context={}
    for offset in range(0, len(tools), MAX_TOOLS):
        bodies, batch = apply_joint_tools(bodies, tools[offset:offset+MAX_TOOLS], oc, _context=context)
        audit.extend(batch)
    return bodies, audit


def install_modified_definitions(definitions,instances,bodies,audits,oc):
    """A machined occurrence gets its own exact local definition, never peers."""
    changed={entry["instance_id"] for entry in audits}
    by_id={item["id"]:item for item in instances}
    for body in bodies:
        if body["id"] not in changed:continue
        center=[(a+b)/2 for a,b in zip(body["bounds_mm"]["min"],body["bounds_mm"]["max"])]
        key=body["id"]+":room-machined"
        local=_shift(body["shape"],[-v for v in center])
        _valid_single(local,oc,body["id"])
        definitions[key]={"shape":local,"label":body["label"],"role":body["role"],
                          "provenance":{"kind":"room_joint_machining","source_definition_id":body["definition_id"]}}
        for item in (body,by_id[body["id"]]):
            item.update(definition_id=key,rotation=IDENTITY,translation_mm=center)


def check_retained_contacts(bodies,contacts,changed_ids,oc):
    """Re-prove every source support contact touched by a machined body."""
    by_id={body["id"]:body for body in bodies};checks=[]
    for spec in contacts:
        if not changed_ids.intersection((spec["body_a"],spec["body_b"])):continue
        if any(key not in by_id for key in (spec["body_a"],spec["body_b"])):
            _error("Обработка потеряла опорную деталь",{"contact_id":spec["id"]})
        selected=[by_id[spec["body_a"]],by_id[spec["body_b"]]]
        center=[sum(body["bounds_mm"]["min"][k]+body["bounds_mm"]["max"][k] for body in selected)/4 for k in range(3)]
        shapes=[_shift(body["shape"],[-v for v in center]) for body in selected]
        labels=[{**body,"instance_id":body["id"],"id":i+1,"bounds_mm":step._bbox(shapes[i],oc)} for i,body in enumerate(selected)]
        from .wall_assembly_geometry import _wall_contacts
        check=_native_call({'phase':'retained_wall_contact','contact_id':spec['id'],
            'body_a':spec['body_a'],'body_b':spec['body_b']},_wall_contacts,shapes,labels,[spec],oc)[0]
        if check["status"]!="valid":
            _error("Обработка угла нарушила опорный контакт",check)
        checks.append(check)
    return checks


def _wall_body_bounds(bodies):
    """One linear pass, reused by every junction of this native build."""
    result={}
    for body in bodies:
        prefix=body['id'].split(':',1)[0]
        bounds=body['bounds_mm']
        if prefix not in result:
            result[prefix]={key:list(bounds[key]) for key in ('min','max')}
        else:
            for axis in range(3):
                result[prefix]['min'][axis]=min(result[prefix]['min'][axis],bounds['min'][axis])
                result[prefix]['max'][axis]=max(result[prefix]['max'][axis],bounds['max'][axis])
    return result


def external_partition_tools(joint,bodies,walls=None,*,wall_bounds=None):
    """Complementary structural wedges; panels keep their calibrated seats.

    The same bisector also mitres the projecting lips of horizontal ceiling
    profiles at an internal corner. Internal backing already lies wholly on
    its own side, so this does not turn its calibrated butt panels into mitres.
    """
    if joint.get("kind") not in {"external_corner","internal_corner"}:return []
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon,BRepBuilderAPI_MakeFace
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt,gp_Vec
    point=joint["front_intersection_mm"];a,b=joint["axes_a"],joint["axes_b"]
    normal=[a[k]-b[k] for k in range(3)];length=math.sqrt(sum(v*v for v in normal));normal=[v/length for v in normal]
    clearance=joint.get("support_clearance_mm",.2)
    if type(clearance) not in (int,float) or not math.isfinite(clearance) or not 0<=clearance<=2:
        _error("Недопустимый монтажный зазор между угловыми опорами")
    by_wall=_wall_body_bounds(bodies) if wall_bounds is None else wall_bounds
    adjacent=[by_wall[f'w{index+1}'] for index in joint['wall_indices'] if f'w{index+1}' in by_wall]
    if not adjacent:_error('У сопряжения отсутствуют детали обеих стен',{'joint_id':joint.get('id')})
    # A cutter only acts on these two wall prefixes. Remote floors/walls must
    # not inflate its polygon or cause six global scans for every junction.
    bounds={key:[(min if key=="min" else max)(item[key][k] for item in adjacent)+(-10 if key=="min" else 10)
                 for k in range(3)] for key in ("min","max")}
    xmin,ymin,zmin=bounds["min"];xmax,ymax,zmax=bounds["max"]
    bands=joint.get('segments_z_mm')
    if bands is not None:
        bands=[list(interval) for interval in bands]
        # Panel gaps at the physical floor/ceiling do not change the seat of
        # the perimeter profile. Extend only the outside limits, never an
        # internal height transition such as a doorway head at 2100 mm.
        selected=[walls[i] for i in joint['wall_indices']] if walls else []
        limits=[]
        for wall in selected:
            plan=wall.get('plan',{});panels=plan.get('panels',[])
            if not panels:continue
            base=wall['translation_mm'][2];height=plan['wall']['height_mm']
            cfg=plan.get('mount_configuration',{})
            floor_gap=cfg.get('floor_gap_mm',min(p['y_mm'] for p in panels))
            ceiling_gap=cfg.get('ceiling_gap_mm',height-max(p['y_mm']+p['height_mm'] for p in panels))
            limits.append((base,base+height,base+floor_gap,base+height-ceiling_gap))
        if len(limits)==2:
            bottom=max(row[0] for row in limits);top=min(row[1] for row in limits)
            lowest=max(row[2] for row in limits);highest=min(row[3] for row in limits)
            bands=[[bottom if bottom-1e-5<=low<=lowest+1e-5 else low,
                    top if highest-1e-5<=high<=top+1e-5 else high] for low,high in bands]
    result=[]
    for index,sign in zip(joint["wall_indices"],(1.,-1.)):
        direction=[sign*v for v in normal];offset=clearance/2
        polygon=[[xmin,ymin],[xmax,ymin],[xmax,ymax],[xmin,ymax]]
        def distance(p):return sum(direction[k]*(p[k]-point[k]) for k in range(2))-offset
        clipped=[]
        for current,following in zip(polygon,polygon[1:]+polygon[:1]):
            d0,d1=distance(current),distance(following)
            if (d0>=0) if bands is None else (d0<=0):clipped.append(current)
            if (d0>=0)!=(d1>=0):
                fraction=d0/(d0-d1)
                clipped.append([current[k]+fraction*(following[k]-current[k]) for k in range(2)])
        if len(clipped)<3:_error("Угловое деление оставило пустую область стены")
        for band,(low,high) in enumerate(bands if bands is not None else [(zmin,zmax)]):
            if high-low<=1e-6:continue
            wire=BRepBuilderAPI_MakePolygon()
            for x,y in clipped:wire.Add(gp_Pnt(x,y,low))
            wire.Close();face=BRepBuilderAPI_MakeFace(wire.Wire()).Face()
            shape=BRepPrimAPI_MakePrism(face,gp_Vec(0,0,high-low)).Shape()
            result.append({"id":f"corner:{joint['id']}:structural-bisector-w{index+1}-z{band}","joint_id":joint["id"],
                           "kind":"cutout" if bands is not None else "halfspace",
                           "wall_index":index,"roles":["backing","adhesive","frame","profile"],"shape":shape,
                           "retained_plane":{"normal":direction,"point_mm":point,"offset_mm":offset},"support_clearance_mm":clearance,
                           "strategy":"complementary_structural_bisector"})
    return result


def perimeter_bond_end_stops(joints,walls,bodies,tools,oc):
    """Plan flat ends for project glue, not intricate disconnected fragments.

    A metal corner has several hooks/ribs. Imprinting all those hooks in the
    end of a project adhesive film can leave unsupported little islands. The
    glue is applied only up to one deliberate transverse stop, at the farthest
    *actual intersecting* corner part along the wall. Profiles keep their exact
    native copes. No material is dropped after a Boolean and no source metal
    is replaced by its bounding box. Existing hardware and bond contacts are
    still mandatory when these explicit finite cutters are applied.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from .room_assembly_worker import _move
    result=[]
    for joint in joints:
        for side in joint["sides"]:
            index=side["wall_index"];wall=walls[index]
            runs=[run for run in wall.get("plan",{}).get("profile_runs",[])
                  if run.get("family") in {"sky","start"} and run.get("axis")=="horizontal"]
            prefixes=tuple(f"w{index+1}:bond-{run['id']}-" for run in runs)
            if not prefixes:continue
            candidates=[tool for tool in tools if tool.get("joint_id")==joint["id"] and tool["wall_index"]==index
                        and tool["kind"]=="cutout" and "adhesive" in tool.get("roles",[])]
            if not candidates:continue
            rotation=wall["rotation"];translation=wall["translation_mm"]
            inverse=[list(column) for column in zip(*rotation)]
            origin=[-sum(inverse[i][j]*translation[j] for j in range(3)) for i in range(3)]
            def local_bounds(shape):return step._bbox(_move(shape,inverse,origin,copy=True),oc)
            tool_shapes=[(tool,step._bbox(tool["shape"],oc),local_bounds(tool["shape"])) for tool in candidates]
            for body in bodies:
                if body["role"]!="adhesive" or not body["id"].startswith(prefixes):continue
                selected=[]
                for tool,bounds,local in tool_shapes:
                    if not _overlap(body["bounds_mm"],bounds):continue
                    overlap=_localized_boolean(BRepAlgoAPI_Common,body["shape"],tool["shape"],body["bounds_mm"],bounds)
                    if _native_call({'phase':'perimeter_bond_stop','joint_id':joint['id'],
                        'instance_id':body['id'],'tool_id':tool['id']},ag._mass,overlap,oc)>VOLUME_TOLERANCE:selected.append(local)
                if not selected:continue
                bounds=local_bounds(body["shape"])
                lo,hi=list(bounds["min"]),list(bounds["max"])
                if side["end"]=="start":
                    stop=max(value["max"][0] for value in selected);hi[0]=stop
                elif side["end"]=="end":
                    stop=min(value["min"][0] for value in selected);lo[0]=stop
                else:_error("Не задан торец клеевого шва")
                # The intended film stop is a construction choice, recorded
                # below. A completely consumed film instead requires replanning.
                if hi[0]<=lo[0]+1e-6:continue
                if side["end"]=="start":lo[0]-=.1
                else:hi[0]+=.1
                for k in (1,2):lo[k]-=.1;hi[k]+=.1
                cutter=_move(_box({"min":lo,"max":hi}),rotation,translation,copy=True)
                result.append({"id":f"corner:{joint['id']}:bond-end-stop:{body['id']}","joint_id":joint["id"],
                               "kind":"cutout","wall_index":index,"roles":["adhesive"],"instance_ids":[body["id"]],
                               "shape":cutter,"strategy":"planar_perimeter_bond_end_stop","end_stop_local_x_mm":stop})
    return result


def original_substrates(walls,oc):
    """Recreate only the original parametric masonry, never extended finish.

    Original wall assemblies explicitly model substrate as a box minus the
    opening rectangles.  Recreating that source primitive avoids transmitting
    every old threaded screw a second time.  No finish-span extension is
    allowed to enlarge the physical support.  Drilling is handled separately;
    sleeve containment is proved against the material before drilling.
    """
    from .room_assembly_worker import _move
    from .wall_assembly_geometry import _box as box, _cut
    from .wall_opening_bounds import opening_cut_bounds
    result=[];seen=set()
    for wall in walls:
        source=wall.get("original_base",wall.get("original_substrate"))
        if source is None:continue
        if not isinstance(source,dict) or not isinstance(source.get("plan"),dict):
            _error("Не задано исходное основание комнаты")
        plan=source["plan"];rotation=source.get("rotation");translation=source.get("translation_mm")
        if (not isinstance(rotation,list) or len(rotation)!=3 or any(not isinstance(row,list) or len(row)!=3 for row in rotation)
                or not isinstance(translation,list) or len(translation)!=3):
            _error("Повреждено положение исходного основания")
        fields=[*sum(rotation,[]),*translation]
        values=[plan["wall"]["length_mm"],plan["wall"]["height_mm"],plan["layers"]["substrate_front_mm"],
                plan["configuration"]["substrate_thickness_mm"],source["wall_thickness_mm"]]
        if any(type(v) not in (float,int) or not math.isfinite(v) or abs(v)>1e7 for v in fields+values) or min(values)<=0:
            _error("Некорректные размеры исходного основания")
        length,height,front,depth,physical_depth=values;depth=min(depth,physical_depth)
        if any(abs(sum(rotation[i][k]*rotation[j][k] for k in range(3))-(i==j))>1e-8 for i in range(3) for j in range(3)):
            _error("Масштабирование исходного основания запрещено")
        shape=box([length,depth,height],[0.,front,0.])
        for opening in plan.get("openings",[]):
            x,z,w,h=(opening[k] for k in ("x_mm","y_mm","width_mm","height_mm"))
            if any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>1e7 for v in (x,z,w,h)) or min(w,h)<=0:
                _error("Повреждён проём исходного основания")
            x,z,right,top=opening_cut_bounds(opening,length,height,outside=.01)
            shape=_cut(shape,box([right-x,depth+.02,top-z],[x,front-.01,z]))
        shape=_move(shape,rotation,translation,copy=True)
        bounds=step._bbox(shape,oc)
        signature=(wall["wall_id"],tuple(round(v,6) for key in ("min","max") for v in bounds[key]))
        if signature in seen:continue
        seen.add(signature)
        result.append({"id":f"physical-substrate-{len(result)+1}","wall_id":wall["wall_id"],"shape":shape,
                       "bounds_mm":bounds,"role":"substrate","label":"Исходное физическое основание"})
    return result


def check_anchor_embedment(bodies,substrates,oc,*,_substrate_index=None):
    """Every actual sleeve must be wholly inside original, finite material."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    checks=[]
    index=_BoundsIndex(substrates) if _substrate_index is None else _substrate_index
    if index.items is not substrates:_error('Индекс оснований относится к другой сборке')
    for body in bodies:
        if body["role"]!="anchor_sleeve":continue
        candidates=index.query(body['bounds_mm'],epsilon=-1e-6)
        remainder=body["shape"]
        # Sequential subtraction is subtraction of a union even where two
        # structural walls meet.  No overlapping-base volumes are double
        # counted and no decorative backing can masquerade as masonry.
        for base in candidates:
            remainder=_localized_boolean(BRepAlgoAPI_Cut,remainder,base["shape"],body["bounds_mm"],base["bounds_mm"])
            if ag._mass(remainder,oc)<=VOLUME_TOLERANCE:break
        outside=ag._mass(remainder,oc)
        check={"instance_id":body["id"],"status":"valid" if outside<=VOLUME_TOLERANCE else "invalid",
               "sleeve_volume_mm3":body["volume_mm3"],"outside_original_substrate_mm3":outside,
               "substrate_ids":[base["id"] for base in candidates],"scope":"complete_real_sleeve_inside_original_wall_material"}
        if check["status"]!="valid":
            _error("Анкер вышел за физическое основание: требуется перенос всей крепёжной ячейки",check)
        checks.append(check)
    return checks


def replace_boundary_cells(walls,joints,bodies,instances):
    """Replace a whole declared L-start + its screws, never sever hardware."""
    by_id={body["id"]:body for body in bodies};removed={};audit=[]
    for joint in joints:
        for side in joint.get("sides",[]):
            index=side["wall_index"];wall=walls[index];plan=wall.get("plan",{})
            if not plan.get("system") or side.get("replace_boundary_profile") is False:continue
            boundary=0. if side["end"]=="start" else plan["wall"]["length_mm"]
            if wall.get('room_span',{}).get('sections'):
                boundary=side['panel_end_local_x_mm']-wall['room_span']['start_mm']
            runs=[run for run in plan.get("profile_runs",[]) if run.get("family")=="lstart"
                  and run.get("axis")=="vertical" and abs(run.get("edge_mm",1e9)-boundary)<1e-5]
            prefix=f"w{index+1}:"
            for run in runs:
                low,high=run["start_mm"]+wall["translation_mm"][2],run["end_mm"]+wall["translation_mm"][2]
                # The fitted planner splits complete profile/screw cells at
                # an opening head. Below it there is no corner kit: retain
                # that independent end cell, including all of its hardware.
                if not any(min(high,b)-max(low,a)>1e-5 for a,b in joint["segments_z_mm"]):
                    continue
                if not any(a-1e-5<=low and high<=b+1e-5 for a,b in joint["segments_z_mm"]):
                    _error("Угловая замена не покрывает всю высоту исходного крепёжного прогона",{"joint_id":joint["id"],"run_id":run["id"]})
                screws=[item for item in plan.get("profile_screws",[]) if item["profile_id"]==run["id"]]
                expected={prefix+"profile-"+run["id"]:"profile",**{prefix+"screw-"+item["id"]:"fastener" for item in screws}}
                if not screws or any(key not in by_id or by_id[key]["role"]!=role for key,role in expected.items()):
                    _error("Не удалось выделить полный исходный крепёжный комплект торца",{"joint_id":joint["id"],"run_id":run["id"]})
                for identifier in expected:
                    if identifier in removed:
                        _error("Два угла пытаются заменить один исходный крепёжный комплект",
                               {"instance_id":identifier,"joint_id":joint["id"],"previous_joint_id":removed[identifier],
                                "wall_index":index,"wall_id":wall.get("wall_id"),"face_id":wall.get("face_id"),"run_id":run["id"]})
                    removed[identifier]=joint["id"]
                    body=by_id[identifier]
                    audit.append({"kind":"complete_boundary_cell_replaced","joint_id":joint["id"],"run_id":run["id"],
                                  "instance_id":identifier,"role":body["role"],"removed_volume_mm3":body["volume_mm3"],
                                  "replacement":"source_bound_corner_kit","hardware_clipped":False})
    return [body for body in bodies if body["id"] not in removed],[item for item in instances if item["id"] not in removed],audit


def prepare_native_joints(request,definitions,instances,bodies,oc):
    """Install calibrated kits and explicit cuts; return native proof duties."""
    from OCP.TopLoc import TopLoc_Location
    from .room_corner_parts import build_corner_parts
    joints=request.get("joints",[])
    if not joints:return bodies,instances,None
    walls=request["walls"];kits=[];tools=[];corner_sources={}
    for joint in joints:
        if (not isinstance(joint.get("id"),str) or not joint["id"] or len(joint["id"])>128
                or any(type(i) is not int or not 0<=i<len(walls) for i in joint.get("wall_indices",[]))):
            _error("Повреждён идентификатор или стороны сопряжения")
        kit=_native_call({'phase':'corner_kit_build','joint_id':joint['id'],'wall_indices':joint['wall_indices']},
            build_corner_parts,joint,request.get("source_content",{}),oc,source_cache=corner_sources)
        prefix=f"corner:{joint['id']}:"
        for key in ("panel_tools","clearance_tools"):
            for item in kit.get(key,[]):
                tools.append({**item,"id":prefix+item["id"],"joint_id":joint["id"],"kind":"cutout"})
        # Full-height corner parts occupy the floor/ceiling profile ends too.
        # Machine only the actual intersecting material of those explicit
        # perimeter profiles, not their entire bounding rectangles or screws.
        for index in joint["wall_indices"]:
            runs=[run for run in walls[index].get("plan",{}).get("profile_runs",[])
                  if run.get("family") in {"sky","start","lstart"} and run.get("axis")=="horizontal"]
            ids=[f"w{index+1}:profile-{run['id']}" for run in runs]
            if not ids:continue
            for part in kit["parts"]:
                if part["role"] not in {"profile","frame","backing","adhesive"}:continue
                tools.append({"id":prefix+f"cope-w{index+1}-"+part["id"],"joint_id":joint["id"],"kind":"cutout",
                              "wall_index":index,"roles":["profile"],"instance_ids":ids,"shape":part["shape"]})
        # A V0 wall has a real 3-mm glue bed: the corner angle occupies one
        # millimetre of that bed in its lap. Leave the angle as metal, not as
        # overlapping glue; likewise fit any physical backing at that lap.
        for index in joint["wall_indices"]:
            for part in kit["parts"]:
                if part["role"] not in {"profile","frame","backing","adhesive"}:continue
                tools.append({"id":prefix+f"lap-seat-w{index+1}-"+part["id"],"joint_id":joint["id"],"kind":"cutout",
                              "wall_index":index,"roles":["adhesive","backing"],"shape":part["shape"]})
        kits.append({"joint":joint,"kit":kit,"prefix":prefix})
    source_count=len(bodies)
    active_joints=[entry["joint"] for entry in kits if entry["kit"].get("parts")]
    bodies,instances,replaced=replace_boundary_cells(walls,active_joints,bodies,instances)
    # Partition the actual wall layers *before* machining profile pockets.
    # This prevents corner coping from leaving overlapping glue fragments and
    # gives both projecting Sky lips one common physical mitre boundary.
    wall_bounds=_wall_body_bounds(bodies)
    tools=([tool for joint in active_joints for tool in external_partition_tools(joint,bodies,walls,wall_bounds=wall_bounds)]
           +perimeter_bond_end_stops(active_joints,walls,bodies,tools,oc)+tools)
    if request.get("scope_kind")=="building":
        # Keep the per-operation batch guard, not a fixed building-wide corner
        # count. Every batch sees all current bodies/hardware and keeps proofs.
        bodies,machining=apply_building_joint_tools(bodies,tools,oc)
    else:bodies,machining=apply_joint_tools(bodies,tools,oc)
    contacts=[]
    active_ids={body["id"] for body in bodies}
    for index,wall in enumerate(walls,1):
        prefix=f"w{index}:"
        for spec in wall.get("contact_specs",[]):
            a,b=prefix+spec["body_a"],prefix+spec["body_b"]
            if a in active_ids and b in active_ids:
                contacts.append({**spec,"id":prefix+spec["id"],"body_a":a,"body_b":b})
    retained=check_retained_contacts(bodies,contacts,{a["instance_id"] for a in machining},oc)
    install_modified_definitions(definitions,instances,bodies,machining,oc)
    added=[];prototypes=[]
    for entry in kits:
        prefix,kit,joint=entry["prefix"],entry["kit"],entry["joint"]
        for part in kit["parts"]:
            identity=prefix+part["id"];shape=part["shape"]
            if identity in active_ids:_error("Повторяющаяся деталь угла")
            active_ids.add(identity)
            transform=shape.Location().Transformation()
            if abs(transform.ScaleFactor()-1)>1e-12 or transform.IsNegative():
                _error("Угловая деталь отражена или масштабирована",{"instance_id":identity})
            local=_valid_single(shape.Located(TopLoc_Location()),oc,identity)
            key=next((name for proto,name,role in prototypes if role==part["role"] and proto.IsPartner(local) and proto.Orientation()==local.Orientation()),None)
            if key is None:
                key=identity+":definition";prototypes.append((local,key,part["role"]))
                definitions[key]={"shape":local,"role":part["role"],"label":part["label"],"provenance":part.get("provenance",{})}
            rotation=[[transform.Value(i+1,j+1) for j in range(3)] for i in range(3)]
            translation=[transform.Value(i+1,4) for i in range(3)]
            item={"id":identity,"definition_id":key,"rotation":rotation,"translation_mm":translation,"role":part["role"],
                  "wall_id":walls[joint["wall_indices"][0]]["wall_id"],"face_id":prefix,"joint_id":joint["id"]}
            if request.get("scope_kind")=="building":item["floor_id"]=walls[joint["wall_indices"][0]]["floor_id"]
            volume=_native_call({'phase':'corner_part_volume','joint_id':joint['id'],'instance_id':identity},definition_volume,local,oc)
            instances.append(item)
            bodies.append({**item,"shape":shape,"label":part["label"],"volume_mm3":volume,"bounds_mm":step._bbox(shape,oc)})
            added.append({"kind":"corner_part_added","joint_id":joint["id"],"instance_id":identity,"role":part["role"],
                          "volume_mm3":volume,"source_id":part.get("source_id"),"provenance":part.get("provenance",{})})
    if len(bodies)!=source_count-len(replaced)+len(added):_error("Баланс состава комнаты нарушен")
    bases=original_substrates(walls,oc)
    if not bases:_error("Для угловой сборки не переданы исходные физические основания")
    return bodies,instances,{"kits":kits,"substrates":bases,"retained_contacts":retained,"contacts":contacts,
                            "source_count":source_count,"replaced":replaced,"machining":machining,"added":added,
                            "changed_ids":{entry["instance_id"] for entry in machining}}


def verify_native_joints(bodies,state,oc):
    """Prove corner hardware, panel seats and foundation against final STEP."""
    if state is None:return None
    from .wall_assembly_geometry import _wall_collisions, _wall_contacts
    by_id,by_prefix,panels_by_wall=_body_groups(bodies)
    substrate_index=_BoundsIndex(state['substrates'])
    checks=[];all_engagements=[];panel_checks=[];anchor_checks=[]
    for entry in state["kits"]:
        joint,kit,prefix=entry["joint"],entry["kit"],entry["prefix"]
        if not kit.get("parts"):
            checks.append({"joint_id":joint["id"],"status":"not_applicable","part_count":0,
                           "reason":"Нет общего участка панелей по высоте; существующие торцевые крепления сохранены."})
            continue
        selected=by_prefix.get(prefix,[])
        origin=joint["front_intersection_mm"]
        def localized(body):return _shift(body["shape"],[-v for v in origin])
        shapes=[localized(body) for body in selected]
        labels=[{**body,"id":i+1,"instance_id":body["id"],"bounds_mm":step._bbox(shapes[i],oc)} for i,body in enumerate(selected)]
        fastenings=[]
        for item in kit.get("fastenings",[]):
            fastenings.append({**item,"id":prefix+item["id"],"fastener_id":prefix+item["fastener_id"],"seat_id":prefix+item["seat_id"],
                               "axis_origin_mm":[item["axis_origin_mm"][k]-origin[k] for k in range(3)],
                               "engagement_targets":[prefix+name for name in item["engagement_targets"]]})
        collisions,fastener_checks,engagements=_native_call({'phase':'corner_fastening_check','joint_id':joint['id']},
            _wall_collisions,shapes,labels,fastenings,oc,localize_fasteners=True)
        if collisions or any(check["status"]!="valid" for check in fastener_checks):
            _error("Угловой комплект не прошёл проверку крепежа и пересечений",{"joint_id":joint["id"],"collisions":collisions,
                   "invalid_fastenings":[check for check in fastener_checks if check["status"]!="valid"]})
        contacts=[{**item,"id":prefix+item["id"],"body_a":prefix+item["body_a"],"body_b":prefix+item["body_b"]} for item in kit.get("contacts",[])]
        contact_checks=_native_call({'phase':'corner_internal_contacts','joint_id':joint['id']},_wall_contacts,shapes,labels,contacts,oc)
        if any(check["status"]!="valid" for check in contact_checks):
            _error("Угловой комплект потерял опорный контакт",{"joint_id":joint["id"],"contacts":[check for check in contact_checks if check["status"]!="valid"]})
        for spec in kit.get("panel_contacts",[]):
            part=by_id[prefix+spec["body_id"]];candidates=[];total=0.
            for body in panels_by_wall.get(f"w{spec['wall_index']+1}:",[]):
                if not _overlap(body["bounds_mm"],part["bounds_mm"],epsilon=-1e-5):continue
                pair=[part,body];solids=[localized(item) for item in pair]
                metadata=[{**item,"id":i+1,"instance_id":item["id"],"bounds_mm":step._bbox(solids[i],oc)} for i,item in enumerate(pair)]
                duty={"id":prefix+"panel-seat:"+body["id"],"body_a":part["id"],"body_b":body["id"],"min_area_mm2":.1}
                check=_native_call({'phase':'corner_panel_contact','joint_id':joint['id'],
                    'body_a':part['id'],'body_b':body['id']},_wall_contacts,solids,metadata,[duty],oc)[0]
                if check["overlap_volume_mm3"]>VOLUME_TOLERANCE:_error("Угловой профиль пересекает панель вместо посадки",check)
                if check["contact_area_mm2"]>.001:
                    candidates.append(check);total+=check["contact_area_mm2"]
            if total+1e-5<spec["min_area_mm2"]:
                _error("Угловой профиль не касается настоящей панели",{"joint_id":joint["id"],"part_id":part["id"],"wall_index":spec["wall_index"],"contact_area_mm2":total})
            panel_checks.append({"joint_id":joint["id"],"profile_id":part["id"],"wall_index":spec["wall_index"],
                                 "status":"valid","contact_area_mm2":total,"contacts":candidates})
        # The calibrated bolt's own frame bearing is checked in addition to
        # the positive-area contact obligations: a head edge is insufficient.
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Common,BRepAlgoAPI_Cut
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
        from OCP.gp import gp_Pln,gp_Pnt,gp_Dir
        foundations={}
        for spec in kit.get("anchors",[]):
            bolt,frame=(by_id[prefix+spec[key]] for key in ("bolt_id","frame_id"))
            axis=[spec["axis_origin_mm"][k]-origin[k] for k in range(3)]
            plane=BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(*axis),gp_Dir(*spec["axis_direction"])),-6.,6.,-6.,6.).Face()
            head=ag._boolean_shape(BRepAlgoAPI_Common,localized(bolt),plane)
            cylinder=ag._cylinder({"axis_origin_mm":axis,"axis_direction":spec["axis_direction"]},3.05,-.01,.02)
            head=ag._boolean_shape(BRepAlgoAPI_Cut,head,cylinder)
            anchor_context={'phase':'corner_anchor_bearing','joint_id':joint['id'],'anchor_id':prefix+spec['id']}
            available=_native_call(anchor_context,ag._mass,head,oc,surface=True)
            contact=_native_call(anchor_context,lambda:ag._mass(ag._boolean_shape(BRepAlgoAPI_Common,head,localized(frame)),oc,surface=True))
            if available<1. or contact+1e-5<available*.9:
                _error("Головка углового анкера не опирается на каркас",{"anchor_id":prefix+spec["id"],"head_area_mm2":available,"contact_area_mm2":contact})
            sleeve,spacer=(by_id[prefix+spec[key]] for key in ("sleeve_id","spacer_id"))
            candidates=substrate_index.query(sleeve['bounds_mm'],spacer['bounds_mm'],epsilon=-1e-5)
            if not candidates:_error("Угловой анкер не достигает исходного основания",{"anchor_id":prefix+spec["id"]})
            from .wall_assembly_geometry import _fuse
            foundation_key=tuple(base["id"] for base in candidates)
            if foundation_key not in foundations:
                foundation=_fuse([localized(base) for base in candidates])
                # Every anchor in this same corner and same original-wall
                # union sees precisely the same drilled foundation. Build it
                # once, retaining separate contact proof for every sleeve.
                for tool in kit.get("substrate_tools",[]):
                    foundation=ag._boolean_shape(BRepAlgoAPI_Cut,foundation,_shift(tool["shape"],[-v for v in origin]))
                foundations[foundation_key]=foundation
            foundation=foundations[foundation_key]
            base_id=prefix+"foundation:"+spec["id"]
            proof_shapes=[localized(sleeve),localized(spacer),foundation]
            proof_labels=[{"id":1,"instance_id":sleeve["id"],"bounds_mm":step._bbox(proof_shapes[0],oc)},
                          {"id":2,"instance_id":spacer["id"],"bounds_mm":step._bbox(proof_shapes[1],oc)},
                          {"id":3,"instance_id":base_id,"bounds_mm":step._bbox(foundation,oc)}]
            required_cylinder_area=2*math.pi*4.*spec["min_embedment_mm"]
            duties=[{"id":prefix+spec["id"]+":sleeve-material","body_a":sleeve["id"],"body_b":base_id,"min_area_mm2":required_cylinder_area},
                    {"id":prefix+spec["id"]+":spacer-material","body_a":spacer["id"],"body_b":base_id,"min_area_mm2":.1}]
            support_checks=_native_call({'phase':'corner_anchor_foundation','joint_id':joint['id'],
                'anchor_id':prefix+spec['id']},_wall_contacts,proof_shapes,proof_labels,duties,oc)
            if any(item["status"]!="valid" for item in support_checks):
                _error("Угловая опора или втулка не соединена с исходным основанием",{"anchor_id":prefix+spec["id"],"contacts":support_checks})
            anchor_checks.append({"anchor_id":prefix+spec["id"],"status":"valid","head_bearing_area_mm2":available,
                                  "frame_contact_area_mm2":contact,"required_frame_contact_area_mm2":available*.9,"foundation_contacts":support_checks})
        checks.append({"joint_id":joint["id"],"status":"valid","part_count":len(selected),"fastening_checks":fastener_checks,
                       "contact_checks":contact_checks,"internal_collision_count":0,"assumptions":kit.get("assumptions",[])})
        all_engagements.extend(engagements)
    embedment=check_anchor_embedment(bodies,state["substrates"],oc,_substrate_index=substrate_index)
    retained=check_retained_contacts(bodies,state["contacts"],state["changed_ids"],oc)
    return {"status":"valid","validation_frame":"exported_step_roundtrip","joints":checks,"panel_seats":panel_checks,
            "corner_anchor_bearings":anchor_checks,"anchor_embedment":embedment,"intentional_engagements":all_engagements,
            "retained_support_contacts":retained,"source_body_count":state["source_count"],"result_body_count":len(bodies),
            "replaced_cell_parts":state["replaced"],"machining":state["machining"],"added_parts":state["added"],
            "hardware_clipped_count":0}
