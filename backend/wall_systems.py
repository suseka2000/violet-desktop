"""Exact, evidence-bound families available to the whole-wall native builder.

The system key is not inferred from a filename or merely from 'concealed'.
The selected immutable manual rule, its component codes and measured STEP
recipe must agree. Other catalogue families remain explicit unsupported cases.
"""
from __future__ import annotations

from copy import deepcopy
import math

from .assembly_fasteners import CALIBRATIONS as HARDWARE
from .assembly_recipes import CALIBRATIONS as OPEN_PROFILES
from .assembly_recipes_concealed import CALIBRATIONS_EXTRA, DOCUMENT_HASHES


HIDDEN_RULES = {"concealed-2-1": "vh3", "concealed-2-2": "vh3", "concealed-2-3": "vh3",
                "concealed-2-4": "vh2", "concealed-2-5": "vh2", "concealed-2-6": "vh3",
                "concealed-2-7": "vh3"}
FAMILIES = {"V-T1", "V-T2", "V-Base", "VH-1", "VH-2", "VH-3", "VH-4", "V-Spike", "V-F", "V-F2", "V-Z", "V-PC", "V-Clip"}
PERIMETER_VERSION = "measured-hidden-perimeter/1"
SOURCE_MODELS = {
    **{key: {"source_id": value["source_id"], "filename": value["filename"], "component_code": value["component_code"]}
       for key, value in HARDWARE.items()},
    **{key: {"source_id": value["source_id"], "filename": value["filename"], "component_code": value["component_code"]}
       for key, value in (("t1",OPEN_PROFILES["V-T1"]),("vh2",CALIBRATIONS_EXTRA["VH-2"]),
                          ("vh3",CALIBRATIONS_EXTRA["VH-3"]),("lstart",CALIBRATIONS_EXTRA["V-L-start"]),
                          ("sky",CALIBRATIONS_EXTRA["V-Sky"]),("start",OPEN_PROFILES["V-Start-1"]))},
}


def source_models_for_system(key, perimeter=()):
    if key not in {"legacy_v0", "vh2", "vh3"}:
        raise ValueError("Нет точного набора STEP для выбранной системы стены")
    return deepcopy([SOURCE_MODELS[name] for name in ("screw", "frame", "t1") if key == "legacy_v0"] or
                    [SOURCE_MODELS[name] for name in ("screw", "frame", key, "lstart", *sorted(set(perimeter)))])


