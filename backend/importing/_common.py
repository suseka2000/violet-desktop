"""Shared guards for import parsers and the source store."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .errors import SourceLimitError, UnsafeSourceNameError


MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_CAD_SOURCE_BYTES = 64 * 1024 * 1024
MAX_DXF_ENTITIES = 50_000
MAX_DXF_VERTICES = 250_000
MAX_DXF_VERTICES_PER_ENTITY = 50_000
MAX_DXF_CANDIDATES = 10_000
MAX_PREVIEW_SEGMENTS = 5_000
MAX_CONVERSION_SEGMENTS = 2_000
MAX_PDF_PAGES = 300
MAX_PDF_PAGE_MM = 20_000.0
MAX_RASTER_PIXELS = 25_000_000
MAX_RASTER_SIDE = 20_000

_SCOPE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SOURCE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def safe_filename(filename: str) -> str:
    if not isinstance(filename, str):
        raise UnsafeSourceNameError("Имя исходного файла должно быть строкой")
    candidate = filename.strip()
    if not candidate or len(candidate) > 255:
        raise UnsafeSourceNameError("Имя исходного файла должно содержать от 1 до 255 символов")
    if candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
        raise UnsafeSourceNameError(
            "Укажите только имя исходного файла, без каталогов"
        )
    if Path(candidate).name != candidate:
        raise UnsafeSourceNameError(
            "Укажите только имя исходного файла, без каталогов"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise UnsafeSourceNameError("Имя исходного файла содержит управляющие символы")
    return candidate


def safe_scope(scope: str) -> str:
    if not isinstance(scope, str) or not _SCOPE_PATTERN.fullmatch(scope):
        raise UnsafeSourceNameError(
            "Область хранения должна начинаться с буквы или цифры и содержать "
            "только латинские буквы, цифры, точку, подчёркивание или дефис"
        )
    if scope in {".", ".."}:
        raise UnsafeSourceNameError("Недопустимая область хранения исходников")
    return scope


def safe_source_id(source_id: str) -> str:
    if not isinstance(source_id, str) or not _SOURCE_ID_PATTERN.fullmatch(source_id):
        raise UnsafeSourceNameError("Идентификатор исходника должен быть SHA-256 в нижнем регистре")
    return source_id


def bounded_content(content: bytes, *, max_bytes: int = MAX_SOURCE_BYTES) -> bytes:
    if not isinstance(content, bytes):
        raise TypeError("Содержимое исходника должно быть передано как байты")
    if not content:
        raise SourceLimitError("Пустой исходный файл не принимается")
    if len(content) > max_bytes:
        raise SourceLimitError(
            f"Размер исходника — {len(content)} байт; допустимо не более {max_bytes} байт",
            details={"byte_length": len(content), "max_bytes": max_bytes},
        )
    return content


def content_id(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def media_type_for(filename: str, content: bytes) -> str:
    lower = filename.casefold()
    prefix = content[:1024].lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if content.startswith(b"\x89PNG\r\n\x1a\n") or lower.endswith(".png"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff") or lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if b"%PDF-" in content[:1024] or lower.endswith(".pdf"):
        return "application/pdf"
    if content.startswith(b"AutoCAD Binary DXF\r\n\x1a\x00") or lower.endswith(".dxf"):
        return "image/vnd.dxf"
    if content.startswith(b"AC10") or lower.endswith(".dwg"):
        return "image/vnd.dwg"
    if lower.endswith(".svg") or prefix.startswith((b"<svg", b"<?xml")):
        return "image/svg+xml"
    return "application/octet-stream"
