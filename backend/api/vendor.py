"""Read-only supplier-library HTTP adapter. No path is accepted from a client."""
from urllib.parse import parse_qs, urlsplit

from ..vendor_library import VendorLibraryError, reference_schema


def handle_vendor(handler, method, path, library):
    if path != "/api/v1/vendor-library" and not path.startswith("/api/v1/vendor-library/"):
        return False
    try:
        if method != "GET":
            raise VendorLibraryError("Библиотека доступна только для чтения", status=405, code="method_not_allowed")
        query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True, max_num_fields=10)
        if any(len(values) != 1 for values in query.values()):
            raise VendorLibraryError("Параметр запроса указан несколько раз")
        if path == "/api/v1/vendor-library":
            if set(query) - {"q", "kind", "category"}:
                raise VendorLibraryError("Неизвестный фильтр библиотеки")
            handler.send_json(library.inventory(q=query.get("q", [""])[0], kind=query.get("kind", [None])[0] or None, category=query.get("category", [None])[0] or None))
        elif path == "/api/v1/vendor-library/schema":
            handler.send_json(reference_schema())
        elif path == "/api/v1/vendor-library/snapshot":
            handler.send_json(library.snapshot())
        else:
            parts = path.strip("/").split("/")
            if len(parts) != 5 or parts[4] not in {"original", "preview.png"}:
                raise VendorLibraryError("Адрес материала не найден", status=404, code="vendor_not_found")
            if parts[4] == "original":
                if query:
                    raise VendorLibraryError("Скачивание не принимает параметры")
                content, asset = library.original(parts[3])
                # ASCII download name prevents Content-Disposition injection.
                handler.send_bytes(content, "application/octet-stream", f"vendor-{asset['id'][:12]}.{asset['format']}")
            else:
                if set(query) - {"page"}:
                    raise VendorLibraryError("Неизвестный параметр предпросмотра")
                raw_page = query.get("page", ["1"])[0]
                if not raw_page.isascii() or not raw_page.isdecimal() or len(raw_page) > 3:
                    raise VendorLibraryError("Некорректный номер страницы")
                handler.send_bytes(library.preview(parts[3], page=int(raw_page)), "image/png")
    except VendorLibraryError as error:
        handler.send_error_json(error.status, error.code, str(error))
    return True