def wall_system_capabilities():
    """UI/API catalogue independent of plan identity and the live directory.

    'supported' means the bounded recipe exists, not that every configuration
    is valid or that the manufacturer has approved the project adaptation.
    """
    data = [
        ("legacy_v0", "V-T1 / открытая кромка V-0", "supported", "open_mount", ["open-1-1","open-1-3","open-1-4"], ["V-T1"],
         "Панель 8–13 мм, шов 2 мм. Проектный каркас V-WT, подоснова 12 мм и клей 3 мм.", "Сохранённый рецепт открытого поля; плинтусы и прочие изделия собираются отдельно."),
        ("vh2", "VH-2 / скрытые пазы Vb, Vc, Vd", "supported", "concealed_mount", ["concealed-2-4","concealed-2-5"], ["VH-2","V-L-start","V-Sky","V-Start-1"],
         "Шов 3–5 мм. Vb/Vd: 9,1–12,2 мм; Vc: 9,6–12,2 мм. Сплошная подоснова 12 мм.", "Точные профильные сечения, реальные пазы и крепёж; каркас и обрамление являются проектной адаптацией."),
        ("vh3", "VH-3 / скрытые пазы Vb, Vc, Vd", "supported", "concealed_mount", ["concealed-2-1","concealed-2-2","concealed-2-3","concealed-2-6","concealed-2-7"], ["VH-3","V-L-start","V-Sky","V-Start-1"],
         "Шов 16–18 мм. Vb/Vd: 9,1–12,2 мм; Vc: 9,6–12,2 мм. Сплошная подоснова 10 мм.", "Точные профильные сечения, реальные пазы и крепёж; ограничение 12,2 мм задаёт крепёж обрамления V-L-start, а не только стыка VH-3."),
        ("vbase_cap2", "V-Base / V-Cap2", "unsupported", "open_mount", ["open-1-2"], ["V-Base","V-Cap2"],
         "В библиотеке имеются оба STEP, но жёсткая защёлка имеет натяг.", "Реальные профили пересекаются в зоне защёлки. Без модели упругой посадки нельзя выдавать такой стык за проверенную сборку без коллизий."),
        ("vbase_led", "V-Base / V-LED", "unsupported", "open_mount", ["open-1-5"], ["V-Base","V-LED"],
         "Методичка задаёт шов 13 мм и отдельную подсветку.", "Нет измеренной полной посадки профиля, светового элемента и крепежа этой стеновой системы; она не заменяется V-T1."),
        ("vt2", "V-T2", "unsupported", "open_mount", ["open-1-7"], ["V-T2"],
         "В методичке шов 11 мм; у имеющегося STEP ширина лицевой крышки 10 мм.", "Измеренная крышка не перекрывает такой шов и не образует контакт с обеими панелями по схеме V-T1. Другая посадка и путь закрепления не подтверждены; профиль не масштабируется."),
        ("vh1", "VH-1", "unsupported", "open_mount", ["open-1-6"], ["VH-1"],
         "Каталог отмечает размеры сечения как заказные.", "Наличие STEP не определяет совместимую толщину панели, шов и посадку имеющегося самореза. Полная объёмная сборка не калибрована."),
        ("vh4", "VH-4", "unsupported", "concealed_mount", [], ["VH-4"],
         "STEP присутствует среди исходных моделей.", "Нет подтверждённого правила плоского поля, размеров обработки кромки и закрепления этого точного STEP."),
        ("vspike", "V-Spike / кляймеры", "unsupported", "concealed_mount", ["martynova-assembly"], ["V-Spike","V-L13-2","V-Corn-15","V-Start-3"],
         "В инструкции шаг кляймеров 500 мм; это не шаг саморезов каркаса.", "Модель с названием «Кляймер» не установлена как точный V-Spike; отсутствует проверенная посадка зубьев в материал и полного комплекта этой сборки."),
        ("other_hidden", "V-F / V-F2 / V-Z / V-PC / V-Clip", "unsupported", "concealed_mount", [], ["V-F","V-F2","V-Z","V-PC","V-Clip"],
         "Нужен конкретный комплект и измеренные сопряжения.", "Эти крепления не заменяются VH-2/VH-3 по похожему названию; V-F-Clip из библиотеки не считается тождественным любому V-Clip."),
        ("radius_removable", "Радиусные и съёмные сборки", "unsupported", "concealed_mount", ["concealed-2-8-1","concealed-2-8-2","concealed-2-9","concealed-2-10","concealed-2-11","concealed-2-12","concealed-2-13","concealed-2-14","concealed-2-15","concealed-2-16"], ["VH-2","VH-3","V-Flex"],
         "Методички отдельно задают изгибы, подрезки и монтажное перемещение.", "Плоская стеновая сборка не доказывает радиусную форму или возможность снятия панели; эти правила не выбираются автоматически для обычной стены."),
    ]
    return [{"key":key,"label":label,"status":status,"mounting_system":mounting,"rule_ids":rules,
             "component_codes":codes,"conditions":conditions,"reason":reason,
             "source_models":source_models_for_system(key) if status=="supported" else []}
            for key,label,status,mounting,rules,codes,conditions,reason in data]


def profile_footprint(family, inward_sign=1):
    """Measured transverse extent relative to the edge_mm datum."""
    if family == "vh2":
        return -26.36, 27.36
    if family == "vh3":
        return -33.5, 32.5
    if family == "lstart" and inward_sign in {-1, 1}:
        return (-2., 26.) if inward_sign == 1 else (-26., 2.)
    if family in {"sky", "start"} and inward_sign in {-1, 1}:
        low, high = (-8., 39.8) if family == "sky" else (-59., 16.)
        return (low, high) if inward_sign == 1 else (-high, -low)
    raise ValueError("Неизвестное измеренное сечение профиля")


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int,float)) and math.isfinite(value)


