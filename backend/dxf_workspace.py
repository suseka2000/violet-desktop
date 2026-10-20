"""Large DXF reference workspaces, with stable source coordinates and explicit limits.

Call the isolated wrappers from dxf_workspace_worker in HTTP code. These helpers
never save/audit-repair the original and never turn mixed-scale drawings into rooms.
"""
from __future__ import annotations

import base64
from collections import Counter
import io
import math
import re
import tempfile

import ezdxf
from ezdxf import bbox, units
from PIL import Image, ImageDraw

from backend.importing.cad_preview import display_geometry, polyline_points
from backend.importing.errors import MalformedSourceError, RepairRequiredError, SelectionError, SourceLimitError

MAX_BYTES = 64 * 1024**2
MAX_ENTITIES = 300_000
MAX_LAYOUTS = 128
MAX_SELECTABLE = 3000
MAX_SELECTION_SEGMENTS = 12000
MAX_RENDER_PRIMITIVES = 1_500_000
MAX_COORDINATE = 1e12
BASE_WARNINGS = [
    "Разметка использует исходные координаты DXF. Перед созданием стен задайте масштаб по известному размеру: даже Model может содержать виды в разных масштабах.",
    "Растровый обзор — подложка, а не готовая рабочая документация. Штриховки показаны контурами; шрифты могут заменяться. Внешние ссылки и изображения не загружаются.",
]


def _plain(value):
    return re.sub(r"\\U\+([0-9a-fA-F]{4})", lambda match: "\ufffd" if 0xD800 <= int(match[1], 16) <= 0xDFFF else chr(int(match[1], 16)), str(value))


def _read(content):
    if not isinstance(content, bytes):
        raise TypeError("DXF должен быть передан байтами")
    if not content or len(content) > MAX_BYTES:
        raise SourceLimitError("DXF должен быть непустым и не превышать 64 МиБ")
    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf") as source:
            source.write(content)
            source.flush()
            doc = ezdxf.readfile(source.name)
        if len(doc.entitydb) > MAX_ENTITIES or len(doc.layouts) > MAX_LAYOUTS:
            raise SourceLimitError("DXF превышает лимит 300 000 объектов или 128 пространств")
        audit = doc.audit()
        if audit.errors:
            raise MalformedSourceError(f"DXF содержит неисправимые ошибки: {len(audit.errors)}")
        if audit.fixes:
            raise RepairRequiredError(f"DXF требует исправлений: {len(audit.fixes)}. Исправьте копию в CAD и загрузите снова.")
        return doc, {"error_count": 0, "fix_count": 0, "source_modified": False}
    except (SourceLimitError, MalformedSourceError):
        raise
    except (ezdxf.DXFError, ValueError, TypeError, OSError, UnicodeError) as exc:
        raise MalformedSourceError("Не удалось прочитать DXF; проверьте формат и целостность файла") from exc


def _units(doc):
    code = int(doc.units)
    factor = None
    try:
        name = str(units.unit_name(code)) if code else "unitless"
        if code:
            factor = float(units.conversion_factor(code, units.MM))
            if not math.isfinite(factor) or factor <= 0:
                factor = None
    except (ValueError, TypeError, IndexError, ZeroDivisionError):
        name = f"code-{code}"
    return {"declared_code": code, "declared_name": name, "millimetre_factor": factor,
            "coordinate_units": "source", "requires_calibration": True}


def _valid_bounds(values):
    return len(values) == 4 and all(isinstance(v, (float, int)) and not isinstance(v, bool)
        and math.isfinite(v) and abs(v) <= MAX_COORDINATE for v in values)


def _bounds_dict(values):
    return dict(zip(("min_x", "min_y", "max_x", "max_y"), values))


def _extents(entities):
    box = bbox.extents((e for e in entities if e.dxftype() != "VIEWPORT"), fast=True)
    if not box.has_data:
        return None
    result = [float(box.extmin.x), float(box.extmin.y), float(box.extmax.x), float(box.extmax.y)]
    if not _valid_bounds(result):
        raise SourceLimitError("Координаты DXF выходят за допустимые конечные пределы")
    return result


