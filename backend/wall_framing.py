"""Deterministic, opening-aware setting-out for an entire mounted wall.

This is a geometric project adaptation of the supplied open-mount system, not
an anchor selection or a structural calculation.  All distances are millimetres.
The preview's elevation Y becomes assembly Z; assembly Y points into the wall.
No source project, snapshot or existing mounting assignment is modified.
"""
from __future__ import annotations

import math
from copy import deepcopy
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .domain.models import Model
from .mount_assemblies import digest

Positive = Annotated[float, Field(strict=True, allow_inf_nan=False, gt=0)]
EPS = 1e-6
FRAME_WIDTH = 28.0
BACKING_WIDTH = 60.0
FRAME_DEPTH = 27.0
ANCHOR_SCREW_CLEARANCE = 25.0
MEMBER_END_CLEARANCE = 20.0
HIDDEN_HEAD_CLEARANCE = 11.0


class WallFramingConfiguration(Model):
    frame_pitch_mm: Annotated[Positive, Field(ge=100, le=1200)] = 600
    screw_pitch_mm: Annotated[Positive, Field(ge=60, le=800)] = 400
    anchor_pitch_mm: Annotated[Positive, Field(ge=100, le=1200)] = 600
    screw_edge_mm: Annotated[Positive, Field(ge=15, le=150)] = 50
    anchor_edge_mm: Annotated[Positive, Field(ge=40, le=300)] = 100
    opening_edge_mm: Annotated[Positive, Field(ge=40, le=300)] = 100
    cavity_mm: Annotated[Positive, Field(ge=45, le=300)] = 80
    substrate_type: Literal["concrete", "solid_masonry", "hollow_masonry", "gypsum", "unknown"] = "concrete"
    substrate_thickness_mm: Annotated[Positive, Field(ge=60, le=1000)] = 160
    anchor_embedment_mm: Annotated[Positive, Field(ge=30, le=150)] = 50
    allow_project_assumptions: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def rear_cover(self):
        if self.substrate_thickness_mm < self.anchor_embedment_mm + 20:
            raise ValueError("После анкера должно оставаться не менее 20 мм основания")
        return self


DEFAULT_CONFIGURATION = WallFramingConfiguration().model_dump(mode="json")


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: требуется конечное число")
    if value < 0 or (positive and value <= 0) or value > 100_000 or not math.isfinite(value):
        raise ValueError(f"{name}: размер вне диапазона")
    return float(value)


def _rounded(value):
    return round(float(value), 6)


def substrate_interval(wall):
    """Actual substrate in fitted face coordinates, never an invented extension.

    Corner fitting can extend the finish beyond the source wall. Its physical
    substrate boundary then differs from the panel boundary on just one end;
    increasing one common XYZ anchor clearance cannot represent that geometry.
    The same strict interval is consumed by the planner and the native solids.
    """
    length = _number(wall.get("length_mm"), "wall.length_mm", positive=True)
    if "substrate_span_mm" not in wall:
        return 0., length
    span = wall["substrate_span_mm"]
    if not isinstance(span, dict) or set(span) != {"start_mm", "end_mm"}:
        raise ValueError("wall.substrate_span_mm: требуются только start_mm и end_mm")
    start = _number(span["start_mm"], "wall.substrate_span_mm.start_mm")
    end = _number(span["end_mm"], "wall.substrate_span_mm.end_mm", positive=True)
    if not start < end <= length:
        raise ValueError("wall.substrate_span_mm: основание должно находиться внутри длины стены")
    return start, end


def _rect(value, length, height, label):
    result = deepcopy(value)
    if not isinstance(value.get("id"), str) or not value["id"]:
        raise ValueError(f"{label}: требуется идентификатор")
    for key in ("x_mm", "y_mm", "width_mm", "height_mm"):
        result[key] = _number(value.get(key), f"{label}.{key}", positive=key in {"width_mm", "height_mm"})
    if result["x_mm"] + result["width_mm"] > length + EPS or result["y_mm"] + result["height_mm"] > height + EPS:
        raise ValueError(f"{label}: прямоугольник выходит за границы стены")
    if label in {'opening','substrate_opening'}:
        from .wall_opening_bounds import opening_cut_bounds
        x,z,right,top=opening_cut_bounds(result,length,height)
        result.update(x_mm=x,y_mm=z,width_mm=right-x,height_mm=top-z)
    return result


def substrate_openings(plan):
    """Actual voids only; finish-only corner notches do not remove concrete."""
    wall = plan["wall"]
    if "substrate_openings" not in wall:
        return plan.get("openings", [])
    values = wall["substrate_openings"]
    if not isinstance(values, list) or len(values) > 200:
        raise ValueError("wall.substrate_openings: требуется список фактических проёмов")
    known = {opening["id"]: opening for opening in plan.get("openings", [])}
    checked = []
    seen = set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Некорректный фактический проём основания")
        opening = _rect(value, wall["length_mm"], wall["height_mm"], "substrate_opening")
        original = known.get(opening["id"])
        if opening["id"] in seen or original is None or any(
            abs(opening[key]-original[key]) > EPS for key in ("x_mm", "y_mm", "width_mm", "height_mm")
        ):
            raise ValueError("Фактический проём основания не соответствует раскладке стены")
        seen.add(opening["id"])
        checked.append(opening)
    return checked


def _subtract(lo, hi, blocked):
    spans = [(lo, hi)]
    for a, b in sorted(blocked):
        updated = []
        for left, right in spans:
            if b <= left + EPS or a >= right - EPS:
                updated.append((left, right))
            else:
                if a > left + EPS:
                    updated.append((left, min(a, right)))
                if b < right - EPS:
                    updated.append((max(b, left), right))
        spans = updated
    return spans


def _distributed(lo, hi, pitch):
    if hi < lo - EPS:
        return []
    if hi - lo <= EPS:
        return [(lo + hi) / 2]
    count = max(1, math.ceil((hi - lo) / pitch))
    return [lo + (hi - lo) * i / count for i in range(count + 1)]


def _columns(length, panels, openings, config, *, substrate=None):
    start, end = substrate if substrate is not None else (0., length)
    lo, hi = start + config.anchor_edge_mm, end - config.anchor_edge_mm
    if hi < lo:
        return []
    # Outer and jamb supports take priority over decorative joint centres.
    chosen = []
    preferred = [lo, hi]
    for opening in openings:
        preferred.extend((opening["x_mm"] - config.opening_edge_mm,
                          opening["x_mm"] + opening["width_mm"] + config.opening_edge_mm))
    secondary = []
    for panel in panels:
        secondary.extend((panel["x_mm"], panel["x_mm"] + panel["width_mm"]))
    def half_width(x):
        return config.anchor_edge_mm if abs(x-lo) < EPS or abs(x-hi) < EPS else BACKING_WIDTH/2
    for x in preferred + sorted(set(secondary)):
        if lo - EPS <= x <= hi + EPS and all(abs(x - old) >= half_width(x) + half_width(old) + EPS for old in chosen):
            chosen.append(x)
    chosen.sort()
    # Never silently exceed the declared maximum support pitch after merging
    # close joint/jamb axes (their 60-mm-wide backing strips must not overlap).
    result = []
    for a, b in zip(chosen, chosen[1:]):
        result.extend(_distributed(a, b, config.frame_pitch_mm)[:-1])
    if chosen:
        result.append(chosen[-1])
    return sorted({_rounded(x) for x in result})


def _point_opening_distance(x, z, opening):
    dx = max(opening["x_mm"] - x, 0, x - opening["x_mm"] - opening["width_mm"])
    dz = max(opening["y_mm"] - z, 0, z - opening["y_mm"] - opening["height_mm"])
    return math.hypot(dx, dz)


def _axis_points(member, config, anchors):
    horizontal = member.get("orientation") == "horizontal"
    lo, hi = (member["x_start_mm"], member["x_end_mm"]) if horizontal else (member["z_start_mm"], member["z_end_mm"])
    if anchors:
        lo, hi = lo + MEMBER_END_CLEARANCE, hi - MEMBER_END_CLEARANCE
        pitch = config.anchor_pitch_mm
    else:
        edge = min(config.screw_edge_mm, max(MEMBER_END_CLEARANCE, (hi - lo) / 2))
        lo, hi = lo + edge, hi - edge
        pitch = config.screw_pitch_mm
    return lo, hi, pitch


