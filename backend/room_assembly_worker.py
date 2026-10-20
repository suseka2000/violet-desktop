"""Exact native STEP composition with complete inventory and geometry checks.

HTTP rooms/buildings use the disk-stream transport in building_assembly_worker.
The legacy inline transport below remains for compatibility, without model
count quotas. Both routes retain cancellation and physical resource guards.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from .desktop_runtime import worker_command, worker_launch_options, track_worker
import tempfile
import time

from .assembly_worker import _json
from .step_geometry import StepGeometryError
from .step_worker import SLOTS, _resident_bytes
from .room_assembly import matmul, transform_point
MIRROR = [[1.,0.,0.],[0.,-1.,0.],[0.,0.,1.]]


def _error(message, code="ROOM_ASSEMBLY_INVALID", details=None):
    raise StepGeometryError(message, code=code, details=details)


def _check_instances(wall, *, max_bodies=None):
    bodies=wall.get("bodies");instances=wall.get("instances")
    if (not isinstance(bodies,list) or not isinstance(instances,list) or not bodies
            or len(bodies)!=len(instances) or (max_bodies is not None and len(bodies)>max_bodies)
            or any(not isinstance(item,dict) for item in [*bodies,*instances])):
        _error("STEP-реестр тел и экземпляров стены расходится")
    if (any(not isinstance(item.get("instance_id"),str) or not 1<=len(item["instance_id"])<=512 for item in bodies)
            or any(not isinstance(item.get("id"),str) or not 1<=len(item["id"])<=512 for item in instances)):
        _error("Некорректный идентификатор STEP-экземпляра")
    by_body={item.get("instance_id"):item for item in bodies}
    ids=[item.get("id") for item in instances]
    if len(by_body)!=len(bodies) or len(set(ids))!=len(ids) or set(ids)!=set(by_body):
        _error("Идентификаторы STEP-экземпляров повреждены")
    for instance in instances:
        rotation=instance.get("rotation");translation=instance.get("translation_mm")
        if (not isinstance(rotation,list) or len(rotation)!=3 or any(not isinstance(row,list) or len(row)!=3 for row in rotation)
                or not isinstance(translation,list) or len(translation)!=3):
            _error("Некорректная матрица STEP-экземпляра")
        if any(type(v) not in (float,int) or not math.isfinite(v) or abs(v)>1e10 for v in [*sum(rotation,[]),*translation]):
            _error("Некорректное число в STEP-экземпляре")
        if any(abs(sum(rotation[i][k]*rotation[j][k] for k in range(3))-(i==j))>1e-8 for i in range(3) for j in range(3)):
            _error("STEP-экземпляр масштабирован или скошен")
        determinant=(rotation[0][0]*(rotation[1][1]*rotation[2][2]-rotation[1][2]*rotation[2][1])
                     -rotation[0][1]*(rotation[1][0]*rotation[2][2]-rotation[1][2]*rotation[2][0])
                     +rotation[0][2]*(rotation[1][0]*rotation[2][1]-rotation[1][1]*rotation[2][0]))
        if abs(determinant-1)>1e-8:
            _error("Исходный STEP-экземпляр имеет отражённую систему координат")
        body=by_body[instance["id"]]
        if instance.get("definition_id")!=body.get("definition_id") or instance.get("role")!=body.get("role"):
            _error("STEP-экземпляр относится к другой детали")


def _read(stream):
    line = stream.readline()
    if not line.endswith(b"\n"):
        _error("Передача реестра помещения оборвана")
    request = _json(line)
    if not isinstance(request, dict) or set(request)-{"walls","joints","sources"} or not isinstance(request.get("walls"),list) or not request["walls"]:
        _error("Некорректный состав помещения")
    joints=request.get("joints",[])
    if not isinstance(joints,list) or any(not isinstance(joint,dict) for joint in joints):
        _error("Некорректный перечень сопряжений помещения")
    from .whole_wall_assemblies import _bounded_numbers
    _bounded_numbers(joints)
    for wall in request["walls"]:
        _check_instances(wall)
        size = wall.get("byte_length")
        if type(size) is not int or size<1:
            _error("Некорректный размер STEP стены")
        wall["content"] = stream.read(size)
        if len(wall["content"])!=size or hashlib.sha256(wall["content"]).hexdigest()!=wall.get("source_id"):
            _error("Изменились байты исходной сборки стены")
        for key in ("rotation", "translation_mm"):
            vector = wall.get(key)
            if key=="rotation" and (not isinstance(vector,list) or len(vector)!=3 or any(not isinstance(row,list) or len(row)!=3 for row in vector)):
                _error("Матрица стены должна иметь размер 3 × 3")
            values = sum(vector, []) if key=="rotation" and isinstance(vector,list) and all(isinstance(row,list) for row in vector) else vector
            if not isinstance(values,list) or len(values)!=(9 if key=="rotation" else 3) or any(type(v) not in (float,int) or not math.isfinite(v) or abs(v)>1e10 for v in values):
                _error("Некорректное положение стены")
        rotation=wall["rotation"]
        if any(abs(sum(rotation[i][k]*rotation[j][k] for k in range(3))-(i==j))>1e-8 for i in range(3) for j in range(3)):
            _error("Масштабирование или сдвиг формы деталей запрещены")
    sources=request.get("sources",[])
    if not isinstance(sources,list):
        _error("Недопустимый перечень исходников углов")
    source_content={}
    for source in sources:
        if not isinstance(source,dict) or set(source)!={"source_id","byte_length"}:
            _error("Некорректный исходник угла")
        identity,size=source["source_id"],source["byte_length"]
        if (not isinstance(identity,str) or len(identity)!=64 or any(c not in "0123456789abcdef" for c in identity)
                or identity in source_content or type(size) is not int or size<1):
            _error("Некорректный размер или SHA исходника угла")
        content=stream.read(size)
        if len(content)!=size or hashlib.sha256(content).hexdigest()!=identity:
            _error("Изменились байты исходника угла")
        source_content[identity]=content
    request["source_content"]=source_content
    if stream.read(1):
        _error("Лишние байты запроса комнаты")
    return request


def _read_solids(content, oc, *, max_bodies=None):
    reader=oc["STEPControl_Reader"]()
    status=reader.ReadFile(str(content)) if isinstance(content,Path) else reader.ReadStream("room-source.step", io.BytesIO(content))
    if status!=oc["IFSelect_RetDone"]:
        _error("Не удалось прочитать STEP стены")
    reader.SetSystemLengthUnit(1.)
    if reader.TransferRoots()<=0:
        _error("STEP стены не содержит тел")
    from . import step_geometry as step
    solids=step._shapes(reader.OneShape(),oc["TopAbs_SOLID"],oc)
    if not solids or (max_bodies is not None and len(solids)>max_bodies):
        _error("Недопустимое число тел STEP", "ROOM_ASSEMBLY_LIMIT")
    return solids


def _move(shape, rotation, translation, copy=False):
    from OCP.gp import gp_Trsf
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    transform=gp_Trsf()
    transform.SetValues(*[v for row,shift in zip(rotation,translation) for v in (*row,shift)])
    return BRepBuilderAPI_Transform(shape,transform,copy).Shape()


def _fastener_definition_transform(instance,axis):
    """Proper rotation instead of reflection: never manufacture left threads.

    The room-wall conversion is orientation reversing. Reflecting the *pose*
    around a plane containing the screw axis, followed by the wall conversion,
    yields a proper rigid rotation of the unchanged real screw. The axis and
    seat point stay exactly fixed. The thread is not healed, scaled or mirrored.
    """
    for key in ("axis_origin_mm","axis_direction"):
        vector=axis.get(key)
        if not isinstance(vector,list) or len(vector)!=3 or any(type(v) not in (float,int) or not math.isfinite(v) or abs(v)>1e10 for v in vector):
            _error("Повреждена сохранённая ось самореза")
    inverse=[list(row) for row in zip(*instance["rotation"])]
    origin=transform_point(inverse,[0.,0.,0.],[axis["axis_origin_mm"][k]-instance["translation_mm"][k] for k in range(3)])
    direction=transform_point(inverse,[0.,0.,0.],axis["axis_direction"])
    norm=math.sqrt(sum(v*v for v in direction))
    if abs(norm-1)>1e-8:_error("Ось самореза должна быть единичной")
    basis=[0.,0.,1.] if abs(direction[2])<.9 else [1.,0.,0.]
    dot=sum(a*b for a,b in zip(basis,direction))
    normal=[basis[k]-dot*direction[k] for k in range(3)]
    length=math.sqrt(sum(v*v for v in normal));normal=[v/length for v in normal]
    reflection=[[(1. if i==j else 0.)-2*normal[i]*normal[j] for j in range(3)] for i in range(3)]
    distance=sum(a*b for a,b in zip(normal,origin))
    shift=[2*distance*v for v in normal]
    return matmul(MIRROR,reflection),transform_point(MIRROR,[0.,0.,0.],shift),origin,direction


def _rigid_definition(shape,oc,identifier):
    from OCP.TopLoc import TopLoc_Location
    location=shape.Location().Transformation()
    if abs(location.ScaleFactor()-1)>1e-12 or location.IsNegative():
        _error("STEP изменил масштаб или ориентацию экземпляра",details={"instance_id":identifier})
    local=shape.Located(TopLoc_Location())
    # Validity is intrinsic to the BRep. OCCT's face classifier can report
    # UnorientableShape on the same valid screw translated 8774 mm from zero.
    # Check its unchanged definition at local coordinates, and independently
    # check the exact rigid location and every world-space bound below.
    if not oc["BRepCheck_Analyzer"](local).IsValid():
        _error(f"Некорректное STEP-тело: {identifier}",details={"instance_id":identifier,"validation_frame":"rigid_definition_coordinates"})
    return local


def _export(definitions, instances, oc, *, surfacecurve_mode=0, output_path=None, floors=None):
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TopoDS import TopoDS_Compound
    from OCP.BRep import BRep_Builder
    from OCP.TopLoc import TopLoc_Location
    from OCP.gp import gp_Trsf
    from OCP.TDataStd import TDataStd_Name
    from OCP.Interface import Interface_Static
    doc=TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    tool=XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    compound=TopoDS_Compound();BRep_Builder().MakeCompound(compound)
    root=tool.AddShape(compound,True)
    TDataStd_Name.Set_s(root,TCollection_ExtendedString("Building finish / millimetres" if floors is not None else "Room finish / millimetres"))
    floor_labels={};floor_names={item["id"]:item.get("name",item["id"]) for item in floors or []}
    used={item["definition_id"] for item in instances}
    labels={key:tool.AddShape(item["shape"],False) for key,item in definitions.items() if key in used}
    for key,label in labels.items():
        TDataStd_Name.Set_s(label,TCollection_ExtendedString(definitions[key]["label"]))
    for item in instances:
        parent=root
        if floors is not None:
            floor_id=item.get("floor_id","unassigned")
            if floor_id not in floor_labels:
                floor_shape=TopoDS_Compound();BRep_Builder().MakeCompound(floor_shape)
                floor_label=tool.AddShape(floor_shape,True)
                TDataStd_Name.Set_s(floor_label,TCollection_ExtendedString(floor_names.get(floor_id,floor_id)))
                floor_instance=tool.AddComponent(root,floor_label,TopLoc_Location())
                TDataStd_Name.Set_s(floor_instance,TCollection_ExtendedString(floor_names.get(floor_id,floor_id)))
                floor_labels[floor_id]=floor_label
            parent=floor_labels[floor_id]
        transform=gp_Trsf();transform.SetValues(*[v for row,shift in zip(item["rotation"],item["translation_mm"]) for v in (*row,shift)])
        label=tool.AddComponent(parent,labels[item["definition_id"]],TopLoc_Location(transform))
        TDataStd_Name.Set_s(label,TCollection_ExtendedString(item["id"]))
    tool.UpdateAssemblies();writer=STEPCAFControl_Writer()
    previous=Interface_Static.IVal_s("write.surfacecurve.mode")
    try:
        Interface_Static.SetIVal_s("write.surfacecurve.mode",surfacecurve_mode)
        if not writer.Transfer(doc,STEPControl_AsIs):
            _error("Не удалось подготовить STEP помещения")
        if output_path is not None:
            # Native STEP assembly is streamed to a private worker-owned file;
            # no second full copy or base64 expansion of a building in memory.
            if writer.Write(str(output_path))!=oc["IFSelect_RetDone"]:
                _error("Не удалось записать STEP здания")
        else:
            output=io.BytesIO()
            if writer.Writer().WriteStream(output)!=oc["IFSelect_RetDone"]:
                _error("Не удалось записать STEP помещения")
    finally:
        Interface_Static.SetIVal_s("write.surfacecurve.mode",previous)
    if output_path is not None:return Path(output_path)
    return output.getvalue()


def _consuming_bounds_index(bounds, tolerance=1e-4):
    """Tolerance-aware 3D box index; consume each matched occurrence once.

    Index both corners, not only min-X: repeated floors and aligned fixing rows
    otherwise produce a quadratic scan of already-used candidates. Range tests
    use the original coordinates without rounding, so tiny movements across a
    decimal/grid boundary cannot hide a valid match.
    """
    vectors=[[*item["min"],*item["max"]] for item in bounds]
    absent=len(vectors)
    def build(indices, parent=None):
        low=[min(vectors[i][axis] for i in indices) for axis in range(6)]
        high=[max(vectors[i][axis] for i in indices) for axis in range(6)]
        node={"low":low,"high":high,"first":min(indices),"parent":parent}
        if len(indices)<=8:
            node["items"]=sorted(indices);node["children"]=None
        else:
            axis=max(range(6),key=lambda axis:high[axis]-low[axis])
            indices.sort(key=lambda i:vectors[i][axis]);middle=len(indices)//2
            node["items"]=None
            node["children"]=(build(indices[:middle],node),build(indices[middle:],node))
        return node
    root=build(list(range(absent))) if vectors else None
    def consume(expected):
        target=[*expected["min"],*expected["max"]];best=absent;selected=None
        def visit(node):
            nonlocal best,selected
            if node["first"]>=best or any(node["low"][axis]-target[axis]>=tolerance
                    or target[axis]-node["high"][axis]>=tolerance for axis in range(6)):
                return
            if node["children"] is not None:
                first,second=node["children"]
                if second["first"]<first["first"]:first,second=second,first
                visit(first);visit(second)
            else:
                for index in node["items"]:
                    if index>=best:break
                    # Keep exactly the old six-coordinate acceptance predicate.
                    if all(abs(target[axis]-vectors[index][axis])<tolerance for axis in range(6)):
                        best=index;selected=node;break
        if root is not None:visit(root)
        if selected is None:return None
        selected["items"].remove(best)  # at most eight entries in a leaf
        node=selected
        while node is not None:
            node["first"]=(min(child["first"] for child in node["children"]) if node["children"] is not None
                           else node["items"][0] if node["items"] else absent)
            node=node["parent"]
        return best
    return consume


def _roundtrip(content, placed, oc):
    """Check every exported occurrence, including reflected STEP definitions."""
    from . import step_geometry as step, assembly_geometry as ag
    imported=_read_solids(content,oc)
    if len(imported)!=len(placed):
        _error("Экспорт изменил количество деталей комнаты")
    candidates=sorted([(shape,step._bbox(shape,oc)) for shape in imported],key=lambda item:item[1]["min"][0])
    consume=_consuming_bounds_index([bounds for _,bounds in candidates])
    volumes={};matched=[]
    for item in placed:
        expected=item["bounds_mm"]
        match=consume(expected)
        if match is None:
            _error("STEP-экспорт изменил положение детали комнаты")
        shape,_=candidates[match]
        key=(shape.TShape(),shape.Orientation())
        known=volumes.get(key)
        if known is None:
            local=_rigid_definition(shape,oc,item["id"])
            known=ag._mass(local,oc);volumes[key]=known
        else:
            transform=shape.Location().Transformation()
            if abs(transform.ScaleFactor()-1)>1e-12 or transform.IsNegative():_error("STEP изменил расположение экземпляра")
        if abs(known-item["volume_mm3"])>max(.01,item["volume_mm3"]*1e-7):
            _error("STEP-экспорт изменил объём детали комнаты")
        matched.append({**item,"shape":shape})
    return matched


def _spatial_pairs(bodies):
    """Balanced 3D AABB tree: upper floors do not scan every lower-floor part."""
    def overlaps(a,b):
        return all(min(a["max"][k],b["max"][k])-max(a["min"][k],b["min"][k])>1e-5 for k in range(3))
    def tree(indices):
        bounds={"min":[min(bodies[i]["bounds_mm"]["min"][k] for i in indices) for k in range(3)],
                "max":[max(bodies[i]["bounds_mm"]["max"][k] for i in indices) for k in range(3)]}
        if len(indices)<=8:return bounds,indices,None
        axis=max(range(3),key=lambda k:bounds["max"][k]-bounds["min"][k])
        indices.sort(key=lambda i:sum(bodies[i]["bounds_mm"][side][axis] for side in ("min","max")))
        middle=len(indices)//2
        return bounds,None,(tree(indices[:middle]),tree(indices[middle:]))
    if not bodies:return
    root=tree(list(range(len(bodies))))
    for index,body in enumerate(bodies):
        pending=[root]
        while pending:
            bounds,indices,children=pending.pop()
            if not overlaps(body["bounds_mm"],bounds):continue
            if children is not None:pending.extend(children);continue
            for other_index in indices:
                if other_index>=index:continue
                other=bodies[other_index]
                if body["face_id"]!=other["face_id"] and overlaps(body["bounds_mm"],other["bounds_mm"]):
                    yield body,other


def _collisions(bodies, oc, *, streamed=False):
    """Only cross-face pairs; same-wall checks are preserved, not repeated."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from . import assembly_geometry as ag
    checked=0;collisions=[]
    for body,other in _spatial_pairs(bodies):
        bounds=body["bounds_mm"]
        checked+=1
        center=[(max(bounds["min"][k],other["bounds_mm"]["min"][k])+min(bounds["max"][k],other["bounds_mm"]["max"][k]))/2 for k in range(3)]
        identity=[[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]]
        # A shared rigid shift improves thin-thread boolean conditioning
        # without altering either relative placement or physical overlap.
        common=ag._boolean_shape(BRepAlgoAPI_Common,_move(body["shape"],identity,[-v for v in center]),_move(other["shape"],identity,[-v for v in center]))
        volume=ag._mass(common,oc)
        if volume>.001:
            collisions.append({"instance_a":other["id"],"instance_b":body["id"],
                "wall_a":other["wall_id"],"wall_b":body["wall_id"],"face_a":other["face_id"],"face_b":body["face_id"],
                "label_a":other["label"],"label_b":body["label"],"volume_mm3":volume,
                "position_mm":center})
    return {"status":"conflicts" if collisions else "valid",
            "scope":"different_wall_faces_exact_brep_after_aabb", "exact_pairs_checked":checked,
            "complete":True,"collisions":collisions,"maximum_pairs":None,
            "maximum_seconds":None}


