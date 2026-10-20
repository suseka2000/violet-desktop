"""Resource-contained whole-wall kernel; no caller-controlled paths or imports."""
from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path
import subprocess
import sys
from .desktop_runtime import worker_command, worker_launch_options, track_worker
import tempfile
import time

from .assembly_worker import _json
from .step_geometry import StepGeometryError
from .step_worker import SLOTS, _resident_bytes
from .wall_assembly_geometry import build_wall_assembly, validate_wall_plan, validate_wall_sources, required_source_ids

MAX_HEADER_BYTES=2*1024**2
MAX_OUTPUT_BYTES=128*1024**2
from .building_assembly_worker import MAX_RSS_BYTES, DISK_RESERVE_BYTES
WORKER_TIMEOUT_SECONDS=240
HIDDEN_WORKER_TIMEOUT_SECONDS=360


def wall_build_timeout(plan):
    """No arbitrary wall-complexity timeout; users can cancel background work."""
    return None


def _read_request(stream):
    raw=stream.readline()
    if not raw.endswith(b"\n"):
        raise StepGeometryError("Оборвана раскладка стены",code="WALL_ASSEMBLY_INVALID")
    try:header=_json(raw)
    except (ValueError,UnicodeError) as exc:raise StepGeometryError("Некорректный запрос сборки стены",code="WALL_ASSEMBLY_INVALID") from exc
    if not isinstance(header,dict) or set(header)!={"plan","sources"} or not isinstance(header["sources"],list):
        raise StepGeometryError("Некорректный комплект STEP стены",code="WALL_ASSEMBLY_INVALID")
    plan=validate_wall_plan(header["plan"]);sources={};total=0
    expected_sources=required_source_ids(plan)
    if len(header["sources"])!=len(expected_sources):
        raise StepGeometryError("Некорректный комплект STEP стены",code="WALL_ASSEMBLY_INVALID")
    for entry in header["sources"]:
        if not isinstance(entry,dict) or set(entry)!={"source_id","byte_length"} or entry["source_id"] not in expected_sources or entry["source_id"] in sources:
            raise StepGeometryError("Некорректный исходный STEP стены",code="WALL_ASSEMBLY_INVALID")
        size=entry["byte_length"]
        if isinstance(size,bool) or not isinstance(size,int) or not 1<=size<=64*1024**2 or total+size>64*1024**2:
            raise StepGeometryError("Слишком большой комплект STEP стены",code="WALL_ASSEMBLY_LIMIT")
        total+=size;sources[entry["source_id"]]=stream.read(size)
        if len(sources[entry["source_id"]])!=size:raise StepGeometryError("Передача STEP прервана",code="WALL_ASSEMBLY_INVALID")
    if stream.read(1):raise StepGeometryError("Лишние байты запроса",code="WALL_ASSEMBLY_INVALID")
    validate_wall_sources(sources,plan)
    return sources,plan


