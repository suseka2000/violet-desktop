"""Measured panel terminations where an adjacent opening interrupts a junction.

The vertical opening band contains one panel edge, not two solid wall panels.
Use the exact existing jamb seat only when the opening jamb is geometrically
coincident with the current endpoint. A partition crossing an opening's middle
does not become a supported junction by this route.
"""
from __future__ import annotations

from copy import deepcopy
import math


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def adjacent_opening_terminal(preview, node, pinned_assets):
    from .assembly_recipes import _profile_recipe_for, _unsupported

    edge = node.get("edge_context") or {}
    local = edge.get("local_panel_junction") or edge.get("local_panel_continuation") or {}
    expected = 90. if local.get("kind") == "internal_corner" else 180. if local.get("kind") == "straight" else None
    angle = local.get("angle_deg")
    if (expected is None or local.get("basis") != "mounted_face_normals" or not _finite(angle)
            or not math.isclose(angle, expected, abs_tol=1e-6)):
        return _unsupported("Для окончания у смежного проёма не подтверждено направление сопрягаемых сторон стены.")
    adjacent = edge.get("adjacent_mounting", [])
    if (len(adjacent) != 1 or adjacent[0].get("geometry_signature_verified") is not True
            or adjacent[0].get("wall_id") != local.get("adjacent_wall_id")
            or adjacent[0].get("face_id") != local.get("adjacent_face_id")):
        return _unsupported("Окончание у смежного проёма требует одной актуальной, геометрически проверенной соседней стороны.")
    identifiers = edge.get("adjacent_opening_ids", [])
    active = [void for void in edge.get("adjacent_voids", []) if void.get("opening_id") in identifiers]
    if len(identifiers) != 1 or len(active) != 1:
        return _unsupported("Для окончания панели не определён один конкретный проём соседней стены.")
    opening = active[0]
    distance = opening.get("distance_to_jamb_mm")
    bottom, top, y = opening.get("bottom_mm"), opening.get("top_mm"), node.get("y_mm")
    if (opening.get("wall_id") != local.get("adjacent_wall_id")
            or opening.get("jamb_side") not in {"start", "end"}
            or not _finite(distance) or abs(distance) > 1e-6
            or not all(_finite(value) for value in (bottom, top, y)) or not bottom < y < top
            or not _finite(opening.get("width_mm")) or opening["width_mm"] <= 0
            or opening.get("opening_kind") not in {"door", "passage", "window"}):
        return _unsupported("Торцевая посадка требует точного совпадения с боковой гранью смежного проёма и высоты внутри проёма. Пересечение середины проёма не заменяется откосом.")

    oriented = deepcopy(node)
    oriented.pop("edge_context", None)
    for key in ("selection_origin", "requested_rule_id", "inherited_rule_id", "inherited_component_codes", "group_selection_status"):
        oriented.pop(key, None)
    oriented.update(kind="opening_jamb_left" if node["kind"] == "left_edge" else "opening_jamb_right",
                    opening_kind=opening["opening_kind"], detail_scope="cladding_edges_only")
    # The opening is on another wall. Do not silently claim that a separately
    # chosen corner profile is a valid opening profile, or borrow an unrelated
    # opening override without its context-bound applicability decision.
    if (preview.get("configuration", {}).get("node_rule_ids") or {}).get("openings"):
        from .mount_node_groups import apply_node_groups
        from .mount_defaults import node_decisions
        from .wall_mount import MountConfiguration, ReferenceSnapshot
        cfg = MountConfiguration.model_validate(preview["configuration"])
        snapshot = ReferenceSnapshot.model_validate(preview.get("snapshot"))
        chosen = next((item for item in preview.get("candidates", []) if item.get("rule_id") == preview.get("selected_rule_id")), None)
        if chosen:
            source_rule = next((rule for rule in snapshot.rules if rule.id == chosen["rule_id"]), None)
            chosen = {**chosen, "title": chosen.get("title", source_rule.title if source_rule else chosen["rule_id"]),
                      "status": chosen.get("status", "unknown"), "checks": chosen.get("checks", [])}
        oriented.setdefault("id", "adjacent-opening-terminal")
        oriented["label"] = "Окончание панели у смежного проёма"
        oriented.update(status="unresolved", rule_id=None, source=None)
        # As in group_recipe_for, the explicit opening choice authorizes its
        # labelled local adaptation; it does not alter the wall configuration.
        effective = cfg.model_copy(update={"allow_project_assumptions": True})
        assumptions = node_decisions(effective, snapshot, chosen, [oriented], preview.get("selection_status"))
        apply_node_groups(cfg, effective, snapshot, pinned_assets, chosen, [oriented], assumptions)
    result = _profile_recipe_for(preview, oriented, pinned_assets)
    if not result.get("spec"):
        return result
    result["recipe_id"] = result["recipe_id"].removesuffix("/1.0") + "-adjacent-opening-terminal/1.0"
    result["label"] = "Окончание панели у смежного проёма / " + result["label"]
    result["assumptions"].append(
        "Этот местный разрез относится только к высотному участку смежного проёма: его боковая грань точно совпадает с концом текущей стены. "
        "Показана одна существующая панель с торцевым профилем; отсутствующая внутри проёма вторая облицовка не создаётся. "
        "Выше/ниже проёма применяются самостоятельные узлы сопряжения. Дверная коробка, полотно и состав откоса этим торцевым узлом не заменяются.")
    return result
