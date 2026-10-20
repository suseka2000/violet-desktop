"""Exact-source local corner assemblies, never bent substitutes for stock parts.

The primary recipe is an explicitly project-designed external cap.  Native
STEP measurements and contact checks, not the file's article-like name, bind
its geometry.  In particular V_030-Уголок 25х25.stp measures 20 x 20: it is not
accepted as a 25 mm angle.  V-Ang / V-T-Ang / V-Cub are distinct assemblies.
"""
from __future__ import annotations

import math


CALIBRATIONS_EXTRA = {
    "external-angle20": {
        "source_id": "c5359a487a9d53819d96990ce8e2920007610068c631bd187f4e63610fe75537",
        "filename": "V_012-Уголок 20х20_1.stp",
        "component_code": "Уголок 20×20×1 (наружная проектная накладка; не подтверждённый V-Corn-20)",
        "bbox_mm": [[0, -15, -20], [20, 15, 0]],
        "extrusion_axis": "y",
        "wall_mm": 1.0,
        "inner_corner_xz_mm": [1.0, -1.0],
        "section_vertices_xz_mm": [[0, 0], [20, 0], [20, -1], [1, -1], [1, -20], [0, -20]],
    },
    "external-V-Cub": {
        "source_id": "0b0355ebc4c016e2758483e2036a2544d107b7382d7e0a7b371e9507d48ff070",
        "filename": "V_021-V-Cub.stp",
        "component_code": "V-Cub (геометрия STEP; не подтверждённый V-Cub-1 или V-Ang 45)",
        "bbox_mm": [[-19.2194581708795, 0, -22.1640196184485], [19.2194581708795, 30, 18.21945817087955]],
        "extrusion_axis": "y",
        # In the rigid +45-degree section frame X=(x+z)/sqrt(2),
        # Y=(-x+z)/sqrt(2), Z=-y. This is a rotation, not a mirror.
        "outer_tip_xy_mm": -15.67232857055661,
        "tongue_front_xy_mm": -10.67232857055661,
        "tongue_rear_xy_mm": -8.67232857055661,
        "stem_xy_mm": 1.3,
        "tongue_tip_xy_mm": 11.3,
        # Complete rear ears extend beyond the tongue. Measured in the same
        # rigid section frame; the native envelope regression pins this to
        # the supplied STEP rather than treating tongue reach as stock width.
        "section_max_xy_mm": 25.76620484434716,
        "panel_end_xy_mm": 1.8,
        "panel_face_xy_mm": -16.17232857055661,
    },
}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _source(key, assets):
    calibrated = CALIBRATIONS_EXTRA[key]
    match = next((asset for asset in assets if asset.get("source_id") == calibrated["source_id"]), None)
    if match is None:
        return None
    names = sorted({alias.get("filename") for alias in match.get("aliases", []) if isinstance(alias.get("filename"), str) and alias["filename"]})
    if not names and isinstance(match.get("filename"), str) and match["filename"]:
        names = [match["filename"]]
    if not names:
        return None
    return {"source_id": calibrated["source_id"], "filename": calibrated["filename"] if calibrated["filename"] in names else names[0], "component_code": calibrated["component_code"]}


def _configuration_error(preview, node):
    cfg = preview.get("configuration", {})
    if cfg.get("allow_project_assumptions") is not True:
        return "Посадка угла требует явно включённого предварительного проектного режима."
    if not preview.get("selected_rule_id") or preview.get("selection_status") in {"incompatible", "no_compatible_candidate"}:
        return "Для стены не выбрана совместимая сборка; произвольная угловая деталь не назначена."
    t, gap = cfg.get("panel_thickness_mm"), cfg.get("joint_mm")
    if not _number(t) or not _number(gap) or not 0 < t <= 200 or gap < 0:
        return "Некорректные толщина панели или торцевой зазор."
    if node.get("kind") not in {"left_edge", "right_edge"}:
        return "Угловой рецепт применим только к распознанному углу двух стен."
    edge = node.get("edge_context") or {}
    if edge.get("corner_type") != "external_corner":
        return "Этот рецепт предназначен для наружного угла; внутренний угол или свободный торец им не заменяется."
    # A coarse floor tolerance may classify a nearly-right corner as right.
    # Do not silently rotate its panels to exactly 90 degrees in a real solid.
    angle = edge.get("interior_angle_deg")
    if edge.get("right_angle") is not True or (angle is not None and (not _number(angle) or not math.isclose(angle, 270.0, abs_tol=1e-6))):
        return "Для непрямого наружного угла нет проверенного жёсткого STEP-профиля. Уголок 90° не сгибается и угол помещения не округляется."
    for adjacent in edge.get("adjacent_mounting", []):
        other = adjacent.get("configuration")
        default_edge = "Vb" if cfg.get("mounting_system") == "concealed_mount" else "V-0"
        if not isinstance(other, dict) or other.get("mounting_system") != cfg.get("mounting_system") or (other.get("panel_edge") or default_edge) != (cfg.get("panel_edge") or default_edge) or not _number(other.get("panel_thickness_mm")) or not math.isclose(other["panel_thickness_mm"], t, abs_tol=1e-6):
            return "Соседняя стена имеет другую или неизвестную толщину, систему либо кромку панели. Симметричная угловая посадка не подменяет её назначение."
        if "joint_mm" in other and (not _number(other["joint_mm"]) or not math.isclose(other["joint_mm"], gap, abs_tol=1e-6)):
            return "На сторонах угла назначены разные швы. Торцевой зазор нужно согласовать, а не менять автоматически."
        known_materials = {"veneer", "hpl", "painted"}
        if cfg.get("material") in known_materials and other.get("material") in known_materials and cfg["material"] != other["material"]:
            return "На сторонах угла назначены разные материалы. Симметричная посадка требует отдельной проверки обработки и закрепления обеих панелей."
    return None


