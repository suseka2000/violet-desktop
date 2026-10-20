"""Calibrated rigid placements of known STEP solids and parameterized panels.

These are reviewed geometric recipes, not manufacturer-approved mounting
assemblies. Every calibrated frame is bound to exact source bytes. A filename,
bounding box or similar article number cannot substitute an unknown variant.
No source part is scaled, stretched, cut or bent to make it fit.
"""
from __future__ import annotations

import math

IDENTITY=[[1,0,0],[0,1,0],[0,0,1]]
SAMPLE_LENGTH=30.0
PANEL_SAMPLE=80.0
# Native frames measured from actual section edges (not merely bbox guesses).
# x / y / z are original STEP axes. Values and source hashes are test fixtures
# for each supported rigid recipe, never a generic profile-name mapping.
CALIBRATIONS={
    "V-T1":{
        "source_id":"752fd5fcb8a498729714677d66e783355d01d4ee5709dffd60c51a45dae05978",
        "filename":"V_038-Т-профиль-гвоздь_V-T-1.stp",
        "component_code":"V-T1",
        "bbox_mm":[[-3,-15,-7.3],[3,15,0]],"extrusion_axis":"y","seat_z_mm":-1.2,
    },
    "V-L13":{
        "source_id":"b5e0b8cbcf87504c0491b734eee64f6fee740974f9d840b2e63bdfd1185c36ed",
        "filename":"V_018-Профиль VL-13.stp",
        "component_code":"V-L13",
        "bbox_mm":[[0,-15,-55],[15.5,15,0]],"extrusion_axis":"y","clear_x_mm":[1.0,14.2],"seat_z_mm":-1.0,
    },
    "angle20":{
        "source_id":"c5359a487a9d53819d96990ce8e2920007610068c631bd187f4e63610fe75537",
        "filename":"V_012-Уголок 20х20_1.stp",
        "component_code":"Уголок 20×20×1 (геометрия STEP; не подтверждённый V-Corn-20)",
        "bbox_mm":[[0,-15,-20],[20,15,0]],"extrusion_axis":"y","wall_mm":1.0,
    },
    "V-Start-1":{
        "source_id":"52450d5463bb789799321a5c8ed42a3b8ee0e9f1ba5ff12cbe4f31c729b9ef9e",
        "filename":"V+07-Плинтус_V-Start-1.stp",
        "component_code":"V-Start-1",
        "bbox_mm":[[-220,0,-15],[220,75,0]],"extrusion_axis":"x","shelf_top_y_mm":59.0,
    },
}


def _unsupported(message):
    return {"message":message}


def edge_height_mismatch(preview, node):
    """An elevated bottom or a free top is not the room floor/ceiling.

    Empty for legacy/default full-height walls, preserving their recipe IDs.
    Also forms part of detail identity so a valid wall cannot lend its detail
    to an identically configured low partition.
    """
    wall = preview.get("wall") or {}
    if (preview.get("configuration",{}).get("installation_zone")=="exterior"
            and node.get("boundary_basis")=="exterior_cladding_limits"):
        # Explicit exterior terminations reference the cladding boundary, not
        # a fictitious room ceiling / ground build-up on the unowned face.
        return {}
    if node.get("kind") == "floor" and abs(wall.get("base_mm", 0)) > 1e-6:
        return {"wall_base_mm": wall["base_mm"]}
    if node.get("kind") == "ceiling":
        if wall.get("space_height_mm") is not None:
            delta = wall.get("base_mm", 0) + wall["height_mm"] - wall["space_height_mm"]
            if abs(delta) > 1e-6:
                return {"top_minus_space_ceiling_mm": delta}
        elif wall.get("space_height_basis") in {"unknown", "ambiguous_rooms"}:
            return {"space_ceiling_unknown": True, "space_height_basis": wall["space_height_basis"]}
    return {}


def _source(key,assets):
    calibration=CALIBRATIONS[key]
    match=next((a for a in assets if a.get("source_id")==calibration["source_id"]),None)
    if match is None:
        return None
    # Hash establishes the geometry; use a pinned alias, not a current path.
    aliases=match.get("aliases") or [{"filename":match.get("filename",calibration["filename"])}]
    name=next((a["filename"] for a in aliases if a.get("filename")==calibration["filename"]),None)
    name=name or sorted(a["filename"] for a in aliases if a.get("filename"))[0]
    return {"source_id":calibration["source_id"],"filename":name,"component_code":calibration["component_code"]}


