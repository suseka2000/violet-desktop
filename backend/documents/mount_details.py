"""Explicitly schematic junction diagrams, never fabricated STEP assemblies.

Only panel thickness and chosen gaps are dimensional facts in these diagrams.
Backing and fittings are represented as concepts, not manufactured geometry.
"""
import json

from .scene import BLUE, GRAY, INK, LIGHT, RED, wrap


def detail_key(preview, node):
    from ..assembly_recipes import edge_height_mismatch
    from ..assembly_recipes_groups import explicit_group_rule
    config = preview["configuration"]
    local_rule = explicit_group_rule(preview, node)
    edge = node.get("edge_context", {})
    # One detail mark is a statement about BOTH the construction conditions and
    # its evidence. Locations/IDs can differ, but unlike materials, edge shapes,
    # angles, checks or source references must not inherit the first location's
    # explanatory sheet merely because the nominal rule happens to be the same.
    height_context = edge_height_mismatch(preview, node)
    # Group drawings only when their recipe's acceptance conditions agree.
    # Otherwise one valid branch could lend its STEP cut to a different-height
    # or stale neighbour merely because both nominal panel settings match.
    local = edge.get("local_panel_junction") or edge.get("local_panel_continuation")
    adjacent_corner_rules = sorted({choice for adjacent in edge.get("adjacent_mounting", [])
                                   if (choice := (((adjacent.get("configuration") or {}).get("node_rule_ids") or {}).get("corners")))})
    junction_evidence = {}
    if local or edge.get("corner_type") == "straight":
        wall = preview.get("wall") or {}
        junction_evidence = {
            "current_base_mm": wall.get("base_mm"), "current_height_mm": wall.get("height_mm"),
            "has_adjacent_openings": bool(edge.get("adjacent_opening_ids")),
            "interior_wall_contact": any(item.get("contact_kind") == "wall_interior" for item in edge.get("adjacent_walls", [])),
            "neighbors": sorted([{
                "configuration": {key: adjacent["configuration"].get(key) for key in (
                    "floor_gap_mm", "ceiling_gap_mm", "allow_project_assumptions", "assembly_rule_id")}
                    if isinstance(adjacent.get("configuration"), dict) else None,
                "base_mm": adjacent.get("wall_base_mm"), "height_mm": adjacent.get("wall_height_mm"),
                "geometry_signature_verified": adjacent.get("geometry_signature_verified"),
                "local_pair_matches": (adjacent.get("wall_id") == local.get("adjacent_wall_id") and
                                       adjacent.get("face_id") == local.get("adjacent_face_id")) if local else None,
            } for adjacent in edge.get("adjacent_mounting", [])], key=lambda value: json.dumps(value, sort_keys=True)),
        }
    opening_evidence = None
    terminal_opening_rule = None
    if edge.get("adjacent_opening_ids"):
        # This edge is resolved as an opening termination, although its plan
        # marker remains left_edge/right_edge. Its explicit opening choice
        # must not share a cut with another wall's different opening choice.
        if local:
            terminal_opening_rule = (config.get("node_rule_ids") or {}).get("openings")
        active_ids = set(edge["adjacent_opening_ids"])
        opening_evidence = sorted([{
            **{key: void.get(key) for key in ("opening_kind", "jamb_side", "distance_to_jamb_mm", "width_mm")},
            "within_active_void": (isinstance(node.get("y_mm"), (int, float))
                                   and isinstance(void.get("bottom_mm"), (int, float))
                                   and isinstance(void.get("top_mm"), (int, float))
                                   and void["bottom_mm"] < node["y_mm"] < void["top_mm"]),
            "matches_adjacent_wall": bool(local and void.get("wall_id") == local.get("adjacent_wall_id")),
        } for void in edge.get("adjacent_voids", []) if void.get("opening_id") in active_ids],
            key=lambda value: json.dumps(value, sort_keys=True))
    evidence = json.dumps({
        **({"installation_zone":"exterior"} if config.get("installation_zone")=="exterior" else {}),
        **({"requested_group_rule": local_rule} if local_rule else {}),
        **({"adjacent_corner_rules": adjacent_corner_rules} if adjacent_corner_rules else {}),
        **({"edge_height_context": height_context} if height_context else {}),
        **({"junction_compatibility": junction_evidence} if junction_evidence else {}),
        **({"adjacent_opening_terminal": opening_evidence} if opening_evidence is not None else {}),
        **({"terminal_opening_rule": terminal_opening_rule} if terminal_opening_rule else {}),
        **({"local_panel_junction": {key: edge["local_panel_junction"].get(key)
             for key in ("kind", "angle_deg", "right_angle", "basis")}}
           if edge.get("local_panel_junction") else {}),
        **({"local_panel_continuation": {key: edge["local_panel_continuation"].get(key)
             for key in ("kind", "angle_deg", "basis", "substrate_offset_delta_mm")}}
           if edge.get("local_panel_continuation") else {}),
        "sources": node.get("source_refs") or ([node["source"]] if node.get("source") else []),
        "component_codes": node.get("component_codes", []),
        "required_checks": node.get("required_checks", []),
        "decision_reason": node.get("decision_reason", ""),
        "adjacent_mounting": sorted([
            {key: adjacent["configuration"].get(key) for key in ("panel_thickness_mm", "panel_edge", "mounting_system", "material", "joint_mm")}
            if isinstance(adjacent.get("configuration"),dict) else None
            for adjacent in edge.get("adjacent_mounting", [])
        ], key=lambda value: json.dumps(value,sort_keys=True)),
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return (node["kind"], node.get("rule_id"), node.get("decision_status", node["status"]),
            edge.get("corner_type"), edge.get("interior_angle_deg"), edge.get("right_angle"),
            node.get("opening_kind"), config["mounting_system"], config.get("material"),
            config.get("panel_edge"), (node.get("rule_id") if local_rule else preview.get("selected_rule_id")), config["panel_thickness_mm"],
            config["joint_mm"], config["floor_gap_mm"], config["ceiling_gap_mm"], evidence)


def detail_catalog(walls):
    catalog = {}
    for wi, preview in enumerate(walls, 1):
        for ni, node in enumerate(preview["nodes"], 1):
            key = detail_key(preview, node)
            entry = catalog.setdefault(key, {"mark": f"Д{len(catalog)+1:03d}", "preview": preview, "node": node, "locations": []})
            entry["locations"].append(f"М{wi:03d}/{ni}")
    return catalog


def _sketch(sheet, node, config, x, y, w, h, num):
    """A legend-based abstraction: no invented holes, anchors or profile sizes."""
    kind = node["kind"]
    a, b, c = x+12, x+w*.42, x+w*.68
    top, bottom = y+12, y+h-17
    panel_fill = "#efe3cc"
    sheet.rect(x, y, w, h, GRAY, "#fbfcfe", .15)
    if kind in {"vertical_joint", "horizontal_joint"}:
        mid = x+w/2
        sheet.rect(a, top, w-24, 12, GRAY, LIGHT)
        sheet.rect(a, top+37, mid-a-4, 9, INK, panel_fill)
        sheet.rect(mid+4, top+37, x+w-12-mid-4, 9, INK, panel_fill)
        sheet.line(mid, top+13, mid, top+35, BLUE, .7, True)
        sheet.dimension(mid-4, mid+4, top+57, top+47, f"Шов {num(config['joint_mm'])} мм")
        sheet.text(a+2, top+8, "Основание", 2.7, GRAY)
        sheet.text(a, bottom, f"Панели t = {num(config['panel_thickness_mm'])} мм", 3)
    elif kind in {"floor", "ceiling"}:
        sheet.rect(a, top, 16, bottom-top, GRAY, LIGHT)
        sheet.rect(b, top+7, 9, bottom-top-14, INK, panel_fill)
        end = bottom if kind == "floor" else top
        sheet.line(a, end, c+15, end, INK, .6)
        gap = config["floor_gap_mm" if kind == "floor" else "ceiling_gap_mm"]
        sheet.line(b+14, end, c, end+(10 if kind=="ceiling" else -10), BLUE, .2)
        sheet.text(c-5, end+(17 if kind=="ceiling" else -13), f"Зазор {num(gap)} мм", 2.8, BLUE)
        sheet.rect(b-5, end-5, 20, 10, BLUE, weight=.2)
        sheet.text(a, bottom+9, f"Панель t = {num(config['panel_thickness_mm'])} мм", 2.8)
    elif kind in {"left_edge", "right_edge"}:
        edge = node.get("edge_context", {})
        corner = edge.get("corner_type", "unresolved")
        if corner in {"internal_corner", "external_corner"} and edge.get("right_angle") is not True:
            # A right-angle symbol would contradict a measured oblique corner.
            # Keep two unconnected panel symbols; do not invent mitres or
            # hardware geometry even when the reference-line angle is known.
            sheet.rect(a, top+23, w*.29, 8, INK, panel_fill)
            sheet.rect(a+w*.42, top+23, w*.29, 8, INK, panel_fill)
            sheet.circle(x+w/2, top+27, 5, BLUE, weight=.4)
            angle = edge.get("interior_angle_deg")
            sheet.text(a, top+49, f"Угол по опорным линиям: {num(angle)}°" if angle is not None else "Угол не определён", 2.8)
            sheet.text(a, top+56, "Конструкция стыка не показана", 2.8, RED)
            sheet.text(a, bottom-4, "Нужен отдельный узел, не типовой угол 90°", 2.7)
        elif corner not in {"internal_corner", "external_corner"}:
            # A collinear boundary or unresolved free edge is not a 90-degree
            # corner. Never suggest a bent fitting merely to fill the drawing.
            sheet.rect(a, top, w-24, 14, GRAY, LIGHT)
            sheet.rect(a, top+33, w*.4, 8, INK, panel_fill)
            end = a+w*.4
            if corner == "straight":
                sheet.rect(end+8, top+33, w-32-w*.4, 8, INK, panel_fill)
            sheet.circle(end+4, top+37, 5, BLUE, weight=.4)
            sheet.text(a, bottom-4, "Прямое соединение" if corner == "straight" else "Край стены: примыкание уточняется", 2.8)
        elif corner == "external_corner":
            sheet.polygon([(a,top),(a+18,top),(a+18,bottom-18),(c,bottom-18),(c,bottom),(a,bottom)], GRAY, LIGHT)
            sheet.rect(a-10,top+8,7,bottom-top-5,INK,panel_fill)
            sheet.rect(a,bottom+4,c-a,7,INK,panel_fill)
            sheet.circle(a-3,bottom+4,4,BLUE,weight=.4)
        else:
            sheet.polygon([(a,top),(a+18,top),(a+18,bottom-18),(c,bottom-18),(c,bottom),(a,bottom)], GRAY, LIGHT)
            sheet.rect(a+30, top+10, 8, bottom-top-42, INK, panel_fill)
            sheet.rect(a+43, bottom-30, c-a-43, 8, INK, panel_fill)
            sheet.circle(a+38, bottom-30, 6, BLUE, weight=.4)
        if corner in {"internal_corner", "external_corner"} and edge.get("right_angle") is True:
            sheet.text(a+25, top+7, "Внутренний угол" if corner == "internal_corner" else "Наружный угол", 2.8)
    else:
        sheet.rect(a, top, w*.42, 16, GRAY, LIGHT)
        sheet.rect(a, top+30, w*.38, 8, INK, panel_fill)
        sheet.rect(a+w*.38+5, top+30, 8, bottom-top-30, INK, panel_fill)
        framed = node.get("opening_kind") in {"door", "window"}
        if framed:
            sheet.rect(a+w*.38+17, bottom-16, 22, 16, GRAY, LIGHT)
        sheet.circle(a+w*.38+5, top+34, 6, BLUE, weight=.4)
        sheet.text(a+w*.38+15, bottom+8, "Рама / коробка: условно" if framed else "Граница проёма", 2.7, GRAY)
        sheet.text(a, top+52, "Откос", 2.8)
    sheet.text(x+5, y+h-4, "Схема принципа, без масштаба. Синим - место профиля.", 2.5, GRAY)


def add_detail_sheets(catalog, new, width, height, num):
    for entry in catalog.values():
        if entry.get("assembly"):
            from .assemblies import add_assembly_detail
            add_assembly_detail(entry, new, width, height, num)
            continue
        preview, node = entry["preview"], entry["node"]
        config = preview["configuration"]
        sheet = new(f"{entry['mark']} / {node['label']} / схема примыкания", "wall_mount_detail")
        sheet.references.extend([preview["wall"]["id"], preview["wall"]["face_id"]])
        sheet.text(23, 38, "Принципиальная схема примыкания / без масштаба", 3.3, INK, True)
        left = width*.47
        _sketch(sheet, node, config, 23, 47, left-29, min(105,height-164), num)
        x, y, text_width = left+5, 51, width-left-30
        state = node.get("decision_status", node["status"])
        status = {"source_reference":"По схеме производителя", "project_assumption":"Проектная адаптация", "reference_candidate":"Вариант по методичке", "unresolved":"Конструктивное решение не задано"}.get(state,"Проектное решение")
        y = sheet.paragraph(x, y, status, text_width, 3.5, RED if state in {"unresolved","project_assumption"} else BLUE, True)+3
        sources = node.get("source_refs") or ([node["source"]] if node.get("source") else [])
        for source in sources[:3]:
            y = sheet.paragraph(x,y,f"Источник: {source.get('filename',source.get('document_id',''))}, PDF-страница {source.get('page','?')}.",text_width,2.9)+2
        codes = node.get("component_codes") or []
        if codes:
            y=sheet.paragraph(x,y,"Обозначения: "+", ".join(codes),text_width,2.9)+3
        sheet.paragraph(x,y,"Профиль и основание показаны условно. Геометрию деталей, посадки и крепление задают в сборке узла.",text_width,2.9)
        y=164
        locations=entry["locations"]
        shown=", ".join(locations[:12])+(". Остальные привязки - в полной ведомости мест примыканий." if len(locations)>12 else "")
        y=sheet.paragraph(23,y,"Применяется: "+shown,width-48,2.8)+3
        material={"veneer":"шпон", "hpl":"HPL", "painted":"окраска", "unspecified":"не задана"}.get(config.get("material"),"не задана")
        panel_edge=config.get("panel_edge") or "не задана"
        y=sheet.paragraph(23,y,"Назначено в модели: отделка "+material+"; кромка "+panel_edge+"; толщина панели "+num(config["panel_thickness_mm"])+" мм; шов "+num(config["joint_mm"])+" мм.",width-48,2.9)+3
        checks=node.get("required_checks") or ["Проверить основание, зазоры, монтажную последовательность и совместимость деталей перед выпуском для монтажа."]
        for check in checks[:3]:
            if y+12>height-58:break
            lines=wrap(str(check),width-48,2.8)
            if y+len(lines)*4.34>height-58:break
            y=sheet.paragraph(23,y,"Проверить: "+str(check),width-48,2.8,RED)+2
        sheet.text(23,height-57,"Обозначения Дxxx относятся к схемам этого комплекта, а не к артикулам производителя.",2.8,GRAY)


def draw_schematic_view(sheet, entry, box, num):
    """Compact symbolic detail; its title clearly distinguishes it from a cut."""
    x, y, w, h = box
    preview, node = entry["preview"], entry["node"]
    config = preview["configuration"]
    title = wrap(f"Узел {entry['mark']}. {node['label']} / схема", w-8, 3, True)
    for index, line in enumerate(title):
        sheet.text(x+3, y+5+index*4.4, line, 3, INK, True)
    top = y+10+len(title)*4.4
    _sketch(sheet, node, config, x+3, top, w*.59, min(100,h-55), num)
    right, text_width = x+w*.63, w*.37-6
    state = node.get("decision_status", node.get("status"))
    message = "Решение не задано" if state == "unresolved" else "Проектная адаптация" if state == "project_assumption" else "По методичке"
    ty = sheet.paragraph(right, top+5, message, text_width, 2.7, INK, True)+3
    ty = sheet.paragraph(right, ty, "Панель "+num(config["panel_thickness_mm"])+" мм; шов "+num(config["joint_mm"])+" мм; кромка "+(config.get("panel_edge") or "не задана")+".", text_width, 2.5)+3
    codes = node.get("component_codes", [])
    if codes:
        ty = sheet.paragraph(right, ty, ", ".join(codes), text_width, 2.5)+3
    sheet.paragraph(right, ty, "Условная геометрия. Точные размеры деталей - в сборке узла.", text_width, 2.5, GRAY)
    places = entry["locations"]
    text = "Места: "+", ".join(places[:6]) + (f"; ещё {len(places)-6} в реестре ZIP." if len(places)>6 else ".")
    sheet.paragraph(x+3, y+h-12, text, w-8, 2.5, GRAY)
    sheet.references.extend([preview["wall"]["id"],preview["wall"]["face_id"]])
