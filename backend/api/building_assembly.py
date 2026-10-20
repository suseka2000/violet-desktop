"""Saved project IDs only; geometry stays in background jobs and streamed files."""
import re
import shutil
from urllib.parse import parse_qs, urlsplit

from pydantic import Field
from ..domain.models import Identifier, Model


class BuildingRequest(Model):
    project_id: Identifier
    building_id: Identifier
    expected_revision: int = Field(strict=True, ge=0)


class CancelRequest(Model):
    expected_revision: int = Field(strict=True, ge=0)


def handle_building_assembly(handler, method, path, jobs):
    from .server import HttpError
    if path == "/api/v1/building-assembly/start":
        if method != "POST":
            raise HttpError(405, "method_not_allowed", "Используйте POST")
        value = BuildingRequest.model_validate(handler.body())
        handler.send_json(jobs.enqueue(value.project_id, value.building_id, value.expected_revision), 202)
        return True
    match = re.fullmatch(r"/api/v1/building-assembly/([0-9a-f]{32})(?:/(export\.step|cancel|preview/([A-Za-z0-9_-]{1,128})))?", path)
    if match is None:
        return False
    identifier, action, chunk_id = match.groups()
    if action == "cancel":
        if method != "POST":
            raise HttpError(405, "method_not_allowed", "Используйте POST")
        value = CancelRequest.model_validate(handler.body())
        handler.send_json(jobs.cancel(identifier, value.expected_revision))
        return True
    if method != "GET":
        raise HttpError(405, "method_not_allowed", "Используйте GET")
    if action == "export.step":
        query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
        if set(query) - {"view"} or len(query.get("view", ["complete"])) != 1 or query.get("view", ["complete"])[0] not in {"complete", "nodes"}:
            raise ValueError("Неизвестный режим экспорта здания")
        nodes = query.get("view", ["complete"])[0] == "nodes"
        stream, size = jobs.open_export(identifier, nodes=nodes)
        with stream:
            handler.headers_for(200, "application/step", size, "building-nodes.step" if nodes else "building-finish.step")
            shutil.copyfileobj(stream, handler.wfile, length=1024 * 1024)
    elif chunk_id:
        handler.send_json(jobs.preview(identifier, chunk_id))
    else:
        handler.send_json(jobs.status(identifier))
    return True