def _screw_positions(lo, hi, pitch, anchors):
    allowed = _subtract(lo, hi, [(x - ANCHOR_SCREW_CLEARANCE, x + ANCHOR_SCREW_CLEARANCE) for x in anchors])
    if not allowed:
        return []
    def nearest(target):
        return min((min(b, max(a, target)) for a, b in allowed), key=lambda p: (abs(p - target), p))
    values = sorted({nearest(t) for t in _distributed(lo, hi, pitch)})
    # A projection away from an access bore can enlarge the adjacent pitch.
    # Add a real additional screw rather than relaxing the spacing check.
    for _ in range(40):
        gaps = [(a, b) for a, b in zip(values, values[1:]) if b - a > pitch + EPS]
        if not gaps:
            break
        additions = {nearest((a + b) / 2) for a, b in gaps}
        if additions.issubset(values):
            return []
        values = sorted(set(values) | additions)
    return values


def _connected_panels(panels):
    """Rectangles sharing one panel_id may be pieces of the same notched panel."""
    pending = list(range(len(panels)))
    groups = []
    while pending:
        component = [pending.pop(0)]
        for index in component:
            a = panels[index]
            for other in pending[:]:
                b = panels[other]
                if a.get("panel_id", a["id"]) != b.get("panel_id", b["id"]):
                    continue
                overlap_x = min(a["x_mm"] + a["width_mm"], b["x_mm"] + b["width_mm"]) - max(a["x_mm"], b["x_mm"])
                overlap_z = min(a["y_mm"] + a["height_mm"], b["y_mm"] + b["height_mm"]) - max(a["y_mm"], b["y_mm"])
                # A common edge is connected; a point alone is not a panel.
                if (overlap_x >= -EPS and overlap_z > EPS) or (overlap_z >= -EPS and overlap_x > EPS):
                    pending.remove(other)
                    component.append(other)
        groups.append([panels[i] for i in component])
    return groups


def _support_area(panel, member):
    backing_width = member["backing_width_mm"]
    if member.get("orientation") == "horizontal":
        x, z, w, h = member["x_start_mm"], member["z_mm"] - backing_width / 2, member["x_end_mm"] - member["x_start_mm"], backing_width
    else:
        x, z, w, h = member["x_mm"] - backing_width / 2, member["z_start_mm"], backing_width, member["z_end_mm"] - member["z_start_mm"]
    return max(0, min(x + w, panel["x_mm"] + panel["width_mm"]) - max(x, panel["x_mm"])) * max(0, min(z + h, panel["y_mm"] + panel["height_mm"]) - max(z, panel["y_mm"]))


def _plan_legacy_v0(preview, configuration=None):
    """Return an immutable-input setting-out plan; invalid geometry stays visible.

    ``ready`` means the positions meet these explicit *geometric* constraints.
    The native builder must still check actual BRep parts, bores and contacts.
    No strength, pull-out capacity, fire resistance or anchor product approval
    is inferred from a successful geometric plan.
    """
    config = configuration if isinstance(configuration, WallFramingConfiguration) else WallFramingConfiguration.model_validate(configuration or {})
    wall = deepcopy(preview["wall"])
    length = _number(wall.get("length_mm"), "wall.length_mm", positive=True)
    height = _number(wall.get("height_mm"), "wall.height_mm", positive=True)
    substrate_start, substrate_end = substrate_interval(wall)
    if len(preview.get("panels", [])) > 2500 or len(preview.get("openings", [])) > 200:
        raise ValueError("Для полной стены допускается не более 2500 фрагментов панелей и 200 проёмов")
    panels = [_rect(p, length, height, "panel") for p in preview.get("panels", [])]
    openings = [_rect(o, length, height, "opening") for o in preview.get("openings", [])]
    mount = deepcopy(preview.get("effective_configuration", preview.get("configuration", {})))
    thickness = _number(mount.get("panel_thickness_mm", 12), "panel_thickness_mm", positive=True)
    issues = []
    unsupported = []
    if mount.get("mounting_system") != "open_mount" or mount.get("panel_edge") not in {None, "V-0"}:
        unsupported.append("Полная сборка сейчас рассчитана на открытый монтаж V-0; скрытый крепёж нельзя подменять этой конструкцией.")
    if mount.get("joint_mm") != 2:
        unsupported.append("Полная сборка с профилем V-T1 поддерживает шов 2 мм. Другой шов требует другого профиля и не будет заменён автоматически.")
    if not 8 <= thickness <= 13:
        unsupported.append("Толщина панели полного каркаса должна быть в диапазоне 8–13 мм V-0.")
    if config.substrate_type not in {"concrete", "solid_masonry"}:
        unsupported.append("Геометрическая анкеровка предусмотрена в сплошное основание; для пустотелого основания или гипсокартона нужна другая конструкция.")
    if not config.allow_project_assumptions:
        unsupported.append("Для полной стены нужно разрешить проектную адаптацию каркаса и параметрического анкера.")
    assumptions = [
        {"id": "wall-frame-adaptation", "kind": "project_assumption", "message": "Непрерывные V-WT, полосы подосновы толщиной 12 мм (обычно шириной 60 мм, крайние расширены до края стены) и клеевой слой 3 мм образуют проектную стеновую адаптацию. Это не заводской каркас V-WS/V-SH из листа 0.1."},
        {"id": "wall-frame-spacing", "kind": "project_assumption", "message": f"Максимальный шаг опор {config.frame_pitch_mm:g} мм, саморезов {config.screw_pitch_mm:g} мм, анкеров {config.anchor_pitch_mm:g} мм задан параметрами проекта. Число 400 мм в публичной инструкции относится к другим показанным креплениям, не является подтверждением этой адаптации."},
        {"id": "wall-frame-anchor", "kind": "project_assumption", "message": f"Анкер — геометрическая модель болта Ø6 и втулки Ø8 с заделкой {config.anchor_embedment_mm:g} мм; конкретный артикул, несущая способность и пригодность к основанию не назначены."},
        {"id": "wall-frame-substrate", "kind": "project_assumption", "message": f"Основание принято сплошным ({config.substrate_type}), толщиной {config.substrate_thickness_mm:g} мм; отступ анкера от края {config.anchor_edge_mm:g} мм и от проёма {config.opening_edge_mm:g} мм — геометрические параметры проекта, а не расчётные минимумы изготовителя."},
        {"id": "wall-frame-boundary", "kind": "project_assumption", "message": "Каркас крепится к стеновому основанию через дистанционные опоры. Крепление к полу/потолку, передача усилий между соседними стенами и расчёт нагрузок не входят в эту стеновую сборку."},
    ]
    result = {"schema_version": "wall-framing-plan/1.0", "status": "unsupported" if unsupported else "ready", "wall": wall,
              "configuration": config.model_dump(mode="json"), "mount_configuration": mount,
              "geometry_signature": preview.get("geometry_signature"), "panels": panels, "openings": openings,
              "joint_lines": deepcopy(preview.get("joint_lines", [])),
              "layers": {"panel_thickness_mm": thickness, "adhesive_thickness_mm": 3, "backing_thickness_mm": 12,
                         "frame_depth_mm": FRAME_DEPTH, "substrate_front_mm": thickness + config.cavity_mm},
              "coordinates": {"x": "along_wall_face", "y": "depth_into_substrate", "z": "up_from_wall_bottom", "panel_rect_y": "assembly_z"},
              "posts": [], "rails": [], "screws": [], "anchors": [], "checks": [], "issues": issues,
              "assumptions": assumptions,
              "evidence": [{"path": "for_interior-rd-generator/manuals/opened.pdf", "sha256": "84e27ca0cf2b61b0fc7bf70e50cb46e5094d7e8950c5a31a3279c09c75ca32d0", "page": 8, "scope": "Состав заводского каркаса 0.1; шаги не заданы"},
                           {"path": "for_interior-rd-generator/manuals/public-violet/violet-open-installation-public.pdf", "sha256": "856a0e8d78534e1d3ee04ca86dec78ea399d0b1c7517291b0bc1f7602630445e", "page": 5, "scope": "Другой показанный каркас: направляющие 400 мм, опоры 610–615 мм, шов омега 8 мм; не переносить без адаптации"},
                           {"path": "for_interior-rd-generator/manuals/public-violet/violet-open-installation-public.pdf", "sha256": "856a0e8d78534e1d3ee04ca86dec78ea399d0b1c7517291b0bc1f7602630445e", "page": 8, "scope": "Шаг 400 мм относится к JP81 и омега-профилю"}]}
    if unsupported:
        issues.extend({"code": "WALL_FRAMING_UNSUPPORTED", "severity": "error", "message": message} for message in unsupported)
        return _finish(result)
    if min(substrate_end - substrate_start, height) <= 2 * config.anchor_edge_mm:
        issues.append({"code": "WALL_FRAMING_TOO_SMALL", "severity": "error", "message": "Размер стены не оставляет места для анкеров с заданными отступами."})
        return _finish(result)
    return _populate_frame(result, config)


