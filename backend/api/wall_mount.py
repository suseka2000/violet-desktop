"""Read-only wall mounting preview against pinned supplier references."""
from ..wall_mount import BatchMountRequest, WallMountRequest, batch_preview_wall_mount, preview_wall_mount, validate_wall_mounting


def handle_wall_mount(handler, method, path, vendor_library, model_library):
    if path not in {"/api/v1/wall-mount/preview","/api/v1/wall-mount/batch-preview"}:
        return False
    from .server import HttpError
    if method != "POST":
        raise HttpError(405, "method_not_allowed", "Предпросмотр принимает POST")
    batch=path.endswith("/batch-preview")
    request = (BatchMountRequest if batch else WallMountRequest).model_validate(handler.body())
    saved = validate_wall_mounting(request.project)
    snapshot = saved.snapshot if saved else vendor_library.snapshot()
    # Both the screen and exported documents use the same pinned model names.
    # A newer live library must never silently alter an older project's output.
    if batch:
        handler.send_json(batch_preview_wall_mount(request.project,request.scope,request.configuration,snapshot,request.replace_existing))
    else:
        handler.send_json(preview_wall_mount(request.project, request.wall_id, request.face_id,request.configuration, snapshot))
    return True
