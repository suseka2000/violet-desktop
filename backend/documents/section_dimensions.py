"""Overall section dimensions anchored to the drawn material, not bbox corners.

All points and gaps are paper millimetres.  A saved section may be wider at its
head than at its foot; the unoccupied corner of its bounding box is therefore
not a valid origin for an extension line.  Extrema below come only from actual
paths, and each extension has its own origin.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .scene import BLUE, width


@dataclass(frozen=True)
class DimensionPlacement:
    axis: int
    side: int
    anchors: tuple[tuple[float, float], tuple[float, float]]
    coordinate: float
    reach: float


def dimension_placement(paths, axis=0, side=1, gap=7):
    """Choose true extremal anchors closest to the requested exterior side.

    ``axis=0`` measures width and ``axis=1`` height. ``side=-1`` is top/left
    and ``side=1`` bottom/right in paper space. This also works with translated
    or reflected sections and disconnected component paths.
    """
    if axis not in (0, 1) or side not in (-1, 1) or gap <= 0:
        raise ValueError("Invalid section dimension axis, side or gap")
    points = [tuple(point) for path in paths for point in path]
    if not points:
        return None
    transverse = 1-axis
    low, high = min(p[axis] for p in points), max(p[axis] for p in points)
    if high-low < .01:
        return None
    tolerance = max(1e-8, (high-low)*1e-9)
    anchors = tuple(min((p for p in points if abs(p[axis]-edge) <= tolerance),
                        key=lambda p: (-side*p[transverse], p[axis])) for edge in (low, high))
    outside = min(p[transverse] for p in points) if side < 0 else max(p[transverse] for p in points)
    coordinate = outside+side*gap
    return DimensionPlacement(axis, side, anchors, coordinate,
                              sum(abs(coordinate-p[transverse]) for p in anchors))


def _label(placement, value, vertical_size=2.5):
    a, b = placement.anchors
    if placement.axis == 0:
        return ((a[0]+b[0])/2, placement.coordinate-1.2, 2.8, "middle")
    return (placement.coordinate+placement.side*1.5, (a[1]+b[1])/2, vertical_size,
            "end" if placement.side < 0 else "start")


def _fits(placement, value, bounds, vertical_size=2.5):
    if bounds is None:
        return True
    left, top, right, bottom = bounds
    x, y, size, anchor = _label(placement, value, vertical_size)
    length = width(value, size)
    tx = x-length if anchor == "end" else x-length/2 if anchor == "middle" else x
    a, b = placement.anchors
    if placement.axis == 0:
        geom_left, geom_right = a[0]-.7, b[0]+.7
        geom_top = min(a[1], b[1], placement.coordinate-1.2)
        geom_bottom = max(a[1], b[1], placement.coordinate+1.2)
    else:
        geom_left = min(a[0], b[0], placement.coordinate-1.2)
        geom_right = max(a[0], b[0], placement.coordinate+1.2)
        geom_top, geom_bottom = a[1]-.7, b[1]+.7
    return (min(tx, geom_left) >= left and max(tx+length, geom_right) <= right and
            min(y-size, geom_top) >= top and max(y+size*.25, geom_bottom) <= bottom)


def _draw(sheet, placement, value, vertical_size=2.5):
    first = len(sheet.primitives)
    a, b = placement.anchors
    coordinate, side = placement.coordinate, placement.side
    if placement.axis == 0:
        for point in (a, b):
            sheet.line(*point, point[0], coordinate+side*1.2, BLUE, .15)
            sheet.line(point[0]-.7, coordinate+.7, point[0]+.7, coordinate-.7, BLUE, .25)
        sheet.line(a[0], coordinate, b[0], coordinate, BLUE, .18)
    else:
        for point in (a, b):
            sheet.line(*point, coordinate+side*1.2, point[1], BLUE, .12)
            sheet.line(coordinate-.7, point[1]+.7, coordinate+.7, point[1]-.7, BLUE, .2)
        sheet.line(coordinate, a[1], coordinate, b[1], BLUE, .15)
    x, y, size, anchor = _label(placement, value, vertical_size)
    sheet.text(x, y, value, size, BLUE, anchor=anchor)
    base = (a[0], coordinate) if placement.axis == 0 else (coordinate, a[1])
    sheet.register_dimension(range(first, len(sheet.primitives)), a, b, base, value,
                             direction=(1., 0.) if placement.axis == 0 else (0., 1.))


def draw_section_dimensions(sheet, paths, width_label, height_label=None, *, bounds=None,
                            horizontal_gap=7, vertical_gap=6, vertical_size=2.5, obstacles=()):
    """Draw the shortest valid exterior dimensions without changing view scale.

    Bounds exclude the title, legend and footer. If neither side fits, the
    caller can allocate a larger cell; silently crossing its text is not an
    option. For equally good sides retain bottom-width / left-height placement.
    """
    paths = list(paths)
    obstacle_points = [point for path in obstacles for point in path]
    placements = []
    for axis, label, gap, preferred in ((0, width_label, horizontal_gap, 1),
                                        (1, height_label, vertical_gap, -1)):
        if label is None:
            continue
        candidates = [dimension_placement(paths, axis, side, gap) for side in (preferred, -preferred)]
        candidates = [p for p in candidates if p is not None]
        if obstacle_points:
            exterior = (min(point[1-axis] for point in obstacle_points), max(point[1-axis] for point in obstacle_points))
            adjusted = []
            for placement in candidates:
                coordinate = min(placement.coordinate, exterior[0]-gap) if placement.side < 0 else max(placement.coordinate, exterior[1]+gap)
                adjusted.append(replace(placement, coordinate=coordinate,
                    reach=sum(abs(coordinate-point[1-axis]) for point in placement.anchors)))
            candidates = adjusted
        if not candidates:
            continue
        fitting = [p for p in candidates if _fits(p, label, bounds, vertical_size)]
        if not fitting:
            raise ValueError("Размеры сечения не помещаются в ячейке")
        chosen = min(fitting, key=lambda p: (round(p.reach, 8), p.side != preferred))
        _draw(sheet, chosen, label, vertical_size)
        placements.append(chosen)
    return placements
