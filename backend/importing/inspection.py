"""Format-dispatching, bounded source inspection without network access."""

from __future__ import annotations

from io import BytesIO
import math
from pathlib import Path
import warnings as warnings_module

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from ._common import (
    MAX_PDF_PAGES,
    MAX_PDF_PAGE_MM,
    MAX_RASTER_PIXELS,
    MAX_RASTER_SIDE,
    bounded_content,
    content_id,
    safe_filename,
)
from .dxf import inspect_dxf
from .cad import inspect_dwg
from .errors import MalformedSourceError, SourceLimitError, UnsupportedSourceError


def _base(filename: str, payload: bytes, source_format: str, media_type: str) -> dict:
    return {
        "schema_version": "1.0",
        "source_id": content_id(payload),
        "filename": filename,
        "format": source_format,
        "media_type": media_type,
        "byte_length": len(payload),
        "units": None,
        "bounds": None,
        "entity_count": 0,
        "entity_types": {},
        "layers": [],
        "candidates": [],
        "candidates_truncated": False,
        "preview_segments": [],
        "preview_truncated": False,
        "preview_skipped_entity_types": {},
        "selectable_entities": [],
        "omitted_entities": [],
        "omitted_entities_truncated": False,
        "layouts": [],
        "warnings": [],
        "missing_semantics": [],
    }


def _inspect_pdf(filename: str, payload: bytes) -> dict:
    try:
        reader = PdfReader(BytesIO(payload), strict=True)
    except (PdfReadError, OSError, ValueError, TypeError, OverflowError) as exc:
        raise MalformedSourceError(f"Не удалось прочитать PDF {filename!r}: файл повреждён или имеет неверный формат") from exc
    if reader.is_encrypted:
        raise UnsupportedSourceError(
            "Зашифрованный PDF не поддерживается; сначала сохраните незашифрованную копию"
        )
    try:
        page_count = len(reader.pages)
    except (PdfReadError, OSError, ValueError, TypeError, OverflowError) as exc:
        raise MalformedSourceError("Не удалось прочитать список страниц PDF") from exc
    if page_count < 1:
        raise MalformedSourceError("PDF не содержит страниц")
    if page_count > MAX_PDF_PAGES:
        raise SourceLimitError(
            f"В PDF {page_count} страниц; допустимо не более {MAX_PDF_PAGES}",
            details={"page_count": page_count, "max_pages": MAX_PDF_PAGES},
        )

    pages: list[dict] = []
    for index in range(page_count):
        try:
            page = reader.pages[index]
            width_points = abs(float(page.mediabox.width))
            height_points = abs(float(page.mediabox.height))
            rotation = int(page.get("/Rotate", 0) or 0) % 360
        except (PdfReadError, OSError, ValueError, TypeError, OverflowError) as exc:
            raise MalformedSourceError(f"Не удалось прочитать размер страницы PDF {index + 1}") from exc
        values = (width_points, height_points)
        if not all(math.isfinite(value) and value > 0.0 for value in values):
            raise MalformedSourceError(f"У страницы PDF {index + 1} некорректный MediaBox")
        width_mm = width_points * 25.4 / 72.0
        height_mm = height_points * 25.4 / 72.0
        if width_mm > MAX_PDF_PAGE_MM or height_mm > MAX_PDF_PAGE_MM:
            raise SourceLimitError(
                f"Страница PDF {index + 1} превышает допустимый размер",
                details={
                    "width_mm": width_mm,
                    "height_mm": height_mm,
                    "max_page_mm": MAX_PDF_PAGE_MM,
                },
            )
        pages.append(
            {
                "index": index,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "rotation_degrees": rotation,
            }
        )

    result = _base(filename, payload, "pdf", "application/pdf")
    result.update(
        {
            "page_count": page_count,
            "pages": pages,
            "missing_semantics": [
                "Страницы PDF используются только как подложка: геометрия и масштаб не определяются автоматически.",
                "Перед обводкой пользователь должен отдельно выполнить калибровку и разметку.",
            ],
        }
    )
    return result


def _safe_dpi(value) -> tuple[float, float] | None:
    try:
        if isinstance(value, (int, float)):
            first = second = float(value)
        else:
            first, second = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if not all(math.isfinite(item) and 1.0 <= item <= 100_000.0 for item in (first, second)):
        return None
    return first, second


