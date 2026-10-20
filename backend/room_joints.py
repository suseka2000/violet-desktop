"""Room setting-out at actual intersections of the finished wall planes.

The two 90-degree seats are the measured seats used by the local assemblies.
Panel ends and complete fastening rows are replanned together; a screw is never
shortened to disguise the overlap of two independently assembled walls.
"""
from __future__ import annotations

from copy import deepcopy
import math

from .mount_assemblies import digest

JOINT_POLICY = "room-joints/1.0"
CORNER_GAP_MM = 2.0
CUB_END_MM = 17.97232857055661
EPS = 1e-6


def _cross(a, b):
    return a[0]*b[1]-a[1]*b[0]


def _dot(a, b):
    return a[0]*b[0]+a[1]*b[1]


def _line_intersection(a, u, b, v):
    determinant = _cross(u, v)
    if abs(determinant) < EPS:
        return None
    delta = [b[k]-a[k] for k in range(2)]
    s, t = _cross(delta, v)/determinant, _cross(delta, u)/determinant
    return [a[k]+s*u[k] for k in range(2)], s, t


def _height_intervals(plan, x, elevation):
    # A corner beside a door is present only where both panels actually exist.
    intervals = sorted((p["y_mm"]+elevation, p["y_mm"]+p["height_mm"]+elevation)
                       for p in plan["panels"]
                       if p["x_mm"]-EPS <= x <= p["x_mm"]+p["width_mm"]+EPS)
    merged = []
    for low, high in intervals:
        if merged and low <= merged[-1][1]+EPS:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])
    return merged


def _intersect_heights(first, second):
    return [[max(a, c), min(b, d)] for a, b in first for c, d in second
            if min(b, d)-max(a, c) > EPS]


def _subtract_heights(intervals, blocked):
    result = [list(interval) for interval in intervals]
    for low, high in sorted(blocked):
        result = [part for start, end in result for part in
                  ([start, min(end, low)], [max(start, high), end])
                  if part[1]-part[0] > EPS]
    return result


def physical_wall_topology(floor):
    """Incident substrate rays and real opening voids, including unmounted walls."""
    result = []
    openings = {}
    for opening in floor.openings:
        openings.setdefault(opening.wall_id, []).append(opening)
    for wall in floor.walls:
        length = math.hypot(wall.end.x-wall.start.x, wall.end.y-wall.start.y)
        tangent = [(wall.end.x-wall.start.x)/length, (wall.end.y-wall.start.y)/length]
        elevation = floor.elevation_mm+wall.base_mm
        endpoints = []
        for point, x, sign in ((wall.start, 0., 1.), (wall.end, length, -1.)):
            voids = [[elevation+opening.sill_mm, elevation+opening.sill_mm+opening.height_mm]
                     for opening in openings.get(wall.id, [])
                     if opening.offset_mm-EPS <= x <= opening.offset_mm+opening.width_mm+EPS]
            endpoints.append({"baseline": [floor.origin.x+point.x, floor.origin.y+point.y],
                              "ray": [v*sign for v in tangent],
                              "heights": _subtract_heights([[elevation, elevation+wall.height_mm]], voids)})
        result.append({"wall": {"wall_id": wall.id}, "endpoints": endpoints})
    return result


def _sector_blocked_heights(a, b, ea, eb, physical, neighbours, tolerance):
    """Only adjacent physical rays bound a visible finish sector.

    Following A's finish normal selects the 90/180/270-degree room sector.
    A third wall strictly inside it hides that pair, even if that wall's face
    is unmounted. Opposite faces of one substrate are not separate obstacles.
    """
    direction = 1. if _cross(ea["ray"], a["normal"]) > 0 else -1.
    def angle(ray):
        return (direction*math.atan2(_cross(ea["ray"], ray), _dot(ea["ray"], ray))) % math.tau
    sweep = angle(eb["ray"])
    blocked, owners = [], set()
    excluded = {a["wall"]["wall_id"], b["wall"]["wall_id"]}
    for index in neighbours:
        item = physical[index]
        if item["wall"]["wall_id"] in excluded:
            continue
        for endpoint in item["endpoints"]:
            if (math.dist(endpoint["baseline"], ea["baseline"]) <= tolerance
                    and EPS < angle(endpoint["ray"]) < sweep-EPS):
                blocked.extend(endpoint["heights"])
                owners.add(item["wall"]["wall_id"])
    return blocked, sorted(owners)


