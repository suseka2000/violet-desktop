"""Compact mixed-paper album, rendered from the same revision-bound data.

Views are redrawn in paper-space cells, never photographically shrunk. Full
geometry, machining, source records and checks remain in the portable package.
An absent actual cut stays an explicitly labelled schematic, not a fake cut.
"""
from __future__ import annotations

from collections import defaultdict
from math import fsum
import re

from .scene import BLUE, GRAY, INK, LIGHT, RED, Sheet, width as text_width, wrap

A3 = (420, 297)
A2 = (594, 420)
MATERIALS = {"veneer": "Шпон", "hpl": "HPL", "painted": "Окраска", "unspecified": "Не задан"}


def compact_marks(values):
    """Lossless ranges of generator marks; unknown labels are kept verbatim."""
    groups, other = {}, []
    for value in dict.fromkeys(str(value) for value in values):
        match = re.fullmatch(r"([^\d]+)(\d+)", value)
        if match:
            prefix, digits = match.groups()
            groups.setdefault((prefix, len(digits)), set()).add(int(digits))
        else:
            other.append(value)
    result = []
    for (prefix, digits), numbers in groups.items():
        ordered = sorted(numbers)
        first = last = ordered[0]
        for number in [*ordered[1:], None]:
            if number == last + 1:
                last = number
                continue
            result.append(f"{prefix}{first:0{digits}d}" + (f"-{prefix}{last:0{digits}d}" if last != first else ""))
            first = last = number
    return ", ".join([*result, *other])


def _node_register_rows(catalog):
    """Keep every junction location in bounded, self-identifying table rows.

    A reused detail can have thousands of locations. Joining them into one cell
    exceeds the document field limit before table pagination can run. Generated
    location identifiers are indivisible; repeat the detail identity in each
    continuation row instead of truncating references or shrinking paper text.
    """
    rows = []
    for entry in catalog.values():
        prefix = [entry["mark"], entry["node"]["label"],
                  "Разрез сборки" if entry.get("assembly") else "Схема"]
        chunk, length = [], 0
        for location in entry["locations"]:
            value = str(location)
            extra = len(value) + (2 if chunk else 0)
            if chunk and length + extra > 1200:
                rows.append([*prefix, ", ".join(chunk)])
                chunk, length, extra = [], 0, len(value)
            chunk.append(value)
            length += extra
        if chunk or not entry["locations"]:
            rows.append([*prefix, ", ".join(chunk)])
    return rows


def _cover(project):
    sheet = Sheet("Обложка", "cover", *A3)
    sheet.text(39, 38, "VIOLET", 6.2, INK, True)
    sheet.text(39, 49, "Стеновые панели / проект отделки", 3.4, GRAY)
    y = sheet.paragraph(39, 84, project.metadata.name, 342, 7.2, INK, True, leading=10.8)
    if project.metadata.address:
        y = sheet.paragraph(39, y + 9, project.metadata.address, 342, 3.6, leading=5.7)
    sheet.line(39, max(131, y + 9), 380, max(131, y + 9), BLUE, .45)
    y = max(151, y + 27)
    if y + 23 > 205:
        raise ValueError("Название и адрес не помещаются на обложке A3. Сократите текст или перенесите подробности в описание проекта.")
    zones = {binding.get("configuration", {}).get("installation_zone", "interior")
             for binding in project.properties.get("wall_mounting", {}).get("walls", [])}
    subject = ("НАРУЖНАЯ ОБЛИЦОВКА СТЕН" if zones == {"exterior"} else
               "ВНУТРЕННЯЯ И НАРУЖНАЯ ОТДЕЛКА" if "exterior" in zones else "ВНУТРЕННЯЯ ОТДЕЛКА СТЕН")
    sheet.text(39, y, subject, 5.1, INK, True)
    sheet.text(39, y + 11, "Планы, развёртки, узлы и ведомости", 4.1)
    sheet.text(39, y + 23, f"Стадия {project.metadata.stage}", 3.6)
    y = 216
    for label, value in (("Заказчик", project.metadata.customer), ("Разработал", project.metadata.author)):
        if value:
            y = sheet.paragraph(39, y, label + ": " + value, 342, 3.1) + 7
    if y > 258:
        raise ValueError("Реквизиты заказчика и автора не помещаются на обложке A3. Перенесите подробности в описание проекта.")
    sheet.text(39, 266, project.updated_at.date().isoformat(), 3.1)
    sheet.text(380, 266, f"Версия модели {project.revision}", 3.1, anchor="end")
    return sheet