def _populate_frame(result, config, profile_runs=None):
    """Shared member/anchor setting-out; None preserves the original V0 path."""
    length, height = result["wall"]["length_mm"], result["wall"]["height_mm"]
    panels, openings = result["panels"], result["openings"]
    substrate = substrate_interval(result["wall"])
    anchor_left, anchor_right = substrate[0] + config.anchor_edge_mm, substrate[1] - config.anchor_edge_mm
    # Place posts only where their entire backing stays out of a real opening.
    # The wider anchor exclusion gives a conservative rectangular clearance at
    # corners; final anchors are additionally checked by Euclidean distance.
    columns = (_columns(length, panels, openings, config, substrate=substrate) if profile_runs is None else
               _hidden_columns(length, config, profile_runs, openings, substrate=substrate))
    maximum_column_pitch = max((b-a for a,b in zip(columns, columns[1:])), default=0)
    result["checks"].append({"code": "frame_support_pitch", "status": "valid" if maximum_column_pitch <= config.frame_pitch_mm + EPS else "invalid", "maximum_mm": _rounded(maximum_column_pitch), "limit_mm": config.frame_pitch_mm})
    posts = result["posts"]
    for x in columns:
        backing_width = (2 * config.anchor_edge_mm if abs(x-anchor_left) < EPS or abs(x-anchor_right) < EPS else BACKING_WIDTH) if profile_runs is None else FRAME_WIDTH
        blocked = []
        for opening in openings:
            clearance = max(config.opening_edge_mm, backing_width/2)
            if opening["x_mm"] - clearance + EPS < x < opening["x_mm"] + opening["width_mm"] + clearance - EPS:
                blocked.append((opening["y_mm"] - config.opening_edge_mm, opening["y_mm"] + opening["height_mm"] + config.opening_edge_mm))
        for low, high in _subtract(0, height, blocked):
            if high - low < 2 * MEMBER_END_CLEARANCE + EPS:
                continue
            posts.append({"id": f"post-{len(posts)+1}", "orientation": "vertical", "x_mm": x,
                          "z_start_mm": _rounded(low), "z_end_mm": _rounded(high),
                          "width_mm": FRAME_WIDTH, "backing_width_mm": backing_width, "depth_mm": FRAME_DEPTH})
    # Independent anchored lintels/sills: cut them between backing strips so
    # horizontal and vertical members have no volumetric overlap.
    levels = {}
    for opening in openings:
        for level in (opening["y_mm"] - config.opening_edge_mm,
                      opening["y_mm"] + opening["height_mm"] + config.opening_edge_mm):
            levels.setdefault(level, []).append((max(anchor_left, opening["x_mm"] - config.opening_edge_mm),
                                                min(anchor_right, opening["x_mm"] + opening["width_mm"] + config.opening_edge_mm)))
    rails = result["rails"]
    for z in sorted(levels):
        if z < config.anchor_edge_mm or z > height - config.anchor_edge_mm:
            continue
        blocks = [(p["x_mm"] - p["backing_width_mm"] / 2, p["x_mm"] + p["backing_width_mm"] / 2) for p in posts
                  if p["z_start_mm"] < z + BACKING_WIDTH / 2 - EPS and p["z_end_mm"] > z - BACKING_WIDTH / 2 + EPS]
        blocks.extend((o["x_mm"] - config.opening_edge_mm, o["x_mm"] + o["width_mm"] + config.opening_edge_mm)
                      for o in openings if o["y_mm"] - config.opening_edge_mm + EPS < z < o["y_mm"] + o["height_mm"] + config.opening_edge_mm - EPS)
        requested = sorted(levels[z])
        merged = []
        for left, right in requested:
            if merged and left <= merged[-1][1] + EPS:
                merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
            else:
                merged.append((left, right))
        spans = [span for left, right in merged for span in _subtract(left, right, blocks)]
        for left, right in spans:
            if right - left < 2 * MEMBER_END_CLEARANCE + 2 * ANCHOR_SCREW_CLEARANCE + EPS:
                continue
            rails.append({"id": f"rail-{len(rails)+1}", "orientation": "horizontal", "z_mm": _rounded(z),
                          "x_start_mm": _rounded(left), "x_end_mm": _rounded(right),
                          "width_mm": FRAME_WIDTH, "backing_width_mm": BACKING_WIDTH, "depth_mm": FRAME_DEPTH})
    members = posts + rails
    retained = set()
    for member in members:
        horizontal = member["orientation"] == "horizontal"
        lo, hi, pitch = _axis_points(member, config, True)
        lo = max(lo, anchor_left if horizontal else config.anchor_edge_mm)
        hi = min(hi, anchor_right if horizontal else height - config.anchor_edge_mm)
        profile_blocks = [] if profile_runs is None else _member_profile_blocks(member, profile_runs, 10.)
        anchor_positions = _distributed(lo, hi, pitch) if profile_runs is None else _positions_avoiding(lo, hi, pitch, profile_blocks, HIDDEN_HEAD_CLEARANCE)
        # A rail/batten without a real anchor is never exported as supported.
        if not anchor_positions:
            continue
        slo, shi, spitch = _axis_points(member, config, False)
        screw_positions = _screw_positions(slo, shi, spitch, anchor_positions) if profile_runs is None else _positions_avoiding(slo, shi, spitch, [*profile_blocks, *((a-ANCHOR_SCREW_CLEARANCE,a+ANCHOR_SCREW_CLEARANCE) for a in anchor_positions)], HIDDEN_HEAD_CLEARANCE)
        if not screw_positions:
            continue
        retained.add(member["id"])
        for values, positions, prefix in ((result["anchors"], anchor_positions, "anchor"), (result["screws"], screw_positions, "screw")):
            for position in positions:
                x, z = (position, member["z_mm"]) if horizontal else (member["x_mm"], position)
                values.append({"id": f"{prefix}-{len(values)+1}", "post_id": member["id"], "x_mm": _rounded(x), "z_mm": _rounded(z)})
    result["posts"] = [p for p in posts if p["id"] in retained]
    result["rails"] = [p for p in rails if p["id"] in retained]
    if profile_runs is None:
        _validate_plan(result)
        return _finish(result)
    return result


