"""Full-wall framing overlays and compact schedules from saved assemblies.

The elevation deliberately uses installation symbols, not enlarged screw
silhouettes. Actual three-dimensional geometry remains in the associated STEP.
No coordinates, pitches or quantities are guessed by the document renderer.
"""
from __future__ import annotations

from collections import defaultdict

from .scene import BLUE, GRAY, INK, wrap

ANCHOR = "#a75216"
PROFILE = "#783dab"
PROFILE_NAMES = {"vh2":"VH-2", "vh3":"VH-3", "lstart":"V-L-start (адаптация)"}
SUBSTRATES = {"concrete": "Бетон", "solid_masonry": "Полнотелая кладка",
              "hollow_masonry": "Пустотелая кладка", "gypsum": "Гипсовое основание",
              "unknown": "Основание не определено"}


def _data(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _plan(record):
    value = _data(record)
    return value.get("plan", value)


def framing_legend(record):
    legend = "Пунктир - каркас; + - саморез подосновы; кольцо - анкер каркаса."
    if _plan(record).get("profile_runs"):
        legend += " Фиолетовые линии / x - профили / их саморезы."
    return legend


def draw_framing_overlay(sheet, record, transform, scale):
    """Add installation symbols to a mounting view's existing transformation.

    ``transform(x,z)`` uses local wall millimetres with Z upwards. Everything is
    fully vectorial; the shared scene exporter preserves this in PDF and DXF.
    The caller is responsible for selecting a non-stale saved assembly.
    """
    plan = _plan(record)
    start = len(sheet.primitives)
    for post in plan.get("posts", []):
        x1, y1 = transform(post["x_mm"], post["z_start_mm"])
        x2, y2 = transform(post["x_mm"], post["z_end_mm"])
        sheet.line(x1, y1, x2, y2, BLUE, .22, True)
    for rail in plan.get("rails", []):
        x1, y1 = transform(rail["x_start_mm"], rail["z_mm"])
        x2, y2 = transform(rail["x_end_mm"], rail["z_mm"])
        sheet.line(x1, y1, x2, y2, BLUE, .22, True)
    for run in plan.get("profile_runs", []):
        vertical = run["axis"] == "vertical"
        x1, y1 = transform(run["edge_mm"] if vertical else run["start_mm"], run["start_mm"] if vertical else run["edge_mm"])
        x2, y2 = transform(run["edge_mm"] if vertical else run["end_mm"], run["end_mm"] if vertical else run["edge_mm"])
        sheet.line(x1, y1, x2, y2, PROFILE, .28)
    for screw in plan.get("screws", []):
        x, y = transform(screw["x_mm"], screw["z_mm"])
        radius = .48
        sheet.line(x-radius, y, x+radius, y, INK, .14)
        sheet.line(x, y-radius, x, y+radius, INK, .14)
    for screw in plan.get("profile_screws", []):
        x, y = transform(screw["x_mm"], screw["z_mm"])
        radius = .48
        sheet.line(x-radius, y-radius, x+radius, y+radius, PROFILE, .18)
        sheet.line(x-radius, y+radius, x+radius, y-radius, PROFILE, .18)
    for anchor in plan.get("anchors", []):
        x, y = transform(anchor["x_mm"], anchor["z_mm"])
        sheet.circle(x, y, .82, ANCHOR, weight=.18)
    sheet.layout.setdefault("whole_wall_overlays", []).append({
        "wall_id": plan["wall"]["id"], "face_id": plan["wall"]["face_id"],
        "primitive_start": start, "primitive_end": len(sheet.primitives),
        "post_count": len(plan.get("posts", [])), "rail_count": len(plan.get("rails", [])),
        "screw_count": len(plan.get("screws", []))+len(plan.get("profile_screws", [])), "anchor_count": len(plan.get("anchors", [])),
        "frame_screw_count": len(plan.get("screws", [])),
        "profile_screw_count": len(plan.get("profile_screws", [])), "profile_count": len(plan.get("profile_runs", [])),
        "scale": scale,
    })


def draw_whole_wall_view(sheet, record, mark, box, num):
    """Optional standalone framing elevation, with readable baseline dimensions."""
    from . import scale_for
    from .openings import vertical_dimension
    plan = _plan(record)
    x, y, width, height = box
    wall = plan["wall"]
    title = wrap(f"{mark}. Каркас и крепёж / {wall['name']}", width-8, 3.2, True)
    for index, line in enumerate(title):
        sheet.text(x+3, y+5+index*4.6, line, 3.2, INK, True)
    title_h = len(title)*4.6
    scale = scale_for(wall["length_mm"], wall["height_mm"], width-48, height-title_h-45)
    dw, dh = wall["length_mm"]/scale, wall["height_mm"]/scale
    ox, oy = x+20+(width-48-dw)/2, y+title_h+16
    transform = lambda a, b: (ox+a/scale, oy+(wall["height_mm"]-b)/scale)
    sheet.text(x+3, y+title_h+10, f"М 1:{num(scale)}. Положение крепежа - по сохранённой 3D-сборке.", 2.5, GRAY)
    for piece in plan.get("panels", []):
        px, py = transform(piece["x_mm"], piece["y_mm"]+piece["height_mm"])
        sheet.rect(px, py, piece["width_mm"]/scale, piece["height_mm"]/scale, GRAY, "#f3eadc", .12)
    for opening in plan.get("openings", []):
        px, py = transform(opening["x_mm"], opening["y_mm"]+opening["height_mm"])
        sheet.rect(px, py, opening["width_mm"]/scale, opening["height_mm"]/scale, INK, "#ffffff", .25)
    sheet.rect(ox, oy, dw, dh, INK, weight=.25)
    draw_framing_overlay(sheet, record, transform, scale)
    sheet.dimension(ox, ox+dw, oy+dh+8, oy+dh, num(wall["length_mm"]), BLUE)
    vertical_dimension(sheet, ox+dw+8, oy, oy+dh, ox+dw, num(wall["height_mm"]))
    lines = wrap(framing_legend(record), width-6, 2.5)
    for index, line in enumerate(lines):
        sheet.text(x+3, y+height-8-(len(lines)-index-1)*3.8, line, 2.5, GRAY)
    sheet.text(x+3, y+height-3, "Координаты X / Z отсчитываются от левого нижнего угла развёртки.", 2.5, GRAY)


def add_whole_wall_tables(records, table, num, marks=None):
    """Lossless compact totals/cut list; every individual coordinate lives in ZIP."""
    from .album import compact_marks
    marks = marks or {}
    summary, lengths, assumptions = [], defaultdict(list), defaultdict(list)
    component_totals = defaultdict(lambda: {"count":0, "marks":[]})
    systems, attachment_rows, profile_lengths = [], [], defaultdict(list)
    machining_totals = defaultdict(lambda: {"count":0, "marks":[], "panels":set()})
    for index, item in enumerate(records, 1):
        record = _data(item)
        plan = _plan(record)
        wall, cfg = plan["wall"], plan["configuration"]
        mark = marks.get((wall["id"], wall["face_id"]), f"КС{index:03d}")
        summary.append([mark, wall["name"],
            f"{len(plan.get('posts', []))} / {len(plan.get('rails', []))}",
            str(len(plan.get("screws", []))+len(plan.get("profile_screws", []))), str(len(plan.get("anchors", []))),
            f"{num(cfg['frame_pitch_mm'])} / {num(cfg['screw_pitch_mm'])} / {num(cfg['anchor_pitch_mm'])}",
            SUBSTRATES.get(cfg["substrate_type"], cfg["substrate_type"])+f" / {num(cfg['substrate_thickness_mm'])}",
            num(cfg["anchor_embedment_mm"])])
        if plan.get("system"):
            system = plan["system"]
            actual_profiles = dict.fromkeys(PROFILE_NAMES.get(run["family"], run["family"]) for run in plan.get("profile_runs", []))
            systems.append([mark, system.get("label", system.get("key", "")), " / ".join(actual_profiles),
                            str(len(plan.get("screws", []))), str(len(plan.get("profile_screws", []))),
                            str(len(plan.get("profile_runs", [])))])
            if system.get("scope"):
                assumptions[system["scope"]].append(mark)
        attachment = plan.get("attachment_summary")
        if attachment:
            attachment_rows.append([mark, *[str(attachment[name]) if name in attachment else "-" for name in
                ("mechanical_panel_count", "adhesive_panel_count", "clip_count")]])
        for run in plan.get("profile_runs", []):
            profile_lengths[(PROFILE_NAMES.get(run["family"], run["family"]), num(run["end_mm"]-run["start_mm"]))].append(mark)
        for operation in record.get("geometry", {}).get("machining", []):
            identity = (operation["kind"], tuple(num(value) for value in operation["size_mm"]))
            machining_totals[identity]["count"] += 1
            machining_totals[identity]["marks"].append(mark)
            machining_totals[identity]["panels"].add((mark, operation["panel_id"]))
        for post in plan.get("posts", []):
            identity = ("Стойка", num(post["width_mm"]), num(post["depth_mm"]), num(post["z_end_mm"]-post["z_start_mm"]))
            lengths[identity].append((mark, post["id"]))
        for rail in plan.get("rails", []):
            identity = ("Перемычка", num(rail["width_mm"]), num(rail["depth_mm"]), num(rail["x_end_mm"]-rail["x_start_mm"]))
            lengths[identity].append((mark, rail["id"]))
        for text in plan.get("assumptions", []):
            if isinstance(text, dict):
                text = text.get("message", str(text))
            assumptions[text].append(mark)
        for component in record.get("geometry", {}).get("bom", []):
            if component.get("role") == "substrate" or component.get("count", 0) <= 0:
                continue
            provenance = component.get("provenance", {})
            kind = provenance.get("kind", "project_parametric")
            origin = {"source_step":"Исходный STEP", "exact_section_extrusion":"Точное сечение STEP; длина по раскладке", "project_parametric":"Проектная геометрия", "project_machined_panel":"Панель с фактическими пазами и выборками"}.get(kind,kind)
            label = "Панели по раскладке" if component.get("role") == "panel" else component["label"]
            if provenance.get("length_mm") is not None:
                label += f"; L={num(provenance['length_mm'])} мм"
            source_id = provenance.get("source_id", "")
            if source_id:
                source = next((source for source in record.get("source_models", []) if source.get("source_id") == source_id), {})
                origin += "; " + (source.get("filename") or source.get("name") or source_id[:12])
            identity = (label, origin, component.get("role"), source_id)
            component_totals[identity]["count"] += component["count"]
            component_totals[identity]["marks"].append(mark)
    if not summary:
        return
    table("Каркас и крепёж стен / расстановка по всей поверхности",
          ["Вид", "Стена", "Стойки / перемычки", "Саморезы, шт.", "Анкеры, шт.", "Макс. шаг стойки / самореза / анкера, мм", "Основание / t, мм", "Заделка, мм"],
          summary, [1, 2, 1.2, 1.1, 1.1, 2.5, 2.3, 1.1], "whole_wall_quantities")
    if systems:
        table("Системы монтажа / состав крепления", ["Вид", "Система", "Профили", "Саморезы каркаса, шт.", "Саморезы профилей, шт.", "Отрезки профиля, шт."],
              systems, [1, 3, 2, 1.5, 1.5, 1.5], "whole_wall_systems")
    if attachment_rows:
        table("Крепление панелей / принятая система", ["Вид", "Панелей на профилях", "Панелей на клею", "Клипс, шт."],
              attachment_rows, [1, 3, 3, 2], "whole_wall_attachments")
    table("Заготовки каркаса / фактические длины",
          ["Виды", "Деталь", "Ширина x глубина, мм", "Длина, мм", "Количество, шт."],
          [[compact_marks(mark for mark, _ in entries), identity[0], f"{identity[1]} x {identity[2]}", identity[3], str(len(entries))]
           for identity, entries in lengths.items()], [2.5, 2, 2, 1.5, 1.5], "whole_wall_members")
    if profile_lengths:
        table("Заготовки монтажных профилей / фактические длины", ["Виды", "Профиль", "Длина, мм", "Количество, шт."],
              [[compact_marks(entries), family, length, str(len(entries))] for (family, length), entries in profile_lengths.items()],
              [2.5, 3, 1.5, 1.5], "whole_wall_profiles")
    if component_totals:
        table("Состав полных сборок стен", ["Виды", "Элемент", "Количество тел, шт.", "Происхождение"],
              [[compact_marks(value["marks"]), label, str(value["count"]), origin]
               for (label,origin,_role,_source),value in component_totals.items()], [2.5,4,1.5,3], "whole_wall_components")
    if machining_totals:
        names = {"edge_groove": "Торцевой паз", "rear_edge_relief": "Тыльная выборка"}
        table("Обработка панелей / операции в STEP", ["Виды", "Операция", "Размер X x Y x Z, мм", "Операций", "Панелей"],
              [[compact_marks(value["marks"]), names.get(kind, kind), " x ".join(size), str(value["count"]), str(len(value["panels"]))]
               for (kind,size),value in machining_totals.items()], [2, 3, 3, 1, 1], "whole_wall_machining")
    notes = ["Пунктир на развёртках - стойки и перемычки, крест - саморез, кольцо - анкер. Положение символов соответствует координатам крепежа; размер символа условный.",
             "Саморез подосновы соединяет её с каркасом; анкер соединяет каркас с основанием. Это разные отверстия, а не пара в одной точке. В принятой сборке они стоят на осях опор с разными шагами; между осью анкера и саморезом оставлен доступ не менее 25 мм. Совмещение осей привело бы к пересечению деталей.",
             "Шаг в ведомости - максимальный заданный. Фактическая расстановка учитывает края панелей и обход проёмов. Каждый элемент, его координаты и геометрические проверки включены в данные сборки и STEP.",
             "Количество крепежа полного каркаса приведено отдельно от состава образцов узлов. Состав образца узла не является дополнительным количеством крепежа на стену.",
             "Анкеровка показана с заданными диаметрами и глубиной. Расчёт несущей способности основания и крепежа в геометрическую проверку не входит."]
    if profile_lengths:
        notes.append("Фиолетовая линия - монтажный профиль; диагональный крест - его крепёж к подоснове. Крепление каркаса и крепление профиля учтены отдельно; графические символы не заменяют геометрию STEP.")
    if machining_totals:
        notes.append("Ведомость обработки перечисляет реальные операции вычитания в панелях. X - вдоль стены, Y - глубина, Z - высота; размеры в сборочных координатах. Начало и размер каждой операции, панель и профиль сохранены в данных сборки; совпадающие операции сгруппированы без потери количества.")
    table("Принятые решения по каркасу и основанию", ["Виды", "Примечание"],
          [["Все", text] for text in notes]+[[compact_marks(values), text] for text, values in assumptions.items()],
          [1.5, 10], "whole_wall_notes")
