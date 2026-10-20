"""Read-only, hash-pinned supplier files. Native CAD files are not approved details.

The root is supplied by the local application, never by an HTTP request. The
library does not execute a supplied file, follow a symlink, upload anything or
alter originals. PDF/image inspection runs in a bounded subprocess. That is a
resource boundary, not an operating-system security sandbox.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time
from threading import BoundedSemaphore, RLock
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from .domain.models import Model
from .library_io import (root_directory as _root_fd, child_directory, close_directory,
                         open_file, is_link, entry_stat)
from .desktop_runtime import worker_command, worker_launch_options

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_DEPTH = 6
MAX_FACT_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_BYTES = 20 * 1024 * 1024
WORKER_SLOTS = BoundedSemaphore(2)
FORMATS = {".sldprt", ".sldasm", ".m3d", ".pdf", ".jpg", ".jpeg", ".dwg", ".dxf", ".step", ".stp"}
NATIVE_FORMATS = {"sldprt", "sldasm", "m3d"}
SHA256 = re.compile(r"^[a-f0-9]{64}$")
DWG_VERSIONS = {"AC1009": "AutoCAD R12", "AC1012": "AutoCAD R13", "AC1014": "AutoCAD R14", "AC1015": "AutoCAD 2000–2002", "AC1018": "AutoCAD 2004–2006", "AC1021": "AutoCAD 2007–2009", "AC1024": "AutoCAD 2010–2012", "AC1027": "AutoCAD 2013–2017", "AC1032": "AutoCAD 2018+"}
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class VendorLibraryError(ValueError):
    def __init__(self, message, *, code="vendor_library_invalid", status=422):
        super().__init__(message)
        self.code, self.status = code, status


class ReferenceDrawing(Model):
    source_sha256: Digest
    source_filename: Annotated[str, Field(min_length=1, max_length=255)]
    source_size_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_FILE_BYTES)] | None = None
    status: Literal["experimental_reference"]
    status_label: Annotated[str, Field(max_length=500)] | None = None
    derived_sha256: Digest
    derived_size_bytes: Annotated[int, Field(strict=True, ge=0, le=2 * 1024 * 1024 * 1024)] | None = None
    converter: dict[str, JsonValue]
    diagnostics: dict[str, JsonValue]
    summary: dict[str, JsonValue]
    limitations: list[Annotated[str, Field(max_length=3000)]] = Field(max_length=100)


class ReferenceAsset(Model):
    asset_id: Digest
    sha256: Digest
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    relative_path: Annotated[str, Field(min_length=1, max_length=2048)]
    format: str
    kind: Literal["model", "manual", "reference_drawing"]
    geometry_status: str
    approval_status: Literal["unreviewed"] = "unreviewed"
    byte_size: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)]
    category: str
    category_label: str
    classification_inferred: bool
    page_count: Annotated[int, Field(ge=1, le=500)] | None = None
    preview_url: str | None = None
    original_url: str
    reference_drawing: ReferenceDrawing | None = None

    @model_validator(mode="after")
    def pinned_reference(self):
        path = PurePosixPath(self.relative_path)
        if self.asset_id != self.sha256 or path.is_absolute() or ".." in path.parts or "\\" in self.relative_path or "\x00" in self.relative_path or path.name != self.filename:
            raise ValueError("Некорректная ссылка на библиотечный материал")
        if "." + self.format not in FORMATS:
            raise ValueError("Неизвестный формат библиотечного материала")
        prefix = f"/api/v1/vendor-library/{self.asset_id}"
        if self.original_url != prefix + "/original" or self.preview_url not in {None, prefix + "/preview.png?page=1"}:
            raise ValueError("Ссылка материала должна указывать на его локальную версию")
        if self.reference_drawing and (self.format != "dwg" or self.reference_drawing.source_sha256 != self.sha256):
            raise ValueError("Экспериментальная выжимка DWG не совпадает с исходником")
        return self


class ManualSource(Model):
    document_id: Annotated[str, Field(min_length=1, max_length=200)]
    page: Annotated[int, Field(strict=True, ge=1, le=500)]


class ManualDocument(Model):
    id: Annotated[str, Field(min_length=1, max_length=200)]
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    sha256: Digest
    title: Annotated[str, Field(min_length=1, max_length=500)]
    page_count: Annotated[int, Field(strict=True, ge=1, le=500)]
    source_status: Literal["verified", "changed", "missing", "page_unverified"] = "missing"
    asset_id: Digest | None = None


class ManualRule(Model):
    id: Annotated[str, Field(min_length=1, max_length=200)]
    system: Literal["open_mount", "concealed_mount", "general"]
    topic: Annotated[str, Field(min_length=1, max_length=300)]
    title: Annotated[str, Field(min_length=1, max_length=500)]
    requirement: Annotated[str, Field(min_length=1, max_length=10000)]
    source: ManualSource
    kind: Literal["numeric", "checklist", "detail"]
    parameter: Annotated[str, Field(max_length=200)] | None = None
    value: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None = None
    value_min: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None = None
    value_max: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None = None
    unit: Annotated[str, Field(max_length=100)] | None = None
    scope: Annotated[str, Field(max_length=3000)]
    review_status: Literal["transcribed"]
    component_codes: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=100)
    context: Annotated[str, Field(max_length=3000)] | None = None
    contexts: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=100)
    assembly_id: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    source_status: Literal["verified", "changed", "missing", "page_unverified"] = "missing"
    source_sha256: Digest | None = None
    preview_url: str | None = None

    @model_validator(mode="after")
    def numeric_requirement(self):
        if self.kind == "numeric":
            fixed = self.value is not None and self.value_min is None and self.value_max is None
            interval = self.value is None and self.value_min is not None and self.value_max is not None and self.value_min <= self.value_max
            if not self.parameter or not self.unit or not (fixed or interval):
                raise ValueError("Числовая выписка требует параметр, единицу и одно значение либо корректный диапазон")
        return self


class ReferenceSnapshot(Model):
    """An immutable provenance attachment, not an installation approval."""
    schema_version: Literal["vendor-reference/1.0"] = "vendor-reference/1.0"
    snapshot_id: Digest
    assets: list[ReferenceAsset] = Field(max_length=MAX_ENTRIES)
    facts_sha256: Digest | None = None
    rule_ids: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=2000)
    documents: list[ManualDocument] = Field(default_factory=list, max_length=100)
    rules: list[ManualRule] = Field(default_factory=list, max_length=2000)
    approval_status: Literal["unreviewed"] = "unreviewed"

    @model_validator(mode="after")
    def verify_hash_and_links(self):
        data = self.model_dump(mode="json", exclude={"snapshot_id"})
        expected = _digest(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        if self.snapshot_id != expected:
            raise ValueError("Содержимое снимка библиотеки не совпадает с его SHA256")
        assets = {asset.asset_id: asset for asset in self.assets}
        docs = {doc.id: doc for doc in self.documents}
        rules = {rule.id: rule for rule in self.rules}
        if len(docs) != len(self.documents) or len(rules) != len(self.rules) or len(set(self.rule_ids)) != len(self.rule_ids):
            raise ValueError("Повторяющиеся идентификаторы в снимке библиотеки")
        for doc in self.documents:
            asset = assets.get(doc.asset_id)
            if doc.source_status == "verified" and (not asset or asset.sha256 != doc.sha256 or asset.page_count != doc.page_count):
                raise ValueError("Подтверждённый первоисточник отсутствует в снимке")
        for rule in self.rules:
            doc = docs.get(rule.source.document_id)
            if not doc or rule.source_sha256 != doc.sha256:
                raise ValueError("Правило ссылается на неизвестный первоисточник")
            if rule.source_status == "verified" and (doc.source_status != "verified" or rule.source.page > doc.page_count):
                raise ValueError("Страница правила не подтверждена первоисточником")
            expected_url = f"/api/v1/vendor-library/{doc.asset_id}/preview.png?page={rule.source.page}"
            if rule.preview_url not in {None, expected_url}:
                raise ValueError("Ссылка на страницу правила не совпадает с первоисточником")
        if set(self.rule_ids) != {rule.id for rule in self.rules if rule.source_status == "verified"}:
            raise ValueError("Список подтверждённых выписок не совпадает со снимком")
        return self


def reference_schema():
    result = ReferenceSnapshot.model_json_schema()
    result["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return result


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _safe_file(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(part in {".", "..", ""} for part in parts) or "\\" in relative or "\x00" in relative:
        raise VendorLibraryError("Недопустимый путь библиотечного файла")
    directories = [parent := _root_fd(root)]
    try:
        for part in parts[:-1]:
            parent = child_directory(parent, part)
            directories.append(parent)
        fd = open_file(parent, parts[-1])
    finally:
        for directory in reversed(directories):
            close_directory(directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise VendorLibraryError("Библиотека принимает только обычные файлы без символических и жёстких ссылок")
        if before.st_size > MAX_FILE_BYTES:
            raise VendorLibraryError("Библиотечный файл превышает лимит 64 МиБ")
        chunks, remaining = [], MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        content = b"".join(chunks)
        if len(content) > MAX_FILE_BYTES or _signature(before) != _signature(after) or len(content) != after.st_size:
            raise VendorLibraryError("Файл изменился во время чтения; повторите обновление библиотеки", code="vendor_source_changed", status=409)
        return content, after
    finally:
        os.close(fd)


def _worker(content, action, page=1):
    if not WORKER_SLOTS.acquire(blocking=False):
        raise VendorLibraryError("Обработчики библиотеки заняты; повторите запрос", code="vendor_busy", status=503)
    try:
        try:
            result = subprocess.run(worker_command("backend.vendor_library", action, str(page)), input=content, capture_output=True, timeout=20, check=False, cwd=Path(__file__).resolve().parents[1], **worker_launch_options())
        except subprocess.TimeoutExpired as exc:
            raise VendorLibraryError("Обработка библиотечного документа превысила 20 секунд") from exc
        if result.returncode or len(result.stdout) > MAX_PREVIEW_BYTES:
            raise VendorLibraryError("Не удалось прочитать документ; проверьте формат и номер страницы")
        if action == "preview":
            if not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                raise VendorLibraryError("Некорректный результат предпросмотра")
            return result.stdout
        return json.loads(result.stdout)
    finally:
        WORKER_SLOTS.release()


def _category(filename, fmt):
    name = filename.casefold()
    if fmt == "sldasm":
        return "assembly", "Сборка (не утверждённый узел)"
    if any(word in name for word in ("саморез", "клаймер", "кляймер", "clip", "крепёж", "крепеж")):
        return "fastener", "Крепёж"
    if any(word in name for word in ("подвес", "уголок", "краб", "крест")):
        return "bracket", "Кронштейн / соединитель"
    if any(word in name for word in ("плинтус", "крышка", "заглушка", "v-cap")):
        return "trim", "Обрамление / заглушка"
    if any(word in name for word in ("профиль", "омега", "v-start", "vh-", "vl-", "v-base", "v-т", "v-t")):
        return "profile", "Профиль"
    return "other", "Требуется классификация"


def _recommendations(fmt):
    if fmt in NATIVE_FORMATS:
        return ["STEP AP214 или AP242, миллиметры, без изменения масштаба", "DXF с размерными ортогональными видами и нужными разрезами (1:1)", "PDF с размерами, маркировками и обозначением плоскостей разреза", "Для сборки: полный комплект зависимых деталей и подтверждённая спецификация"]
    if fmt == "dwg":
        return ["DXF с сохранением слоёв, блоков, единиц и координат", "PDF всех листов в исходном масштабе с основной надписью", "Отдельно передать внешние ссылки XREF, шрифты и изображения, если они используются"]
    return []


class VendorLibrary:
    def __init__(self, root: str | Path, *, facts_path: str | Path | None = None, facts_paths: list[str | Path] | None = None, drawing_facts_path: str | Path | None = None):
        self.root = Path(os.path.abspath(root))  # Deliberately do not resolve symlinks.
        self.facts_paths = [Path(path) for path in (facts_paths or [])]
        if facts_path:
            self.facts_paths.insert(0, Path(facts_path))
        if len(self.facts_paths) > 10:
            raise ValueError("Не более 10 наборов выписок")
        self.drawing_facts_path = Path(drawing_facts_path) if drawing_facts_path else None
        self._cache, self._preview_cache = {}, {}
        self._lock = RLock()

    def _paths(self):
        warnings, paths, scanned, budget = [], [], 0, 0
        try:
            root_fd = _root_fd(self.root)
        except FileNotFoundError:
            return False, [], ["Папка материалов ещё не найдена. Исходные файлы не создаются автоматически."]
        except OSError:
            return False, [], ["Папка материалов недоступна или содержит символическую ссылку."]

        def visit(fd, prefix, depth):
            nonlocal scanned, budget
            if depth > MAX_DEPTH:
                warnings.append("Превышена глубина вложенности библиотеки: 6 каталогов.")
                return
            with os.scandir(fd) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_ENTRIES:
                        return
                    if entry.name.startswith((".", "~$")):
                        continue
                    relative = f"{prefix}/{entry.name}" if prefix else entry.name
                    try:
                        info = entry_stat(fd, entry)
                        if is_link(info):
                            warnings.append(f"Символическая ссылка пропущена: {relative}")
                        elif stat.S_ISDIR(info.st_mode):
                            child = child_directory(fd, entry.name)
                            try:
                                visit(child, relative, depth + 1)
                            finally:
                                close_directory(child)
                        elif stat.S_ISREG(info.st_mode) and Path(entry.name).suffix.lower() in FORMATS:
                            if info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                                warnings.append(f"Файл пропущен (ссылка или размер больше 64 МиБ): {relative}")
                                continue
                            if budget + info.st_size > MAX_TOTAL_BYTES:
                                warnings.append(f"Превышен общий лимит чтения 512 МиБ; пропущен: {relative}")
                                continue
                            budget += info.st_size
                            paths.append((relative, info))
                    except OSError:
                        warnings.append(f"Не удалось безопасно прочитать: {relative}")
        try:
            visit(root_fd, "", 0)
        finally:
            close_directory(root_fd)
        if scanned > MAX_ENTRIES:
            warnings.append("Обход ограничен 1000 элементами. Библиотека показана не полностью.")
        return True, sorted(paths, key=lambda value: value[0].casefold()), warnings

    def _asset(self, relative, info, *, metadata_allowed=True):
        cached = self._cache.get(relative)
        if cached and cached[0] == _signature(info) and (cached[1].get("metadata_status") != "deferred" or not metadata_allowed):
            return deepcopy(cached[1])
        content, info = _safe_file(self.root, relative)
        filename, fmt, sha = Path(relative).name, Path(relative).suffix.lower()[1:], _digest(content)
        kind = "model" if fmt in NATIVE_FORMATS | {"step", "stp"} else "reference_drawing" if fmt in {"dwg", "dxf"} else "manual"
        category, category_label = _category(filename, fmt) if kind == "model" else ("reference", "Справочный документ")
        warnings = []
        geometry_status = "export_required" if fmt in NATIVE_FORMATS else "conversion_required" if fmt == "dwg" else "not_imported" if kind == "model" else "reference_only"
        version, signature_status, page_count, preview_url, metadata_status = None, "extension_only", None, None, "not_applicable"
        if fmt == "dwg":
            header = content[:6].decode("ascii", errors="replace")
            version = {"signature": header, "description": DWG_VERSIONS.get(header, "Версия DWG не определена")}
            signature_status = "recognized" if header in DWG_VERSIONS else "mismatch"
        elif fmt == "pdf" or fmt in {"jpg", "jpeg"}:
            expected = b"%PDF-" in content[:1024] if fmt == "pdf" else content.startswith(b"\xff\xd8\xff")
            signature_status = "recognized" if expected else "mismatch"
            if expected and metadata_allowed:
                try:
                    metadata = _worker(content, "metadata")
                    page_count = metadata["page_count"]
                    preview_url = f"/api/v1/vendor-library/{sha}/preview.png?page=1"
                    metadata_status = "available"
                except (ValueError, OSError):
                    metadata_status = "failed"
                    warnings.append("Предпросмотр недоступен: документ не прошёл ограниченную проверку.")
            elif expected:
                metadata_status = "deferred"
                warnings.append("Проверка документа отложена из-за лимита времени; обновите библиотеку.")
        if signature_status == "mismatch":
            warnings.append("Сигнатура не совпадает с ожидаемым форматом; файл нельзя считать проверенным.")
        if kind == "model":
            warnings.append("Категория определена только по имени. Геометрия, состав и применимость не проверены; это не утверждённый монтажный узел.")
        if fmt == "sldasm":
            warnings.append("Связи сборки и наличие всех зависимых деталей пока не прочитаны.")
        if fmt in NATIVE_FORMATS:
            warnings.append("Версия родного CAD-файла не определена; требуется нейтральный экспорт.")
        result = {"id": sha, "sha256": sha, "filename": filename, "relative_path": relative, "byte_size": info.st_size,
                  "format": fmt, "format_version": version, "signature_status": signature_status,
                  "kind": kind, "category": category, "category_label": category_label, "classification_inferred": kind == "model",
                  "geometry_status": geometry_status, "approval_status": "unreviewed", "assembly_references_status": "pending" if fmt == "sldasm" else "not_applicable",
                  "page_count": page_count, "preview_url": preview_url, "metadata_status": metadata_status, "original_url": f"/api/v1/vendor-library/{sha}/original",
                  "export_recommendations": _recommendations(fmt), "warnings": warnings, "reference_drawing": None}
        self._cache[relative] = (_signature(info), deepcopy(result))
        return result

    def _facts_one(self, facts_path, assets):
        empty = {"schema_version": "manual-facts/1.0", "documents": [], "rules": [], "status": "missing", "sha256": None, "warnings": []}
        if not facts_path.is_file():
            return empty
        try:
            with facts_path.open("rb") as handle:
                content = handle.read(MAX_FACT_BYTES + 1)
            if len(content) > MAX_FACT_BYTES:
                raise ValueError("Файл выписок превышает 2 МиБ")
            data = json.loads(content, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Нечисловое значение")))
            if not isinstance(data, dict) or data.get("schema_version") != "manual-facts/1.0":
                raise ValueError("Неизвестная версия выписок")
            documents, rules = data.get("documents"), data.get("rules")
            if not isinstance(documents, list) or len(documents) > 100 or not isinstance(rules, list) or len(rules) > 2000:
                raise ValueError("Некорректный объём выписок")
            documents = [ManualDocument.model_validate(item).model_dump(mode="json") for item in documents]
            rules = [ManualRule.model_validate(item).model_dump(mode="json") for item in rules]
            indexed = {}
            for document in documents:
                if not isinstance(document, dict) or not isinstance(document.get("id"), str) or document["id"] in indexed or not SHA256.fullmatch(str(document.get("sha256", ""))):
                    raise ValueError("Некорректный первоисточник выписки")
                match = next((asset for asset in assets if asset["sha256"] == document["sha256"] and asset["format"] in {"pdf", "jpg", "jpeg"}), None)
                same_name = any(asset["filename"] == document.get("filename") for asset in assets)
                document["source_status"] = ("verified" if match["page_count"] == document["page_count"] else "page_unverified") if match else "changed" if same_name else "missing"
                document["asset_id"] = match["id"] if match else None
                indexed[document["id"]] = document
            seen = set()
            for rule in rules:
                if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or len(rule["id"]) > 200 or rule["id"] in seen:
                    raise ValueError("Некорректный идентификатор выписки")
                seen.add(rule["id"])
                source = rule.get("source")
                if not isinstance(source, dict) or source.get("document_id") not in indexed or type(source.get("page")) is not int or not 1 <= source["page"] <= 500:
                    raise ValueError("Некорректная ссылка на страницу первоисточника")
                if rule.get("system") not in {"open_mount", "concealed_mount", "general"}:
                    raise ValueError("Неизвестная система монтажа")
                if rule.get("review_status") != "transcribed":
                    raise ValueError("Выписка не должна объявляться утверждённой нормой")
                doc = indexed[source["document_id"]]
                asset = next((item for item in assets if item["id"] == doc["asset_id"]), None)
                valid_page = bool(asset and asset["page_count"] and source["page"] <= asset["page_count"])
                rule["source_status"] = doc["source_status"] if doc["source_status"] != "verified" else "verified" if valid_page else "page_unverified"
                rule["source_sha256"] = doc["sha256"]
                rule["preview_url"] = f"/api/v1/vendor-library/{doc['asset_id']}/preview.png?page={source['page']}" if valid_page else None
            return {**data, "documents": documents, "rules": rules, "status": "available", "sha256": _digest(content), "warnings": ["Выписки требуют инженерной проверки. Совпадение SHA256 подтверждает первоисточник, но не применимость узла."]}
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return {**empty, "status": "invalid", "warnings": ["Файл выписок не прошёл проверку. Правила из него не используются."]}

    def _facts(self, assets):
        results = [self._facts_one(path, assets) for path in self.facts_paths]
        documents, rules, warnings = {}, {}, []
        hashes = []
        for result in results:
            warnings.extend(result["warnings"])
            if result["sha256"]:
                hashes.append(result["sha256"])
            for doc in result["documents"]:
                if doc["id"] in documents and documents[doc["id"]] != doc:
                    return {"schema_version": "manual-facts/1.0", "documents": [], "rules": [], "status": "invalid", "sha256": None, "warnings": ["Конфликт идентификаторов первоисточников: выписки не используются."]}
                documents[doc["id"]] = doc
            for rule in result["rules"]:
                if rule["id"] in rules:
                    return {"schema_version": "manual-facts/1.0", "documents": [], "rules": [], "status": "invalid", "sha256": None, "warnings": ["Конфликт идентификаторов правил: выписки не используются."]}
                rules[rule["id"]] = rule
        status = "available" if hashes and all(result["status"] == "available" for result in results) else "partial" if hashes else "invalid" if any(result["status"] == "invalid" for result in results) else "missing"
        return {"schema_version": "manual-facts/1.0", "documents": list(documents.values()), "rules": list(rules.values()), "status": status, "sha256": _digest("\n".join(sorted(hashes)).encode()) if hashes else None, "warnings": list(dict.fromkeys(warnings))}

    def _drawing_facts(self, assets):
        if not self.drawing_facts_path or not self.drawing_facts_path.is_file():
            return []
        try:
            with self.drawing_facts_path.open("rb") as source:
                content = source.read(MAX_FACT_BYTES + 1)
            if len(content) > MAX_FACT_BYTES:
                raise ValueError("Слишком большой реестр DWG")
            data = json.loads(content)
            if data.get("schema_version") != 1 or not isinstance(data.get("records"), list) or len(data["records"]) > 100:
                raise ValueError("Неверный реестр DWG")
            records = [ReferenceDrawing.model_validate(record).model_dump(mode="json") for record in data["records"]]
            indexed = {record["source_sha256"]: record for record in records}
            if len(indexed) != len(records):
                raise ValueError("Повторяющиеся исходники DWG")
            for asset in assets:
                if asset["format"] == "dwg" and asset["sha256"] in indexed:
                    asset["reference_drawing"] = indexed[asset["sha256"]]
            missing = set(indexed) - {asset["sha256"] for asset in assets if asset["format"] == "dwg"}
            return ["Часть экспериментальных выжимок DWG не показана: исходник отсутствует или изменён."] if missing else []
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return ["Реестр экспериментального чтения DWG не прошёл проверку и не используется."]

    def inventory(self, *, q="", kind=None, category=None):
        if len(q) > 300 or kind not in {None, "model", "manual", "reference_drawing"} or category not in {None, "profile", "fastener", "bracket", "trim", "assembly", "other", "reference"}:
            raise VendorLibraryError("Недопустимый фильтр библиотеки")
        with self._lock:
            started = time.monotonic()
            available, paths, warnings = self._paths()
            assets = []
            for relative, info in paths:
                try:
                    assets.append(self._asset(relative, info, metadata_allowed=time.monotonic() - started < 6))
                except (OSError, ValueError):
                    warnings.append(f"Файл недоступен или изменился: {relative}")
            live_paths = {relative for relative, _ in paths}
            self._cache = {key: value for key, value in self._cache.items() if key in live_paths}
            facts = self._facts(assets)
            warnings.extend(self._drawing_facts(assets))
            needle = q.strip().casefold()
            selected = [asset for asset in assets if (not kind or asset["kind"] == kind) and (not category or asset["category"] == category) and (not needle or needle in (asset["filename"] + " " + asset["relative_path"] + " " + asset["category_label"]).casefold())]
            return {"schema_version": "vendor-library/1.0", "available": available, "read_only": True, "assets": selected,
                    "summary": {"total": len(assets), "filtered": len(selected), "by_kind": dict(Counter(asset["kind"] for asset in assets)), "by_format": dict(Counter(asset["format"] for asset in assets)), "approved_junctions": 0},
                    "warnings": warnings + ["Компоненты и сборки не являются утверждёнными узлами. Автоматическое назначение креплений по имени запрещено."],
                    "manual_facts": facts,
                    "export_queue": [{"asset_id": asset["id"], "filename": asset["filename"], "format": asset["format"], "recommendations": asset["export_recommendations"]} for asset in selected if asset["export_recommendations"]]}

    def original(self, asset_id):
        if not isinstance(asset_id, str) or not SHA256.fullmatch(asset_id):
            raise VendorLibraryError("Некорректный идентификатор материала", status=404, code="vendor_not_found")
        with self._lock:
            known = [value[1] for value in self._cache.values() if value[1]["id"] == asset_id]
            if not known:
                known = [asset for asset in self.inventory()["assets"] if asset["id"] == asset_id]
            for asset in known:
                try:
                    content, _ = _safe_file(self.root, asset["relative_path"])
                except (OSError, ValueError):
                    continue
                if _digest(content) == asset_id:
                    return content, deepcopy(asset)
            if known:
                raise VendorLibraryError("Исходник изменён или удалён. Обновите библиотеку; прежняя ссылка не будет выдана за новый файл.", status=409, code="vendor_source_changed")
            raise VendorLibraryError("Материал не найден в библиотеке", status=404, code="vendor_not_found")

    def preview(self, asset_id, *, page=1):
        if type(page) is not int or not 1 <= page <= 500:
            raise VendorLibraryError("Номер страницы должен быть целым числом от 1 до 500")
        content, asset = self.original(asset_id)  # Revalidate source even for cached previews.
        if asset["format"] not in {"pdf", "jpg", "jpeg"} or not asset["preview_url"] or not asset["page_count"] or page > asset["page_count"]:
            raise VendorLibraryError("Предпросмотр этой страницы недоступен")
        key = asset_id, page
        with self._lock:
            if key in self._preview_cache:
                return self._preview_cache[key]
        result = _worker(content, "preview", page)
        with self._lock:
            if len(self._preview_cache) >= 4:
                self._preview_cache.pop(next(iter(self._preview_cache)))
            self._preview_cache[key] = result
        return result

    def snapshot(self):
        inventory = self.inventory()
        facts = inventory["manual_facts"]
        data = {"schema_version": "vendor-reference/1.0", "assets": [{"asset_id": asset["id"], **{key: asset[key] for key in ("sha256", "filename", "relative_path", "format", "kind", "geometry_status", "approval_status", "byte_size", "category", "category_label", "classification_inferred", "page_count", "preview_url", "original_url", "reference_drawing")}} for asset in inventory["assets"]],
                "facts_sha256": facts["sha256"], "rule_ids": [rule["id"] for rule in facts["rules"] if rule["source_status"] == "verified"], "documents": facts["documents"], "rules": facts["rules"], "approval_status": "unreviewed"}
        data["snapshot_id"] = _digest(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        return ReferenceSnapshot.model_validate(data).model_dump(mode="json")


def _main():
    content = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("Файл слишком большой")
    if sys.argv[1] == "preview":
        from .source_preview import render
        sys.stdout.buffer.write(render(content, int(sys.argv[2])))
    elif sys.argv[1] == "metadata":
        if b"%PDF-" in content[:1024]:
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(content) as document:
                count = len(document)
                if not 1 <= count <= 500:
                    raise ValueError("Слишком много страниц")
        else:
            from io import BytesIO
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = 40_000_000
            with Image.open(BytesIO(content)) as image:
                if image.format != "JPEG" or image.width * image.height > Image.MAX_IMAGE_PIXELS:
                    raise ValueError("Неподдерживаемое изображение")
                image.verify()
            count = 1
        sys.stdout.write(json.dumps({"page_count": count}))
    else:
        raise ValueError("Неизвестное действие")


if __name__ == "__main__":
    try:
        _main()
    except Exception:
        sys.exit(2)