def run_whole_wall_build(sources,plan,*,timeout_seconds=None,cancelled=None):
    maximum=wall_build_timeout(plan)
    if timeout_seconds is None:timeout_seconds=maximum
    if timeout_seconds is not None and (isinstance(timeout_seconds,bool) or not isinstance(timeout_seconds,(int,float)) or not math.isfinite(timeout_seconds) or timeout_seconds<=0):
        raise StepGeometryError("Некорректный лимит времени сборки стены",code="WALL_ASSEMBLY_LIMIT")
    plan=validate_wall_plan(plan);validate_wall_sources(sources,plan)
    header=json.dumps({"plan":plan,"sources":[{"source_id":key,"byte_length":len(sources[key])} for key in sorted(sources)]},ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()+b"\n"
    if not SLOTS.acquire(blocking=False):raise StepGeometryError("Обработчик STEP занят. Дождитесь завершения расчёта.",code="STEP_BUSY")
    try:
        if cancelled is not None and cancelled():
            raise StepGeometryError("Расчёт стены отменён", code="STEP_CANCELLED")
        with tempfile.TemporaryDirectory(prefix="interior-whole-wall-") as directory:
            scratch=Path(directory)
            with (scratch/"input").open("wb") as source:
                source.write(header)
                for key in sorted(sources):source.write(sources[key])
            with (scratch/"input").open("rb") as source,(scratch/"result").open("w+b") as output,(scratch/"log").open("w+b") as log:
                process=track_worker(subprocess.Popen(worker_command("backend.wall_assembly_worker"),stdin=source,stdout=output,stderr=log,cwd=str(Path(__file__).resolve().parents[1]),**worker_launch_options()))
                deadline=time.monotonic()+timeout_seconds if timeout_seconds is not None else None
                resource_at=0.
                try:
                    while process.poll() is None:
                        if cancelled is not None and cancelled():
                            raise StepGeometryError("Расчёт стены отменён", code="STEP_CANCELLED")
                        current=time.monotonic()
                        if deadline is not None and current>deadline:raise StepGeometryError(f"Расчёт всей стены превысил заданное время {timeout_seconds:g} секунд",code="STEP_TIMEOUT")
                        if current>=resource_at:
                            rss=_resident_bytes(process.pid)
                            if rss is not None and rss>MAX_RSS_BYTES:raise StepGeometryError("Недостаточно рабочей памяти для сборки стены",code="WALL_ASSEMBLY_RESOURCES")
                            if shutil.disk_usage(scratch).free<DISK_RESERVE_BYTES:raise StepGeometryError("Недостаточно временного диска для сборки стены",code="WALL_ASSEMBLY_RESOURCES")
                            resource_at=current+1.
                        time.sleep(.1)
                finally:
                    if process.poll() is None:process.kill()
                    process.wait()
                if process.returncode:raise StepGeometryError("Обработчик стены завершился с ошибкой",code="STEP_WORKER_FAILED")
                output.seek(0)
                try:envelope=_json(output.read())
                except (ValueError,UnicodeError) as exc:raise StepGeometryError("Некорректный ответ сборки стены",code="STEP_WORKER_FAILED") from exc
                if not isinstance(envelope,dict) or len(envelope)!=1:raise StepGeometryError("Некорректный ответ сборки стены",code="STEP_WORKER_FAILED")
                if "error" in envelope:
                    error=envelope["error"]
                    raise StepGeometryError(error.get("message","Ошибка сборки стены"),code=error.get("code","STEP_WORKER_FAILED"),details=error.get("details"))
                if not isinstance(envelope.get("result"),dict) or envelope["result"].get("schema")!="whole-wall-assembly-build/1":raise StepGeometryError("Пустая сборка стены",code="STEP_WORKER_FAILED")
                return envelope["result"]
    finally:SLOTS.release()


def main():
    response_fd=os.dup(1);os.dup2(2,1)
    try:
        try:
            import resource
            if sys.platform.startswith("linux"):resource.setrlimit(resource.RLIMIT_AS,(MAX_RSS_BYTES*2,MAX_RSS_BYTES*2))
        except (ImportError,OSError,ValueError):pass
        if len(sys.argv)!=1:raise StepGeometryError("Обработчик не принимает пути",code="WALL_ASSEMBLY_INVALID")
        sources,plan=_read_request(sys.stdin.buffer)
        try:
            import resource
            maximum=wall_build_timeout(plan)
            if maximum is not None:resource.setrlimit(resource.RLIMIT_CPU,(maximum-5,maximum))
        except (ImportError,OSError,ValueError):pass
        envelope={"result":build_wall_assembly(sources,plan)}
    except StepGeometryError as exc:envelope={"error":exc.as_dict()}
    except Exception:envelope={"error":{"code":"WALL_ASSEMBLY_BUILD_FAILED","message":"Не удалось безопасно собрать стену. Проверьте раскладку и исходные STEP.","details":{}}}
    raw=json.dumps(envelope,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()
    with os.fdopen(response_fd,"wb") as output:output.write(raw)


if __name__=="__main__":main()
