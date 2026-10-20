"""Detailed local cuts with explicitly separated construction context.

Saved assembly contours stay exact, including real support and fastener bodies.
Only legacy panel/profile assemblies receive source-bound 2D hardware context.
Floor/ceiling fragments remain identified architectural drawing conventions,
not inferred building surveys or additional bodies in the solid assembly.
"""
from __future__ import annotations

import math

from .scene import INK, BLUE, GRAY, RED, wrap


def has_assembly_support(record):
    """Never illustrate duplicate backing/hardware over saved physical bodies."""
    return any(body.get("role") in {"fastener", "frame", "backing", "substrate"}
               for body in record.bodies)


def assembly_fasteners(record):
    return [body for body in record.bodies
            if body.get("kind") == "step" and body.get("role") == "fastener"]


def _bounds(paths):
    points = [p for path in paths for p in path]
    return ([min(p[a] for p in points) for a in (0, 1)],
            [max(p[a] for p in points) for a in (0, 1)])


def _distance_to_segment(point, start, end):
    delta = (end[0]-start[0], end[1]-start[1])
    length2 = delta[0]**2+delta[1]**2
    t = max(0., min(1., ((point[0]-start[0])*delta[0]+(point[1]-start[1])*delta[1])/length2)) if length2 else 0.
    return math.dist(point, (start[0]+t*delta[0], start[1]+t*delta[1]))


