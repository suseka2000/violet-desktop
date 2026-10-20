"""Exact local STEP routes for independently assigned junction groups.

Legacy, non-overridden details retain their original descriptors and hashes.
An explicit choice never falls back to the global wall's edge or corner recipe.
"""
from copy import deepcopy
import math


def node_group(node):
    kind = node.get("kind", "")
    return ("joints" if kind in {"vertical_joint", "horizontal_joint"} else
            kind if kind in {"floor", "ceiling"} else
            "corners" if kind in {"left_edge", "right_edge"} else
            "openings" if kind.startswith("opening_") else None)


def explicit_group_rule(preview, node):
    """The saved request is authoritative; missing preview metadata is unsafe."""
    cfg = preview.get("configuration", {})
    requested = (cfg.get("node_rule_ids") or {}).get(node_group(node))
    return requested or (node.get("requested_rule_id") if node.get("selection_origin") == "group_override" else None)


def group_recipe_for(preview, node, assets):
    from .assembly_recipes import (_descriptor, _legacy_recipe_for, _panel,
                                  _placement, _source, _ungrouped_profile_recipe_for,
                                  edge_height_mismatch)
    from .assembly_recipes_concealed import _source as hidden_source

    requested = explicit_group_rule(preview, node)
    if requested is None:
        return None
    fail = lambda message: {"message": message}
    cfg = preview.get("configuration", {})
    if (node.get("selection_origin") != "group_override" or node.get("requested_rule_id") != requested
            or node.get("rule_id") != requested or node.get("status") == "unresolved"
            or node.get("decision_status") == "unresolved"):
        return fail("Выбранный вариант этой группы не совместим или не определён. Общая система стены не подставлена вместо него.")
    rules = {rule.get("id"): rule for rule in preview.get("snapshot", {}).get("rules", [])}
    rule = rules.get(requested)
    if not rule or rule.get("source_status") != "verified":
        return fail("Выбранный вариант группы отсутствует среди проверенных правил зафиксированной библиотеки.")
    codes = set(node.get("component_codes", []))
    if not codes or not codes.issubset(set(rule.get("component_codes", []))):
        return fail("Профили выбранного узла не соответствуют его зафиксированному правилу; замена общим профилем запрещена.")
    if edge_height_mismatch(preview, node):
        return fail("Этот край стены не совпадает с полом или потолком помещения; выбранное примыкание не заменяет свободный торец.")
    t, gap = cfg.get("panel_thickness_mm"), cfg.get("joint_mm")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (t, gap)) or not 0 < t <= 200 or gap < 0:
        return fail("Некорректная толщина панели или шов.")
    local = deepcopy(preview)
    local["configuration"].pop("node_rule_ids", None)
    # Selecting the separately labelled local option is consent for this
    # group's project adaptation only, never for the rest of the wall.
    local["configuration"]["allow_project_assumptions"] = True
    local["selected_rule_id"] = requested
    local["selection_status"] = "group_override"
    local["candidates"] = [{"rule_id": requested, "status": "project_assumption",
                            "component_codes": list(rule.get("component_codes", []))}]
    selected_node = deepcopy(node)
    selected_node.pop("selection_origin", None)
    group = node_group(node)

    if group == "joints":
        families = codes.intersection({"V-T1", "V-T2", "V-Base", "VH-1", "VH-2", "VH-3", "VH-4", "V-Spike", "V-F", "V-F2", "V-Z", "V-PC", "V-Clip"})
        if len(families) != 1:
            return fail("Для выбранного стыка не определён один точный STEP-профиль; неоднозначный перечень не заменён V-T1/VH.")
        # Profile catalog pages are not substitutes for a complete hidden seat.
        # The hidden resolver requires its own exact flat-wall manual rule.
        return _ungrouped_profile_recipe_for(local, selected_node, assets)

    if group == "ceiling" and codes == {"V-Sky"}:
        clearance = cfg.get("ceiling_gap_mm", 0)
        if not isinstance(clearance, (int, float)) or isinstance(clearance, bool) or not math.isfinite(clearance) or clearance < 8:
            return fail("Выбранному V-Sky требуется зазор до потолка не менее 8 мм. Он не заменён уголком или V-L13, зазор не изменён.")
        if not 1 <= t <= 17.8:
            return fail("Панель не помещается в измеренную посадку V-Sky (толщина 1–17,8 мм).")
        source = hidden_source("V-Sky", assets)
        if not source:
            return fail("Нет точного зафиксированного STEP V-Sky для выбранного потолочного узла.")
        return _descriptor("concealed-sky-top", "Верх панели / выбранный V-Sky", source,
            _placement(source, [[0, 1, 0], [0, 0, 1], [1, 0, 0]], [0, t, 0]),
            [_panel("edge", [80, t, 30], [0, 0, -15])], [
                "V-Sky назначен только потолочной группе. Стыки, пол, углы и проёмы используют собственные назначения.",
                "X направлен от верхнего торца вниз, Y от лица в толщину; тыльная грань касается полки V-Sky, верхний торец прямой.",
                "Профиль занимает X от -8 до 39,8 мм. Зазор не меняется; условная геометрия потолка не добавляется.",
            ], deepcopy(node.get("source_refs") or []))

    if group == "floor" and codes in ({"V-Start"}, {"V-Start-1"}):
        clearance = cfg.get("floor_gap_mm", 0)
        if not isinstance(clearance, (int, float)) or isinstance(clearance, bool) or not math.isfinite(clearance) or not math.isclose(clearance, 59., abs_tol=1e-6) or not 0 < t <= 14:
            return fail("Для имеющегося STEP V-Start-1 нужна отметка низа панели 59 мм и толщина до 14 мм. Другой плинтус или задний уголок не подставлен.")
        if _source("V-Start-1", assets) is None:
            return fail("Нет точного зафиксированного STEP V-Start-1 для выбранного нижнего примыкания.")
        # This calibrated seat depends on the straight bottom end, not on the
        # joint system. Preserve the real project's configuration and sources.
        local["configuration"]["mounting_system"] = "open_mount"
        result = _legacy_recipe_for(local, selected_node, assets)
        if result.get("spec"):
            result["source_refs"] = deepcopy(node.get("source_refs") or [])
        return result

    if group in {"floor", "ceiling", "openings"} and codes == {"V-L13"}:
        return _legacy_recipe_for(local, selected_node, assets)

    # Fixing the already resolved automatic detail is not a request for an
    # unrelated replacement. Preserve its exact measured, labelled adaptation
    # (e.g. the known door L-profile / L13 or corner / 20x20 recipe). Only the
    # original rule and its original components qualify; another choice cannot
    # obtain this exception merely by sharing a family name or a node kind.
    inherited = (node.get("inherited_rule_id") == requested and
                 codes.issubset(set(node.get("inherited_component_codes", []))))
    if inherited and group in {"corners", "openings", "floor", "ceiling"}:
        baseline = deepcopy(preview)
        baseline["configuration"].pop("node_rule_ids", None)
        baseline["configuration"]["allow_project_assumptions"] = True
        result = _ungrouped_profile_recipe_for(baseline, selected_node, assets)
        if result.get("spec"):
            sources = {item["source_id"] for item in result.get("source_models", [])}
            from .assembly_recipes import CALIBRATIONS
            # The same broad manual rule must not authorize a new silent
            # fallback from a selected end profile to the zero-gap rear angle.
            if group in {"floor", "ceiling"} and (codes != {"L-profile"} or
                    sources != {CALIBRATIONS["V-L13"]["source_id"]}):
                return fail("Выбранное торцевое примыкание не совпадает с измеренным профилем прежнего рецепта. Задний уголок или другой плинтус вместо него не подставлен.")
            return result
        return result
    return fail("Для явно выбранных профилей этой группы (" + ", ".join(sorted(codes)) +
                ") ещё нет точной STEP-посадки. Общая система стены, V-L13 и задний уголок не подставлены вместо выбора.")