def _native_preview(definitions,instances,oc, *, streamed=False):
    """Real profile/fastener edges, sampled once per exact native definition.

    Wall previews have a small 6k budget and may contain bounding boxes. A
    room does not inherit that loss: it samples its imported, placed STEP
    definitions. Fine curves may be simplified; no part becomes a fake box.
    """
    from . import step_geometry as step
    entries=[];aliases={};identical={};total=0;coarse=0
    # Per-definition curve sampling never drops a part or an edge, regardless
    # of the total inventory. Fine native curves remain exact in STEP.
    allowance=12000
    for key,item in definitions.items():
        paths=[];sample_count=0;approximate=False
        for edge in step._shapes(item["shape"],oc["TopAbs_EDGE"],oc):
            points,rough=step._sample_edge(edge,oc,preview=True)
            if len(points)>=2:
                paths.append(points);sample_count+=len(points);approximate|=rough
        if sample_count>allowance:
            # Keep every edge, but reduce the number of chords on curves.
            minimum=sum(min(len(path),3) for path in paths)
            # Preserve every real edge even when one imported definition is
            # intricate. Display sampling is not an assembly rejection quota.
            ratio=(max(allowance,minimum)-minimum)/max(1,sample_count-minimum)
            simplified=[]
            for path in paths:
                count=min(len(path),max(min(len(path),3),int(len(path)*ratio)))
                indices=sorted({round(i*(len(path)-1)/(count-1)) for i in range(count)})
                simplified.append([path[index] for index in indices])
            paths=simplified;approximate=True
        bounds=step._bbox(item["shape"],oc)
        # Reuse identical visual definitions across separately imported wall
        # STEP documents; this only deduplicates view paths, never real solids.
        encoded=json.dumps({"role":item["role"],"bounds":bounds,"paths":paths},sort_keys=True,separators=(",",":")).encode()
        signature=hashlib.sha256(encoded).hexdigest()
        if signature in identical:
            aliases[key]=identical[signature];continue
        aliases[key]=key;identical[signature]=key
        total+=sum(len(path) for path in paths)
        coarse+=int(approximate)
        entries.append({"id":key,"role":item["role"],"label":item["label"],"bounds_mm":bounds,
                        "paths":paths,"representation":"exact_edges","curves_approximate":approximate})
    return {"schema":"wall-assembly-wire-preview/1","definitions":entries,
            "instances":[{**item,"definition_id":aliases[item["definition_id"]]} for item in instances],
            "point_count":total,"coarsened_definition_count":coarse,
            "note":"Рёбра реальных STEP-деталей; кривые отображаются полилиниями. STEP сохраняет точную геометрию."}


