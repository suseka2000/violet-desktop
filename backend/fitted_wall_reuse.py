"""Cross-scope recovery of exact, verified fitted-wall checkpoints.

Only completed jobs of the same saved project/revision/input signature qualify.
The file index is built once per assembly, not scanned again for every wall.
Each hit is parsed through the current full record/STEP validation and rebound
to the exact physical plan. The caller still performs complete native assembly
and exported-STEP checks; this only avoids repeating wall manufacture.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

from .wall_geometry_cache import geometry_key, rebind_record, kernel_signature
from .whole_wall_assemblies import WholeWallRecord


class FittedCheckpointContext:
    """Whitelist exactly the records generated/verified with this kernel.

    A folder stamp alone is insufficient: retrying under a new kernel can
    leave old fitted-N files beside newly generated files. Bind every proof
    to the record fingerprint and exact STEP bytes. Legacy unmarked records
    are deliberately not migrated by guessing which old kernel produced them.
    """
    def __init__(self, folder, project_id, revision, signature):
        self.folder = Path(folder)
        self.identity = {"schema": "fitted-wall-context/1", "project_id": project_id,
                         "source_revision": revision, "source_signature": signature,
                         "kernel_signature": kernel_signature()}
        self.records = {}
        if self.folder.is_symlink():
            return
        for path in self.folder.glob("fitted-*.context.json"):
            try:
                if path.is_symlink():
                    continue
                value = json.loads(path.read_text())
                name = value.get("filename")
                proof = value.get("record")
                if (isinstance(name, str) and re.fullmatch(r"fitted-[1-9][0-9]*\.json", name)
                        and path == self.metadata_path(name)
                        and all(value.get(key) == item for key, item in self.identity.items())
                        and isinstance(proof, dict)
                        and all(isinstance(proof.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", proof[key])
                                for key in ("fingerprint", "step_sha256"))):
                    self.records[name] = proof
            except (OSError, ValueError, TypeError, AttributeError):
                continue

    def metadata_path(self, filename):
        return self.folder / (Path(filename).stem + ".context.json")

    def accepts(self, filename, record):
        return (self.identity["kernel_signature"] == kernel_signature()
                and self.records.get(filename) == {"fingerprint": record.fingerprint,
                                                  "step_sha256": record.assembled_step.source_id})

    def remember(self, filename, record, *, verified_job_id=None):
        if (self.identity["kernel_signature"] != kernel_signature()
                or re.fullmatch(r"fitted-[1-9][0-9]*\.json", filename) is None):
            raise ValueError("Изменилась версия ядра сохранённых стен")
        self.records[filename] = {"fingerprint": record.fingerprint, "step_sha256": record.assembled_step.source_id}
        self.folder.mkdir(parents=True, exist_ok=True)
        # One small sidecar per new record, never rewrite the growing index
        # after every wall (which would be quadratic for a large building).
        path = self.metadata_path(filename)
        temporary = path.with_suffix(".tmp")
        if path.is_symlink() or temporary.is_symlink() or self.folder.is_symlink():
            raise ValueError("Некорректный каталог сохранённых стен")
        evidence = {}
        if verified_job_id is not None:
            if re.fullmatch(r"[0-9a-f]{32}", verified_job_id) is None:
                raise ValueError("Некорректный идентификатор проверенной сборки")
            evidence["verified_job_id"] = verified_job_id
        temporary.write_text(json.dumps({**self.identity, "filename": filename, "record": self.records[filename], **evidence}, sort_keys=True))
        temporary.replace(path)


class CompletedFittedWalls:
    def __init__(self, contexts):
        self.contexts = tuple(contexts)
        self.folders = tuple(context.folder for context in self.contexts)
        self._index = None
        self.index_passes = 0
        self.indexed_files = 0
        self.hits = 0

    @classmethod
    def for_snapshot(cls, repository_path, project_id, revision, signature, *, exclude_job=None):
        repository_path = Path(repository_path)
        contexts = []
        for suffix in (".room-assemblies.sqlite3", ".building-assemblies.sqlite3"):
            database = repository_path.with_name(repository_path.stem + suffix)
            if not database.is_file() or database.is_symlink():
                continue
            try:
                with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
                    rows = connection.execute(
                        "SELECT id,status_json FROM room_jobs WHERE project_id=? AND revision=? AND signature=? ORDER BY updated_at DESC",
                        (project_id, revision, signature)).fetchall()
            except (OSError, sqlite3.Error):
                continue
            for identifier, raw in rows:
                if identifier == exclude_job or not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
                    continue
                try:
                    status = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(status, dict) or status.get("status") != "ready":
                    continue
                folder = database.with_suffix("") / identifier
                if not folder.is_symlink() and folder.is_dir():
                    context = FittedCheckpointContext(folder, project_id, revision, signature)
                    if context.records:
                        contexts.append(context)
        return cls(contexts)

    def _ensure_index(self):
        if self._index is not None:
            return
        self._index = {}
        self.index_passes += 1
        for context in self.contexts:
            folder = context.folder
            if folder.is_symlink():
                continue
            for path in sorted(folder.glob("fitted-*.json")):
                if (path.is_symlink() or not path.is_file()
                        or re.fullmatch(r"fitted-[1-9][0-9]*\.json", path.name) is None
                        or path.name not in context.records):
                    continue
                try:
                    # Index only the immutable physical-plan/source key. Do not
                    # retain all solid inventories/compressed STEP in RAM.
                    with path.open() as stream:
                        record = json.load(stream)
                    proof = context.records.get(path.name)
                    if (not proof or record.get("fingerprint") != proof["fingerprint"]
                            or record.get("assembled_step", {}).get("source_id") != proof["step_sha256"]):
                        continue
                    key = geometry_key(record["plan"], record["source_models"])
                    stat = path.stat()
                    self._index.setdefault(key, []).append((path, (stat.st_ino, stat.st_size, stat.st_mtime_ns), context))
                    self.indexed_files += 1
                except (OSError, ValueError, KeyError, TypeError, OverflowError):
                    continue

    def get(self, plan, source_models):
        self._ensure_index()
        for path, stamp, context in self._index.get(geometry_key(plan, source_models), ()):
            try:
                if path.is_symlink() or path.parent.is_symlink():
                    continue
                stat = path.stat()
                if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != stamp:
                    continue
                record = WholeWallRecord.model_validate_json(path.read_bytes())
                if not context.accepts(path.name, record):
                    continue
                result = rebind_record(record, plan, source_models)
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                continue
            self.hits += 1
            return result
        return None
