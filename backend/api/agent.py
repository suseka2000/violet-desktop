"""Loopback-only agent routes. Secrets are write-only, edits are proposals."""
import base64
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator
from ..domain.models import Model, Project
from ..agent.file_reader import EXTENSIONS, MAX_BYTES
from ..agent.navigation import DESTINATIONS

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class HistoryMessage(Model):
    role: Literal["user", "assistant"]
    text: Annotated[str, Field(max_length=6000)]


class RunRequest(Model):
    project: Project
    message: Annotated[str, Field(min_length=1, max_length=12000)]
    network_consent: Literal[True]
    allow_library: bool = Field(default=True, strict=True)
    source_ids: list[Digest] = Field(default_factory=list, max_length=30)
    selection: dict = Field(default_factory=dict)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def bounded_context(self):
        import json
        if len(json.dumps(self.selection)) > 16000:
            raise ValueError("Контекст выбора слишком большой")
        return self


class UploadRequest(Model):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    content_base64: Annotated[str, Field(min_length=1, max_length=MAX_BYTES * 4 // 3 + 8)]


class ProposalRequest(Model):
    project: Project


def handle_agent(handler, method, path, runs):
    if not path.startswith("/api/v1/agent/"):
        return False
    from .server import HttpError
    if path == "/api/v1/agent/settings":
        if method == "GET":
            handler.send_json(runs.settings.public())
        elif method == "PUT":
            handler.send_json(runs.settings.update(handler.body()))
        else:
            raise HttpError(405, "method_not_allowed", "Используйте GET или PUT")
        return True
    if path == "/api/v1/agent/help" and method == "GET":
        handler.send_json({"destinations": [{"target": k, "label": v[0], "instructions": v[1]} for k, v in DESTINATIONS.items()]})
        return True
    if path == "/api/v1/agent/files" and method == "POST":
        request = UploadRequest.model_validate(handler.body())
        if Path(request.filename).suffix.lower() not in EXTENSIONS:
            raise ValueError("Прикрепите PDF, изображение, текст, JSON, CSV, DOCX, XLSX, DWG/DXF или STEP")
        try:
            content = base64.b64decode(request.content_base64, validate=True)
        except ValueError:
            raise ValueError("Повреждённое вложение") from None
        if not 0 < len(content) <= MAX_BYTES:
            raise ValueError("Вложение должно быть не пустым и не больше 32 МиБ")
        record = runs.source_store.put(request.filename, content)
        handler.send_json({"source_id": record.source_id, "filename": record.filename,
                           "bytes": record.byte_length, "uploaded_to_openai": False}, 201)
        return True
    if path == "/api/v1/agent/runs" and method == "POST":
        request = RunRequest.model_validate(handler.body())
        handler.send_json(runs.start(request), 202)
        return True
    parts = path.strip("/").split("/")
    if len(parts) >= 5 and parts[:4] == ["api", "v1", "agent", "runs"]:
        run = runs.get(parts[4])
        if len(parts) == 5 and method == "GET":
            handler.send_json(run.public())
            return True
        if len(parts) == 6 and parts[5] == "cancel" and method == "POST":
            Model.model_validate(handler.body())
            handler.send_json(runs.cancel(run.id))
            return True
        if len(parts) == 6 and parts[5] == "proposal":
            if method == "GET":
                handler.send_json(runs.proposal(run.id))
            elif method == "POST":
                current = ProposalRequest.model_validate(handler.body())
                handler.send_json(runs.proposal(run.id, current.project))
            else:
                raise HttpError(405, "method_not_allowed", "Используйте GET или POST")
            return True
        if len(parts) == 7 and parts[5] == "preview" and method == "GET":
            image = run.context.previews.get(parts[6])
            if image is None:
                raise HttpError(404, "not_found", "Предпросмотр не найден")
            handler.send_bytes(image, "image/svg+xml; charset=utf-8")
            return True
    raise HttpError(404, "not_found", "Команда агента не найдена")
