"""Framed, disk-backed building composition using the existing exact CAD kernel.

No caller paths cross the native boundary. Frames preserve every record; the
building is bounded by available RSS/disk, not a wall/body or frame-byte quota.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from .desktop_runtime import worker_command, worker_launch_options, track_worker, physical_memory_bytes

from .assembly_worker import _json
from .step_geometry import StepGeometryError
from .step_worker import SLOTS, _resident_bytes

def _configured_budget(name,default):
    value=os.environ.get(name)
    if value is None:return default
    if not value.isdigit() or int(value)<1:raise ValueError(f"{name} должен быть положительным целым числом")
    return int(value)


_physical_memory=physical_memory_bytes()
MAX_RSS_BYTES=_configured_budget("INTERIOR_BUILDING_MAX_RSS_MIB",max(1024,int(_physical_memory*.8)//1024**2))*1024**2
# Resource budgets are configurable, unlike geometry quotas. Reserve actual
# free disk for the rest of the service instead of rejecting an 8-GiB building.
DISK_RESERVE_BYTES=2*1024**3
MAX_DISK_BYTES=_configured_budget("INTERIOR_BUILDING_MAX_DISK_MIB",max(1,(shutil.disk_usage(tempfile.gettempdir()).free-DISK_RESERVE_BYTES)//1024**2))*1024**2
TIMEOUT_SECONDS=_configured_budget("INTERIOR_BUILDING_TIMEOUT_SECONDS",0)
CHUNK_INSTANCES=512
CHUNK_POINTS=100000


def _error(message,code="BUILDING_ASSEMBLY_INVALID"):
    raise StepGeometryError(message,code=code)


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():_error("Расчёт здания отменён","STEP_CANCELLED")


def _sha(value):
    return isinstance(value,str) and len(value)==64 and all(char in "0123456789abcdef" for char in value)


def file_evidence(path):
    size=0;digest=hashlib.sha256()
    with Path(path).open("rb") as source:
        while block:=source.read(1024**2):size+=len(block);digest.update(block)
    return {"byte_length":size,"sha256":digest.hexdigest()}


def _write_frame(stream,value):
    content=json.dumps(value,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()+b"\n"
    _write_bounded(stream,content)


def _write_bounded(stream,content):
    if stream.tell()+len(content)>MAX_DISK_BYTES:
        _error("Запрос исчерпал дисковый бюджет сборки","BUILDING_ASSEMBLY_LIMIT")
    stream.write(content)


def _read_frame(stream):
    line=stream.readline()
    if not line or not line.endswith(b"\n"):
        _error("Оборван или превышен один блок реестра здания","BUILDING_ASSEMBLY_LIMIT")
    value=_json(line)
    if not isinstance(value,dict):_error("Блок реестра должен быть объектом")
    return value


def write_request(stream,result,records,corner_sources=None,*,cancelled=None):
    walls=result["walls"];joints=result.get("joints",[])
    if not walls or len(walls)!=len(records):_error("Состав здания и исходных стен расходится")
    _write_frame(stream,{"schema":"building-native-stream/1","wall_count":len(walls),"joint_count":len(joints)})
    for floor in result.get("floors",[]):
        _write_frame(stream,{"kind":"floor","floor":{key:floor[key] for key in ("id","name","elevation_mm")}})
    sent=set()
    def source(identity,content,corner=False):
        _check_cancelled(cancelled)
        if not _sha(identity) or not isinstance(content,bytes) or not content:
            _error("Некорректный исходник здания")
        if hashlib.sha256(content).hexdigest()!=identity:_error("Изменились байты исходника здания")
        if identity in sent:
            if corner:_write_frame(stream,{"kind":"corner_source","source_id":identity})
            return
        _write_frame(stream,{"kind":"source","source_id":identity,"byte_length":len(content),"corner":corner})
        for offset in range(0,len(content),1024**2):
            _check_cancelled(cancelled);_write_bounded(stream,content[offset:offset+1024**2])
        sent.add(identity)
    for placement,record in zip(walls,records):
        _check_cancelled(cancelled)
        identity=record.assembled_step.source_id
        if identity not in sent:source(identity,record.assembled_step.content())
        _write_frame(stream,{"kind":"wall","wall":{**placement,"source_id":identity,"plan":record.plan,
            "bodies":record.geometry["bodies"],"instances":record.geometry["preview"]["instances"],
            "contact_specs":[{key:item[key] for key in ("id","body_a","body_b","min_area_mm2")} for item in record.geometry["contact_checks"]],
            "fastening_axes":[{key:item[key] for key in ("fastener_id","axis_origin_mm","axis_direction")} for item in record.geometry["fastening_checks"]]}})
    corner_sources=corner_sources or {}
    if not isinstance(corner_sources,dict):_error("Некорректные исходники углов здания")
    for identity in sorted(corner_sources):source(identity,corner_sources[identity],True)
    for joint in joints:
        _check_cancelled(cancelled);_write_frame(stream,{"kind":"joint","joint":joint})
    _write_frame(stream,{"kind":"end"})


def _check_wall(wall):
    from .room_assembly_worker import _check_instances
    from .whole_wall_assemblies import _bounded_numbers
    if not isinstance(wall,dict):_error("Некорректный реестр стены")
    _bounded_numbers(wall);_check_instances(wall,max_bodies=None)
    if any(not isinstance(wall.get(key),str) or not 1<=len(wall[key])<=128 for key in ("wall_id","face_id","floor_id")):
        _error("Не задана сторона стены или этаж")
    rotation,translation=wall.get("rotation"),wall.get("translation_mm")
    if (not isinstance(rotation,list) or len(rotation)!=3 or any(not isinstance(row,list) or len(row)!=3 for row in rotation)
            or not isinstance(translation,list) or len(translation)!=3):_error("Некорректная матрица стены")
    if any(type(value) not in (int,float) or not math.isfinite(value) or abs(value)>1e10 for value in [*sum(rotation,[]),*translation]):
        _error("Некорректные координаты стены")
    if any(abs(sum(rotation[i][k]*rotation[j][k] for k in range(3))-(i==j))>1e-8 for i in range(3) for j in range(3)):
        _error("Масштабирование и сдвиг формы стены запрещены")


def read_request(stream,directory):
    header=_read_frame(stream)
    if set(header)!={"schema","wall_count","joint_count"} or header["schema"]!="building-native-stream/1":
        _error("Неизвестный поток сборки здания")
    if type(header["wall_count"]) is not int or header["wall_count"]<1 or type(header["joint_count"]) is not int or header["joint_count"]<0:
        _error("Некорректное число блоков сборки")
    directory=Path(directory);sources={};corners=set();walls=[];joints=[];floors=[];floor_ids=set();seen=set();total=0
    while True:
        value=_read_frame(stream);kind=value.get("kind")
        if kind=="end":
            if set(value)!={"kind"} or stream.read(1):_error("Лишние данные после конца здания")
            break
        if kind=="floor":
            floor=value.get("floor")
            if (set(value)!={"kind","floor"} or not isinstance(floor,dict) or set(floor)!={"id","name","elevation_mm"}
                    or not isinstance(floor["id"],str) or not 1<=len(floor["id"])<=128 or floor["id"] in floor_ids
                    or not isinstance(floor["name"],str) or not 1<=len(floor["name"])<=500
                    or type(floor["elevation_mm"]) not in (int,float) or not math.isfinite(floor["elevation_mm"])):
                _error("Некорректные сведения об этаже")
            floor_ids.add(floor["id"]);floors.append(floor)
        elif kind=="source":
            if set(value)!={"kind","source_id","byte_length","corner"}:_error("Некорректный блок исходника")
            identity,size=value["source_id"],value["byte_length"]
            if not _sha(identity) or identity in sources or type(size) is not int or size<1 or type(value["corner"]) is not bool:
                _error("Некорректный размер или SHA исходника")
            total+=size
            if total>MAX_DISK_BYTES:_error("Исходники исчерпали дисковый бюджет сборки","BUILDING_ASSEMBLY_LIMIT")
            path=directory/("source-"+identity+".step");check=hashlib.sha256();remaining=size
            with path.open("xb") as output:
                while remaining:
                    block=stream.read(min(1024**2,remaining))
                    if not block:_error("Передача исходника здания оборвана")
                    output.write(block);check.update(block);remaining-=len(block)
            if check.hexdigest()!=identity:_error("Контрольная сумма исходника здания не совпадает")
            sources[identity]=path
            if value["corner"]:corners.add(identity)
        elif kind=="corner_source":
            if set(value)!={"kind","source_id"} or not _sha(value["source_id"]) or value["source_id"] not in sources:_error("Неизвестный исходник угла")
            corners.add(value["source_id"])
        elif kind=="wall":
            if set(value)!={"kind","wall"}:_error("Некорректный блок стены")
            wall=value["wall"];_check_wall(wall)
            identity=wall.get("source_id");key=(wall["wall_id"],wall["face_id"])
            if not _sha(identity) or identity not in sources or key in seen:_error("Неизвестный STEP или повторяющаяся сторона стены")
            seen.add(key);wall["content_path"]=sources[identity];walls.append(wall)
            if len(walls)>header["wall_count"]:_error("Лишние стены в потоке здания")
        elif kind=="joint":
            from .whole_wall_assemblies import _bounded_numbers
            if set(value)!={"kind","joint"} or not isinstance(value["joint"],dict):_error("Некорректный блок угла")
            _bounded_numbers(value["joint"]);joints.append(value["joint"])
            if len(joints)>header["joint_count"]:_error("Лишние углы в потоке здания")
        else:_error("Неизвестный тип блока здания")
    if len(walls)!=header["wall_count"] or len(joints)!=header["joint_count"]:_error("Поток здания не содержит всех блоков")
    return {"scope_kind":"building","walls":walls,"joints":joints,"floors":floors,
            "source_content":{key:sources[key].read_bytes() for key in corners},"output_directory":directory}


def _write_json(path,value):
    with path.open("w",encoding="utf-8") as stream:
        json.dump(value,stream,ensure_ascii=False,separators=(",",":"),allow_nan=False)
    return {"path":str(path),**file_evidence(path)}


def write_building_outputs(result,request,full,nodes):
    directory=request["output_directory"]
    bodies=result.pop("bodies");preview=result.pop("wire_preview")
    body_file=_write_json(directory/"bodies.json",bodies)
    definitions={item["id"]:item for item in preview["definitions"]}
    points={key:sum(len(path) for path in item.get("paths",[])) for key,item in definitions.items()}
    floor_by_wall={item["wall_id"]:item["floor_id"] for item in request["walls"]}
    grouped=defaultdict(list)
    for instance in preview["instances"]:
        item={**instance,"floor_id":floor_by_wall[instance["wall_id"]]}
        grouped[item["wall_id"]].append(item)
    chunks=[]
    for wall_id,instances in grouped.items():
        batch=[];used=set();count=0;part=0
        def flush():
            nonlocal batch,used,count,part
            if not batch:return
            part+=1;identity=hashlib.sha256(wall_id.encode()).hexdigest()[:24]+f"-{part}"
            value={**{key:value for key,value in preview.items() if key not in {"definitions","instances","point_count"}},
                   "definitions":[definitions[key] for key in sorted(used)],"instances":batch,"point_count":count}
            chunks.append({"id":identity,"wall_id":wall_id,"floor_id":floor_by_wall[wall_id],
                "face_ids":sorted({item["face_id"] for item in batch}),"point_count":count,"instance_count":len(batch),
                **_write_json(directory/f"preview-{identity}.json",value)})
            batch=[];used=set();count=0
        for instance in instances:
            key=instance["definition_id"]
            additional=0 if key in used else points[key]
            if batch and (len(batch)>=CHUNK_INSTANCES or count+additional>CHUNK_POINTS):flush()
            if key not in used:used.add(key);count+=points[key]
            batch.append(instance)
        flush()
    full_evidence=file_evidence(full);node_evidence=file_evidence(nodes)
    return {**result,"step_path":str(full),"nodes_step_path":str(nodes),
        "step_sha256":full_evidence["sha256"],"nodes_step_sha256":node_evidence["sha256"],
        "step_byte_length":full_evidence["byte_length"],"nodes_step_byte_length":node_evidence["byte_length"],
        "bodies_path":body_file["path"],"bodies_sha256":body_file["sha256"],"bodies_byte_length":body_file["byte_length"],
        "preview_chunks":chunks,"preview_point_count":preview["point_count"],"preview_definition_count":len(definitions)}


def run_building_build(result,records,corner_sources=None,*,output_directory,cancelled=None):
    _check_cancelled(cancelled)
    if not SLOTS.acquire(blocking=False):_error("Обработчик STEP занят","STEP_BUSY")
    try:
        with tempfile.TemporaryDirectory(prefix="interior-building-") as directory:
            scratch=Path(directory).resolve()
            with (scratch/"input").open("wb") as stream:write_request(stream,result,records,corner_sources,cancelled=cancelled)
            if (scratch/"input").stat().st_size>MAX_DISK_BYTES:_error("Запрос исчерпал дисковый бюджет сборки","BUILDING_ASSEMBLY_LIMIT")
            with (scratch/"input").open("rb") as source,(scratch/"result").open("w+b") as output,(scratch/"log").open("w+b") as log:
                _check_cancelled(cancelled)
                process=track_worker(subprocess.Popen(worker_command("backend.building_assembly_worker"),stdin=source,stdout=output,stderr=log,cwd=scratch,**worker_launch_options()))
                deadline=time.monotonic()+TIMEOUT_SECONDS;resource_at=0.
                try:
                    while process.poll() is None:
                        _check_cancelled(cancelled)
                        current=time.monotonic()
                        if TIMEOUT_SECONDS and current>deadline:_error("Сборка здания исчерпала настроенный бюджет времени вычислений","STEP_TIMEOUT")
                        if current>=resource_at:
                            rss=_resident_bytes(process.pid)
                            if rss is not None and rss>MAX_RSS_BYTES:_error("Сборка здания исчерпала настроенный бюджет рабочей памяти","BUILDING_ASSEMBLY_LIMIT")
                            if (sum(path.stat().st_size for path in scratch.iterdir() if path.is_file())>MAX_DISK_BYTES
                                    or shutil.disk_usage(scratch).free<DISK_RESERVE_BYTES):
                                _error("Недостаточно временного диска для сборки здания","BUILDING_ASSEMBLY_LIMIT")
                            resource_at=current+1.
                        time.sleep(.1)
                finally:
                    if process.poll() is None:process.kill()
                    process.wait()
                _check_cancelled(cancelled)
                if process.returncode:_error("Обработчик здания завершился с ошибкой","STEP_WORKER_FAILED")
                output.seek(0);payload=output.read()
                response=_json(payload)
                if not isinstance(response,dict) or set(response) not in ({"result"},{"error"}):_error("Некорректный ответ обработчика здания")
            if "error" in response:
                error=response["error"];raise StepGeometryError(error["message"],code=error.get("code","STEP_WORKER_FAILED"),details=error.get("details"))
            built=response["result"];target=Path(output_directory)
            files=[("step_path","step_sha256","step_byte_length"),("nodes_step_path","nodes_step_sha256","nodes_step_byte_length"),("bodies_path","bodies_sha256","bodies_byte_length")]
            entries=[(built,path,sha,size) for path,sha,size in files]+[(chunk,"path","sha256","byte_length") for chunk in built["preview_chunks"]]
            allowed={"building.step","nodes.step","bodies.json"}
            for chunk in built["preview_chunks"]:
                identity=chunk.get("id")
                if not isinstance(identity,str) or not 1<=len(identity)<=128 or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in identity):
                    _error("Некорректный идентификатор части просмотра")
                allowed.add(f"preview-{identity}.json")
            seen=set()
            for entry,path_key,sha_key,size_key in entries:
                _check_cancelled(cancelled)
                original=Path(entry[path_key])
                if original.name not in allowed or original in seen or original.parent!=scratch or original.is_symlink() or not original.is_file():
                    _error("Некорректный служебный путь результата")
                seen.add(original)
                evidence=file_evidence(original)
                if evidence!={"sha256":entry[sha_key],"byte_length":entry[size_key]}:_error("Повреждён файл результата здания")
            _check_cancelled(cancelled);target.mkdir(parents=True,exist_ok=True)
            for entry,path_key,_,_ in entries:
                _check_cancelled(cancelled);original=Path(entry[path_key]);destination=target/original.name
                with original.open("rb") as source,destination.open("xb") as output:
                    while block:=source.read(1024**2):
                        _check_cancelled(cancelled);output.write(block)
                entry[path_key]=str(destination)
            return built
    finally:SLOTS.release()


def worker_error(error):
    """Keep native call sites and bounded IDs, never shapes or STEP payloads."""
    result = error.as_dict()
    details = dict(result["details"]) if isinstance(result.get("details"), dict) else {}
    sites = []
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        site = {"file": Path(frame.f_code.co_filename).name,
                "function": frame.f_code.co_name, "line": trace.tb_lineno}
        for name in ("body", "tool", "joint", "spec", "fastener"):
            value = frame.f_locals.get(name)
            if isinstance(value, dict):
                identity = {key: value[key] for key in ("id", "instance_id", "joint_id", "role", "wall_index", "body_a", "body_b")
                            if key in value and isinstance(value[key], (str, int)) and len(str(value[key])) <= 256}
                if identity:
                    site[name] = identity
        sites.append(site)
        trace = trace.tb_next
    details["call_sites"] = sites[-12:]
    result["details"] = details
    return result


def main():
    descriptor=os.dup(1);os.dup2(2,1)
    try:
        if len(sys.argv)!=1:_error("Обработчик здания не принимает пути")
        try:
            import resource
            if TIMEOUT_SECONDS:resource.setrlimit(resource.RLIMIT_CPU,(max(1,TIMEOUT_SECONDS-5),TIMEOUT_SECONDS))
            resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_DISK_BYTES,MAX_DISK_BYTES))
            if sys.platform.startswith("linux"):resource.setrlimit(resource.RLIMIT_AS,(MAX_RSS_BYTES*2,MAX_RSS_BYTES*2))
        except (ImportError,OSError,ValueError):pass
        from .room_assembly_worker import build_room
        request=read_request(sys.stdin.buffer,Path.cwd())
        response={"result":build_room(request)}
    except StepGeometryError as error:response={"error":worker_error(error)}
    except Exception:
        import traceback
        traceback.print_exc();response={"error":{"code":"BUILDING_ASSEMBLY_FAILED","message":"Не удалось собрать здание из проверенных STEP стен"}}
    with os.fdopen(descriptor,"w",encoding="utf-8") as output:json.dump(response,output,ensure_ascii=False,separators=(",",":"),allow_nan=False)


if __name__=="__main__":main()