def _validate_plan(result):
    config = WallFramingConfiguration.model_validate(result["configuration"])
    length, height = result["wall"]["length_mm"], result["wall"]["height_mm"]
    substrate_start, substrate_end = substrate_interval(result["wall"])
    members = result["posts"] + result["rails"]
    checks = result["checks"]
    def check(name, valid, **details):
        checks.append({"code": name, "status": "valid" if valid else "invalid", **details})
    for anchor in result["anchors"]:
        x, z = anchor["x_mm"], anchor["z_mm"]
        edge = min(x - substrate_start, substrate_end - x, z, height - z)
        opening_distance = min((_point_opening_distance(x, z, o) for o in result["openings"]), default=None)
        check("anchor_edge_clearance", edge + EPS >= config.anchor_edge_mm and (opening_distance is None or opening_distance + EPS >= config.opening_edge_mm), entity_id=anchor["id"], edge_distance_mm=_rounded(edge), opening_distance_mm=_rounded(opening_distance) if opening_distance is not None else None)
    for member in members:
        horizontal = member["orientation"] == "horizontal"
        field = "x_mm" if horizontal else "z_mm"
        for key, pitch in (("screws", config.screw_pitch_mm), ("anchors", config.anchor_pitch_mm)):
            values = sorted(p[field] for p in result[key] if p["post_id"] == member["id"])
            maximum = max((b - a for a, b in zip(values, values[1:])), default=0)
            check(f"{key}_pitch", bool(values) and maximum <= pitch + EPS, entity_id=member["id"], maximum_mm=_rounded(maximum), limit_mm=pitch, count=len(values))
        screws = [s for s in result["screws"] if s["post_id"] == member["id"]]
        anchors = [a for a in result["anchors"] if a["post_id"] == member["id"]]
        clearance = min(abs(s[field] - a[field]) for s in screws for a in anchors)
        check("screw_anchor_access_clearance", clearance + EPS >= ANCHOR_SCREW_CLEARANCE, entity_id=member["id"], minimum_mm=_rounded(clearance), required_mm=ANCHOR_SCREW_CLEARANCE)
        # Full backing footprint, not only the centreline, must avoid voids.
        intersections = [o["id"] for o in result["openings"] if _support_area(o, member) > EPS]
        check("member_avoids_openings", not intersections, entity_id=member["id"], opening_ids=intersections)
    for index, group in enumerate(_connected_panels(result["panels"]), 1):
        supported = [m for m in members if sum(_support_area(p, m) for p in group) > 100]
        area = sum(_support_area(p, m) for p in group for m in supported)
        check("panel_connected_support", bool(supported), entity_id=f"panel-group-{index}", panel_ids=[p["id"] for p in group], support_ids=[m["id"] for m in supported], nominal_contact_area_mm2=_rounded(area))
    # Backing rectangles may touch but may never occupy the same volume.
    for i, first in enumerate(members):
        if first["orientation"] == "horizontal":
            rect = {"x_mm": first["x_start_mm"], "y_mm": first["z_mm"] - first["backing_width_mm"] / 2, "width_mm": first["x_end_mm"] - first["x_start_mm"], "height_mm": first["backing_width_mm"]}
        else:
            rect = {"x_mm": first["x_mm"] - first["backing_width_mm"] / 2, "y_mm": first["z_start_mm"], "width_mm": first["backing_width_mm"], "height_mm": first["z_end_mm"] - first["z_start_mm"]}
        overlaps = [m["id"] for m in members[i+1:] if _support_area(rect, m) > EPS]
        check("backing_nonoverlap", not overlaps, entity_id=first["id"], other_ids=overlaps)
    if not members:
        check("frame_not_empty", False)
    if not result["panels"]:
        check("panels_not_empty", False)


def _positions_avoiding(lo, hi, pitch, blocked, minimum=0.):
    allowed = _subtract(lo, hi, blocked)
    if hi < lo-EPS or not allowed:
        return []
    def closest(value):
        return min((min(b,max(a,value)) for a,b in allowed), key=lambda p:(abs(p-value),p))
    positions = []
    for value in sorted({closest(value) for value in _distributed(lo,hi,pitch)}):
        if not positions or value-positions[-1]>=minimum-EPS:
            positions.append(value)
    for _ in range(40):
        additions = []
        for a,b in zip(positions,positions[1:]):
            if b-a<=pitch+EPS:
                continue
            free=_subtract(a+minimum,b-minimum,[*blocked,*((p-minimum,p+minimum) for p in positions+additions)])
            if free:
                target=(a+b)/2
                additions.append(min((min(d,max(c,target)) for c,d in free),key=lambda p:(abs(p-target),p)))
        if not additions or set(additions).issubset(positions):
            break
        positions = sorted(set(positions)|set(additions))
    return positions


def _run_box(run, clearance=0.):
    from .wall_systems import profile_footprint
    low, high = profile_footprint(run["family"],run.get("inward_sign",1))
    if run["axis"] == "vertical":
        return (run["edge_mm"]+low-clearance,run["start_mm"]-clearance,
                run["edge_mm"]+high+clearance,run["end_mm"]+clearance)
    return (run["start_mm"]-clearance,run["edge_mm"]+low-clearance,
            run["end_mm"]+clearance,run["edge_mm"]+high+clearance)


def _hidden_columns(length, config, runs, openings, *, substrate=None):
    # Positions projected to an allowed interval endpoint must lie strictly
    # outside the later 10 mm clearance band, not on its inclusive boundary.
    # Otherwise all anchors are removed and the nominal post pitch is false.
    blocked = [(box[0],box[2]) for run in runs if run["axis"] == "vertical" for box in [_run_box(run,10.1)]]
    start,end = substrate if substrate is not None else (0.,length)
    lo,hi=start+config.anchor_edge_mm,end-config.anchor_edge_mm
    if hi < lo:
        return []
    allowed=_subtract(lo,hi,blocked)
    preferred=[]
    for opening in openings:
        for target,direction in ((opening["x_mm"]-config.opening_edge_mm,-1),
                                 (opening["x_mm"]+opening["width_mm"]+config.opening_edge_mm,1)):
            if not lo-EPS<=target<=hi+EPS:
                continue
            candidates=[min(b,target) for a,b in allowed if a<=target] if direction==-1 else [max(a,target) for a,b in allowed if b>=target]
            if candidates:
                preferred.append(min(candidates,key=lambda x:(abs(x-target),x)))
    chosen=[]
    for value in [*preferred,*_positions_avoiding(lo,hi,config.frame_pitch_mm,blocked)]:
        if all(abs(value-old)>=FRAME_WIDTH+.1 for old in chosen):
            chosen.append(value)
    for _ in range(40):
        chosen.sort()
        gaps=[(a,b) for a,b in zip(chosen,chosen[1:]) if b-a>config.frame_pitch_mm+EPS]
        if not gaps:
            break
        new=[]
        for a,b in gaps:
            free=_subtract(a,b,[*blocked,*((x-FRAME_WIDTH-.1,x+FRAME_WIDTH+.1) for x in chosen+new)])
            if free:
                target=(a+b)/2
                new.append(min((min(d,max(c,target)) for c,d in free),key=lambda x:(abs(x-target),x)))
        if not new:
            break
        chosen.extend(new)
    return sorted({_rounded(value) for value in chosen})


def _member_profile_blocks(member, runs, clearance):
    horizontal = member["orientation"] == "horizontal"
    transverse = member["z_mm"] if horizontal else member["x_mm"]
    blocked = []
    for run in runs:
        left,bottom,right,top = _run_box(run,clearance)
        if (bottom-EPS < transverse < top+EPS) if horizontal else (left-EPS < transverse < right+EPS):
            blocked.append((left,right) if horizontal else (bottom,top))
    return blocked


