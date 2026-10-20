"""Raster-only untrusted source preview, isolated behind a subprocess timeout."""
from __future__ import annotations

from io import BytesIO
import subprocess
import sys
from threading import BoundedSemaphore
from .desktop_runtime import worker_command, worker_launch_options

MAX_PREVIEW = 1800
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_OUTPUT_BYTES = 20 * 1024 * 1024
PREVIEW_SLOTS = BoundedSemaphore(2)


def preview_bytes(content: bytes, *, page: int = 1) -> bytes:
    if not isinstance(content, bytes):
        raise TypeError("Содержимое исходника должно быть передано как байты")
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Исходник превышает 8 МиБ")
    if not 1 <= page <= 500:
        raise ValueError("Номер страницы вне диапазона 1..500")
    if not PREVIEW_SLOTS.acquire(blocking=False):
        raise ValueError(
            "Заняты оба обработчика предпросмотра. Повторите запрос после завершения текущей обработки."
        )
    try:
        try:
            result = subprocess.run(
                worker_command("backend.source_preview", str(page)),
                input=content,
                capture_output=True,
                timeout=20,
                check=False,
                **worker_launch_options(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Просмотр исходника превысил лимит 20 секунд") from exc
        if result.returncode:
            raise ValueError(
                "Не удалось построить безопасное изображение страницы: "
                "проверьте файл и номер страницы"
            )
        if (
            len(result.stdout) > MAX_PREVIEW_OUTPUT_BYTES
            or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            raise ValueError("Некорректный результат просмотра")
        return result.stdout
    finally:
        PREVIEW_SLOTS.release()


def render(content, page):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000
    if b"%PDF-" in content[:1024]:
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(content) as pdf:
            if not 1 <= page <= len(pdf):
                raise ValueError("Страница не найдена")
            pdfpage = pdf[page - 1]
            try:
                width, height = pdfpage.get_size()
                if width <= 0 or height <= 0:
                    raise ValueError("Пустая страница")
                bitmap = pdfpage.render(scale=min(MAX_PREVIEW / width, MAX_PREVIEW / height, 2))
                try:
                    image = bitmap.to_pil().copy().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                pdfpage.close()
    else:
        if page != 1:
            raise ValueError("У изображения только одна страница")
        with Image.open(BytesIO(content)) as original:
            if original.width * original.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("Изображение слишком большое")
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((MAX_PREVIEW, MAX_PREVIEW))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


if __name__ == "__main__":
    try:
        data = sys.stdin.buffer.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("Файл слишком большой")
        sys.stdout.buffer.write(render(data, int(sys.argv[1])))
    except Exception:
        sys.exit(2)
