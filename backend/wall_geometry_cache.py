"""Bounded, content-addressed reuse of fully checked local wall geometry.

Room placement and editable labels are not geometry. Every dimension, opening,
panel cut, fastening coordinate, source SHA and kernel version remains in the
key. A hit is rebound to the requesting plan and checked again before use.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from functools import lru_cache
import hashlib
from pathlib import Path
import sqlite3
import time
import zlib

from .mount_assemblies import digest

MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 256


@contextmanager
def _connection(path):
    connection = sqlite3.connect(path, timeout=15)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


@lru_cache(maxsize=1)
def kernel_signature():
    root = Path(__file__).parent
    names = ("wall_assembly_geometry.py", "wall_assembly_concealed.py",
             "assembly_geometry.py", "assembly_fasteners.py", "step_geometry.py",
             "whole_wall_material_records.py", "whole_wall_assemblies.py",
             "wall_framing.py", "wall_systems.py", "assembly_recipes_concealed.py")
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})


def geometry_key(plan, source_models):
    # Native wall solids always use local X/depth/Z. Never drop panels, openings,
    # layer settings or checks; only the enclosing room/world identity differs.
    physical = {key: value for key, value in plan.items() if key not in {"id", "geometry_signature"}}
    metadata = {"id", "face_id", "name", "floor_id", "side", "space_id",
                "space_height_basis", "ceiling_space_id", "start", "end", "view_direction"}
    physical["wall"] = {key: value for key, value in plan["wall"].items() if key not in metadata}
    return digest({"policy": "whole-wall-geometry-cache/1", "kernel": kernel_signature(),
                   "plan": physical, "sources": sorted(source["source_id"] for source in source_models)})


def rebind_record(record, plan, source_models):
    from .whole_wall_assemblies import WholeWallRecord, record_identity
    if geometry_key(record.plan, record.source_models) != geometry_key(plan, source_models):
        raise ValueError("Сохранённая геометрия относится к другой расстановке стены")
    value = record.model_dump(mode="json")
    value.update(id=record_identity(plan, source_models), wall_id=plan["wall"]["id"],
                 face_id=plan["wall"]["face_id"], label=plan["wall"]["name"],
                 plan=deepcopy(plan), configuration=deepcopy(plan["configuration"]),
                 source_models=deepcopy(source_models))
    value["geometry"]["plan_id"] = plan["id"]
    value["geometry"]["spec_sha256"] = digest(plan)
    value["fingerprint"] = digest({key: item for key, item in value.items() if key != "fingerprint"})
    return WholeWallRecord.model_validate(value)


class WallGeometryCache:
    """Disposable derived storage, independent of portable project limits."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _connection(self.path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS wall_geometry (
                cache_key TEXT PRIMARY KEY, payload BLOB NOT NULL,
                byte_length INTEGER NOT NULL, touched_at REAL NOT NULL)""")

    def put(self, record):
        # Only a validated model from the native or portable record boundary is
        # accepted. Do not validate the entire growing project for this write.
        from .whole_wall_assemblies import WholeWallRecord
        if not isinstance(record, WholeWallRecord):
            raise TypeError("Нужна проверенная сборка стены")
        raw = record.model_dump_json().encode()
        if len(raw) > MAX_ENTRY_BYTES:
            return
        payload = zlib.compress(raw, level=1)
        key = geometry_key(record.plan, record.source_models)
        with _connection(self.path) as connection:
            connection.execute("INSERT OR REPLACE INTO wall_geometry VALUES (?,?,?,?)",
                               (key, payload, len(raw), time.time()))
            rows = connection.execute("SELECT cache_key,length(payload) FROM wall_geometry ORDER BY touched_at DESC").fetchall()
            total = 0
            for index, (identifier, size) in enumerate(rows):
                total += size
                if index >= MAX_ENTRIES or total > MAX_CACHE_BYTES:
                    connection.execute("DELETE FROM wall_geometry WHERE cache_key=?", (identifier,))

    def get(self, plan, source_models):
        from .whole_wall_assemblies import WholeWallRecord
        key = geometry_key(plan, source_models)
        with _connection(self.path) as connection:
            row = connection.execute("SELECT payload,byte_length FROM wall_geometry WHERE cache_key=?", (key,)).fetchone()
            if row is None:
                return None
            try:
                if not 0 < row[1] <= MAX_ENTRY_BYTES or len(row[0]) > MAX_ENTRY_BYTES:
                    raise ValueError("Размер записи кэша")
                decoder = zlib.decompressobj()
                raw = decoder.decompress(row[0], MAX_ENTRY_BYTES + 1)
                if len(raw) != row[1] or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise ValueError("Повреждена запись кэша")
                record = WholeWallRecord.model_validate_json(raw)
                result = rebind_record(record, plan, source_models)
            except (ValueError, zlib.error, TypeError, KeyError):
                connection.execute("DELETE FROM wall_geometry WHERE cache_key=?", (key,))
                return None
            connection.execute("UPDATE wall_geometry SET touched_at=? WHERE cache_key=?", (time.time(), key))
            return result
