"""Bounded CAD workspace routes. Originals stay content-addressed and immutable.

These endpoints are separate from the small 2D room importer: accepting a CAD
document never implies that all its views are room geometry or approved nodes.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import importlib.util
import json
from pathlib import Path
from threading import BoundedSemaphore, RLock
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, model_validator

from ..domain.models import Model
from ..importing._common import MAX_CAD_SOURCE_BYTES, safe_filename, safe_source_id

CAD_SLOTS = BoundedSemaphore(2)
_CACHE_LOCK = RLock()
_CACHE: OrderedDict[str, tuple[dict, int]] = OrderedDict()
MAX_CACHE_BYTES = 24 * 1024**2
Finite = Annotated[float, Field(allow_inf_nan=False, ge=-1e12, le=1e12)]


class DxfViewRequest(Model):
    layout: Annotated[int, Field(strict=True, ge=1, le=500)] = 1
    region: list[Finite] | None = Field(default=None, min_length=4, max_length=4)
    layers: list[Annotated[str, Field(min_length=1, max_length=255)]] | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def ordered_region(self):
        if self.region and (self.region[2] <= self.region[0] or self.region[3] <= self.region[1]):
            raise ValueError("Выберите непустой прямоугольный участок чертежа")
        return self


class SectionRequest(Model):
    axis: Literal["x", "y", "z"]
    offset_mm: Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-1e9, le=1e9)]
    component_ids: list[Annotated[int, Field(strict=True, ge=1, le=256)]] | None = Field(default=None, min_length=1, max_length=256)


def cad_capabilities():
    step = importlib.util.find_spec("OCP") is not None
    return {"max_source_bytes": MAX_CAD_SOURCE_BYTES,
            "dxf": {"available": True, "layouts": True, "regions": True},
            "step": {"available": step, "sections": step,
                     "message": "Просмотр компонентов и геометрические сечения" if step else
                     "Установите дополнительные зависимости из requirements-cad.txt и перезапустите программу"},
            "engineering_approved": False}


def _kind(filename):
    suffix = Path(filename).suffix.lower()
    if suffix == ".dxf":
        return "dxf"
    if suffix in {".step", ".stp"}:
        return "step"
    raise ValueError("Рабочая область CAD принимает DXF и STEP/STP. Для DWG используйте обычный импорт с конвертером.")


def _cache_get(key):
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached:
            _CACHE.move_to_end(key)
            return cached[0]
    return None


def _cache_put(key, result):
    size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
    if size <= MAX_CACHE_BYTES:
        with _CACHE_LOCK:
            _CACHE[key] = (result, size)
            while len(_CACHE) > 6 or sum(value[1] for value in _CACHE.values()) > MAX_CACHE_BYTES:
                _CACHE.popitem(last=False)


def _inspect(content, filename):
    kind = _kind(filename)
    digest = hashlib.sha256(content).hexdigest()
    key = f"{kind}:{digest}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "filename": filename}
    if kind == "step":
        from ..step_worker import run_step_inspect
        result = run_step_inspect(content)
    else:
        from ..dxf_workspace_worker import run_dxf_inspect
        result = run_dxf_inspect(content)
    result = {**result, "format": kind, "source_id": digest, "byte_length": len(content), "cad_workspace": True}
    _cache_put(key, result)
    return {**result, "filename": filename}


def _read_upload(handler):
    from .server import HttpError
    if handler.headers.get_content_type() != "application/octet-stream":
        raise HttpError(415, "binary_required", "Передайте CAD-файл как application/octet-stream")
    if handler.headers.get("Transfer-Encoding"):
        raise HttpError(400, "invalid_encoding", "Требуется Content-Length")
    try:
        size = int(handler.headers.get("Content-Length", "-1"))
    except ValueError as exc:
        raise HttpError(400, "invalid_length", "Некорректный Content-Length") from exc
    if size < 0:
        raise HttpError(411, "length_required", "Требуется Content-Length")
    if not 0 < size <= MAX_CAD_SOURCE_BYTES:
        raise HttpError(413, "cad_file_too_large", "CAD-файл должен быть непустым и не больше 64 МиБ")
    handler.connection.settimeout(60)
    content = handler.rfile.read(size)
    if len(content) != size:
        raise HttpError(400, "incomplete_body", "Файл передан не полностью")
    return content


def handle_cad_assets(handler, method, path, source_store):
    from .server import HttpError
    if not path.startswith("/api/v1/cad-assets/"):
        return False
    if path == "/api/v1/cad-assets/capabilities" and method == "GET":
        handler.send_json(cad_capabilities())
        return True
    if not CAD_SLOTS.acquire(blocking=False):
        raise HttpError(429, "cad_busy", "Заняты обработчики CAD. Дождитесь завершения текущей операции.")
    try:
        if path == "/api/v1/cad-assets/sources" and method == "GET":
            records = source_store.list_records()
            handler.send_json({"sources": [record.model_dump(mode="json") for record in records
                                          if Path(record.filename).suffix.lower() in {".dxf", ".step", ".stp"}]})
            return True
        if path == "/api/v1/cad-assets/upload" and method == "POST":
            query = parse_qs(urlsplit(handler.path).query)
            names = query.get("filename", [])
            if len(names) != 1:
                raise ValueError("Укажите одно имя CAD-файла")
            filename = safe_filename(names[0])
            _kind(filename)
            content = _read_upload(handler)
            inspection = _inspect(content, filename)
            record = source_store.put(filename, content)
            handler.send_json({"source_id": record.source_id, "source": record.model_dump(mode="json"),
                               "inspection": inspection}, 201)
            return True
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            raise HttpError(404, "not_found", "Операция CAD не найдена")
        source_id, operation = safe_source_id(parts[3]), parts[4]
        if (method, operation) not in {("GET", "inspect"), ("GET", "original"),
                                       ("POST", "dxf-view"), ("POST", "section"), ("POST", "section.dxf"), ("POST", "section-snapshot")}:
            raise HttpError(404, "not_found", "Операция CAD не найдена")
        record = source_store.get_record(source_id)
        kind = _kind(record.filename)
        content = source_store.get_bytes(source_id)
        if operation == "inspect":
            handler.send_json({"source_id": source_id, "source": record.model_dump(mode="json"),
                               "inspection": _inspect(content, record.filename)})
        elif operation == "original":
            handler.send_bytes(content, "application/octet-stream", f"source-{source_id[:12]}{Path(record.filename).suffix.lower()}")
        elif operation == "dxf-view":
            if kind != "dxf":
                raise ValueError("Выбор листа доступен только для DXF")
            request = DxfViewRequest.model_validate(handler.body())
            from ..dxf_workspace_worker import run_dxf_view
            handler.send_json(run_dxf_view(content, **request.model_dump()))
        else:
            if kind != "step":
                raise ValueError("Сечения доступны только для STEP")
            request = SectionRequest.model_validate(handler.body())
            from ..step_worker import run_step_section
            if request.component_ids is not None:
                if len(request.component_ids) != len(set(request.component_ids)):
                    raise ValueError("Номера тел сечения не должны повторяться")
                request.component_ids.sort()
            cache_key = "section:" + source_id + ":" + request.model_dump_json()
            result = _cache_get(cache_key)
            if result is None:
                result = run_step_section(content, **request.model_dump())
                _cache_put(cache_key, result)
            if operation == "section.dxf":
                from ..step_geometry import section_dxf
                handler.send_bytes(section_dxf(result), "image/vnd.dxf", f"section-{source_id[:12]}-{request.axis}.dxf")
            elif operation == "section-snapshot":
                from ..cad_sections import make_section_snapshot
                handler.send_json(make_section_snapshot(source_id, request.model_dump(), result))
            else:
                handler.send_json(result)
        return True
    finally:
        CAD_SLOTS.release()
