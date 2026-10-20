"""Local CAD adapters; no downloads, shell commands or client-selected binaries."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from ._common import MAX_SOURCE_BYTES, bounded_content, content_id, safe_filename
from .errors import MalformedSourceError, SourceLimitError, UnsupportedSourceError

ODA_ENV = "VIOLET_ODA_CONVERTER"


def _converter_path() -> Path | None:
    configured = os.environ.get(ODA_ENV)
    if configured:
        candidate = Path(configured).expanduser()
        # A server operator may configure an absolute executable; HTTP input
        # cannot override this value or append arguments.
        if not candidate.is_absolute():
            return None
        return candidate.resolve() if candidate.is_file() and os.access(candidate, os.X_OK) else None
    discovered = shutil.which("ODAFileConverter")
    candidates = ([Path(discovered)] if discovered else []) + [
        Path("/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"),
        Path("C:/Program Files/ODA/ODAFileConverter/ODAFileConverter.exe"),
    ]
    return next((p.resolve() for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


def import_capabilities() -> dict:
    available = _converter_path() is not None
    from ..api.cad_assets import cad_capabilities
    workspace = cad_capabilities()
    return {
        "formats": [
            {"extensions": [".dxf"], "status": "available", "description": "ASCII и binary DXF: выбор объектов, разметка, прямолинейные 2D-контуры помещений."},
            {"extensions": [".dwg"], "status": "available" if available else "requires_converter", "description": "Локальное преобразование ODA File Converter в DXF; исходный DWG сохраняется без изменений."},
            {"extensions": [".sldprt", ".sldasm", ".slddrw"], "status": "export_required", "description": "Из SOLIDWORKS экспортируйте нужный 2D-вид в DXF/DWG масштаба 1:1. Нативные модели и сборки не читаются."},
            {"extensions": [".step", ".stp"], "status": "available" if workspace["step"]["available"] else "requires_dependency", "description": workspace["step"]["message"] + ". Не преобразуются автоматически в помещения или утверждённые узлы."},
            {"extensions": [".iges", ".igs"], "status": "unsupported", "description": "Для 3D-модели используйте STEP."},
            {"extensions": [".pdf", ".png", ".jpg", ".jpeg"], "status": "available", "description": "Подложка с ручной калибровкой и обводкой."},
        ],
        "dwg": {"available": available, "adapter": "ODA File Converter", "configuration_env": ODA_ENV,
                "message": "Локальный конвертер обнаружен." if available else "ODA File Converter не обнаружен. Установите его отдельно или экспортируйте DXF из AutoCAD."},
        "limits": {"max_source_bytes": MAX_SOURCE_BYTES, "converter_timeout_seconds": 20},
        "cad_workspace": workspace,
        "limitations": ["Нет обещания полного воспроизведения AutoCAD: внешние ссылки не загружаются, proxy/AEC-объекты, 3D и листы не превращаются в модель помещений.",
                        "Блоки и кривые показываются приближённо, но не становятся границами помещения без ручной обводки."],
    }


def dwg_to_dxf(content: bytes, filename: str) -> bytes:
    payload = bounded_content(content)
    safe_filename(filename)
    if not re.match(rb"AC10[0-9]{2}", payload[:6]):
        raise MalformedSourceError("В файле DWG отсутствует поддерживаемая сигнатура AutoCAD AC10xx")
    executable = _converter_path()
    if executable is None:
        raise UnsupportedSourceError(
            "Для DWG нужен локальный ODA File Converter. Установите его отдельно и задайте "
            "VIOLET_ODA_CONVERTER или экспортируйте 2D DXF из AutoCAD/SOLIDWORKS.",
            details={"suggested_format": "dxf", "capability": "requires_converter"},
        )
    with tempfile.TemporaryDirectory(prefix="violet-cad-") as directory:
        root = Path(directory)
        incoming, outgoing = root / "input", root / "output"
        incoming.mkdir()
        outgoing.mkdir()
        (incoming / "drawing.dwg").write_bytes(payload)
        try:
            completed = subprocess.run(
                [str(executable), str(incoming), str(outgoing), "ACAD2018", "DXF", "0", "0", "drawing.dwg"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=20, check=False, cwd=root,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceLimitError("Преобразование DWG превысило 20 секунд; экспортируйте меньший фрагмент в DXF") from exc
        except OSError as exc:
            raise UnsupportedSourceError("Не удалось запустить настроенный ODA File Converter") from exc
        output = outgoing / "drawing.dxf"
        if completed.returncode or not output.is_file() or output.is_symlink():
            raise MalformedSourceError("ODA File Converter не создал DXF. Проверьте DWG в исходной программе и экспортируйте 2D DXF.")
        if output.stat().st_size > MAX_SOURCE_BYTES:
            raise SourceLimitError("Промежуточный DXF превышает 8 МиБ; экспортируйте меньший фрагмент")
        return bounded_content(output.read_bytes())


def inspect_dwg(filename: str, content: bytes, *, units_override: str | None = None) -> dict:
    from .dxf import inspect_dxf
    payload = dwg_to_dxf(content, filename)
    result = inspect_dxf("converted.dxf", payload, units_override=units_override)
    result.update(source_id=content_id(content), filename=safe_filename(filename), format="dwg",
                  media_type="image/vnd.dwg", byte_length=len(content),
                  conversion={"adapter": "ODA File Converter", "derived_format": "dxf", "derived_sha256": content_id(payload)})
    result["warnings"].append("DWG прочитан через локальный ODA File Converter; оригинал не изменён. Сверьте важные размеры и состав объектов с AutoCAD.")
    return result


def convert_cad_source(content: bytes, *, filename: str, **parameters):
    from .dxf import convert_dxf
    if Path(filename).suffix.casefold() != ".dwg" and not content.startswith(b"AC10"):
        return convert_dxf(content, filename=filename, **parameters)
    payload = dwg_to_dxf(content, filename)
    result = convert_dxf(payload, filename=filename, **parameters)
    original_id = content_id(content)
    report = result.report.model_copy(update={"source_id": original_id, "source_format": "dwg",
        "warnings": (*result.report.warnings, "DWG преобразован локальным ODA File Converter; ссылки ведут на неизменённый оригинал.")})
    for building in result.project.buildings:
        for floor in building.floors:
            floor.properties["source_import"].update(source_id=original_id, source_format="DWG", derived_sha256=content_id(payload))
            floor.properties["source_import_report"] = report.model_dump(mode="json")
            for entity in [*floor.walls, *floor.spaces]:
                entity.properties.update(source_id=original_id, source_format="DWG")
    return result.model_copy(update={"source_id": original_id, "report": report})
