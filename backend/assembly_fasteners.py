"""Exact-source 3D hardware for local, rigid mounting recipes.

The positions below are geometric project seats, not inferred screw spacing or
load ratings. The screw is the supplied solid, including its thread and head.
Only its shaft can engage explicitly named bodies; native validation remains
responsible for the actual bearing contact and every unintended intersection.
"""
from __future__ import annotations

from copy import deepcopy


CALIBRATIONS = {
    "screw": {
        "source_id": "8d824e4668bc79edacc819dad66e1d75c722adfef552119d4bab117934888daa",
        "filename": "V_04-Саморез.stp", "component_code": "Саморез / исходный STEP",
        "bearing_x_mm": 2.45, "tip_x_mm": 27.45, "shaft_radius_mm": 2.11,
        "head_radius_mm": 5.4, "head_height_mm": 2.45,
        "bearing_annulus_outer_radius_mm": 4.8,
        "thread_core_radius_mm": 1.34, "full_thread_end_x_mm": 24.261638761148,
    },
    "frame": {
        "source_id": "3340c9ee6da2f527c920f5e371071fa27e5ece7efa36f5c049f59035bda818cb",
        "filename": "V_06-Профиль_V-WT(ПН_27х28).stp", "component_code": "V-WT / исходный STEP 28×27",
        "web_z_mm": [0., -1.2], "width_mm": 28., "length_mm": 240.,
    },
}
BACKING_MM = 12.
ADHESIVE_MM = 3.
PROFILE_SOURCES = {
    "t1": "752fd5fcb8a498729714677d66e783355d01d4ee5709dffd60c51a45dae05978",
    "l13": "b5e0b8cbcf87504c0491b734eee64f6fee740974f9d840b2e63bdfd1185c36ed",
    "angle": "c5359a487a9d53819d96990ce8e2920007610068c631bd187f4e63610fe75537",
    "vh2": "f033ba4887ca6c17c6ca97cb4db33319bc80efb6f260064b5526cc98a52d0cf4",
    "vh3": "3b8572c34db5deb6624f486e3955d179d723d07f3e9c69d52b83754328919848",
    "lstart": "0537f42d83894e942131d0b60ab4d37958e6a8a8c1d4e6abce7b5e55c96aa196",
    "start": "52450d5463bb789799321a5c8ed42a3b8ee0e9f1ba5ff12cbe4f31c729b9ef9e",
    "sky": "f3b20317ff7ba3ddb417da9b862940d02ae7c2a4bc2881da25ebf994608963a9",
    "cub": "0b0355ebc4c016e2758483e2036a2544d107b7382d7e0a7b371e9507d48ff070",
}


def _plus(a, b, factor=1.):
    return [a[i] + b[i]*factor for i in range(3)]


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _matrix(*columns):
    return [[columns[j][i] for j in range(3)] for i in range(3)]


def _source(key, assets):
    calibrated = CALIBRATIONS[key]
    asset = next((a for a in assets if a.get("source_id") == calibrated["source_id"]), None)
    if asset is None:
        return None
    names = sorted({a["filename"] for a in asset.get("aliases", []) if a.get("filename")})
    names = names or [asset.get("filename") or calibrated["filename"]]
    return {"source_id": calibrated["source_id"], "filename": calibrated["filename"] if calibrated["filename"] in names else names[0],
            "component_code": calibrated["component_code"]}


def _partial(descriptor, message):
    result = deepcopy(descriptor)
    result["fastener_assembly"] = {"status": "unsupported", "message": message}
    if "spec" in result:
        result.setdefault("assumptions", []).append(message)
    return result


