"""Lossless mounting dimensions: short modules never disappear at small scales."""
from __future__ import annotations

from collections import defaultdict
import math

from .scene import BLUE, GRAY, width as text_width


def axis_runs(spans):
    """Group only consecutive equal modules with an equal actual gap."""
    runs = []
    for start, width in spans:
        start, width = float(start), float(width)
        previous = runs[-1] if runs else None
        gap = start - previous["end_mm"] if previous else None
        if previous and math.isclose(width, previous["width_mm"], abs_tol=1e-6) and (
            previous["count"] == 1 or math.isclose(gap, previous["gap_mm"], abs_tol=1e-6)
        ) and gap >= -1e-6:
            previous["count"] += 1
            previous["gap_mm"] = max(0., gap)
            previous["end_mm"] = start + width
        else:
            runs.append({"start_mm": start, "end_mm": start + width,
                         "width_mm": width, "gap_mm": 0., "count": 1})
    return runs


def run_label(run, num):
    if run["count"] == 1:
        return num(run["width_mm"])
    result = f"{run['count']} x {num(run['width_mm'])}"
    if run["gap_mm"]:
        result += f" + {run['count']-1} x {num(run['gap_mm'])}"
    return result + " = " + num(run["end_mm"]-run["start_mm"])


def draw_panel_chain(sheet, spans, ox, oy, scale, num, *, mark):
    """Print a complete repeated chain, and retain narrow runs in the schedule.

    A repeated 600 mm module can be less than the text width on paper. One
    dimension of its exact combined extent is legible and still editable.
    Unique tiny infills get a short leader rather than silently losing a size.
    """
    runs = axis_runs(spans)
    occupied = []
    for index, run in enumerate(runs):
        a, b = ox + run["start_mm"]/scale, ox + run["end_mm"]/scale
        label = run_label(run, num)
        tw = text_width(label, 2.8)
        if b-a >= tw+2:
            start = len(sheet.primitives)
            sheet.dimension(a, b, oy-5, oy, label, BLUE)
            for primitive in sheet.primitives[start:]:
                if "_dimension" in primitive:
                    primitive["_dimension"]["value_mm"] = run["end_mm"]-run["start_mm"]
            occupied.append((a+(b-a-tw)/2-1, a+(b-a+tw)/2+1))
        else:
            # Separate lane above the chain. Do not overlap another short label.
            short = num(run["width_mm"]) if run["count"] == 1 else f"{run['count']} x {num(run['width_mm'])}"
            short_w = text_width(short, 2.5)
            centre = (a+b)/2
            if not any(lo < centre+short_w/2+1 and centre-short_w/2-1 < hi for lo, hi in occupied):
                sheet.line(a, oy, a, oy-4, BLUE, .13)
                sheet.line(b, oy, b, oy-4, BLUE, .13)
                sheet.line(centre, oy-4, centre, oy-9, BLUE, .13)
                sheet.text(centre, oy-10, short, 2.5, BLUE, anchor="middle")
                occupied.append((centre-short_w/2-1, centre+short_w/2+1))
        sheet.layout.setdefault("mount_dimension_runs", []).append({"view_mark": mark, **run})


def setting_out_rows(walls, marks, num):
    """All X/Y chains and opening offsets, including sub-paper-size fragments."""
    from ..panel_layout import regular_axis
    rows = []
    for index, view in enumerate(walls, 1):
        mark, wall, cfg = f"М{index:03d}", view["wall"], view["configuration"]
        layout = view.get("layout", {})
        columns = layout.get("columns")
        spans = [(c["start_mm"], c["width_mm"]) for c in columns] if columns is not None else regular_axis(wall["length_mm"], cfg["panel_width_mm"], cfg["joint_mm"])
        for axis, values in (("X", spans), ("Y", [(r["start_mm"], r["height_mm"]) for r in layout.get("rows", [])])):
            for run in axis_runs(values):
                rows.append([mark, axis, num(run["start_mm"]), num(run["end_mm"]), run_label(run, num),
                             "От базы раскладки" if axis == "X" else "От низа стены"])
        rows.append([mark, "Низ / верх", num(cfg["floor_gap_mm"]), num(cfg["ceiling_gap_mm"]),
                     "Зазоры панели, мм", "По параметрам выбранной раскладки"])
        for opening in view["openings"]:
            rows.append([mark, marks.get(opening["id"], "Проём"), num(opening["x_mm"]),
                         num(opening["x_mm"]+opening["width_mm"]),
                         f"Низ {num(opening['y_mm'])}; верх {num(opening['y_mm']+opening['height_mm'])}",
                         f"Ш x В: {num(opening['width_mm'])} x {num(opening['height_mm'])} мм"])
    from .album import compact_marks
    grouped = {}
    for row in rows:
        grouped.setdefault(tuple(row[1:]), []).append(row[0])
    return [[compact_marks(views), *values] for values, views in grouped.items()]


def panel_piece_rows(walls, num):
    """Group equal rectangular pieces losslessly, without calling them blanks.

    The portable package retains every polygon and its local coordinates.
    Printed identifiers link each rectangle back to the numbered parent panel.
    """
    from .album import compact_marks, MATERIALS
    groups = {}
    for index, view in enumerate(walls, 1):
        cfg = view["configuration"]
        parents = defaultdict(list)
        for piece in view["panels"]:
            parents[piece.get("panel_id", piece.get("id", piece["index"]))].append(piece)
        labels = {}
        for pieces in parents.values():
            parent = f"П{pieces[0]['index']}"
            for part_index, piece in enumerate(pieces, 1):
                labels[id(piece)] = parent + (f".{part_index}" if len(pieces) > 1 else "")
        for piece in view["panels"]:
            # Non-rectangular records retain their distinction and references.
            polygon = piece.get("polygon", [])
            rectangle = len(set(tuple(p) for p in polygon)) == 4 and not piece.get("holes")
            key = (cfg["material"], cfg["panel_thickness_mm"], piece["width_mm"], piece["height_mm"], rectangle)
            item = groups.setdefault(key, {"count": 0, "views": defaultdict(list)})
            item["count"] += 1
            item["views"][f"М{index:03d}"].append(labels[id(piece)])
    rows = []
    for (material, thickness, width, height, rectangle), item in groups.items():
        places = []
        for mark, indices in item["views"].items():
            prefix, current = mark+": ", ""
            for reference in compact_marks(indices).split(", "):
                if current and len(prefix)+len(current)+len(reference)+2 > 700:
                    places.append(prefix+current)
                    current = ""
                current += (", " if current else "")+reference
            places.append(prefix+current)
        # Bound individual table cells, not the total number of references.
        chunks, current = [], ""
        for value in places:
            if current and len(current)+len(value) > 700:
                chunks.append(current); current = ""
            current += ("; " if current else "")+value
        chunks.append(current)
        for ci, locations in enumerate(chunks):
            rows.append([MATERIALS.get(material, material), num(thickness), num(width), num(height),
                         str(item["count"]) if ci == 0 else "продолжение", locations,
                         ("Прямоугольные части; без припусков" if rectangle else "Габарит; контур и отверстия в данных проекта")
                         + ". ПN.k: часть k панели N на развёртке"])
    return rows
