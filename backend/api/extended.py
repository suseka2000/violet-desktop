"""Import and document routes, separated from project persistence."""
from __future__ import annotations

import base64
from functools import lru_cache
from threading import RLock
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

from pydantic import Field

from ..domain.models import Model, Name, Positive, Project
from ..workspace import Calibration, EditorGuides, SourceMarkup

BUILD_LOCK = RLock()


class InspectRequest(Model):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    content_base64: Annotated[str, Field(min_length=1, max_length=12 * 1024 * 1024)]
    units_override: Annotated[str, Field(max_length=8)] | None = None


class ConvertRequest(Model):
    handles: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(min_length=1, max_length=1000)
    layer: Annotated[str, Field(max_length=255)] | None = None
    units_override: Annotated[str, Field(max_length=8)] | None = None
    project_name: Name = "Импорт DXF"
    building_name: Name = "Здание"
    floor_name: Name = "Этаж 1"
    room_height_mm: Positive = 3000
    wall_thickness_mm: Positive = 150


class CatalogRequest(Model):
    catalog: dict


class VendorReviewRequest(Model):
    project: Project


@lru_cache(maxsize=4)
def _report(snapshot: str):
    from ..engineering import build_engineering
    project = Project.model_validate_json(snapshot)
    return build_engineering(project, catalog=project.properties.get("node_catalog"))


@lru_cache(maxsize=2)
def _documents(snapshot: str, paper_format: str, derived_assemblies=False):
    from ..documents import build_documents
    return build_documents(Project.model_validate_json(snapshot), paper_format=paper_format, report=_report(snapshot),
                           derived_assemblies=derived_assemblies)


def handle_extended(handler, method, path, repository, service, source_store, *, project_overlay=None):
    """Return False when this route belongs to the original adapter."""
    from ..source_worker import inspect_isolated as inspect_source, convert_isolated as convert_dxf
    from ..engineering import catalog_schema, validate_catalog
    from ..documents.catalog import preflight_catalog
    from .server import project_result

    parts = path.strip("/").split("/")
    query = parse_qs(urlsplit(handler.path).query)
    if path == "/api/v1/vendor-design/review" and method == "POST":
        from ..vendor_design import build_vendor_report, validate_vendor_design
        request = VendorReviewRequest.model_validate(handler.body())
        validation = validate_vendor_design(request.project)
        if not validation["valid"]:
            raise ValueError("Некорректные ссылки на материалы: " + str(validation["issues"][:3]))
        handler.send_json(build_vendor_report(request.project, []))
        return True
    if path == "/api/v1/sources/capabilities" and method == "GET":
        from ..importing import import_capabilities
        handler.send_json(import_capabilities())
        return True
    if path == "/api/v1/node-catalog/schema" and method == "GET":
        handler.send_json(catalog_schema())
        return True
    if path == "/api/v1/node-catalog/validate" and method == "POST":
        request = CatalogRequest.model_validate(handler.body())
        handler.send_json(preflight_catalog(request.catalog))
        return True
    if path == "/api/v1/source-markup/schema" and method == "GET":
        handler.send_json(SourceMarkup.model_json_schema())
        return True
    if path == "/api/v1/editor-guides/schema" and method == "GET":
        handler.send_json(EditorGuides.model_json_schema())
        return True
    if path == "/api/v1/sources/inspect" and method == "POST":
        request = InspectRequest.model_validate(handler.body())
        try:
            content = base64.b64decode(request.content_base64, validate=True)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Некорректная кодировка файла") from exc
        inspection = inspect_source(request.filename, content, units_override=request.units_override)
        record = source_store.put(request.filename, content)
        handler.send_json({"source_id": record.source_id, "source": record.model_dump(mode="json"), "inspection": inspection}, 201)
        return True
    if len(parts) in {4, 5} and parts[:3] == ["api", "v1", "sources"]:
        source_id = parts[3]
        record = source_store.get_record(source_id)
        action = parts[4] if len(parts) == 5 else ""
        if method == "GET" and action == "":
            handler.send_json({"source": record.model_dump(mode="json"), "inspection": inspect_source(record.filename, source_store.get_bytes(source_id), units_override=query.get("units_override", [None])[0])})
            return True
        if method == "GET" and action == "preview.png":
            from ..source_preview import preview_bytes
            result = preview_bytes(source_store.get_bytes(source_id), page=int(query.get("page", ["1"])[0]))
            handler.send_bytes(result, "image/png")
            return True
        if method == "GET" and action == "original":
            handler.send_bytes(source_store.get_bytes(source_id), "application/octet-stream", f"source-{source_id[:12]}")
            return True
        if method == "POST" and action == "convert":
            request = ConvertRequest.model_validate(handler.body())
            result = convert_dxf(source_store.get_bytes(source_id), filename=record.filename, **request.model_dump())
            project = service.save(result.project, expected_revision=0, message="Импорт выбранных контуров DXF")
            handler.send_json({**project_result(project), "import_report": result.report.model_dump(mode="json"), "source_id": source_id}, 201)
            return True
    if len(parts) >= 5 and parts[:3] == ["api", "v1", "projects"] and method == "GET":
        action = parts[4]
        if action not in {"engineering", "documents.pdf", "documents.dxf", "documents.zip", "sheets"}:
            return False
        revision = query.get("revision", [None])[0]
        if revision is not None:
            revision = int(revision)
            if revision < 1:
                raise ValueError("Версия должна быть положительным целым числом")
        project = repository.get(parts[3], revision=revision)
        if project_overlay is not None:
            project = project_overlay(project, historical=revision is not None)
        snapshot = project.model_dump_json()
        with BUILD_LOCK:
            if action == "engineering" and len(parts) == 5:
                handler.send_json(_report(snapshot))
                return True
            # Keep the established API default for integrations; the editor
            # explicitly requests ALBUM for the new compact document layout.
            paper = query.get("format", ["A3"])[0]
            documents = _documents(snapshot, paper, derived_assemblies=project_overlay is not None)
        if action == "documents.pdf" and len(parts) == 5:
            handler.send_bytes(documents.pdf(), "application/pdf", f"rd-{project.id}-r{project.revision}-{paper}.pdf")
            return True
        if action == "documents.dxf" and len(parts) == 5:
            handler.send_bytes(documents.dxf(), "application/dxf", f"rd-{project.id}-r{project.revision}-{paper}.dxf")
            return True
        if action == "documents.zip" and len(parts) == 5:
            handler.send_bytes(documents.package(), "application/zip", f"rd-{project.id}-r{project.revision}-{paper}.zip")
            return True
        if action == "sheets":
            if len(parts) == 5:
                handler.send_json(documents.index())
                return True
            if len(parts) == 6 and parts[5].endswith(".svg"):
                number = int(parts[5][:-4])
                if not 1 <= number <= len(documents.sheets):
                    raise ValueError("Лист не найден")
                handler.send_bytes(documents.sheets[number - 1].svg(), "image/svg+xml")
                return True
    return False