def construction_graphics(entry):
    record = entry["assembly"]
    kind = entry["node"]["kind"]
    exterior = (record.configuration.get("installation_zone") == "exterior"
                or entry.get("preview", {}).get("wall", {}).get("installation_zone") == "exterior")
    vertical = kind in {"floor", "ceiling", "opening_head", "opening_sill"}
    start = record.recipe_id.startswith(("floor-start1", "concealed-floor-start1"))
    canonical = (lambda p: (p[1], -p[0])) if start else (lambda p: (p[0], p[1]))
    direction = -1 if kind in {"ceiling", "opening_sill"} else 1
    orient = (lambda p: (-p[1], direction*p[0])) if vertical else (lambda p: (p[0], p[1]))
    parts, supports = [], []
    for component, body in zip(record.geometry_snapshot.result.components, record.bodies):
        paths = [[canonical(p) for p in path] for path in component.paths]
        loops = [[canonical(p) for p in loop] for loop in body.get("section_loops", [])]
        # A real 3D instance can lie outside this one section plane. It stays in
        # STEP and the BOM, but must not acquire an invented 2D silhouette.
        if not paths:
            continue
        parts.append({"id": str(component.id), "label": str(body.get("label") or component.label),
                      "paths": paths, "loops": loops, "role": body.get("role", "profile"),
                      "origin": "assembly", "source_id": body.get("source_id")})
    actual = list(parts)
    physical_support = has_assembly_support(record)
    actual_lo, actual_hi = _bounds([p for part in actual for p in part["paths"]])
    context_index = 0

    def add(label, paths, loops=(), role="context", source_id=None):
        nonlocal context_index
        context_index += 1
        part = {"id": f"К{context_index}", "label": label, "paths": paths,
                "loops": loops, "role": role, "origin": "context", "source_id": source_id}
        parts.append(part)
        return part

    def rectangle(label, lo, hi, role="backing"):
        ring = [(lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1]), (lo[0], lo[1])]
        return add(label, [ring], [ring], role)

    # Context is optional if the exact source is absent from the saved library.
    # The core assembly remains portable and never borrows an unpinned variant.
    symbols = {} if physical_support else entry.get("context_symbols", {})
    screw = symbols.get("screw")
    frames = [value for key in ("frame_ws", "frame_wt") if (value := symbols.get(key))]

    def symbol(value, mapper, label, role):
        if not value:
            return
        return add(label, [[mapper(p) for p in path] for path in value["paths"]],
                   [[mapper(p) for p in loop] for loop in value.get("loops", [])], role, value["source_id"])

    def screw_at(origin, vector):
        if not screw:
            return
        # Source extraction declares the longitudinal tip/head direction.
        head = screw.get("head_x_mm", screw["bounds_mm"]["min"][0])
        sign = screw.get("tip_direction", 1)
        mapper = lambda p: (origin[0]+vector[0]*(p[0]-head)*sign-vector[1]*p[1],
                            origin[1]+vector[1]*(p[0]-head)*sign+vector[0]*p[1])
        symbol(screw, mapper, "Саморез: контур STEP; положение проектное", "fastener")

    def support(a, b, normal, label, *, screw_count=2):
        """A broken backing strip and its outward local frame, in native mm.

        Normal always points AWAY from the panel, on either leg of either
        corner. Its thickness is a project choice, not a measured wall offset.
        """
        if physical_support:
            return
        rear_a = (a[0]+12*normal[0], a[1]+12*normal[1])
        rear_b = (b[0]+12*normal[0], b[1]+12*normal[1])
        ring = [a,b,rear_b,rear_a,a]
        add(label, [ring], [ring], "backing")
        length = math.hypot(b[0]-a[0], b[1]-a[1])
        tangent = ((b[0]-a[0])/length, (b[1]-a[1])/length)
        supports.append({"a":a,"b":b,"normal":normal,"tangent":tangent,"length":length,"screw_count":screw_count})

    floor_edge = None
    boundary = None
    panels = [part for part in actual if part["role"] == "panel"]
    if vertical and panels:
        panel_low = min(_bounds(part["paths"])[0][0] for part in panels)
        allowance = record.configuration.get("floor_gap_mm" if kind == "floor" else "ceiling_gap_mm", 0)
        if kind in {"floor", "ceiling"}:
            floor_edge = panel_low-float(allowance)
            # The allowance is measured from the PANEL, not from the lowest
            # profile/frame vertex. A concealed end cap can project below the
            # panel (V-L-start: 2 mm); a Start-1 plinth instead fills the whole
            # 59 mm allowance. Keep both values visible and never move a level
            # just to make the drawing look as though the hardware touches it.
            panel_points = [point for part in panels for path in part["paths"] for point in path]
            anchor = min((point for point in panel_points if abs(point[0]-panel_low) < 1e-7), key=lambda point: point[1])
            # A screw or bracket outside the chosen section still must not
            # penetrate the floor/ceiling. Saved body bounds are in the common
            # assembled 3D frame; X is the inward datum, except Start-1's Y.
            axis = 1 if start else 0
            body_extents = [body.get("bounds_mm", {}).get("min") for body in record.bodies]
            complete_bounds = bool(body_extents) and all(isinstance(value, (list, tuple)) and len(value) == 3 for value in body_extents)
            assembly_low = min(value[axis] for value in body_extents) if complete_bounds else actual_lo[0]
            boundary = {
                "kind": kind, "panel_clearance_mm": float(allowance),
                "assembly_clearance_mm": assembly_low-floor_edge,
                "clearance_basis": "all_3d_bodies" if complete_bounds else "visible_section",
                "panel_anchor": orient(anchor),
                "level_anchor": orient((floor_edge, anchor[1])),
            }
            if exterior:
                boundary["installation_zone"] = "exterior"
                add("Нижняя граница наружной облицовки" if kind == "floor" else "Верхняя граница наружной облицовки",
                    [[(floor_edge, actual_lo[1]-28), (floor_edge, actual_hi[1]+38)]], role="cladding_boundary")
            elif kind == "floor":
                # These are labelled local project fragments, not measured
                # screed/slab thicknesses. A ceiling is NOT a mirrored floor:
                # the source album has distinct suspended ceiling conditions,
                # none of which can be inferred from the wall's top datum.
                rectangle("Основание пола / проектный фрагмент",
                          (floor_edge-36, actual_lo[1]-28), (floor_edge-12, actual_hi[1]+38), "concrete")
                rectangle("Чистовой слой / проектный фрагмент",
                          (floor_edge-12, actual_lo[1]-28), (floor_edge, actual_hi[1]+38), "finish")
            else:
                add("Плоскость чистого потолка; состав потолка не задан",
                    [[(floor_edge, actual_lo[1]-28), (floor_edge, actual_hi[1]+38)]], role="ceiling_plane")
            if kind == "floor" and record.recipe_id.startswith("flush-backing-angle20"):
                screw_at((floor_edge+1, actual_hi[1]-9), (-1, 0))
    corners = "90-" in record.recipe_id
    if not corners:
        rear = actual_hi[1]
        lo, hi = actual_lo[0], actual_hi[0]
        if start:
            lo = 8
        if vertical and floor_edge is not None:
            lo = max(lo, floor_edge+3)
        # A local 12 mm project backing strip is identified in the legend;
        # no unknown global wall offset or support spacing is dimensioned.
        if hi-lo > 20:
            support((lo,rear),(hi,rear),(0,1),"Подоснова 12 мм (проектное решение)")
    else:
        # Separate broken fragments avoid suggesting a false continuous mitre
        # at a corner whose substrate has not been surveyed.
        internal = record.recipe_id.startswith(("internal90", "concealed-inner90"))
        panel_bounds=[_bounds(part["paths"]) for part in actual if part["role"]=="panel"]
        depth=max((value["bounds_mm"]["size"][1] for value in frames),default=0)
        # At an external corner the two backing normals point into the same
        # quadrant. Keep each local support past the other's entire envelope;
        # a narrow native WT may fit where a native 60 mm WS cannot. Never
        # shrink either profile or let perpendicular framing cross in space.
        clear_x=max((hi[0]+12+depth+1 for lo,hi in panel_bounds if hi[1]-lo[1]>hi[0]-lo[0]),default=0)
        clear_y=max((hi[1]+12+depth+1 for lo,hi in panel_bounds if hi[0]-lo[0]>hi[1]-lo[1]),default=0)
        for part in actual:
            if part["role"] != "panel":
                continue
            lo, hi = _bounds(part["paths"])
            if hi[0]-lo[0] > hi[1]-lo[1]:
                # Leave a broken 16 mm end near the unmeasured corner. A
                # native 60 mm stud then fits in the remaining 64 mm sample.
                a, b = lo[0]+min(16,(hi[0]-lo[0])*.25), hi[0]
                if not internal:
                    a=max(a,clear_x)
                rear = lo[1] if internal else hi[1]
                if b-a>12:
                    support((a,rear),(b,rear),(0,-1 if internal else 1),
                            "Подоснова стороны A / фрагмент 12 мм",screw_count=1)
            else:
                a, b = lo[1]+min(16,(hi[1]-lo[1])*.25), hi[1]
                if not internal:
                    a=max(a,clear_y)
                rear = lo[0] if internal else hi[0]
                if b-a>12:
                    support((rear,a),(rear,b),(-1 if internal else 1,0),
                            "Подоснова стороны Б / фрагмент 12 мм",screw_count=1)
    for backing in supports:
        a,b=backing["a"],backing["b"]
        tangent,normal=backing["tangent"],backing["normal"]
        middle=((a[0]+b[0])/2,(a[1]+b[1])/2)
        frame=next((value for value in frames if backing["length"] >= value["bounds_mm"]["size"][0]),None)
        if frame:
            flo, fhi = frame["bounds_mm"]["min"], frame["bounds_mm"]["max"]
            # The web (native Z=0) faces the backing, not the open flanges:
            # a 27 mm screw can therefore cross the 12 mm backing and web.
            # This is a rigid orientation of the measured section, no fitting
            # by stretching and no assertion of a verified 3D fastener seat.
            mapper = lambda p: (middle[0]+tangent[0]*(p[0]-(flo[0]+fhi[0])/2)+normal[0]*(12+fhi[1]-p[1]),
                                middle[1]+tangent[1]*(p[0]-(flo[0]+fhi[0])/2)+normal[1]*(12+fhi[1]-p[1]))
            symbol(frame, mapper, ("Стоечный профиль V-WS" if frame.get("family")=="frame_ws" else "Профиль V-WT")+": сечение STEP", "frame")
            # Two visible positions are a local graphic, not a fixing pitch.
            offsets=(-16,16) if backing["screw_count"]>1 and frame["bounds_mm"]["size"][0]>=40 else (0,)
            for along in offsets:
                screw_at((middle[0]+tangent[0]*along,middle[1]+tangent[1]*along),normal)
        elif screw:
            screw_at(middle,normal)
    for part in parts:
        part["paths"] = [[orient(p) for p in path] for path in part["paths"]]
        part["loops"] = [[orient(p) for p in loop] for loop in part["loops"]]
    low, high = _bounds([p for part in parts for p in part["paths"]])
    return {"parts": parts, "bounds": (low, high),
            "actual_paths": [p for part in parts if part["origin"] == "assembly" for p in part["paths"]],
            "physical_support": physical_support,
            "fastener_count": len(assembly_fasteners(record)),
            "section_fastener_count": sum(part["role"] == "fastener" for part in actual),
            "boundary": boundary,
            "level": (orient((floor_edge, actual_lo[1]-25)) if floor_edge is not None else None),
            "level_label": (("низ облицовки" if kind == "floor" else "верх облицовки") if exterior
                            else ("у.ч.п." if kind == "floor" else "ур. чист. пот."))}


