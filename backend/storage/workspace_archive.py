"""Bounded, local backup and restore for a complete application workspace.

The archive contains one transactionally consistent SQLite snapshot and every
immutable source object known to ``SourceStore``.  It is an interchange format
for local backups, not an upload or remote synchronisation mechanism.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sqlite3
import stat
import tempfile
from typing import BinaryIO
import zipfile

from pydantic import ValidationError

from ..importing import SourceRecord, SourceStore
from ..importing._common import MAX_CAD_SOURCE_BYTES
from ..importing._common import safe_filename, safe_scope
from .repository import DATABASE_VERSION, Repository


ARCHIVE_FORMAT = "interior-rd-workspace"
ARCHIVE_SCHEMA_VERSION = "1.0"
ARCHIVE_MANIFEST = "manifest.json"
DATABASE_MEMBER = "database.sqlite3"
RESTORED_DATABASE = "projects.sqlite3"
RESTORED_MANIFEST = "workspace-manifest.json"

# Limits are intentionally independent of ZIP metadata supplied by an
# untrusted archive.  Every entry is streamed and checked against both these
# limits and the signed-in-manifest byte count.
MAX_ARCHIVE_BYTES = 2_500_000_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2_000_000_000
MAX_DATABASE_BYTES = 1_000_000_000
MAX_ARCHIVE_ENTRIES = 8_194
MAX_ARCHIVE_SOURCES = 4_096
MAX_WORKSPACE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SOURCE_MANIFEST_BYTES = 64 * 1024
_CHUNK_BYTES = 1024 * 1024
_SHA256_LENGTH = 64


class WorkspaceArchiveError(ValueError):
    """A workspace archive is unsafe, corrupt, or incompatible."""

    code = "WORKSPACE_ARCHIVE_INVALID"


class WorkspaceArchiveLimitError(WorkspaceArchiveError):
    """A workspace archive exceeds a declared resource boundary."""

    code = "WORKSPACE_ARCHIVE_LIMIT_EXCEEDED"


def _reject_json_constant(value: str):
    raise WorkspaceArchiveError(f"Недопустимая константа JSON: {value}")


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceArchiveError(f"Поле JSON повторяется: {key}")
        result[key] = value
    return result


def _load_json(payload: bytes, *, label: str):
    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_strict_object,
        )
    except WorkspaceArchiveError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise WorkspaceArchiveError(f"Некорректный JSON: {label}") from exc


def _json_bytes(value) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hash_file(path: Path, *, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise WorkspaceArchiveLimitError(
                    f"Файл {path.name!r} превышает допустимый размер"
                )
            digest.update(chunk)
    return total, digest.hexdigest()


def _require_exact_keys(value: object, expected: set[str], *, label: str) -> dict:
    if not isinstance(value, dict):
        raise WorkspaceArchiveError(f"{label} должен быть объектом JSON")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise WorkspaceArchiveError(
            f"Некорректные поля {label}: отсутствуют {missing}, лишние {extra}"
        )
    return value


def _bounded_integer(value, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkspaceArchiveError(f"{label} должен быть целым числом")
    if not minimum <= value <= maximum:
        raise WorkspaceArchiveLimitError(
            f"{label} должен быть в диапазоне {minimum}..{maximum}"
        )
    return value


def _digest(value, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WorkspaceArchiveError(f"{label} должен быть SHA-256 в нижнем регистре")
    return value


def _member_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise WorkspaceArchiveError("Архив содержит небезопасное имя файла")
    path = PurePosixPath(name)
    windows_path = PureWindowsPath(name)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise WorkspaceArchiveError(f"Небезопасный путь внутри архива: {name!r}")
    if str(path) != name:
        raise WorkspaceArchiveError(f"Неканонический путь внутри архива: {name!r}")
    return name


def _source_paths(scope: str, source_id: str) -> tuple[str, str]:
    base = f"sources/{scope}/objects/{source_id[:2]}/{source_id}"
    return f"{base}/source", f"{base}/manifest.json"


def _zip_info(name: str) -> zipfile.ZipInfo:
    # ZIP timestamps cannot represent dates before 1980.  A fixed timestamp
    # avoids leaking local file mtimes and keeps metadata predictable.
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o444) << 16
    return info


def _write_zip_bytes(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    archive.writestr(_zip_info(name), payload)


def _write_zip_file(archive: zipfile.ZipFile, name: str, path: Path) -> None:
    with path.open("rb") as source, archive.open(_zip_info(name), "w") as destination:
        shutil.copyfileobj(source, destination, length=_CHUNK_BYTES)


def _publish_exclusive(temporary: Path, target: Path) -> None:
    try:
        os.link(temporary, target)
    except FileExistsError:
        raise FileExistsError(target) from None
    else:
        temporary.unlink()


def backup_workspace(
    repository: Repository,
    source_store: SourceStore,
    path: str | Path,
) -> dict:
    """Create an exclusive, atomic ZIP backup of the database and sources."""

    if not isinstance(repository, Repository):
        raise TypeError("repository должен быть Repository")
    if not isinstance(source_store, SourceStore):
        raise TypeError("source_store должен быть SourceStore")
    target = Path(path).expanduser()
    if target.is_symlink() or target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.is_dir():
        raise NotADirectoryError(target.parent)

    archive_fd, archive_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(archive_fd)
    temporary_archive = Path(archive_name)
    with tempfile.TemporaryDirectory(prefix="workspace-backup-", dir=target.parent) as work:
        temporary_database = Path(work) / DATABASE_MEMBER
        try:
            repository.backup(temporary_database)
            database_size, database_sha256 = _hash_file(
                temporary_database, max_bytes=MAX_DATABASE_BYTES
            )
            referenced_source_ids = _validate_database(temporary_database)

            records = sorted(source_store.list_records(), key=lambda item: item.source_id)
            if len(records) > MAX_ARCHIVE_SOURCES:
                raise WorkspaceArchiveLimitError(
                    f"В рабочей области больше {MAX_ARCHIVE_SOURCES} исходников"
                )
            scope = safe_scope(source_store.scope)
            source_entries: list[dict] = []
            source_payloads: list[tuple[str, bytes, str, bytes]] = []
            total_uncompressed = database_size
            for record in records:
                payload = source_store.get_bytes(record.source_id)
                record_payload = _json_bytes(record.model_dump(mode="json"))
                source_path, record_path = _source_paths(scope, record.source_id)
                entry = {
                    "record": record.model_dump(mode="json"),
                    "source_path": source_path,
                    "source_byte_length": len(payload),
                    "source_sha256": _sha256_bytes(payload),
                    "record_path": record_path,
                    "record_byte_length": len(record_payload),
                    "record_sha256": _sha256_bytes(record_payload),
                }
                source_entries.append(entry)
                source_payloads.append((source_path, payload, record_path, record_payload))
                total_uncompressed += len(payload) + len(record_payload)
                if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise WorkspaceArchiveLimitError(
                        "Суммарный размер рабочей области превышает лимит backup"
                    )
            available_source_ids = {record.source_id for record in records}
            missing_source_ids = sorted(referenced_source_ids - available_source_ids)
            if missing_source_ids:
                raise WorkspaceArchiveError(
                    "В проектах есть ссылки на отсутствующие исходники: "
                    + ", ".join(missing_source_ids[:5])
                )

            manifest = {
                "format": ARCHIVE_FORMAT,
                "schema_version": ARCHIVE_SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database": {
                    "path": DATABASE_MEMBER,
                    "byte_length": database_size,
                    "sha256": database_sha256,
                },
                "source_store": {
                    "scope": scope,
                    "sources": source_entries,
                },
            }
            manifest_payload = _json_bytes(manifest)
            if len(manifest_payload) > MAX_WORKSPACE_MANIFEST_BYTES:
                raise WorkspaceArchiveLimitError("Manifest backup превышает допустимый размер")
            if total_uncompressed + len(manifest_payload) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise WorkspaceArchiveLimitError(
                    "Суммарный размер рабочей области превышает лимит backup"
                )

            with zipfile.ZipFile(
                temporary_archive,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=False,
            ) as archive:
                _write_zip_file(archive, DATABASE_MEMBER, temporary_database)
                for source_path, payload, record_path, record_payload in source_payloads:
                    _write_zip_bytes(archive, source_path, payload)
                    _write_zip_bytes(archive, record_path, record_payload)
                _write_zip_bytes(archive, ARCHIVE_MANIFEST, manifest_payload)

            archive_size = temporary_archive.stat().st_size
            if archive_size > MAX_ARCHIVE_BYTES:
                raise WorkspaceArchiveLimitError("ZIP backup превышает допустимый размер")
            with temporary_archive.open("rb") as stream:
                os.fsync(stream.fileno())
            _publish_exclusive(temporary_archive, target)
            return manifest
        finally:
            temporary_archive.unlink(missing_ok=True)


def _check_eocd(path: Path) -> None:
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise WorkspaceArchiveLimitError("ZIP backup превышает допустимый размер")
    if size < 22:
        raise WorkspaceArchiveError("Файл слишком мал для ZIP backup")
    with path.open("rb") as stream:
        if stream.read(4) != b"PK\x03\x04":
            raise WorkspaceArchiveError("ZIP backup имеет посторонний префикс")
        tail_size = min(size, 65_557)
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or position + 22 > len(tail):
        raise WorkspaceArchiveError("В ZIP backup отсутствует корректный конец каталога")
    comment_length = int.from_bytes(tail[position + 20 : position + 22], "little")
    if position + 22 + comment_length != len(tail):
        raise WorkspaceArchiveError("После ZIP backup обнаружены посторонние данные")
    if comment_length:
        raise WorkspaceArchiveError("Комментарии ZIP не допускаются")


def _validate_zip_infos(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
        raise WorkspaceArchiveLimitError(
            f"ZIP должен содержать от 1 до {MAX_ARCHIVE_ENTRIES} файлов"
        )
    names = [info.filename for info in infos]
    if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
        raise WorkspaceArchiveError("ZIP содержит повторяющиеся имена файлов")
    total = 0
    result = {}
    for info in infos:
        name = _member_name(info.filename)
        if info.is_dir():
            raise WorkspaceArchiveError("Каталоги отдельными записями ZIP не допускаются")
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG}:
            raise WorkspaceArchiveError(f"ZIP содержит ссылку или специальный файл: {name}")
        if info.flag_bits & 0x1:
            raise WorkspaceArchiveError("Зашифрованные элементы ZIP не поддерживаются")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise WorkspaceArchiveError("Неподдерживаемый метод сжатия ZIP")
        if info.extra or info.comment:
            raise WorkspaceArchiveError("Дополнительные поля элементов ZIP не допускаются")
        if info.file_size < 0 or info.compress_size < 0:
            raise WorkspaceArchiveError("В ZIP указан отрицательный размер")
        total += info.file_size
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise WorkspaceArchiveLimitError("Распакованный ZIP превышает допустимый размер")
        result[name] = info
    return result


def _read_small_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum: int,
    label: str,
) -> bytes:
    if info.file_size > maximum:
        raise WorkspaceArchiveLimitError(f"{label} превышает допустимый размер")
    with archive.open(info, "r") as stream:
        payload = stream.read(maximum + 1)
        if len(payload) > maximum or stream.read(1):
            raise WorkspaceArchiveLimitError(f"{label} превышает допустимый размер")
    if len(payload) != info.file_size:
        raise WorkspaceArchiveError(f"Размер {label} не совпадает с каталогом ZIP")
    return payload


def _parse_manifest(payload: bytes) -> tuple[dict, dict[str, dict]]:
    manifest = _require_exact_keys(
        _load_json(payload, label=ARCHIVE_MANIFEST),
        {"format", "schema_version", "created_at", "database", "source_store"},
        label="manifest",
    )
    if manifest["format"] != ARCHIVE_FORMAT:
        raise WorkspaceArchiveError("Это не backup рабочей области Interior RD")
    if manifest["schema_version"] != ARCHIVE_SCHEMA_VERSION:
        raise WorkspaceArchiveError(
            f"Версия backup {manifest['schema_version']!r} не поддерживается"
        )
    try:
        created_at = datetime.fromisoformat(manifest["created_at"])
    except (TypeError, ValueError) as exc:
        raise WorkspaceArchiveError("Некорректная дата создания backup") from exc
    if created_at.tzinfo is None:
        raise WorkspaceArchiveError("Дата создания backup должна содержать часовой пояс")

    database = _require_exact_keys(
        manifest["database"], {"path", "byte_length", "sha256"}, label="database"
    )
    if database["path"] != DATABASE_MEMBER:
        raise WorkspaceArchiveError("Некорректный путь базы в manifest")
    _bounded_integer(
        database["byte_length"],
        label="Размер базы",
        minimum=1,
        maximum=MAX_DATABASE_BYTES,
    )
    _digest(database["sha256"], label="SHA-256 базы")

    source_store = _require_exact_keys(
        manifest["source_store"], {"scope", "sources"}, label="source_store"
    )
    try:
        scope = safe_scope(source_store["scope"])
    except (TypeError, ValueError) as exc:
        raise WorkspaceArchiveError("Некорректная область SourceStore в manifest") from exc
    sources = source_store["sources"]
    if not isinstance(sources, list):
        raise WorkspaceArchiveError("source_store.sources должен быть списком")
    if len(sources) > MAX_ARCHIVE_SOURCES:
        raise WorkspaceArchiveLimitError(
            f"В backup больше {MAX_ARCHIVE_SOURCES} исходников"
        )

    parsed: dict[str, dict] = {}
    order: list[str] = []
    for index, raw in enumerate(sources):
        entry = _require_exact_keys(
            raw,
            {
                "record",
                "source_path",
                "source_byte_length",
                "source_sha256",
                "record_path",
                "record_byte_length",
                "record_sha256",
            },
            label=f"source_store.sources[{index}]",
        )
        try:
            record = SourceRecord.model_validate(entry["record"])
        except (ValidationError, ValueError, TypeError) as exc:
            raise WorkspaceArchiveError(f"Некорректная запись исходника №{index + 1}") from exc
        try:
            if safe_filename(record.filename) != record.filename:
                raise WorkspaceArchiveError("В записи исходника небезопасное имя файла")
        except (TypeError, ValueError) as exc:
            raise WorkspaceArchiveError("В записи исходника небезопасное имя файла") from exc
        source_path, record_path = _source_paths(scope, record.source_id)
        if entry["source_path"] != source_path or entry["record_path"] != record_path:
            raise WorkspaceArchiveError("Путь исходника не соответствует его SHA-256")
        source_size = _bounded_integer(
            entry["source_byte_length"],
            label="Размер исходника",
            minimum=1,
            maximum=MAX_CAD_SOURCE_BYTES,
        )
        record_size = _bounded_integer(
            entry["record_byte_length"],
            label="Размер записи исходника",
            minimum=1,
            maximum=MAX_SOURCE_MANIFEST_BYTES,
        )
        if source_size != record.byte_length:
            raise WorkspaceArchiveError("Размер исходника не совпадает с его записью")
        if _digest(entry["source_sha256"], label="SHA-256 исходника") != record.source_id:
            raise WorkspaceArchiveError("SHA-256 исходника не совпадает с source_id")
        _digest(entry["record_sha256"], label="SHA-256 записи исходника")
        if record.source_id in parsed:
            raise WorkspaceArchiveError("Один source_id повторяется в manifest")
        order.append(record.source_id)
        parsed[record.source_id] = {
            "entry": entry,
            "record": record,
            "source_path": source_path,
            "record_path": record_path,
            "source_size": source_size,
            "record_size": record_size,
        }
    if order != sorted(order):
        raise WorkspaceArchiveError("Исходники в manifest должны быть отсортированы по source_id")
    return manifest, parsed


def _stream_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    expected_size: int,
    expected_sha256: str,
    destination: BinaryIO | None = None,
) -> None:
    if info.file_size != expected_size:
        raise WorkspaceArchiveError(f"Размер {info.filename!r} не совпадает с manifest")
    digest = hashlib.sha256()
    total = 0
    with archive.open(info, "r") as source:
        while True:
            chunk = source.read(_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise WorkspaceArchiveError(f"Файл {info.filename!r} длиннее значения в manifest")
            digest.update(chunk)
            if destination is not None:
                destination.write(chunk)
    if total != expected_size:
        raise WorkspaceArchiveError(f"Файл {info.filename!r} короче значения в manifest")
    if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        raise WorkspaceArchiveError(f"SHA-256 не совпадает для {info.filename!r}")


def _write_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as destination:
        _stream_member(
            archive,
            info,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            destination=destination,
        )
        destination.flush()
        os.fsync(destination.fileno())


def _validate_source_record(payload: bytes, expected: SourceRecord) -> None:
    data = _load_json(payload, label=f"manifest исходника {expected.source_id}")
    try:
        actual = SourceRecord.model_validate(data)
    except (ValidationError, ValueError, TypeError) as exc:
        raise WorkspaceArchiveError(
            f"Некорректный manifest исходника {expected.source_id}"
        ) from exc
    if actual != expected:
        raise WorkspaceArchiveError(
            f"Manifest исходника {expected.source_id} не совпадает с общим manifest"
        )


def _project_source_ids(project) -> set[str]:
    from ..workspace import SourceMarkup, validate_cad_assets

    try:
        result: set[str] = {asset.source_id for asset in validate_cad_assets(project.properties.get("cad_assets", []))}
    except (ValidationError, ValueError, TypeError) as exc:
        raise WorkspaceArchiveError("Некорректные ссылки на STEP-модели проекта") from exc
    for building in project.buildings:
        for floor in building.floors:
            markup = floor.properties.get("source_markup")
            if markup is not None:
                try:
                    parsed = SourceMarkup.model_validate(markup)
                except (ValidationError, ValueError, TypeError) as exc:
                    raise WorkspaceArchiveError(
                        f"Некорректная разметка исходника на этаже {floor.name!r}"
                    ) from exc
                result.update(source.source_id for source in parsed.sources)
            source_import = floor.properties.get("source_import")
            if isinstance(source_import, dict):
                source_id = source_import.get("source_id")
                if source_id is not None:
                    result.add(_digest(source_id, label="source_import.source_id"))
    return result


def _validate_database(path: Path) -> set[str]:
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise WorkspaceArchiveError("Не удалось открыть базу из backup") from exc
    connection.row_factory = sqlite3.Row
    referenced_source_ids: set[str] = set()
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise WorkspaceArchiveError("Проверка целостности SQLite не пройдена")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkspaceArchiveError("В базе backup нарушены внешние ключи")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {1, DATABASE_VERSION}:
            raise WorkspaceArchiveError(
                f"Версия базы {version} не поддерживается; ожидается {DATABASE_VERSION}"
            )
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%'"
        ).fetchall()
        tables = {row["name"] for row in objects if row["type"] == "table"}
        if tables != {"projects", "project_revisions"}:
            raise WorkspaceArchiveError("Состав таблиц базы backup не соответствует схеме")
        if any(row["type"] in {"trigger", "view"} for row in objects):
            raise WorkspaceArchiveError("Триггеры и представления в базе backup не допускаются")
        required = {
            "projects": {
                "id", "name", "revision", "created_at", "updated_at",
                "snapshot_json", "snapshot_sha256",
            },
            "project_revisions": {
                "project_id", "revision", "created_at", "actor", "message",
                "snapshot_json", "snapshot_sha256",
            },
        }
        if version >= 2:
            required["projects"].add("deleted_at")
        for table, columns in required.items():
            actual = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if not columns.issubset(actual):
                raise WorkspaceArchiveError(f"В таблице {table} не хватает обязательных полей")

        project_rows = connection.execute(
            "SELECT id, name, revision, created_at, updated_at, snapshot_json, snapshot_sha256, "
            + ("deleted_at " if version >= 2 else "NULL AS deleted_at ") +
            "FROM projects ORDER BY id"
        ).fetchall()
        revision_total = 0
        for row in project_rows:
            project_id = row["id"]
            revision = int(row["revision"])
            try:
                latest = Repository._decode_snapshot(
                    row["snapshot_json"],
                    row["snapshot_sha256"],
                    project_id=project_id,
                    revision=revision,
                )
            except Exception as exc:
                raise WorkspaceArchiveError(
                    f"Повреждён текущий снимок проекта {project_id!r}"
                ) from exc
            if row["name"] != latest.metadata.name:
                raise WorkspaceArchiveError("Имя проекта не совпадает с текущим снимком")
            referenced_source_ids.update(_project_source_ids(latest))
            try:
                created_at = datetime.fromisoformat(row["created_at"])
                updated_at = datetime.fromisoformat(row["updated_at"])
            except (TypeError, ValueError) as exc:
                raise WorkspaceArchiveError("Некорректная дата проекта в базе backup") from exc
            if created_at.tzinfo is None or updated_at.tzinfo is None:
                raise WorkspaceArchiveError("Даты проекта в базе backup должны содержать часовой пояс")
            if latest.created_at != created_at or latest.updated_at != updated_at:
                raise WorkspaceArchiveError("Даты проекта не совпадают с текущим снимком")
            if row["deleted_at"] is not None:
                try:
                    deleted_at = datetime.fromisoformat(row["deleted_at"])
                except (TypeError, ValueError) as exc:
                    raise WorkspaceArchiveError("Некорректная дата удаления проекта") from exc
                if deleted_at.tzinfo is None or deleted_at != updated_at:
                    raise WorkspaceArchiveError("Дата удаления не совпадает с текущей версией проекта")

            revisions = connection.execute(
                "SELECT revision, created_at, snapshot_json, snapshot_sha256 "
                "FROM project_revisions WHERE project_id = ? ORDER BY revision",
                (project_id,),
            ).fetchall()
            revision_total += len(revisions)
            if [int(item["revision"]) for item in revisions] != list(range(1, revision + 1)):
                raise WorkspaceArchiveError("История проекта содержит пропущенные версии")
            for item in revisions:
                item_revision = int(item["revision"])
                try:
                    historical = Repository._decode_snapshot(
                        item["snapshot_json"],
                        item["snapshot_sha256"],
                        project_id=project_id,
                        revision=item_revision,
                    )
                    journal_time = datetime.fromisoformat(item["created_at"])
                except Exception as exc:
                    raise WorkspaceArchiveError(
                        f"Повреждена версия {item_revision} проекта {project_id!r}"
                    ) from exc
                if journal_time.tzinfo is None or historical.updated_at != journal_time:
                    raise WorkspaceArchiveError("Дата версии не совпадает со снимком проекта")
                referenced_source_ids.update(_project_source_ids(historical))
            head = revisions[-1]
            if (
                head["snapshot_json"] != row["snapshot_json"]
                or head["snapshot_sha256"] != row["snapshot_sha256"]
            ):
                raise WorkspaceArchiveError("Текущая версия проекта не совпадает с концом истории")

        stored_revision_total = int(
            connection.execute("SELECT COUNT(*) FROM project_revisions").fetchone()[0]
        )
        if revision_total != stored_revision_total:
            raise WorkspaceArchiveError("В базе есть история без соответствующего проекта")
    except WorkspaceArchiveError:
        raise
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        raise WorkspaceArchiveError("База SQLite в backup повреждена") from exc
    finally:
        connection.close()
    return referenced_source_ids


def _read_and_validate_archive(
    archive_path: Path,
    staging: Path,
    *,
    restore_sources: bool,
) -> dict:
    _check_eocd(archive_path)
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise WorkspaceArchiveError("Не удалось открыть ZIP backup") from exc
    with archive:
        try:
            infos = _validate_zip_infos(archive)
            manifest_info = infos.get(ARCHIVE_MANIFEST)
            if manifest_info is None:
                raise WorkspaceArchiveError("В ZIP отсутствует manifest.json")
            manifest_payload = _read_small_entry(
                archive,
                manifest_info,
                maximum=MAX_WORKSPACE_MANIFEST_BYTES,
                label="manifest backup",
            )
            manifest, sources = _parse_manifest(manifest_payload)
            expected_names = {ARCHIVE_MANIFEST, DATABASE_MEMBER}
            for item in sources.values():
                expected_names.add(item["source_path"])
                expected_names.add(item["record_path"])
            if set(infos) != expected_names:
                raise WorkspaceArchiveError("ZIP содержит лишние файлы или не содержит обязательные")

            database = manifest["database"]
            database_target = staging / RESTORED_DATABASE
            _write_member(
                archive,
                infos[DATABASE_MEMBER],
                database_target,
                expected_size=database["byte_length"],
                expected_sha256=database["sha256"],
            )
            referenced_source_ids = _validate_database(database_target)
            missing_source_ids = sorted(referenced_source_ids - set(sources))
            if missing_source_ids:
                raise WorkspaceArchiveError(
                    "В базе backup есть ссылки на отсутствующие исходники: "
                    + ", ".join(missing_source_ids[:5])
                )

            scope = manifest["source_store"]["scope"]
            for item in sources.values():
                entry = item["entry"]
                record_payload = _read_small_entry(
                    archive,
                    infos[item["record_path"]],
                    maximum=MAX_SOURCE_MANIFEST_BYTES,
                    label=f"manifest исходника {item['record'].source_id}",
                )
                if len(record_payload) != item["record_size"] or not hmac.compare_digest(
                    _sha256_bytes(record_payload), entry["record_sha256"]
                ):
                    raise WorkspaceArchiveError("Manifest исходника не прошёл проверку SHA-256")
                _validate_source_record(record_payload, item["record"])

                if restore_sources:
                    object_root = (
                        staging
                        / "sources"
                        / scope
                        / "objects"
                        / item["record"].source_id[:2]
                        / item["record"].source_id
                    )
                    source_target = object_root / "source"
                    record_target = object_root / "manifest.json"
                    _write_member(
                        archive,
                        infos[item["source_path"]],
                        source_target,
                        expected_size=item["source_size"],
                        expected_sha256=entry["source_sha256"],
                    )
                    with record_target.open("xb") as destination:
                        destination.write(record_payload)
                        destination.flush()
                        os.fsync(destination.fileno())
                    try:
                        os.chmod(source_target, 0o444)
                        os.chmod(record_target, 0o444)
                        os.chmod(object_root, 0o555)
                    except OSError:
                        pass
                else:
                    _stream_member(
                        archive,
                        infos[item["source_path"]],
                        expected_size=item["source_size"],
                        expected_sha256=entry["source_sha256"],
                    )

            if restore_sources:
                restored_store = SourceStore(staging / "sources", scope=scope, max_bytes=MAX_CAD_SOURCE_BYTES)
                restored = restored_store.list_records()
                # SourceStore lists in upload-time order, not SHA order.
                if sorted(record.source_id for record in restored) != sorted(sources):
                    raise WorkspaceArchiveError("Восстановленный набор исходников не совпадает с manifest")
                for record in restored:
                    restored_store.get_bytes(record.source_id)

            with (staging / RESTORED_MANIFEST).open("xb") as destination:
                destination.write(manifest_payload)
                destination.flush()
                os.fsync(destination.fileno())
            return manifest
        except (zipfile.BadZipFile, RuntimeError, EOFError, OSError) as exc:
            if isinstance(exc, WorkspaceArchiveError):
                raise
            raise WorkspaceArchiveError("ZIP backup повреждён") from exc


def validate_workspace_archive(path: str | Path) -> dict:
    """Validate every member, source digest and SQLite snapshot without restoring."""

    archive_path = Path(path).expanduser()
    if archive_path.is_symlink() or not archive_path.is_file():
        raise WorkspaceArchiveError("Укажите обычный файл ZIP backup")
    with tempfile.TemporaryDirectory(prefix="workspace-validate-") as directory:
        return _read_and_validate_archive(
            archive_path,
            Path(directory),
            restore_sources=False,
        )


def restore_workspace(path: str | Path, target_dir: str | Path) -> dict:
    """Restore into a new directory and publish it only after full validation."""

    archive_path = Path(path).expanduser()
    target = Path(target_dir).expanduser()
    if archive_path.is_symlink() or not archive_path.is_file():
        raise WorkspaceArchiveError("Укажите обычный файл ZIP backup")
    if target.is_symlink() or target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent)
    )
    try:
        manifest = _read_and_validate_archive(
            archive_path,
            staging,
            restore_sources=True,
        )
        if target.is_symlink() or target.exists():
            raise FileExistsError(target)
        os.rename(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "ARCHIVE_FORMAT",
    "ARCHIVE_SCHEMA_VERSION",
    "WorkspaceArchiveError",
    "WorkspaceArchiveLimitError",
    "backup_workspace",
    "restore_workspace",
    "validate_workspace_archive",
]