def _placement(source,rotation,translation):
    return {"id":"profile","label":source["component_code"],"source_id":source["source_id"],"component_ids":[1],
            "rotation":rotation,"translation_mm":translation}


def _panel(identifier,size,translation):
    label={"left":"левая","right":"правая","panel-a":"A","panel-b":"Б","lower":"нижняя","edge":"кромка"}.get(identifier,identifier)
    return {"id":identifier,"label":"Параметрическая панель "+label,"role":"panel","size_mm":size,
            "rotation":IDENTITY,"translation_mm":translation}


def _references(preview,rule_id):
    """Catalogue comparisons from the pinned snapshot, not a live library.

    These pages support the profile geometry only. They do not certify the
    project's rigid placement or establish a SKU for a generically named STEP.
    A snapshot without this exact source simply has no catalogue comparison.
    """
    snapshot=preview.get("snapshot") or {}
    documents={d["id"]:d for d in snapshot.get("documents",[])}
    rule=next((r for r in snapshot.get("rules",[]) if r.get("id")==rule_id),None)
    if not rule:
        return []
    source=rule.get("source",{})
    document=documents.get(source.get("document_id"),{})
    if document.get("sha256")!="1925d3a8a669b1a171618cbd9a513cf7f7a2ae8f4769465a6cd5988935c177b9":
        return []
    return [{"rule_id":rule["id"],"title":rule["title"],"scope":rule.get("scope",""),
             "requirement":rule.get("requirement",""),"reference_kind":"profile_geometry_comparison",
             "document_id":document["id"],"filename":document["filename"],"page":source["page"],
             "sha256":document["sha256"],"source_status":rule.get("source_status","unverified"),
             "preview_url":rule.get("preview_url"),
             "note":"Сравнительный источник геометрии профиля; не подтверждение марки STEP, посадки или полного монтажного узла."}]


def _descriptor(recipe,label,source,placement,panels,assumptions,source_refs=None):
    return {"recipe_id":recipe+"/1.0","label":label,"source_models":[source],
            "source_refs":source_refs or [],
            "assumptions":[
                "Проектная геометрическая сборка: реальные STEP-детали и параметрические фрагменты панелей. Не утверждённый узел изготовителя.",
                "Посадки, материал панели, крепёж, нагрузки и монтажную последовательность проверяет проектировщик; крепёж и основание не выдуманы.",
                "Длина фрагмента панели вдоль профиля 30 мм; исходный STEP не масштабирован и не обрезан.",
                *assumptions],
            "spec":{"schema_version":"assembled-section-request/1.0","placements":[placement],"boxes":panels,
                    "section":{"axis":"z","offset_mm":0.0},"collision_tolerance_mm3":0.001}}


def _flush_backing_edge(preview, node, pinned_assets):
    """An explicit rear bracket for a flush edge, not a squeezed end cap.

    The measured angle lies completely behind the panel and inside its local
    height interval. This keeps the user's zero allowance genuinely zero; the
    older L13/L-start/Sky recipes must never be moved through a floor/ceiling to
    make the picture look complete. Source geometry remains entirely rigid.
    """
    cfg = preview["configuration"]
    thickness = cfg["panel_thickness_mm"]
    if cfg.get("mounting_system") not in {"open_mount", "concealed_mount"} or not 8 <= thickness <= 13:
        return _unsupported("Тыльный уголок у торца предусмотрен для панелей толщиной 8–13 мм; зазор и панель не изменены.")
    source = _source("angle20", pinned_assets)
    if source is None:
        return _unsupported("При нулевом зазоре торцевой профиль не помещается. Для тыльной проектной посадки нет точного STEP уголка 20×20×1.")
    kind = node["kind"]
    gap = cfg.get("floor_gap_mm" if kind == "floor" else "ceiling_gap_mm", 0)
    return _descriptor("flush-backing-angle20-project", "Прямой торец / тыльный уголок 20×20", source,
        _placement(source, [[1, 0, 0], [0, 0, -1], [0, 1, 0]], [0, thickness, 0]),
        [_panel("edge", [80, thickness, 30], [0, 0, -15])], [
            "Проектное закрепление прямого торца сзади. Это не V-L13, V-L-start, V-Sky или V-Start и не подтверждённая замена заводского узла.",
            "X направлен от торца внутрь панели, Y — от лица в толщину. Уголок целиком находится при X от 0 до 20 мм, Y от t до t+20 мм; полка касается тыльной стороны, прямой торец панели не фрезеруется.",
            f"Заданный зазор {gap:g} мм до {'пола' if kind == 'floor' else 'потолка'} сохранён. Профиль не выступает за торец, не врезается в пол/потолок и не заставляет приподнимать либо укорачивать панель.",
            "Нулевой зазор является настройкой проекта, а не рекомендацией изготовителя. Необходимость деформационного зазора зависит от материала, влажности и условий эксплуатации.",
            "Тыльный уголок требует подосновы и отдельного крепежа. Сам контакт полки с панелью не подтверждает несущую способность или возможность монтажа; посадка учитывается в проектных решениях.",
        ], _references(preview, "profile-28"))


