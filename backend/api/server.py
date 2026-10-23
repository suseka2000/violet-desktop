"""Local development adapter with no server dependencies beyond the domain.

    python -m backend serve --host 127.0.0.1 --port 8310

Only loopback binding is allowed: authentication and remote deployment belong
to a separate adapter. No request can provide a filesystem path.
"""
from __future__ import annotations

import gzip
import hmac
import json
import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError

from ..domain.models import (Identifier, Model, Name, Number, Opening, Positive,
                             Project, ValidationReport)
from ..geometry.validation import validate_project
from ..service import EntityNotFoundError, InvalidProjectError, ProjectService
from ..storage.repository import ConflictError, NotFoundError, Repository

ROOT = Path(__file__).resolve().parents[2]
# Complete-wall records include a measured result for each installed fastener.
# Source-file endpoints still enforce their own independent import limits.
MAX_BODY = 56 * 1024 * 1024
Revision = Annotated[int, Field(strict=True, ge=0)]


def accepts_gzip(header: str) -> bool:
    """Honor explicit gzip;q=0 even when a wildcard allows other encodings."""
    weights = {}
    for entry in header.lower().split(","):
        token, *parameters = entry.strip().split(";")
        quality = 1.
        for parameter in parameters:
            key, separator, value = parameter.strip().partition("=")
            if key == "q" and separator:
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.
        weights[token] = quality if 0 <= quality <= 1 else 0.
    return weights.get("gzip", weights.get("*", 0.)) > 0


class CreateRequest(Model):
    name: Name
    address: str = ""
    author: str = ""


class ProjectRequest(Model):
    project: Project


class SaveRequest(ProjectRequest):
    expected_revision: Revision
    message: Annotated[str, Field(max_length=1000)] = "Изменение проекта"
    defer_assemblies: bool = Field(default=False, strict=True)


class BuildingRequest(Model):
    expected_revision: Revision
    name: Name


class FloorRequest(BuildingRequest):
    building_id: Identifier
    elevation_mm: Number = 0
    default_height_mm: Positive = 3000


class RoomRequest(Model):
    expected_revision: Revision
    floor_id: Identifier
    code: Name
    name: Name
    x: Number = 0
    y: Number = 0
    width_mm: Positive
    depth_mm: Positive
    height_mm: Positive | None = None
    thickness_mm: Positive = 150


class OpeningRequest(Model):
    expected_revision: Revision
    floor_id: Identifier
    opening: Opening


class RestoreRequest(Model):
    expected_revision: Revision
    source_revision: Annotated[int, Field(strict=True, ge=1)]


class RevisionRequest(Model):
    expected_revision: Revision


class TrashProjectRequest(Model):
    id: Identifier
    revision: Revision


class EmptyTrashRequest(Model):
    confirmation: Literal["empty-project-trash"]
    projects: Annotated[list[TrashProjectRequest], Field(max_length=10000)]


class AssemblyJobRequest(RevisionRequest):
    force: bool = Field(default=False, strict=True)


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def reject_nonfinite(value: str):
    raise ValueError(f"Недопустимое числовое значение JSON: {value}")


def project_result(project: Project) -> dict:
    return {"project": project.model_dump(mode="json"),
            "validation": validate_project(project).model_dump(mode="json")}


