"""Explicit finished levels with labelled model defaults, not survey claims."""
from .scene import GRAY, INK


def view_levels(project, floor_id, space_id):
    floor = next(f for b in project.buildings for f in b.floors if f.id == floor_id)
    space = next((s for s in floor.spaces if s.id == space_id), None)
    result = []
    for kind, label in (("finished_floor", "у.ч.п."), ("finished_ceiling", "ур. чист. пот.")):
        candidates = [level for level in floor.levels if level.kind == kind]
        distinct = {float(level.elevation_mm) for level in candidates}
        if len(distinct) == 1:
            result.append({"kind": kind, "label": label, "elevation_mm": distinct.pop(), "basis": "explicit"})
        elif len(distinct) > 1:
            result.append({"kind": kind, "label": label, "elevation_mm": None, "basis": "ambiguous"})
        elif kind == "finished_floor":
            result.append({"kind": kind, "label": label, "elevation_mm": 0., "basis": "model"})
        elif space is not None:
            result.append({"kind": kind, "label": label, "elevation_mm": float(space.height_mm), "basis": "model"})
        else:
            result.append({"kind": kind, "label": label, "elevation_mm": None, "basis": "missing"})
    return result


def mounting_view_levels(project, preview):
    """Exterior finish limits are not room floor or ceiling elevations."""
    wall = preview["wall"]
    if (preview.get("configuration", {}).get("installation_zone") == "exterior"
            or wall.get("installation_zone") == "exterior"):
        base = float(wall.get("base_mm", 0))
        return [
            {"kind": "cladding_bottom", "label": "низ облицовки", "elevation_mm": base, "basis": "model"},
            {"kind": "cladding_top", "label": "верх облицовки", "elevation_mm": base + float(wall["height_mm"]), "basis": "model"},
        ]
    return view_levels(project, wall["floor_id"], wall.get("space_id"))


def draw_levels(sheet, levels, transform, length_mm, base_mm, height_mm, *, absolute_offset_mm=0):
    """Place a level only at its actual elevation, never at a convenient edge."""
    notes=[]
    for level in levels:
        elevation = level["elevation_mm"]
        label = level["label"] + (" (по модели)" if level["basis"] == "model" else "")
        if elevation is None:
            notes.append(label+": "+("несколько отметок" if level["basis"] == "ambiguous" else "не задан"))
            continue
        value = f"{(elevation+absolute_offset_mm)/1000:+.3f}".replace(".",",")
        if elevation < base_mm-1e-6 or elevation > base_mm+height_mm+1e-6:
            notes.append(label+" "+value+" (вне вида)")
            continue
        lx,ly=transform(length_mm,elevation-base_mm)
        sheet.line(lx-3,ly,lx+4,ly,INK,.15)
        sheet.line(lx+1,ly,lx+2.5,ly-1.5,INK,.15)
        sheet.line(lx+1,ly,lx-.5,ly-1.5,INK,.15)
        sheet.text(lx+5,ly-1,value+(" абс." if absolute_offset_mm else ""),2.5,INK)
        sheet.text(lx+5,ly+3,label,2.5,GRAY)
    return notes
