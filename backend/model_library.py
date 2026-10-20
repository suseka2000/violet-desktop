"""Cheap, read-only STEP catalogue rooted in an application-configured folder.

Listing hashes bytes but never starts a CAD/PDF parser. An explicit open verifies
the selected digest again before handing bytes to the bounded CAD worker. File
names and suffixes such as ``_1`` are provenance, not configuration identities.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
from threading import RLock
import unicodedata
import uuid
from datetime import datetime, timezone

from .vendor_library import (
    MAX_DEPTH, MAX_ENTRIES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, SHA256,
    VendorLibraryError, _root_fd, _safe_file, _signature,
)
from .library_io import (child_directory, close_directory, open_file, is_link,
                         make_directory, replace_file, unlink_file)

FORMATS = {".step", ".stp"}
CACHE_VERSION = "step-inspection-cache/1.0"
MAX_INSPECTION_BYTES = 8 * 1024 * 1024


class ModelLibraryError(ValueError):
    def __init__(self, message, *, code="model_library_invalid", status=422):
        super().__init__(message)
        self.code, self.status = code, status


def _search_text(value):
    return unicodedata.normalize("NFC", value).casefold()


class ModelLibrary:
    def __init__(self, root: str | Path, *, cache_dir: str | Path | None = None):
        # Do not resolve symlinks: the shared secure reader rejects every ancestor.
        self.root = Path(os.path.abspath(root))
        self.cache_dir = Path(os.path.abspath(cache_dir)) if cache_dir is not None else None
        self._cache = {}
        self._known: OrderedDict[str, dict] = OrderedDict()
        self._lock = RLock()

    def _paths(self):
        warnings, paths = [], []
        visited, byte_budget, complete = 0, 0, True
        try:
            root_fd = _root_fd(self.root)
        except FileNotFoundError:
            return False, False, [], ["Папка моделей ещё не найдена."]
        except OSError:
            return False, False, [], ["Папка моделей недоступна или содержит символическую ссылку."]

        def visit(directory, prefix, depth):
            nonlocal visited, byte_budget, complete
            if depth > MAX_DEPTH:
                complete = False
                warnings.append("Превышена глубина вложенности библиотеки: 6 каталогов.")
                return
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        complete = False
                        return
                    if entry.name.startswith((".", "~$")):
                        continue
                    relative = f"{prefix}/{entry.name}" if prefix else entry.name
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if is_link(info):
                            warnings.append(f"Символическая ссылка пропущена: {relative}")
                        elif stat.S_ISDIR(info.st_mode):
                            child = child_directory(directory, entry.name)
                            try:
                                visit(child, relative, depth + 1)
                            finally:
                                close_directory(child)
                            if visited > MAX_ENTRIES:
                                return
                        elif stat.S_ISREG(info.st_mode) and Path(entry.name).suffix.lower() in FORMATS:
                            if info.st_nlink != 1 or not 0 < info.st_size <= MAX_FILE_BYTES:
                                complete = False
                                warnings.append(f"STEP пропущен (ссылка, пустой файл или размер больше 64 МиБ): {relative}")
                                continue
                            if byte_budget + info.st_size > MAX_TOTAL_BYTES:
                                complete = False
                                warnings.append(f"Лимит чтения STEP 512 МиБ; пропущен: {relative}")
                                continue
                            byte_budget += info.st_size
                            paths.append((relative, info))
                    except OSError:
                        complete = False
                        warnings.append(f"Не удалось безопасно прочитать: {relative}")

        try:
            visit(root_fd, "", 0)
        finally:
            close_directory(root_fd)
        if visited > MAX_ENTRIES:
            warnings.append("Обход ограничен 1000 элементами. Библиотека показана не полностью.")
        return True, complete, sorted(paths, key=lambda item: _search_text(item[0])), warnings

    def inventory(self, *, q="", format=None):
        if not isinstance(q, str) or len(q) > 300 or (format is not None and (not isinstance(format, str) or format not in {"step", "stp"})):
            raise ModelLibraryError("Недопустимый фильтр библиотеки моделей")
        with self._lock:
            available, complete, paths, warnings = self._paths()
            grouped = {}
            for relative, info in paths:
                try:
                    cached = self._cache.get(relative)
                    if not cached or cached[0] != _signature(info):
                        content, info = _safe_file(self.root, relative)
                        digest = hashlib.sha256(content).hexdigest()
                        # A signature is not geometric validation; full parsing is explicit.
                        recognized = content.lstrip().startswith(b"ISO-10303-21;")
                        cached = (_signature(info), digest, recognized)
                        self._cache[relative] = cached
                    _, digest, recognized = cached
                    alias = {"filename": Path(relative).name, "relative_path": relative,
                             "format": Path(relative).suffix.lower()[1:]}
                    if digest not in grouped:
                        grouped[digest] = {
                            "id": digest, "source_id": digest, "sha256": digest,
                            **alias, "byte_size": info.st_size, "aliases": [],
                            "duplicate_count": 0,
                            "geometry_status": "not_inspected" if recognized else "invalid_signature",
                            "signature_status": "recognized" if recognized else "mismatch",
                            "approval_status": "unreviewed",
                            "open_url": f"/api/v1/model-library/{digest}/open",
                        }
                    grouped[digest]["aliases"].append(alias)
                except (OSError, VendorLibraryError):
                    complete = False
                    warnings.append(f"STEP недоступен или изменился: {relative}")
            live = {relative for relative, _ in paths}
            self._cache = {path: item for path, item in self._cache.items() if path in live}
            assets = list(grouped.values())
            for asset in assets:
                asset["duplicate_count"] = len(asset["aliases"]) - 1
                metadata = self._inspection_metadata(asset["id"])
                asset["inspection"] = metadata["summary"] if metadata else None
                if metadata:
                    summary = metadata["summary"]
                    asset["geometry_status"] = "inspected" if summary["valid_components"] == summary["component_count"] else "invalid_geometry"
                self._known[asset["id"]] = deepcopy(asset)
                self._known.move_to_end(asset["id"])
            while len(self._known) > MAX_ENTRIES or sum(len(asset["aliases"]) for asset in self._known.values()) > MAX_ENTRIES * 2:
                self._known.popitem(last=False)
            needle = _search_text(q.strip())
            selected = [asset for asset in assets if any(
                (format is None or alias["format"] == format)
                and (not needle or needle in _search_text(alias["relative_path"]))
                for alias in asset["aliases"])]
            files = sum(len(asset["aliases"]) for asset in assets)
            return {
                "schema_version": "model-library/1.0", "available": available,
                "complete": complete, "read_only": True, "assets": deepcopy(selected),
                "summary": {"files": files, "unique_models": len(assets),
                            "duplicate_files": files - len(assets), "filtered": len(selected)},
                "warnings": warnings + [
                    "Геометрия проверяется при открытии. Модели не являются утверждёнными монтажными узлами.",
                    "Суффиксы имён (_1, _2 и другие) не подтверждают конфигурации. Объединяются только побайтово одинаковые STEP.",
                ],
            }

    def original(self, model_id):
        if not isinstance(model_id, str) or not SHA256.fullmatch(model_id):
            raise ModelLibraryError("Модель не найдена", code="model_not_found", status=404)
        with self._lock:
            asset = self._known.get(model_id)
            if asset is None:
                self.inventory()
                asset = self._known.get(model_id)
            if asset is None:
                raise ModelLibraryError("Модель не найдена", code="model_not_found", status=404)
            for alias in asset["aliases"]:
                try:
                    content, _ = _safe_file(self.root, alias["relative_path"])
                except (OSError, VendorLibraryError):
                    continue
                if hashlib.sha256(content).hexdigest() == model_id:
                    return content, deepcopy(asset)
            raise ModelLibraryError(
                "Исходник изменился или недоступен. Обновите библиотеку и выберите модель заново.",
                code="model_source_changed", status=409,
            )

    def _cache_read(self, filename, max_bytes):
        if self.cache_dir is None:
            return None
        try:
            parent = _root_fd(self.cache_dir)
            try:
                descriptor = open_file(parent, filename)
            finally:
                close_directory(parent)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= max_bytes:
                    return None
                content = stream.read(max_bytes + 1)
                if len(content) != before.st_size or _signature(before) != _signature(os.fstat(stream.fileno())):
                    return None
                return content
        except OSError:
            return None

    def _cache_write(self, filename, content):
        if self.cache_dir is None:
            return
        parent = _root_fd(self.cache_dir.parent)
        try:
            try:
                make_directory(parent, self.cache_dir.name)
            except FileExistsError:
                pass
            directory = child_directory(parent, self.cache_dir.name)
            temporary = "." + uuid.uuid4().hex + ".tmp"
            try:
                descriptor = open_file(directory, temporary, create=True)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                replace_file(directory, temporary, filename)
            finally:
                try:
                    unlink_file(directory, temporary)
                except FileNotFoundError:
                    pass
                close_directory(directory)
        finally:
            close_directory(parent)

    def _inspection_metadata(self, model_id):
        raw = self._cache_read(f"{model_id}.metadata.json", 32 * 1024)
        if raw is None:
            return None
        try:
            metadata = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            summary = metadata["summary"]
            if (metadata["schema_version"] != CACHE_VERSION or metadata["source_id"] != model_id
                    or not SHA256.fullmatch(metadata["inspection_sha256"])
                    or type(summary["component_count"]) is not int or not 1 <= summary["component_count"] <= 256
                    or type(summary["valid_components"]) is not int or not 0 <= summary["valid_components"] <= summary["component_count"]
                    or summary["units"] != "mm" or not isinstance(summary["checked_at"], str)
                    or len(summary["checked_at"]) > 64):
                return None
            # Bounds are display metadata only, never model geometry.
            import math
            if any(not isinstance(summary["bounds_mm"][key], list) or len(summary["bounds_mm"][key]) != 3
                   or any(type(value) not in {int, float} or not math.isfinite(value) for value in summary["bounds_mm"][key])
                   for key in ("min", "max", "size")):
                return None
            return metadata
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return None

    def cached_inspection(self, model_id):
        """Return a versioned, bounded derived cache; caller still verifies source."""
        if not isinstance(model_id, str) or not SHA256.fullmatch(model_id):
            return None
        metadata = self._inspection_metadata(model_id)
        if not metadata:
            return None
        raw = self._cache_read(f"{model_id}.inspection.json", MAX_INSPECTION_BYTES)
        if raw is None or hashlib.sha256(raw).hexdigest() != metadata["inspection_sha256"]:
            return None
        try:
            result = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if (result["source_id"] != model_id or result["schema"] != "step-inspection/1"
                    or result["engineering_approved"] is not False or result["units"] != "mm"
                    or result["component_count"] != metadata["summary"]["component_count"]
                    or len(result["components"]) != result["component_count"]):
                return None
            return result
        except (ValueError, TypeError, KeyError, RecursionError):
            return None

    def remember_inspection(self, model_id, result):
        """Cache only successful worker results. Failure to cache must not lose CAD."""
        if self.cache_dir is None:
            return
        if not isinstance(model_id, str) or not SHA256.fullmatch(model_id) or result.get("source_id") != model_id:
            raise ModelLibraryError("Результат проверки не соответствует исходнику")
        try:
            raw = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            if len(raw) > MAX_INSPECTION_BYTES or result["schema"] != "step-inspection/1" or result["engineering_approved"] is not False:
                return
            metadata = {"schema_version": CACHE_VERSION, "source_id": model_id,
                        "inspection_sha256": hashlib.sha256(raw).hexdigest(),
                        "summary": {"component_count": result["component_count"],
                                    "valid_components": sum(component["valid"] is True for component in result["components"]),
                                    "bounds_mm": result["bounds_mm"], "units": result["units"],
                                    "checked_at": datetime.now(timezone.utc).isoformat()}}
            # Commit metadata last; a torn pair is ignored and regenerated.
            self._cache_write(f"{model_id}.inspection.json", raw)
            self._cache_write(f"{model_id}.metadata.json", json.dumps(metadata, ensure_ascii=False, allow_nan=False).encode())
        except (OSError, ValueError, KeyError, TypeError):
            return