def _layout_info(space, page, unit_info):
    extents = _extents(space)
    counts = Counter(str(e.dxf.get("layer", "0")) for e in space)
    return {"page": page, "name": _plain(space.name), "is_model": space.is_modelspace,
        "kind": "model" if space.is_modelspace else "paper", "entity_count": len(space),
        "types": dict(Counter(e.dxftype() for e in space)),
        "layers": [{"name": name, "display_name": _plain(name), "count": count} for name, count in sorted(counts.items())],
        "bounds": _bounds_dict(extents) if extents else None,
        "units": unit_info, "coordinate_space": "dxf_source", "requires_calibration": True}


def inspect_dxf_workspace(content: bytes) -> dict:
    doc, audit = _read(content)
    unit_info = _units(doc)
    return {"format": "dxf", "dxf_version": doc.dxfversion, "units": unit_info,
        "layouts": [_layout_info(space, index, unit_info) for index, space in enumerate(doc.layouts, 1)],
        "warnings": list(BASE_WARNINGS), "audit": audit}


def validate_view_parameters(layout=1, region=None, layers=None):
    if isinstance(layout, bool) or not isinstance(layout, int) or not 1 <= layout <= MAX_LAYOUTS:
        raise SelectionError("Номер пространства должен быть целым числом от 1 до 128")
    if region is not None:
        if not isinstance(region, (list, tuple)) or not _valid_bounds(region) or not region[0] < region[2] or not region[1] < region[3]:
            raise SelectionError("Область должна содержать четыре конечные координаты: min_x, min_y, max_x, max_y")
        if region[2] - region[0] < 1e-9 or region[3] - region[1] < 1e-9:
            raise SelectionError("Выбранная область слишком мала")
    if layers is not None and (not isinstance(layers, list) or len(layers) > 256 or
            any(not isinstance(name, str) or len(name) > 1024 for name in layers)):
        raise SelectionError("Слои должны быть списком не более 256 названий")


def _intersects(segment, region):
    """Liang–Barsky clipping. Endpoints returned in original source coordinates."""
    (ax, ay), (bx, by) = segment
    if not all(math.isfinite(v) and abs(v) <= MAX_COORDINATE for v in (ax, ay, bx, by)):
        raise SourceLimitError("В геометрии DXF недопустимые координаты")
    dx, dy = bx - ax, by - ay
    lo, hi = 0.0, 1.0
    for p, q in ((-dx, ax-region[0]), (dx, region[2]-ax), (-dy, ay-region[1]), (dy, region[3]-ay)):
        if p == 0:
            if q < 0:
                return None
        else:
            t = q / p
            if p < 0:
                lo = max(lo, t)
            else:
                hi = min(hi, t)
            if lo > hi:
                return None
    return ((ax + lo*dx, ay + lo*dy), (ax + hi*dx, ay + hi*dy))


def _selectable(entities, region):
    selected, matching, segment_count, omitted = [], 0, 0, Counter()
    for entity in entities:
        segments, exact, reason = display_geometry(entity)
        if not segments:
            omitted[entity.dxftype()] += 1
            continue
        if not any(_intersects(segment, region) for segment in segments):
            continue
        matching += 1
        if len(selected) >= MAX_SELECTABLE or segment_count + len(segments) > MAX_SELECTION_SEGMENTS:
            continue
        points = []
        kind = entity.dxftype()
        closed = False
        if kind == "LINE" and exact:
            points = list(segments[0])
        elif kind in {"LWPOLYLINE", "POLYLINE"} and exact:
            points = [(x, y) for x, y, bulge in polyline_points(entity)]
            closed = bool(entity.closed) if kind == "LWPOLYLINE" else bool(entity.is_closed)
        selected.append({"handle": str(entity.dxf.handle), "type": kind, "entity_type": kind,
            "layer": str(entity.dxf.get("layer", "0")),
            "points": [{"x": x, "y": y} for x, y in points],
            "segments": [{"start": {"x": a[0], "y": a[1]}, "end": {"x": b[0], "y": b[1]}} for a, b in segments],
            "exact_geometry": exact, "closed": closed,
            "room_supported": exact and closed and len(points) >= 3, "warning": reason})
        segment_count += len(segments)
    return selected, matching, dict(omitted)


