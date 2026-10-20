"""Explicit local STEP open, using the same worker and store as CAD upload."""
from urllib.parse import parse_qs, urlsplit

from ..model_library import ModelLibraryError


def handle_model_library(handler, method, path, library, source_store):
    if path != "/api/v1/model-library" and not path.startswith("/api/v1/model-library/"):
        return False
    try:
        try:
            query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True, max_num_fields=4)
        except ValueError as exc:
            raise ModelLibraryError("Слишком много параметров библиотеки") from exc
        if any(len(values) != 1 for values in query.values()):
            raise ModelLibraryError("Параметр запроса указан несколько раз")
        if path == "/api/v1/model-library":
            if method != "GET":
                raise ModelLibraryError("Список моделей доступен только для чтения", code="method_not_allowed", status=405)
            if set(query) - {"q", "format"}:
                raise ModelLibraryError("Неизвестный фильтр библиотеки моделей")
            handler.send_json(library.inventory(q=query.get("q", [""])[0], format=query.get("format", [None])[0] or None))
            return True
        parts = path.strip("/").split("/")
        if len(parts) != 5 or parts[4] != "open":
            raise ModelLibraryError("Операция библиотеки не найдена", code="model_not_found", status=404)
        if method != "POST":
            raise ModelLibraryError("Открытие модели требует POST", code="method_not_allowed", status=405)
        if query or handler.body() != {}:
            raise ModelLibraryError("Открытие модели не принимает пути, имена файлов или другие параметры")
        from .cad_assets import CAD_SLOTS, _inspect
        if not CAD_SLOTS.acquire(blocking=False):
            raise ModelLibraryError("Заняты обработчики CAD. Дождитесь завершения текущей операции.", code="cad_busy", status=429)
        try:
            content, asset = library.original(parts[3])
            inspection = library.cached_inspection(asset["id"])
            if inspection is None:
                inspection = _inspect(content, asset["filename"])
                library.remember_inspection(asset["id"], inspection)
            inspection = {**inspection, "filename": asset["filename"], "format": "step",
                          "cad_workspace": True, "byte_length": len(content)}
            record = source_store.put(asset["filename"], content)
            handler.send_json({"source_id": record.source_id, "source": record.model_dump(mode="json"),
                               "inspection": inspection, "library_asset": asset}, 201)
        finally:
            CAD_SLOTS.release()
    except ModelLibraryError as error:
        handler.send_error_json(error.status, error.code, str(error))
    return True