def _exact_definition_bytes(shape):
    """Lossless native topology/geometry, including locations and orientation.

    Tessellation is display-only and is deliberately excluded. Do not round,
    heal, strip locations or otherwise normalize a shape to manufacture a hit.
    """
    from OCP.BinTools import BinTools, BinTools_FormatVersion_CURRENT
    output=io.BytesIO()
    BinTools.Write_s(shape,output,False,False,BinTools_FormatVersion_CURRENT)
    return output.getvalue()


def _deduplicate_exact_definitions(definitions,instances,bodies):
    """Share only byte-identical definitions with identical semantic metadata.

    Keep just digests and keys, not a second building-sized copy of the BRep.
    A digest match is only a candidate: serialize that canonical shape again
    and compare the complete bytes before changing any definition reference.
    Every occurrence, placement and independently checked body stays intact.
    """
    canonical={};buckets={};aliases={}
    for key,item in definitions.items():
        metadata=json.dumps({name:value for name,value in item.items() if name!="shape"},
                            sort_keys=True,separators=(",",":"),allow_nan=False)
        content=_exact_definition_bytes(item["shape"])
        signature=(metadata,hashlib.sha256(content).digest())
        match=next((candidate for candidate in buckets.get(signature,[])
                    if content==_exact_definition_bytes(canonical[candidate]["shape"])),None)
        if match is None:
            match=key;canonical[key]=item;buckets.setdefault(signature,[]).append(key)
        aliases[key]=match
    reused=len(definitions)-len(canonical)
    if reused:
        for collection in (instances,bodies):
            for item in collection:item["definition_id"]=aliases[item["definition_id"]]
    return canonical,{"definition_count_before_dedup":len(definitions),"exact_definition_reuses":reused}


