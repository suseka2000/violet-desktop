"""Byte-calibrated concealed profiles with real subtractive panel grooves.

Only the local panel sample is generated/machined. STEP profiles retain their
original shape and length. Catalogue comparisons do not approve an assembly,
its loads, fastening, panel material, or mounting/removal sequence.
"""
from __future__ import annotations

import math


CALIBRATIONS_EXTRA = {
    "VH-2": {
        "source_id": "f033ba4887ca6c17c6ca97cb4db33319bc80efb6f260064b5526cc98a52d0cf4",
        "filename": "V_024-VH-2.stp", "component_code": "VH-2",
        "bbox_mm": [[-15, 0, -53.72], [15, 10.4, 0]], "extrusion_axis": "x",
        "stem_z_mm": [-28.36, -26.36], "engagement_y_mm": [8.4, 10.4],
    },
    "VH-3": {
        "source_id": "3b8572c34db5deb6624f486e3955d179d723d07f3e9c69d52b83754328919848",
        "filename": "V_09-Профиль VH-3.stp", "component_code": "VH-3",
        "bbox_mm": [[-33.5, -15, -10.4], [32.5, 15, 0]], "extrusion_axis": "y",
        "outer_stem_x_mm": [-7.5, 7.5], "engagement_z_mm": [-10.4, -8.4],
    },
    "V-Sky": {
        "source_id": "f3b20317ff7ba3ddb417da9b862940d02ae7c2a4bc2881da25ebf994608963a9",
        "filename": "V_023-V-sky.stp", "component_code": "V-Sky",
        "bbox_mm": [[-15, -8, -19], [15, 39.8, 1.2]], "extrusion_axis": "x",
    },
    "V-L-start": {
        "source_id": "0537f42d83894e942131d0b60ab4d37958e6a8a8c1d4e6abce7b5e55c96aa196",
        "filename": "V_010-Виолет V-L-start_1.stp",
        "component_code": "V-L-start (проектная адаптация; не подтверждённый V-Ang-Start)",
        "bbox_mm": [[-28, -50, -10.4], [0, 50, 0]], "extrusion_axis": "y",
        "engagement_z_mm": [-10.4, -8.3824],
    },
}

DOCUMENT_HASHES = {
    "violet-concealed-mount-2025": "e5440cd6ded1738c797e8313be6ba127ba1fb305ceccab92bc8ef0d8433e6339",
    "profiles-220826": "1925d3a8a669b1a171618cbd9a513cf7f7a2ae8f4769465a6cd5988935c177b9",
}


def _unsupported(message):
    return {"message": message}


def _source(key, assets):
    calibration = CALIBRATIONS_EXTRA[key]
    match = next((a for a in assets if a.get("source_id") == calibration["source_id"]), None)
    if not match:
        return None
    aliases = match.get("aliases") or [{"filename": match.get("filename", calibration["filename"])}]
    names = sorted(a["filename"] for a in aliases if a.get("filename"))
    filename = calibration["filename"] if calibration["filename"] in names else names[0] if names else calibration["filename"]
    return {"source_id": calibration["source_id"], "filename": filename, "component_code": calibration["component_code"]}


def _references(preview, *rule_ids):
    snapshot = preview.get("snapshot") or {}
    documents = {d["id"]: d for d in snapshot.get("documents", [])}
    rules = {r["id"]: r for r in snapshot.get("rules", [])}
    result = []
    for rule_id in dict.fromkeys(rule_ids):
        rule = rules.get(rule_id, {})
        source = rule.get("source", {})
        doc = documents.get(source.get("document_id"), {})
        if not doc or doc.get("sha256") != DOCUMENT_HASHES.get(doc["id"]) or rule.get("source_status") != "verified":
            continue
        result.append({"rule_id": rule["id"], "title": rule["title"], "scope": rule.get("scope", ""),
                       "requirement": rule.get("requirement", ""), "reference_kind": "concealed_geometry_reference",
                       "document_id": doc["id"], "filename": doc["filename"], "page": source["page"],
                       "sha256": doc["sha256"], "source_status": "verified", "preview_url": rule.get("preview_url"),
                       "note": "Источник профиля/паза; окончательные посадки и назначение STEP в проектной сборке требуют проверки."})
    return result