def _draw_clearance(sheet, data, transform, num):
    """An editable dimension of the requested panel-to-building clearance.

    It occupies the open air on the panel's face side, rather than measuring
    the arbitrary extent of the local floor fragment or the assembly bbox.
    """
    boundary = data.get("boundary")
    if not boundary:
        return
    a, b = sorted((transform(boundary["panel_anchor"]), transform(boundary["level_anchor"])), key=lambda point: point[1])
    if b[1]-a[1] < 1e-7:
        return
    coordinate = max(a[0], b[0])+4.
    first = len(sheet.primitives)
    for px, py in (a, b):
        sheet.line(px, py, coordinate+1., py, BLUE, .12)
        sheet.line(coordinate-.6, py+.6, coordinate+.6, py-.6, BLUE, .18)
    sheet.line(coordinate, a[1], coordinate, b[1], BLUE, .15)
    # A very small true gap still needs legible text, outside the two ticks.
    label_y = (a[1]+b[1])/2+.8 if b[1]-a[1] >= 5. else a[1]-2.
    label = num(boundary["panel_clearance_mm"])
    sheet.text(coordinate+1.5, label_y, label, 2.5, BLUE)
    sheet.register_dimension(range(first, len(sheet.primitives)), a, b, (coordinate, a[1]), label,
                             direction=(0., 1.), value_mm=boundary["panel_clearance_mm"])
    for primitive in sheet.primitives[first:]:
        primitive["_clearance_dimension"] = True


