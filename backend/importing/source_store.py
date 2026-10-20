"""Immutable, content-addressed storage for local source bytes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from pydantic import ValidationError

from ._common import (
    MAX_SOURCE_BYTES,
    bounded_content,
    content_id,
    media_type_for,
    safe_filename,
    safe_scope,
    safe_source_id,
)
from .errors import SourceIntegrityError, SourceLimitError, SourceNotFoundError
from .records import SourceRecord


def _reject_json_constant(value: str):
    raise ValueError(f"Недопустимая нестандартная константа JSON: {value}")


class SourceStore:
    """Store originals by SHA-256 inside one application-controlled scope.

    The only external identifier is a lowercase SHA-256 digest.  User supplied
    filenames are metadata and are never used as path components.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        scope: str = "local",
        max_bytes: int = MAX_SOURCE_BYTES,
    ) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes должен быть положительным целым числом")
        self.max_bytes = max_bytes
        self.scope = safe_scope(scope)
        root_path = Path(root).expanduser()
        root_path.mkdir(parents=True, exist_ok=True)
        if root_path.is_symlink():
            raise SourceIntegrityError("Корневой каталог хранилища не может быть символической ссылкой")
        self.root = root_path.resolve()
        self.scope_path = self.root / self.scope
        if self.scope_path.exists() and self.scope_path.is_symlink():
            raise SourceIntegrityError("Область хранилища не может быть символической ссылкой")
        self.objects_path = self.scope_path / "objects"
        self.objects_path.mkdir(parents=True, exist_ok=True)
        if self.objects_path.is_symlink() or not self.objects_path.is_dir():
            raise SourceIntegrityError("Каталог объектов хранилища имеет небезопасный тип")

    def put(self, filename: str, content: bytes) -> SourceRecord:
        safe_name = safe_filename(filename)
        payload = bounded_content(content, max_bytes=self.max_bytes)
        source_id = content_id(payload)
        target = self._object_path(source_id)
        self._ensure_prefix(target.parent)

        if target.exists():
            existing = self.get_record(source_id)
            self.get_bytes(source_id)
            return existing

        temporary = Path(
            tempfile.mkdtemp(prefix=f".{source_id}.", dir=str(target.parent))
        )
        record = SourceRecord(
            source_id=source_id,
            filename=safe_name,
            byte_length=len(payload),
            media_type=media_type_for(safe_name, payload),
            stored_at=datetime.now(timezone.utc),
        )
        try:
            self._write_new_file(temporary / "source", payload)
            manifest = json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            self._write_new_file(temporary / "manifest.json", manifest)
            try:
                os.rename(temporary, target)
            except OSError:
                if not target.exists():
                    raise
                shutil.rmtree(temporary)
                existing = self.get_record(source_id)
                self.get_bytes(source_id)
                return existing
            self._make_read_only(target)
            return record
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise

    def get_record(self, source_id: str) -> SourceRecord:
        object_path = self._checked_object_path(source_id)
        manifest_path = object_path / "manifest.json"
        self._require_regular_file(manifest_path)
        try:
            raw = self._read_regular_file(manifest_path, max_bytes=64 * 1024)
            data = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
            record = SourceRecord.model_validate(data)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise SourceIntegrityError(
                f"Манифест исходника {source_id} повреждён или имеет неверный формат"
            ) from exc
        if record.source_id != source_id:
            raise SourceIntegrityError("SHA-256 в манифесте исходника не совпадает с путём хранения")
        if record.byte_length > self.max_bytes:
            raise SourceIntegrityError("Сохранённый исходник превышает лимит этого хранилища")
        return record

    def get_bytes(self, source_id: str) -> bytes:
        object_path = self._checked_object_path(source_id)
        record = self.get_record(source_id)
        source_path = object_path / "source"
        self._require_regular_file(source_path)
        payload = self._read_regular_file(
            source_path,
            max_bytes=min(self.max_bytes, record.byte_length) + 1,
        )
        if len(payload) != record.byte_length:
            raise SourceIntegrityError("Размер сохранённого исходника не совпадает с манифестом")
        if content_id(payload) != source_id:
            raise SourceIntegrityError("SHA-256 сохранённого исходника не совпадает с идентификатором")
        return payload

    def exists(self, source_id: str) -> bool:
        object_path = self._object_path(safe_source_id(source_id))
        prefix_path = object_path.parent
        if prefix_path.is_symlink() or (prefix_path.exists() and not prefix_path.is_dir()):
            raise SourceIntegrityError("Префикс хранилища имеет небезопасный тип")
        if object_path.is_symlink():
            raise SourceIntegrityError("Каталог исходника не может быть символической ссылкой")
        if not object_path.exists():
            return False
        if not object_path.is_dir():
            raise SourceIntegrityError("Путь исходника не является обычным каталогом")
        source_path = object_path / "source"
        manifest_path = object_path / "manifest.json"
        for member in (source_path, manifest_path):
            if member.is_symlink() or (member.exists() and not member.is_file()):
                raise SourceIntegrityError("Хранилище содержит небезопасный объект")
        return source_path.is_file() and manifest_path.is_file()

    def list_records(self) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        if not self.objects_path.exists():
            return records
        for prefix in sorted(self.objects_path.iterdir(), key=lambda item: item.name):
            if prefix.is_symlink():
                raise SourceIntegrityError("Хранилище содержит символическую ссылку вместо префикса")
            if not prefix.is_dir():
                raise SourceIntegrityError("Хранилище содержит объект неожиданного типа")
            for object_path in sorted(prefix.iterdir(), key=lambda item: item.name):
                if object_path.is_symlink():
                    raise SourceIntegrityError("Хранилище содержит символическую ссылку вместо исходника")
                if not object_path.is_dir():
                    raise SourceIntegrityError("Хранилище содержит объект неожиданного типа")
                records.append(self.get_record(object_path.name))
        records.sort(key=lambda item: (item.stored_at, item.source_id))
        return records

    def _object_path(self, source_id: str) -> Path:
        identifier = safe_source_id(source_id)
        return self.objects_path / identifier[:2] / identifier

    def _checked_object_path(self, source_id: str) -> Path:
        object_path = self._object_path(source_id)
        prefix_path = object_path.parent
        if prefix_path.is_symlink() or (prefix_path.exists() and not prefix_path.is_dir()):
            raise SourceIntegrityError("Префикс хранилища имеет небезопасный тип")
        if not object_path.exists():
            raise SourceNotFoundError(source_id)
        if object_path.is_symlink() or not object_path.is_dir():
            raise SourceIntegrityError("Путь исходника не является обычным каталогом")
        expected_parent = prefix_path.resolve()
        if object_path.resolve().parent != expected_parent:
            raise SourceIntegrityError("Исходник оказался за пределами своей области хранения")
        return object_path

    @staticmethod
    def _ensure_prefix(prefix_path: Path) -> None:
        if prefix_path.is_symlink():
            raise SourceIntegrityError("Префикс хранилища не может быть символической ссылкой")
        prefix_path.mkdir(parents=False, exist_ok=True)
        if prefix_path.is_symlink() or not prefix_path.is_dir():
            raise SourceIntegrityError("Префикс хранилища имеет небезопасный тип")

    @staticmethod
    def _write_new_file(path: Path, content: bytes) -> None:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _make_read_only(object_path: Path) -> None:
        try:
            os.chmod(object_path / "source", 0o444)
            os.chmod(object_path / "manifest.json", 0o444)
            os.chmod(object_path, 0o555)
        except OSError:
            # Content addressing, exclusive creation and digest verification are
            # the security boundary; chmod is defense in depth on supporting FSes.
            pass

    @staticmethod
    def _require_regular_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise SourceIntegrityError(f"Сохранённый исходник неполон: нет {path.name}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceIntegrityError(f"Элемент исходника не является обычным файлом: {path.name}")

    @staticmethod
    def _read_regular_file(path: Path, *, max_bytes: int) -> bytes:
        # Unlike open(..., "rb"), os.open defaults to CRT text mode on
        # Windows: os.read would translate CRLF and treat Ctrl-Z as EOF.
        # Content-addressed originals must preserve every byte and their hash.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise SourceIntegrityError(f"Не удалось безопасно открыть элемент исходника: {path.name}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise SourceIntegrityError(f"Элемент исходника не является обычным файлом: {path.name}")
            if metadata.st_size > max_bytes:
                raise SourceLimitError(
                    f"Элемент {path.name} превышает допустимый размер",
                    details={"byte_length": metadata.st_size, "max_bytes": max_bytes},
                )
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > max_bytes:
                raise SourceLimitError(f"Элемент {path.name} превышает допустимый размер")
            return payload
        finally:
            os.close(descriptor)