def _groove_panel(identifier, thickness, x, edge, *, edge_at_right=False):
    # Groove datum is its front-side surface 5.5 mm from the panel face.
    # Width is an explicit project choice within the transcribed interval.
    from .assembly_recipes import _panel
    depth = 11. if edge == "Vb" else 16.
    width = 3.1 if edge == "Vc" else 2.6
    panel = _panel(identifier, [80, thickness, 30], [x, 0, -15])
    origin_x = 80-depth if edge_at_right else -1.
    panel["cutouts"] = [{"id": "edge-groove", "origin_mm": [origin_x, 5.5, -1], "size_mm": [depth+1, width, 32]}]
    if edge == "Vc":
        # Only the rear strip is removed, not the complete 5 mm panel edge.
        panel["cutouts"].append({"id": "rear-edge-relief", "origin_mm": [75 if edge_at_right else -1, 8.6, -1],
                                 "size_mm": [6, thickness-8.6+1, 32]})
    return panel


def _groove_assumptions(edge, implicit):
    depth = 11 if edge == "Vb" else 16
    width = 3.1 if edge == "Vc" else 2.6
    return [
        *( ["Тип бокового паза не задан: для этой предварительной сборки выбран Vb; конфигурация стены не изменена."] if implicit else [] ),
        f"Паз {edge}: глубина {depth} мм, раскрытие {width:g} мм, лицевая полка 5,5 мм. Раскрытие выбрано проектом в диапазоне методички; это не производственный допуск.",
        "Пазы действительно вычтены из объёма панелей. Профиль опирается на поверхность паза на отметке 5,5 мм, зазор над полкой - проектный.",
        "Толщина панели ограничена геометрией конкретного STEP: оставлено не менее 1 мм заднего материала паза, исключено проникновение в заднюю полку профиля. Это не каталог допустимых панелей.",
        *( ["Задний участок Vc шириной 5 мм удалён согласно рисунку кромки; пригодность подрезки и монтажного движения нужно проверить на всей панели."] if edge == "Vc" else [] ),
    ]