def draw_construction_view(sheet, entry, box, num):
    from .assemblies import _hatch
    from .section_dimensions import draw_section_dimensions
    x, y, w, h = box
    record = entry["assembly"]
    title = wrap(f"{'Узлы' if len(entry.get('aliases', [])) > 1 else 'Узел'} {entry.get('display_mark', entry['mark'])}. {entry.get('display_label', entry['node']['label'])}", w-8, 3, True)
    for index, line in enumerate(title):
        sheet.text(x+3, y+5+index*4.4, line, 3, INK, True)
    top = y+10+len(title)*4.4
    data = entry.get("_construction_graphics")
    if data is None:
        data = construction_graphics(entry)
        entry["_construction_graphics"] = data
    low, high = data["bounds"]
    bw, bh = high[0]-low[0], high[1]-low[1]
    gx, gy, gw, gh = x+17, top+3, w*.62-25, h-(top-y)-45
    if min(gw, gh) < 22:
        raise ValueError("Подробный узел не помещается в ячейке")
    scale = next((v for v in (.5, 1, 2, 4, 5, 10, 20, 50) if max(bw/gw, bh/gh) <= v), None)
    if scale is None:
        raise ValueError("Подробный узел не помещается в ячейке")
    ox, oy = gx+(gw-bw/scale)/2, gy+(gh-bh/scale)/2
    transform = lambda p: (ox+(p[0]-low[0])/scale, oy+(high[1]-p[1])/scale)
    lx, ly, lw = x+w*.64, top+4, w*.36-6
    grouped = {}
    for part in data["parts"]:
        label = part['label'].replace("Параметрическая панель", "Панель").replace("Саморез: контур STEP; положение проектное", "Саморез / сечение STEP").replace("Стоечный профиль V-WS: сечение STEP", "Профиль V-WS / сечение STEP")
        grouped.setdefault((label, part['origin']), []).append(part['id'])
    labels = [(origin, wrap(f"{', '.join(ids)}. {label}", lw, 2.5)) for (label, origin), ids in grouped.items()]
    if ly+6+sum(len(lines)*3.5+1.8 for _, lines in labels) > y+h-34:
        raise ValueError("Состав подробного узла не помещается в ячейке")
    sheet.text(lx, ly, f"М {num(1/scale)}:1" if scale < 1 else f"М 1:{num(scale)}", 2.8, BLUE, True)
    ly += 6
    fills = {"panel": "#f6dfc2", "backing": "#dce8f0", "substrate": "#dce8f0", "adhesive": "#dde9d8", "concrete": "#ececea", "finish": "#eee0ec"}
    # Fills first, all outlines after: a second body cannot erase a thin profile.
    for part in data["parts"]:
        loops = [[transform(p) for p in loop] for loop in part["loops"]]
        if loops and part["role"] in fills:
            sheet.region(loops, "none", fills[part["role"]], 0)
        if part["role"] != "fastener":
            _hatch(sheet, loops, "#aaa5a0" if part["role"] == "panel" else "#9aa5ac", 1 if part["role"] in {"panel", "finish"} else -1)
    # Longitudinal fasteners are not hatched. Their actual cut silhouettes mask
    # support hatching where the shaft intentionally engages the material.
    # Draw them last, without changing their saved outlines or placements.
    ordered = sorted(data["parts"], key=lambda part: part["role"] == "fastener")
    for part in ordered:
        if part["role"] == "fastener" and part["loops"]:
            sheet.region([[transform(p) for p in loop] for loop in part["loops"]], "none", "#ffffff", 0)
        for path in part["paths"]:
            sheet.polygon([transform(p) for p in path], INK, weight=.28 if part["origin"] == "assembly" else .18, closed=False)
    number_positions = []
    for origin, lines in labels:
        if ly+len(lines)*3.5 > y+h-34:
            raise ValueError("Состав подробного узла не помещается в ячейке")
        for line in lines:
            sheet.text(lx, ly, line, 2.5, INK if origin == "assembly" else GRAY)
            ly += 3.5
        ly += 1.8
    # Dimension only the actual saved cut, never the invented extent of a
    # broken architectural fragment or a proposed fastening position.
    alo, ahi = _bounds(data["actual_paths"])
    dimension_start = len(sheet.primitives)
    draw_section_dimensions(sheet, [[transform(p) for p in path] for path in data["actual_paths"]],
        num(ahi[0]-alo[0]), num(ahi[1]-alo[1]), bounds=(x+3, top-3, lx-3, y+h-32),
        obstacles=[[transform(p) for p in path] for part in data["parts"] for path in part["paths"]],
        horizontal_gap=8, vertical_gap=8)
    _draw_clearance(sheet, data, transform, num)
    dimension_segments = [((primitive["x1"],primitive["y1"]),(primitive["x2"],primitive["y2"]))
                          for primitive in sheet.primitives[dimension_start:] if primitive["type"] == "line"]
    for part in data["parts"]:
        # Place callouts outside actual small profile contours, with leaders.
        px, py = transform(part["paths"][0][0])
        candidates = []
        for shift in (0, -7, 7, -14, 14, -21, 21, -28, 28, -35, 35):
            # 4 mm keeps the 2.5 mm discs clear of the material and leaves
            # room for exterior dimensions in a three-column A2 cell.
            for cx in (ox-4, ox+bw/scale+4):
                cy = py+shift
                if not (x+5 < cx < lx-5 and top+1 < cy < y+h-38):
                    continue
                if any(math.hypot(cx-a, cy-b) < 6 for a,b in number_positions):
                    continue
                if any(_distance_to_segment((cx,cy), a, b) < 3.1 for a,b in dimension_segments):
                    continue
                candidates.append((math.hypot(cx-px, cy-py), cx, cy))
        if not candidates:
            raise ValueError("Выноски подробного узла не помещаются в ячейке")
        _, cx, cy = min(candidates)
        number_positions.append((cx, cy))
        sheet.line(px, py, cx, cy, GRAY, .12)
        sheet.circle(cx, cy, 2.5, BLUE, "#ffffff", .16)
        sheet.text(cx, cy+.85, part["id"], 2.5, BLUE, anchor="middle")
    if data["level"]:
        px, py = transform(data["level"])
        sheet.polygon([(px-2,py-2),(px,py),(px+2,py-2)], INK, weight=.2, closed=False)
        sheet.line(px-2,py,px+10,py,INK,.15)
        sheet.text(px-2,py-3,data["level_label"],2.5,BLUE,anchor="end")
    note = (f"Поз. 1… - тела 3D-сборки; крепёж: {data['fastener_count']} шт. в местном фрагменте. К - архитектурное окружение, не тела сборки."
            if data["physical_support"] else
            "Поз. 1… - сборка STEP. К - проектный контекст крепления; шаг и количество крепежа этим разрезом не задаются.")
    sheet.paragraph(x+3, y+h-27, note, w-8, 2.5, GRAY, leading=3.5)
    boundary = data.get("boundary")
    panel_note = f"Панель {num(record.configuration['panel_thickness_mm'])} мм; шов {num(record.configuration['joint_mm'])} мм."
    if boundary:
        gap_note = f"{panel_note} От торца панели до {data['level_label']} {num(boundary['panel_clearance_mm'])} мм."
        if boundary["assembly_clearance_mm"] < -1e-5:
            gap_note += f" Пересечение уровня: {num(-boundary['assembly_clearance_mm'])} мм."
        sheet.text(x+3, y+h-14, gap_note, 2.5, RED if boundary["assembly_clearance_mm"] < -1e-5 else INK)
    else:
        sheet.text(x+3, y+h-14, panel_note+" Местный фрагмент.", 2.5)
    places = entry["locations"]
    sheet.paragraph(x+3, y+h-7, "Места: "+", ".join(places[:6])+(f"; ещё {len(places)-6} в реестре ZIP." if len(places)>6 else "."), w-8, 2.5, GRAY, leading=3.5)
    sheet.references.extend([record.id, entry["preview"]["wall"]["id"], entry["preview"]["wall"]["face_id"]])
