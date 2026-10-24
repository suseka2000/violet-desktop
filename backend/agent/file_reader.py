"""Read user-selected attachments in a bounded decoder process, never execute them."""
from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from threading import BoundedSemaphore
import zipfile

from ..desktop_runtime import worker_command, worker_launch_options

EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".md", ".json", ".csv", ".dxf", ".dwg", ".step", ".stp", ".docx", ".xlsx"}
SLOTS = BoundedSemaphore(2)
MAX_BYTES = 32 * 1024**2


def read_isolated(content, filename, **options):
    if len(content) > MAX_BYTES:
        raise ValueError("Вложение агента превышает 32 МиБ; выделите нужную часть исходника")
    if not SLOTS.acquire(blocking=False):
        raise ValueError("Обработчики файлов заняты; повторите запрос")
    try:
        payload = {"filename": filename, "content": base64.b64encode(content).decode(), **options}
        completed = subprocess.run(worker_command("backend.agent.file_reader"),
                                   input=json.dumps(payload).encode(), capture_output=True, timeout=90,
                                   **worker_launch_options())
        if completed.returncode or len(completed.stdout) > 12 * 1024**2:
            raise ValueError("Не удалось прочитать файл в допустимое время или объём памяти")
        result = json.loads(completed.stdout)
        if "error" in result:
            raise ValueError(result["error"])
        return result
    except subprocess.TimeoutExpired:
        raise ValueError("Чтение файла остановлено через 90 секунд; выберите меньший фрагмент") from None
    finally:
        SLOTS.release()


def image_result(content):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000
    with Image.open(BytesIO(content)) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((1600, 1600))
        output = BytesIO()
        image.save(output, format="JPEG", quality=85)
        return {"image_data": "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode(),
                "width": image.width, "height": image.height}


def cad_entities(doc, options):
    from ezdxf import units
    page = options.get("page", 1)
    layouts = list(doc.layouts)
    if not 1 <= page <= len(layouts):
        raise ValueError("Такого пространства в DXF нет")
    space = layouts[page-1]
    layer = options.get("layer")
    entities = [e for e in space if not layer or str(e.dxf.get("layer", "0")) == layer]
    region = options.get("region")
    if region is not None:
        import math
        from ezdxf import bbox
        if len(region) != 4 or not all(math.isfinite(v) for v in region) or region[0] >= region[2] or region[1] >= region[3]:
            raise ValueError("Область CAD: xmin,ymin,xmax,ymax, с положительной шириной и высотой")
        selected = []
        for entity in entities:
            bounds = bbox.extents([entity], fast=True)
            if bounds.has_data and bounds.extmax.x >= region[0] and bounds.extmax.y >= region[1] and bounds.extmin.x <= region[2] and bounds.extmin.y <= region[3]:
                selected.append(entity)
        entities = selected
    offset, limit = options.get("offset", 0), options.get("limit", 80)
    def encode(e, depth=0):
        kind = e.dxftype()
        item = {"handle": str(e.dxf.get("handle", "")), "type": kind, "layer": str(e.dxf.get("layer", "0"))}
        def point(value):
            return [float(value[0]), float(value[1])]
        if kind == "LINE":
            item.update(start=point(e.dxf.start), end=point(e.dxf.end), start_z=e.dxf.start.z, end_z=e.dxf.end.z)
        elif kind in {"LWPOLYLINE", "POLYLINE"}:
            from ..importing.cad_preview import polyline_points, fitted_polyline
            points = polyline_points(e)
            extrusion = list(e.dxf.get("extrusion", (0., 0., 1.)))
            straight = not any(p[2] for p in points) and not fitted_polyline(e)
            planar = kind == "LWPOLYLINE" or bool(e.is_2d_polyline)
            item.update(points=[[p[0], p[1]] for p in points[:2000]], bulges=[p[2] for p in points[:2000]],
                        vertex_count=len(points), points_partial=len(points)>2000, extrusion=extrusion,
                        exact_straight_xy=straight and planar and extrusion == [0., 0., 1.],
                        coordinate_note="points contain x,y only; bulges are arc curvature, NOT z. Non-default extrusion uses OCS, not world XY.",
                        closed=bool(e.closed) if kind == "LWPOLYLINE" else bool(e.is_closed))
        elif kind in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            item.update(text=(e.plain_text() if hasattr(e, "plain_text") else e.dxf.get("text", ""))[:2000],
                        insert=point(e.dxf.insert))
        elif kind in {"ARC", "CIRCLE"}:
            item.update(center=point(e.dxf.center), radius=e.dxf.radius)
            if kind == "ARC":
                item.update(start_angle=e.dxf.start_angle, end_angle=e.dxf.end_angle)
        elif kind == "INSERT":
            item.update(block=e.dxf.name, insert=point(e.dxf.insert), rotation=e.dxf.get("rotation", 0),
                        xscale=e.dxf.get("xscale", 1), yscale=e.dxf.get("yscale", 1),
                        attributes={a.dxf.tag: a.dxf.text for a in e.attribs})
            if depth < 2:
                from itertools import islice
                item["geometry"] = [encode(child, depth+1) for child in islice(e.virtual_entities(), 80)]
                item["geometry_may_be_partial"] = True
        elif kind == "DIMENSION":
            item["text"] = e.dxf.get("text", "")
            try:
                item["measurement"] = e.get_measurement()
            except Exception:
                pass
        return item
    code = int(doc.header.get("$INSUNITS", 0))
    try:
        factor = units.conversion_factor(code, units.MM) if code else None
    except (ValueError, TypeError):
        factor = None
    return {"layout": space.name, "is_modelspace": space.is_modelspace,
            "coordinate_units": "source_units", "unit_code": code, "millimetre_factor": factor,
            "region": region, "total": len(entities), "next_offset": offset+limit if offset+limit < len(entities) else None,
            "entities": [encode(e) for e in entities[offset:offset+limit]],
            "warning": "Размеры на листе не равны размерам помещения. Уточните масштаб; неизвестные единицы не считать миллиметрами."}