def _general(project, report):
    sheet = Sheet("Общие данные и условные обозначения", "general", *A3)
    y = 40
    notes = [
        "Альбом содержит планы помещений, раскладки облицовки и узлы примыканий. Все виды относятся к одной версии модели.",
        "Линейные размеры - в миллиметрах; высотные отметки - в метрах. На монтажных видах Мxxx отметки даны от пола этажа; на видах Рxx указаны абсолютные отметки проекта. Масштаб подписан у каждого вида. Печать 100%, без подгонки.",
        "У.ч.п. - уровень чистого пола, ур. чист. пот. - уровень чистого потолка. При отсутствии явных уровней используются ноль этажа и высота помещения с пометкой «по модели», не результаты обмера.",
        "Геометрия модели и площади помещений заданы по опорным линиям стен. Размеры плана привязаны к изображённым граням стен, без отделки. Чистовые привязки определяются размерами узлов и конструкцией основания.",
        "Узлы с обозначением «Схема» показывают принцип примыкания. Разрезы сборок содержат сохранённую геометрию тел. Происхождение - исходный STEP или проектное параметрическое тело - указано в составе.",
        "Конкретные вопросы по основанию, креплению, посадкам и конструкции проёмов собраны в разделе замечаний. Полные условия и источники сохранены в приложенных данных проекта.",
    ]
    for number, note in enumerate(notes, 1):
        y = sheet.paragraph(25, y, f"{number}. {note}", 369, 3.1, leading=4.8) + 4
    y += 3
    sheet.text(25, y, "Условные обозначения", 3.8, INK, True)
    for label, description in [
        ("М001", "Монтажная развёртка стены; та же марка дана на плане и в ведомости."),
        ("Д001 / СУ001", "Узел примыкания / специальное изделие. А, Б, В - сечения; .1 - увеличенный фрагмент."),
        ("ДВ-01 / ОК-01", "Дверь / окно; ПР - проход, ЛК - люк, ПО - иной проём."),
        ("Синие линии", "Раскладка панелей, размеры и монтажные привязки."),
        ("Чёрные линии", "Архитектурная основа, контуры деталей и проёмов."),
    ]:
        y += 8
        sheet.text(25, y, label, 2.9, BLUE, True)
        sheet.paragraph(72, y, description, 322, 2.9, leading=4.3)
    if y > 218:
        raise ValueError("Общие данные альбома не помещаются на A3")
    return sheet


def _table(sheets, title, headers, rows, proportions, kind, *, reuse=True, paper=A3):
    from .album_tables import table
    return table(sheets, title, headers, rows, proportions, kind, reuse=reuse, paper=paper)


def _bounds_ok(sheet, box):
    x, y, w, h = box
    for primitive in sheet.primitives:
        kind = primitive["type"]
        if kind == "text":
            length = text_width(primitive["value"], primitive["size"], primitive["bold"])
            left = primitive["x"] - (length / 2 if primitive["anchor"] == "middle" else length if primitive["anchor"] == "end" else 0)
            bounds = (left, primitive["y"] - primitive["size"], left + length, primitive["y"] + .5)
        elif kind == "rect":
            bounds = (primitive["x"], primitive["y"], primitive["x"] + primitive["w"], primitive["y"] + primitive["h"])
        elif kind == "circle":
            bounds = (primitive["x"] - primitive["r"], primitive["y"] - primitive["r"], primitive["x"] + primitive["r"], primitive["y"] + primitive["r"])
        else:
            points = ([p for ring in primitive["rings"] for p in ring] if kind == "region" else
                      primitive.get("points") if "points" in primitive else [(primitive["x1"], primitive["y1"]), (primitive["x2"], primitive["y2"])])
            if not points:
                continue
            bounds = (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))
        if bounds[0] < x - 1 or bounds[1] < y - 1 or bounds[2] > x + w + 1 or bounds[3] > y + h + 1:
            return False
    return True