def _legacy_recipe_for(preview,node,pinned_assets):
    """Return one bounded exact-byte recipe or an explicit unsupported reason.

    All generated panels retain the configured thickness. Global floor/ceiling
    allowances are never silently edited to fit a stock part.
    """
    cfg=preview.get("configuration",{})
    if cfg.get("allow_project_assumptions") is not True:
        return _unsupported("Реальные посадки требуют проектных допущений: включите предварительный проектный режим для этой стены.")
    if not preview.get("selected_rule_id") or preview.get("selection_status") in {"incompatible","no_compatible_candidate"}:
        return _unsupported("Для стены не выбрана совместимая сборка; нельзя подставлять произвольные STEP-детали.")
    t=cfg.get("panel_thickness_mm");gap=cfg.get("joint_mm")
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (t,gap)) or not 0<t<=200 or gap<0:
        return _unsupported("Некорректные толщина панели или межпанельный шов.")
    kind=node.get("kind")
    codes=set(node.get("component_codes",[]))
    chosen=next((c for c in preview.get("candidates",[]) if c.get("rule_id")==preview.get("selected_rule_id")),{})
    system=cfg.get("mounting_system")
    # T1 is a cap on a straight V-0 panel joint. Native cap underside z=-1.2;
    # tooth extrema stay inside the 2 mm gap. Native Y becomes assembly Z.
    if kind=="vertical_joint":
        if "V-T1" not in (codes or set(chosen.get("component_codes",[]))):
            if "V-Base" in (codes or set(chosen.get("component_codes",[]))):
                return _unsupported("Посадка реальных V-Base/V-Cap2 имеет натяг защёлок (объёмное пересечение). Без модели упругой посадки эта сборка автоматически не принимается.")
            return _unsupported("Для профиля этого межпанельного стыка ещё нет калиброванной посадки: скрытый паз и другие профили не заменяются V-T1.")
        if system!="open_mount" or cfg.get("panel_edge") not in {None,"V-0"} or not 8<=t<=13 or not math.isclose(gap,2.,abs_tol=1e-6):
            return _unsupported("Рецепт V-T1 рассчитан только на открытую панель V-0 толщиной 8–13 мм со швом 2 мм.")
        source=_source("V-T1",pinned_assets)
        if not source:
            return _unsupported("В зафиксированном снимке отсутствует точный STEP V-T1, для которого измерена посадка.")
        rotation=[[1,0,0],[0,0,-1],[0,1,0]]
        panels=[_panel("left",[80,t,30],[-81,0,-15]),_panel("right",[80,t,30],[1,0,-15])]
        return _descriptor("t1-v0-joint","Стык V-T1 / две панели V-0",source,_placement(source,rotation,[0,-1.2,0]),panels,[
            "X идёт поперёк шва, Y в толщину панели от лицевой поверхности, Z вдоль профиля. Панели занимают X <= -1 и X >= 1 мм.",
            "Низ шляпки V-T1 совмещён с лицевой плоскостью; шляпка выступает на 1,2 мм, шип входит на 6,1 мм. Это реальные размеры данного STEP.",
            "Два участка шириной 80 мм — локальные образцы, не карта раскроя; слой клея и несущая подоснова в этой сборке не моделируются."],_references(preview,"profile-8"))
    # Project adaptation of a measured generic 20x20x1 angle. This is explicitly
    # NOT a substitution assertion for V-T-Ang 90 or V-Corn-20.
    if kind in {"left_edge","right_edge"}:
        edge=node.get("edge_context",{})
        if edge.get("corner_type")!="internal_corner" or edge.get("right_angle") is not True:
            return _unsupported("Для наружного, непрямого или нераспознанного угла точная сборочная посадка ещё не задана.")
        angle=edge.get("interior_angle_deg")
        if angle is not None and (isinstance(angle,bool) or not isinstance(angle,(int,float)) or not math.isfinite(angle) or not math.isclose(angle,90.,abs_tol=1e-6)):
            return _unsupported("Фактический угол не равен 90°. Жёсткий STEP-уголок не сгибается под округлённый угол помещения.")
        if system!="open_mount" or cfg.get("panel_edge") not in {None,"V-0"} or not 8<=t<=13:
            return _unsupported("Угловая проектная адаптация доступна только для открытых панелей V-0 толщиной 8–13 мм; скрытая фрезеровка неизвестна.")
        for adjacent in edge.get("adjacent_mounting",[]):
            other=adjacent.get("configuration")
            if not isinstance(other,dict) or other.get("mounting_system")!=system or other.get("panel_edge") not in {None,"V-0"} or not math.isclose(other.get("panel_thickness_mm",-1),t,abs_tol=1e-6):
                return _unsupported("На соседней стене уже назначены другая толщина, система или кромка панели. Симметричный угловой рецепт их не заменяет; нужна отдельная посадка.")
            if other.get("material") is not None and other.get("material") != cfg.get("material"):
                return _unsupported("Материалы соседних панелей различаются; симметричный угловой рецепт не подменяет отдельное решение.")
            if other.get("joint_mm") is not None and not math.isclose(other["joint_mm"],gap,abs_tol=1e-6):
                return _unsupported("На соседних стенах заданы разные швы; согласуйте угловое примыкание отдельно.")
        if t+gap>=19:
            return _unsupported("При этой толщине и шве уголок 20×20 не имеет контакта с обеими тыльными плоскостями; положение деталей не подгоняется.")
        source=_source("angle20",pinned_assets)
        if not source:
            return _unsupported("Нет точного STEP уголка 20×20×1 с проверенной геометрией. V-Ang/V-T-Ang не подменяются похожим именем.")
        rotation=[[1,0,0],[0,0,-1],[0,1,0]]
        panels=[_panel("panel-a",[80,t,30],[0,0,-15]),_panel("panel-b",[t,80,30],[-t,t+gap,-15])]
        return _descriptor("internal90-angle20-project","Внутренний угол 90° / проектный уголок 20×20",source,_placement(source,rotation,[-t-1,-1,0]),panels,[
            "Это отдельная проектная адаптация обычного уголка 20×20×1, а не воспроизведение V-T-Ang 90 или подтверждённая замена V-Corn-20.",
            "Две панели образуют внутренний угол без фрезеровки; торцевой зазор принят равным заданному межпанельному шву как проектное допущение.",
            "Контакт полок с обеими тыльными гранями предусмотрен рецептом; фактическую площадь контакта, несущую способность и способ закрепления нужно проверить отдельно.",
            "Толщина второй панели сверена с известным назначением соседней стены, а при его отсутствии предварительно принята равной выбранной стене. Проверить обе стороны перед применением."],_references(preview,"profile-28"))
    # A stock sill profile has a 1 mm shelf occupying native y=58..59.
    # The 58 mm catalogue dimension is NOT the top contact plane for the panel.
    if kind=="floor" and math.isclose(cfg.get("floor_gap_mm",0),59.,abs_tol=1e-6) and system=="open_mount" and 0<t<=14:
        source=_source("V-Start-1",pinned_assets)
        if source:
            panels=[_panel("lower",[t,80,30],[1,59,-15])]
            return _descriptor("floor-start1-top59-project","Низ панели / V-Start-1, опора 59 мм",source,
                _placement(source,[[0,0,-1],[0,1,0],[1,0,0]],[0,0,0]),panels,[
                "Локальный Y — высота от пола. Низ STEP расположен на Y=0, полка занимает 58–59 мм, низ панели — строго 59 мм по настройке стены.",
                "Конкретный V-Start-1 выбран как проектная адаптация: общее обозначение V-Start из сборки не доказывает тождество артикула.",
                "Пол/подоснова не моделируются; тыльная плоскость панели касается вертикальной полки. Исходный плинтус имеет длину 440 мм, не масштабирован."],_references(preview,"profile-1"))
    opening_kinds={"opening_jamb_left","opening_jamb_right","opening_head","opening_sill"}
    if kind not in opening_kinds|{"floor","ceiling"}:
        return _unsupported("Для этого места нет калиброванного рецепта реальных STEP-посадок.")
    if system!="open_mount":
        return _unsupported("L13 не заменяет скрытую обработку кромки: для скрытой системы нужны точные пазы панели и посадки.")
    # The limiting lip, not the 15.5 mm outer envelope, defines panel clearance.
    if not 0<t<=13.2:
        return _unsupported("Панель не помещается в фактический просвет V-L13 (13,2 мм); профиль не растягивается.")
    source=_source("V-L13",pinned_assets)
    if not source:
        return _unsupported("Нет точного STEP V-L13, для которого измерены просвет и опорная плоскость.")
    allowance=cfg.get("floor_gap_mm",0) if kind=="floor" else cfg.get("ceiling_gap_mm",0) if kind=="ceiling" else None
    if allowance is not None and allowance<1:
        return _unsupported("Торцевая стенка V-L13 имеет толщину 1 мм. При текущем зазоре профиль заходит в пол/потолок; зазор автоматически не увеличен.")
    left=1+(13.2-t)/2
    rotation=[[0,0,-1],[1,0,0],[0,-1,0]]
    translation=[-1,-left,0]
    panels=[_panel("edge",[80,t,30],[0,0,-15])]
    return _descriptor("l13-edge-project","Кромка панели / V-L13 (проектная адаптация)",source,_placement(source,rotation,translation),panels,[
        "Реальный V-L13 выбран как проектная адаптация обрамления, не как доказанная замена L/F-профиля из другой инструкции.",
        "В местной системе X направлен от торца внутрь панели, Y — в толщину, Z — вдоль профиля. Панель начинается X=0, торцевая стенка профиля занимает от X=-1 до X=0 мм.",
        f"Панель толщиной {t:g} мм центрирована в фактическом просвете 13,2 мм; боковой зазор {(13.2-t)/2:g} мм на сторону — проектное допущение.",
        "Откос/оконная рама/пол/потолок не включены как выдуманные тела; проверить реальный проём и возможность надевания профиля.",
        *( ["Текущий зазор до пола/потолка сохранён; торцевая стенка 1 мм помещается в нём. Высота пола не переносится на рабочую схему автоматически."] if allowance is not None else [] )],_references(preview,"profile-22"))


