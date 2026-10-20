"""Remove disposable output belonging only to permanently deleted projects."""
from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import nullcontext


def purge_project_caches(project_ids, assembly_jobs, room_jobs, building_jobs=None):
    """No shared CAD library/source, arbitrary path or active project is removed.

    Call only with identifiers returned by Repository.empty_trash(). Workers
    validate their project and token before publishing, and these locks prevent
    a checkpoint from racing the deletion of its queue/result records.
    """
    result = {"removed_files": 0, "freed_bytes": 0, "warnings": []}
    logger = logging.getLogger("interior_rd.project_trash")
    for project_id in project_ids:
        try:
            # A newly imported project can reuse an identifier; never purge it.
            with (building_jobs._lock if building_jobs else nullcontext()), room_jobs._lock, assembly_jobs._lock, assembly_jobs.repository._lock:
                exists = assembly_jobs.repository._connection.execute(
                    "SELECT 1 FROM projects WHERE id=?", (project_id,)
                ).fetchone()
                if exists:
                    continue
                targets = []
                for jobs in (room_jobs, building_jobs):
                    if jobs is None:
                        continue
                    with jobs._connection() as connection:
                        rows = connection.execute(
                            "SELECT id FROM room_jobs WHERE project_id=?", (project_id,)
                        ).fetchall()
                        connection.execute("DELETE FROM room_jobs WHERE project_id=?", (project_id,))
                    targets.extend((jobs, row) for row in rows)
                with assembly_jobs._connection() as connection:
                    connection.execute("DELETE FROM assembly_jobs WHERE project_id=?", (project_id,))
                    connection.execute("DELETE FROM assembly_results WHERE project_id=?", (project_id,))
                for jobs, row in targets:
                    identifier = row["id"]
                    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
                        continue
                    folder = jobs.directory / identifier
                    if folder.is_symlink() or not folder.is_dir():
                        continue
                    for path in folder.iterdir():
                        known = path.name in {"room.step", "nodes.step", "result.json",
                                              "room.step.tmp", "nodes.step.tmp", "result.json.tmp"}
                        known = known or bool(re.fullmatch(r"fitted-[1-9][0-9]*\.(?:json|tmp)", path.name))
                        if jobs is building_jobs or hasattr(jobs, "api_prefix"):
                            from .building_assembly_jobs import known_output
                            known = known_output(path.name)
                        if not known or path.is_symlink() or not path.is_file():
                            continue
                        size = path.stat().st_size
                        path.unlink()
                        result["removed_files"] += 1
                        result["freed_bytes"] += size
                    try:
                        folder.rmdir()  # Only succeeds for this exact, empty job directory.
                    except OSError:
                        pass
        except (OSError, RuntimeError, sqlite3.Error) as error:
            logger.warning("Disposable cache cleanup failed for %s: %s", project_id, error)
            result["warnings"].append("Проект удалён. Часть старого кэша пока не удалось очистить.")
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result