def _grid(sheets, items, title, kind, draw, *, wide=False):
    """Four cells per A2, with larger-cell fallback for long legends/titles."""
    boxes = [(23, 34, 269, 154), (302, 34, 269, 154), (23, 198, 269, 154), (302, 198, 269, 154)]
    if wide:
        boxes = [(23, 34, 548, 154), (23, 198, 548, 154)]
    page, position = None, 0
    for item in items:
        trial = Sheet(title, kind, *A2)
        failure = None
        try:
            draw(trial, item, boxes[position])
        except ValueError as error:
            if "помещается" not in str(error):
                raise
            failure = error
        if failure is None and _bounds_ok(trial, boxes[position]):
            if page is None:
                page = Sheet(title, kind, *A2)
                sheets.append(page)
            page.primitives.extend(trial.primitives)
            page.references.extend(trial.references)
            page.rect(*boxes[position], GRAY, weight=.1)
            position += 1
            if position == len(boxes):
                page, position = None, 0
            continue
        # Preserve already rendered cells and redraw the difficult view at a
        # larger physical size. Never shrink dimensions/fonts to force a fit.
        page, position = None, 0
        full = Sheet(title + " / увеличенный вид", kind, *A2)
        full_box = (23, 34, 548, 318)
        draw(full, item, full_box)
        if not _bounds_ok(full, full_box):
            raise ValueError("Вид не помещается даже на отдельном A2. Сократите длинные подписи или разделите состав узла.")
        sheets.append(full)


def _names(project):
    names = {}
    for building in project.buildings:
        names[building.id] = building.name
        for floor in building.floors:
            names[floor.id] = floor.name
            for space in floor.spaces:
                names[space.id] = space.code + " / " + space.name
            for wall in floor.walls:
                names[wall.id] = wall.name
            for opening in floor.openings:
                names[opening.id] = opening.name
    return names


def _mount_packets(sheets, views, catalog, project, marks, num, title, specials=()):
    """Draw each view/detail once, with scale-preserving adaptive packing."""
    from .album_graphics import mount_packets
    mount_packets(sheets, views, catalog, project, marks, num, title, specials)


