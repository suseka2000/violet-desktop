"""Opening-edge visibility shared by previews and engineering derivations.

An opening remains a void, not a guessed leaf/frame/reveal construction.
Only its actual perimeter next to finish material creates a junction. Zone
clipping never creates a new jamb, head or sill inside the original opening.
"""
from __future__ import annotations


def subtract_intervals(low, high, blocked, tolerance=1e-7):
    cursor = low
    for start, end in sorted(blocked):
        if end <= cursor + tolerance or start >= high - tolerance:
            continue
        if start > cursor + tolerance:
            yield cursor, min(start, high)
        cursor = max(cursor, end)
        if cursor >= high - tolerance:
            return
    if cursor < high - tolerance:
        yield cursor, high


def visible_finish_segments(start, end, void_rects, tolerance=1e-7):
    """Axis-aligned finish-boundary pieces outside the union of openings."""
    vertical = start[0] == end[0]
    low, high = (start[1], end[1]) if vertical else (start[0], end[0])
    blocked = []
    for x, y, width, height in void_rects:
        hits = x - tolerance <= start[0] <= x + width + tolerance if vertical else y - tolerance <= start[1] <= y + height + tolerance
        if hits:
            blocked.append((y, y + height) if vertical else (x, x + width))
    for low, high in subtract_intervals(low, high, blocked, tolerance):
        yield ((start[0], low), (start[0], high)) if vertical else ((low, start[1]), (high, start[1]))


def visible_opening_edges(opening_rect, finish_rect, void_rects=(), tolerance=1e-7):
    """Yield (kind, start_xy, end_xy) where finish adjoins a real hole edge.

    Coordinates are in the caller's elevation frame. ``void_rects`` may include
    the opening itself: only void on the *finish side* of an edge blocks it.
    This handles overlapping/touching openings and partial-height zones.
    """
    x, y, width, height = opening_rect
    fx, fy, fw, fh = finish_rect
    edges = [
        ("opening_head", False, y + height, x, x + width, 1),
        ("opening_sill", False, y, x, x + width, -1),
        ("opening_jamb_left", True, x, y, y + height, -1),
        ("opening_jamb_right", True, x + width, y, y + height, 1),
    ]
    for kind, vertical, coordinate, low, high, direction in edges:
        normal_low, normal_high = (fx, fx + fw) if vertical else (fy, fy + fh)
        if direction > 0:
            adjacent_finish = normal_low - tolerance <= coordinate < normal_high - tolerance
        else:
            adjacent_finish = normal_low + tolerance < coordinate <= normal_high + tolerance
        if not adjacent_finish:
            continue
        span_low, span_high = (fy, fy + fh) if vertical else (fx, fx + fw)
        low, high = max(low, span_low), min(high, span_high)
        if high - low <= tolerance:
            continue
        blocked = []
        for vx, vy, vw, vh in void_rects:
            void_low, void_high = (vx, vx + vw) if vertical else (vy, vy + vh)
            blocks = (void_low <= coordinate + tolerance and void_high > coordinate + tolerance) if direction > 0 else (void_low < coordinate - tolerance and void_high >= coordinate - tolerance)
            if blocks:
                blocked.append((vy, vy + vh) if vertical else (vx, vx + vw))
        for start, end in subtract_intervals(low, high, blocked, tolerance):
            yield kind, (coordinate, start) if vertical else (start, coordinate), (coordinate, end) if vertical else (end, coordinate)


def door_parameter_issues(opening):
    """Report legacy dimensional ambiguity without making old JSON unreadable.

    Earlier versions did not define whether a frame width included architraves.
    Values stay intact; a warning requests interpretation, not a guessed resize.
    """
    door = opening.door
    if door is None:
        return []
    result = []
    comparisons = [
        ("DOOR_LEAF_EXCEEDS_FRAME", door.leaf_width_mm, door.frame_width_mm,
         "Ширина полотен больше указанной ширины коробки. Уточните смысл и замеры обоих габаритов."),
        ("DOOR_FRAME_EXCEEDS_OPENING", door.frame_width_mm, opening.width_mm,
         "Ширина коробки больше проёма. Проверьте, не включены ли наличники или наружное обрамление; монтажный габарит коробки не определён."),
        ("DOOR_LEAF_EXCEEDS_OPENING", door.leaf_width_mm, opening.width_mm,
         "Ширина полотен больше проёма. Уточните тип двери и смысл введённых размеров; распашная посадка не подтверждена."),
    ]
    for code, value, limit, message in comparisons:
        if value is not None and limit is not None and value > limit:
            result.append({"code":code,"message":message,"severity":"warning",
                           "details":{"opening_id":opening.id,"leaf_width_mm":door.leaf_width_mm,
                                      "frame_width_mm":door.frame_width_mm,"opening_width_mm":opening.width_mm}})
    return result


def opening_details(opening, face_side):
    """Preserve entered parameters; explicitly project A/B onto each view."""
    door = opening.door.model_dump(mode="json") if opening.door is not None else None
    view_door = None
    if door is not None:
        handing = door["handing"]
        if face_side == "right" and handing in {"left", "right"}:
            handing = "right" if handing == "left" else "left"
        swing = door["opening_side"]
        view_door = {**door, "handing": handing,
                     "swing_relative_to_view": "unspecified" if swing == "unspecified" else "toward_viewer" if swing == face_side else "away_from_viewer"}
    return {"reveal_finish": opening.reveal_finish, "reveal_depth_mm": None,
            "door": door, "view_door": view_door,
            "door_parameter_basis": "source_wall_A_to_B", "view_reversed": face_side == "right",
            "detail_scope": "cladding_edges_only", "leaf_frame_geometry": "not_modeled",
            "parameter_issues": door_parameter_issues(opening)}