def process(content, filename, options):
    extension = Path(filename).suffix.lower()
    if extension not in EXTENSIONS:
        raise ValueError("Формат не поддерживается агентом")
    mode, page = options.get("mode", "text"), options.get("page", 1)
    if extension in {".dwg", ".dxf"}:
        if extension == ".dwg":
            from ..importing.cad import dwg_to_dxf
            content = dwg_to_dxf(content, filename)
        from ..dxf_workspace import _read, inspect_dxf_workspace, view_dxf_workspace
        if mode == "image":
            view = view_dxf_workspace(content, layout=page, region=options.get("region"), layers=[options["layer"]] if options.get("layer") else None)
            image = image_result(base64.b64decode(view.pop("preview")["data_base64"]))
            return {**image, **{k: view[k] for k in ("bounds", "units", "layout_name", "warnings")}}
        if mode == "summary":
            return inspect_dxf_workspace(content)
        return cad_entities(_read(content)[0], options)
    if extension in {".step", ".stp"}:
        from ..step_worker import run_step_inspect
        result = run_step_inspect(content)
        return {k: v for k, v in result.items() if k not in {"mesh", "meshes", "preview", "triangles", "vertices"}}
    if extension == ".pdf":
        from pypdf import PdfReader
        pdf = PdfReader(BytesIO(content))
        if mode == "summary":
            return {"format": "pdf", "pages": len(pdf.pages)}
        if not 1 <= page <= len(pdf.pages):
            raise ValueError("Страница не найдена")
        if mode == "image":
            from ..source_preview import render
            return {**image_result(render(content, page)), "page": page, "pages": len(pdf.pages)}
        text = pdf.pages[page-1].extract_text() or ""
        offset = options.get("offset", 0)
        return {"page": page, "pages": len(pdf.pages), "text": text[offset:offset+16000],
                "next_offset": offset+16000 if len(text) > offset+16000 else None,
                "hint": "Для размеров и схем используйте mode=image; отсутствие текста не означает пустую страницу."}
    if extension in {".png", ".jpg", ".jpeg", ".webp"}:
        return image_result(content)
    if extension in {".docx", ".xlsx"}:
        from xml.etree import ElementTree
        with zipfile.ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(e.file_size for e in entries) > 48 * 1024**2:
                raise ValueError("Слишком большой распакованный документ")
            selected = [e for e in entries if e.filename == "word/document.xml" or
                        e.filename == "xl/sharedStrings.xml" or e.filename.startswith("xl/worksheets/") and e.filename.endswith(".xml")]
            text = "\n".join(" ".join(ElementTree.fromstring(archive.read(e)).itertext()) for e in selected)
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1251", errors="replace")
    offset = options.get("offset", 0)
    return {"text": text[offset:offset+16000], "characters": len(text),
            "next_offset": offset+16000 if len(text) > offset+16000 else None}


def main():
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (75, 80))
        except (ImportError, ValueError, OSError):
            pass
        payload = json.loads(sys.stdin.buffer.read(46 * 1024**2))
        content = base64.b64decode(payload.pop("content"), validate=True)
        filename = payload.pop("filename")
        if len(content) > MAX_BYTES:
            raise ValueError("Вложение превышает 32 МиБ")
        result = process(content, filename, payload)
    except Exception as error:
        result = {"error": str(error)[:800]}
    # Frozen Windows stdout may use cp1252 even with PYTHONUTF8 set. ASCII JSON
    # preserves Unicode after decoding and is portable across all pipe encodings.
    sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
