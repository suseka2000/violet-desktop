"""SQLite persistence for immutable project snapshots and their revision journal.

The repository intentionally knows nothing about geometric validity.  It only
validates the Pydantic contract, generates revisions, and protects concurrent
writes with optimistic revision checks.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Iterator

from pydantic import ValidationError

from ..domain.models import Project, utc_now


DATABASE_VERSION = 2


class StorageError(Exception):
    """Base class for repository failures."""


class NotFoundError(StorageError, LookupError):
    """A project or requested historical revision does not exist."""


class ConflictError(StorageError):
    """The caller tried to save against a stale project revision."""

    def __init__(
        self,
        project_id: str,
        expected_revision: int,
        actual_revision: int,
        project_revision: int | None = None,
    ):
        self.project_id = project_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.project_revision = project_revision
        message = (
            f"Revision conflict for project {project_id!r}: "
            f"expected {expected_revision}, actual {actual_revision}"
        )
        if project_revision is not None and project_revision != actual_revision:
            message += f", project carries stale revision {project_revision}"
        super().__init__(message)


class CorruptSnapshotError(StorageError, ValueError):
    """A stored snapshot failed its digest or model validation check."""


class UnsupportedDatabaseVersionError(StorageError, ValueError):
    """The database was created by an unsupported schema version."""


def _canonical_project_json(project: Project) -> str:
    """Return a stable UTF-8 JSON representation used for snapshot hashes."""

    return json.dumps(
        project.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_revision(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _normalise_actor(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("actor must be a string")
    return value


def _normalise_message(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("message must be a string")
    return value


def _publish_file(temp_path: Path, target: Path, *, overwrite: bool) -> None:
    """Publish a complete file without exposing a partially written target."""

    if overwrite:
        os.replace(temp_path, target)
        return

    try:
        # A same-directory hard link is an atomic create-if-absent operation.
        os.link(temp_path, target)
    except FileExistsError:
        raise
    else:
        temp_path.unlink()


class Repository:
    """Persistent, revisioned project repository backed by SQLite."""

    def __init__(self, db_path: str | Path):
        if isinstance(db_path, Path):
            raw_path = os.fspath(db_path)
        elif isinstance(db_path, str):
            raw_path = db_path
        else:
            raise TypeError("db_path must be str or pathlib.Path")

        if not raw_path:
            raise ValueError("db_path must not be empty")

        self._memory = raw_path == ":memory:"
        self.db_path = Path(raw_path) if not self._memory else Path(":memory:")
        if not self._memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            if self.db_path.exists() and self.db_path.is_dir():
                raise IsADirectoryError(self.db_path)

        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            raw_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if not self._memory:
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")

        try:
            self._initialise_database()
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    def __enter__(self) -> "Repository":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageError("Repository is closed")

    def _initialise_database(self) -> None:
        with self._lock:
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version > DATABASE_VERSION:
                raise UnsupportedDatabaseVersionError(
                    f"Database schema version {version} is newer than supported version "
                    f"{DATABASE_VERSION}"
                )
            if version < 0:
                raise UnsupportedDatabaseVersionError(f"Invalid database schema version {version}")

            if version == 0:
                # Start from the original snapshot schema, then apply the same
                # migrations as an existing database.
                self._connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS projects (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        snapshot_sha256 TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS project_revisions (
                        project_id TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision >= 1),
                        created_at TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        message TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        snapshot_sha256 TEXT NOT NULL,
                        PRIMARY KEY (project_id, revision),
                        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_projects_updated_at
                        ON projects(updated_at DESC, id);
                    PRAGMA user_version = 1;
                    COMMIT;
                    """
                )
                version = 1
            if version == 1:
                # A nullable marker keeps every snapshot and shared source
                # intact. Migration is atomic and leaves existing projects active.
                self._connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE projects ADD COLUMN deleted_at TEXT;
                    CREATE INDEX idx_projects_deleted_at
                        ON projects(deleted_at, updated_at DESC, id);
                    PRAGMA user_version = 2;
                    COMMIT;
                    """
                )
            self._verify_schema()

    def _verify_schema(self) -> None:
        required = {
            "projects": {
                "id", "name", "revision", "created_at", "updated_at",
                "snapshot_json", "snapshot_sha256", "deleted_at",
            },
            "project_revisions": {
                "project_id", "revision", "created_at", "actor", "message",
                "snapshot_json", "snapshot_sha256",
            },
        }
        for table, columns in required.items():
            rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual = {row["name"] for row in rows}
            if not columns.issubset(actual):
                missing = ", ".join(sorted(columns - actual))
                raise StorageError(f"Database table {table!r} is missing columns: {missing}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._ensure_open()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    @staticmethod
    def _decode_snapshot(
        snapshot_json: str,
        expected_sha256: str,
        *,
        project_id: str,
        revision: int,
    ) -> Project:
        actual_sha256 = _sha256(snapshot_json)
        if not isinstance(expected_sha256, str) or not hmac.compare_digest(
            actual_sha256, expected_sha256
        ):
            raise CorruptSnapshotError(
                f"Snapshot integrity check failed for project {project_id!r}, revision {revision}"
            )
        try:
            project = Project.model_validate_json(snapshot_json)
        except (ValidationError, ValueError, TypeError) as exc:
            raise CorruptSnapshotError(
                f"Snapshot is invalid for project {project_id!r}, revision {revision}"
            ) from exc
        if project.id != project_id or project.revision != revision:
            raise CorruptSnapshotError(
                f"Snapshot identity mismatch for project {project_id!r}, revision {revision}"
            )
        return project

    def list_projects(self) -> list[dict]:
        """Return compact latest-revision metadata, newest first."""

        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, name, revision, updated_at
                FROM projects
                WHERE deleted_at IS NULL
                ORDER BY updated_at DESC, id ASC
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "revision": int(row["revision"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def list_trash(self) -> list[dict]:
        """Return recoverable projects; no source files are ever removed."""

        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, name, revision, updated_at, deleted_at
                FROM projects WHERE deleted_at IS NOT NULL
                ORDER BY deleted_at DESC, id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def empty_trash(self, expected_projects: list[dict]) -> dict:
        """Permanently remove exactly the trash snapshot confirmed by the caller.

        The comparison and deletion share one write transaction. A concurrent
        restore, archive or revision change therefore requires fresh confirmation.
        Shared source files and active projects are outside this operation.
        """
        if not isinstance(expected_projects, list):
            raise ValueError("expected_projects must be a list")
        expected = {}
        for item in expected_projects:
            if (not isinstance(item, dict) or set(item) != {"id", "revision"}
                    or not isinstance(item["id"], str) or not item["id"]
                    or item["id"] in expected):
                raise ValueError("Trash confirmation requires unique project identifiers and revisions")
            expected[item["id"]] = _validate_revision(item["revision"], name="revision")
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, revision FROM projects WHERE deleted_at IS NOT NULL"
            ).fetchall()
            current = {row["id"]: int(row["revision"]) for row in rows}
            if set(current) != set(expected):
                raise ConflictError("trash", len(expected), len(current))
            for project_id, revision in expected.items():
                if current[project_id] != revision:
                    raise ConflictError(project_id, revision, current[project_id])
            deleted_revisions = 0
            for project_id, revision in expected.items():
                deleted_revisions += connection.execute(
                    "SELECT COUNT(*) FROM project_revisions WHERE project_id=?", (project_id,)
                ).fetchone()[0]
                connection.execute(
                    "DELETE FROM projects WHERE id=? AND revision=? AND deleted_at IS NOT NULL",
                    (project_id, revision),
                )
            return {"deleted_count": len(expected), "project_ids": sorted(expected),
                    "deleted_revisions": deleted_revisions}

    def head(self, project_id: str) -> dict:
        """Read the active snapshot identity without decoding its CAD payload."""
        self._ensure_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT id, revision, snapshot_sha256, updated_at FROM projects "
                "WHERE id=? AND deleted_at IS NULL", (project_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Project {project_id!r} was not found")
        return dict(row)

    def get(self, project_id: str, revision: int | None = None) -> Project:
        """Load the latest or a historical immutable project snapshot."""

        self._ensure_open()
        if revision is not None:
            _validate_revision(revision, name="revision")

        with self._lock:
            if revision is None:
                row = self._connection.execute(
                    """
                    SELECT revision, snapshot_json, snapshot_sha256
                    FROM projects WHERE id = ? AND deleted_at IS NULL
                    """,
                    (project_id,),
                ).fetchone()
            else:
                row = self._connection.execute(
                    """
                    SELECT r.revision, r.snapshot_json, r.snapshot_sha256
                    FROM project_revisions r JOIN projects p ON p.id = r.project_id
                    WHERE r.project_id = ? AND r.revision = ? AND p.deleted_at IS NULL
                    """,
                    (project_id, revision),
                ).fetchone()

        if row is None:
            suffix = "" if revision is None else f" at revision {revision}"
            raise NotFoundError(f"Project {project_id!r}{suffix} was not found")
        return self._decode_snapshot(
            row["snapshot_json"],
            row["snapshot_sha256"],
            project_id=project_id,
            revision=int(row["revision"]),
        )

    def save(
        self,
        project: Project,
        *,
        expected_revision: int,
        actor: str = "local",
        message: str = "",
    ) -> Project:
        """Store a new immutable snapshot after an optimistic revision check."""

        expected_revision = _validate_revision(expected_revision, name="expected_revision")
        actor = _normalise_actor(actor)
        message = _normalise_message(message)
        try:
            # Nested list mutation can bypass assignment validation, so validate
            # the complete Pydantic contract at the storage boundary.
            candidate = Project.model_validate(project.model_dump(mode="json"))
        except (ValidationError, ValueError, TypeError) as exc:
            raise ValueError("project does not satisfy the Project contract") from exc

        with self._transaction() as connection:
            current = connection.execute(
                """
                SELECT revision, created_at, snapshot_json, snapshot_sha256, deleted_at
                FROM projects WHERE id = ?
                """,
                (candidate.id,),
            ).fetchone()
            if current is not None and current["deleted_at"] is not None:
                raise NotFoundError(f"Project {candidate.id!r} was not found")
            return self._save_locked(
                connection,
                candidate,
                current=current,
                expected_revision=expected_revision,
                actor=actor,
                message=message,
            )

    def _save_locked(
        self,
        connection: sqlite3.Connection,
        candidate: Project,
        *,
        current: sqlite3.Row | None,
        expected_revision: int,
        actor: str,
        message: str,
    ) -> Project:
        actual_revision = 0 if current is None else int(current["revision"])
        if expected_revision != actual_revision or candidate.revision != actual_revision:
            raise ConflictError(
                candidate.id,
                expected_revision,
                actual_revision,
                project_revision=candidate.revision,
            )

        if current is None:
            created_at = candidate.created_at
        else:
            current_snapshot = self._decode_snapshot(
                current["snapshot_json"],
                current["snapshot_sha256"],
                project_id=candidate.id,
                revision=actual_revision,
            )
            try:
                created_at = datetime.fromisoformat(current["created_at"])
            except (TypeError, ValueError) as exc:
                raise CorruptSnapshotError(
                    f"Stored creation timestamp is invalid for project {candidate.id!r}"
                ) from exc
            if created_at.tzinfo is None:
                raise CorruptSnapshotError(
                    f"Stored creation timestamp has no timezone for project {candidate.id!r}"
                )
            if current_snapshot.created_at != created_at:
                raise CorruptSnapshotError(
                    f"Stored creation timestamp disagrees with the latest snapshot for "
                    f"project {candidate.id!r}"
                )

        revision = actual_revision + 1
        updated_at = utc_now()
        if updated_at < created_at:
            updated_at = created_at.astimezone(timezone.utc)

        stored = Project.model_validate(
            candidate.model_copy(
                update={
                    "revision": revision,
                    "created_at": created_at,
                    "updated_at": updated_at,
                },
                deep=True,
            ).model_dump(mode="json")
        )
        snapshot_json = _canonical_project_json(stored)
        snapshot_sha256 = _sha256(snapshot_json)
        created_at_text = stored.created_at.isoformat()
        updated_at_text = stored.updated_at.isoformat()

        if current is None:
            connection.execute(
                """
                INSERT INTO projects (
                    id, name, revision, created_at, updated_at,
                    snapshot_json, snapshot_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.id,
                    stored.metadata.name,
                    revision,
                    created_at_text,
                    updated_at_text,
                    snapshot_json,
                    snapshot_sha256,
                ),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE projects
                SET name = ?, revision = ?, updated_at = ?,
                    snapshot_json = ?, snapshot_sha256 = ?
                WHERE id = ? AND revision = ?
                """,
                (
                    stored.metadata.name,
                    revision,
                    updated_at_text,
                    snapshot_json,
                    snapshot_sha256,
                    stored.id,
                    actual_revision,
                ),
            )
            if cursor.rowcount != 1:
                fresh = connection.execute(
                    "SELECT revision FROM projects WHERE id = ?", (stored.id,)
                ).fetchone()
                fresh_revision = 0 if fresh is None else int(fresh["revision"])
                raise ConflictError(stored.id, expected_revision, fresh_revision)

        connection.execute(
            """
            INSERT INTO project_revisions (
                project_id, revision, created_at, actor, message,
                snapshot_json, snapshot_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored.id,
                revision,
                updated_at_text,
                actor,
                message,
                snapshot_json,
                snapshot_sha256,
            ),
        )
        return stored

    def history(self, project_id: str) -> list[dict]:
        """Return the append-only journal in chronological revision order."""

        self._ensure_open()
        with self._lock:
            exists = self._connection.execute(
                "SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)
            ).fetchone()
            if exists is None:
                raise NotFoundError(f"Project {project_id!r} was not found")
            rows = self._connection.execute(
                """
                SELECT revision, created_at, actor, message
                FROM project_revisions
                WHERE project_id = ?
                ORDER BY revision ASC
                """,
                (project_id,),
            ).fetchall()
        return [
            {
                "revision": int(row["revision"]),
                "created_at": row["created_at"],
                "actor": row["actor"],
                "message": row["message"],
            }
            for row in rows
        ]

    def restore(
        self,
        project_id: str,
        source_revision: int,
        expected_revision: int,
        actor: str = "local",
    ) -> Project:
        """Copy a historical snapshot into a new head revision."""

        source_revision = _validate_revision(source_revision, name="source_revision")
        expected_revision = _validate_revision(expected_revision, name="expected_revision")
        actor = _normalise_actor(actor)

        with self._transaction() as connection:
            current = connection.execute(
                """
                SELECT revision, created_at, snapshot_json, snapshot_sha256
                FROM projects WHERE id = ? AND deleted_at IS NULL
                """,
                (project_id,),
            ).fetchone()
            if current is None:
                raise NotFoundError(f"Project {project_id!r} was not found")

            actual_revision = int(current["revision"])
            if expected_revision != actual_revision:
                raise ConflictError(project_id, expected_revision, actual_revision)

            source = connection.execute(
                """
                SELECT revision, snapshot_json, snapshot_sha256
                FROM project_revisions
                WHERE project_id = ? AND revision = ?
                """,
                (project_id, source_revision),
            ).fetchone()
            if source is None:
                raise NotFoundError(
                    f"Project {project_id!r} at revision {source_revision} was not found"
                )

            historical = self._decode_snapshot(
                source["snapshot_json"],
                source["snapshot_sha256"],
                project_id=project_id,
                revision=int(source["revision"]),
            )
            candidate = historical.model_copy(update={"revision": actual_revision}, deep=True)
            return self._save_locked(
                connection,
                candidate,
                current=current,
                expected_revision=expected_revision,
                actor=actor,
                message=f"Restore revision {source_revision}",
            )

    def delete(self, project_id: str, *, expected_revision: int, actor: str = "local") -> dict:
        """Move an active project to the recoverable trash as a new revision."""

        project, deleted_at = self._set_deleted(
            project_id, expected_revision=expected_revision, actor=actor, deleted=True
        )
        return {"deleted": True, "project_id": project.id,
                "revision": project.revision, "deleted_at": deleted_at}

    def undelete(self, project_id: str, *, expected_revision: int, actor: str = "local") -> Project:
        """Restore exactly the saved project without discarding its history."""

        project, _ = self._set_deleted(
            project_id, expected_revision=expected_revision, actor=actor, deleted=False
        )
        return project

    def _set_deleted(self, project_id: str, *, expected_revision: int, actor: str,
                     deleted: bool) -> tuple[Project, str | None]:
        expected_revision = _validate_revision(expected_revision, name="expected_revision")
        actor = _normalise_actor(actor)
        with self._transaction() as connection:
            current = connection.execute(
                """
                SELECT revision, created_at, snapshot_json, snapshot_sha256, deleted_at
                FROM projects WHERE id = ?
                """, (project_id,),
            ).fetchone()
            if current is None or (current["deleted_at"] is not None) == deleted:
                raise NotFoundError(f"Project {project_id!r} was not found")
            actual_revision = int(current["revision"])
            if expected_revision != actual_revision:
                raise ConflictError(project_id, expected_revision, actual_revision)
            candidate = self._decode_snapshot(
                current["snapshot_json"], current["snapshot_sha256"],
                project_id=project_id, revision=actual_revision,
            )
            stored = self._save_locked(
                connection, candidate, current=current, expected_revision=expected_revision,
                actor=actor,
                message="Проект перемещён в корзину" if deleted else "Проект восстановлен из корзины",
            )
            deleted_at = stored.updated_at.isoformat() if deleted else None
            connection.execute("UPDATE projects SET deleted_at = ? WHERE id = ?",
                               (deleted_at, project_id))
            return stored, deleted_at

    def backup(self, target: Path, *, overwrite: bool = False) -> None:
        """Create a consistent SQLite backup at an explicit target path."""

        self._ensure_open()
        if not isinstance(target, Path):
            raise TypeError("backup target must be pathlib.Path")
        target = target.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)

        if not self._memory:
            source_resolved = self.db_path.expanduser().resolve(strict=False)
            target_resolved = target.resolve(strict=False)
            if source_resolved == target_resolved:
                raise ValueError("backup target must differ from the repository database")
            if self.db_path.exists() and target.exists():
                try:
                    if os.path.samefile(self.db_path, target):
                        raise ValueError("backup target must differ from the repository database")
                except FileNotFoundError:
                    pass

        if target.exists() and not overwrite:
            raise FileExistsError(target)

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            # sqlite3.connect accepts the pre-created empty file.  SQLite's
            # backup API supplies a transactionally consistent database image.
            destination = sqlite3.connect(temporary)
            try:
                with self._lock:
                    self._connection.backup(destination)
                destination.commit()
            finally:
                destination.close()

            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            _publish_file(temporary, target, overwrite=overwrite)
        finally:
            temporary.unlink(missing_ok=True)