def compose_album(original_sheets, project, report, whole_walls=(), stale_whole_walls=(), *, derived_assemblies=False, assembly_records=None):
    from . import MAX_SHEETS, num
    from .assemblies import draw_assembly_view
    from .mount_details import draw_schematic_view
    from .openings import opening_marks, opening_schedule
    from .wall_mount import build_mount_catalog, draw_mount_view

    sheets = [_cover(project), _general(project, report), Sheet("Содержание комплекта", "contents", *A3)]
    def table(title, headers, rows, proportions, kind):
        _table(sheets, title, headers, rows, proportions, kind, paper=A2)
    names = _names(project)
    mounting = report.get("wall_mounting") or {}
    walls = mounting.get("walls", [])
    catalog = build_mount_catalog(project, mounting, assembly_records=assembly_records, server_owned=derived_assemblies)
    from .component_legend import component_legend_sheets
    sheets.extend(component_legend_sheets(catalog, num))
    marks = opening_marks(project)
    from .special_sections import special_entries,context_view,add_special_tables
    specials=special_entries(project,derived_assemblies=derived_assemblies)
    sheets.extend(sheet for sheet in original_sheets if sheet.kind == "plan")
    # Keep a legacy elevation if any of its faces has no replacement montage,
    # or if its explicit zones carry additional geometry not in wall mounting.
    mounted = {(view["wall"]["id"], view["wall"]["face_id"]) for view in walls}
    needed_elevations = {view["id"] for view in report.get("elevations", [])
                         if view.get("zones") or (view["wall_id"], view.get("wall_face_id", view.get("face_id"))) not in mounted}
    sheets.extend(sheet for sheet in original_sheets if sheet.kind == "elevations" and any(value in needed_elevations for value in sheet.references))

    groups = defaultdict(list)
    by_face = {(record.wall_id, record.face_id): record for record in whole_walls}
    # A freestanding partition can belong to the room geometrically without
    # rewriting canonical face ownership. Ambiguity stays unassigned.
    contexts={}
    from ..wall_mount import mount_scope_targets
    for building in project.buildings:
        for floor in building.floors:
            try:resolved=mount_scope_targets(project,{"kind":"floor","id":floor.id})
            except ValueError:continue
            for target in resolved["targets"]:
                if target["space"]:contexts[(target["wall"].id,target["face"].id)]=target["space"].id
    for index, view in enumerate(walls, 1):
        wall = view["wall"]
        space_id=wall.get("space_id") or contexts.get((wall["id"],wall["face_id"]))
        # This copy is document context only, never a model or binding mutation.
        contextual={**view,"wall":{**wall,"space_id":space_id}, "document_whole_wall": by_face.get((wall["id"],wall["face_id"]))}
        zone = wall.get("installation_zone", view["configuration"].get("installation_zone", "interior"))
        groups[(wall["floor_id"],space_id if zone != "exterior" else "exterior")].append((f"М{index:03d}",contextual))
    extra_keys=set()
    for entry in specials:
        key=(entry["wall_id"],entry["face_id"])
        if entry["status"]!="ready" or key in mounted or key in extra_keys:continue
        extra_keys.add(key)
        view=context_view(project,entry,specials)
        wall=view["wall"]
        space_id=wall.get("space_id") or contexts.get(key)
        wall["space_id"]=space_id
        groups[(wall["floor_id"],space_id)].append((f"ВИ{len(extra_keys):03d}",view))
    for (floor_id, space_id), views in groups.items():
        title = ("Наружная облицовка / " + names.get(floor_id, "Этаж") if space_id == "exterior" else
                 "Развёртки / " + names.get(space_id, names.get(floor_id, "Монтаж стен")))
        first = len(sheets)
        _mount_packets(sheets,views,catalog,project,marks,num,title,specials)
        from .mount_plans import add_mount_plan
        add_mount_plan(sheets, first, views, project, marks, title)

    actual = [entry for entry in catalog.values() if entry.get("assembly")]
    from .whole_wall import add_whole_wall_tables
    wall_marks = {(view["wall"]["id"],view["wall"]["face_id"]):f"М{index:03d}" for index,view in enumerate(walls,1)}
    add_whole_wall_tables(whole_walls, table, num, wall_marks)
    if stale_whole_walls:
        table("Сборки стен, требующие пересчёта", ["Стена", "Причина"],
              [[item["label"], item["message"]] for item in stale_whole_walls], [2,8], "whole_wall_stale")
    schematic = [entry for entry in catalog.values() if not entry.get("assembly")]
    # Standalone supplied sections and custom library drawings remain intact;
    # they are not silently converted into assembled mounting details.
    sheets.extend(sheet for sheet in original_sheets if sheet.kind in {"cad_section", "node_candidate"})

    summary = []
    for index, view in enumerate(walls, 1):
        wall, cfg, count = view["wall"], view["configuration"], view["summary"]
        summary.append([f"М{index:03d}", wall["name"], MATERIALS.get(cfg["material"], cfg["material"]) + f" / {num(cfg['panel_thickness_mm'])}",
                        num(wall["length_mm"]) + " x " + num(wall["height_mm"]), num(cfg["joint_mm"]),
                        str(count["panel_count"]), str(count["piece_count"]), num(count["area_m2"])])
    if summary:
        summary.append(["ИТОГО", "По назначенным сторонам", "", "", "", str(sum(v["summary"]["panel_count"] for v in walls)),
                        str(sum(v["summary"]["piece_count"] for v in walls)), num(fsum(v["summary"]["area_m2"] for v in walls))])
    table("Ведомость облицовки стен", ["Вид", "Стена", "Отделка / t, мм", "Длина x высота, мм", "Шов, мм", "Панелей", "Частей", "Нетто, м²"],
           summary, [1, 1.5, 2.1, 2.2, 1, 1, 1, 1.3], "wall_mount_quantities")
    table("Ведомость дверей, окон и проёмов", ["Марка", "Этаж / помещение", "Вид / наименование", "Ш x В, мм", "Абс. низ, мм", "Абс. верх, мм", "Примечание"],
           opening_schedule(project, num), [1, 1.8, 1.6, 1.3, 1, 1, 3.1], "openings")
    from .mount_setting_out import setting_out_rows, panel_piece_rows
    table("Монтажные привязки / размеры в миллиметрах", ["Вид", "Ось / элемент", "Начало X / низ", "Конец X / верх", "Цепочка / отметки", "Привязка"],
          setting_out_rows(walls, marks, num), [1, 1.4, 1.4, 1.4, 3, 3.8], "mount_setting_out")
    table("Размеры частей панелей / без технологических припусков", ["Материал", "t, мм", "Ш, мм", "В, мм", "Частей", "Виды / номера панелей", "Примечание"],
          panel_piece_rows(walls, num), [1.3, .7, .9, .9, .9, 5, 2.3], "mount_panel_sizes")
    add_special_tables(specials,table,num)

    # Group identical body machining by dimensions, not only by a shared name;
    # different thicknesses/cutouts never inherit each other's fabrication note.
    machining = {}
    for entry in actual:
        record = entry["assembly"]
        for body in record.bodies:
            for cut in body.get("cutouts", []):
                origin = [max(0., value) for value in cut["origin_mm"]]
                removed = [max(0., min(body["size_mm"][axis], cut["origin_mm"][axis] + cut["size_mm"][axis]) - origin[axis]) for axis in range(3)]
                key = (str(body.get("label", body["id"])), num(record.configuration["panel_thickness_mm"]),
                       cut["id"], tuple(origin), tuple(removed))
                machining.setdefault(key, []).append(entry["mark"])
    table("Пазы и выборки панелей / местные координаты образца", ["Узлы", "Панель / t, мм", "Вырез", "Начало X / Y / Z, мм", "Габарит в теле X / Y / Z, мм"],
           [[compact_marks(values), key[0] + " / " + key[1], key[2], " / ".join(map(num, key[3])), " / ".join(map(num, key[4]))]
            for key, values in machining.items()], [2, 2.3, 2, 3, 3], "assembly_machining")

    table("Реестр узлов и мест применения", ["Узел", "Примыкание", "Изображение", "Места на развёртках"],
           _node_register_rows(catalog), [1, 2.8, 1.8, 6], "wall_mount_nodes")

    checks = {}
    entity_marks = defaultdict(list)
    for index, view in enumerate(walls, 1):
        for identifier in (view["wall"]["id"], view["wall"]["face_id"]):
            entity_marks[identifier].append(f"М{index:03d}")
    for issue in report.get("issues", []):
        locations = [location for identifier in issue.get("entity_ids", [])
                     for location in entity_marks.get(identifier, [names.get(identifier, identifier)])]
        key = (issue["code"], issue.get("severity", "warning"), issue["message"])
        checks.setdefault(key, []).extend(locations or ["Комплект"])
    table("Замечания и недостающие исходные данные", ["Код", "Относится к", "Содержание"],
           [[key[0], compact_marks(locations), key[2]] for key, locations in checks.items()], [2.6, 2, 7.4], "issues")

    required = {}
    for index, view in enumerate(walls, 1):
        for assumption in view.get("assumptions", []):
            required.setdefault(str(assumption.get("required_check", assumption.get("message", ""))), []).append(f"М{index:03d}")
    for entry in schematic:
        for check in entry["node"].get("required_checks", []):
            required.setdefault(str(check), []).append(entry["mark"])
    table("Условия применения и проверки узлов", ["Виды / узлы", "Проверить"],
           [[compact_marks(marks), text] for text, marks in required.items() if text], [2.1, 9.9], "mount_checks")

    # One source/recipe row, not dozens of repeated SHA paragraphs per node.
    recipes = {}
    for entry in actual:
        record = entry["assembly"]
        recipes.setdefault((record.recipe_id, record.label, tuple(source.filename for source in record.source_models), bool(record.spec.get("fastenings"))), []).append(entry["mark"])
    table("Примечания к реальным узлам / исходные профили", ["Узлы", "Решение", "STEP-профили / условия"],
           [[compact_marks(marks), key[1], "; ".join(key[2]) + (
               ". Крепёж входит в 3D-сборку. Проверены опоры головок, зацепление, контакты деталей и пересечения. Нагрузки, шаг и анкеровка к зданию отдельно не рассчитаны. Проверки и полный состав - в ZIP."
               if key[3] else ". Посадки приняты проектным рецептом; крепёж, нагрузки и монтажный ход уточняются. Полные допущения - в JSON сборок ZIP.")
             ] for key, marks in recipes.items()], [2, 3.3, 6.7], "assembly_notes")
    decisions = {}
    for entry in actual:
        note = ("К на разрезах - только архитектурное окружение: местные слои пола и плоскость потолка. Крепёж и опорные детали показаны из сохранённых тел 3D-сборки; проверены посадки и пересечения. Количество относится к местному узлу, не ко всей стене; шаг, нагрузки и анкеровка к зданию не рассчитаны. Геометрия и проверки - в ZIP."
                if entry["assembly"].spec.get("fastenings") else
                "К на разрезах - проектный контекст: подоснова, фрагменты пола/потолка, каркас и крепёж. Контуры каркаса и саморезов взяты из STEP библиотеки; их расстановка не является проверенной 3D-сборкой и не задаёт количество или шаг крепления. Происхождение и точные контуры - mount-drawing-context.json в ZIP.")
        decisions.setdefault(note, []).append(entry["mark"])
    for entry in actual:
        for assumption in entry["assembly"].assumptions:
            decisions.setdefault(assumption, []).append(entry["mark"])
    table("Принятые решения для сборок узлов", ["Узлы", "Условие и граница применения"],
           [[compact_marks(marks), text] for text, marks in decisions.items()], [2, 10], "assembly_decisions")

    from ..mount_assemblies import assembly_usage
    _, unused = assembly_usage(project, mounting, saved=assembly_records, server_owned=derived_assemblies)
    table("Сборки, требующие повторного построения", ["Сборка", "Причина"],
           [[record.label, "Параметры, геометрический контекст или исходники изменились, либо соответствующее примыкание удалено. Сборка не включена в разрезы; выполните построение заново."]
            for record in unused], [3, 9], "assembly_stale")

    # Measurements and explicit legacy-zone schedules remain present when used.
    sheets.extend(sheet for sheet in original_sheets if sheet.kind in {"measurements", "schedule", "nodes", "vendor_assemblies", "vendor_requirements", "vendor_checks"}
                  and (sheet.kind != "measurements" or project.measurements))
    report["album_layout"] = {"schema_version": "reference-album/1.0", "sheet_count_before_contents": len(sheets),
        "view_marks": [{"mark": f"М{index:03d}", "wall_id": view["wall"]["id"], "face_id": view["wall"]["face_id"]} for index, view in enumerate(walls, 1)],
        "detail_marks": [{"mark": entry["mark"], "locations": entry["locations"],
                          "assembly_id": entry["assembly"].id if entry.get("assembly") else None,
                          "representation": "actual_section" if entry.get("assembly") else "schematic"} for entry in catalog.values()],
        "technical_records": ["engineering.json", "project.json"]
            + (["wall-mounting.json", "wall-mount-panels.csv", "wall-mount-junctions.csv"] if mounting else [])
            + (["assemblies/index.json"] if actual or project.properties.get("mount_assemblies", {}).get("items") else [])
            + (["mount-drawing-context.json"] if actual else [])
            + (["special-assemblies/index.json"] if specials else []),
        "special_sections":[{"mark":entry["mark"],"status":entry["status"],"wall_id":entry["wall_id"],"face_id":entry["face_id"]} for entry in specials]}
    from .album_tables import combine_general_contents
    combine_general_contents(sheets)
    report["album_layout"]["sheet_count_before_contents"] = len(sheets)
    if len(sheets) > MAX_SHEETS:
        raise ValueError(f"Альбом превышает лимит {MAX_SHEETS} листов. Разделите проект.")
    return sheets