def _ungrouped_profile_recipe_for(preview, node, pinned_assets):
    """Dispatch calibrated families; changing rooms does not change a local seat.

    All new routes retain the opt-in and selected-system checks of the legacy
    recipes. A horizontal seam is an explicit project orientation of the same
    local cross-section, never a claim about load-bearing or end fixings.
    """
    cfg = preview.get("configuration", {})
    if cfg.get("allow_project_assumptions") is not True:
        return _unsupported("Реальные посадки требуют проектных допущений: включите предварительный проектный режим для этой стены.")
    if not preview.get("selected_rule_id") or preview.get("selection_status") in {"incompatible", "no_compatible_candidate"}:
        return _unsupported("Для стены не выбрана совместимая сборка; нельзя подставлять произвольные STEP-детали.")
    t, gap = cfg.get("panel_thickness_mm"), cfg.get("joint_mm")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (t, gap)) or not 0 < t <= 200 or gap < 0:
        return _unsupported("Некорректные толщина панели или межпанельный шов.")
    if edge_height_mismatch(preview, node):
        return _unsupported("Край этой стены не совпадает с полом/потолком помещения либо отметка потолка неизвестна. Типовой узел к полу или потолку не подменяет свободный торец перегородки.")
    if node.get("kind") in {"left_edge", "right_edge"}:
        edge = node.get("edge_context") or {}
        if edge.get("adjacent_opening_ids"):
            from .assembly_recipes_opening_returns import adjacent_opening_terminal
            return adjacent_opening_terminal(preview, node, pinned_assets)
        local = edge.get("local_panel_junction")
        if local:
            # A T-shaped wall graph has two distinct inside panel corners, not
            # one generic T profile. The geometry resolver chooses the mounted
            # neighbour facing THIS side; never assemble the other quarter or
            # continue a flat seam through the partition's cladding.
            angle = local.get("angle_deg")
            if local.get("kind") != "internal_corner" or local.get("basis") != "mounted_face_normals" or local.get("right_angle") is not True or isinstance(angle, bool) or not isinstance(angle, (int, float)) or not math.isfinite(angle) or not math.isclose(angle, 90., abs_tol=1e-6):
                return _unsupported("Локальная пара панелей Т-примыкания должна образовывать единственный точный угол 90°. Неоднозначная или непрямая ветвь не заменяется типовым уголком.")
            adjacent = edge.get("adjacent_mounting", [])
            if len(adjacent) != 1 or edge.get("adjacent_opening_ids"):
                return _unsupported("Для этой стороны Т-примыкания нужны две актуальные сплошные облицовки; проём или отсутствие монтажа на соседней стороне не подменяется углом.")
            neighbor = adjacent[0]
            other = neighbor.get("configuration")
            if neighbor.get("geometry_signature_verified") is not True or neighbor.get("wall_id") != local.get("adjacent_wall_id") or neighbor.get("face_id") != local.get("adjacent_face_id"):
                return _unsupported("Назначение смежной стороны Т-примыкания не соответствует проверенной геометрии; пересчитайте монтаж этой стороны.")
            default_edge = "Vb" if cfg.get("mounting_system") == "concealed_mount" else "V-0"
            fields = ("panel_thickness_mm", "joint_mm", "mounting_system", "material", "floor_gap_mm", "ceiling_gap_mm")
            if not isinstance(other, dict) or other.get("allow_project_assumptions") is not True or any(other.get(key) != cfg.get(key) for key in fields) or (other.get("panel_edge") or default_edge) != (cfg.get("panel_edge") or default_edge):
                return _unsupported("Стороны Т-примыкания имеют разные настройки или не допускают проектную адаптацию; симметричный угол не подменяет их параметры.")
            if other.get("assembly_rule_id") not in {None, preview.get("selected_rule_id")}:
                return _unsupported("На смежной стороне Т-примыкания выбрана другая сборка; отдельная посадка не подменяется общей.")
            wall = preview.get("wall") or {}
            for key, neighbor_key in (("base_mm", "wall_base_mm"), ("height_mm", "wall_height_mm")):
                a, b = wall.get(key), neighbor.get(neighbor_key)
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (a, b)) or not math.isclose(a, b, abs_tol=1e-6):
                    return _unsupported("Отметки и высоты смежных стен Т-примыкания различаются или неизвестны; единый разрез по всей высоте не назначен.")
            from copy import deepcopy
            oriented = deepcopy(node)
            oriented["edge_context"].pop("local_panel_junction")
            oriented["edge_context"].update(corner_type="internal_corner", right_angle=True, interior_angle_deg=angle)
            result = _profile_recipe_for(preview, oriented, pinned_assets)
            if result.get("spec"):
                result["recipe_id"] = result["recipe_id"].removesuffix("/1.0")+"-t-junction-face/1.0"
                result["label"] = "Т-примыкание / угол выбранной стороны / "+result["label"]
                result["assumptions"].append("Показана только выбранная пара облицовок при Т-примыкании: направления сторон и фактический угол 90° определены геометрией стен, настройки и актуальность соседнего монтажа проверены. Применена та же измеренная посадка заднего уголка, что у внутреннего угла; весь Т-каркас и соединение оснований этой локальной сборкой не моделируются.")
            return result
        # A free *panel* edge uses the same local measured terminal profile as
        # an opening edge. This does not invent a full cap across the thickness
        # of a partition or silently join its separately configured other face.
        free = edge.get("connection") == "free_end" and edge.get("corner_type") in {None,"unresolved"} and not edge.get("adjacent_walls") and not edge.get("adjacent_opening_ids")
        straight = edge.get("corner_type") == "straight"
        if free or straight:
            from copy import deepcopy
            oriented = deepcopy(node)
            if straight:
                angle = edge.get("interior_angle_deg")
                if not isinstance(angle,(int,float)) or isinstance(angle,bool) or not math.isfinite(angle) or not math.isclose(angle,180.,abs_tol=1e-6):
                    return _unsupported("Прямой стык разрешён только при фактическом угле 180°, без округления и изгиба профиля.")
                adjacent = edge.get("adjacent_mounting", [])
                if len(adjacent) != 1 or edge.get("adjacent_opening_ids") or any(item.get("contact_kind") == "wall_interior" for item in edge.get("adjacent_walls", [])):
                    return _unsupported("Прямой стык требует двух известных сплошных облицовок; Т-примыкание или проём не заменяется стыком панелей.")
                neighbor = adjacent[0]
                other = neighbor.get("configuration")
                continuation = edge.get("local_panel_continuation")
                if continuation:
                    if (continuation.get("kind") != "straight" or continuation.get("basis") != "mounted_face_normals"
                            or continuation.get("angle_deg") != 180.
                            or not isinstance(continuation.get("substrate_offset_delta_mm"), (int, float))
                            or not math.isclose(continuation["substrate_offset_delta_mm"], 0., abs_tol=1e-6)
                            or neighbor.get("geometry_signature_verified") is not True
                            or neighbor.get("wall_id") != continuation.get("adjacent_wall_id")
                            or neighbor.get("face_id") != continuation.get("adjacent_face_id")
                            or not isinstance(other, dict) or other.get("allow_project_assumptions") is not True):
                        return _unsupported("Для прямого продолжения нужны две актуальные сонаправленные облицовки в одной плоскости; ступень основания или другая сторона не заменяется плоским стыком.")
                default_edge = "Vb" if cfg.get("mounting_system") == "concealed_mount" else "V-0"
                fields = ("panel_thickness_mm", "joint_mm", "mounting_system", "material", "floor_gap_mm", "ceiling_gap_mm")
                if not isinstance(other,dict) or any(other.get(key) != cfg.get(key) for key in fields) or (other.get("panel_edge") or default_edge) != (cfg.get("panel_edge") or default_edge):
                    return _unsupported("Смежные участки прямой стены имеют разные настройки панели, шва или примыканий; общий стык не подставлен.")
                if other.get("assembly_rule_id") not in {None,preview.get("selected_rule_id")}:
                    return _unsupported("На соседнем участке явно выбрана другая сборка; общий стык требует согласования.")
                wall=preview.get("wall") or {}
                for key,neighbor_key in (("base_mm","wall_base_mm"),("height_mm","wall_height_mm")):
                    a,b=wall.get(key),neighbor.get(neighbor_key)
                    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (a,b)) or not math.isclose(a,b,abs_tol=1e-6):
                        return _unsupported("Для прямого стыка должны совпадать известные отметки и высоты двух стен; ступень высоты не игнорируется.")
                chosen=next((candidate for candidate in preview.get("candidates",[]) if candidate.get("rule_id")==preview.get("selected_rule_id")),{})
                oriented.update(kind="vertical_joint",component_codes=list(chosen.get("component_codes",[])))
            else:
                oriented["kind"]="opening_jamb_left"
            result=_profile_recipe_for(preview,oriented,pinned_assets)
            if result.get("spec"):
                result["recipe_id"]=result["recipe_id"].removesuffix("/1.0")+("-straight-boundary" if straight else "-free-panel-edge")+"/1.0"
                result["label"]=("Прямое соединение участков / " if straight else "Свободная кромка панели / ")+result["label"]
                result["assumptions"].append(
                    "Локальный прямой стык применён на коллинеарной границе двух участков стены: проверены угол 180°, одинаковые отметки, высота, система и параметры панелей. Шов является проектным стыком облицовки, а не деформационным швом здания."
                    if straight else "Измеренная торцевая посадка применена к свободной кромке одной облицовки. Это не коробчатое закрытие всей толщины перегородки, не сопряжение со второй стороной и не узел Т-примыкания; противоположная сторона назначается отдельно.")
            return result
    if node.get("kind") in {"floor", "ceiling"}:
        allowance = cfg.get("floor_gap_mm" if node["kind"] == "floor" else "ceiling_gap_mm", 0)
        if isinstance(allowance, (int, float)) and not isinstance(allowance, bool) and math.isfinite(allowance) and 0 <= allowance < 1:
            return _flush_backing_edge(preview, node, pinned_assets)
    if node.get("kind") == "horizontal_joint":
        from copy import deepcopy
        oriented = deepcopy(node)
        oriented["kind"] = "vertical_joint"
        result = _profile_recipe_for(preview, oriented, pinned_assets)
        if result.get("spec"):
            result["assumptions"].append("Поперечный разрез прямого стыка применён к горизонтальному шву как проектная ориентация. Несущая способность, опирание и крепление нижней/верхней панели отдельно не подтверждены.")
        return result
    if cfg.get("mounting_system") == "concealed_mount":
        from .assembly_recipes_concealed import recipe_for_concealed
        return recipe_for_concealed(preview, node, pinned_assets)
    if node.get("kind") in {"left_edge", "right_edge"} and node.get("edge_context", {}).get("corner_type") == "external_corner":
        from .assembly_recipes_corners import recipe_for_corner
        return recipe_for_corner(preview, node, pinned_assets)
    return _legacy_recipe_for(preview, node, pinned_assets)


