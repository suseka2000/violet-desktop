"""Read-only lightweight presentation; never enqueues assembly jobs."""
from ..finish_presentation import PreviewRequest, build_finish_presentation


def handle_finish_presentation(handler, method, path):
    if path != "/api/v1/finish-presentation/preview":
        return False
    if method != "POST":
        from .server import HttpError
        raise HttpError(405, "method_not_allowed", "Предпросмотр принимает POST")
    request = PreviewRequest.model_validate(handler.body())
    handler.send_json(build_finish_presentation(request.project, request.scope))
    return True