def _wall_section_spans(contexts, joints, unresolved):
    """Rectangular finish cutouts represent end seats that change with height."""
    assigned = {index: [] for index in range(len(contexts))}
    for joint in joints:
        for side in joint['sides']:
            assigned[side['wall_index']].append((side,joint['segments_z_mm']))
    result = []
    for index, context in enumerate(contexts):
        length, elevation = context['length'], context['origin'][2]
        panels = context['plan']['panels']
        heights = [(p['y_mm'],p['y_mm']+p['height_mm']) for p in panels]
        events = {value for interval in heights for value in interval}
        for _,intervals in assigned[index]:
            events.update(value-elevation for interval in intervals for value in interval)
        sections = []
        ordered = sorted(events)
        for low,high in zip(ordered,ordered[1:]):
            mid=(low+high)/2
            if high-low<=EPS or not any(a-EPS<=mid<=b+EPS for a,b in heights):
                continue
            seats = {'start': [], 'end': []}
            for side,intervals in assigned[index]:
                if any(a-EPS<=mid+elevation<=b+EPS for a,b in intervals):
                    seats[side['end']].append(side['panel_end_local_x_mm'])
            if any(values and max(values)-min(values)>EPS for values in seats.values()):
                unresolved.append({'wall_indices':[index], 'segments_z_mm':[[low+elevation,high+elevation]],
                                   'reason':'На одной высоте торцу назначены несовместимые посадки углов'})
                continue
            span={'start_mm':seats['start'][0] if seats['start'] else 0.,
                  'end_mm':seats['end'][0] if seats['end'] else length,
                  'z_start_mm':low,'z_end_mm':high}
            if span['end_mm']-span['start_mm']<=200.:
                raise ValueError(f"После согласования углов сторона {context['wall']['label']} не оставляет места для каркаса и анкеров")
            if (sections and abs(sections[-1]['start_mm']-span['start_mm'])<EPS
                    and abs(sections[-1]['end_mm']-span['end_mm'])<EPS):
                sections[-1]['z_end_mm']=high
            else:sections.append(span)
        if not sections:
            result.append({'start_mm':0.,'end_mm':length});continue
        # Finish gaps at floor/ceiling are not little structural ledges. Extend
        # the first/last seat to the wall bounds; panels retain their own gaps.
        sections[0]['z_start_mm']=0.
        sections[-1]['z_end_mm']=context['plan']['wall']['height_mm']
        span={'start_mm':min(s['start_mm'] for s in sections),
              'end_mm':max(s['end_mm'] for s in sections)}
        if len(sections)>1:span['sections']=sections
        result.append(span)
    return result


def _endpoint_candidate_pairs(contexts, tolerance):
    """Spatial broad phase with the original lexicographic wall-pair order.

    Cell neighbours, followed by the exact distance predicate, preserve joins
    straddling a grid boundary. The narrow phase still tests all four endpoint
    combinations, so pair discovery never changes joint ordering or identity.
    """
    if (isinstance(tolerance, bool) or not isinstance(tolerance, (int, float))
            or not math.isfinite(tolerance) or tolerance < 0):
        raise ValueError("Допуск соединения стен должен быть конечным неотрицательным числом")
    # A double-width cell also avoids a near-boundary floating-point quotient
    # rounding two mathematically adjacent cells apart at large coordinates.
    cell_size = max(2*tolerance, EPS)
    cells, pairs = {}, set()
    for i, context in enumerate(contexts):
        for endpoint in context["endpoints"]:
            point = endpoint["baseline"]
            cell = (math.floor(point[0]/cell_size), math.floor(point[1]/cell_size))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j, other in cells.get((cell[0]+dx, cell[1]+dy), ()):
                        if j == i or context["wall"]["wall_id"] == contexts[j]["wall"]["wall_id"]:
                            continue
                        if math.dist(point, other) <= tolerance:
                            pairs.add((j, i))
            cells.setdefault(cell, []).append((i, point))
    return sorted(pairs)