def _hidden_profile_runs(preview, family, perimeter_families=()):
    """Deterministic measured profile bands; vertical seams take precedence."""
    cfg = preview.get("effective_configuration",preview.get("configuration",{}))
    raw = []
    for index, line in enumerate(preview.get("joint_lines", []),1):
        vertical = line["kind"] == "vertical_joint"
        a,b = line["from"],line["to"]
        along,across = ("y_mm","x_mm") if vertical else ("x_mm","y_mm")
        raw.append({"id":f"joint-{index}", "family":family,"axis":"vertical" if vertical else "horizontal",
                    "start_mm":min(a[along],b[along]),"end_mm":max(a[along],b[along]),"edge_mm":a[across],
                    "context":line["kind"]})
    directions = {"left_edge":("vertical",1),"right_edge":("vertical",-1),
                  "floor":("horizontal",1),"ceiling":("horizontal",-1),
                  "opening_jamb_left":("vertical",-1),"opening_jamb_right":("vertical",1),
                  "opening_head":("horizontal",1),"opening_sill":("horizontal",-1)}
    for index, region in enumerate(preview.get("mounting_regions",[]),1):
        kind = region.get("kind")
        if kind not in directions or (kind == "floor" and cfg.get("floor_gap_mm",0) < 2) or (kind == "ceiling" and cfg.get("ceiling_gap_mm",0) < 2):
            continue
        axis, sign = directions[kind]
        along,across = ("y_mm","x_mm") if axis == "vertical" else ("x_mm","y_mm")
        a,b = region["from"],region["to"]
        perimeter = ("sky" if kind == "ceiling" and "sky" in perimeter_families else
                     "start" if kind == "floor" and "start" in perimeter_families else "lstart")
        raw.append({"id":f"edge-{index}","family":perimeter,"axis":axis,"inward_sign":sign,
                    "start_mm":min(a[along],b[along]),"end_mm":max(a[along],b[along]),"edge_mm":a[across],
                    "context":kind})
    # A finish-only gap at a stepped corner must not create a separate 2-mm
    # vertical fastening cell. Its new corner stock starts at the physical
    # transition; the horizontal L-start spans the small adjacent tip gap.
    for margin in preview['wall'].get('finish_transition_clearances',[]):
        from .wall_systems import profile_footprint
        edge=_number(margin.get('edge_mm'),'finish_transition.edge_mm')
        structural=_number(margin.get('structural_z_mm'),'finish_transition.structural_z_mm')
        finish=_number(margin.get('finish_z_mm'),'finish_transition.finish_z_mm')
        kind=margin.get('kind')
        footprint=profile_footprint('lstart',-1 if kind=='opening_sill' else 1)
        projection=footprint[1] if kind=='opening_sill' else -footprint[0]
        opposing=_number(margin.get('opposing_projection_mm',0.),'finish_transition.opposing_projection_mm')
        expected=structural+(-1 if kind=='opening_sill' else 1)*(projection+opposing+.05)
        if (margin.get('profile_family')!='lstart' or margin.get('scope')!='finish_only'
                or kind not in {'opening_sill','opening_head'} or abs(finish-expected)>EPS or opposing>2.+EPS
                or abs(_number(margin.get('projection_mm'),'finish_transition.projection_mm')-projection)>EPS
                or abs(_number(margin.get('clearance_mm'),'finish_transition.clearance_mm')-.05)>EPS):
            raise ValueError('Некорректный калиброванный зазор ступени отделки')
        for run in raw:
            if run['family']!='lstart' or run['axis']!='vertical' or abs(run['edge_mm']-edge)>EPS:continue
            if margin['kind']=='opening_sill' and abs(run['start_mm']-finish)<=EPS:run['start_mm']=structural
            elif margin['kind']=='opening_head' and abs(run['end_mm']-finish)<=EPS:run['end_mm']=structural
    output = []
    for category in ("perimeter", "vertical", "horizontal"):
        selected_runs = ([r for r in raw if r["family"] in {"sky", "start"}] if category == "perimeter" else
                         [r for r in raw if r["axis"] == category and r["family"] not in {"sky", "start"}])
        for run in selected_runs:
            axis = run["axis"]
            left,bottom,right,top = _run_box(run)
            blocked = []
            for other in output:
                a,b,c,d = _run_box(other,.05)
                if min(right,c)-max(left,a) <= EPS or min(top,d)-max(bottom,b) <= EPS:
                    continue
                # Preserve an explicitly selected VH seam; never silently
                # replace it with a terminal L-start. Native preflight rejects
                # overlapping parallel VH seams on impossibly narrow panels.
                if axis == other["axis"] and run["family"] in {"vh2", "vh3"}:
                    continue
                blocked.append((b,d) if axis=="vertical" else (a,c))
            for part,(lo,hi) in enumerate(_subtract(run["start_mm"],run["end_mm"],blocked),1):
                if hi-lo >= 20-EPS:
                    output.append({**run,"id":run["id"]+f"-{part}","start_mm":_rounded(lo),"end_mm":_rounded(hi),"edge_mm":_rounded(run["edge_mm"])})
    return _split_boundary_runs(_setback_transition_profiles(output,preview), preview)


def _setback_transition_profiles(runs,preview):
    """Saw a horizontal stock end before placing its complete screw cell.

    Cope-cutting the thin section around an upper corner may detach a hook.
    An axial stock setback leaves its full cross-section, and the normal
    frame/screw planner subsequently verifies the new supported length.
    """
    reserved=preview.get('corner_transition_clearances',[])
    if not isinstance(reserved,list):raise ValueError('Некорректные габариты высотного сопряжения')
    checked=[]
    for item in reserved:
        if not isinstance(item,dict) or not isinstance(item.get('bounds_mm'),dict):
            raise ValueError('Некорректные габариты углового комплекта')
        box=item['bounds_mm'];transitions=item.get('transitions_z_mm')
        if (set(box)!={'min','max'} or any(not isinstance(box[key],list) or len(box[key])!=3 for key in box)
                or not isinstance(transitions,list) or not transitions):
            raise ValueError('Некорректные границы углового комплекта')
        if any(type(v) not in (float,int) or not math.isfinite(v) or abs(v)>1e6
               for v in [*box['min'],*box['max'],*transitions]) or any(a>=b for a,b in zip(box['min'],box['max'])):
            raise ValueError('Некорректные конечные размеры углового комплекта')
        checked.append((item,box,transitions))
    output=[]
    cfg=preview.get('effective_configuration',preview.get('configuration',{}));thickness=cfg.get('panel_thickness_mm',12.)
    for run in runs:
        if run['axis']!='horizontal' or not checked:
            output.append(run);continue
        left,bottom,right,top=_run_box(run)
        if run['family']=='sky':front,back=thickness-19.,thickness+1.2
        elif run['family']=='start':front,back=thickness-14.,thickness+1.
        elif run['family'] in {'lstart','vh2','vh3'}:front,back=5.5,15.9
        else:raise ValueError('Нет калиброванной глубины горизонтального профиля')
        blocked=[];causes=[]
        for item,box,transitions in checked:
            if (not any(bottom-EPS<=z<=top+EPS for z in transitions)
                    or min(back,box['max'][1])-max(front,box['min'][1])<=EPS
                    or min(top,box['max'][2])-max(bottom,box['min'][2])<=EPS
                    or min(right,box['max'][0])-max(left,box['min'][0])<=EPS):continue
            blocked.append((box['min'][0]-.05,box['max'][0]+.05))
            causes.append({'joint_id':item['joint_id'],'part_id':item['part_id']})
        if not blocked:
            output.append(run);continue
        pieces=_subtract(run['start_mm'],run['end_mm'],blocked)
        if not pieces or any(hi-lo<20-EPS for lo,hi in pieces):
            raise ValueError('Угловой комплект не оставляет целую горизонтальную крепёжную ячейку длиной 20 мм')
        for number,(lo,hi) in enumerate(pieces,1):
            output.append({**run,'id':run['id']+f'-corner{number}',
                'start_mm':_rounded(lo),'end_mm':_rounded(hi),'corner_end_setbacks':causes})
    return output


def _split_boundary_runs(runs, preview):
    """Cut terminal profiles before placing their complete fastening cells.

    At a door-ended T junction the corner can exist only above the opening.
    Its upper profile and fasteners must be independently replaceable; never
    trim a finished native screw or discard the still-needed lower profile.
    Unchanged runs retain their IDs so unrelated walls keep their CAD cache.
    """
    boundaries = preview.get("boundary_breaks_z_mm", {})
    if not isinstance(boundaries, dict) or set(boundaries)-{"start", "end", "edges"}:
        raise ValueError("boundary_breaks_z_mm: требуются границы start/end")
    length = preview["wall"]["length_mm"]
    height = preview["wall"]["height_mm"]
    cuts = {}
    for side, values in boundaries.items():
        if side == "edges":
            continue
        if not isinstance(values, (list, tuple)):
            raise ValueError("boundary_breaks_z_mm: требуются списки высот")
        cuts[side] = sorted({_number(value, "boundary_breaks_z_mm") for value in values})
        if any(value > height+EPS for value in cuts[side]):
            raise ValueError("Граница углового профиля выходит за высоту стены")
    edges = boundaries.get("edges", [])
    if not isinstance(edges, list):
        raise ValueError("boundary_breaks_z_mm.edges: требуется список кромок")
    checked_edges = []
    for item in edges:
        if not isinstance(item, dict) or set(item) != {"edge_mm", "z_mm"} or not isinstance(item["z_mm"], list):
            raise ValueError("Некорректная ступенчатая кромка профиля")
        edge = _number(item["edge_mm"], "boundary.edge_mm")
        values = [_number(value, "boundary.z_mm") for value in item["z_mm"]]
        if edge > length+EPS or any(value > height+EPS for value in values):
            raise ValueError("Ступенчатая кромка выходит за размеры стены")
        checked_edges.append((edge, values))
    output = []
    for run in runs:
        side = ("start" if abs(run["edge_mm"]) <= EPS else
                "end" if abs(run["edge_mm"]-length) <= EPS else None)
        values = {*cuts.get(side, []), *(value for edge, values in checked_edges
                                        if abs(run["edge_mm"]-edge) <= EPS for value in values)}
        breaks = ([value for value in sorted(values)
                   if run["start_mm"]+EPS < value < run["end_mm"]-EPS]
                  if run["family"] == "lstart" and run["axis"] == "vertical" else [])
        if not breaks:
            output.append(run)
            continue
        points = [run["start_mm"], *breaks, run["end_mm"]]
        for index, (low, high) in enumerate(zip(points, points[1:]), 1):
            if high-low < 20-EPS:
                raise ValueError("Сопряжение оставляет торцевой профиль короче 20 мм; нужна другая раскладка")
            output.append({**run, "id": f"{run['id']}-z{index}",
                           "start_mm": _rounded(low), "end_mm": _rounded(high)})
    return output


