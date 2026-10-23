"""Shared, deterministic setting-out of panel cells before opening subtraction.

Nominal panel sizes are upper bounds. Hidden mounting may need narrower infill
panels so an opening does not leave a sliver that its edge groove cuts through.
This changes the drawing itself, never just the solids passed to the CAD kernel.
"""
from __future__ import annotations

import math

LAYOUT_VERSION = "concealed-safe-infill/2"
EPS = 1e-7


def layout_pieces(length, height, config, openings, *, terminal_infill_mm=0.):
    """Shared visible panel layout; no STEP, node selection or native solids.

    The customer viewer and album must use the same opening subtraction and
    concealed-system infills as the engineering preview, not a second grid.
    """
    from .engineering import _subtract_rectangles
    usable = height - config.floor_gap_mm - config.ceiling_gap_mm
    if length <= 1e-6 or usable <= 0:
        raise ValueError("Размер стены и зазоры не оставляют места для панелей")
    if not all(math.isfinite(value) for value in (
            length / (config.panel_width_mm + config.joint_mm),
            usable / (config.panel_height_mm + config.joint_mm))):
        raise ValueError("Размер панели не представим с точностью расчёта; проверьте единицы размеров")
    columns, rows, layout = concealed_layout(length, usable, config.floor_gap_mm,
        config, openings, terminal_infill_mm=terminal_infill_mm)
    cutters = [(o["x_mm"], o["y_mm"], o["width_mm"], o["height_mm"]) for o in openings]
    panels = []
    for row, (y, h) in enumerate(rows):
        for col, (x, w) in enumerate(columns):
            if min(w, h) <= 1e-6:
                continue
            cell_id = f"panel-{row+1}-{col+1}"
            for part, (px, py, pw, ph) in enumerate(_subtract_rectangles((x, y, w, h), cutters, EPS), 1):
                if min(round(pw, 6), round(ph, 6)) <= 0:
                    continue
                panels.append({"id": f"{cell_id}-{part}", "panel_id": cell_id, "index": len(panels)+1,
                    "x_mm": round(px, 6), "y_mm": round(py, 6), "width_mm": round(pw, 6), "height_mm": round(ph, 6),
                    "polygon": [[px, py], [px+pw, py], [px+pw, py+ph], [px, py+ph]], "holes": [],
                    "area_m2": round(pw*ph/1e6, 9), "is_cut": pw != config.panel_width_mm or ph != config.panel_height_mm,
                    "thickness_mm": config.panel_thickness_mm})
    return panels, columns, rows, layout


def regular_axis(length, maximum, gap):
    return [(i * (maximum + gap), min(maximum, length - i * (maximum + gap)))
            for i in range(math.ceil(length / (maximum + gap)))
            if length - i * (maximum + gap) > EPS]


def _merge(intervals):
    result = []
    for low, high in sorted(intervals):
        if high < low - EPS:
            continue
        if result and low <= result[-1][1] + EPS:
            result[-1] = (result[-1][0], max(result[-1][1], high))
        else:
            result.append((low, high))
    return result


def _intersection(first, second):
    result, a, b = [], 0, 0
    while a < len(first) and b < len(second):
        low, high = max(first[a][0], second[b][0]), min(first[a][1], second[b][1])
        if high >= low - EPS:
            result.append((low, max(low, high)))
        if first[a][1] < second[b][1]:
            a += 1
        else:
            b += 1
    return _merge(result)


def _nearest(intervals, target):
    return min((max(low, min(high, target)) for low, high in intervals),
               key=lambda value: (abs(value - target), value))


