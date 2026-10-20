"""Independent, context-bound choices for the five groups of wall details.

The legacy automatic decision is retained unless that exact group is overridden.
An explicit catalogue adaptation is a local project choice, not consent to turn
on assumptions for every other node of the wall.
"""
from __future__ import annotations

from .mount_defaults import FAMILIES, assumption, source_reference
from .vendor_design import _source

GROUPS = {"joints": "Стыки панелей", "floor": "Пол", "ceiling": "Потолок",
          "corners": "Углы и торцы", "openings": "Проёмы"}
CONTEXTS = {"joints": {"panel_joint", "horizontal_joint"}, "floor": {"floor_junction"},
            "ceiling": {"ceiling_junction"}, "corners": {"internal_corner", "external_corner", "wall_end"},
            "openings": {"opening_head", "opening_sill", "opening_jamb", "opening_jamb_left", "opening_jamb_right"}}
TOPICS = {"mounting_assembly", "junction_detail", "junction_reference", "opening_detail", "profile_geometry", "profile_section"}
FRAME_CODES = {"V-WS", "V-SH", "V-WT", "V-HSIF", "V-C1", "V-Tape"}
NUMERIC_PARAMETERS = {"joints": {"panel_joint_width_mm", "panel_rear_gap_mm", "profile_rear_clearance_mm"},
                      "floor": {"plinth_support_height_mm", "radius_ang_start_piece_length_mm"},
                      "ceiling": set(), "corners": {"clip_pitch_mm"}, "openings": set()}


def group_for_node(node):
    kind = node["kind"]
    return "joints" if kind in {"vertical_joint", "horizontal_joint"} else kind if kind in {"floor", "ceiling"} else "openings" if kind.startswith("opening_") else "corners"


def rule_eligible_for_group(rule, group):
    return (rule.kind == "detail" and rule.topic in TOPICS and
            bool(set(rule.contexts) & CONTEXTS[group]))


def validate_group_rule_ids(assignments, snapshot):
    rules = {rule.id: rule for rule in snapshot.rules}
    for group, ident in assignments.items():
        rule = rules.get(ident)
        if group not in GROUPS or rule is None or not rule_eligible_for_group(rule, group):
            raise ValueError("Выбранный узел отсутствует в зафиксированной методичке или относится к другой группе")
        if rule.source_status != "verified":
            raise ValueError("Первоисточник выбранного узла группы не подтверждён")


def component_codes_for_group(rule, group):
    """Do not advertise a floor profile as the ceiling in a multi-detail page."""
    codes = list(rule.component_codes)
    if group == "ceiling":
        # V-LED in the verified compound pages is a lit panel joint, not a
        # ceiling detail. The page-wide lighting flag must not leak its parts
        # into an independently selected V-Sky termination.
        return [code for code in codes if code in {"V-Sky", "L-profile", "V-L13-2"}]
    if group == "floor":
        if rule.id == "open-1-5":
            # This page assigns its LED to the V-Base panel joint. Unlike the
            # concealed lit-plinth pages, it is not a floor-lighting profile.
            codes = [code for code in codes if code != "V-LED"]
        selected = [code for code in codes if code.startswith("V-Start") or code in {"V-Ang-Start", "V-L13-2", "L-profile", "V-PW", "V-LED"}]
        return selected or [code for code in codes if code not in FRAME_CODES | FAMILIES]
    if group == "corners":
        return [code for code in codes if code not in FRAME_CODES and not code.startswith("V-Start")]
    return codes


def _check(parameter, status, message, **values):
    return {"parameter": parameter, "status": status, "message": message, **values}


def _context(node):
    kind = node["kind"]
    if kind in {"left_edge", "right_edge"}:
        edge = node.get("edge_context", {})
        local = edge.get("local_panel_junction", {})
        if _valid_local_corner(edge):
            return local["kind"]
        return edge.get("corner_type") or ("wall_end" if edge.get("connection") == "free_end" else "unresolved")
    return {"vertical_joint": "panel_joint", "horizontal_joint": "horizontal_joint", "floor": "floor_junction", "ceiling": "ceiling_junction"}.get(kind, kind)