def _profile_screws(result):
    cfg = WallFramingConfiguration.model_validate(result["configuration"])
    members = result["posts"]+result["rails"]
    screws = []
    checks = result["checks"]
    for run in result["profile_runs"]:
        if run["family"] == "sky":
            # The rear V-Sky flange is bonded to the common backing. A screw
            # through it would put its head through the visible panel.
            continue
        offsets = [-19.,20.] if run["family"]=="vh2" else [0.] if run["family"]=="vh3" else [-29.] if run["family"]=="start" else [18.6*run["inward_sign"]]
        for row, offset in enumerate(offsets,1):
            transverse = run["edge_mm"]+offset
            along_x = run["axis"]=="horizontal"
            edge = min(cfg.screw_edge_mm,(run["end_mm"]-run["start_mm"])/2)
            lo,hi = run["start_mm"]+edge,run["end_mm"]-edge
            blocked = []
            for member in members:
                if member["orientation"]=="vertical":
                    a,b,c,d = member["x_mm"]-16.25,member["z_start_mm"]-2.25,member["x_mm"]+16.25,member["z_end_mm"]+2.25
                else:
                    a,b,c,d = member["x_start_mm"]-2.25,member["z_mm"]-16.25,member["x_end_mm"]+2.25,member["z_mm"]+16.25
                if (b-EPS < transverse < d+EPS) if along_x else (a-EPS < transverse < c+EPS):
                    blocked.append((a,c) if along_x else (b,d))
            for anchor in result["anchors"]:
                other = anchor["z_mm"] if along_x else anchor["x_mm"]
                if abs(other-transverse)<12:
                    axis = anchor["x_mm"] if along_x else anchor["z_mm"]
                    blocked.append((axis-12,axis+12))
            # Project a real circular head clearance onto this screw row.
            # Previously two targets projected to opposite ends of a tiny
            # allowed interval and produced overlapping 10.8 mm STEP heads.
            for screw in [*result["screws"],*screws]:
                across=screw["z_mm"] if along_x else screw["x_mm"]
                distance=abs(across-transverse)
                if distance<HIDDEN_HEAD_CLEARANCE:
                    radius=math.sqrt(HIDDEN_HEAD_CLEARANCE**2-distance**2)+.00001
                    along=screw["x_mm"] if along_x else screw["z_mm"]
                    blocked.append((along-radius,along+radius))
            positions = _positions_avoiding(lo,hi,cfg.screw_pitch_mm,blocked,HIDDEN_HEAD_CLEARANCE)
            checks.append({"code":"profile_fasteners_not_empty","status":"valid" if positions else "invalid","entity_id":run["id"],"row":row})
            maximum = max((b-a for a,b in zip(positions,positions[1:])),default=0)
            checks.append({"code":"profile_fasteners_pitch","status":"valid" if maximum <= cfg.screw_pitch_mm+EPS else "invalid",
                           "entity_id":run["id"],"row":row,"maximum_mm":_rounded(maximum),"limit_mm":cfg.screw_pitch_mm})
            for position in positions:
                x,z = (position,transverse) if along_x else (transverse,position)
                screws.append({"id":f"profile-screw-{len(screws)+1}","profile_id":run["id"],"x_mm":_rounded(x),"z_mm":_rounded(z),
                               "seat_y_mm":5.5 if run["family"]=="vh3" else result["layers"]["panel_thickness_mm"] if run["family"]=="start" else 14.7})
    result["profile_screws"] = screws


def _panel_profile_contact(panel, run, gap):
    vertical = run["axis"]=="vertical"
    across, size, along, span = ("x_mm","width_mm","y_mm","height_mm") if vertical else ("y_mm","height_mm","x_mm","width_mm")
    overlap = min(panel[along]+panel[span],run["end_mm"])-max(panel[along],run["start_mm"])
    if overlap <= EPS:
        return False
    if run["family"] in {"lstart", "sky", "start"}:
        boundary = panel[across] if run["inward_sign"]==1 else panel[across]+panel[size]
        return abs(boundary-run["edge_mm"])<=EPS
    return abs(panel[across]+panel[size]-(run["edge_mm"]-gap/2))<=EPS or abs(panel[across]-(run["edge_mm"]+gap/2))<=EPS


def _groove_remaining_web(group, runs, gap, depth, *, evidence=None):
    """Conservative remaining material between local opposed edge grooves.

    Work on the manufactured panel union, not on artificial rectangles used
    to subtract a window. Section slabs change only at rectangle/run ends.
    A one-millimetre web is a geometric guard, not a strength specification.
    """
    minimum = None
    for axis in ("vertical","horizontal"):
        across,size,along,span = ("x_mm","width_mm","y_mm","height_mm") if axis=="vertical" else ("y_mm","height_mm","x_mm","width_mm")
        active_runs = [run for run in runs if run["axis"]==axis and run["family"] not in {"sky", "start"}]
        breaks = sorted({point for panel in group for point in (panel[along],panel[along]+panel[span])} |
                        {point for run in active_runs for point in (run["start_mm"],run["end_mm"])})
        for low,high in zip(breaks,breaks[1:]):
            mid = (low+high)/2
            intervals = sorted((panel[across],panel[across]+panel[size]) for panel in group if panel[along]-EPS<mid<panel[along]+panel[span]+EPS)
            merged = []
            for a,b in intervals:
                if merged and a<=merged[-1][1]+EPS:
                    merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
                else:
                    merged.append((a,b))
            for a,b in merged:
                cuts = set()
                for run in active_runs:
                    if not run["start_mm"]-EPS<mid<run["end_mm"]+EPS:
                        continue
                    boundaries = [(run["edge_mm"],run["inward_sign"])] if run["family"]=="lstart" else [(run["edge_mm"]-gap/2,-1),(run["edge_mm"]+gap/2,1)]
                    for edge,inward in boundaries:
                        if abs(edge-a)<=EPS and inward==1:
                            cuts.add("left")
                        if abs(edge-b)<=EPS and inward==-1:
                            cuts.add("right")
                if cuts:
                    web=b-a-len(cuts)*depth
                    if minimum is None or web < minimum:
                        minimum = web
                        if evidence is not None:
                            evidence.update(axis=axis, strip_start_mm=_rounded(a), strip_end_mm=_rounded(b),
                                span_start_mm=_rounded(low), span_end_mm=_rounded(high),
                                strip_width_mm=_rounded(b-a), groove_depth_mm=_rounded(depth),
                                groove_count=len(cuts), required_strip_mm=_rounded(len(cuts)*depth+1))
    return minimum