def resolve_wall_system(preview):
    """Return a family descriptor, or a human-readable unsupported reason.

Legacy V0 returns only an internal dispatch descriptor. It is deliberately
not inserted into old plans, preserving their serialized identity byte-for-byte.
"""
    cfg = preview.get("effective_configuration", preview.get("configuration", {}))
    selected_id = preview.get("selected_rule_id")
    selected = next((candidate for candidate in preview.get("candidates", []) if candidate.get("rule_id") == selected_id), {})
    codes = set(selected.get("component_codes", []))
    mounting = cfg.get("mounting_system")
    def unsupported(message):
        return {"key":"unsupported", "label":"Система без полной 3D-посадки", "supported":False,
                "mounting_system":mounting, "panel_edge":cfg.get("panel_edge"),
                "selected_rule_id":selected_id, "component_codes":sorted(codes), "reason":message}
    assignments = cfg.get("node_rule_ids") or {}
    perimeter_choices = {}
    if assignments:
        # Whole-wall builders have calibrated joint fields and a fixed edge
        # recipe. A local assignment cannot silently be ignored by that field.
        from .assembly_recipes_groups import node_group
        labels = {"joints":"стыков", "floor":"пола", "ceiling":"потолка",
                  "corners":"углов", "openings":"проёмов"}
        for group, requested in assignments.items():
            members = [node for node in preview.get("nodes", []) if node_group(node) == group]
            if not members:
                if group == "openings" and not preview.get("openings"):
                    continue
                return unsupported("Нет проверенных мест группы «" + labels.get(group, group) + "» для полной сборки стены.")
            if any(node.get("selection_origin") != "group_override" or node.get("requested_rule_id") != requested or
                   node.get("rule_id") != requested or node.get("decision_status") == "unresolved" or
                   node.get("status") == "unresolved" for node in members):
                return unsupported("Отдельный выбор для " + labels.get(group, group) + " не совместим со всеми местами этой группы. Полная стена не заменяет его прежними узлами.")
            if group == "joints":
                rule = next((item for item in preview.get("snapshot", {}).get("rules", []) if item.get("id") == requested), None)
                if rule is None or rule.get("source_status") != "verified":
                    return unsupported("Выбранное правило стыков не найдено в проверенном снимке библиотеки.")
                selected_id = requested
                selected = {"rule_id":requested, "status":"candidate", "component_codes":rule.get("component_codes", [])}
                codes = set(selected["component_codes"])
                if mounting == "open_mount" and (codes & FAMILIES) != {"V-T1"}:
                    return unsupported("Полная стена не имеет точной посадки выбранных стыков: " + ", ".join(sorted(codes & FAMILIES)) + ". Они не заменены V-T1.")
                continue
            group_codes = {code for node in members for code in node.get("component_codes", [])}
            if mounting == "concealed_mount" and group == "ceiling" and group_codes == {"V-Sky"} and cfg.get("ceiling_gap_mm", 0) >= 8:
                perimeter_choices["ceiling"] = "sky"
                continue
            if mounting == "concealed_mount" and group == "floor" and group_codes in ({"V-Start"}, {"V-Start-1"}) and math.isclose(cfg.get("floor_gap_mm", 0), 59., abs_tol=1e-6):
                perimeter_choices["floor"] = "start"
                continue
            unchanged = all(node.get("inherited_rule_id") == requested and
                            bool(node.get("component_codes")) and
                            set(node.get("component_codes", [])).issubset(set(node.get("inherited_component_codes", []))) for node in members)
            if not unchanged:
                return unsupported("Для отдельно выбранных узлов " + labels.get(group, group) +
                                   " ещё нет согласованного рецепта полной стены. Выбор сохранён в локальных узлах и документации; прежний фиксированный периметр не выдан за новую сборку.")
    if mounting == "open_mount":
        # Previously prepared T1 plans retain the old serialization. An
        # explicitly chosen different family must never silently become T1.
        other = (codes & FAMILIES) - {"V-T1"}
        if other or selected_id in {"open-1-2", "open-1-5", "open-1-6", "open-1-7", "martynova-assembly"}:
            return unsupported("Для выбранной системы " + ", ".join(sorted(other or codes)) +
                               " ещё не подтверждена полная STEP-посадка. Она не заменяется V-T1.")
        return {"key":"legacy_v0", "supported":True}
    if mounting != "concealed_mount":
        return unsupported("Неизвестная система монтажа стены.")
    family = HIDDEN_RULES.get(selected_id)
    if not family or (not assignments.get("joints") and preview.get("selection_status") in {"incompatible","no_compatible_candidate","choice_required"}):
        return unsupported("Для полной скрытой стены выберите совместимую плоскую систему VH-2 или VH-3. Радиусные, съёмные и другие специальные системы не заменяются плоской стеной.")
    code = "VH-2" if family == "vh2" else "VH-3"
    snapshot = preview.get("snapshot", {})
    rules = {rule.get("id"):rule for rule in snapshot.get("rules", [])}
    documents = {document.get("id"):document for document in snapshot.get("documents", [])}
    rule = rules.get(selected_id, {})
    if code not in codes or (codes & FAMILIES) != {code} or set(rule.get("component_codes", [])) != codes or "flat_wall" not in rule.get("contexts", []) or selected.get("status") == "incompatible":
        return unsupported("Выбранная сборка, её перечень компонентов и плоская система VH-2/VH-3 не согласованы. Подмена профиля запрещена.")
    required_rules = [selected_id, selected_id+"-joint", "concealed-edges", "concealed-profile-vh2-vh3", "concealed-profile-angstart"]
    refs = []
    for identifier in required_rules:
        item = rules.get(identifier, {})
        source = item.get("source", {})
        document = documents.get(source.get("document_id"), {})
        if item.get("source_status") != "verified" or document.get("source_status") != "verified" or document.get("sha256") != DOCUMENT_HASHES.get(document.get("id")) or source.get("document_id") != "violet-concealed-mount-2025":
            return unsupported("В снимке нет проверенной исходной страницы " + identifier + "; скрытые пазы и крепёж не создаются по предположению.")
        refs.append({"rule_id":identifier, "document_id":document["id"], "filename":document["filename"],
                     "sha256":document["sha256"], "page":source["page"], "scope":item.get("requirement", "")})
    t, gap, edge = cfg.get("panel_thickness_mm"), cfg.get("joint_mm"), cfg.get("panel_edge")
    low, high = (3., 5.) if family == "vh2" else (16.,18.)
    if not _finite(gap) or not low <= gap <= high:
        return unsupported(f"{code} требует шов {low:g}–{high:g} мм; заданный шов не изменён.")
    if edge not in {"Vb", "Vc", "Vd"}:
        return unsupported("Скрытая полная сборка требует выбранный паз Vb, Vc или Vd; гладкая кромка Va не фрезеруется молча.")
    # The VH3 seam alone permits 12.5, but the actual V-L-start perimeter
    # screw head starts at Y=12.25. A complete wall must respect BOTH seats.
    minimum, maximum = (9.6 if edge == "Vc" else 9.1), 12.2
    if not _finite(t) or not minimum <= t <= maximum:
        return unsupported(f"Для {code}/{edge} с имеющимся STEP-саморезом проверен диапазон {minimum:g}–{maximum:g} мм. Вне него головка либо рёбра профиля пересекают панель.")
    if cfg.get("allow_project_assumptions") is not True:
        return unsupported("Для скрытой стены требуется разрешить проектную подоснову, анкеровку и адаптацию V-L-start; подтверждённые размеры панелей не меняются.")
    perimeter = []
    if perimeter_choices.get("ceiling") == "sky" or ("V-Sky" in codes and cfg.get("ceiling_gap_mm", 0) >= 8):
        perimeter.append("sky")
    if perimeter_choices.get("floor") == "start" or ("V-Start" in codes and math.isclose(cfg.get("floor_gap_mm", 0), 59., abs_tol=1e-6)):
        perimeter.append("start")
    if "start" in perimeter and abs(preview.get("wall", {}).get("base_mm", 0)) > 1e-6:
        return unsupported("V-Start1 требует совпадения низа стены с уровнем пола; поднятая стена не заменена плинтусным узлом.")
    return {"key":family, "label":f"Скрытое крепление {code} / {edge}", "supported":True,
            "mounting_system":mounting, "panel_edge":edge, "selected_rule_id":selected_id,
            "component_codes":sorted(codes), "profile_code":code, "source_models":source_models_for_system(family, perimeter),
            **({"perimeter_families": sorted(perimeter), "perimeter_version": PERIMETER_VERSION} if perimeter else {}),
            "source_refs":refs, "project_adaptation":True,
            "backing_front_mm":15.9, "backing_thickness_mm":12. if family == "vh2" else 10.,
            "groove":{"depth_mm":11. if edge == "Vb" else 16., "width_mm":3.1 if edge == "Vc" else 2.6,
                      "face_mm":5.5, "rear_relief_mm":5. if edge == "Vc" else 0.},
            "scope":("Плоское поле, подкреплённые пазы и измеренные профили периметра. V-Sky установлен при потолочном зазоре от 8 мм, V-Start1 — при низе панели 59 мм. Соединение профилей с общей подосновой является проектной адаптацией; подсветка и соседние углы собираются отдельно." if perimeter else
                     "Плоское поле, подкреплённые пазы и обрамление по периметру. Самостоятельная стеновая адаптация; плинтус, подсветка и соседние углы не выдаются за части этой сборки.")}