def build_server(db_path: str | Path, *, host: str = "127.0.0.1", port: int = 8310,
                 assets_root: str | Path | None = None, desktop_token: str | None = None) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Этот локальный адаптер поддерживает только 127.0.0.1 или localhost")
    root = Path(assets_root).resolve() if assets_root is not None else ROOT
    if desktop_token is not None and (not 32 <= len(desktop_token) <= 512 or not desktop_token.isascii()):
        raise ValueError("Некорректный ключ настольного сеанса")
    repository = Repository(db_path)
    service = ProjectService(repository)
    from ..importing import SourceStore
    from ..importing._common import MAX_CAD_SOURCE_BYTES
    source_store = SourceStore(Path(db_path).resolve().parent / "sources", scope="local", max_bytes=MAX_CAD_SOURCE_BYTES)
    from ..vendor_library import VendorLibrary
    vendor_library = VendorLibrary(root / "for_interior-rd-generator", facts_paths=[
        root / "resources" / "vendor" / "manual-facts.json",
        root / "resources" / "vendor" / "profile-facts.json",
        root / "resources" / "vendor" / "public-installation-facts.json",
    ], drawing_facts_path=root / "resources" / "vendor" / "reference-drawings.json")
    from ..model_library import ModelLibrary
    model_library = ModelLibrary(vendor_library.root, cache_dir=Path(db_path).resolve().parent / "model-inspections")
    from ..assembly_jobs import AssemblyJobs, preserve_framing_settings
    assembly_jobs = AssemblyJobs(repository, model_library, source_store, start=False)
    from ..stream_room_assembly_jobs import StreamRoomAssemblyJobs as RoomAssemblyJobs
    room_assembly_jobs = RoomAssemblyJobs(repository, assembly_jobs, start=False)
    from ..building_assembly_jobs import BuildingAssemblyJobs
    building_assembly_jobs = BuildingAssemblyJobs(repository, assembly_jobs, start=False)

    class Handler(BaseHTTPRequestHandler):
        server_version = "InteriorRD/0.3"

        def log_message(self, fmt, *args):
            logging.getLogger("interior_rd.http").info(fmt, *args)

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

        def do_PUT(self):
            self.handle_request("PUT")

        def do_DELETE(self):
            self.handle_request("DELETE")

        def do_OPTIONS(self):
            self.send_error_json(405, "method_not_allowed", "Метод не поддерживается")

        def headers_for(self, status, content_type, length, attachment=None, encoding=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if content_type.startswith("application/json"):
                self.send_header("Vary", "Accept-Encoding")
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.end_headers()

        def send_json(self, value, status=200, attachment=None):
            # CAD evidence contains many repeated, numeric fields. Pretty JSON
            # more than doubled the project response (104 MB on my_house).
            # Preserve readable downloaded JSON; keep interactive transport small.
            data = (json.dumps(value, ensure_ascii=False, allow_nan=False,
                               **({"indent": 2} if attachment else {"separators": (",", ":")})) + "\n").encode("utf-8")
            encoding = None
            if len(data) >= 4096 and accepts_gzip(self.headers.get("Accept-Encoding", "")):
                compressed = gzip.compress(data, compresslevel=1, mtime=0)
                if len(compressed) < len(data):
                    data, encoding = compressed, "gzip"
            self.headers_for(status, "application/json; charset=utf-8", len(data), attachment, encoding)
            self.wfile.write(data)

        def send_bytes(self, data, content_type, attachment=None):
            self.headers_for(200, content_type, len(data), attachment)
            self.wfile.write(data)

        def send_error_json(self, status, code, message, details=None):
            error = {"code": code, "message": message}
            if details is not None:
                error["details"] = details
            self.send_json({"error": error}, status)

        def check_origin(self):
            port = self.server.server_address[1]
            allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host", "") not in allowed_hosts:
                raise HttpError(403, "invalid_host", "Используйте локальный адрес программы")
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{h}" for h in allowed_hosts}:
                raise HttpError(403, "invalid_origin", "Запрос из другого сайта запрещён")
            if desktop_token is not None:
                supplied = self.headers.get("X-Violet-Desktop-Token", "")
                if not supplied.isascii() or not hmac.compare_digest(supplied, desktop_token):
                    raise HttpError(401, "desktop_auth_required", "Недействительный локальный сеанс приложения")

        def body(self):
            if self.headers.get_content_type() != "application/json":
                raise HttpError(415, "json_required", "Ожидается Content-Type: application/json")
            if self.headers.get("Transfer-Encoding"):
                raise HttpError(400, "invalid_encoding", "Требуется Content-Length")
            try:
                size = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                raise HttpError(400, "invalid_length", "Некорректный Content-Length")
            if size < 0:
                raise HttpError(411, "length_required", "Требуется Content-Length")
            if size > MAX_BODY:
                raise HttpError(413, "body_too_large", "JSON-запрос превышает 56 МиБ; ограничения размера импортируемых файлов действуют отдельно")
            self.connection.settimeout(15)
            data = self.rfile.read(size)
            if len(data) != size:
                raise HttpError(400, "incomplete_body", "Получено неполное тело запроса")
            try:
                result = json.loads(data.decode("utf-8"), parse_constant=reject_nonfinite)
            except (ValueError, UnicodeError, RecursionError):
                raise HttpError(400, "invalid_json", "Некорректный JSON")
            if not isinstance(result, dict):
                raise HttpError(400, "object_required", "JSON должен быть объектом")
            return result

        def handle_request(self, method):
            counted = False
            try:
                self.check_origin()
                path = urlsplit(self.path).path
                if desktop_token is not None and path == "/api/v1/desktop/health" and method == "GET":
                    self.send_json({"status": "ok", "protocol": 1,
                                    "active_jobs": self.server.desktop_active_jobs(),
                                    "closing": self.server.desktop_closing.is_set()})
                    return
                if desktop_token is not None and path == "/api/v1/desktop/shutdown" and method == "POST":
                    body = self.body()
                    if set(body) - {"require_idle"} or ("require_idle" in body and type(body["require_idle"]) is not bool):
                        raise HttpError(422, "invalid_input", "Допускается только логическое поле require_idle")
                    if not self.server.begin_desktop_shutdown(require_idle=body.get("require_idle", False)):
                        raise HttpError(409, "desktop_busy", "Расчёт или сохранение ещё выполняются. Обновление отложено.")
                    try:
                        self.send_json({"status": "closing"})
                    finally:
                        self.server.request_desktop_shutdown()
                    return
                if desktop_token is not None:
                    # Admission and idle-update quiescing share a lock. A new
                    # save/build cannot enter after the updater observed idle.
                    with self.server.desktop_request_lock:
                        if self.server.desktop_closing.is_set():
                            raise HttpError(503, "desktop_closing", "Приложение завершает работу")
                        if path.startswith("/api/"):
                            self.server.desktop_requests += 1
                            counted = True
                if method == "GET" and path in {"/", "/index.html", "/app.js", "/engineering-jobs.js", "/project-history.js", "/styles.css", "/model-editor.js", "/snapping.js", "/drafting.js", "/cad-mapping.js", "/vendor-library.js", "/cad-workspace.js", "/draft-recovery.js", "/editor-cad.css", "/wall-mount.js", "/wall-mount.css", "/batch-mount.js", "/batch-mount.css", "/mount-sections.js", "/mount-sections.css", "/mount-assemblies.js", "/mount-assemblies.css", "/special-assemblies.js", "/special-assemblies.css", "/whole-wall.js", "/whole-wall.css", "/room-assembly.js", "/room-assembly.css", "/building-assembly.js", "/building-assembly.css", "/finish-viewer.js", "/finish-presentation.js", "/finish-presentation.css"}:
                    filename = "index.html" if path == "/" else path[1:]
                    resource = root / "frontend" / filename
                    if not resource.is_file():
                        raise HttpError(404, "not_found", "Интерфейс не найден")
                    data = resource.read_bytes()
                    mime = mimetypes.guess_type(resource.name)[0] or "application/octet-stream"
                    self.headers_for(200, mime + "; charset=utf-8", len(data))
                    self.wfile.write(data)
                    return
                if path == "/api/v1/health" and method == "GET":
                    self.send_json({"status": "ok", "version": "0.3.0", "schema_version": "1.0"})
                    return
                if path == "/api/v1/schema" and method == "GET":
                    schema = Project.model_json_schema()
                    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
                    self.send_json(schema)
                    return
                if path == "/api/v1/projects":
                    if method == "GET":
                        self.send_json({"projects": repository.list_projects()})
                    elif method == "POST":
                        request = CreateRequest.model_validate(self.body())
                        self.send_json(project_result(service.create(**request.model_dump())), 201)
                    else:
                        raise HttpError(405, "method_not_allowed", "Метод не поддерживается")
                    return
                if path == "/api/v1/projects/trash":
                    if method == "GET":
                        self.send_json({"projects": repository.list_trash()})
                    elif method == "DELETE":
                        request = EmptyTrashRequest.model_validate(self.body())
                        try:
                            result = repository.empty_trash([item.model_dump() for item in request.projects])
                        except ConflictError:
                            raise HttpError(409, "trash_changed", "Состав корзины изменился. Обновите её и подтвердите очистку заново.")
                        from ..project_trash import purge_project_caches
                        result["cache_cleanup"] = purge_project_caches(result["project_ids"], assembly_jobs, room_assembly_jobs, building_assembly_jobs)
                        self.send_json(result)
                    else:
                        raise HttpError(405, "method_not_allowed", "Метод не поддерживается")
                    return
                if path == "/api/v1/demo" and method == "POST":
                    Model.model_validate(self.body())
                    self.send_json(project_result(service.demo()), 201)
                    return
                if path in {"/api/v1/import", "/api/v1/validate"} and method == "POST":
                    request = ProjectRequest.model_validate(self.body())
                    from ..workspace import validate_workspace
                    validate_workspace(request.project)
                    from ..workspace import validate_source_references
                    validate_source_references(request.project, source_store)
                    if path.endswith("validate"):
                        self.send_json(validate_project(request.project).model_dump(mode="json"))
                    else:
                        self.send_json(project_result(service.import_copy(request.project)), 201)
                    return
                from .vendor import handle_vendor
                if handle_vendor(self, method, path, vendor_library):
                    return
                from .model_library import handle_model_library
                if handle_model_library(self, method, path, model_library, source_store):
                    return
                from .mount_sections import handle_mount_sections
                if handle_mount_sections(self, method, path, model_library, source_store):
                    return
                from .special_assemblies import handle_special_assemblies
                if handle_special_assemblies(self, method, path, model_library, source_store):
                    return
                from .whole_wall import handle_whole_wall
                if handle_whole_wall(self, method, path, model_library, source_store, geometry_cache=assembly_jobs.wall_geometry_cache):
                    return
                from .finish_presentation import handle_finish_presentation
                if handle_finish_presentation(self, method, path):
                    return
                from .room_assembly import handle_room_assembly
                if handle_room_assembly(self, method, path, room_assembly_jobs):
                    return
                from .building_assembly import handle_building_assembly
                if handle_building_assembly(self, method, path, building_assembly_jobs):
                    return
                from .mount_assemblies import handle_mount_assemblies
                if handle_mount_assemblies(self, method, path, model_library, source_store):
                    return
                from .wall_mount import handle_wall_mount
                if handle_wall_mount(self, method, path, vendor_library, model_library):
                    return
                from .cad_assets import handle_cad_assets
                if handle_cad_assets(self, method, path, source_store):
                    return
                from .extended import handle_extended
                if handle_extended(self, method, path, repository, service, source_store, project_overlay=assembly_jobs.for_documents):
                    return
                parts = path.strip("/").split("/")
                if len(parts) in {4, 5} and parts[:3] == ["api", "v1", "projects"]:
                    project_id = parts[3]
                    action = parts[4] if len(parts) == 5 else ""
                    if action in {"assembly-job", "assembly-result"}:
                        project = repository.get(project_id)
                        if method == "GET":
                            self.send_json(assembly_jobs.status(project) if action == "assembly-job" else assembly_jobs.result(project))
                        elif method == "POST" and action == "assembly-job":
                            request = AssemblyJobRequest.model_validate(self.body())
                            if request.expected_revision != project.revision:
                                raise ConflictError(project.id, request.expected_revision, project.revision)
                            self.send_json(assembly_jobs.enqueue(project, seed_revision=project.revision, force=request.force), 202)
                        else:
                            raise HttpError(405, "method_not_allowed", "Метод не поддерживается")
                        return
                    if method == "DELETE" and action == "":
                        request = RevisionRequest.model_validate(self.body())
                        result = repository.delete(project_id, expected_revision=request.expected_revision)
                        assembly_jobs.cancel(project_id)
                        self.send_json(result)
                        return
                    if method == "POST" and action == "undelete":
                        request = RevisionRequest.model_validate(self.body())
                        result = repository.undelete(project_id, expected_revision=request.expected_revision)
                        status = assembly_jobs.enqueue(result, seed_revision=result.revision)
                        self.send_json({**project_result(assembly_jobs.overlay(result)), "engineering": status})
                        return
                    if method == "GET":
                        if action == "":
                            project = repository.get(project_id)
                            # The overlay and its status must describe the same
                            # checkpoint before the client skips a second large
                            # assembly-result download on opening a project.
                            with assembly_jobs._lock:
                                visible = assembly_jobs.overlay(project)
                                status = assembly_jobs.status(project)
                                response = {**project_result(visible), "engineering": status,
                                            "assemblies_included": status["status"] in {"ready", "partial"}
                                            and all(key in visible.properties for key in ("mount_assemblies", "whole_wall_assemblies"))}
                            self.send_json(response)
                        elif action == "history":
                            self.send_json({"history": repository.history(project_id)})
                        elif action == "export":
                            project = assembly_jobs.overlay(repository.get(project_id))
                            self.send_json(project.model_dump(mode="json"), attachment=f"project-{project.id}.json")
                        else:
                            raise HttpError(404, "not_found", "Адрес не найден")
                        return
                    if method == "PUT" and action == "":
                        request = SaveRequest.model_validate(self.body())
                        if request.project.id != project_id:
                            raise HttpError(422, "project_id_mismatch", "ID проекта не совпадает с адресом запроса")
                        previous = repository.get(project_id)  # PUT cannot silently create.
                        from ..workspace import validate_source_references
                        candidate = preserve_framing_settings(request.project) if request.defer_assemblies else request.project
                        validate_source_references(candidate, source_store)
                        result = service.save(candidate, expected_revision=request.expected_revision, message=request.message)
                        response = project_result(result)
                        if request.defer_assemblies:
                            response["engineering"] = assembly_jobs.enqueue(result, seed_revision=previous.revision)
                        self.send_json(response)
                        return
                    requests = {"buildings": (BuildingRequest, service.add_building),
                                "floors": (FloorRequest, service.add_floor),
                                "rooms": (RoomRequest, service.add_room),
                                "openings": (OpeningRequest, service.add_opening),
                                "restore": (RestoreRequest, service.restore)}
                    if method == "POST" and action in requests:
                        cls, operation = requests[action]
                        request = cls.model_validate(self.body())
                        kwargs = {key: getattr(request, key) for key in cls.model_fields}
                        result = operation(project_id=project_id, **kwargs)
                        status = assembly_jobs.enqueue(result, seed_revision=result.revision)
                        self.send_json({**project_result(assembly_jobs.overlay(result)), "engineering": status})
                        return
                raise HttpError(404, "not_found", "Адрес не найден")
            except HttpError as error:
                self.send_error_json(error.status, error.code, error.message)
            except ValidationError as error:
                details = [{"path": "/" + "/".join(map(str, e["loc"])), "message": e["msg"], "code": e["type"]}
                           for e in error.errors(include_url=False, include_input=False)]
                self.send_error_json(422, "invalid_document", "Проверьте формат и значения проекта", details)
            except InvalidProjectError as error:
                self.send_error_json(422, "invalid_geometry", str(error), error.report.model_dump(mode="json"))
            except ConflictError as error:
                self.send_error_json(409, "revision_conflict",
                    "Проект уже изменён. Загрузите актуальную версию и перенесите в неё свои правки.",
                    {"expected_revision": error.expected_revision, "actual_revision": error.actual_revision})
            except (NotFoundError, EntityNotFoundError) as error:
                self.send_error_json(404, "not_found", str(error))
            except FileNotFoundError:
                self.send_error_json(404, "source_not_found", "Исходный файл не найден в локальном хранилище")
            except ValueError as error:
                self.send_error_json(422, getattr(error, "code", "invalid_input"), str(error), getattr(error, "details", None))
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                logging.getLogger("interior_rd.http").debug("Client disconnected")
            except Exception:
                logging.getLogger("interior_rd.http").exception("Request failed")
                self.send_error_json(500, "internal_error", "Не удалось выполнить запрос. Подробности в журнале программы.")
            finally:
                if counted:
                    with self.server.desktop_request_lock:
                        self.server.desktop_requests -= 1

    class LocalServer(ThreadingHTTPServer):
        def desktop_active_jobs(self):
            # Queued work also matters: an update must not silently discard it.
            active = {"queued", "waiting_for_walls", "running"}
            count = 0
            for jobs, table in ((assembly_jobs, "assembly_jobs"), (room_assembly_jobs, "room_jobs"),
                                (building_assembly_jobs, "room_jobs")):
                with jobs._connection() as connection:
                    count += sum(json.loads(row[0]).get("status") in active for row in
                                 connection.execute(f"SELECT status_json FROM {table}"))
            from ..desktop_runtime import active_worker_count
            with self.desktop_request_lock:
                return max(count, active_worker_count()) + self.desktop_requests

        def begin_desktop_shutdown(self, *, require_idle=False):
            with self.desktop_request_lock:
                if self.desktop_closing.is_set():
                    return True
                if require_idle and self.desktop_active_jobs():
                    return False
                self.desktop_closing.set()
                return True

        def request_desktop_shutdown(self, *, require_idle=False):
            if not self.begin_desktop_shutdown(require_idle=require_idle):
                return False
            with self.desktop_request_lock:
                if self.desktop_shutdown_started:
                    return True
                self.desktop_shutdown_started = True
            # shutdown() must run outside serve_forever's thread.
            threading.Thread(target=self.shutdown, name="violet-desktop-shutdown", daemon=True).start()
            return True

        def server_close(self):
            building_assembly_jobs.close()
            room_assembly_jobs.close()
            assembly_jobs.close()
            if desktop_token is not None:
                from ..desktop_runtime import stop_workers
                stop_workers()
            super().server_close()
            repository.close()

    try:
        server = LocalServer((host, port), Handler)
    except BaseException:
        building_assembly_jobs.close()
        room_assembly_jobs.close()
        assembly_jobs.close()
        repository.close()
        raise
    server.daemon_threads = True
    server.desktop_closing = threading.Event()
    server.desktop_request_lock = threading.RLock()
    server.desktop_requests = 0
    server.desktop_shutdown_started = False
    server.assembly_jobs = assembly_jobs
    server.room_assembly_jobs = room_assembly_jobs
    server.building_assembly_jobs = building_assembly_jobs
    assembly_jobs.start()
    room_assembly_jobs.start()
    building_assembly_jobs.start()
    return server