def _inspect_raster(filename: str, payload: bytes, expected_format: str) -> dict:
    warning_messages: list[str] = []
    exif_orientation: int | None = None
    try:
        with warnings_module.catch_warnings():
            warnings_module.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                actual_format = str(image.format or "").upper()
                width, height = int(image.width), int(image.height)
                mode = str(image.mode)
                dpi = _safe_dpi(image.info.get("dpi"))
                has_icc_profile = bool(image.info.get("icc_profile"))
                has_exif = bool(image.info.get("exif"))
                if actual_format != expected_format:
                    raise MalformedSourceError(
                        f"Расширение файла не соответствует изображению: ожидался {expected_format}, "
                        f"получен {actual_format or 'неизвестный формат'}"
                    )
                if width <= 0 or height <= 0:
                    raise MalformedSourceError("У растрового изображения некорректные размеры")
                if width > MAX_RASTER_SIDE or height > MAX_RASTER_SIDE:
                    raise SourceLimitError(
                        "Сторона растрового изображения превышает допустимый размер",
                        details={
                            "width_px": width,
                            "height_px": height,
                            "max_side_px": MAX_RASTER_SIDE,
                        },
                    )
                pixels = width * height
                if pixels > MAX_RASTER_PIXELS:
                    raise SourceLimitError(
                        f"В растровом изображении {pixels} пикселей; допустимо не более {MAX_RASTER_PIXELS}",
                        details={"pixels": pixels, "max_pixels": MAX_RASTER_PIXELS},
                    )
                image.verify()
            # Reopen and force decoding: metadata-only parsing is insufficient to
            # catch a truncated or corrupt compressed pixel stream.
            with Image.open(BytesIO(payload)) as decoded:
                decoded.load()
                exif = decoded.getexif()
                has_exif = has_exif or bool(exif)
                raw_orientation = exif.get(274)
                try:
                    exif_orientation = int(raw_orientation) if raw_orientation is not None else None
                except (TypeError, ValueError, OverflowError):
                    exif_orientation = None
                if exif_orientation is not None and not 1 <= exif_orientation <= 8:
                    warning_messages.append(
                        "Значение EXIF orientation некорректно и не используется."
                    )
                    exif_orientation = None
    except (SourceLimitError, MalformedSourceError):
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        TypeError,
        OverflowError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise MalformedSourceError(
            f"Не удалось безопасно декодировать растровый исходник {filename!r}: файл повреждён или имеет неверный формат"
        ) from exc

    width_mm = height_mm = None
    dpi_value = None
    if dpi is not None:
        dpi_value = {"x": dpi[0], "y": dpi[1]}
        width_mm = width / dpi[0] * 25.4
        height_mm = height / dpi[1] * 25.4
    else:
        warning_messages.append(
            "DPI изображения отсутствует или некорректен; физический размер нужно откалибровать вручную."
        )
    media_type = "image/png" if expected_format == "PNG" else "image/jpeg"
    result = _base(filename, payload, expected_format.casefold(), media_type)
    result.update(
        {
            "width_px": width,
            "height_px": height,
            "pixel_count": width * height,
            "mode": mode,
            "dpi": dpi_value,
            "width_mm": width_mm,
            "height_mm": height_mm,
            "has_icc_profile": has_icc_profile,
            "has_exif": has_exif,
            "exif_orientation": exif_orientation,
            "warnings": warning_messages,
            "missing_semantics": [
                "Растровое изображение используется только как подложка; геометрия не распознаётся автоматически.",
                "Перед обводкой пользователь должен отдельно выполнить калибровку и разметку.",
            ],
        }
    )
    return result


def inspect_source(
    filename: str,
    content: bytes,
    units_override: str | None = None,
) -> dict:
    """Inspect one local source and return JSON-compatible metadata.

    Supported inputs are strict DXF, optional locally converted DWG,
    unencrypted PDF, PNG and JPEG. SVG is
    deliberately rejected because active/external content must not cross this
    boundary. Missing native CAD adapters receive actionable export guidance.
    """

    safe_name = safe_filename(filename)
    payload = bounded_content(content)
    suffix = Path(safe_name).suffix.casefold()
    initial = payload[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    lowered_initial = initial.lower()

    if suffix == ".svg" or b"<svg" in lowered_initial:
        raise UnsupportedSourceError(
            "SVG намеренно не принимается: он может содержать активное или внешнее содержимое. "
            "Используйте DXF, PDF, PNG или JPEG"
        )
    if suffix == ".dwg" or payload.startswith(b"AC10"):
        return inspect_dwg(safe_name, payload, units_override=units_override)
    if suffix in {".sldprt", ".sldasm", ".slddrw"}:
        raise UnsupportedSourceError("Нативные файлы SOLIDWORKS пока не читаются. Экспортируйте нужный 2D-вид в DXF или DWG масштаба 1:1; для подложки можно использовать PDF.", details={"suggested_format": "dxf", "capability": "export_required"})
    if suffix in {".step", ".stp", ".iges", ".igs"}:
        raise UnsupportedSourceError("3D-модель не является планом помещения. Экспортируйте плоский 2D-вид в DXF; импорт STEP/IGES пока не реализован.", details={"suggested_format": "dxf"})

    if suffix == ".dxf":
        if (
            b"%PDF-" in payload[:1024]
            or payload.startswith(b"\x89PNG\r\n\x1a\n")
            or payload.startswith(b"\xff\xd8\xff")
        ):
            raise MalformedSourceError("Файл назван DXF, но его сигнатура соответствует другому формату")
        return inspect_dxf(safe_name, payload, units_override=units_override)

    if suffix == ".pdf":
        if b"%PDF-" not in payload[:1024]:
            raise MalformedSourceError("В первых 1024 байтах отсутствует заголовок PDF")
        return _inspect_pdf(safe_name, payload)
    if suffix == ".png":
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise MalformedSourceError("Отсутствует сигнатура PNG")
        return _inspect_raster(safe_name, payload, "PNG")
    if suffix in {".jpg", ".jpeg"}:
        if not payload.startswith(b"\xff\xd8\xff"):
            raise MalformedSourceError("Отсутствует сигнатура JPEG")
        return _inspect_raster(safe_name, payload, "JPEG")
    raise UnsupportedSourceError(
        f"Расширение исходника {suffix or '(нет)'!r} не поддерживается; используйте DXF, PDF, PNG или JPEG"
    )