def _concealed_cub(preview, pinned_assets):
    from .assembly_recipes import _descriptor, _placement
    from .assembly_recipes_concealed import _groove_panel, _references

    cfg = preview["configuration"]
    edge = cfg.get("panel_edge") or "Vb"
    thickness = cfg["panel_thickness_mm"]
    if edge not in {"Vb", "Vc", "Vd"}:
        return {"message": "Реальному наружному V-Cub нужны боковые пазы Vb, Vc или Vd. Гладкие панели Va/V-0 не фрезеруются без выбора кромки."}
    minimum = 9.6 if edge == "Vc" else 9.1
    if not minimum <= thickness <= 13:
        return {"message": f"Посадка этого V-Cub/{edge} проверена для толщины {minimum:g}–13 мм. Нужно сохранить материал за пазом; профиль не растягивается."}
    selected = next((candidate for candidate in preview.get("candidates", []) if candidate.get("rule_id") == preview["selected_rule_id"]), {})
    if not set(selected.get("component_codes", [])).intersection({"VH-2", "VH-3"}):
        return {"message": "Проектная угловая адаптация V-Cub предусмотрена для выбранной системы VH-2/VH-3. Другие скрытые крепления автоматически не заменены."}
    # Groove dimensions are meaningful only with their immutable source;
    # no current library fallback and no unverified guessed milling dimensions.
    refs = _references(preview, "concealed-edges", "profile-19", preview["selected_rule_id"])
    if not {"concealed-edges", "profile-19", preview["selected_rule_id"]}.issubset({ref["rule_id"] for ref in refs}):
        return {"message": "Нет зафиксированных проверенных страниц паза, профиля V-Cub и выбранной скрытой системы. Размеры фрезеровки не подставлены по памяти."}
    source = _source("external-V-Cub", pinned_assets)
    if source is None:
        return {"message": "Нет точного зафиксированного STEP V-Cub с калиброванными двумя угловыми зацепами. V-Cub-1/V-Ang 45 по названию не подставлены."}
    calibration = CALIBRATIONS_EXTRA["external-V-Cub"]
    face, end = calibration["panel_face_xy_mm"], calibration["panel_end_xy_mm"]
    panel_a = _groove_panel("panel-a", thickness, end, edge)
    panel_a["translation_mm"] = [end, face, -15]
    panel_b = _groove_panel("panel-b", thickness, end, edge)
    panel_b.update(rotation=[[0, 1, 0], [1, 0, 0], [0, 0, -1]], translation_mm=[face, end, 15])
    cosine = math.sqrt(0.5)
    rotation = [[cosine, 0, cosine], [-cosine, 0, cosine], [0, -1, 0]]
    depth = 11 if edge == "Vb" else 16
    width = 3.1 if edge == "Vc" else 2.6
    return _descriptor(f"external90-cub-{edge.lower()}-project", f"Наружный угол 90° / V-Cub, пазы {edge}", source,
        _placement(source, rotation, [0, 0, 15]), [panel_a, panel_b], [
            "Это отдельная проектная адаптация конкретного STEP V-Cub для двух панелей VH-2/VH-3. Не утверждается тождество V-Cub-1 из монтажного альбома или замена V-Ang 45.",
            *( ["Кромка не задана: предварительно выбран паз Vb; сохранённая конфигурация стены не изменена."] if cfg.get("panel_edge") is None else [] ),
            f"На обеих панелях реально вычтен паз {edge}: глубина {depth} мм, раскрытие {width:g} мм, 5,5 мм от лицевой плоскости. Раскрытие — проектный размер из диапазона методички, не производственный допуск.",
            "Обе передние поверхности зацепов STEP касаются лицевых стенок пазов. Торцы панелей отстоят от угловых стоек на 0,5 мм; лицевая плоскость каждой панели выступает относительно соответствующей грани угловой коробки на 0,5 мм. Эти посадки заданы проектом, не выданы за размер изготовителя.",
            "Межпанельный шов выбранной стены сохранён для прямых стыков. В самом углу промежуток занимает реальный профиль V-Cub; его размер не заменён обычным межпанельным швом.",
            "В локальном сечении X и Y идут по двум взаимно перпендикулярным плоскостям панелей, Z вдоль угла. Изменён только жёсткий поворот STEP, панели профрезерованы отдельно.",
            *( ["Задняя полка кромки Vc подрезана на участке 5 мм по схеме кромки. Совместимость такой обработки с конкретным материалом требует согласования."] if edge == "Vc" else [] ),
            "Сечение показывает геометрический зацеп, но не доказывает свободный ход, возможность съёма или монтаж двух целых панелей. Последовательность сборки, подоснова, отверстия и крепёж должны быть проверены отдельно.",
            "Толщина и выбранный паз второй панели сверены с известным назначением соседней стены; если назначения нет, они приняты равными первой как проектное допущение.",
        ], refs)


