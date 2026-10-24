"""Durable, version-guarded CAD work, separate from editable project snapshots.

Saving a mounting specification never invokes a CAD worker. Derived geometry
is cached by all of its source inputs and overlaid on reads only for that exact
input signature. Completion cannot advance the user's revision or overwrite a
newer draft. Interrupted jobs resume after a server restart.
"""
from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid

from .domain.models import Project
from .mount_assemblies import digest
from .panel_layout import LAYOUT_VERSION
from .wall_systems import PERIMETER_VERSION
from .storage.repository import NotFoundError

DERIVED_KEYS = ("mount_assemblies", "whole_wall_assemblies")
ACTIVE = {"queued", "running"}
POLICY = "background-mount-assemblies/1.0"


def source_project(project: Project) -> Project:
    """Copy editable inputs without walking or trusting embedded CAD caches."""
    copy = project.model_copy()
    copy.properties = {key: value for key, value in project.properties.items() if key not in DERIVED_KEYS}
    return copy


def input_signature(project: Project) -> str:
    value = source_project(project).model_dump(mode="json")
    for key in ("revision", "created_at", "updated_at"):
        value.pop(key, None)
    # Presentation colour cannot invalidate or enqueue a physical CAD build.
    # Keep it in source_project/persistence; exclude it only from this key.
    value.get("properties", {}).pop("finish_presentation", None)
    value.get("properties", {}).pop("document_preferences", None)
    return digest({"policy": POLICY, "panel_layout": LAYOUT_VERSION,
                   "perimeter": PERIMETER_VERSION, "project": value})


def preserve_framing_settings(project: Project) -> Project:
    """Extract user-edited framing inputs before discarding bulky CAD output."""
    current = source_project(project)
    mounting_value = current.properties.get("wall_mounting", {})
    setting_value = current.properties.get("whole_wall_settings", {})
    assembly_value = project.properties.get("whole_wall_assemblies", {})
    if any(not isinstance(value, dict) for value in (mounting_value, setting_value, assembly_value)):
        raise ValueError("Некорректные настройки или сборки монтажа")
    if any(not isinstance(value.get(key, []), list) for value, key in
           ((mounting_value, "walls"), (setting_value, "walls"), (assembly_value, "items"))):
        raise ValueError("Настройки и сборки монтажа должны содержать списки")
    mounting = mounting_value.get("walls", [])
    for item in [*mounting, *setting_value.get("walls", []), *assembly_value.get("items", [])]:
        if not isinstance(item, dict) or not isinstance(item.get("wall_id"), str) or not isinstance(item.get("face_id"), str):
            raise ValueError("В настройках сборки требуется сторона стены")
    targets = {(item.get("wall_id"), item.get("face_id")) for item in mounting if isinstance(item, dict)}
    settings = { (item.get("wall_id"), item.get("face_id")): item
                for item in setting_value.get("walls", []) if isinstance(item, dict)}
    for item in assembly_value.get("items", []):
        if isinstance(item, dict) and (item.get("wall_id"), item.get("face_id")) in targets:
            settings.setdefault((item["wall_id"], item["face_id"]), {"wall_id": item["wall_id"], "face_id": item["face_id"], "configuration": item.get("configuration", {})})
    if settings:
        current.properties["whole_wall_settings"] = {"schema_version": "whole-wall-settings/1.0",
            "walls": [item for key, item in settings.items() if key in targets]}
    return current


def _now():
    return datetime.now(timezone.utc).isoformat()


