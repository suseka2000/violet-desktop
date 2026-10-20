"""Limit derived corner kits to the actual fitted panel edges, in world Z."""
from __future__ import annotations

import math

from .mount_assemblies import digest
from .room_joints import EPS, _height_intervals, _intersect_heights


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Некорректная отметка согласованного угла")
    return value


def _bands(value):
    if not isinstance(value, list) or not value:
        raise ValueError("У согласованного угла нет общего участка панелей")
    output = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("Некорректные границы углового участка")
        low, high = map(_number, item)
        if high <= low or (output and low < output[-1][1] - EPS):
            raise ValueError("Угловые участки имеют пересекающиеся или неверные границы")
        output.append([low, high])
    return output


def refine_fitted_joints(result, previews_by_index):
    """Return a shallow derived result; never edit source placements/previews.

    A local finish clearance changes the panel, not the measured doorway or
    physical wall. Corner stock must finish at the remaining panel edge too.
    Original bands are retained so repeated retries are idempotent and cannot
    successively shorten the same corner. Unchanged joints retain their IDs.
    """
    walls = result.get("walls", [])
    joints = []
    height_cache = {}
    checked = set()
    for joint in result.get("joints", []):
        original = _bands(joint.get("source_segments_z_mm", joint.get("segments_z_mm")))
        intervals = original
        sides = joint.get("sides")
        if not isinstance(sides, list) or len(sides) != 2:
            raise ValueError("У углового комплекта должны быть две согласованные стороны")
        for side in sides:
            index = side.get("wall_index")
            if type(index) is not int or not 0 <= index < len(walls) or index not in previews_by_index:
                raise ValueError("Для угла отсутствует согласованная развёртка стены")
            preview = previews_by_index[index]
            if index not in checked:
                panels = preview.get("panels")
                if not isinstance(panels, list):
                    raise ValueError("В согласованной развёртке отсутствуют панели")
                for panel in panels:
                    if not isinstance(panel, dict):
                        raise ValueError("Некорректная панель согласованной стены")
                    values = {key: _number(panel.get(key)) for key in ("x_mm", "y_mm", "width_mm", "height_mm")}
                    if values["width_mm"] <= 0 or values["height_mm"] <= 0:
                        raise ValueError("Панель согласованной стены должна иметь положительный размер")
                checked.add(index)
            placement = walls[index]
            x = _number(side.get("panel_end_local_x_mm")) - _number(placement.get("room_span", {}).get("start_mm", 0.))
            translation = placement.get("translation_mm")
            if not isinstance(translation, (list, tuple)) or len(translation) != 3:
                raise ValueError("Не задано положение согласованной стены по высоте")
            elevation = _number(translation[2])
            key = (index, x, elevation)
            if key not in height_cache:
                height_cache[key] = _height_intervals(preview, x, elevation)
            intervals = _intersect_heights(intervals, height_cache[key])
        if not intervals or any(high - low < 100. for low, high in intervals):
            raise ValueError("После монтажного зазора у угла нет целого участка панелей высотой не менее 100 мм")
        current = _bands(joint.get("segments_z_mm"))
        if (len(current) == len(intervals) and
                all(abs(a - b) <= EPS for old, new in zip(current, intervals) for a, b in zip(old, new))):
            joints.append(joint)
            continue
        refined = {key: value for key, value in joint.items() if key != "id"}
        refined.update(segments_z_mm=intervals, source_segments_z_mm=original,
                       source_joint_id=joint.get("source_joint_id", joint["id"]))
        refined["id"] = digest(refined)
        joints.append(refined)
    return {**result, "joints": joints}