def build_room(request):
    from .step_geometry import rigid_bounds_cache
    from .native_definition_cache import native_definition_cache
    with rigid_bounds_cache() as bounds, native_definition_cache() as definitions:
        started=time.monotonic()
        result=_build_room(request)
        result["performance"].update(native_seconds=round(time.monotonic()-started,3),
            exact_bounds_computed=bounds["misses"],rigid_bounds_reused=bounds["hits"],
            definition_cache=dict(definitions))
        return result


def _build_room(request):
    stage_seconds={};stage_started=time.monotonic()
    def finish_stage(name):
        nonlocal stage_started
        now=time.monotonic();stage_seconds[name]=round(max(0.,now-stage_started),3);stage_started=now
    from . import step_geometry as step, assembly_geometry as ag
    oc=step._occt();definitions={};instances=[];placed=[];fastener_count=0;maximum_axis_error=0.
    streamed=request.get("scope_kind")=="building"
    source_manifests={};imported_wall_count=0;reused_wall_count=0
    for number,wall in enumerate(request["walls"],1):
        prefix=f"w{number}:"
        source_key=wall.get("source_id") if streamed else None
        # Retain only checked metadata and the local definitions we already
        # need for export. Never keep a second copy of each source assembly,
        # and never reimport a source because an LRU happened to evict it.
        manifest=hashlib.sha256(json.dumps({"bodies":[{k:v for k,v in body.items() if k!="label"} for body in wall["bodies"]],
            "instances":wall["instances"],"axes":wall.get("fastening_axes",[])},sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        reused=source_key in source_manifests if source_key else False
        if reused:
            if source_manifests[source_key]!=manifest:_error("Один STEP используется с разными реестрами деталей")
            reused_wall_count+=1;source_solids=[];consume_source=None
        else:
            content=Path(wall["content_path"]) if streamed else wall["content"]
            solids=_read_solids(content,oc)
            if len(solids)!=len(wall["bodies"]):_error("STEP и реестр тел стены расходятся")
            source_solids=sorted([(shape,step._bbox(shape,oc)) for shape in solids],key=lambda item:item[1]["min"][0])
            consume_source=_consuming_bounds_index([bounds for _,bounds in source_solids])
            imported_wall_count+=1
            del solids
        by_instance={item["id"]:item for item in wall["instances"]}
        axes={item["fastener_id"]:item for item in wall.get("fastening_axes",[])}
        for body in wall["bodies"]:
            if body["role"]=="substrate":
                continue
            bounds=body["bounds_mm"]
            if not reused:
                match=consume_source(bounds)
                if match is None:_error("Габариты исходной STEP-детали не соответствуют сохранённой сборке")
                shape,_=source_solids[match]
            instance=by_instance[body["instance_id"]]
            key=("source:"+source_key+":" if streamed else prefix)+body["definition_id"]
            definition_rotation,definition_translation=MIRROR,[0.,0.,0.]
            if body["role"]=="fastener":
                axis=axes.get(body["instance_id"])
                if axis is None:_error("Для самореза отсутствует проверенная ось посадки")
                definition_rotation,definition_translation,axis_origin,axis_direction=_fastener_definition_transform(instance,axis)
                fastener_count+=1
            if key not in definitions:
                if reused:_error("Проверенная STEP-деталь отсутствует в реестре определений")
                inverse=[list(row) for row in zip(*instance["rotation"])]
                translation=transform_point(inverse,[0.,0.,0.],[-v for v in instance["translation_mm"]])
                local=_move(shape,inverse,translation)
                mirrored=_move(local,definition_rotation,definition_translation,copy=True)
                if not oc["BRepCheck_Analyzer"](mirrored).IsValid():
                    _error("Изменение системы координат повредило деталь")
                if abs(ag._mass(mirrored,oc)-body["volume_mm3"])>max(.01,body["volume_mm3"]*1e-7):
                    _error("Изменение системы координат изменило объём детали")
                definitions[key]={"shape":mirrored,"label":body["label"],"role":body["role"],
                                  "source_rotation":definition_rotation,"source_translation_mm":definition_translation,
                                  "provenance":body.get("provenance",{})}
            elif (any(abs(definitions[key]["source_rotation"][i][j]-definition_rotation[i][j])>1e-8 for i in range(3) for j in range(3))
                  or any(abs(definitions[key]["source_translation_mm"][i]-definition_translation[i])>1e-6 for i in range(3))):
                _error("Одна STEP-деталь используется с несовместимыми посадочными осями")
            rotation=matmul(matmul(wall["rotation"],instance["rotation"]),MIRROR)
            translation=transform_point(wall["rotation"],wall["translation_mm"],instance["translation_mm"])
            item={"id":prefix+body["instance_id"],"definition_id":key,"rotation":rotation,
                  "translation_mm":translation,"role":body["role"],"wall_id":wall["wall_id"],"face_id":wall["face_id"]}
            if streamed:item["floor_id"]=wall["floor_id"]
            instances.append(item)
            world=_move(definitions[key]["shape"],rotation,translation)
            if body["role"]=="fastener":
                actual=transform_point(rotation,translation,transform_point(definition_rotation,definition_translation,axis_origin))
                expected=transform_point(wall["rotation"],wall["translation_mm"],axis["axis_origin_mm"])
                actual_direction=transform_point(matmul(rotation,definition_rotation),[0.,0.,0.],axis_direction)
                expected_direction=transform_point(wall["rotation"],[0.,0.,0.],axis["axis_direction"])
                error=max(abs(actual[k]-expected[k]) for k in range(3))
                maximum_axis_error=max(maximum_axis_error,error)
                if error>1e-5 or max(abs(actual_direction[k]-expected_direction[k]) for k in range(3))>1e-8:
                    _error("Поворот самореза сместил его ось или посадку")
            placed.append({**item,"shape":world,"wall_id":wall["wall_id"],"face_id":wall["face_id"],
                           "label":body["label"],"volume_mm3":body["volume_mm3"],"bounds_mm":step._bbox(world,oc)})
        if source_key:source_manifests[source_key]=manifest
    finish_stage("source_import_and_placement")
    from .room_assembly_joints_geometry import prepare_native_joints,verify_native_joints
    placed,instances,joint_state=prepare_native_joints(request,definitions,instances,placed,oc)
    used={item["definition_id"] for item in instances}
    definitions={key:value for key,value in definitions.items() if key in used}
    definition_reuse={"definition_count_before_dedup":len(definitions),"exact_definition_reuses":0}
    if streamed:definitions,definition_reuse=_deduplicate_exact_definitions(definitions,instances,placed)
    output_directory=request.get("output_directory")
    floor_metadata=request.get("floors",[]) if streamed else None
    finish_stage("joint_preparation")
    full=_export(definitions,instances,oc,output_path=output_directory/"building.step" if streamed else None,floors=floor_metadata)
    nodes=[item for item in instances if item["role"] not in {"panel","adhesive"}]
    node_step=_export(definitions,nodes,oc,output_path=output_directory/"nodes.step" if streamed else None,floors=floor_metadata)
    finish_stage("step_export")
    imported=_roundtrip(full,placed,oc)
    _roundtrip(node_step,[item for item in placed if item["role"] not in {"panel","adhesive"}],oc)
    finish_stage("step_roundtrip")
    joint_checks=verify_native_joints(imported,joint_state,oc)
    finish_stage("joint_verification")
    checks=_collisions(imported,oc,streamed=streamed)
    checks["validation_frame"]="exported_step_roundtrip"
    finish_stage("cross_wall_collisions")
    if streamed and (checks.get("status")!="valid" or not checks.get("complete") or checks.get("collisions")):
        # All physical/export proofs above still run. Do not spend time sampling
        # and packaging a preview that the publication guard must reject anyway.
        collisions=checks.get("collisions",[]);samples=[]
        for item in collisions[:8]:
            if not isinstance(item,dict):continue
            sample={key:item[key].encode("utf-8")[:256].decode("utf-8",errors="ignore")
                    for key in ("instance_a","instance_b","wall_a","wall_b","face_a","face_b","label_a","label_b")
                    if isinstance(item.get(key),str)}
            volume=item.get("volume_mm3")
            if type(volume) in (int,float) and math.isfinite(volume):sample["volume_mm3"]=volume
            position=item.get("position_mm")
            if isinstance(position,list) and len(position)==3 and all(type(v) in (int,float) and math.isfinite(v) for v in position):
                sample["position_mm"]=position
            samples.append(sample)
        _error("Сборка здания не прошла полную проверку пересечений; просмотр и STEP не опубликованы",
               "BUILDING_COLLISIONS_INVALID",{"body_count":len(instances),"verified_body_count":len(imported),
               "cross_wall":{"status":checks.get("status"),"complete":checks.get("complete"),
                   "validation_frame":"exported_step_roundtrip","exact_pairs_checked":checks.get("exact_pairs_checked"),
                   "collision_count":len(collisions),"omitted_collision_count":max(0,len(collisions)-len(samples)),"collisions":samples},
               "joint_status":joint_checks.get("status") if isinstance(joint_checks,dict) else None,
               "preview_skipped":True,"stages_seconds":dict(stage_seconds)})
    preview=_native_preview(definitions,instances,oc,streamed=streamed)
    inventory=[{key:item[key] for key in ("id","definition_id","wall_id","face_id","role","label","volume_mm3","bounds_mm",*(('floor_id',) if streamed else ()))} for item in placed]
    low=[min(body["bounds_mm"]["min"][k] for body in placed) for k in range(3)]
    high=[max(body["bounds_mm"]["max"][k] for body in placed) for k in range(3)]
    result={"checks":checks,"body_count":len(instances),"verified_body_count":len(imported),"node_body_count":len(nodes),
            "bodies":inventory,"bounds_mm":{"min":low,"max":high,"size":[b-a for a,b in zip(low,high)]},
            "wire_preview":preview,"joint_checks":joint_checks,
            "geometry_validation":{"frame":"rigid_definition_coordinates","world_bounds":"every_exported_instance",
                                   "proper_fastener_count":sum(item["role"]=="fastener" for item in placed),"mirrored_fastener_count":0,"maximum_axis_error_mm":maximum_axis_error},
            "source_reuse":{"native_wall_imports":imported_wall_count,"native_wall_reuses":reused_wall_count,
                            "definition_count":len(definitions),**definition_reuse}}
    if streamed:
        from .building_assembly_worker import write_building_outputs
        result=write_building_outputs(result,request,full,node_step)
    else:
        result={**result,"step":base64.b64encode(full).decode(),"nodes_step":base64.b64encode(node_step).decode(),
                "step_sha256":hashlib.sha256(full).hexdigest(),"nodes_step_sha256":hashlib.sha256(node_step).hexdigest()}
    finish_stage("preview_and_output")
    result["performance"]={"stages_seconds":stage_seconds}
    return result


def run_room_build(result, records, corner_sources=None, *, cancelled=None):
    def check_cancelled():
        if cancelled is not None and cancelled():
            _error("Расчёт помещения отменён", "STEP_CANCELLED")

    check_cancelled()
    from .building_assembly_worker import MAX_RSS_BYTES, MAX_DISK_BYTES
    walls=[];contents=[]
    for placement,record in zip(result["walls"],records):
        check_cancelled()
        content=record.assembled_step.content();contents.append(content)
        walls.append({**placement,"byte_length":len(content),"source_id":record.assembled_step.source_id,"plan":record.plan,
                      "bodies":record.geometry["bodies"],"instances":record.geometry["preview"]["instances"],
                      "contact_specs":[{key:item[key] for key in ("id","body_a","body_b","min_area_mm2")} for item in record.geometry["contact_checks"]],
                      "fastening_axes":[{key:item[key] for key in ("fastener_id","axis_origin_mm","axis_direction")} for item in record.geometry["fastening_checks"]]})
    corner_sources=corner_sources or {}
    if not isinstance(corner_sources,dict) or any(not isinstance(value,bytes) for value in corner_sources.values()):
        _error("Некорректные исходники углов")
    source_manifest=[{"source_id":key,"byte_length":len(corner_sources[key])} for key in sorted(corner_sources)]
    contents.extend(corner_sources[key] for key in sorted(corner_sources))
    header=json.dumps({"walls":walls,"joints":result.get("joints",[]),"sources":source_manifest},ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()+b"\n"
    check_cancelled()
    if not SLOTS.acquire(blocking=False):
        _error("Обработчик STEP занят", "STEP_BUSY")
    try:
        check_cancelled()
        with tempfile.TemporaryDirectory(prefix="interior-room-") as directory:
            root=Path(directory)
            with (root/"input").open("wb") as stream:
                stream.write(header)
                for content in contents:stream.write(content)
            with (root/"input").open("rb") as source,(root/"output").open("w+b") as output,(root/"log").open("w+b") as log:
                check_cancelled()
                process=track_worker(subprocess.Popen(worker_command("backend.room_assembly_worker"),stdin=source,stdout=output,stderr=log,cwd=str(Path(__file__).resolve().parents[1]),**worker_launch_options()))
                try:
                    while process.poll() is None:
                        check_cancelled()
                        if sum(path.stat().st_size for path in root.iterdir() if path.is_file())>MAX_DISK_BYTES:_error("Сборка помещения исчерпала доступный дисковый бюджет", "ROOM_ASSEMBLY_LIMIT")
                        rss=_resident_bytes(process.pid)
                        if rss is not None and rss>MAX_RSS_BYTES:_error("Сборка помещения исчерпала доступный бюджет памяти", "ROOM_ASSEMBLY_LIMIT")
                        time.sleep(.1)
                finally:
                    if process.poll() is None:process.kill()
                    process.wait()
                check_cancelled()
                if process.returncode:_error("Обработчик комнаты завершился с ошибкой", "STEP_WORKER_FAILED")
                output.seek(0);response=_json(output.read())
                check_cancelled()
                if "error" in response:
                    error=response["error"]
                    raise StepGeometryError(error["message"],code=error.get("code","STEP_WORKER_FAILED"),details=error.get("details"))
                return response["result"]
    finally:
        SLOTS.release()


def main():
    descriptor=os.dup(1);os.dup2(2,1)
    try:
        if len(sys.argv)!=1:_error("Обработчик комнаты не принимает пути")
        try:
            import resource
            from .building_assembly_worker import MAX_RSS_BYTES, MAX_DISK_BYTES
            resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_DISK_BYTES,MAX_DISK_BYTES))
            if sys.platform.startswith("linux"):resource.setrlimit(resource.RLIMIT_AS,(MAX_RSS_BYTES*2,MAX_RSS_BYTES*2))
        except (ImportError,OSError,ValueError):pass
        request=_read(sys.stdin.buffer)
        response={"result":build_room(request)}
    except StepGeometryError as error:response={"error":error.as_dict()}
    except Exception:
        import traceback
        traceback.print_exc()
        response={"error":{"code":"ROOM_ASSEMBLY_FAILED","message":"Не удалось собрать помещение из сохранённых STEP стен"}}
    raw=json.dumps(response,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()
    with os.fdopen(descriptor,"wb") as output:output.write(raw)


if __name__=="__main__":main()