def _valid_local_corner(edge):
    local = edge.get("local_panel_junction", {})
    if edge.get("adjacent_opening_ids") or local.get("kind") != "internal_corner" or not local.get("right_angle"):
        return False
    matching = [item for item in edge.get("adjacent_mounting", []) if
                item.get("geometry_signature_verified") is True and
                item.get("wall_id") == local.get("adjacent_wall_id") and item.get("face_id") == local.get("adjacent_face_id")]
    return len(matching) == 1


def _node_applicability(rule, node):
    context, contexts = _context(node), set(rule.contexts)
    direct = context in contexts or (context.startswith("opening_jamb") and "opening_jamb" in contexts)
    # A window sill/head may explicitly adopt the documented side reveal, but
    # is labelled as an adaptation; a door jamb never becomes a window sill.
    window_analogy = (node.get("opening_kind") == "window" and context in {"opening_head", "opening_sill"}
               and "window_opening" in contexts and bool(contexts & {"opening_jamb", "opening_jamb_left", "opening_jamb_right"}))
    horizontal_analogy = context == "horizontal_joint" and "panel_joint" in contexts
    analogy = window_analogy or horizontal_analogy
    checks = [_check("node_context", "match" if direct or analogy else "mismatch",
                     "Контекст узла указан в источнике" if direct else "Поворот схемы стыка для горизонтального шва — проектная адаптация; несущая работа требует проверки" if horizontal_analogy else "Перенос бокового оконного откоса на верх/низ окна — проектная адаптация" if window_analogy else "Источник относится к другому месту примыкания")]
    if context in {"internal_corner", "external_corner"} and not (node.get("edge_context", {}).get("right_angle") or _valid_local_corner(node.get("edge_context", {}))):
        checks.append(_check("corner_angle", "mismatch", "Эта схема предназначена для прямого угла; геометрия текущего угла не подтверждена"))
    if context.startswith("opening_"):
        kind = node.get("opening_kind")
        expected = "window_opening" if kind == "window" else "door_opening" if kind in {"door", "passage"} else None
        specific = contexts & {"window_opening", "door_opening"}
        if specific and expected not in specific:
            checks.append(_check("opening_kind", "mismatch", "Источник относится к другому типу проёма"))
    return checks, analogy