def safe_axis(length, maximum, gap, opening_edges, minimum, *, max_cells=None):
    """Shortest feasible sequence of panels, with nearest nominal seam positions.

Intervals represent reachable seam centres. Their continuous propagation is
bounded by the number of opening edges, not a millimetre search grid. Keeping
the earlier reachable layers permits backtracking instead of greedily painting
the final panel into a narrow corner. None means no safe layout exists without
changing source dimensions; the caller retains explicit downstream diagnostics.
"""
    if length <= maximum + EPS:
        return [(0., length)]
    if maximum < minimum - EPS:
        return None
    blocked = _merge((max(0., edge - minimum - gap / 2),
                      min(length, edge + minimum + gap / 2))
                     for edge in opening_edges if 0 < edge < length)
    allowed, cursor = [], 0.
    for low, high in blocked:
        if low >= cursor:
            allowed.append((cursor, low))
        cursor = max(cursor, high)
    if cursor <= length:
        allowed.append((cursor, length))
    reachable = _intersection(allowed, [(minimum + gap / 2, maximum + gap / 2)])
    layers = []
    terminal = [(length - maximum - gap / 2, length - minimum - gap / 2)]
    max_panels = int((length + gap) // (minimum + gap))
    if max_cells is not None:max_panels=min(max_cells,max_panels)
    for seam_count in range(1, max_panels):
        if not reachable:
            break
        layers.append(reachable)
        final = _intersection(reachable, terminal)
        if final:
            seams = [_nearest(final, seam_count * (maximum + gap) - gap / 2)]
            for index in range(seam_count - 2, -1, -1):
                previous = _intersection(layers[index],
                                         [(seams[-1] - maximum - gap,
                                           seams[-1] - minimum - gap)])
                seams.append(_nearest(previous, (index + 1) * (maximum + gap) - gap / 2))
            seams.reverse()
            starts = [0., *(value + gap / 2 for value in seams)]
            ends = [*(value - gap / 2 for value in seams), length]
            return [(round(start, 6), round(end - start, 6)) for start, end in zip(starts, ends)]
        reachable = _intersection(allowed, _merge((low + minimum + gap, high + maximum + gap)
                                                   for low, high in reachable))
    return None


def concealed_layout(length, usable_height, floor_gap, config, openings, *, terminal_infill_mm=0.):
    """Return common X/Y cell spans and an auditable layout explanation.

60 mm is a project setting-out allowance, NOT a manufacturer strength limit.
It clears the measured VH-2/VH-3 and V-L-start transverse profile envelopes
and two 16 mm grooves with a remaining web. Native checks stay authoritative.
Room-derived walls may additionally reserve that width at both terminal
panels: a valid groove alone does not clear the neighbouring corner support.
"""
    if (type(terminal_infill_mm) not in (int, float) or not math.isfinite(terminal_infill_mm)
            or not 0 <= terminal_infill_mm <= 1000):
        raise ValueError("Недопустимый минимальный крайний добор")
    x_regular = regular_axis(length, config.panel_width_mm, config.joint_mm)
    y_regular = regular_axis(usable_height, config.panel_height_mm, config.joint_mm)
    x_spans, y_spans = x_regular, y_regular
    report = {"version": LAYOUT_VERSION, "status": "regular", "adjusted_axes": [],
              "nominal_width_mm": config.panel_width_mm, "nominal_height_mm": config.panel_height_mm}
    if config.mounting_system == "open_mount":
        # Preserve existing V0 panels, including legitimate narrow notches.
        # Repair only a proven uncovered exterior strip: a terminal joint is
        # not a third perimeter allowance. Keep all nominal maxima unchanged.
        for axis, length_value, maximum, original in (
            ("x", length, config.panel_width_mm, x_regular),
            ("y", usable_height, config.panel_height_mm, y_regular),
        ):
            if not original or original[-1][0]+original[-1][1] >= length_value-EPS:
                continue
            minimum=min(60., maximum/3)
            spans=safe_axis(length_value,maximum,config.joint_mm,[],minimum)
            report["minimum_infill_mm"]=minimum
            if spans is None:
                report.setdefault("unresolved_axes",[]).append(axis)
                report["status"]="no_safe_partition"
            else:
                report["adjusted_axes"].append(axis)
                report["status"]="adjusted"
                if axis=="x":x_spans=spans
                else:y_spans=spans
    if config.mounting_system == "concealed_mount" and config.panel_edge in {"Vb", "Vc", "Vd"}:
        minimum = 60.
        depth = 11. if config.panel_edge == "Vb" else 16.
        report["minimum_infill_mm"] = minimum
        failed = []
        for axis, length_value, maximum, original, cuts in (
            ("x", length, config.panel_width_mm, x_regular,
             [(opening["x_mm"], opening["x_mm"] + opening["width_mm"]) for opening in openings]),
            ("y", usable_height, config.panel_height_mm, y_regular,
             [(opening["y_mm"] - floor_gap, opening["y_mm"] + opening["height_mm"] - floor_gap) for opening in openings]),
        ):
            # Keep existing safe settings and their CAD identity untouched.
            # A complete narrow panel may receive two opposed grooves; a
            # notched leg at an opening needs at least one groove plus web.
            # The strict native planner still checks the actual active runs.
            unsafe = any(width < 2 * depth + 1 - EPS for _, width in original)
            # A nominal grid may end inside what would have been its last
            # joint, leaving an unfilled strip at the wall boundary. That is
            # not an extra floor/ceiling allowance and cannot be discarded.
            unsafe |= bool(original and original[-1][0] + original[-1][1] < length_value - EPS)
            unsafe |= any(0 < low - start < depth + 1 - EPS or 0 < start + width - high < depth + 1 - EPS
                          for start, width in original for low, high in cuts)
            axis_minimum = minimum
            if axis == "x" and terminal_infill_mm and len(original) > 1:
                terminal_unsafe = min(original[0][1], original[-1][1]) < terminal_infill_mm - EPS
                unsafe |= terminal_unsafe
                axis_minimum = max(axis_minimum, terminal_infill_mm)
                if terminal_unsafe:
                    report["terminal_infill_mm"] = terminal_infill_mm
                    report["terminal_infill_basis"] = "room_corner_support_clearance"
            if axis == "y" and config.ceiling_gap_mm >= 8 and len(original) > 1:
                # Exact V-Sky reaches39.8mm down the panel. The broadest VH
                # seam reaches33.5mm; a short top infill must not put those
                # two physical profiles through each other. Keep the seam,
                # changing the common drawing rather than omitting hardware.
                unsafe |= original[-1][1] < 39.8 + 33.5 - config.joint_mm / 2 + .1
                axis_minimum = 80.
            if not unsafe:
                continue
            edges = [value for cut in cuts for value in cut]
            spans = safe_axis(length_value, maximum, config.joint_mm, edges, axis_minimum)
            if spans is None:
                failed.append(axis)
            elif any(abs(a-c) > 1e-6 or abs(b-d) > 1e-6 for (a,b),(c,d) in zip(original,spans)) or len(original) != len(spans):
                report["adjusted_axes"].append(axis)
                if axis == "x":
                    x_spans = spans
                else:
                    y_spans = spans
        if failed:
            report["unresolved_axes"] = failed
        if report["adjusted_axes"]:
            report["status"] = "adjusted"
        elif failed:
            report["status"] = "no_safe_partition"
    report["column_widths_mm"] = [width for _, width in x_spans]
    report["row_heights_mm"] = [height for _, height in y_spans]
    report["columns"] = [{"start_mm": round(start, 6), "width_mm": round(width, 6)} for start, width in x_spans]
    report["rows"] = [{"start_mm": round(start + floor_gap, 6), "height_mm": round(height, 6)} for start, height in y_spans]
    return x_spans, [(start + floor_gap, height) for start, height in y_spans], report