def _profile_recipe_for(preview, node, pinned_assets):
    # An explicitly selected ceiling/floor/etc. is a local construction, not
    # permission to reuse the global wall recipe's convenient default profile.
    from .assembly_recipes_groups import group_recipe_for
    if node.get("kind") in {"left_edge", "right_edge"} and node.get("edge_context", {}).get("adjacent_opening_ids"):
        # This height band terminates at a measured opening jamb. Its opening
        # choice must be resolved before an unrelated corner override. The
        # terminal resolver validates the junction and opening before routing
        # to the normal, context-bound opening-group resolver.
        from .assembly_recipes_opening_returns import adjacent_opening_terminal
        return adjacent_opening_terminal(preview, node, pinned_assets)
    if node.get("kind") in {"left_edge", "right_edge"}:
        for neighbor in node.get("edge_context", {}).get("adjacent_mounting", []):
            choice = ((neighbor.get("configuration") or {}).get("node_rule_ids") or {}).get("corners")
            if choice and choice != node.get("rule_id"):
                return _unsupported("На смежной стороне отдельно назначен другой угловой узел. Единая STEP-посадка не подменяет два разных назначения; согласуйте группу углов на обеих сторонах.")
    grouped = group_recipe_for(preview, node, pinned_assets)
    return grouped if grouped is not None else _ungrouped_profile_recipe_for(preview, node, pinned_assets)


