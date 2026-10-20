"""A drawing key of the components actually present in saved node sections.

There is no catalogue lookup or synthetic icon fallback here. A component is
shown only when its STEP source and its preserved section geometry agree with
the saved assembly. Translation is ignored for deduplication, but neither
rotation, reflection nor shape changes are ignored.
"""
from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite

from .scene import BLUE, GRAY, INK, LIGHT, Sheet, wrap


def _data(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _normalised_paths(paths):
    """Keep the actual geometry; normalise only its location and edge order."""
    paths = [[[float(p[0]), float(p[1])] for p in path] for path in paths if len(path) >= 2]
    if not paths or any(not isfinite(v) for path in paths for p in path for v in p):
        return None
    low = [min(p[axis] for path in paths for p in path) for axis in (0, 1)]
    shifted = [[(p[0]-low[0], p[1]-low[1]) for p in path] for path in paths]
    # Quantisation absorbs CAD floating point noise without rotating an item or
    # identifying two different profiles by their bounding boxes.
    canonical = []
    for path in shifted:
        points = tuple((round(x, 5), round(y, 5)) for x, y in path)
        canonical.append(min(points, tuple(reversed(points))))
    return shifted, tuple(sorted(canonical)), low


def component_entries(catalog):
    """Deduplicate exact source components from the current detail catalogue."""
    result = {}
    for detail in catalog.values():
        record = detail.get("assembly")
        if record is None:
            continue
        record = _data(record)
        snapshot = _data(record.get("geometry_snapshot") or {})
        section = _data(snapshot.get("result") or {})
        sources = {_data(source)["source_id"]: _data(source) for source in record.get("source_models", [])}
        bodies = {str(body["id"]): body for body in record.get("bodies", [])}
        for component in section.get("components", []):
            component = _data(component)
            body = bodies.get(str(component.get("id")), {})
            source = sources.get(body.get("source_id"))
            # Parametric panels, floor contexts and adhesive boxes are already
            # identified in the node drawings. They are not supplier products.
            if body.get("kind") != "step" or source is None:
                continue
            geometry = _normalised_paths(component.get("paths", []))
            if geometry is None:
                continue
            paths, canonical, low = geometry
            code = str(source.get("component_code") or body.get("label") or component.get("label") or "Компонент")
            identity = {
                "source_id": source["source_id"], "source_component_id": body.get("source_component_id"),
                "code": code, "axis": section.get("axis"), "projection_axes": section.get("projection_axes", []),
                "rotation": body.get("rotation"), "paths": canonical,
            }
            signature = sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            if signature not in result:
                loops = [[(float(p[0])-low[0], float(p[1])-low[1]) for p in loop]
                         for loop in body.get("section_loops", []) if len(loop) >= 3]
                result[signature] = {
                    "id": signature, "source_id": source["source_id"], "filename": source.get("filename", ""),
                    "code": code, "paths": paths, "loops": loops,
                    "width_mm": max(p[0] for path in paths for p in path),
                    "height_mm": max(p[1] for path in paths for p in path),
                    "axis": section.get("axis", ""), "projection_axes": section.get("projection_axes", []),
                    "role": body.get("role") or "profile", "locations": [], "assembly_ids": [],
                }
            entry = result[signature]
            if detail.get("mark") and detail["mark"] not in entry["locations"]:
                entry["locations"].append(detail["mark"])
            if record.get("id") and record["id"] not in entry["assembly_ids"]:
                entry["assembly_ids"].append(record["id"])
    return list(result.values())


def _scale(required):
    values = (.25, .5, 1, 2, 4, 5, 10, 20, 25, 50, 100)
    return next((value for value in values if value >= required), max(required, 1.))


def component_legend_sheets(catalog, num):
    """Return paginated A2 sheets, preserving every unique real section.

    Twelve readable cells per page; cell count is layout, never a component
    quota. Full source IDs and applicability are attached as scene metadata.
    """
    from .album import compact_marks
    entries = component_entries(catalog)
    sheets = []
    for offset in range(0, len(entries), 12):
        page = Sheet("Применённые профили и крепёж / графическая легенда", "component_legend", 594, 420)
        page.text(23, 37, "Реальные сечения деталей из узлов этого проекта. Положение соответствует указанным узлам; размеры - в мм.", 3, GRAY)
        page.text(23, 43, "Одинаковые изделия в разных положениях показаны отдельно. Полный состав и количества приведены в ведомостях.", 2.7, GRAY)
        page.layout["component_legend"] = []
        for index, entry in enumerate(entries[offset:offset+12]):
            column, row = index % 4, index // 4
            x, y, w, h = 23+column*137.75, 49+row*101, 134.75, 98
            page.rect(x, y, w, h, "#ccd4dc", weight=.12)
            label = f"ПФ{offset+index+1:03d}. {entry['code']}"
            title = wrap(label, w-8, 2.8, True)
            filename = wrap(entry["filename"], w-8, 2.15)
            locations = wrap("Узлы: "+compact_marks(entry["locations"]), w-8, 2.3)
            # Very long real labels are not silently truncated. Continue the
            # text below the diagram; enough space is allocated dynamically.
            title_h = len(title)*4
            footer_h = (len(filename)+len(locations))*3.4+10
            plot_top, plot_bottom = y+title_h+5, y+h-footer_h
            if plot_bottom-plot_top < 12:
                raise ValueError("Подписи компонента не помещаются в графическую легенду; сократите название изделия")
            for li, text in enumerate(title):
                page.text(x+4, y+5+li*4, text, 2.8, INK, True)
            factor = _scale(max(entry["width_mm"]/(w-20), entry["height_mm"]/(plot_bottom-plot_top-4), .25))
            dw, dh = entry["width_mm"]/factor, entry["height_mm"]/factor
            ox, oy = x+(w-dw)/2, plot_top+(plot_bottom-plot_top-dh)/2
            transform = lambda p: (ox+p[0]/factor, oy+dh-p[1]/factor)
            # Fill only saved closed wires; do not close open geometry or
            # create a rectangle/circle in place of a missing section.
            if entry["loops"]:
                page.region([[transform(p) for p in loop] for loop in entry["loops"]], "none", LIGHT, 0)
            for path in entry["paths"]:
                page.polygon([transform(p) for p in path], INK, weight=.2, closed=False)
            scale_label = f"М {num(1/factor)}:1" if factor < 1 else f"М 1:{num(factor)}"
            page.text(x+w-4, plot_bottom+3, scale_label, 2.4, BLUE, anchor="end")
            page.text(x+4, plot_bottom+3, f"Сечение {num(entry['width_mm'])} x {num(entry['height_mm'])}", 2.3, GRAY)
            ty = plot_bottom+7
            for text in filename:
                page.text(x+4, ty, text, 2.15, GRAY)
                ty += 3.4
            for text in locations:
                page.text(x+4, ty, text, 2.3, BLUE)
                ty += 3.4
            page.references.extend(value for value in [entry["source_id"], *entry["assembly_ids"]] if value not in page.references)
            page.layout["component_legend"].append({
                "mark": f"ПФ{offset+index+1:03d}", "component_id": entry["id"], "source_id": entry["source_id"],
                "code": entry["code"], "filename": entry["filename"], "locations": entry["locations"],
                "assembly_ids": entry["assembly_ids"], "scale": factor, "box": (x,y,w,h),
                "section_size_mm": [entry["width_mm"], entry["height_mm"]],
            })
        sheets.append(page)
    return sheets