def recipe_for_corner(preview, node, pinned_assets):
    """Pure descriptor or an explicit reason; never changes panel parameters."""
    from .assembly_recipes import _descriptor, _panel, _placement, _references

    reason = _configuration_error(preview, node)
    if reason:
        return {"message": reason}
    cfg = preview["configuration"]
    t, gap = cfg["panel_thickness_mm"], cfg["joint_mm"]
    if cfg.get("mounting_system") == "concealed_mount":
        return _concealed_cub(preview, pinned_assets)
    if cfg.get("mounting_system") != "open_mount" or cfg.get("panel_edge") not in {None, "V-0"} or not 8 <= t <= 13:
        return {"message": "Наружная накладка рассчитана на открытые панели V-0 толщиной 8–13 мм. Скрытый паз и угловой профиль V-Cub/V-Ang требуют отдельной посадки."}
    # Native legs are 20 mm overall, only 19 mm inside. A square butt joint
    # consumes t+gap on the second leg. Keep at least 2 mm geometric overlap;
    # it is NOT an engineering calculation of adhesive/bearing capacity.
    overlap = 19.0 - t - gap
    if overlap < 2.0 - 1e-6:
        return {"message": "У реального уголка 20×20×1 не остаётся проектного нахлёста 2 мм на вторую панель. Шов, толщина и размер STEP автоматически не изменены."}
    source = _source("external-angle20", pinned_assets)
    if source is None:
        return {"message": "Нет точного зафиксированного STEP уголка 20×20×1 с проверенным сечением. Одноимённые модели и файл «25×25» его не заменяют."}
    rotation = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
    panels = [_panel("panel-a", [80, t, 30], [0, 0, -15]), _panel("panel-b", [t, 80, 30], [0, t + gap, -15])]
    return _descriptor("external90-angle20-project", "Наружный угол 90° / накладка 20×20", source,
        _placement(source, rotation, [-1, -1, 0]), panels, [
            "Накладка — отдельная проектная адаптация обычного уголка 20×20×1, не воспроизведение V-T-Ang 90, V-Ang 45 или подтверждённая замена V-Corn-20.",
            "В сечении лицевая сторона панели A лежит на Y=0, панели Б — на X=0; подоснова расположена в положительном квадранте. Снаружи находится накладка толщиной 1 мм; Z идёт вдоль угла.",
            f"Панели имеют прямой торец без скрытого фрезерования. Между торцом панели Б и тыльной плоскостью A оставлено {gap:g} мм: заданный межпанельный шов использован как явное проектное допущение для угла.",
            f"Геометрический контакт накладки: 19 мм по панели A и {overlap:g} мм по панели Б. Порог 2 мм — только проектный геометрический фильтр, не доказательство достаточной площади приклейки или прочности.",
            "Для примыкания принята нулевая толщина слоя под накладкой. Марку и толщину герметика/клея, подготовку поверхности, пожарные требования и фактическую посадку необходимо согласовать; герметик и крепёж не изображены как известные детали.",
            "Толщина второй панели сверена с назначением соседней стены; при отсутствии назначения предварительно принята равной выбранной стене. Материал не означает подтверждённой несущей способности.",
        ], _references(preview, "profile-28"))