def augment_recipe(descriptor, preview, node, pinned_assets):
    """Add real hardware to supported seats, without moving existing solids.

    Missing sources and unmeasured seats keep the original geometry and an
    explicit unsupported status. This function is pure and contains no CAD,
    file-system or live-catalogue access; the worker verifies exact solids.
    """
    if "spec" not in descriptor or descriptor.get("fastener_assembly"):
        return deepcopy(descriptor)
    if preview.get("configuration", {}).get("allow_project_assumptions") is not True:
        return _partial(descriptor, "Для проектных посадок крепежа нужно разрешить проектные допущения; известные панели и профили не изменены.")
    screw, frame = _source("screw", pinned_assets), _source("frame", pinned_assets)
    if screw is None or frame is None:
        return _partial(descriptor, "Нет точных зафиксированных STEP самореза и V-WT для проверяемой 3D-посадки; крепёж не заменён условными телами.")
    recipe = descriptor["recipe_id"].split("/")[0]
    simple = recipe.startswith(("t1-v0-joint", "l13-edge-project", "flush-backing-angle20", "internal90-angle20", "external90-angle20", "concealed-inner90-backing-angle20"))
    hidden = recipe.startswith(("concealed-vh-2-", "concealed-vh-3-", "concealed-lstart-"))
    start = recipe.startswith(("floor-start1-top59", "concealed-floor-start1-top59"))
    sky = recipe.startswith("concealed-sky-top")
    cub = recipe.startswith("external90-cub-")
    flush = recipe.startswith("flush-backing-angle20")
    if not (simple or hidden or start or sky or cub):
        return _partial(descriptor, "У этого STEP-профиля ещё нет измеренной свободной площадки под головку имеющегося самореза. Крепёж не просверливает лицевую панель и не подставляется наугад.")
    family = ("t1" if recipe.startswith("t1-v0") else "l13" if recipe.startswith("l13-") else "vh2" if recipe.startswith("concealed-vh-2-")
              else "vh3" if recipe.startswith("concealed-vh-3-") else "lstart" if hidden else "start" if start else "sky" if sky else "cub" if cub else "angle")
    profile = next((part for part in descriptor["spec"]["placements"] if part["id"] == "profile"), {})
    if profile.get("source_id") != PROFILE_SOURCES[family] or profile.get("component_ids") != [1]:
        return _partial(descriptor, "Калибровка крепежа не соответствует точному исходному профилю; похожее имя или иной состав тел не заменяет проверенный STEP.")
    thickness = preview["configuration"]["panel_thickness_mm"]
    # A perimeter detail belonging to a calibrated hidden wall must keep the
    # wall's unchanged face/backing datums. Legacy standalone/V0 details keep
    # their historical local supports because they have no such wall context.
    perimeter_context = None
    if (start or sky) and preview["configuration"].get("mounting_system") == "concealed_mount" and 9.1 <= thickness <= 12.2:
        from .wall_systems import HIDDEN_RULES
        joints_rule = (preview["configuration"].get("node_rule_ids") or {}).get("joints") or preview.get("selected_rule_id")
        wall_family = HIDDEN_RULES.get(joints_rule)
        if wall_family:
            perimeter_context = {"family":wall_family,"backing_front_mm":15.9,
                                 "backing_mm":10. if wall_family=="vh3" else 12.}
    internal = recipe.startswith(("internal90-angle20", "concealed-inner90"))
    if internal and thickness < 10.8:
        return _partial(descriptor, "Головка имеющегося STEP-самореза диаметром 10,8 мм не помещается на открытой полке внутреннего уголка между панелями. Уголок не объявлен закреплённым одним касанием; нужен другой крепёж или отдельное клеевое решение.")
    if hidden and family != "vh3" and thickness > 12.2:
        return _partial(descriptor, "При этой толщине панели головка имеющегося самореза не помещается за панелью перед полкой скрытого профиля; требуется другой крепёж, панель не утоняется.")
    result = deepcopy(descriptor)
    spec = result["spec"]
    spec["fastenings"] = []
    spec["section"]["offset_mm"] = .01
    connections = []
    original_panels = [part["id"] for part in spec["boxes"] if part.get("role") == "panel"]
    if not recipe.startswith("l13-edge-project") and not flush:
        connections.extend([["profile", panel] for panel in original_panels])

    def box(identifier, label, size, origin, tangent, normal, role="substrate"):
        along = _cross(tangent, normal)
        item = {"id": identifier, "label": label, "role": role, "size_mm": size,
                "rotation": _matrix(tangent, normal, along), "translation_mm": origin}
        spec["boxes"].append(item)
        return identifier

    def place(identifier, source, rotation, translation, role):
        spec["placements"].append({"id": identifier, "label": source["component_code"], "role": role,
            "source_id": source["source_id"], "component_ids": [1], "rotation": rotation, "translation_mm": translation})

    def screw_at(identifier, origin, normal, tangent, seat, targets):
        # Native X is the screw axis; all transforms have determinant +1.
        place(identifier, screw, _matrix(normal, tangent, _cross(normal, tangent)),
              _plus(origin, normal, -2.45), "fastener")
        spec["fastenings"].append({"id": identifier+"-engagement", "fastener_id": identifier,
            "seat_id": seat, "axis_origin_mm": origin, "axis_direction": normal,
            "shaft_radius_mm": 2.11, "head_radius_mm": 5.4, "head_height_mm": 2.45,
            "shaft_length_mm": 25., "min_engagement_mm": 1., "engagement_targets": targets})

    def support(identifier, panel_id, rear, tangent, normal, *, adhesive=True, flange_seat=None, backing_mm=BACKING_MM, backing_width=28., frame_offset=0., adhesive_mm=ADHESIVE_MM):
        # WT's native X spans the backing, Y is extrusion, Z=0 is the web
        # bearing face. Preserve its complete native 240 mm extrusion.
        along = _cross(tangent, normal)
        backing_front = _plus(rear, normal, adhesive_mm if adhesive else 0.)
        base = _plus(_plus(backing_front, tangent, -backing_width/2), along, -15.)
        backing_id = box("backing-"+identifier, f"Подоснова {backing_mm:g} мм / проектный фрагмент", [backing_width, backing_mm, 30.], base, tangent, normal)
        frame_id = "frame-"+identifier
        place(frame_id, frame, _matrix(tangent, along, [-v for v in normal]), _plus(_plus(backing_front, normal, backing_mm), tangent, frame_offset), "frame")
        if adhesive:
            # A real 12×12 head-clearance pocket in a 3 mm project adhesive
            # layer. Four separate strips form the surrounding contact area.
            for index, (x, z, width, length) in enumerate(((-14., -15., 8., 30.), (6., -15., 8., 30.), (-6., -15., 12., 9.), (-6., 6., 12., 9.))):
                origin = _plus(_plus(rear, tangent, x), along, z)
                adhesive_id = box(f"adhesive-{identifier}-{index+1}", f"Клеевой слой {adhesive_mm:g} мм / проектный фрагмент", [width, adhesive_mm, length], origin, tangent, normal, "adhesive")
                connections.extend([[panel_id, adhesive_id], [adhesive_id, backing_id]])
            seat, origin, targets = backing_id, backing_front, [backing_id, frame_id]
        else:
            seat, origin, targets = "profile", flange_seat, ["profile", backing_id, frame_id]
            connections.append(["profile", backing_id])
        screw_id = "screw-"+identifier
        screw_at(screw_id, origin, normal, tangent, seat, targets)
        connections.append([backing_id, frame_id])
        connections.extend([[screw_id, target] for target in targets])

    if hidden:
        # Native back-flange coordinates: rear15.9, front14.7. The corrugated
        # wings were tested with exact screw/head Common, not bounding boxes:
        # VH2 clears at X=-19/+20; Lstart clears at18.6. VH3's two wings never
        # clear this head. Its central front flange atY=5.5 has a genuine seat
        # inside the17..20mm joint instead. A10mm backing keeps the whole WT
        # web before21.81mm (the measured start of this screw's tapered tip).
        positions = [-19., 20.] if family == "vh2" else [0.] if family == "vh3" else [18.6]
        for index, x in enumerate(positions):
            support(str(index+1), None, [x, 15.9, 0.], [1., 0., 0.], [0., 1., 0.], adhesive=False,
                    flange_seat=[x, 5.5 if family == "vh3" else 14.7, 0.], backing_mm=10. if family == "vh3" else BACKING_MM)
    elif start:
        bridge = 15.9-(thickness+1.) if perimeter_context else 0.
        support("1", None, [-bridge, 30., 0.], [0., 1., 0.], [-1., 0., 0.], adhesive=False,
                flange_seat=[1., 30., 0.], backing_mm=perimeter_context["backing_mm"] if perimeter_context else BACKING_MM)
        if perimeter_context:
            connections.remove(["profile","backing-1"])
            for index,(x,z,width,length) in enumerate(((-14.,-15.,8.,30.),(6.,-15.,8.,30.),(-6.,-15.,12.,9.),(-6.,6.,12.,9.))):
                identifier=box(f"adhesive-start-{index+1}",f"Соединение V-Start1 с подосновой {bridge:g} мм / проектный",
                    [width,bridge,length],[0.,30.+x,z],[0.,1.,0.],[-1.,0.,0.],"adhesive")
                connections.extend([["profile",identifier],[identifier,"backing-1"]])
    elif sky:
        support("1", "profile", [20., thickness+1.2, 0.], [1., 0., 0.], [0., 1., 0.],
                backing_mm=perimeter_context["backing_mm"] if perimeter_context else BACKING_MM,
                adhesive_mm=15.9-(thickness+1.2) if perimeter_context else ADHESIVE_MM)
    elif cub:
        panels = {item["id"]: item for item in spec["boxes"]}
        a, b = panels["panel-a"], panels["panel-b"]
        support("a", "panel-a", [a["translation_mm"][0]+60., a["translation_mm"][1]+thickness, 0.], [1., 0., 0.], [0., 1., 0.])
        support("b", "panel-b", [b["translation_mm"][0]+thickness, b["translation_mm"][1]+60., 0.], [0., 1., 0.], [1., 0., 0.])
    else:
        external = recipe.startswith("external90-angle20")
        if internal:
            panels = {item["id"]: item for item in spec["boxes"]}
            support("a", "panel-a", [60., 0., 0.], [1., 0., 0.], [0., -1., 0.])
            support("b", "panel-b", [-thickness, panels["panel-b"]["translation_mm"][1]+60., 0.], [0., 1., 0.], [-1., 0., 0.])
            # An exposed part of the measured angle's horizontal leg lies in
            # the corner void, X=-t..0. The supplied 10.8mm head fits here only
            # for t>=10.8. It directly clamps the actual1mm STEP leg to a third
            # backing/frame, without drilling through either visible panel.
            support("angle", None, [-thickness/2, -1., 0.], [1., 0., 0.], [0., -1., 0.],
                    adhesive=False, flange_seat=[-thickness/2, 0., 0.])
        elif external:
            support("a", "panel-a", [60., thickness, 0.], [1., 0., 0.], [0., 1., 0.])
            support("b", "panel-b", [thickness, thickness+preview["configuration"]["joint_mm"]+60., 0.], [0., 1., 0.], [1., 0., 0.])
        else:
            for index, panel in enumerate(list(spec["boxes"])):
                if panel.get("role") != "panel":
                    continue
                x, y, _ = panel["translation_mm"]
                centre = x+panel["size_mm"][0]/2 if recipe.startswith("t1-v0-joint") else x+66.
                support(str(index+1), panel["id"], [centre, y+thickness, 0.], [1., 0., 0.], [0., 1., 0.])
            if recipe.startswith("l13-edge-project"):
                # L13 has an explicit centring clearance, not contact. This
                # local project adhesive joins the actual rear flange to the
                # panel without moving the stock profile or thickening panel.
                rear_inner = 14.5-(1+(13.2-thickness)/2)
                if rear_inner-thickness > 1e-6:
                    identifier = box("adhesive-profile", "Клеевой слой в посадочном зазоре L13 / проектный", [12., rear_inner-thickness, 30.], [6., thickness, -15.], [1., 0., 0.], [0., 1., 0.], "adhesive")
                    connections.extend([["edge", identifier], [identifier, "profile"]])
            if flush:
                # A distinct bounded project variant: a rigid3mm shift AWAY
                # from the panel makes room for the measured2.45mm head. X=0
                # (the real floor/ceiling edge) and the panel never move.
                profile_part = next(part for part in spec["placements"] if part["id"] == "profile")
                profile_part["translation_mm"][1] += ADHESIVE_MM
                for index, (x, z, width, length) in enumerate(((1., -15., 7., 30.), (8., -15., 12., 9.), (8., 6., 12., 9.))):
                    identifier = box(f"adhesive-angle-{index+1}", "Клеевой слой под тыльным уголком 3 мм / проектный", [width, ADHESIVE_MM, length],
                                     [x, thickness, z], [1., 0., 0.], [0., 1., 0.], "adhesive")
                    connections.extend([["edge", identifier], [identifier, "profile"]])
                support("angle", None, [14., thickness+4., 0.], [1., 0., 0.], [0., 1., 0.],
                        adhesive=False, flange_seat=[14., thickness+3., 0.], backing_width=26., frame_offset=1.)

    result["source_models"].extend([source for source in (screw, frame) if source["source_id"] not in {s["source_id"] for s in result["source_models"]}])
    # Every non-fastener connection is checked as a real positive-area face
    # contact. A named graph alone must never turn a floating profile or glue
    # strip into a connected assembly. Screw pairs have stricter native shaft
    # engagement and bearing checks in the separate fastenings collection.
    screw_ids = {item["fastener_id"] for item in spec["fastenings"]}
    pairs = sorted({tuple(sorted(pair)) for pair in connections if not screw_ids.intersection(pair)})
    spec["contacts"] = [{"id": f"contact-{index+1}", "body_a": a, "body_b": b, "min_area_mm2": .1}
                        for index, (a, b) in enumerate(pairs)]
    result["recipe_id"] = recipe+"-fastened3d/"+("1.2" if perimeter_context else "1.1" if internal or flush else "1.0")
    result["fastener_assembly"] = {"status": "native_validation_required", "fastening_count": len(spec["fastenings"]),
        "connections": connections, "source_calibrations": deepcopy(CALIBRATIONS), "backing_mm": perimeter_context["backing_mm"] if perimeter_context else 10. if family == "vh3" else BACKING_MM,
        "adhesive_mm": 15.9-(thickness+(1.2 if sky else 1.)) if perimeter_context else ADHESIVE_MM if simple or sky or cub else None,
        **({"wall_layer_context":perimeter_context} if perimeter_context else {}),
        "profile_source_id": PROFILE_SOURCES[family],
        "bearing_seats": [{"fastener_id": item["fastener_id"], "seat_id": item["seat_id"], "origin_mm": item["axis_origin_mm"], "direction": item["axis_direction"]} for item in spec["fastenings"]],
        "scope": "local_mounting_fragment_not_building_anchors_or_fastener_pitch"}
    replacements = {
        "слой клея и несущая подоснова в этой сборке не моделируются": "крепление подосновы и клеевые фрагменты добавлены отдельным 3D-рецептом",
        "Пол/подоснова не моделируются": "Строительный пол не моделируется; подоснова добавлена отдельным 3D-рецептом",
        "Задняя подоснова и саморезы не построены: материал, крепёж, места отверстий и нагрузки здесь не подтверждены.": "Задняя подоснова и реальные саморезы добавлены отдельным 3D-рецептом; материал, шаг крепежа и нагрузки не определяются геометрической проверкой.",
        "подоснова, клей и крепёж не моделируются": "подоснова и крепёж добавлены отдельным 3D-рецептом; клей этого плинтуса не назначен",
        "Окно, дверная коробка, основание и крепёж не построены выдуманными телами.": "Окно и дверная коробка в этот местный узел не включены. Проектная подоснова и реальный крепёж добавлены отдельным 3D-рецептом.",
        "Несущая подоснова и крепёж не придуманы; проверить монтажный ход и необходимость плинтуса.": "Проектная подоснова и реальный крепёж добавлены отдельным 3D-рецептом; проверить монтажный ход и необходимость плинтуса.",
    }
    result["assumptions"] = [next((text.replace(old, new) for old, new in replacements.items() if old in text), text)
                             for text in result["assumptions"]]
    if flush:
        result["assumptions"] = [text for text in result["assumptions"] if "Y — от лица в толщину" not in text and "Сам контакт полки с панелью" not in text]
    result["assumptions"].extend([
        "Крепёж является реальным телом STEP, а не добавленным на разрез контуром: опорная плоскость головки X=2,45 мм, длина под головкой 25 мм, исходные размеры не изменены.",
        f"Добавлена проектная подоснова {result['fastener_assembly']['backing_mm']:g} мм и реальный V-WT с сохранённой длиной 240 мм. Это местный образец крепления, не шаг саморезов, не расчёт нагрузок и не узел анкеровки к капитальной стене.",
        "Пересечение резьбы разрешается только с перечисленными телами, в цилиндре стержня после опорной плоскости; головка, панель, соседний профиль и другие саморезы исключений не получают. Геометрия каждого соединения проверяется ядром CAD.",
        *( ["Панели связаны с подосновой проектным клеевым слоем 3 мм; головки крепления подосновы скрыты в отдельных карманах 12×12 мм. Метод крепления не заменён видимыми саморезами сквозь лицевую панель. Слой 3 мм сопоставим со сборкой 1.1, но его перенос на торцы и углы — проектное допущение; марка клея и прочность не назначены."] if simple else [] ),
        *( [f"Саморезы проходят через {'центральную лицевую' if family == 'vh3' else 'опорную'} полку профиля, подоснову и стенку V-WT. Пазы и исходные панели не изменены; отсутствие геометрической коллизии не подтверждает монтажный ход или несущую способность."] if hidden or start else [] ),
        *( [f"Задняя плоскость V-Sky соединена с подосновой проектным клеевым слоем {result['fastener_assembly']['adhesive_mm']:g} мм. Саморезы закрепляют подоснову к V-WT, а не пробивают полку через лицевую панель. Прочность клеевого соединения и монтажный ход не определяются геометрическим расчётом."] if sky else [] ),
        *( ["Положение лица панели, профиля и подосновы согласовано с полной скрытой стеной: лицевая плоскость подосновы на глубине 15,9 мм; толщина и крепёж не заимствованы у открытого монтажа."] if perimeter_context else [] ),
        *( ["Для угла V-Cub принято отдельное несъёмное проектное решение: тыльные участки панелей соединены с подосновой клеевым слоем 3 мм, а подоснова прикреплена саморезами к V-WT. Исходный V-Cub и пазы сохранены; это не утверждение съёмности или заводской комплектации угла VH."] if cub else [] ),
        *( ["У VH-3 головка установлена на центральную лицевую полку в открытом шве 17–20 мм; рифлёные боковые полки не подходят под имеющуюся головку. Подоснова 10 мм выбрана проектом, чтобы полная резьба, а не только острие, прошла стенку V-WT. Полка профиля 1,2 мм, промежуток до подосновы и её толщина учитываются отдельно."] if family == "vh3" else [] ),
        *( ["Внутренний уголок непосредственно закреплён отдельным саморезом через его исходную полку 1 мм в дополнительную подоснову и V-WT. Головка расположена в свободном угловом промежутке при X=-t/2, Y=0 и не проходит сквозь лицевые панели; этот рецепт ограничен толщиной t не менее 10,8 мм. Направление крепления и проектная подоснова не являются подтверждением монтажного хода или несущей способности."] if internal else [] ),
        *( ["Тыльный торцевой уголок жёстко отодвинут от панели на проектные 3 мм клеевого слоя с карманом под головку и непосредственно закреплён через исходную полку 1 мм в подоснову и V-WT. Это отдельный 3D-вариант посадки: панель, исходный STEP и торцевая координата X=0 не изменены, заданный зазор до пола/потолка сохранён. Подоснова под уголком имеет ширину 26 мм, V-WT смещён на 1 мм вдоль неё; оба начинаются при X=1, не пересекая вертикальную полку. Это не анкер в пол или потолок."] if flush else [] ),
    ])
    return result