def plan_room_joints(walls, records, tolerance=0.1, *, physical_walls=None):
    """Return all supported endpoint seats and their original-X wall spans.

    An endpoint uses the *selected face* normal. This distinguishes the two
    physical corners at a T junction and does not join opposite wall faces.
    Arbitrary angles retain an explicit unsupported result rather than turning
    a rigid 90-degree supplied profile into another product.
    """
    contexts = []
    for index, (wall, record) in enumerate(zip(walls, records)):
        plan = record.plan if hasattr(record, "plan") else record["plan"]
        rotation, origin = wall["rotation"], wall["translation_mm"]
        tangent = [rotation[0][0], rotation[1][0]]
        normal = [-rotation[0][1], -rotation[1][1]]
        length = plan["wall"]["length_mm"]
        endpoints = []
        for end, x, sign in (("start", 0., 1.), ("end", length, -1.)):
            front = [origin[k]+x*tangent[k] for k in range(2)]
            baseline = [front[k]-normal[k]*wall["depth_mm"] for k in range(2)]
            endpoints.append({"end": end, "x": x, "ray": [v*sign for v in tangent], "baseline": baseline})
        contexts.append({"index": index, "wall": wall, "plan": plan, "tangent": tangent,
                         "normal": normal, "origin": origin, "length": length, "endpoints": endpoints})
    physical = physical_walls
    if physical is None:
        physical = []
        seen = set()
        for context in contexts:
            identifier = context["wall"]["wall_id"]
            if identifier in seen:
                continue
            seen.add(identifier)
            elevation = context["origin"][2]
            endpoints = []
            for endpoint in context["endpoints"]:
                voids = [[elevation+opening["y_mm"], elevation+opening["y_mm"]+opening["height_mm"]]
                         for opening in context["plan"].get("openings", [])
                         if opening["x_mm"]-EPS <= endpoint["x"] <= opening["x_mm"]+opening["width_mm"]+EPS]
                endpoints.append({**endpoint, "heights": _subtract_heights(
                    [[elevation, elevation+context["plan"]["wall"]["height_mm"]]], voids)})
            physical.append({"wall": context["wall"], "endpoints": endpoints})
    # Reuse the same spatial broad phase. No scan of every building wall for
    # each corner, including when unmounted substrates add extra incident rays.
    combined = [*contexts, *physical]
    neighbours = {i: set() for i in range(len(contexts))}
    candidate_pairs = []
    for i, j in _endpoint_candidate_pairs(combined, tolerance):
        if j < len(contexts):
            candidate_pairs.append((i, j))
        elif i < len(contexts) <= j:
            neighbours[i].add(j-len(contexts))
    joints, unresolved = [], []
    spans = [{"start_mm": 0., "end_mm": item["length"]} for item in contexts]
    for i, j in candidate_pairs:
        a, b = contexts[i], contexts[j]
        if a["wall"]["wall_id"] == b["wall"]["wall_id"]:
            continue
        for ea in a["endpoints"]:
            for eb in b["endpoints"]:
                if math.dist(ea["baseline"], eb["baseline"]) > tolerance:
                    continue
                cosine = max(-1., min(1., _dot(ea["ray"], eb["ray"])))
                angle = math.degrees(math.acos(cosine))
                if abs(angle-180.) < 1e-5 and _dot(a["normal"], b["normal"]) < -1.+EPS:
                    continue  # back-to-back faces are not a finish seam
                if abs(angle-180.) < 1e-5 and _dot(a["normal"], b["normal"]) > 1.-EPS:
                    kind = "straight"
                    # Different finish depths require a stepped transition.
                    pa = [a["origin"][k]+ea["x"]*a["tangent"][k] for k in range(2)]
                    pb = [b["origin"][k]+eb["x"]*b["tangent"][k] for k in range(2)]
                    if math.dist(pa, pb) > tolerance:
                        unresolved.append({"wall_indices": [a["index"], b["index"]], "reason": "Разная глубина отделки на прямом стыке"})
                        continue
                    point = [(pa[k]+pb[k])/2 for k in range(2)]
                    local = [ea["x"], eb["x"]]
                elif abs(angle-90.) < 1e-5:
                    toward_a, toward_b = _dot(a["normal"], eb["ray"]), _dot(b["normal"], ea["ray"])
                    if min(toward_a, toward_b) > 1.-EPS:
                        kind = "internal_corner"
                    elif max(toward_a, toward_b) < -1.+EPS:
                        kind = "external_corner"
                    else:
                        continue
                    hit = _line_intersection(a["origin"], a["tangent"], b["origin"], b["tangent"])
                    if hit is None:
                        continue
                    point, *local = hit
                else:
                    if abs(_dot(a["normal"], eb["ray"])-_dot(b["normal"], ea["ray"])) < EPS:
                        unresolved.append({"wall_indices": [a["index"], b["index"]], "angle_deg": angle,
                                           "reason": "У имеющегося углового профиля фиксированный угол 90°"})
                    continue
                ia = _height_intervals(a["plan"], min(a["length"]-EPS, max(EPS, local[0])), a["origin"][2])
                ib = _height_intervals(b["plan"], min(b["length"]-EPS, max(EPS, local[1])), b["origin"][2])
                intervals = _intersect_heights(ia, ib)
                if not intervals:
                    continue
                blocked, _ = _sector_blocked_heights(a, b, ea, eb, physical, neighbours[i], tolerance)
                visible = _subtract_heights(intervals, blocked)
                if not visible:
                    continue
                intervals = visible
                ca, cb = a["plan"]["mount_configuration"], b["plan"]["mount_configuration"]
                hidden_a, hidden_b = ca.get("mounting_system") == "concealed_mount", cb.get("mounting_system") == "concealed_mount"
                if hidden_a != hidden_b or (hidden_a and ca.get("panel_edge") != cb.get("panel_edge")):
                    unresolved.append({"wall_indices": [a["index"], b["index"]], "reason": "На сторонах стыка назначены несовместимые кромки"})
                    continue
                if kind == "straight":
                    if abs(ca["joint_mm"]-cb["joint_mm"]) > EPS:
                        unresolved.append({"wall_indices": [a["index"], b["index"]], "reason": "На прямом стыке различаются межпанельные швы"})
                        continue
                    offsets = [ca["joint_mm"]/2]*2
                elif kind == "external_corner":
                    offsets = [CUB_END_MM]*2 if hidden_a else [0., ca["panel_thickness_mm"]+CORNER_GAP_MM]
                else:
                    offsets = [0., CORNER_GAP_MM]
                sides = []
                for index, (context, endpoint, x, offset) in enumerate(zip((a,b), (ea,eb), local, offsets)):
                    plan = context["plan"]
                    position = x+offset*(1 if endpoint["end"] == "start" else -1)
                    # A finish plane can legitimately extend past the end
                    # of the centreline on a re-entrant external corner.
                    sides.append({"wall_index": context["index"], "wall_id": context["wall"]["wall_id"],
                                  "face_id": context["wall"]["face_id"], "end": endpoint["end"],
                                  "local_x_mm": endpoint["x"], "front_intersection_local_x_mm": x,
                                  "panel_end_local_x_mm": position, "tangent": [*context["tangent"],0.],
                                  "normal": [*context["normal"],0.], "ray": [*endpoint["ray"],0.],
                                  "thickness_mm": plan["layers"]["panel_thickness_mm"],
                                  "depth_mm": context["wall"]["depth_mm"],
                                  "plan": deepcopy(plan), "layers": deepcopy(plan["layers"]), "configuration": deepcopy(plan["configuration"]),
                                  "mount_configuration": deepcopy(plan["mount_configuration"]),
                                  "substrate_front_mm": plan["layers"]["substrate_front_mm"],
                                  **{key: plan["configuration"][key] for key in ("substrate_thickness_mm", "anchor_embedment_mm", "anchor_pitch_mm", "screw_pitch_mm")}})
                role_configs = [ca,cb]
                # The head pocket belongs to the second panel. Choose a
                # feasible orientation of the same real angle and screw
                # when only one of the panels provides the needed width.
                if kind == "internal_corner" and cb["panel_thickness_mm"] < 10.8 <= ca["panel_thickness_mm"]:
                    sides.reverse();role_configs.reverse()
                    for side,offset in zip(sides,offsets):
                        side["panel_end_local_x_mm"] = side["front_intersection_local_x_mm"]+offset*(1 if side["end"] == "start" else -1)
                joint = {"policy": JOINT_POLICY, "kind": kind, "angle_deg": 90. if kind != "straight" else 180.,
                         "wall_indices": [side["wall_index"] for side in sides], "sides": sides,
                         "front_intersection_mm": [*point, min(a["origin"][2], b["origin"][2])],
                         "vertex_mm": [*ea["baseline"], min(a["origin"][2], b["origin"][2])],
                         "axes_a": sides[0]["ray"], "axes_b": sides[1]["ray"],
                         "panel_end_offsets_mm": offsets, "corner_gap_mm": CORNER_GAP_MM,
                         "segments_z_mm": intervals, "configuration_a": deepcopy(role_configs[0]), "configuration_b": deepcopy(role_configs[1])}
                joint["id"] = digest(joint)
                joints.append(joint)
    spans = _wall_section_spans(contexts,joints,unresolved)
    return {"policy": JOINT_POLICY, "joints": joints, "wall_spans": spans, "unresolved": unresolved}