def recipe_for_concealed(preview, node, pinned_assets):
    """One deterministic local recipe or a visible unsupported reason."""
    from .assembly_recipes import _descriptor, _panel, _placement, _source as source_open

    cfg = preview.get("configuration", {})
    if cfg.get("allow_project_assumptions") is not True:
        return _unsupported("Скрытые STEP-посадки доступны только как предварительные проектные сборки с явными допущениями.")
    if cfg.get("mounting_system") != "concealed_mount" or not preview.get("selected_rule_id") or preview.get("selection_status") in {"incompatible", "no_compatible_candidate"}:
        return _unsupported("Для стены не выбрана совместимая скрытая система монтажа.")
    t, gap = cfg.get("panel_thickness_mm"), cfg.get("joint_mm")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (t, gap)) or not 0 < t <= 200 or not 0 <= gap <= 100:
        return _unsupported("Некорректная толщина или шов скрытой панели.")
    selected = next((c for c in preview.get("candidates", []) if c.get("rule_id") == preview["selected_rule_id"]), {})
    assembly_codes = set(selected.get("component_codes", []))
    if selected.get("status") == "incompatible" or not assembly_codes.intersection({"VH-2", "VH-3"}):
        return _unsupported("Эти скрытые рецепты относятся только к совместимой системе VH-2/VH-3. V-F/V-F2/V-Z/V-PC/V-Clip и другие крепления не заменяются похожими деталями.")
    selected_rule = next((r for r in (preview.get("snapshot") or {}).get("rules", []) if r.get("id") == preview["selected_rule_id"]), {})
    if "flat_wall" not in selected_rule.get("contexts", []):
        return _unsupported("Радиусная, съёмная или специальная сборка не заменяется рецептом обычной плоской стены VH-2/VH-3.")
    kind = node.get("kind")
    edge = cfg.get("panel_edge") or "Vb"
    refs = _references(preview, "concealed-edges", "concealed-profile-vh2-vh3", preview["selected_rule_id"])
    if not refs or not any(r["rule_id"] == preview["selected_rule_id"] for r in refs):
        return _unsupported("Нет зафиксированной проверенной страницы выбранной скрытой сборки; живая библиотека не подставляется вместо снимка.")

    if kind in {"left_edge", "right_edge"}:
        context = node.get("edge_context", {})
        if context.get("corner_type") == "external_corner":
            from .assembly_recipes_corners import recipe_for_corner
            return recipe_for_corner(preview, node, pinned_assets)
        if context.get("corner_type") != "internal_corner" or context.get("right_angle") is not True:
            return _unsupported("Для этого непрямого/наружного угла скрытой системы отдельная проверенная посадка здесь не задана.")
        actual_angle = context.get("interior_angle_deg")
        if actual_angle is not None and (isinstance(actual_angle, bool) or not isinstance(actual_angle, (int, float)) or not math.isfinite(actual_angle) or not math.isclose(actual_angle, 90., abs_tol=1e-6)):
            return _unsupported("Фактический внутренний угол отличается от 90 градусов. Признак близости к прямому углу не разрешает сгибать STEP или округлять геометрию стен.")
        if edge not in {"Va", "Vb", "Vc", "Vd"} or not 9.6 <= t <= 13:
            return _unsupported("Задний угловой рецепт скрытой системы ограничен панелями 9,6-13 мм и кромками Va/Vb/Vc/Vd.")
        for adjacent in context.get("adjacent_mounting", []):
            other = adjacent.get("configuration")
            other_t = other.get("panel_thickness_mm") if isinstance(other, dict) else None
            if not isinstance(other_t, (int, float)) or isinstance(other_t, bool) or other.get("mounting_system") != "concealed_mount" or not math.isclose(other_t, t, abs_tol=1e-6) or (other.get("panel_edge") or "Vb") != edge:
                return _unsupported("На соседней стороне угла задана другая толщина/система/кромка. Симметричная проектная посадка их не заменяет.")
            material, other_material = cfg.get("material", "unspecified"), other.get("material", "unspecified")
            if material != "unspecified" and other_material != "unspecified" and material != other_material:
                return _unsupported("На сторонах угла назначены разные материалы. Их обработка и закрепление требуют отдельного согласования; симметричный рецепт их не уравнивает.")
        source = source_open("angle20", pinned_assets)
        if not source:
            return _unsupported("Для внутреннего скрытого угла нет точного STEP заднего уголка 20 x 20 x 1 мм.")
        return _descriptor("concealed-inner90-backing-angle20", "Внутренний угол / задний уголок 20 x 20 (адаптация)", source,
                           _placement(source, [[1, 0, 0], [0, 0, -1], [0, 1, 0]], [-t-1, -1, 0]),
                           [_panel("panel-a", [80, t, 30], [0, 0, -15]),
                            _panel("panel-b", [t, 80, 30], [-t, t+2, -15])], [
                               "Это проектное примыкание на заднем уголке, а не воспроизведение съёмного угла VH-2/VH-3. Способ скрытого закрепления и монтажный ход не подтверждены.",
                               f"Угловой торцевой зазор принят 2 мм как отдельное проектное допущение. Заданный шов плоского поля {gap:g} мм не изменён.",
                               "В месте угла торцы обеих панелей оставлены прямыми, без пазов; остальные кромки поля имеют свои выбранные пазы. Это отдельная проектная обработка угловых торцов.",
                               "Реальный уголок целиком за тыльными гранями панелей. Контакт полок предусмотрен геометрией; крепёж, контактная площадь и несущая способность проверяются отдельно.",
                               "При известном соседнем назначении толщина и кромка сверены; иначе одинаковая толщина соседней панели является проектным допущением."],
                           refs + _references(preview, "profile-28", "concealed-2-8-1-corners" if "VH-2" in assembly_codes else "concealed-2-9"))

    if kind == "vertical_joint":
        if edge not in {"Vb", "Vc", "Vd"}:
            return _unsupported("Стык VH требует бокового паза Vb, Vc или Vd; гладкая кромка Va не прорезается молча.")
        if not any(r["rule_id"] == "concealed-edges" for r in refs):
            return _unsupported("В снимке нет проверенных размеров паза скрытой панели.")
        key = "VH-2" if "VH-2" in assembly_codes else "VH-3" if "VH-3" in assembly_codes else None
        if key is None:
            return _unsupported("Для V-F/V-F2/V-Z/V-PC/V-Clip нет точного подтверждённого STEP-рецепта; VH-2/VH-3 не подменяют эти артикулы.")
        low, high = (3, 5) if key == "VH-2" else (16, 18)
        if not low <= gap <= high:
            return _unsupported(f"Выбранный {key} требует межпанельный шов {low}-{high} мм по своей странице сборки.")
        minimum = 9.6 if edge == "Vc" else 9.1
        # VH-3 rear corrugation reaches into a 13 mm panel even though the
        # straight rear flange clears it. Do not ignore this real CAD clash.
        maximum = 13. if key == "VH-2" else 12.5
        if not minimum <= t <= maximum:
            return _unsupported(f"Для измеренной посадки {key}/{edge} проверен диапазон толщины {minimum:g}-{maximum:g} мм. За ним недостаточен задний материал паза либо не подтверждён зазор до рёбер STEP; VH-3 толщиной 13 мм имеет реальное пересечение.")
        source = _source(key, pinned_assets)
        if not source:
            return _unsupported(f"В зафиксированных источниках нет точного STEP {key} с измеренной посадкой.")
        if key == "VH-2":
            # Native X is extrusion; asymmetric 18 mm engagement cap has
            # native Z=-34.36..-16.36 and central stem Z=-28.36..-26.36.
            rotation, translation = [[0, 0, 1], [0, -1, 0], [1, 0, 0]], [27.36, 15.9, 0]
        else:
            rotation, translation = [[1, 0, 0], [0, 0, 1], [0, -1, 0]], [0, 15.9, 0]
        panels = [_groove_panel("left", t, -80-gap/2, edge, edge_at_right=True),
                  _groove_panel("right", t, gap/2, edge)]
        refs += _references(preview, "profile-13" if key == "VH-2" else "profile-14")
        return _descriptor(f"concealed-{key.lower()}-{edge.lower()}-joint", f"Скрытый стык {key} / пазы {edge}", source,
                           _placement(source, rotation, translation), panels, [
                               *_groove_assumptions(edge, cfg.get("panel_edge") is None),
                               f"Шов {gap:g} мм сохранён. X - поперёк стыка, Y - от лица в толщину, Z - вдоль неизменённого STEP.",
                               "Обе местные кромки имеют выбранный паз как проектную конфигурацию. Сечение не доказывает возможность съёма панели или свободного хода при монтаже.",
                               "Задняя подоснова и саморезы не построены: материал, крепёж, места отверстий и нагрузки здесь не подтверждены."], refs)

    if kind == "ceiling":
        clearance = cfg.get("ceiling_gap_mm", 0)
        if "V-Sky" not in assembly_codes:
            return _unsupported("Выбранная скрытая сборка не содержит V-Sky; потолочный профиль не подставлен автоматически.")
        if not isinstance(clearance, (int, float)) or isinstance(clearance, bool) or not math.isfinite(clearance) or clearance < 8:
            return _unsupported("Реальный V-Sky выступает на 8 мм выше торца панели: требуется заданный потолочный зазор не менее 8 мм. Зазор не изменён.")
        if not 1 <= t <= 17.8:
            return _unsupported("Показанная посадка V-Sky допустима только для панели толщиной 1-17,8 мм; профиль не масштабируется.")
        source = _source("V-Sky", pinned_assets)
        if not source:
            return _unsupported("Нет точного STEP V-Sky с измеренным сечением 47,8 x 20,2 мм.")
        return _descriptor("concealed-sky-top", "Верх скрытой панели / V-Sky", source,
                           _placement(source, [[0, 1, 0], [0, 0, 1], [1, 0, 0]], [0, t, 0]),
                           [_panel("edge", [80, t, 30], [0, 0, -15])], [
                               "X направлен от верхнего торца вниз по панели, Y - от лица в толщину. Тыльная грань касается полки V-Sky; верхний торец оставлен прямым.",
                               "Профиль занимает X от -8 до 39,8 мм; весь заданный потолочный зазор сохранён. Потолок не создан выдуманным телом.",
                               "Крепёж V-Sky, закрепление панели и монтажный ход требуют проверки; отсутствие коллизии не заменяет крепление."],
                           refs + _references(preview, "concealed-profile-sky"))

    if kind in {"opening_jamb_left", "opening_jamb_right", "opening_head", "opening_sill"}:
        if not any(r["rule_id"] == "concealed-edges" for r in refs):
            return _unsupported("В снимке отсутствуют проверенные размеры паза; фрезеровка торца проёма не создаётся по памяти.")
        if edge not in {"Vb", "Vc", "Vd"} or not (9.6 if edge == "Vc" else 9.1) <= t <= 13:
            return _unsupported("Для этого проектного обрамления нужен паз Vb/Vc/Vd и подходящая толщина панели; прямая кромка не изменяется молча.")
        source = _source("V-L-start", pinned_assets)
        if not source:
            return _unsupported("Нет точного STEP V-L-start для проектного обрамления с пазом.")
        return _descriptor(f"concealed-lstart-opening-{edge.lower()}", "Обрамление с пазом / V-L-start (адаптация)", source,
                           _placement(source, [[-1, 0, 0], [0, 0, 1], [0, 1, 0]], [-2, 15.9, 0]),
                           [_groove_panel("edge", t, 0, edge)], [
                               *_groove_assumptions(edge, cfg.get("panel_edge") is None),
                               "V-L-start выбран для обрамления с пазом как отдельная проектная адаптация, не как доказанная замена L/F-профиля из инструкции по проёмам.",
                               "X направлен от края проёма внутрь панели. Реальная торцевая стенка выступает на 2 мм за панель в проём; проверить раму, откос и монтажный ход. Размер проёма не изменён.",
                               "Паз выполнен также на торце проёма как явное проектное допущение. Сечение не доказывает пригодность крепления для окна, двери, огнестойкости или эксплуатации.",
                               "Нативная длина профиля 100 мм сохранена. Окно, дверная коробка, основание и крепёж не построены выдуманными телами."],
                           refs + _references(preview, "concealed-profile-angstart"))

    if kind == "floor":
        wall_base = preview.get("wall", {}).get("base_mm", 0)
        if isinstance(wall_base, bool) or not isinstance(wall_base, (int, float)) or not math.isfinite(wall_base) or not math.isclose(wall_base, 0., abs_tol=1e-6):
            return _unsupported("Низ стены не совпадает с нулём этажа. Нельзя объявить его полом и разместить плинтус без подтверждённой отметки чистого пола.")
        clearance = cfg.get("floor_gap_mm", 0)
        if not isinstance(clearance, (int, float)) or isinstance(clearance, bool) or not math.isfinite(clearance):
            return _unsupported("Некорректный зазор до пола.")
        if "V-Start" in assembly_codes and math.isclose(clearance, 59., abs_tol=1e-6) and 1 <= t <= 14:
            source = source_open("V-Start-1", pinned_assets)
            if source:
                return _descriptor("concealed-floor-start1-top59", "Низ скрытой панели / V-Start-1", source,
                                   _placement(source, [[0, 0, -1], [0, 1, 0], [1, 0, 0]], [0, 0, 0]),
                                   [_panel("lower", [t, 80, 30], [1, 59, -15])], [
                                       "Локальный Y - высота от пола. Реальная полка STEP занимает отметки 58-59 мм, поэтому низ панели сохранён на заданных 59 мм, не на 58 мм.",
                                       "Конкретный V-Start-1 - проектная адаптация общего V-Start. Прямой нижний торец не заменяет боковые пазы выбранной системы.",
                                       "Исходный плинтус длиной 440 мм не обрезан; подоснова, клей и крепёж не моделируются."],
                                   refs + _references(preview, "concealed-profile-start", "profile-1"))
        if "V-Ang-Start" not in assembly_codes:
            return _unsupported("Нижний профиль выбранной скрытой сборки не имеет калиброванного STEP-рецепта.")
        if not any(r["rule_id"] == "concealed-edges" for r in refs):
            return _unsupported("В снимке отсутствуют проверенные размеры паза; нижняя фрезеровка не создаётся по памяти.")
        if clearance < 2:
            return _unsupported("Рецепту нижнего V-L-start нужен зазор до пола не менее 2 мм для реальной торцевой стенки. Текущий зазор не увеличен.")
        if edge not in {"Vb", "Vc", "Vd"}:
            return _unsupported("Посадке нижнего профиля нужен явно выбранный паз; прямой нижний торец Va не фрезеруется молча.")
        if not (9.6 if edge == "Vc" else 9.1) <= t <= 13:
            return _unsupported("Нижняя профильная посадка не подходит к этой толщине панели и выбранному пазу.")
        source = _source("V-L-start", pinned_assets)
        if not source:
            return _unsupported("Нет точного STEP V-L-start; профиль V-Ang-Start не заменён совпадением имени.")
        return _descriptor(f"concealed-lstart-bottom-{edge.lower()}", "Низ панели с пазом / V-L-start (адаптация)", source,
                           _placement(source, [[-1, 0, 0], [0, 0, 1], [0, 1, 0]], [-2, 15.9, 0]),
                           [_groove_panel("lower", t, 0, edge)], [
                               *_groove_assumptions(edge, cfg.get("panel_edge") is None),
                               "V-L-start с реальным сечением 28 x 10,4 мм принят как проектная адаптация V-Ang-Start, не как доказанное тождество артикула.",
                               "Паз дополнительно выполнен на нижнем торце как проектное допущение. X идёт вверх от низа панели; торцевая стенка занимает X от -2 до 0 мм и помещается в заданный зазор.",
                               "Нативная длина 100 мм сохранена. Несущая подоснова и крепёж не придуманы; проверить монтажный ход и необходимость плинтуса."],
                           refs + _references(preview, "concealed-profile-angstart"))

    return _unsupported("Для этого примыкания скрытой системы нет измеренной STEP-посадки. Проём, угловая подрезка или другой профиль не заменяются плоским стыком.")