def _validate_hidden_plan(result):
    # Reuse all strict frame/anchor checks, but a panel attaches to a profile
    # and a continuous backing, not necessarily directly above a frame strip.
    _validate_plan(result)
    result["checks"] = [c for c in result["checks"] if c["code"] not in {"panel_connected_support","backing_nonoverlap"}]
    checks = result["checks"]
    members = result["posts"]+result["rails"]
    for index,member in enumerate(members):
        small = {**member,"backing_width_mm":member["width_mm"]}
        rect = ({"x_mm":small["x_start_mm"],"y_mm":small["z_mm"]-small["width_mm"]/2,"width_mm":small["x_end_mm"]-small["x_start_mm"],"height_mm":small["width_mm"]}
                if small["orientation"]=="horizontal" else {"x_mm":small["x_mm"]-small["width_mm"]/2,"y_mm":small["z_start_mm"],"width_mm":small["width_mm"],"height_mm":small["z_end_mm"]-small["z_start_mm"]})
        overlaps = [other["id"] for other in members[index+1:] if _support_area(rect,{**other,"backing_width_mm":other["width_mm"]})>EPS]
        checks.append({"code":"frame_members_nonoverlap","status":"valid" if not overlaps else "invalid","entity_id":member["id"],"other_ids":overlaps})
    gap = result["mount_configuration"]["joint_mm"]
    for index,group in enumerate(_connected_panels(result["panels"]),1):
        profiles = [run["id"] for run in result["profile_runs"] if any(_panel_profile_contact(panel,run,gap) for panel in group)]
        checks.append({"code":"panel_profile_engagement","status":"valid" if profiles else "invalid","entity_id":f"panel-group-{index}",
                       "panel_ids":[panel["id"] for panel in group],"profile_ids":profiles})
        evidence = {}
        remaining = _groove_remaining_web(group,result["profile_runs"],gap,result["system"]["groove"]["depth_mm"],evidence=evidence)
        check = {"code":"groove_remaining_web","status":"valid" if remaining is None or remaining>=1.-EPS else "invalid",
                 "entity_id":f"panel-group-{index}","minimum_mm":_rounded(remaining) if remaining is not None else None,"required_mm":1.}
        if check["status"] == "invalid":
            # Diagnostic-only additions: valid plans retain their exact digest
            # and reusable CAD solids. Never delete a narrow leg, move a door,
            # or stop the selected groove merely to turn this guard green.
            across,along = ("X","высота") if evidence["axis"] == "vertical" else ("высота","X")
            check.update(evidence, panel_ids=sorted({p.get("panel_id",p["id"]) for p in group}))
            check["message"] = (
                f"Панель {index}: узкий участок {evidence['strip_width_mm']:g} мм "
                f"({across} {evidence['strip_start_mm']:g}–{evidence['strip_end_mm']:g} мм; "
                f"{along} {evidence['span_start_mm']:g}–{evidence['span_end_mm']:g} мм) "
                f"прорезается насквозь пазом глубиной {evidence['groove_depth_mm']:g} мм "
                f"системы {result['system']['profile_code']}. Для {evidence['groove_count']} паза(ов) "
                f"требуется участок не уже {evidence['required_strip_mm']:g} мм, включая геометрический остаток 1 мм. "
                "Измените раскладку панелей у проёма/края стены; шаг анкеров и саморезов эту ошибку не исправит.")
        checks.append(check)
    for index,run in enumerate(result["profile_runs"]):
        a,b,c,d = _run_box(run)
        overlaps = [other["id"] for other in result["profile_runs"][index+1:] for x,y,u,v in [_run_box(other)] if min(c,u)-max(a,x)>EPS and min(d,v)-max(b,y)>EPS]
        checks.append({"code":"profiles_nonoverlap","status":"valid" if not overlaps else "invalid","entity_id":run["id"],"other_ids":overlaps})
        if run["family"] in {"sky", "start"}:
            panels=[p["id"] for p in result["panels"] if _panel_profile_contact(p,run,gap)]
            checks.append({"code":"perimeter_profile_panel_contact","status":"valid" if panels else "invalid",
                           "entity_id":run["id"],"panel_ids":panels})
    if not result["profile_runs"]:
        checks.append({"code":"profiles_not_empty","status":"invalid"})
    checks.extend(_hidden_support_coverage(result))
    # Global guard, independent of profile/member row membership. Treating
    # all projected heads as sharing depth is conservative and deterministic.
    points=result["screws"]+result["profile_screws"]+result["anchors"]
    cells={}
    for point in points:
        x,z=point["x_mm"],point["z_mm"]
        cell=(math.floor(x/HIDDEN_HEAD_CLEARANCE),math.floor(z/HIDDEN_HEAD_CLEARANCE))
        nearby=[other for dx in (-1,0,1) for dz in (-1,0,1) for other in cells.get((cell[0]+dx,cell[1]+dz),[])]
        close=[other["id"] for other in nearby if math.hypot(x-other["x_mm"],z-other["z_mm"])<HIDDEN_HEAD_CLEARANCE-EPS]
        checks.append({"code":"global_fastener_head_clearance","status":"invalid" if close else "valid",
                       "entity_id":point["id"],"other_ids":close,"required_mm":HIDDEN_HEAD_CLEARANCE})
        cells.setdefault(cell,[]).append(point)


def _hidden_support_coverage(plan):
    """Audit actual retained supports at every changing elevation band.

    A horizontal lintel/sill supports a strip extending half the configured
    support pitch vertically; it is not treated as a post at every height.
    Within each actual substrate interval, gaps between supports and terminal
    overhangs are bounded. The strip width is an explicit geometric project
    assumption, not an assertion of backing-board load capacity.
    """
    cfg = WallFramingConfiguration.model_validate(plan["configuration"])
    length,height = plan["wall"]["length_mm"],plan["wall"]["height_mm"]
    half=cfg.frame_pitch_mm/2
    breaks={0.,height}
    for opening in plan["openings"]:
        breaks.update((opening["y_mm"],opening["y_mm"]+opening["height_mm"]))
    for post in plan["posts"]:
        breaks.update((post["z_start_mm"],post["z_end_mm"]))
    for rail in plan["rails"]:
        breaks.update((max(0.,rail["z_mm"]-half),min(height,rail["z_mm"]+half)))
    checks=[]
    ordered=sorted(breaks)
    for bottom,top in zip(ordered,ordered[1:]):
        if top-bottom<=EPS:
            continue
        z=(bottom+top)/2
        voids=[(o["x_mm"],o["x_mm"]+o["width_mm"]) for o in plan["openings"] if o["y_mm"]-EPS<z<o["y_mm"]+o["height_mm"]+EPS]
        for left,right in _subtract(0,length,voids):
            supports=[(p["x_mm"],p["x_mm"]) for p in plan["posts"] if p["z_start_mm"]-EPS<z<p["z_end_mm"]+EPS and left-EPS<=p["x_mm"]<=right+EPS]
            supports.extend((max(left,r["x_start_mm"]),min(right,r["x_end_mm"])) for r in plan["rails"] if abs(r["z_mm"]-z)<=half+EPS and min(right,r["x_end_mm"])-max(left,r["x_start_mm"])>EPS)
            merged=[]
            for a,b in sorted(supports):
                if merged and a<=merged[-1][1]+EPS:
                    merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
                else:
                    merged.append((a,b))
            maximum=max((b[0]-a[1] for a,b in zip(merged,merged[1:])),default=0.)
            overhang=max(merged[0][0]-left,right-merged[-1][1]) if merged else right-left
            limit=max(half,cfg.anchor_edge_mm,cfg.opening_edge_mm)
            checks.append({"code":"actual_frame_support_coverage","status":"valid" if merged and maximum<=cfg.frame_pitch_mm+EPS and overhang<=limit+EPS else "invalid",
                           "z_start_mm":_rounded(bottom),"z_end_mm":_rounded(top),"x_start_mm":_rounded(left),"x_end_mm":_rounded(right),
                           "maximum_gap_mm":_rounded(maximum),"maximum_overhang_mm":_rounded(overhang),
                           "gap_limit_mm":cfg.frame_pitch_mm,"overhang_limit_mm":limit,"support_count":len(merged)})
    return checks


def validate_hidden_layout(plan):
    """Independent, pure guard for native workers and saved-record validators.

    Reconstruct the positions from bounded geometry/configuration, then audit
    the retained supports. Never trust status/checks supplied in the plan.
    """
    if not isinstance(plan,dict) or plan.get("system",{}).get("key") not in {"vh2","vh3"}:
        raise ValueError("Нужна известная скрытая стеновая система")
    length=_number(plan["wall"].get("length_mm"),"wall.length_mm",positive=True)
    height=_number(plan["wall"].get("height_mm"),"wall.height_mm",positive=True)
    for key in ("panels", "openings", "profile_runs", "posts", "rails", "screws", "anchors", "profile_screws"):
        if not isinstance(plan.get(key),list):
            raise ValueError("Некорректный список элементов скрытой стены: "+key)
    for panel in plan["panels"]:
        _rect(panel,length,height,"panel")
    for opening in plan["openings"]:
        _rect(opening,length,height,"opening")
    for run in plan["profile_runs"]:
        if not isinstance(run,dict) or run.get("family") not in {plan["system"]["key"],"lstart", *plan["system"].get("perimeter_families", [])} or run.get("axis") not in {"vertical","horizontal"}:
            raise ValueError("Неизвестный прогон скрытого профиля")
        along,across=(height,length) if run["axis"]=="vertical" else (length,height)
        start=_number(run.get("start_mm"),"profile.start_mm")
        end=_number(run.get("end_mm"),"profile.end_mm")
        edge=_number(run.get("edge_mm"),"profile.edge_mm")
        if end-start<20-EPS or end>along+EPS or edge>across+EPS:
            raise ValueError("Скрытый профиль выходит за границы стены или слишком короткий")
    cfg=WallFramingConfiguration.model_validate(plan.get("configuration",{}))
    rebuilt=deepcopy(plan)
    for key in ("posts","rails","screws","anchors","profile_screws","checks"):
        rebuilt[key]=[]
    _populate_frame(rebuilt,cfg,rebuilt["profile_runs"])
    _profile_screws(rebuilt)
    for key in ("posts","rails","screws","anchors","profile_screws"):
        if rebuilt[key]!=plan.get(key):
            raise ValueError("Скрытая расстановка не соответствует детерминированному расчёту: "+key)
    _validate_hidden_plan(rebuilt)
    invalid=[check for check in rebuilt["checks"] if check["status"]!="valid"]
    if invalid:
        raise ValueError("Скрытая расстановка нарушает геометрические ограничения: "+", ".join(sorted({c["code"] for c in invalid})))
    return True