class AssemblyJobs:
    def __init__(self, repository, model_library, source_store, *, path=None, start=True):
        self.repository = repository
        self.model_library = model_library
        self.source_store = source_store
        self.path = Path(path or repository.db_path.with_name(repository.db_path.stem + ".assemblies.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._queue = queue.Queue()
        self._thread = None
        self._source_signatures = OrderedDict()
        from .wall_geometry_cache import WallGeometryCache
        self.wall_geometry_cache = WallGeometryCache(self.path.with_suffix(".wall-cache.sqlite3"))
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS assembly_jobs (
                    project_id TEXT PRIMARY KEY, token TEXT NOT NULL,
                    input_signature TEXT NOT NULL, revision INTEGER NOT NULL,
                    seed_revision INTEGER, status_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assembly_results (
                    project_id TEXT NOT NULL, input_signature TEXT NOT NULL,
                    properties_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, input_signature)
                );
                CREATE TABLE IF NOT EXISTS assembly_wall_records (
                    project_id TEXT NOT NULL, input_signature TEXT NOT NULL,
                    wall_id TEXT NOT NULL, face_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, record_json TEXT NOT NULL,
                    PRIMARY KEY(project_id, input_signature, wall_id, face_id),
                    FOREIGN KEY(project_id, input_signature)
                        REFERENCES assembly_results(project_id, input_signature) ON DELETE CASCADE
                );
            """)
        if start:
            self.start()

    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        # A context manager commits but does not close a sqlite connection.
        from contextlib import contextmanager
        @contextmanager
        def opened():
            try:
                with connection:
                    yield connection
            finally:
                connection.close()
        return opened()

    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            with self._connection() as connection:
                rows = connection.execute("SELECT * FROM assembly_jobs").fetchall()
                for row in rows:
                    status = json.loads(row["status_json"])
                    if status["status"] in ACTIVE:
                        status.update(status="queued", message="Расчёт продолжится после перезапуска сервиса.")
                        connection.execute("UPDATE assembly_jobs SET status_json=? WHERE project_id=?",
                                           (json.dumps(status, ensure_ascii=False), row["project_id"]))
                        self._queue.put((row["project_id"], row["token"]))
            self._thread = threading.Thread(target=self._run, name="violet-assembly-jobs", daemon=True)
            self._thread.start()

    def close(self):
        # Native subprocesses are bounded by their own timeouts. Do not block
        # server shutdown for minutes; checkpoints remain queued for restart.
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=.2)

    def _row(self, project_id):
        with self._connection() as connection:
            return connection.execute("SELECT * FROM assembly_jobs WHERE project_id=?", (project_id,)).fetchone()

    def status(self, project):
        signature = input_signature(project)
        row = self._row(project.id)
        if row and row["input_signature"] == signature:
            result = json.loads(row["status_json"])
            result.update(revision=project.revision, job_id=row["token"])
            return result
        return {"status": "not_started", "job_id": None, "input_signature": signature, "revision": project.revision,
                "message": "Сборки ещё не рассчитаны для текущего монтажа.",
                "completed": 0, "total": 0, "ready": 0, "failed": 0, "outcomes": []}

    def enqueue(self, project, *, seed_revision=None, force=False):
        signature = input_signature(project)
        with self._lock:
            row = self._row(project.id)
            if row and row["input_signature"] == signature:
                status = json.loads(row["status_json"])
                if status["status"] in ACTIVE or (not force and status["status"] in {"ready", "partial"}):
                    status.update(revision=project.revision, job_id=row["token"])
                    return status
            token = uuid.uuid4().hex
            status = {"status": "queued", "job_id": token, "input_signature": signature, "revision": project.revision,
                      "message": "Проект сохранён. Узлы и стены рассчитываются в фоне.",
                      "completed": 0, "total": 0, "ready": 0, "failed": 0,
                      "local_ready": 0, "walls_ready": 0, "outcomes": [], "updated_at": _now()}
            with self._connection() as connection:
                connection.execute("INSERT OR REPLACE INTO assembly_jobs VALUES (?,?,?,?,?,?)",
                    (project.id, token, signature, project.revision, seed_revision,
                     json.dumps(status, ensure_ascii=False)))
            self._queue.put((project.id, token))
        return status

    def cancel(self, project_id):
        """Invalidate an in-flight generation when its project enters trash."""
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT status_json FROM assembly_jobs WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                return
            status = json.loads(row["status_json"])
            token = uuid.uuid4().hex
            status.update(status="cancelled", job_id=token, updated_at=_now(), message="Расчёт остановлен: проект перемещён в корзину.")
            connection.execute("UPDATE assembly_jobs SET token=?, status_json=? WHERE project_id=?",
                               (token, json.dumps(status, ensure_ascii=False), project_id))

    def _properties(self, project_id, signature=None):
        with self._connection() as connection:
            # Manifest and per-record rows must belong to the same checkpoint,
            # even when a background wall commits between these SELECTs.
            connection.execute("BEGIN")
            if signature is None:
                row = connection.execute("SELECT input_signature,properties_json FROM assembly_results WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (project_id,)).fetchone()
            else:
                row = connection.execute("SELECT input_signature,properties_json FROM assembly_results WHERE project_id=? AND input_signature=?", (project_id, signature)).fetchone()
            if row is None:
                return None
            value = json.loads(row["properties_json"])
            if value.get("storage_version") != "assembly-results/2":
                return value  # Legacy single-JSON cache remains readable.
            properties = value["properties"]
            if "whole_wall_assemblies" in properties:
                cursor = connection.execute("""SELECT record_json FROM assembly_wall_records
                    WHERE project_id=? AND input_signature=? ORDER BY ordinal, wall_id, face_id""",
                    (project_id, row["input_signature"]))
                properties["whole_wall_assemblies"]["items"] = [json.loads(item["record_json"]) for item in cursor]
            return properties

    def result(self, project):
        signature = input_signature(project)
        with self._lock:
            status = self.status(project)
            return {"revision": project.revision, "input_signature": signature, "job_id": status["job_id"],
                    "properties": self._properties(project.id, signature) or {}, "engineering": status}

    def overlay(self, project, *, historical=False):
        """No stale cached geometry can enter a document or project response."""
        row = self._row(project.id)
        signature = input_signature(project)
        # Legacy untouched snapshots retain their original records until the
        # user requests a recalculation. Afterwards only exact cache is exposed.
        if row is None:
            return project
        current = source_project(project)
        properties = self._properties(project.id, signature)
        if historical and not properties and row["input_signature"] != signature:
            return project
        if properties:
            current.properties.update(properties)
        return current

    def for_documents(self, project, *, historical=False):
        """A partial checkpoint must never masquerade as a completed album."""
        with self._lock:
            status = self.status(project)
            if status["status"] in ACTIVE:
                from .api.server import HttpError
                raise HttpError(409, "assemblies_pending", "Сборки ещё рассчитываются в фоне. Дождитесь завершения перед выпуском альбома.")
            return self.overlay(project, historical=historical)

    def _current(self, project_id, token, signature):
        if self._stop.is_set():
            return False
        row = self._row(project_id)
        if not row or row["token"] != token or row["input_signature"] != signature:
            return False
        try:
            head = self.repository.head(project_id)
            key = (project_id, head["snapshot_sha256"])
            actual = self._source_signatures.get(key)
            if actual is None:
                actual = input_signature(self.repository.get(project_id))
                self._source_signatures[key] = actual
                while len(self._source_signatures) > 16:
                    self._source_signatures.popitem(last=False)
            current = actual == signature
        except NotFoundError:
            current = False
        if not current:
            self._update(project_id, token, status="stale", message="Параметры изменились или проект удалён; этот расчёт больше не применяется.")
        return current

    def _update(self, project_id, token, **values):
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT status_json FROM assembly_jobs WHERE project_id=? AND token=?", (project_id, token)).fetchone()
            if row is None:
                return
            status = json.loads(row["status_json"])
            status.update(values, updated_at=_now())
            connection.execute("UPDATE assembly_jobs SET status_json=? WHERE project_id=? AND token=?",
                               (json.dumps(status, ensure_ascii=False), project_id, token))

    def _checkpoint(self, project, token, properties, *, wall_delta=None, replace_walls=True):
        """Commit only newly completed walls, not the whole growing STEP album.

        Explicit deltas are checked WholeWallRecord instances created by the job.
        The default remains compatible with legacy callers and validates all
        supplied raw records. No flag from an HTTP body selects this boundary.
        """
        signature = input_signature(project)
        if not self._current(project.id, token, signature):
            return False
        from .server_wall_records import ServerWallCollection
        from .whole_wall_assemblies import WholeWallRecord
        if wall_delta is None:
            checked = ServerWallCollection.model_validate(properties.get("whole_wall_assemblies", {"items": []}))
            wall_delta = checked.items
            replace_walls = True
        else:
            wall_delta = tuple(wall_delta)
            if any(not isinstance(item, WholeWallRecord) for item in wall_delta):
                raise TypeError("В контрольную точку передаются только проверенные сборки стен")
        lightweight = {key: value for key, value in properties.items() if key != "whole_wall_assemblies"}
        if "whole_wall_assemblies" in properties:
            lightweight["whole_wall_assemblies"] = {"schema_version": "whole-wall-assemblies/1.0", "items": []}
        serialized = json.dumps({"storage_version": "assembly-results/2", "properties": lightweight}, ensure_ascii=False, separators=(",", ":"))
        encoded_walls = [(item.wall_id, item.face_id, item.model_dump_json()) for item in wall_delta]
        # Guard once more while committing: enqueue() uses the same lock.
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT token FROM assembly_jobs WHERE project_id=?", (project.id,)).fetchone()
            if row is None or row["token"] != token:
                return False
            # UPSERT, not REPLACE: REPLACE would cascade-delete unchanged walls.
            connection.execute("""INSERT INTO assembly_results VALUES (?,?,?,?)
                ON CONFLICT(project_id,input_signature) DO UPDATE SET
                properties_json=excluded.properties_json,updated_at=excluded.updated_at""",
                (project.id, signature, serialized, _now()))
            if replace_walls:
                connection.execute("DELETE FROM assembly_wall_records WHERE project_id=? AND input_signature=?", (project.id, signature))
            first = connection.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM assembly_wall_records WHERE project_id=? AND input_signature=?", (project.id, signature)).fetchone()[0]
            for index, (wall_id, face_id, record_json) in enumerate(encoded_walls):
                connection.execute("""INSERT INTO assembly_wall_records VALUES (?,?,?,?,?,?)
                    ON CONFLICT(project_id,input_signature,wall_id,face_id) DO UPDATE SET record_json=excluded.record_json""",
                    (project.id, signature, wall_id, face_id, first+index, record_json))
            # Derived cache is disposable, unlike immutable source revisions.
            # Keep the current input and one recent reusable set, not a 30-MiB
            # snapshot for every edit/name change forever.
            connection.execute("""DELETE FROM assembly_results WHERE project_id=? AND input_signature NOT IN (
                SELECT input_signature FROM assembly_results WHERE project_id=? ORDER BY updated_at DESC LIMIT 2
            )""", (project.id, project.id))
        return True

    def _run(self):
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                return
            project_id, token = item
            try:
                self._build(project_id, token)
            except Exception as error:
                logging.getLogger("interior_rd.assemblies").exception("Background assembly failed for %s", project_id)
                if not self._stop.is_set():
                    self._update(project_id, token, status="failed", message=str(error) or "Не удалось завершить расчёт сборок.")
            finally:
                self._queue.task_done()

    def _build(self, project_id, token):
        started, started_at = time.monotonic(), _now()
        from .api.mount_assemblies import _descriptors, _build_item, current_assemblies, GeometryResultCache
        from .api.whole_wall import build_wall
        from .server_node_records import merge_server_node
        from .whole_wall_assemblies import validate_wall_settings, WholeWallRecord
        from .server_wall_records import merge_server_records, create_wall_planner
        from .wall_mount import validate_wall_mounting

        row = self._row(project_id)
        if row is None or row["token"] != token:
            return
        signature = row["input_signature"]
        if not self._current(project_id, token, signature):
            self._update(project_id, token, status="stale", message="Параметры изменились; этот расчёт больше не применяется.")
            return
        self._update(project_id, token, status="running", started_at=started_at,
                     message="Проверяем параметры и ранее рассчитанные сборки.")
        original = self.repository.get(project_id)
        project = source_project(original).model_copy(deep=True)
        seed = {key: original.properties[key] for key in DERIVED_KEYS if key in original.properties}
        if row["seed_revision"]:
            try:
                previous = self.repository.get(project_id, revision=row["seed_revision"])
                seed.update({key: previous.properties[key] for key in DERIVED_KEYS if key in previous.properties})
            except NotFoundError:
                pass
        # A crash may leave a partial checkpoint. Combine it with the previous
        # revision instead of losing manual framing parameters for unbuilt walls.
        cached = self._properties(project_id) or {}
        for key in DERIVED_KEYS:
            if key in cached:
                identity = ((lambda item: (item.get("wall_id"), item.get("face_id")))
                            if key == "whole_wall_assemblies" else (lambda item: item.get("id")))
                by_id = {identity(item): item for item in seed.get(key, {}).get("items", []) if isinstance(item, dict)}
                by_id.update({identity(item): item for item in cached[key].get("items", []) if isinstance(item, dict)})
                seed[key] = {**cached[key], "items": list(by_id.values())}
        project.properties.update(deepcopy(seed))
        mounting = validate_wall_mounting(project)
        wall_planner = create_wall_planner(project, mounting=mounting)
        descriptors, skipped = _descriptors(project, None) if mounting and mounting.walls else ({}, [])
        existing = current_assemblies(project, descriptors, server_owned=True)
        project.properties["mount_assemblies"] = existing.model_dump(mode="json", by_alias=True)
        # Keep manually chosen framing parameters per surviving wall, not its
        # obsolete solid. The new plan is re-derived from the current system.
        prior_walls = {(item.get("wall_id"), item.get("face_id")): item
                       for item in seed.get("whole_wall_assemblies", {}).get("items", []) if isinstance(item, dict)}
        # Check each durable seed once. Native STEP validation used to be
        # repeated for the entire growing collection several times per wall.
        checked_prior = {}
        for key, value in prior_walls.items():
            try:
                checked = WholeWallRecord.model_validate(value)
                checked_prior[key] = checked
                self.wall_geometry_cache.put(checked)
            except ValueError:
                pass  # A damaged/stale derived record must be rebuilt.
        project.properties["whole_wall_assemblies"] = {"schema_version": "whole-wall-assemblies/1.0", "items": []}
        walls = list(mounting.walls) if mounting else []
        wall_settings = validate_wall_settings(project)
        total = len(descriptors) + len(walls) + len(skipped)
        outcomes = [{"kind": "node", "status": "unsupported", **item} for item in skipped]
        completed = len(skipped)
        ready = local_ready = walls_ready = 0
        wall_records = []
        properties = lambda: {key: project.properties[key] for key in DERIVED_KEYS}
        # Preserve the previous exact-input checkpoint until wall plans have
        # been matched below. A restart during local-node preparation must not
        # discard hundreds of already completed walls from this same input.
        if not self._checkpoint(project, token, properties(), wall_delta=(), replace_walls=False):
            return
        self._update(project_id, token, status="running", total=total, outcomes=outcomes, failed=len(outcomes),
                     completed=completed, started_at=started_at, message="Подготовка подробных разрезов узлов.")
        known = {entry.id for entry in existing.items}
        cache = GeometryResultCache()
        for identifier, item in descriptors.items():
            if not self._current(project_id, token, signature):
                return
            outcome = {"kind": "node", "id": identifier, "label": item["label"]}
            if identifier in known:
                outcome.update(status="ready", reused=True)
            else:
                try:
                    self._update(project_id, token, message="Узел: " + item["label"])
                    built = self._retry_busy(lambda: _build_item(project, existing, item, self.model_library,
                        self.source_store, timeout_seconds=90, geometry_cache=cache, server_owned=True), project_id, token, signature)
                    if built is None:
                        return
                    outcome.update({key: built[key] for key in ("status", "message") if key in built})
                    if built["status"] == "ready":
                        existing = merge_server_node(existing, built["assembly"])
                        project.properties["mount_assemblies"] = existing.model_dump(mode="json", by_alias=True)
                        known.add(identifier)
                except (ValueError, FileNotFoundError) as error:
                    outcome.update(status="failed", message=str(error))
            completed += 1
            if outcome["status"] == "ready":
                local_ready += 1
                ready += 1
                if not outcome.get("reused") and not self._checkpoint(project, token, properties(), wall_delta=(), replace_walls=False):
                    return
            outcomes.append(outcome)
            self._update(project_id, token, completed=completed, ready=ready, local_ready=local_ready, outcomes=outcomes,
                         failed=sum(entry["status"] != "ready" for entry in outcomes))
        from .wall_geometry_cache import geometry_key as physical_key, rebind_record
        # World placement does not change a wall's local solid. Group identical
        # work before dispatch so opposite faces never race to build it twice.
        groups, physical_groups = {}, set()
        for binding in walls:
            previous = prior_walls.get((binding.wall_id, binding.face_id), {})
            configuration = wall_settings.get((binding.wall_id, binding.face_id), previous.get("configuration", {}))
            outcome = {"kind": "wall", "wall_id": binding.wall_id, "face_id": binding.face_id}
            try:
                plan = wall_planner(binding.wall_id, binding.face_id, configuration)
                outcome["label"] = plan.get("wall", {}).get("name", binding.wall_id)
                key = physical_key(plan, [])
                physical_groups.add(key)
                checked = checked_prior.get((binding.wall_id, binding.face_id))
                if checked and digest(checked.plan) == digest(plan):
                    wall_records = merge_server_records(wall_records, checked)
                    outcome.update(status="ready", reused=True, elapsed_seconds=0)
                    outcomes.append(outcome)
                    completed += 1
                    walls_ready += 1
                    ready += 1
                else:
                    groups.setdefault(key, []).append((binding, configuration, plan, outcome))
            except (ValueError, FileNotFoundError) as error:
                outcome.update(status="failed", message=str(error), elapsed_seconds=0)
                outcomes.append(outcome)
                completed += 1
        if wall_records:
            project.properties["whole_wall_assemblies"] = {"schema_version": "whole-wall-assemblies/1.0",
                "items": [item.model_dump(mode="json") for item in wall_records]}
        if not self._checkpoint(project, token, properties(), wall_delta=wall_records):
            return
        self._update(project_id, token, completed=completed, ready=ready, walls_ready=walls_ready, outcomes=outcomes,
                     failed=sum(entry["status"] != "ready" for entry in outcomes), unique_wall_geometries=len(physical_groups))

        immutable_source = source_project(project)
        def calculate(group):
            item_started = time.monotonic()
            binding, configuration, plan, _ = group[0]
            # Any identical checked seed can serve this group, even when the
            # first face in it was not reached by the previous interrupted run.
            checked = next((checked_prior[(item[0].wall_id, item[0].face_id)] for item in group
                            if (item[0].wall_id, item[0].face_id) in checked_prior
                            and digest(checked_prior[(item[0].wall_id, item[0].face_id)].plan) == digest(item[2])), None)
            if checked is not None:
                return rebind_record(checked, plan, checked.source_models), True, time.monotonic()-item_started
            built = self._retry_busy(lambda: build_wall(immutable_source, binding.wall_id, binding.face_id,
                configuration, self.model_library, self.source_store, derived=True, existing_records=(),
                geometry_cache=self.wall_geometry_cache, resolved_plan=plan,
                cancelled=lambda: not self._current(project_id, token, signature)), project_id, token, signature)
            if built is None:
                return None, False, time.monotonic()-item_started
            return WholeWallRecord.model_validate(built["assembly"]), built.get("reused", False), time.monotonic()-item_started

        pending, remaining = {}, iter(groups.values())
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="violet-wall")
        try:
            def dispatch():
                while len(pending) < 2:
                    group = next(remaining, None)
                    if group is None:
                        break
                    pending[executor.submit(calculate, group)] = (group, time.monotonic())
                if pending:
                    names = [group[0][3]["label"] for group, _ in pending.values()]
                    self._update(project_id, token, message="Сборка стен: " + " · ".join(names),
                                 parallel_workers=len(pending), unique_wall_geometries=len(physical_groups))
            dispatch()
            while pending:
                if not self._current(project_id, token, signature):
                    return
                finished, _ = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                for future in finished:
                    group, item_started = pending.pop(future)
                    error = None
                    try:
                        record, reused, elapsed = future.result()
                        if record is None:
                            return
                    except (ValueError, FileNotFoundError) as failure:
                        error = str(failure)
                        record, reused, elapsed = None, False, time.monotonic()-item_started
                    for index, (_, _, plan, outcome) in enumerate(group):
                        if not self._current(project_id, token, signature):
                            return
                        try:
                            if error:
                                raise ValueError(error)
                            target = record if index == 0 else rebind_record(record, plan, record.source_models)
                            wall_records = merge_server_records(wall_records, target)
                            # Existing dictionaries are immutable result snapshots.
                            # Serialize only the newly completed record, once.
                            items = project.properties["whole_wall_assemblies"]["items"]
                            project.properties["whole_wall_assemblies"] = {"schema_version": "whole-wall-assemblies/1.0",
                                "items": [item for item in items if (item["wall_id"], item["face_id"]) != (target.wall_id, target.face_id)] + [target.model_dump(mode="json")]}
                            outcome.update(status="ready", reused=reused or index > 0)
                            walls_ready += 1
                            ready += 1
                            if not self._checkpoint(project, token, properties(), wall_delta=(target,), replace_walls=False):
                                return
                        except (ValueError, FileNotFoundError) as failure:
                            outcome.update(status="failed", message=str(failure), reused_failure=index > 0)
                        outcome["elapsed_seconds"] = round(elapsed if index == 0 else 0, 3)
                        completed += 1
                        outcomes.append(outcome)
                        self._update(project_id, token, completed=completed, ready=ready, walls_ready=walls_ready,
                            outcomes=outcomes, failed=sum(entry["status"] != "ready" for entry in outcomes),
                            elapsed_seconds=round(time.monotonic()-started, 3))
                dispatch()
        finally:
            # Poll callbacks kill native workers on cancellation; no futures
            # beyond the two active groups have been queued.
            executor.shutdown(wait=False, cancel_futures=True)
        if not self._current(project_id, token, signature):
            return
        failed = sum(outcome["status"] != "ready" for outcome in outcomes)
        self._update(project_id, token, status="partial" if failed else "ready", completed=total,
                     ready=ready, failed=failed, local_ready=local_ready, walls_ready=walls_ready,
                     parallel_workers=0,
                     elapsed_seconds=round(time.monotonic()-started, 3),
                     message=(f"Готово: узлов {local_ready}, стен {walls_ready}. " +
                              (f"Не рассчитано вариантов: {failed}; подробности в отчёте." if failed else "Все сборки обновлены.")), outcomes=outcomes)

    def _retry_busy(self, operation, project_id, token, signature):
        # CAD slots are shared with explicit UI requests. Wait without marking a
        # valid recipe unsupported just because another native worker is busy.
        from .api.server import HttpError
        from .step_geometry import StepGeometryError
        while self._current(project_id, token, signature):
            try:
                return operation()
            except (HttpError, StepGeometryError) as error:
                if error.code not in {"cad_busy", "STEP_BUSY"}:
                    raise ValueError(getattr(error, "message", None) or str(error)) from error
                self._update(project_id, token, message="Ожидание свободного обработчика CAD…")
                self._stop.wait(.5)
        return None