def _candidate(rule, group, nodes, config, snapshot, assets, chosen):
    from .wall_mount import _filename_matches
    documents = {document.id: document for document in snapshot.documents}
    checks = [_check("mounting_system", "match" if rule.system in {config.mounting_system, "general"} else "mismatch",
                     "Система монтажа", actual=config.mounting_system, expected=rule.system)]
    codes = component_codes_for_group(rule, group)
    related = [item for item in snapshot.rules if item.assembly_id == rule.assembly_id and item.system == rule.system and item.source.document_id == rule.source.document_id] if rule.assembly_id else [rule]
    adaptation = rule.topic in {"profile_geometry", "profile_section"}
    if adaptation:
        checks.append(_check("catalogue_adaptation", "unknown", "Каталожное сечение применяется к этому месту как отдельное проектное решение; источник не задаёт полный монтажный узел"))
    if group != "corners" and chosen and chosen["rule_id"] != rule.id:
        adaptation = True
        checks.append(_check("cross_assembly_adaptation", "unknown", "Этот узел взят из другого раздела, чем общая система стены. Назначение только этой группе — проектная адаптация; остальные узлы не заменяются."))
    special = set(rule.contexts) & {"outer_radius", "inner_radius", "removable_panel"}
    if special:
        if group == "joints":
            checks.append(_check("wall_context", "mismatch", "Сборка предназначена для радиусной или съёмной панели, а не обычного поля стены"))
        else:
            adaptation = True
            checks.append(_check("special_context", "unknown", "Узел взят из раздела радиусных или съёмных панелей; его применение здесь — отдельная проектная адаптация"))
    if group == "joints":
        dimensions = [item for item in related if item.kind == "numeric" and item.parameter == "panel_joint_width_mm" and item.unit == "mm"]
        if not dimensions:
            checks.append(_check("joint_mm", "unknown", "Размер шва не задан в проверенной выписке; принимается значение проекта"))
            adaptation = True
        for item in dimensions:
            low = item.value if item.value is not None else item.value_min
            high = item.value if item.value is not None else item.value_max
            status = "unknown" if item.source_status != "verified" else "match" if low-1e-6 <= config.joint_mm <= high+1e-6 else "mismatch"
            checks.append(_check("joint_mm", status, item.requirement, actual_mm=config.joint_mm, minimum_mm=low, maximum_mm=high, rule_id=item.id, source=_source(item, documents)))
            adaptation |= status == "unknown"
    if group == "corners":
        family = set(chosen["component_codes"]) & FAMILIES if chosen else set()
        other = set(codes) & FAMILIES
        # A user may independently select V-Base seams and V-Base corners
        # while retaining a legacy T1 base. Family coordination is reported
        # below, not used to overwrite an already chosen corner when seams
        # later change. Topology, panel edge and mounting-system conflicts
        # remain hard failures.
        if other and (not family or not family & other):
            adaptation = True
            checks.append(_check("profile_family", "unknown", "Семейство углового профиля отличается от общей системы стены или пока не определено. Это отдельное проектное назначение; совместимость с выбранными стыками проверяется отдельно."))
    if config.panel_edge == "V-0" and rule.system == "concealed_mount" or config.panel_edge in {"Va", "Vb", "Vc", "Vd"} and rule.system == "open_mount":
        checks.append(_check("panel_edge", "mismatch", "Обработка кромки относится к другой системе монтажа"))
    if config.panel_edge == "V-0":
        thickness = next((item for item in snapshot.rules if item.id == "open-v0-thickness" and item.source_status == "verified"), None)
        if thickness:
            low = thickness.value if thickness.value is not None else thickness.value_min
            high = thickness.value if thickness.value is not None else thickness.value_max
            checks.append(_check("panel_thickness_mm", "match" if low <= config.panel_thickness_mm <= high else "mismatch", thickness.requirement, actual_mm=config.panel_thickness_mm, minimum_mm=low, maximum_mm=high))
        else:
            adaptation = True
            checks.append(_check("panel_thickness_mm", "unknown", "Нет подтверждённой выписки допустимой толщины панели V-0; её применимость не проверена", actual_mm=config.panel_thickness_mm))
    elif not config.panel_edge:
        adaptation = True
        checks.append(_check("panel_edge", "unknown", "Кромка или паз панели не назначены; совместимость профиля с обработкой панели требует проверки"))
    node_checks, applicable, rejected = [], [], []
    for node in nodes:
        local, analogy = _node_applicability(rule, node)
        accepted = not any(item["status"] == "mismatch" for item in local)
        (applicable if accepted else rejected).append(node["id"])
        node_checks.append({"node_id": node["id"], "label": node["label"], "kind": node["kind"], "applicable": accepted, "project_adaptation": analogy or adaptation, "checks": local})
        if accepted and analogy:
            adaptation = True
    if nodes and not applicable:
        checks.append(_check("coverage", "mismatch", "На этой стороне стены нет узлов, подходящих под область применения этого источника"))
    elif rejected:
        checks.append(_check("coverage", "match", f"Применится к {len(applicable)} из {len(nodes)} мест. Остальные типы примыканий останутся неопределёнными; область применения не расширяется автоматически."))
    statuses = {item["status"] for item in checks}
    status = "incompatible" if "mismatch" in statuses else "unknown" if adaptation or "unknown" in statuses else "candidate"
    matches = [_filename_matches(code, assets) for code in codes]
    requirements = [item for item in related if item.kind != "numeric" or item.parameter in NUMERIC_PARAMETERS[group]
                    or (group == "floor" and item.parameter == "panel_rear_gap_mm" and "А–А" in item.scope)]
    parameters = [{"rule_id": item.id, "parameter": item.parameter, "value": item.value, "value_min": item.value_min,
                   "value_max": item.value_max, "unit": item.unit, "scope": item.scope, "source": _source(item, documents),
                   "comparison": "project_joint_width" if item.parameter == "panel_joint_width_mm" else "reference_dimension_not_a_panel_gap"}
                  for item in requirements if item.kind == "numeric"]
    return {"rule_id": rule.id, "title": rule.title, "assembly_id": rule.assembly_id, "system": rule.system,
            "group_id": group, "status": status, "checks": checks, "source": _source(rule, documents),
            "component_codes": codes, "rule_component_codes": list(rule.component_codes), "contexts": list(rule.contexts),
            "requires_project_adaptation": adaptation, "coverage": {"applicable_node_ids": applicable, "rejected_node_ids": rejected, "total_count": len(nodes), "applicable_count": len(applicable)},
            "node_checks": node_checks, "step_matches": [match for items in matches for match in items[:20]],
            "step_matches_omitted": sum(max(0, len(items)-20) for items in matches),
            "source_parameters": parameters,
            "requirements": [{"rule_id": item.id, "title": item.title, "requirement": item.requirement, "scope": item.scope, "source": _source(item, documents)} for item in requirements],
            "approval_status": "unreviewed"}