def _plan_extended_system(preview, configuration, system):
    config = configuration if isinstance(configuration,WallFramingConfiguration) else WallFramingConfiguration.model_validate(configuration or {})
    wall = deepcopy(preview["wall"])
    length = _number(wall.get("length_mm"),"wall.length_mm",positive=True)
    height = _number(wall.get("height_mm"),"wall.height_mm",positive=True)
    substrate_start,substrate_end = substrate_interval(wall)
    if len(preview.get("panels",[]))>2500 or len(preview.get("openings",[]))>200:
        raise ValueError("Превышено число панелей или проёмов полной стены")
    mount = deepcopy(preview.get("effective_configuration",preview.get("configuration",{})))
    thickness = _number(mount.get("panel_thickness_mm",12),"panel_thickness_mm",positive=True)
    result = {"schema_version":"wall-framing-plan/1.0","status":"ready" if system["supported"] else "unsupported",
              "wall":wall,"configuration":config.model_dump(mode="json"),"mount_configuration":mount,"system":deepcopy(system),
              "geometry_signature":preview.get("geometry_signature"),"panels":[_rect(p,length,height,"panel") for p in preview.get("panels",[])],
              "openings":[_rect(o,length,height,"opening") for o in preview.get("openings",[])],"joint_lines":deepcopy(preview.get("joint_lines",[])),
              "coordinates":{"x":"along_wall_face","y":"depth_into_substrate","z":"up_from_wall_bottom","panel_rect_y":"assembly_z"},
              "layers":{"panel_thickness_mm":thickness,"adhesive_thickness_mm":0.,"backing_thickness_mm":system.get("backing_thickness_mm",12.),
                        "backing_front_mm":15.9,"frame_depth_mm":FRAME_DEPTH,"substrate_front_mm":thickness+config.cavity_mm},
              "posts":[],"rails":[],"screws":[],"anchors":[],"profile_runs":[],"profile_screws":[],"checks":[],"issues":[],
              "assumptions":[],"evidence":deepcopy(system.get("source_refs",[]))}
    reasons = [] if system["supported"] else [system["reason"]]
    if config.substrate_type not in {"concrete","solid_masonry"}:
        reasons.append("Геометрическая анкеровка предусмотрена только в сплошное основание.")
    if not config.allow_project_assumptions:
        reasons.append("Нужно разрешить проектную подоснову, каркас и геометрическую модель анкера.")
    if reasons:
        result["status"]="unsupported"
        result["issues"]=[{"code":"WALL_FRAMING_UNSUPPORTED","severity":"error","message":message} for message in reasons]
        return _finish(result)
    result["assumptions"] = [
        {"id":"hidden-frame-adaptation","kind":"project_assumption","message":"В методичке показаны стойки V-WS, подвесы V-SH и направляющие V-WT. Здесь используется самостоятельная проектная конструкция: непрерывные V-WT, сплошная подоснова с вырезами по проёмам и дистанционная анкеровка. Она не объявлена заводской системой каркаса."},
        {"id":"hidden-backing","kind":"project_assumption","message":f"Тыльная полка скрытого профиля находится на отметке 15,9 мм от лицевой поверхности. Подоснова {system['backing_thickness_mm']:g} мм расположена за полкой. Панели не приклеены: нагрузочный путь идёт через реальные пазы и профили; несущая способность не рассчитана."},
        {"id":"hidden-edges","kind":"project_assumption","message":"Периметр и края проёмов оформлены V-L-start по измеренной проектной посадке, не по сходству названия с V-Ang-Start. Стенка выступает на 2 мм за торец панели. Нижний/верхний профиль устанавливается только если заданный зазор не меньше 2 мм; исходные размеры не меняются."},
        {"id":"hidden-grooves","kind":"project_assumption","message":f"Пазы {system['panel_edge']}: глубина {system['groove']['depth_mm']:g} мм, раскрытие {system['groove']['width_mm']:g} мм, лицевая полка 5,5 мм. Реальное вычитание материала применяется также на проектных торцах обрамления; последовательность сборки и съёмность этим не подтверждены."},
        {"id":"hidden-spacing","kind":"project_assumption","message":f"Максимальный шаг стоек {config.frame_pitch_mm:g} мм, саморезов {config.screw_pitch_mm:g} мм и анкеров {config.anchor_pitch_mm:g} мм задан проектом. Длины профилей получены вытягиванием точных сечений STEP, не растягиванием исходных деталей."},
        {"id":"hidden-anchor","kind":"project_assumption","message":f"Анкер — геометрическая модель болта Ø6 и втулки Ø8 с заделкой {config.anchor_embedment_mm:g} мм в принятое основание {config.substrate_type}, t={config.substrate_thickness_mm:g} мм. Марка анкера, нагрузки и пригодность основания не назначены."},
        {"id":"hidden-scope","kind":"project_assumption","message":system["scope"]},
    ]
    if min(substrate_end-substrate_start,height)<=2*config.anchor_edge_mm:
        result["issues"].append({"code":"WALL_FRAMING_TOO_SMALL","severity":"error","message":"Недостаточно места для анкеров с выбранными отступами."})
        return _finish(result)
    result["profile_runs"] = _hidden_profile_runs(preview,system["key"],system.get("perimeter_families", []))
    if system.get("perimeter_families"):
        next(a for a in result["assumptions"] if a["id"]=="hidden-edges")["message"] = (
            "По потолку выбран точный V-Sky при зазоре не менее 8 мм; при низе панели 59 мм выбран V-Start1. "
            "Остальные торцы/проёмы используют V-L-start по измеренной проектной посадке. "
            "Пазы на прямом торце Sky/Start не добавляются. Профили периметра соединены с общей подосновой "
            "отдельными проектными слоями; панели скрытого поля не приклеены.")
    _populate_frame(result,config,result["profile_runs"])
    _profile_screws(result)
    _validate_hidden_plan(result)
    result["attachment_summary"]={"profile_run_count":len(result["profile_runs"]),
        "profile_length_mm":_rounded(sum(r["end_mm"]-r["start_mm"] for r in result["profile_runs"])),
        "profile_fastener_count":len(result["profile_screws"]),"frame_fastener_count":len(result["screws"]),
        "adhesive_panel_count":0,"clip_count":0,
        "mechanical_panel_count":len(_connected_panels(result["panels"]))}
    return _finish(result)


def plan_wall_framing(preview, configuration=None):
    from .wall_systems import resolve_wall_system
    system = resolve_wall_system(preview)
    if system["key"] == "legacy_v0":
        return _plan_legacy_v0(preview,configuration)
    return _plan_extended_system(preview,configuration,system)


def _finish(result):
    invalid = [c for c in result["checks"] if c["status"] == "invalid"]
    if invalid:
        explanation = " ".join(c["message"] for c in invalid if c.get("message"))
        result["issues"].append({"code": "WALL_FRAMING_GEOMETRY_INVALID", "severity": "error", "message": f"Не пройдено геометрических проверок расстановки: {len(invalid)}. "+(explanation or "Измените шаг/отступы или геометрию стены."), "check_codes": sorted({c["code"] for c in invalid})})
    if result["status"] != "unsupported" and any(i["severity"] == "error" for i in result["issues"]):
        result["status"] = "invalid"
    members = result["posts"] + result["rails"]
    result["summary"] = {"post_count": len(result["posts"]), "rail_count": len(result["rails"]), "screw_count": len(result["screws"]), "anchor_count": len(result["anchors"]),
                         "panel_piece_count": len(result["panels"]), "panel_count": len({p.get("panel_id", p["id"]) for p in result["panels"]}),
                         "frame_length_mm": _rounded(sum((m["x_end_mm"] - m["x_start_mm"]) if m["orientation"] == "horizontal" else (m["z_end_mm"] - m["z_start_mm"]) for m in members)),
                         "check_count": len(result["checks"]), "invalid_check_count": len(invalid)}
    result["id"] = digest(result)
    return result