def _raster(doc, space, region, layer_names):
    from ezdxf.addons.drawing import RenderContext, Frontend, config
    from ezdxf.addons.drawing.backend import Backend

    span_x, span_y = region[2] - region[0], region[3] - region[1]
    width = 1600
    height = max(320, min(1200, int(round(width * span_y / span_x))))
    # Extend the world rectangle, not just image margins, so hit tests and raster
    # coordinates use the exact same transform even for very wide/tall layouts.
    aspect = width / height
    cx, cy = (region[0]+region[2])/2, (region[1]+region[3])/2
    if span_x / span_y < aspect:
        span_x = span_y * aspect
    else:
        span_y = span_x / aspect
    mapped = [cx-span_x/2, cy-span_y/2, cx+span_x/2, cy+span_y/2]
    if not _valid_bounds(mapped):
        raise SourceLimitError("Размер области предпросмотра слишком велик")
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    unit_per_pixel = span_x / width

    class PillowBackend(Backend):
        def __init__(self):
            super().__init__()
            self.primitives = 0

        def count(self, count=1):
            self.primitives += count
            if self.primitives > MAX_RENDER_PRIMITIVES:
                raise SourceLimitError("Предпросмотр превышает лимит 1 500 000 примитивов; выберите меньше слоёв")

        def pixel(self, point):
            return ((point[0]-mapped[0])/span_x*width, (mapped[3]-point[1])/span_y*height)

        def set_background(self, color):
            pass

        def draw_point(self, pos, properties):
            self.count()
            if not all(math.isfinite(value) and abs(value) <= MAX_COORDINATE for value in (pos.x, pos.y)):
                raise SourceLimitError("В геометрии DXF недопустимые координаты")
            if mapped[0] <= pos.x <= mapped[2] and mapped[1] <= pos.y <= mapped[3]:
                draw.point(self.pixel(pos), fill="#334155")

        def draw_line(self, start, end, properties):
            self.count()
            clipped = _intersects(((start[0], start[1]), (end[0], end[1])), mapped)
            if clipped:
                draw.line([self.pixel(p) for p in clipped], fill="#334155", width=1)

        def draw_path(self, path, properties):
            vertices = iter(path.flattening(max(unit_per_pixel * .5, 1e-6)))
            previous = next(vertices, None)
            for point in vertices:
                if previous is not None:
                    self.draw_line(previous, point, properties)
                previous = point

        def draw_filled_polygon(self, points, properties):
            values = list(points.vertices())
            self.count(len(values))
            if len(values) < 3:
                return
            # Polygon clipping prevents Pillow integer overflow for a small
            # zoomed region inside a drawing with distant entities.
            polygon = [(float(p[0]), float(p[1])) for p in values]
            if not all(math.isfinite(value) and abs(value) <= MAX_COORDINATE for point in polygon for value in point):
                raise SourceLimitError("В геометрии DXF недопустимые координаты")
            for axis, boundary, sign in ((0, mapped[0], 1), (0, mapped[2], -1), (1, mapped[1], 1), (1, mapped[3], -1)):
                result = []
                for a, b in zip(polygon, polygon[1:] + polygon[:1]):
                    ina, inb = sign*(a[axis]-boundary) >= 0, sign*(b[axis]-boundary) >= 0
                    if ina:
                        result.append(a)
                    if ina != inb:
                        t = (boundary-a[axis])/(b[axis]-a[axis])
                        result.append((a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1])))
                polygon = result
                if not polygon:
                    return
            draw.polygon([self.pixel(p) for p in polygon], fill="#334155")

        def draw_image(self, image_data, properties):
            pass

        def clear(self):
            draw.rectangle((0, 0, width, height), fill="white")

    backend = PillowBackend()
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE,
        color_policy=config.ColorPolicy.BLACK, hatch_policy=config.HatchPolicy.SHOW_OUTLINE,
        image_policy=config.ImagePolicy.IGNORE, hatching_timeout=1.0,
        max_flattening_distance=max(unit_per_pixel * .5, 1e-6))
    # DXF exports sometimes retain literal Unicode escape sequences. Decode in
    # memory only; drawing instructions and strings are never executed as HTML.
    for entity in doc.entitydb.values():
        if entity.dxftype() == "MTEXT":
            entity.text = _plain(entity.text).replace("^J", "\\P")
        elif entity.dxftype() in {"TEXT", "ATTRIB", "ATTDEF"}:
            entity.dxf.text = _plain(entity.dxf.text)
    Frontend(RenderContext(doc), backend, config=cfg).draw_layout(space,
        filter_func=lambda entity: (layer_names is None or str(entity.dxf.get("layer", "0")) in layer_names), finalize=True)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return {"mime_type": "image/png", "data_base64": base64.b64encode(output.getvalue()).decode("ascii"),
        "width": width, "height": height}, mapped, backend.primitives