def _unresolved(node, rule_id, reason, status):
    node.update(status="unresolved", decision_status="unresolved", rule_id=None, source=None, source_refs=[], component_codes=[],
                detail_key="unresolved."+_context(node), assumption_ids=[], required_checks=[reason], decision_reason=reason,
                requested_rule_id=rule_id, selection_origin="group_override", group_selection_status=status, manufacturer_confirmed=False)


def apply_node_groups(config, effective, snapshot, assets, chosen, nodes, assumptions):
    validate_group_rule_ids(config.node_rule_ids, snapshot)
    if len(snapshot.rules) > 500:
        raise ValueError("Снимок превышает предел подбора групп: 500 правил")
    documents = {document.id: document for document in snapshot.documents}
    by_id = {rule.id: rule for rule in snapshot.rules}
    for node in nodes:
        node["group_id"] = group_for_node(node)
    groups = []
    for group, label in GROUPS.items():
        members = [node for node in nodes if node["group_id"] == group]
        rules = sorted((rule for rule in snapshot.rules if rule.source_status == "verified" and rule_eligible_for_group(rule, group)), key=lambda rule: (rule.source.document_id, rule.source.page, rule.id))
        candidates = [_candidate(rule, group, members, effective, snapshot, assets, chosen) for rule in rules]
        requested = config.node_rule_ids.get(group)
        selected = next((candidate for candidate in candidates if candidate["rule_id"] == requested), None) if requested else None
        if requested:
            replaced = {ident for node in members for ident in node.get("assumption_ids", [])}
            assumptions[:] = [item for item in assumptions if item["id"] not in replaced]
            valid = selected is not None and selected["status"] != "incompatible"
            for node in members:
                node["inherited_rule_id"] = node.get("rule_id")
                node["inherited_component_codes"] = list(node.get("component_codes", []))
                entry = next((item for item in selected["node_checks"] if item["node_id"] == node["id"]), None) if selected else None
                if not valid or not entry or not entry["applicable"]:
                    reason = "; ".join(item["message"] for item in (selected["checks"] if not valid and selected else entry["checks"] if entry else []) if item["status"] == "mismatch") or "Выбранный источник не применим к этому месту примыкания"
                    _unresolved(node, requested, reason, "incompatible" if not valid else "partial")
                    continue
                rule = by_id[requested]
                adapted = selected["requires_project_adaptation"] or entry["project_adaptation"]
                refs = [source_reference(rule, documents, "project_adaptation" if adapted else "assembly")]
                unknown = [item["message"] for item in selected["checks"] if item["status"] == "unknown"]
                unknown.extend(item["message"] for item in entry["checks"] if entry["project_adaptation"] and item["parameter"] == "node_context")
                reason = ("Отдельное проектное решение для этой группы. " + " ".join(dict.fromkeys(unknown))) if adapted else "Источник выбран отдельно для этой группы; остальные группы не изменены."
                node.update(status="project_assumption" if adapted else "reference_candidate", decision_status="project_assumption" if adapted else "source_reference",
                            rule_id=requested, source=selected["source"], source_refs=refs, component_codes=list(selected["component_codes"]),
                            detail_key=("project." if adapted else "source.")+_context(node), required_checks=["Проверить основание, размеры и совместимость с соседними деталями."],
                            assumption_ids=[], decision_reason=reason, requested_rule_id=requested, selection_origin="group_override", group_selection_status="explicit", manufacturer_confirmed=False)
                if adapted:
                    ident = "detail-"+node["id"]
                    assumptions.append(assumption(ident, reason, "Проверить посадку выбранного профиля, основание и последовательность монтажа.", refs))
                    node["assumption_ids"] = [ident]
            accepted = [node for node in members if node["rule_id"] == requested]
            status = "incompatible" if not valid else "partial" if len(accepted) != len(members) else "explicit"
            reason = "Выбор противоречит известным условиям. Другой узел вместо него не назначен." if not valid else "Выбранный вариант применён только к подходящим местам этой группы; неподходящие места отмечены отдельно." if status == "partial" else "Вариант назначен только этой группе. Остальные группы сохранены."
            selected_id = requested if valid else None
        else:
            ids = {node.get("rule_id") for node in members}
            selected_id = next(iter(ids)) if len(ids) == 1 else None
            status = "automatic" if selected_id else "mixed" if any(ids) else "unresolved"
            reason = "Автоматические решения по общей системе стены; отдельного назначения этой группе нет."
        groups.append({"id": group, "label": label, "node_ids": [node["id"] for node in members], "candidates": candidates,
                       "requested_rule_id": requested, "selected_rule_id": selected_id, "selection_status": status, "selection_reason": reason})
    # Independent choices must stay independent. Detect a mixed family without
    # changing a neighbouring group's automatic/explicit rule or node data.
    by_group = {group["id"]: group for group in groups}
    joint_group = by_group["joints"]
    if config.node_rule_ids.get("joints") or config.node_rule_ids.get("corners"):
        joint_rule = by_id.get(joint_group["selected_rule_id"])
        joint_family = set(joint_rule.component_codes) & FAMILIES if joint_rule else set()
        base_family = set(chosen["component_codes"]) & FAMILIES if chosen else set()
        if config.node_rule_ids.get("joints") and joint_family and base_family and not joint_family & base_family:
            joint_group.setdefault("compatibility_warnings", []).append({
                "code": "GROUP_FAMILY_DIFFERS_FROM_BASE", "related_group": "base",
                "message": f"Стыки выбраны для {', '.join(sorted(joint_family))}, общая система стены — {', '.join(sorted(base_family))}. Остальные группы сохранены; совместимость и общую 3D-сборку нужно проверить отдельно.",
            })
        conflicting = [node for node in nodes if node["group_id"] == "corners" and
                       (set(node.get("component_codes", [])) & FAMILIES) and joint_family and
                       not (set(node.get("component_codes", [])) & joint_family)]
        if conflicting:
            families = sorted({code for node in conflicting for code in node.get("component_codes", []) if code in FAMILIES})
            warning = {"code": "GROUP_CORNER_JOINT_FAMILY_CONFLICT", "related_group": "joints",
                       "node_ids": [node["id"] for node in conflicting],
                       "message": f"Углы остаются с профилями {', '.join(families)}, а стыки выбраны для {', '.join(sorted(joint_family))}. Это разные семейства: выберите совместимое угловое решение. Автоматическая замена углов не выполнялась."}
            by_group["corners"].setdefault("compatibility_warnings", []).append(warning)
            joint_group.setdefault("compatibility_warnings", []).append({**warning, "related_group": "corners"})
        if config.node_rule_ids.get("corners") and not joint_family:
            by_group["corners"].setdefault("compatibility_warnings", []).append({
                "code": "GROUP_JOINT_FAMILY_UNDEFINED", "related_group": "joints",
                "message": "Семейство стыков панелей пока не определено. Угловое назначение сохранено, но его совместимость нельзя проверить до выбора стыков.",
            })
    return groups
