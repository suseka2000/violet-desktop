"""Persistent room assembly jobs, isolated from project-save transactions."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import queue
import re
import sqlite3
import threading
import uuid
from collections import OrderedDict
from time import perf_counter

from .assembly_jobs import input_signature
from .room_assembly import POLICY, prepare_room, room_targets, refresh_room_manifest
from .step_geometry import StepGeometryError
from .storage.repository import ConflictError, NotFoundError

ACTIVE={"queued","waiting_for_walls","running"}
MAX_RESULT_BYTES=32*1024**2
MAX_CACHE_BYTES=768*1024**2
MAX_CACHE_JOBS=24


def now():
    return datetime.now(timezone.utc).isoformat()


class RoomAssemblyJobs:
    policy = POLICY
    def __init__(self, repository, assembly_jobs, *, path=None, start=True):
        self.repository=repository;self.assembly_jobs=assembly_jobs
        self.path=Path(path or repository.db_path.with_name(repository.db_path.stem+".room-assemblies.sqlite3"))
        self.directory=self.path.with_suffix("")
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.directory.mkdir(parents=True,exist_ok=True)
        self._lock=threading.RLock();self._queue=queue.Queue();self._stop=threading.Event();self._thread=None
        self._source_signatures=OrderedDict()
        with self._connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS room_jobs (id TEXT PRIMARY KEY,project_id TEXT NOT NULL,space_id TEXT NOT NULL,revision INTEGER NOT NULL,signature TEXT NOT NULL,status_json TEXT NOT NULL,updated_at TEXT NOT NULL)")
        if start:self.start()

    @contextmanager
    def _connection(self):
        connection=sqlite3.connect(self.path,timeout=15);connection.row_factory=sqlite3.Row
        try:
            with connection:yield connection
        finally:connection.close()

    def start(self):
        with self._lock:
            if self._thread is not None:return
            with self._connection() as connection:
                rows=connection.execute("SELECT * FROM room_jobs ORDER BY updated_at").fetchall()
            for row in rows:
                if json.loads(row["status_json"])["status"] in ACTIVE:
                    self._update(row["id"],status="queued",message="3D-сборка продолжится после перезапуска.")
                    self._queue.put(row["id"])
            self._thread=threading.Thread(target=self._run,name="violet-room-assemblies",daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set();self._queue.put(None)
        if self._thread:self._thread.join(timeout=.2)

    def _row(self, identifier):
        with self._connection() as connection:
            row=connection.execute("SELECT * FROM room_jobs WHERE id=?",(identifier,)).fetchone()
        if row is None:raise NotFoundError("3D-сборка помещения не найдена")
        return row

    def _current(self,row):
        if self._stop.is_set():return False
        if json.loads(row["status_json"]).get("policy")!=self.policy:return False
        # Purging a disposable job can leave its source project intact. The
        # running native worker must stop then too, without recreating files.
        with self._connection() as connection:
            latest=connection.execute("SELECT status_json FROM room_jobs WHERE id=?",(row["id"],)).fetchone()
            if latest is None or json.loads(latest["status_json"]).get("status")=="cancelled":return False
        try:
            head=self.repository.head(row["project_id"])
        except NotFoundError:return False
        if head["revision"]!=row["revision"]:return False
        # Polling and every wall checkpoint used to parse and hash the complete
        # saved project. Immutable snapshot hashes give an O(1) currentness check
        # after one signature verification; deletion is still checked every time.
        key=(row["project_id"],head["snapshot_sha256"])
        with self._lock:
            signature=self._source_signatures.get(key)
            if signature is None:
                project=self.repository.get(row["project_id"])
                if project.revision!=row["revision"]:return False
                signature=input_signature(project)
                self._source_signatures[key]=signature
                while len(self._source_signatures)>64:self._source_signatures.popitem(last=False)
            else:self._source_signatures.move_to_end(key)
        return signature==row["signature"]

    def enqueue(self,project_id,space_id,expected_revision):
        project=self.repository.get(project_id)
        if project.revision!=expected_revision:
            raise ConflictError(project_id,expected_revision,project.revision)
        room_targets(project,space_id)
        signature=input_signature(project)
        with self._lock, self._connection() as connection:
            rows=connection.execute("SELECT * FROM room_jobs WHERE project_id=? AND space_id=? AND revision=? AND signature=? ORDER BY updated_at DESC",(project_id,space_id,expected_revision,signature)).fetchall()
            for row in rows:
                status=json.loads(row["status_json"])
                if status.get("policy")!=POLICY:continue
                if status["status"] in ACTIVE:
                    return self.status(row["id"])
                if status["status"]=="ready":
                    try:self._cached_result(row,verify_steps=True)
                    except (ValueError,OSError,KeyError,TypeError):continue
                    return self.status(row["id"])
            # Queued jobs for older room revisions never enter the CAD worker.
            identifier=uuid.uuid4().hex
            status={"job_id":identifier,"project_id":project_id,"space_id":space_id,"source_revision":expected_revision,
                    "policy":POLICY,"status":"queued","message":"Собираем отделку помещения в фоне.","progress":{"completed":0,"total":3},"updated_at":now()}
            connection.execute("INSERT INTO room_jobs VALUES (?,?,?,?,?,?,?)",(identifier,project_id,space_id,expected_revision,signature,json.dumps(status,ensure_ascii=False),now()))
        self._queue.put(identifier)
        return status

    def _update(self,identifier,**values):
        with self._lock, self._connection() as connection:
            row=connection.execute("SELECT status_json FROM room_jobs WHERE id=?",(identifier,)).fetchone()
            if row is None:return
            status=json.loads(row["status_json"]);status.update(values,updated_at=now())
            connection.execute("UPDATE room_jobs SET status_json=?,updated_at=? WHERE id=?",(json.dumps(status,ensure_ascii=False),now(),identifier))

    def _cached_result(self,row,*,verify_steps=False):
        folder=self.directory/row["id"];path=folder/"result.json"
        if not path.is_file() or path.stat().st_size>MAX_RESULT_BYTES:
            raise ValueError("Файл сборки отсутствует или слишком велик")
        result=json.loads(path.read_text())
        if not isinstance(result,dict) or not isinstance(result.get("export_modes"),dict):
            raise ValueError("Некорректный формат кэша комнаты")
        if result.get("source_signature")!=row["signature"] or result.get("source_revision")!=row["revision"] or result.get("policy")!=POLICY:
            raise ValueError("Кэш 3D-комнаты относится к другой версии проекта")
        for filename,mode in (("room.step","complete"),("nodes.step","nodes")):
            metadata=result["export_modes"].get(mode)
            if (not isinstance(metadata,dict) or type(metadata.get("byte_length")) is not int
                    or not 1<=metadata["byte_length"]<=128*1024**2
                    or not isinstance(metadata.get("sha256"),str)
                    or re.fullmatch(r"[0-9a-f]{64}",metadata["sha256"]) is None):
                raise ValueError("Некорректный размер или контрольная сумма кэша STEP")
            path=folder/filename
            if not path.is_file() or path.stat().st_size!=metadata["byte_length"]:
                raise ValueError("Файл STEP сборки отсутствует или повреждён")
            if verify_steps and hashlib.sha256(path.read_bytes()).hexdigest()!=metadata["sha256"]:
                raise ValueError("Контрольная сумма кэша STEP не совпадает")
        return result

    def status(self,identifier):
        row=self._row(identifier);status=json.loads(row["status_json"])
        if not self._current(row):
            if not self._stop.is_set():
                self._update(identifier,status="stale",message="Проект изменился или удалён. Соберите 3D-модель для текущей сохранённой версии.")
            return {**status,"status":"stale","message":"Проект изменился или удалён. Соберите 3D-модель заново."}
        if status["status"]=="ready":
            try:result=self._cached_result(row)
            except (ValueError,OSError,KeyError,TypeError):
                self._update(identifier,status="failed",message="Файл сборки отсутствует или повреждён. Запустите сборку заново.")
                return {**status,"status":"failed","message":"Файл сборки отсутствует или повреждён."}
            status["result"]=result
        return status

    def export(self,identifier,*,nodes=False):
        status=self.status(identifier)
        if status["status"]!="ready":
            from .api.server import HttpError
            raise HttpError(409,"room_assembly_pending",status["message"])
        filename="nodes.step" if nodes else "room.step"
        content=(self.directory/identifier/filename).read_bytes()
        expected=status["result"]["export_modes"]["nodes" if nodes else "complete"]["sha256"]
        if hashlib.sha256(content).hexdigest()!=expected:
            self._update(identifier,status="failed",message="Повреждён STEP помещения; запустите сборку заново.")
            raise ValueError("Повреждён STEP помещения; запустите сборку заново")
        # Recheck after I/O; a save cannot cause an old assembly to be labelled
        # as the current room while its download is being prepared.
        if not self._current(self._row(identifier)):
            from .api.server import HttpError
            raise HttpError(409,"stale_room_assembly","Проект изменился; старый STEP не экспортирован")
        return content

    def _run(self):
        while not self._stop.is_set():
            identifier=self._queue.get()
            if identifier is None:return
            try:self._build(identifier)
            except Exception as error:
                logging.getLogger("interior_rd.room_assembly").exception("Room assembly failed")
                if not self._stop.is_set():self._update(identifier,status="failed",message=str(error) or "Не удалось собрать отделку помещения")
            finally:self._queue.task_done()

    def _trim_cache(self,keep_id):
        """Only disposable generated files; immutable project history stays."""
        with self._connection() as connection:
            rows=connection.execute("SELECT * FROM room_jobs ORDER BY updated_at DESC").fetchall()
        groups={};count=0;size=0
        for row in sorted(rows,key=lambda item:item["id"]!=keep_id):
            status=json.loads(row["status_json"])["status"]
            if status in ACTIVE:continue
            folder=self.directory/row["id"]
            files=[folder/name for name in ("room.step","nodes.step","result.json")]
            files.extend(path for path in folder.glob("fitted-*.json")
                         if re.fullmatch(r"fitted-[1-9][0-9]*\.json",path.name))
            present=[path for path in files if path.is_file()]
            if not present:continue
            amount=sum(path.stat().st_size for path in present)
            group=(row["project_id"],row["space_id"])
            retain=row["id"]==keep_id or (groups.get(group,0)<2 and count<MAX_CACHE_JOBS and size+amount<=MAX_CACHE_BYTES)
            if retain:
                groups[group]=groups.get(group,0)+1;count+=1;size+=amount
            else:
                for path in present:path.unlink()
                if status=="ready":self._update(row["id"],status="failed",message="Устаревший 3D-кэш очищен. При необходимости соберите помещение заново.")

    def _build(self,identifier):
        from .room_assembly_worker import run_room_build
        row=self._row(identifier);requested=False
        started=perf_counter();source=None;result=None;last_checkpoint=None
        while self._current(row):
            if source is None:source=self.repository.get(row["project_id"])
            engineering=self.assembly_jobs.status(source)
            checkpoint=(engineering.get("job_id"),engineering.get("status"),engineering.get("completed"),engineering.get("updated_at"))
            # A native wall can take minutes. Do not deserialize/decompress all
            # earlier walls every three seconds while that checkpoint is unchanged.
            if result is None or checkpoint!=last_checkpoint:
                project=self.assembly_jobs.overlay(source)
                result,records=prepare_room(project,row["space_id"])
                last_checkpoint=checkpoint
            if not result.get("missing"):break
            if engineering["status"] in {"partial","failed"}:
                labels=", ".join(item["label"] for item in result["missing"])
                explanations=[item.get("message","") for item in engineering.get("outcomes",[]) if item.get("status") in {"failed","error"}]
                raise ValueError("Для комнаты не готовы стены: "+labels+". "+"; ".join(explanations[:3])+" Исправьте раскладку и повторите расчёт стен.")
            if engineering["status"] not in {"queued","running"} and not requested:
                self.assembly_jobs.enqueue(source,force=engineering["status"]=="ready")
                requested=True
            elif engineering["status"] not in {"queued","running"} and requested:
                raise ValueError("Расчёт стен завершён без всех сторон помещения. Пересчитайте монтаж.")
            self._update(identifier,status="waiting_for_walls",message=f"Ожидаем объёмные стены: осталось {len(result['missing'])}. Сохранение проекта не заблокировано.",progress={"completed":0,"total":3},engineering={"completed":engineering.get("completed",0),"total":engineering.get("total",0)})
            if self._stop.wait(3):return
        else:
            self._update(identifier,status="stale",message="Проект изменился; эта сборка остановлена.");return
        unresolved=result.get("joint_setting_out",{}).get("unresolved",[])
        if unresolved:
            raise ValueError("Не удалось согласовать стыки помещения: "+"; ".join(item["reason"] for item in unresolved[:5]))
        prepared=perf_counter()
        if result.get("joints") or any(wall.get("rebuild") for wall in result.get("walls", [])):
            from .room_fitted_walls import rebuild_room_walls, _source_bytes
            from .room_corner_parts import required_source_ids,corner_spec
            for joint in result["joints"]:
                corner_spec(joint)
            def progress(index,total,label,reused):
                self._update(identifier,status="running",message=f"Согласуем углы и перестраиваем сторону {index+1} из {total}: {label}",
                             progress={"completed":index,"total":total+2})
            while self._current(row):
                try:
                    fitted=rebuild_room_walls(project,result,records,self.assembly_jobs.model_library,
                                             self.assembly_jobs.source_store,directory=self.directory/identifier,
                                             progress=progress,current=lambda:self._current(row),
                                             geometry_cache=self.assembly_jobs.wall_geometry_cache,checkpoint_lock=self._lock)
                    break
                except StepGeometryError as error:
                    if error.code=="STEP_CANCELLED":
                        if not self._stop.is_set():self._update(identifier,status="stale",message="Проект изменился; перестроение стыков остановлено.")
                        return
                    if error.code!="STEP_BUSY":raise
                    if self._stop.wait(2):return
            else:fitted=None
            if fitted is None:
                self._update(identifier,status="stale",message="Проект изменился; перестроение стыков остановлено.");return
            walls,records=fitted
            result=refresh_room_manifest(result,walls,records)
            source_ids={key for joint in result["joints"] for key in required_source_ids(joint)}
            corner_sources={key:_source_bytes(key,self.assembly_jobs.source_store,self.assembly_jobs.model_library)
                            for key in source_ids}
        else:corner_sources={}
        work_total=len(records)+2 if result.get("joints") else 3
        self._update(identifier,status="running",message="Собираем согласованные стены и угловые узлы; проверяем все соединения и STEP.",
                     progress={"completed":len(records) if result.get("joints") else 1,"total":work_total})
        fitted_at=perf_counter()
        while self._current(row):
            try:
                built=run_room_build(result,records,corner_sources=corner_sources,cancelled=lambda:not self._current(row))
                break
            except StepGeometryError as error:
                if error.code=="STEP_CANCELLED":
                    if not self._stop.is_set():self._update(identifier,status="stale",message="Проект изменился; эта сборка остановлена.")
                    return
                if error.code!="STEP_BUSY":raise
                self._update(identifier,message="Ожидаем свободный обработчик STEP. Проект остаётся доступен.")
                if self._stop.wait(2):return
        else:
            self._update(identifier,status="stale",message="Проект изменился; эта сборка остановлена.");return
        if not self._current(row):
            self._update(identifier,status="stale",message="Проект изменился во время расчёта. Старые результаты не применены.");return
        if not result.get("joints") and built["body_count"]!=result["body_count"]:
            raise ValueError("STEP помещения изменил количество деталей")
        cross=built["checks"]
        result["project_id"]=row["project_id"]
        result["policy"]=POLICY
        result["wire_preview"]=built["wire_preview"]
        if result.get("joints"):
            if not built.get("bodies") or len(built["bodies"])!=built["body_count"]:
                raise ValueError("Согласованная сборка не содержит проверенного состава деталей")
            proof=built.get("joint_checks",{})
            if (not isinstance(proof,dict) or proof.get("status")!="valid"
                    or proof.get("validation_frame")!="exported_step_roundtrip"
                    or proof.get("hardware_clipped_count")!=0
                    or type(proof.get("source_body_count")) is not int
                    or not isinstance(proof.get("replaced_cell_parts"),list)
                    or not isinstance(proof.get("added_parts"),list)
                    or proof["source_body_count"]-len(proof["replaced_cell_parts"])+len(proof["added_parts"])!=built["body_count"]):
                raise ValueError("Согласованные стыки не прошли проверку состава, крепежа и повторно открытого STEP")
            result["bodies"]=built["bodies"]
            result["body_count"]=built["body_count"]
            result["bounds_mm"]=built["bounds_mm"]
        if built["wire_preview"].get("coarsened_definition_count",0):
            result["warnings"].append("Для быстрого просмотра кривые некоторых деталей показаны укрупнёнными полилиниями. Профили и крепёж взяты из STEP; точная геометрия сохранена при экспорте.")
        result["checks"]={"individual_walls":"valid","cross_wall":cross,
                          "geometry_validation":built.get("geometry_validation",{}),
                          "joints":built.get("joint_checks",{}),
                          "room_collisions":cross["status"],"scope":"finish_only_without_substrate"}
        if cross["collisions"]:
            result["warnings"].append(f"Найдены пересечения деталей разных сторон стен: {len(cross['collisions'])}. Они выделены в просмотре; экспорт сохраняет фактическую геометрию без скрытой обрезки деталей.")
        if not cross["complete"]:
            result["warnings"].append("Межстеновая проверка достигла лимита: остальные пары ещё не проверены. Отсутствие других пересечений не подтверждено.")
        if result.get("joints") and (cross["collisions"] or not cross["complete"]):
            raise ValueError("Сопряжения комнаты не прошли полную проверку пересечений; STEP не опубликован")
        result["export_modes"]={
            "complete":{"label":"Отделка комнаты с панелями","excludes":["substrate"],"body_count":built["body_count"],"sha256":built["step_sha256"],"byte_length":len(base64.b64decode(built["step"],validate=True))},
            "nodes":{"label":"Каркас, профили и крепёж без панелей","excludes":["substrate","panel","adhesive"],"body_count":built["node_body_count"],"sha256":built["nodes_step_sha256"],"byte_length":len(base64.b64decode(built["nodes_step"],validate=True))}}
        result["timings_seconds"]={"preparation_and_wait":round(prepared-started,3),
                                  "fitted_walls":round(fitted_at-prepared,3),
                                  "room_and_export":round(perf_counter()-fitted_at,3),
                                  "total":round(perf_counter()-started,3)}
        content=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()
        if len(content)>MAX_RESULT_BYTES:raise ValueError("Просмотр комнаты превышает 32 МиБ")
        # The trash purge takes this same lock: an in-flight worker must not
        # recreate deleted outputs or publish after its row has been removed.
        with self._lock:
            try:latest=self._row(identifier)
            except NotFoundError:return
            if not self._current(latest):return
            folder=self.directory/identifier;folder.mkdir(exist_ok=True)
            for filename,value in (("room.step",base64.b64decode(built["step"],validate=True)),("nodes.step",base64.b64decode(built["nodes_step"],validate=True)),("result.json",content)):
                temporary=folder/(filename+".tmp");temporary.write_bytes(value);temporary.replace(folder/filename)
            if not self._current(latest):
                self._update(identifier,status="stale",message="Проект изменился. Результат оставлен только в устаревшем кэше.");return
            self._update(identifier,status="ready",message=f"3D-сборка готова: {result['wall_count']} сторон стен, {result['body_count']} деталей.",progress={"completed":work_total,"total":work_total})
        self._trim_cache(identifier)
