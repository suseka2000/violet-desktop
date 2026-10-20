"""Offline, exact-source 2D sections for a mounting detail's graphic context.

These are actual screw/frame/suspension contours, not generated rectangles.
Their later placement is a documented project drawing convention, NOT a new
collision-checked solid, fastening specification or an engineering approval.
Only an exact source hash in the project's pinned library enables a symbol.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import math
from pathlib import Path

from .mount_assemblies import digest

RESOURCE = Path(__file__).resolve().parents[1] / "resources/vendor/mount-context-symbols.json"
FAMILIES = frozenset({"screw", "frame_ws", "frame_wt", "suspension"})


def validate_context_symbols(data):
    if not isinstance(data, dict) or data.get("schema_version") != "mount-context-symbols/1.0":
        raise ValueError("Неизвестная версия контуров монтажного контекста")
    if data.get("fingerprint") != digest({key:value for key,value in data.items() if key != "fingerprint"}):
        raise ValueError("Контрольная сумма контуров монтажного контекста не совпадает")
    records = data.get("symbols")
    if not isinstance(records,list) or len(records) > 16:
        raise ValueError("Некорректная библиотека контуров монтажного контекста")
    seen=set()
    for record in records:
        key=(record.get("family"),record.get("source_id"))
        if key in seen or key[0] not in FAMILIES or not isinstance(key[1],str) or len(key[1]) != 64 or any(c not in "0123456789abcdef" for c in key[1]):
            raise ValueError("Некорректный источник монтажного контура")
        seen.add(key)
        if record.get("units") != "mm" or record.get("geometry_kind") != "actual_step_section" or record.get("placement_status") != "project_context_only":
            raise ValueError("Контур должен оставаться фактическим сечением с отдельно заданным графическим размещением")
        components=record.get("components")
        if not isinstance(components,list) or not components or len(components)>8:
            raise ValueError("Некорректный состав сечения монтажного элемента")
        total=0
        for component in components:
            loops=component.get("section_loops")
            paths=component.get("paths")
            if component.get("open_wire_count") != 0 or not isinstance(loops,list) or len(loops)!=component.get("closed_wire_count") or not isinstance(paths,list):
                raise ValueError("Сечение монтажного элемента должно содержать замкнутые контуры")
            for path in [*paths,*loops]:
                if not isinstance(path,list) or len(path)<2:
                    raise ValueError("Пустой контур монтажного элемента")
                for point in path:
                    if not isinstance(point,list) or len(point)!=2 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in point):
                        raise ValueError("Некорректные координаты монтажного элемента")
                total+=len(path)
            if any(len(loop)<4 or loop[0]!=loop[-1] for loop in loops):
                raise ValueError("Разрыв контура монтажного элемента")
        if total>20000:
            raise ValueError("Превышен лимит точек монтажного элемента")
    return data


@lru_cache(maxsize=1)
def _library():
    return validate_context_symbols(json.loads(RESOURCE.read_text(encoding="utf-8")))


def context_symbol(kind, pinned_assets):
    """Return one independent, native-mm section, or None without that source.

    `pinned_assets` is the result of wall_mount.pinned_step_assets(snapshot).
    The screw's longitudinal axis is +X from its head toward its point. WS/WT
    coordinates are original X/Z with their rear web at Z=0. The suspension is
    original X/Y. Callers may rigidly translate/rotate these coordinates, but
    must not nonuniformly scale them to impersonate a different stock part.
    """
    if kind not in FAMILIES:
        return None
    identifiers={asset.get("source_id") for asset in pinned_assets or [] if isinstance(asset,dict)}
    record=next((record for record in _library()["symbols"] if record["family"]==kind and record["source_id"] in identifiers),None)
    if record is None:
        return None
    value=deepcopy(record)
    value["paths"]=[path for component in value["components"] for path in component["paths"]]
    value["loops"]=[loop for component in value["components"] for loop in component["section_loops"]]
    value["fingerprint"]=digest(record)
    return value
