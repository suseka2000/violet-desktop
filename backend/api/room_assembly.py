"""No geometry payload or filesystem paths are accepted from the browser."""
import re
import shutil
from urllib.parse import parse_qs, urlsplit

from pydantic import Field
from ..domain.models import Identifier, Model


class RoomRequest(Model):
    project_id: Identifier
    space_id: Identifier
    expected_revision: int = Field(strict=True,ge=0)


def handle_room_assembly(handler,method,path,jobs):
    if path=="/api/v1/room-assembly/start":
        if method!="POST":
            from .server import HttpError
            raise HttpError(405,"method_not_allowed","Используйте POST")
        value=RoomRequest.model_validate(handler.body())
        handler.send_json(jobs.enqueue(value.project_id,value.space_id,value.expected_revision),202)
        return True
    match=re.fullmatch(r"/api/v1/room-assembly/([0-9a-f]{32})(?:/(export\.step|cancel|preview/([A-Za-z0-9_-]{1,128})))?",path)
    if match is None:return False
    identifier,action,chunk_id=match.groups()
    if action=="cancel":
        from .building_assembly import CancelRequest
        if method!="POST":
            from .server import HttpError
            raise HttpError(405,"method_not_allowed","Используйте POST")
        value=CancelRequest.model_validate(handler.body())
        handler.send_json(jobs.cancel(identifier,value.expected_revision))
        return True
    if method!="GET":
        from .server import HttpError
        raise HttpError(405,"method_not_allowed","Используйте GET")
    if action=="export.step":
        query=parse_qs(urlsplit(handler.path).query)
        if set(query)-{"view"} or len(query.get("view",["complete"]))!=1 or query.get("view",["complete"])[0] not in {"complete","nodes"}:
            raise ValueError("Неизвестный режим экспорта помещения")
        nodes=query.get("view",["complete"])[0]=="nodes"
        if hasattr(jobs,"open_export"):
            stream,size=jobs.open_export(identifier,nodes=nodes)
            with stream:
                handler.headers_for(200,"application/step",size,"room-nodes.step" if nodes else "room-finish.step")
                shutil.copyfileobj(stream,handler.wfile,length=1024*1024)
        else:
            handler.send_bytes(jobs.export(identifier,nodes=nodes),"application/step","room-nodes.step" if nodes else "room-finish.step")
    elif chunk_id:
        handler.send_json(jobs.preview(identifier,chunk_id))
    else:
        handler.send_json(jobs.status(identifier))
    return True
