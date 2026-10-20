"""Sheets of real assembled solid sections, never schematic replacement shapes."""
from __future__ import annotations

import math

from .scene import BLUE, GRAY, INK, RED, wrap


def _hatch(sheet, loops, color, direction):
    """Even-odd clipping against actual closed wires, preserving inner holes."""
    r = math.sqrt(2)
    projected = [[((x + direction*y)/r, (y - direction*x)/r) for x,y in loop] for loop in loops]
    if not projected:
        return
    low = min(y for loop in projected for _, y in loop)
    high = max(y for loop in projected for _, y in loop)
    # Even imported snapshots must never cause unbounded hatch iteration.
    spacing = max(2., (high-low)/400)
    for row in range(math.floor(low / spacing), math.ceil(high / spacing) + 1):
        v = row * spacing
        hits = []
        for loop in projected:
            for a,b in zip(loop, loop[1:] + loop[:1]):
                if (a[1] <= v < b[1]) or (b[1] <= v < a[1]):
                    hits.append(a[0] + (v-a[1])*(b[0]-a[0])/(b[1]-a[1]))
        hits.sort()
        for a,b in zip(hits[::2], hits[1::2]):
            sheet.line((a-direction*v)/r, (direction*a+v)/r,
                       (b-direction*v)/r, (direction*b+v)/r, color, .1)


def add_assembly_detail(entry, new, width, height, num):
    sheet = new(f"{entry['mark']} / {entry['node']['label']} / разрез сборки STEP", "wall_mount_assembly")
    draw_assembly_view(sheet, entry, (23, 34, width-48, height-91), num)


def draw_assembly_view(sheet, entry, box, num):
    """Identical detailed vector cuts in compact albums and legacy formats."""
    from .node_construction import draw_construction_view
    draw_construction_view(sheet, entry, box, num)


def add_assembly_tables(catalog, table):
    from .node_construction import assembly_fasteners, has_assembly_support
    rows, notes, machining = [], [], []
    for entry in catalog.values():
        record = entry.get("assembly")
        if not record:
            continue
        mark = entry["mark"]
        if has_assembly_support(record):
            notes.append([mark,"3D-сборка / состав", f"Каждая числовая позиция соответствует отдельному телу сохранённой STEP-сборки. Крепёж: {len(assembly_fasteners(record))} шт. в данном местном фрагменте. Это состав узла, не общее количество крепежа на стене."])
            notes.append([mark,"Окружение разреза / К", "Только архитектурное окружение: местные фрагменты пола и плоскость потолка. Каркас, подоснова и крепёж показаны из тел сборки, без дополнительных 2D-копий. Контуры и происхождение: mount-drawing-context.json в ZIP."])
            checks = getattr(record, "fastening_checks", None) or []
            for check in checks:
                engagements = check.get("engagements", [])
                depths = "; ".join(f"{value['target_id']}: {float(value['depth_mm']):.3f} мм (мин. {float(value['min_engagement_mm']):.3f} мм)" for value in engagements)
                notes.append([mark, f"3D-крепёж / поз. {check['fastener_body_id']}",
                    f"Посадка: {'проверена' if check['status'] == 'valid' else 'требует исправления'}. Контакт головки с опорой: {float(check['bearing_contact_area_mm2']):.3f} мм². Зацепление стержня: {depths}. Заход резьбы в материал учитывается отдельно от запрещённых пересечений."])
            for check in getattr(record, "contact_checks", None) or []:
                notes.append([mark, f"Опора / поз. {check['body_a_id']} - {check['body_b_id']}",
                    f"Контакт тел: {'проверен' if check['status'] == 'valid' else 'требует исправления'}. Площадь {float(check['contact_area_mm2']):.3f} мм²; минимум {float(check['min_area_mm2']):.3f} мм². Пересечение объёмов: {float(check['overlap_volume_mm3']):.6g} мм³."])
        else:
            notes.append([mark,"Окружение разреза / К", "Подоснова и слои пола/потолка - проектные фрагменты. Каркас и саморезы показаны контурами STEP, их расстановка не входит в проверенную 3D-сборку и не задаёт шаг или количество крепежа. Точные контуры и происхождение: mount-drawing-context.json в ZIP."])
        for body in record.bodies:
            source = next((source for source in record.source_models if source.source_id == body.get("source_id")), None)
            bounds = body.get("bounds_mm", {})
            rows.append([mark,str(body["id"]),str(body.get("label","")),
                         "STEP: "+source.filename if source else "Параметрическое тело, не STEP поставщика",
                         " x ".join(f"{float(v):.3f}".rstrip("0").rstrip(".") for v in bounds.get("size",[]))])
            for cut in body.get("cutouts", []):
                size = body["size_mm"]
                origin = [max(0., v) for v in cut["origin_mm"]]
                removed = [max(0., min(size[i], cut["origin_mm"][i]+cut["size_mm"][i])-origin[i]) for i in range(3)]
                values = lambda vector: " / ".join(f"{v:.3f}".rstrip("0").rstrip(".") for v in vector)
                machining.append([mark, str(body["id"]), cut["id"], values(origin), values(removed)])
        for assumption in record.assumptions:
            notes.append([mark,"Проектная посадка / допущение",assumption])
        for source in record.source_models:
            notes.append([mark,source.component_code,source.filename+"; SHA256 "+source.source_id])
        notes.append([mark,"Сохранённая сборка",f"Рецепт {record.recipe_id}; SHA256 STEP {record.assembled_step.source_id}; контрольная сумма состава {record.fingerprint}."])
        for ref in record.source_refs:
            notes.append([mark,"Каталог профилей / сравнение" if ref.get("reference_kind")=="profile_geometry_comparison" else "Исходная схема / сравнение",
                f"{ref.get('filename', ref.get('document_id',''))}; PDF-страница {ref.get('page','?')}. "+str(ref.get("note") or "Схема служит ориентиром: конкретные адаптации и посадки перечислены отдельно и не подтверждены этим источником.")])
    if rows:
        table("Состав сборок узлов",["Узел","Тело","Обозначение","Происхождение","Габарит тела, мм"],rows,[1,1,2.2,5,2.3],"assembly_components")
        if machining:
            table("Пазы и выборки панелей / местные X,Y,Z",
                  ["Узел", "Тело", "Вырез", "Начало X / Y / Z, мм", "Габарит выреза в теле X / Y / Z, мм"],
                  machining, [1,1,2.5,3.3,4.2], "assembly_machining")
        table("Сборки STEP / посадки, источники и ограничения",["Узел","Условие","Описание"],notes,[1,2.6,9],"assembly_assumptions")