def fitted_preview(project, wall_id, face_id, span, *, mounting=None, assets=None, transition_overrides=None):
    """Re-evaluate opening-aware panel cells for a derived room wall span."""
    from .wall_mount import WallMounting, validate_wall_mounting, preview_wall_mount, pinned_step_assets
    if mounting is not None and not isinstance(mounting,WallMounting):
        raise TypeError("Нужен проверенный монтаж сохранённого проекта")
    # Only this floor's geometry is edited. The pinned catalogue and other
    # floors remain read-only and need not be cloned for every fitted wall.
    virtual=project.model_copy(update={"buildings":[building.model_copy(update={"floors":[
        floor.model_copy(deep=True) if any(w.id==wall_id for w in floor.walls) else floor
        for floor in building.floors]}) for building in project.buildings]})
    original_wall = next(w for b in project.buildings for f in b.floors for w in f.walls if w.id == wall_id)
    floor = next(f for b in virtual.buildings for f in b.floors if any(w.id == wall_id for w in f.walls))
    wall = next(w for w in floor.walls if w.id == wall_id)
    face = next(f for f in wall.faces if f.id == face_id)
    dx, dy = original_wall.end.x-original_wall.start.x, original_wall.end.y-original_wall.start.y
    length = math.hypot(dx, dy); unit = [dx/length, dy/length]
    left, right = span["start_mm"], length-span["end_mm"]
    if face.side == "right":
        left, right = right, left
    wall.start = original_wall.start.model_copy(update={"x": original_wall.start.x+unit[0]*left, "y": original_wall.start.y+unit[1]*left})
    wall.end = original_wall.end.model_copy(update={"x": original_wall.end.x-unit[0]*right, "y": original_wall.end.y-unit[1]*right})
    fitted_length = math.hypot(wall.end.x-wall.start.x, wall.end.y-wall.start.y)
    keep = []
    for opening in floor.openings:
        if opening.wall_id != wall_id:
            keep.append(opening); continue
        low, high = max(0., opening.offset_mm-left), min(fitted_length, opening.offset_mm+opening.width_mm-left)
        # An opening that reaches the physical wall end is an open boundary,
        # not a jamb. Extending the finish plane around an external corner
        # must extend that void too; otherwise the derived preview invents an
        # unsupported panel strip in front of empty space. Interior openings
        # retain their original world coordinates and dimensions.
        if left < -EPS and opening.offset_mm <= EPS:
            low = 0.
        if right < -EPS and opening.offset_mm+opening.width_mm >= length-EPS:
            high = fitted_length
        if high-low > EPS:
            opening.offset_mm, opening.width_mm = low, high-low
            keep.append(opening)
    floor.openings = keep
    physical_opening_ids = {opening.id for opening in keep if opening.wall_id == wall_id}
    mounting = mounting if mounting is not None else validate_wall_mounting(virtual)
    binding = next(item for item in mounting.walls if (item.wall_id, item.face_id) == (wall_id, face_id))
    transition_clearances=[]
    if span.get('sections'):
        from .domain.models import Opening
        # Derive finish-only terminal notches using the same opening-aware
        # panel/frame planner. The saved project and actual substrate voids
        # remain untouched; each lower/upper joint receives its own end seat.
        for section_index, section in enumerate(span['sections']):
            for end, low, high in (
                ('start', 0., section['start_mm']-span['start_mm']),
                ('end', section['end_mm']-span['start_mm'], fitted_length),
            ):
                width=high-low
                if width<=EPS:
                    continue
                z=section['z_start_mm'];top=section['z_end_mm']
                height=top-z
                if height<=EPS:
                    continue
                offset=low if face.side=='left' else fitted_length-high
                identity='finish-cut-'+digest({'wall':wall_id,'face':face_id,'section':section,'end':end})
                if binding.configuration.mounting_system=='concealed_mount':
                    from .wall_systems import profile_footprint
                    # The L-start opening sill/head projects 2 mm past the
                    # panel edge. Reserve that actual stock projection plus
                    # 0.05 mm only in this finish notch, not the source void.
                    edge=high if end=='start' else low
                    for kind,structural in (('opening_sill',z),('opening_head',top)):
                        if not EPS<structural<wall.height_mm-EPS:continue
                        footprint=profile_footprint('lstart',-1 if kind=='opening_sill' else 1)
                        projection=footprint[1] if kind=='opening_sill' else -footprint[0]
                        override=(transition_overrides or {}).get(identity+':'+kind,{})
                        opposing=override.get('opposing_projection_mm',0.)
                        if type(opposing) not in (int,float) or not math.isfinite(opposing) or not 0<=opposing<=2.+EPS:
                            raise ValueError('Нет калиброванного встречного выступа профиля')
                        finish=structural+(-1 if kind=='opening_sill' else 1)*(projection+opposing+.05)
                        if kind=='opening_sill':z=max(0.,finish)
                        else:top=min(wall.height_mm,finish)
                        transition_clearances.append({'opening_id':identity,'kind':kind,'edge_mm':edge,
                            'profile_family':'lstart','structural_z_mm':structural,'finish_z_mm':finish,
                            'projection_mm':projection,'clearance_mm':.05,'scope':'finish_only',
                            **override})
                floor.openings.append(Opening(id=identity,name='Согласование торца отделки',
                    wall_id=wall_id,kind='passage',offset_mm=max(0.,offset),width_mm=width,
                    sill_mm=max(0.,z),height_mm=top-z))
    # Reserve a complete panel/joint/fastener cell beside each corner. A
    # standalone24mm VH2 panel clears two11mm grooves, but its +20mm screw
    # row lies only6mm from the corner and inside the neighbouring WT pocket.
    # Repartition the shared preview, not only the CAD bodies or one screw.
    terminal_infill_mm = 60. if binding.configuration.mounting_system == "concealed_mount" else 0.
    preview = preview_wall_mount(virtual, wall_id, face_id, binding.configuration, mounting.snapshot,
                                 assets if assets is not None else pinned_step_assets(mounting.snapshot), terminal_infill_mm=terminal_infill_mm)
    preview["wall"].update(modeled_substrate=wall.substrate, modeled_wall_thickness_mm=wall.thickness_mm)
    if transition_clearances:preview['wall']['finish_transition_clearances']=transition_clearances
    if span.get('sections'):
        preview['wall']['substrate_openings']=[deepcopy(opening) for opening in preview['openings']
                                              if opening['id'] in physical_opening_ids]
    if span["start_mm"] < -EPS or span["end_mm"] > length+EPS:
        preview["wall"]["substrate_span_mm"] = {
            "start_mm": max(0., -span["start_mm"]),
            "end_mm": min(fitted_length, length-span["start_mm"]),
        }
    return preview