def view_dxf_workspace(content: bytes, layout=1, region=None, layers=None) -> dict:
    validate_view_parameters(layout, region, layers)
    doc, audit = _read(content)
    layouts = list(doc.layouts)
    if layout > len(layouts):
        raise SelectionError("Такого пространства нет в DXF")
    space = layouts[layout-1]
    known_layers = {str(e.dxf.get("layer", "0")) for e in space}
    if layers is not None and set(layers) - known_layers:
        raise SelectionError("Выбран неизвестный слой этого пространства")
    layer_names = set(layers) if layers is not None else None
    def visible(entity):
        name = str(entity.dxf.get("layer", "0"))
        layer_entry = doc.layers.get(name)
        return (layer_names is None or name in layer_names) and not layer_entry.is_off() and not layer_entry.is_frozen() and not entity.dxf.get("invisible", 0)

    entities = [entity for entity in space if visible(entity)]
    extents = _extents(entities)
    region = list(region) if region is not None else list(extents or [0.0, 0.0, 1000.0, 1000.0])
    if region[2] == region[0]:
        region[0] -= .5
        region[2] += .5
    if region[3] == region[1]:
        region[1] -= .5
        region[3] += .5
    if extents and region == extents:
        margin = max(region[2]-region[0], region[3]-region[1]) * .015
        region = [region[0]-margin, region[1]-margin, region[2]+margin, region[3]+margin]
    preview, mapped, primitives = _raster(doc, space, region, layer_names)
    selected, matching, omitted = _selectable(entities, mapped)
    truncated = len(selected) < matching
    warnings = list(BASE_WARNINGS)
    skipped_visibility = sum(1 for entity in space if (layer_names is None or str(entity.dxf.get("layer", "0")) in layer_names) and not visible(entity))
    if skipped_visibility:
        warnings.append(f"Скрытые объекты и выключенные/замороженные слои не отображаются: {skipped_visibility} объектов.")
    unsupported_visual = Counter(e.dxftype() for e in entities if e.dxftype() in {"IMAGE", "OLE2FRAME", "ACAD_PROXY_ENTITY", "UNDERLAY", "PDFUNDERLAY", "DWFUNDERLAY", "DGNUNDERLAY"})
    if unsupported_visual:
        warnings.append("Внешние/неподдерживаемые объекты не показаны: " + ", ".join(f"{kind}: {count}" for kind, count in sorted(unsupported_visual.items())))
    if truncated:
        warnings.append(f"Обзор не обрезан по числу объектов. Для выбора доступны {len(selected)} из {matching} объектов в этой области. Увеличьте нужный участок или выберите отдельные слои.")
    if omitted:
        warnings.append("Не все типы доступны для выбора; обведите их вручную: " + ", ".join(f"{kind}: {count}" for kind, count in sorted(omitted.items())))
    if not space.is_modelspace:
        warnings.append("Пространство листа: размеры на бумаге не равны размерам помещения. Калибруйте каждый вид отдельно.")
    return {"format": "dxf", "page": layout, "layout_name": _plain(space.name),
        "kind": "model" if space.is_modelspace else "paper", "bounds": _bounds_dict(mapped),
        "geometry_bounds": _bounds_dict(extents) if extents else None, "requested_region": region,
        "preview": preview, "units": _units(doc), "coordinate_space": "dxf_source", "requires_calibration": True,
        "selectable_entities": selected, "selection_truncated": truncated,
        "matching_entity_count": matching, "selected_entity_count": len(selected),
        "nonselectable_types": omitted, "render_primitive_count": primitives,
        "warnings": warnings, "audit": audit}
