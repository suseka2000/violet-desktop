"""Persistent building jobs: reuse wall BReps, publish small manifests and files.

The database uses the same queue format as room jobs (its space_id column is an
opaque scope key), in a separate database. No aggregate STEP/base64 is sent to
the browser or retained in project snapshots.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
import re
import tempfile
from time import perf_counter
import uuid
from collections import OrderedDict

from .assembly_jobs import input_signature
from .building_assembly import (BUILDING_POLICY, building_targets,
                                prepare_building, refresh_building_manifest)
from .room_assembly_jobs import ACTIVE, RoomAssemblyJobs, now
from .step_geometry import StepGeometryError
from .storage.repository import ConflictError, NotFoundError


MAX_DIAGNOSTIC_BYTES = 64 * 1024


def verification_failure_details(built):
    """Keep failing pairs and inventory totals ahead of successful CAD proofs.

    Thousands of valid fastener/contact reports must never consume the
    diagnostic budget before the cross-wall collision which rejected export.
    This only selects diagnostic evidence; publication checks stay unchanged.
    """
    cross = built.get("checks", {})
    proof = built.get("joint_checks", {}) or {}
    collisions = cross.get("collisions", [])
    joints = proof.get("joints", [])
    invalid = [item for item in joints if item.get("status") not in {"valid", "not_applicable"}]
    return {
        "body_count": built.get("body_count"),
        "verified_body_count": built.get("verified_body_count"),
        "joint_checks": {
            **{key: proof.get(key) for key in ("status", "validation_frame", "source_body_count",
                                               "result_body_count", "hardware_clipped_count")},
            **{key + "_count": len(proof[key]) if isinstance(proof.get(key), list) else None
               for key in ("replaced_cell_parts", "added_parts", "joints")},
        },
        "cross_wall": {
            **{key: value for key, value in cross.items() if not isinstance(value, (dict, list))},
            "collision_count": len(collisions),
            "omitted_collision_count": max(0, len(collisions) - 128),
            "collisions": collisions[:128],
        },
        "invalid_joints": invalid[:16],
        "omitted_invalid_joint_count": max(0, len(invalid) - 16),
        "geometry_validation": built.get("geometry_validation", {}),
        "performance": built.get("performance", {}),
    }


def native_diagnostic(error, *, phase, revision):
    """Bounded evidence only: retain failed IDs/checks, never CAD payloads."""
    remaining, nodes, truncated = MAX_DIAGNOSTIC_BYTES//2, 1024, False
    omitted = {"step", "nodes_step", "content_base64", "wire_preview", "preview", "bodies",
               "definitions", "instances", "points", "vertices", "polylines", "mesh", "shape"}
    def visit(value, depth=0):
        nonlocal remaining, nodes, truncated
        nodes -= 1
        if remaining <= 16 or nodes < 0 or depth > 7:
            truncated = True
            return None
        remaining -= 4  # JSON separators/container overhead, conservatively.
        if isinstance(value, dict):
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 64 or remaining <= 16 or nodes < 0:
                    truncated = True
                    break
                if not isinstance(key, str) or key.casefold() in omitted or key.casefold().endswith(("_base64", "_path")):
                    truncated = True
                    continue
                if len(key) > 128:
                    truncated = True
                    continue
                remaining -= len(key.encode("utf-8"))+4
                result[key] = visit(item, depth+1)
            return result
        if isinstance(value, (list, tuple)):
            result = []
            for index, item in enumerate(value):
                if index >= 128 or remaining <= 16 or nodes < 0:
                    truncated = True
                    break
                result.append(visit(item, depth+1))
            return result
        if isinstance(value, str):
            raw = value.encode("utf-8")
            limit = max(0, min(512, remaining-4))
            if len(raw) > limit:
                truncated = True
                value = raw[:limit].decode("utf-8", errors="ignore")
            remaining -= len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            return value
        if value is None or isinstance(value, bool):
            remaining -= 5
            return value
        if isinstance(value, (int, float)) and abs(value) <= 1e18 and math.isfinite(value):
            remaining -= len(str(value))
            return value
        truncated = True
        return None
    details = visit(error.details)
    result = {"schema": "building-native-diagnostic/1", "code": error.code[:128],
              "phase": phase, "source_revision": revision,
              "details": details, "truncated": truncated}
    # This fallback protects even unusual JSON escaping/key expansion. It is
    # a diagnostic transport bound, never a limit on model size or checking.
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_DIAGNOSTIC_BYTES:
        result.update(details={"omitted": "Диагностика превысила безопасный размер"}, truncated=True)
    return result


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_file(folder, filename, metadata, *, verify=False):
    if (not isinstance(metadata, dict) or type(metadata.get("byte_length")) is not int
            or metadata["byte_length"] < 1 or not isinstance(metadata.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]) is None):
        raise ValueError("Повреждены сведения о файле 3D-здания")
    path = folder / filename
    if folder.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size != metadata["byte_length"]:
        raise ValueError("Файл 3D-здания отсутствует или повреждён")
    if verify and file_digest(path) != metadata["sha256"]:
        raise ValueError("Контрольная сумма файла 3D-здания не совпадает")
    return path


class BuildingAssemblyJobs(RoomAssemblyJobs):
    policy = BUILDING_POLICY
    scope_key = "building_id"
    result_schema = "building-assembly/1.0"
    full_step_filename = "building.step"
    api_prefix = "/api/v1/building-assembly"

    def _message(self, value):
        return value

    def _targets(self, project, identifier):
        return building_targets(project, identifier)

    def _prepare(self, project, identifier):
        return prepare_building(project, identifier)

    def _refresh_manifest(self, result, walls, records):
        return refresh_building_manifest(result, walls, records)

    def __init__(self, repository, assembly_jobs, *, path=None, start=True):
        self._manifests = OrderedDict()
        path = path or repository.db_path.with_name(repository.db_path.stem + ".building-assemblies.sqlite3")
        super().__init__(repository, assembly_jobs, path=path, start=start)

    def _update(self, identifier, **values):
        # Cancellation is terminal, including when the native worker races a
        # final progress message, an exception, or a revision-change check.
        with self._lock:
            try:
                if json.loads(self._row(identifier)["status_json"]).get("status") == "cancelled":
                    return
            except NotFoundError:
                return
            super()._update(identifier, **values)

    def _native_failure(self, row, error, *, phase):
        diagnostic = native_diagnostic(error, phase=phase, revision=row["revision"])
        logging.getLogger("interior_rd.building_assembly").error(
            "Building assembly diagnostic: %s", json.dumps(diagnostic, ensure_ascii=False))
        # Store with the job, not in a temporary native directory. A cancelled,
        # purged or stale job must not be revived while reporting its failure.
        with self._lock:
            if self._current(row):
                self._update(row["id"], diagnostic=diagnostic)

    def _reject_native_result(self, row, message, code, details):
        error = StepGeometryError(message, code=code, details=details)
        self._native_failure(row, error, phase="building_verification")
        raise error

    def enqueue(self, project_id, building_id, expected_revision):
        project = self.repository.get(project_id)
        if project.revision != expected_revision:
            raise ConflictError(project_id, expected_revision, project.revision)
        self._targets(project, building_id)  # Cheap scope validation; no CAD.
        signature = input_signature(project)
        audited = {}
        last_audited_id = None
        while True:
            candidate = None
            # Select/recheck jobs atomically, but never hash large files while
            # holding the queue lock or a database connection. Concurrent starts
            # re-enter this selection after their audit and share the same job.
            with self._lock, self._connection() as connection:
                if last_audited_id is not None:
                    latest = self._row(last_audited_id)
                    # Do not publish an old result or recreate a job cancelled
                    # or removed while its files were being checked.
                    if json.loads(latest["status_json"])["status"] == "cancelled" or not self._current(latest):
                        return self.status(last_audited_id)
                head = self.repository.head(project_id)
                if head["revision"] != expected_revision:
                    raise ConflictError(project_id, expected_revision, head["revision"])
                retry = None
                rows = connection.execute(
                    "SELECT * FROM room_jobs WHERE project_id=? AND space_id=? AND revision=? AND signature=? ORDER BY updated_at DESC",
                    (project_id, building_id, expected_revision, signature)).fetchall()
                for row in rows:
                    status = json.loads(row["status_json"])
                    if status.get("policy") != self.policy:
                        continue
                    if status["status"] in ACTIVE:
                        return self.status(row["id"])
                    if status["status"] == "ready":
                        token = (row["id"], row["updated_at"])
                        if token not in audited:
                            candidate = row
                            break
                        if audited[token]:
                            return self.status(row["id"])
                        continue
                    if status["status"] == "failed" and retry is None and not (self.directory / row["id"]).is_symlink():
                        retry = row
                if candidate is None:
                    # Explicit retry keeps independently verified fitted-wall
                    # checkpoints. No CAD is opened by enqueue.
                    identifier = retry["id"] if retry is not None else uuid.uuid4().hex
                    status = {"job_id": identifier, "project_id": project_id, self.scope_key: building_id,
                              "source_revision": expected_revision, "policy": self.policy, "status": "queued",
                              "message": self._message("Продолжаем сборку здания. Сохранённые стены будут проверены и использованы повторно."
                                          if retry is not None else "Собираем отделку здания в фоне. Готовые стены будут использованы повторно."),
                              "progress": {"completed": 0, "total": 3}, "updated_at": now()}
                    if retry is not None:
                        connection.execute("UPDATE room_jobs SET status_json=?,updated_at=? WHERE id=?",
                                           (json.dumps(status, ensure_ascii=False), status["updated_at"], identifier))
                        self._manifests.pop(identifier, None)
                    else:
                        connection.execute("INSERT INTO room_jobs VALUES (?,?,?,?,?,?,?)",
                                           (identifier, project_id, building_id, expected_revision, signature,
                                            json.dumps(status, ensure_ascii=False), status["updated_at"]))
            if candidate is None:
                self._queue.put(identifier)
                return status
            try:
                self._cached_result(candidate, verify_steps=True)
            except (ValueError, OSError, KeyError, TypeError):
                audited[(candidate["id"], candidate["updated_at"])] = False
            else:
                audited[(candidate["id"], candidate["updated_at"])] = True
            last_audited_id = candidate["id"]

    def cancel(self, identifier, expected_revision):
        with self._lock:
            row = self._row(identifier)
            if row["revision"] != expected_revision:
                raise ConflictError(row["project_id"], expected_revision, row["revision"])
            status = json.loads(row["status_json"])
            if status["status"] in ACTIVE:
                self._update(identifier, status="cancelled", message="Сборка остановлена. Готовые стены сохранены для повторного запуска.")
        return self.status(identifier)

    def _cached_result(self, row, *, verify_steps=False):
        folder = self.directory / row["id"]
        path = folder / "result.json"
        if folder.is_symlink() or path.is_symlink() or not path.is_file():
            raise ValueError("Нет сохранённой сборки здания")
        stat = path.stat()
        stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._manifests.get(row["id"])
        if cached and cached[0] == stamp:
            result, chunk_index = cached[1:]
        else:
            with path.open() as stream:
                result = json.load(stream)
            chunk_index = None
        if (not isinstance(result, dict) or result.get("schema_version") != self.result_schema
                or result.get("project_id") != row["project_id"] or result.get(self.scope_key) != row["space_id"]
                or result.get("source_signature") != row["signature"] or result.get("source_revision") != row["revision"]
                or result.get("policy") != self.policy or not isinstance(result.get("export_modes"), dict)
                or not isinstance(result.get("preview_chunks"), list)):
            raise ValueError("3D-здание относится к другой сохранённой версии проекта")
        for filename, mode in ((self.full_step_filename, "complete"), ("nodes.step", "nodes")):
            checked_file(folder, filename, result["export_modes"].get(mode), verify=verify_steps)
        if chunk_index is None:
            chunk_index = {}
            for chunk in result["preview_chunks"]:
                if (not isinstance(chunk, dict) or not isinstance(chunk.get("id"), str)
                        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chunk["id"]) is None or chunk["id"] in chunk_index):
                    raise ValueError("Повреждён список фрагментов просмотра здания")
                chunk_index[chunk["id"]] = chunk
            with self._lock:
                self._manifests[row["id"]] = (stamp, result, chunk_index)
                while len(self._manifests) > 8:
                    self._manifests.popitem(last=False)
        # Audit every fragment only on explicit cache reuse. Fetching N chunks
        # must not stat/read the other N fragments on every request (O(N²)).
        if verify_steps:
            for chunk in result["preview_chunks"]:
                checked_file(folder, f"preview-{chunk['id']}.json", chunk, verify=True)
        return result

    def status(self, identifier):
        row = self._row(identifier)
        status = json.loads(row["status_json"])
        if status["status"] == "cancelled":
            return status
        if not self._current(row):
            message = "Проект изменился или удалён. Соберите здание для текущей сохранённой версии."
            if not self._stop.is_set():
                self._update(identifier, status="stale", message=message)
            return {**status, "status": "stale", "message": message}
        if status["status"] == "ready":
            try:
                status["result"] = self._cached_result(row)
            except (ValueError, OSError, KeyError, TypeError):
                message = "Файл сборки здания отсутствует или повреждён. Запустите сборку заново."
                self._update(identifier, status="failed", message=message)
                return {**status, "status": "failed", "message": message}
        return status

    def _ready(self, identifier):
        status = self.status(identifier)
        if status["status"] != "ready":
            from .api.server import HttpError
            raise HttpError(409, "building_assembly_pending", status["message"])
        return status["result"]

    def preview(self, identifier, chunk_id):
        result = self._ready(identifier)
        with self._lock:
            cached = self._manifests.get(identifier)
        chunk = cached[2].get(chunk_id) if cached else next((item for item in result["preview_chunks"] if item["id"] == chunk_id), None)
        if chunk is None:
            raise NotFoundError("Фрагмент 3D-здания не найден")
        path = checked_file(self.directory / identifier, f"preview-{chunk_id}.json", chunk, verify=True)
        with path.open() as stream:
            preview = json.load(stream)
        if not self._current(self._row(identifier)):
            raise ValueError("Проект изменился во время загрузки 3D-здания")
        return {"id": chunk_id, "floor_id": chunk.get("floor_id"),
                "floor_label": chunk.get("floor_label"), "wire_preview": preview}

    def open_export(self, identifier, *, nodes=False):
        """An open descriptor lets HTTP stream even a multi-GB STEP, not a Blob."""
        result = self._ready(identifier)
        mode = "nodes" if nodes else "complete"
        filename = "nodes.step" if nodes else self.full_step_filename
        path = checked_file(self.directory / identifier, filename, result["export_modes"][mode])
        stream = path.open("rb")
        try:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            if digest.hexdigest() != result["export_modes"][mode]["sha256"]:
                self._update(identifier, status="failed", message="Повреждён STEP здания; соберите его заново.")
                raise ValueError("Повреждён STEP здания")
            if not self._current(self._row(identifier)):
                from .api.server import HttpError
                raise HttpError(409, "stale_building_assembly", "Проект изменился; старый STEP не экспортирован")
            stream.seek(0)
            return stream, result["export_modes"][mode]["byte_length"]
        except BaseException:
            stream.close()
            raise

    def _build(self, identifier):
        from .building_assembly_worker import run_building_build
        from .room_fitted_walls import rebuild_room_walls, _source_bytes
        from .room_corner_parts import required_source_ids, corner_spec
        row = self._row(identifier)
        requested = False
        started = perf_counter()
        source = result = last_checkpoint = None
        if self._current(row):
            self._update(identifier, status="running", message="Проверяем готовые стены и состав этажей; геометрию пока не пересчитываем.",
                         progress={"completed": 0, "total": 3})
        while self._current(row):
            if source is None:
                source = self.repository.get(row["project_id"])
            engineering = self.assembly_jobs.status(source)
            if engineering["status"] in {"queued", "running"}:
                # Do not hydrate and validate every completed STEP again on
                # every progress tick. The worker checkpoint is already durable.
                self._update(identifier, status="waiting_for_walls",
                             message="Ожидаем расчёт стен. Готовая геометрия сохраняется и не пересчитывается.",
                             engineering={"completed": engineering.get("completed", 0), "total": engineering.get("total", 0)})
                if self._stop.wait(3):
                    return
                continue
            checkpoint = tuple(engineering.get(key) for key in ("job_id", "status", "completed", "updated_at"))
            if result is None or checkpoint != last_checkpoint:
                project = self.assembly_jobs.overlay(source)
                result, records = self._prepare(project, row["space_id"])
                last_checkpoint = checkpoint
            if not result.get("missing"):
                break
            missing = result["missing"]
            if engineering["status"] in {"partial", "failed"}:
                names = ", ".join(item.get("label", item["wall_id"]) for item in missing[:8])
                reasons = [item.get("message", "") for item in engineering.get("outcomes", []) if item.get("status") in {"failed", "error"}]
                raise ValueError(f"Для здания не готовы {len(missing)} сторон стен: {names}. " + "; ".join(reasons[:3]))
            if engineering["status"] not in {"queued", "running"}:
                if requested:
                    raise ValueError("Расчёт стен завершён без всех сторон здания. Пересчитайте монтаж.")
                self.assembly_jobs.enqueue(source, force=engineering["status"] == "ready")
                requested = True
            self._update(identifier, status="waiting_for_walls",
                         message=f"Ожидаем готовые стены: осталось {len(missing)}. Уже готовые стороны не пересчитываются.",
                         engineering={"completed": engineering.get("completed", 0), "total": engineering.get("total", 0)})
            if self._stop.wait(3):
                return
        else:
            self._update(identifier, status="stale", message="Проект изменился; сборка остановлена.")
            return
        unresolved = result.get("joint_setting_out", {}).get("unresolved", [])
        if unresolved:
            raise ValueError("Не удалось согласовать стыки здания: " + "; ".join(item["reason"] for item in unresolved[:5]))
        prepared = perf_counter()
        folder = self.directory / identifier
        with self._lock:
            if not self._current(row):
                return
            folder.mkdir(exist_ok=True)
        corner_sources = {}
        wall_reuse = {"shared_checkpoint_hits": 0, "index_passes": 0, "indexed_files": 0}
        if result.get("joints") or any(wall.get("rebuild") for wall in result.get("walls", [])):
            from .fitted_wall_reuse import CompletedFittedWalls, FittedCheckpointContext
            shared_checkpoints = CompletedFittedWalls.for_snapshot(
                self.repository.db_path, row["project_id"], row["revision"], row["signature"], exclude_job=identifier)
            checkpoint_context = FittedCheckpointContext(folder, row["project_id"], row["revision"], row["signature"])
            for joint in result["joints"]:
                corner_spec(joint)
            def progress(index, total, label, reused):
                self._update(identifier, status="running", message=f"Сопряжения: {index + 1} из {total} · {label}" + (" · из кэша" if reused else ""),
                             progress={"completed": index, "total": total + 2})
            while self._current(row):
                try:
                    fitted = rebuild_room_walls(
                        project, result, records, self.assembly_jobs.model_library, self.assembly_jobs.source_store,
                        directory=folder, progress=progress, current=lambda: self._current(row),
                        geometry_cache=self.assembly_jobs.wall_geometry_cache, checkpoint_lock=self._lock,
                        shared_checkpoints=shared_checkpoints, checkpoint_context=checkpoint_context)
                    if fitted is None:
                        return
                    walls, records = fitted
                    break
                except StepGeometryError as error:
                    if error.code == "STEP_CANCELLED":
                        return
                    if error.code != "STEP_BUSY":
                        self._native_failure(row, error, phase="fitted_walls")
                        raise
                    if self._stop.wait(2):
                        return
            else:
                return
            result = self._refresh_manifest(result, walls, records)
            wall_reuse.update(shared_checkpoint_hits=shared_checkpoints.hits,
                              index_passes=shared_checkpoints.index_passes, indexed_files=shared_checkpoints.indexed_files)
            source_ids = {key for joint in result["joints"] for key in required_source_ids(joint)}
            corner_sources = {key: _source_bytes(key, self.assembly_jobs.source_store, self.assembly_jobs.model_library) for key in source_ids}
        fitted_at = perf_counter()
        self._update(identifier, status="running", message="Объединяем этажи, проверяем пересечения и записываем STEP здания.",
                     progress={"completed": 2, "total": 3})
        # Only a native worker writes into this private staging folder. Never
        # publish partial files; cancelling keeps reusable fitted-wall cache.
        with tempfile.TemporaryDirectory(prefix="building-stage-", dir=self.directory) as temporary:
            staging = Path(temporary)
            while self._current(row):
                try:
                    built = run_building_build(result, records, corner_sources=corner_sources,
                                               output_directory=staging, cancelled=lambda: not self._current(row))
                    break
                except StepGeometryError as error:
                    if error.code == "STEP_CANCELLED":
                        return
                    if error.code != "STEP_BUSY":
                        self._native_failure(row, error, phase="building_native")
                        raise
                    self._update(identifier, message="Ожидаем свободный обработчик STEP. Редактор доступен.")
                    if self._stop.wait(2):
                        return
            else:
                return
            # A successful STEP roundtrip proves only the exported occurrences.
            # Without joints there is no legitimate addition/removal: also
            # reconcile with the refreshed source inventory before publication.
            if (not result.get("joints") and "body_count" in result
                    and built.get("body_count") != result["body_count"]):
                self._reject_native_result(row, "STEP здания изменил количество деталей; STEP не опубликован",
                    "BUILDING_INVENTORY_INVALID", {"expected_body_count": result["body_count"], "actual_body_count": built.get("body_count")})
            cross = built["checks"]
            if result.get("joints"):
                proof = built.get("joint_checks", {})
                if (proof.get("status") != "valid" or proof.get("validation_frame") != "exported_step_roundtrip"
                        or proof.get("hardware_clipped_count") != 0
                        or type(proof.get("source_body_count")) is not int
                        or not isinstance(proof.get("replaced_cell_parts"), list)
                        or not isinstance(proof.get("added_parts"), list)
                        or proof["source_body_count"] - len(proof["replaced_cell_parts"]) + len(proof["added_parts"]) != built["body_count"]
                        or cross.get("collisions") or not cross.get("complete")):
                    self._reject_native_result(row, "Сопряжения здания не прошли полную проверку; STEP не опубликован",
                        "BUILDING_JOINT_CHECKS_INVALID", verification_failure_details(built))
            validation = built.get("geometry_validation", {})
            if (not built.get("body_count") or built.get("verified_body_count") != built["body_count"]
                    or validation.get("world_bounds") != "every_exported_instance"):
                self._reject_native_result(row, "STEP здания не содержит проверенного состава деталей",
                    "BUILDING_INVENTORY_INVALID", verification_failure_details(built))
            if (not cross.get("complete") or cross.get("status") != "valid" or cross.get("collisions")
                    or cross.get("validation_frame") != "exported_step_roundtrip"):
                self._reject_native_result(row, "Сборка здания не прошла полную проверку пересечений; STEP не опубликован",
                    "BUILDING_COLLISIONS_INVALID", verification_failure_details(built))
            manifest = {key: result[key] for key in ("id", self.scope_key, "label", "floors", "joints", "wall_count", "coverage") if key in result}
            manifest.update(schema_version=self.result_schema, policy=self.policy, project_id=row["project_id"],
                            source_revision=row["revision"], source_signature=row["signature"],
                            body_count=built["body_count"], bounds_mm=built["bounds_mm"],
                            source_reuse=built.get("source_reuse", {}),
                            wall_manufacture_reuse=wall_reuse,
                            native_performance=built.get("performance", {}),
                            checks={"individual_walls": "valid", "building_collisions": cross["status"], "cross_wall": cross,
                                    "geometry_validation": built.get("geometry_validation", {}), "joints": built.get("joint_checks", {})},
                            warnings=list(result.get("warnings", [])), preview_chunks=[], export_modes={})
            if cross.get("collisions"):
                manifest["warnings"].append(f"Найдены пересечения: {len(cross['collisions'])}. Экспорт сохраняет фактическую геометрию.")
            if not cross.get("complete"):
                manifest["warnings"].append("Проверены не все пары деталей; отсутствие других пересечений не подтверждено.")
            outputs = []
            def stage_file(path_value, name, metadata):
                path = Path(path_value)
                if path.parent.resolve() != staging.resolve() or path.is_symlink():
                    raise ValueError("Рабочий файл находится вне каталога сборки")
                checked_file(staging, path.name, metadata, verify=True)
                outputs.append((path, folder / name))
            for mode, prefix, filename, label in (("complete", "", self.full_step_filename, "Отделка с панелями"),
                                                  ("nodes", "nodes_", "nodes.step", "Каркас, узлы и крепёж без панелей")):
                metadata = {"sha256": built[prefix + "step_sha256"], "byte_length": built[prefix + "step_byte_length"],
                            "body_count": built["body_count" if mode == "complete" else "node_body_count"], "label": label,
                            "excludes": ["substrate"] if mode == "complete" else ["substrate", "panel", "adhesive"]}
                stage_file(built[prefix + "step_path"], filename, metadata)
                manifest["export_modes"][mode] = metadata
            floor_names = {item["id"]: item.get("name", item["id"]) for item in manifest.get("floors", [])}
            chunk_ids = set()
            for chunk in built.get("preview_chunks", []):
                key = chunk.get("id")
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key) or key in chunk_ids:
                    raise ValueError("Неоднозначный фрагмент просмотра здания")
                chunk_ids.add(key)
                metadata = {k: v for k, v in chunk.items() if k != "path"}
                metadata.update(url=f"{self.api_prefix}/{identifier}/preview/{key}",
                                floor_label=floor_names.get(chunk.get("floor_id"), "Этаж"),
                                body_count=chunk.get("instance_count", 0))
                stage_file(chunk["path"], f"preview-{key}.json", metadata)
                manifest["preview_chunks"].append(metadata)
            if not manifest["preview_chunks"]:
                raise ValueError("Сборка здания не содержит фрагментов просмотра")
            manifest["timings_seconds"] = {"preparation_and_wait": round(prepared - started, 3),
                                           "fitted_walls": round(fitted_at - prepared, 3),
                                           "building_and_export": round(perf_counter() - fitted_at, 3),
                                           "total": round(perf_counter() - started, 3)}
            with self._lock:
                if not self._current(row):
                    return
                folder.mkdir(exist_ok=True)
                for source_path, destination in outputs:
                    source_path.replace(destination)
                content_path = folder / "result.json.tmp"
                with content_path.open("w") as stream:
                    json.dump(manifest, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                content_path.replace(folder / "result.json")
                self._update(identifier, status="ready", message=f"3D-здание готово: {manifest['wall_count']} сторон стен, {manifest['body_count']} деталей.",
                             progress={"completed": 3, "total": 3})
        self._trim_cache(identifier)

    def _trim_cache(self, keep_id):
        # Never reject the current building because it exceeds a cache quota.
        # Keep one recent generated revision per building; snapshots are intact.
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT * FROM room_jobs ORDER BY updated_at DESC").fetchall()
            seen = set()
            for row in sorted(rows, key=lambda item: item["id"] != keep_id):
                if json.loads(row["status_json"])["status"] in ACTIVE:
                    continue
                key = row["project_id"], row["space_id"]
                if key not in seen:
                    seen.add(key)
                    continue
                folder = self.directory / row["id"]
                if folder.is_symlink() or not folder.is_dir():
                    continue
                for path in folder.iterdir():
                    if known_output(path.name) and not path.is_symlink() and path.is_file():
                        path.unlink()
                self._update(row["id"], status="stale", message="Старая 3D-сборка заменена новой версией здания.")


def known_output(name):
    return name in {"building.step", "room.step", "nodes.step", "result.json", "result.json.tmp", "bodies.json"} or bool(
        re.fullmatch(r"preview-[A-Za-z0-9_-]{1,128}\.json|fitted-[1-9][0-9]*(?:\.context)?\.(?:json|tmp)", name))