def recipe_for(preview, node, pinned_assets):
    """Resolve the unchanged panel/profile seats, then add measured hardware.

    Derived corner/free-edge routes recurse only through the profile resolver:
    augmentation is applied exactly once, after their provenance is complete.
    """
    from .assembly_fasteners import augment_recipe
    from .assembly_recipes_groups import explicit_group_rule
    descriptor = _profile_recipe_for(preview, node, pinned_assets)
    local = preview
    requested = ((preview.get("configuration", {}).get("node_rule_ids") or {}).get("openings")
                 if node.get("edge_context", {}).get("adjacent_opening_ids") else explicit_group_rule(preview, node))
    if descriptor.get("spec") and requested:
        # A labelled group adaptation permits only its own measured hardware;
        # it must not toggle the global assumptions flag or other details.
        local = {**preview, "configuration": {**preview["configuration"], "allow_project_assumptions": True}}
    result=augment_recipe(descriptor, local, node, pinned_assets)
    if (result.get("spec") and preview.get("configuration",{}).get("installation_zone")=="exterior"
            and node.get("kind") in {"floor","ceiling"} and node.get("boundary_basis")=="exterior_cladding_limits"):
        result["recipe_id"]=result["recipe_id"].removesuffix("/1.0")+"-exterior-termination/1.0"
        result["label"]=("Низ" if node["kind"]=="floor" else "Верх")+" наружной облицовки / "+result["label"]
        result["assumptions"].append("Наружное окончание облицовки: зазор отсчитан от заданной нижней/верхней границы стены. Пол, кровля, водоотвод и потолок не подразумеваются и не добавляются как тела. Проверена геометрия существующего профиля; наружная применимость материала и крепления не подтверждена.")
    return result
