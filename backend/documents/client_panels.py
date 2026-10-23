"""Customer album: coloured panel layouts, dimensions and no fixing details.

The same lightweight layout feeds the viewer, SVG, PDF and editable DXF.
This path intentionally does not resolve engineering assembly caches.
"""
from __future__ import annotations

import csv
import hashlib
from io import BytesIO, StringIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from . import DocumentSet, _frame, json_bytes, num, scale_for
from .scene import BLUE, GRAY, INK, LIGHT, Sheet, width as text_width
from ..domain.models import Project
from ..finish_presentation import build_finish_presentation


class ClientDocumentSet(DocumentSet):
    def index(self):
        value = super().index()
        value.update(status="client_presentation", purpose="panels_only")
        return value

    def package(self):
        self._check_snapshot()
        # No full project.json/STEP/manuals in a customer package. The export
        # contains only the presented layout and its colour/dimension schedule.
        data = StringIO()
        writer = csv.writer(data, delimiter=";")
        writer.writerow(["Развёртка", "Этаж", "Помещение", "Панель", "X, мм", "Низ, мм", "Ширина, мм", "Высота, мм", "Толщина, мм", "Цвет"])
        for view in self.report["presentation"]["views"]:
            for panel in view["panels"]:
                writer.writerow([view["mark"], view["floor_name"], view["space_name"], panel["index"],
                    *(num(panel[k]) for k in ("x_mm", "y_mm", "width_mm", "height_mm", "thickness_mm")), view["panel_colors"][panel["id"]]])
        files = {"panel-layouts.pdf": self.pdf(), "panel-layouts.dxf": self.dxf(),
            "panel-layouts.csv": data.getvalue().encode("utf-8-sig"), "sheets.json": json_bytes(self.index()),
            "README.txt": ("АЛЬБОМ РАСКЛАДКИ ПАНЕЛЕЙ ДЛЯ ЗАКАЗЧИКА\n"
                "Планы и развёртки, размеры, номера панелей и выбранные цвета. Без каркаса, крепежа и узлов.\n"
                "Цвет на экране и в PDF - визуальный образец, не код RAL и не заводской образец материала.\n"
                "Размеры в миллиметрах. Фрагменты у проёмов показаны отдельно, не как карта раскроя.\n"
                "PDF, SVG и DXF содержат одни и те же листы. Печать 100%, без подгонки.\n").encode()}
        for i, sheet in enumerate(self.sheets, 1):
            files[f"sheets/{i:03d}.svg"] = sheet.svg()
        for name in ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "OFL.txt"):
            files[f"fonts/{name}"] = (Path(__file__).parent/"fonts"/name).read_bytes()
        files["manifest.json"] = json_bytes({"purpose": "panels_only", "project_id": self.project.id,
            "revision": self.project.revision, "source_snapshot_sha256": self.report["source_snapshot_sha256"],
            "files": [{"path": path, "sha256": hashlib.sha256(value).hexdigest()} for path, value in sorted(files.items())]})
        output = BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for name, value in sorted(files.items()):
                entry = ZipInfo(name, (2020, 1, 1, 0, 0, 0)); entry.compress_type = ZIP_DEFLATED
                archive.writestr(entry, value)
        return output.getvalue()


def _contrast(hex_color):
    channels = [int(hex_color[i:i+2], 16)/255 for i in (1, 3, 5)]
    return "#ffffff" if sum(a*b for a, b in zip(channels, (.2126, .7152, .0722))) < .52 else INK


def draw_view(sheet, view, box):
    x, y, width, height = box
    sheet.rect(x, y, width, height, "#d9e0e8", weight=.1)
    sheet.text(x+4, y+7, f"{view['mark']} · {view['name']}", 3.5, INK, True)
    lines_y = sheet.paragraph(x+4, y+13, f"{view['floor_name']} / {view['space_name']}", width-8, 2.6, GRAY, leading=3.9)
    top = max(y+24, lines_y+5)
    scale = scale_for(view["length_mm"], view["height_mm"], width-83, y+height-top-38)
    drawn_w, drawn_h = view["length_mm"]/scale, view["height_mm"]/scale
    left, bottom = x+24+(width-83-drawn_w)/2, top+drawn_h
    transform = lambda px, py: (left+px/scale, bottom-py/scale)
    sheet.rect(left, top, drawn_w, drawn_h, GRAY, "#ffffff", .15)
    for panel in view["panels"]:
        px, py = transform(panel["x_mm"], panel["y_mm"]+panel["height_mm"])
        pw, ph = panel["width_mm"]/scale, panel["height_mm"]/scale
        color = view["panel_colors"][panel["id"]]
        sheet.rect(px, py, pw, ph, "#5f6872", color, .16)
        label = str(panel["index"])
        if pw > text_width(label, 2.5)+1.2 and ph > 5:
            sheet.text(px+pw/2, py+ph/2+1, label, 2.5, _contrast(color), True, "middle")
    for o in view["openings"]:
        px, py = transform(o["x_mm"], o["y_mm"]+o["height_mm"])
        ow, oh = o["width_mm"]/scale, o["height_mm"]/scale
        # The opening was already subtracted from every panel, never white
        # paint covering overlapping solids in the 3D representation.
        sheet.rect(px, py, ow, oh, GRAY, None, .25)
        label = {"door": "Дверь", "window": "Окно", "passage": "Проход", "hatch": "Люк"}.get(o["kind"], "Проём")
        if ow > text_width(label, 2.4)+1 and oh > 8:
            sheet.text(px+ow/2, py+oh/2, label, 2.4, GRAY, anchor="middle")
    sheet.dimension(left, left+drawn_w, bottom+9, bottom, num(view["length_mm"]))
    from .section_dimensions import draw_section_dimensions
    draw_section_dimensions(sheet, [[(left, top), (left+drawn_w, top), (left+drawn_w, bottom), (left, bottom)]],
                            None, num(view["height_mm"]), bounds=(x+1, top-6, x+width-1, bottom+15))
    # Chain the visible module widths only where labels have real paper room.
    spans = sorted({(p["x_mm"], p["width_mm"]) for p in view["panels"]})
    last_right = -1e9
    for px, pw in spans:
        a, b = left+px/scale, left+(px+pw)/scale
        if a < last_right-.001 or b-a < text_width(num(pw), 2.8)+2:
            continue
        sheet.dimension(a, b, top-4, top, num(pw)); last_right = b
    from .levels import draw_levels, view_levels
    levels = view_levels(sheet._project, view["floor_id"], view["space_id"])
    if view["configuration"].get("installation_zone") == "exterior":
        levels = [{"elevation_mm": view["base_mm"], "label": "низ облицовки", "basis": "model"},
                  {"elevation_mm": view["base_mm"]+view["height_mm"], "label": "верх облицовки", "basis": "model"}]
    draw_levels(sheet, levels, transform, view["length_mm"], view["base_mm"], view["height_mm"])
    cfg = view["configuration"]
    sheet.text(x+4, y+height-14, f"М 1:{num(scale)} · Панель {num(cfg['panel_thickness_mm'])} мм · Шов {num(cfg['joint_mm'])} мм", 2.7)
    sheet.rect(x+4, y+height-10, 7, 4, GRAY, view["color"], .12)
    colors = set(view["panel_colors"].values())
    label = f"Цвет {next(iter(colors)).upper()}" if len(colors) == 1 else f"Цветов: {len(colors)} · по раскладке"
    sheet.text(x+14, y+height-6.7, f"{label} · Фрагментов: {len(view['panels'])}", 2.5, GRAY)
    sheet.references.append(view["face_id"])


def build_client_documents(project):
    project = Project.model_validate_json(project.model_dump_json())
    from ..geometry.validation import validate_project
    validation = validate_project(project)
    if not validation.valid:
        raise ValueError("Перед построением раскладок исправьте ошибки геометрии проекта")
    scene = build_finish_presentation(project)
    if not scene["views"]:
        raise ValueError("Назначьте монтаж хотя бы одной стене: клиентский альбом строится по раскладке панелей")
    sheets = []
    cover = Sheet("Раскладка панелей для заказчика", "client_cover", 420, 297)
    cover.text(29, 55, "VIOLET / ОТДЕЛКА", 6.5, BLUE, True)
    y = cover.paragraph(29, 78, project.metadata.name, 360, 6, INK, True)
    if project.metadata.address:
        y = cover.paragraph(29, y+7, project.metadata.address, 360, 3.3, GRAY)
    y = max(128, y+15)
    cover.text(29, y, "Альбом раскладки стеновых панелей", 4.6, INK, True)
    for line in ("Планы этажей, цветные развёртки и размеры панелей.",
                 "Без чертежей каркаса, узлов и крепёжных деталей.",
                 "Цвет показан для визуального согласования. Оттенок уточняется по образцу материала.",
                 "Номера внутри раскладки соответствуют фрагментам панелей у проёмов.",
                 "Все размеры в миллиметрах. Толщина и швы взяты из настроек монтажа."):
        y = cover.paragraph(29, y+9, line, 360, 3.1, leading=4.7)
    sheets.append(cover)
    from .plan_walls import draw_wall_footprint, wall_footprint
    from .plan_dimensions import draw_plan_overall_dimension
    from .plan_labels import draw_plan_labels, draw_space_labels
    from .openings import opening_marks, draw_plan_opening
    labels = opening_marks(project)
    for building in project.buildings:
        for floor in building.floors:
            views = [v for v in scene["views"] if v["floor_id"] == floor.id]
            if not views:
                continue
            plan = Sheet(f"План раскладки / {floor.name}", "client_plan", 594, 420)
            footprint = wall_footprint(floor, openings=False)
            minx, miny, maxx, maxy = footprint.bounds
            scale = scale_for(maxx-minx, maxy-miny, 460, 228)
            left = 65+(460-(maxx-minx)/scale)/2
            transform = lambda x, y: (left+(x-minx)/scale, 69+(maxy-y)/scale)
            plan.text(25, 38, f"М 1:{scale}. Марки Пxxx отсылают к раскладкам панелей. Размеры основы - без отделки.", 3, GRAY)
            draw_wall_footprint(plan, wall_footprint(floor), transform, INK, LIGHT)
            walls = {w.id: w for w in floor.walls}
            for opening in floor.openings:
                draw_plan_opening(plan, opening, walls[opening.wall_id], transform, scale, labels[opening.id], void_already_cut=True, draw_label=False)
            draw_plan_overall_dimension(plan, footprint, transform)
            draw_plan_labels(plan, floor, transform, scale, labels, [(v["wall_id"], v["side"], v["mark"]) for v in views])
            draw_space_labels(plan, floor, transform)
            sheets.append(plan)
            boxes = [(23, 34, 269, 154), (302, 34, 269, 154), (23, 198, 269, 154), (302, 198, 269, 154)]
            for index, view in enumerate(views):
                if index % 4 == 0:
                    sheet = Sheet(f"Раскладки панелей / {floor.name}", "client_elevations", 594, 420)
                    sheet._project = project
                    sheets.append(sheet)
                draw_view(sheet, view, boxes[index % 4])
    from .album_tables import table
    rows = [[v["mark"], f"{v['floor_name']} / {v['space_name']}", v["name"],
             f"{num(v['configuration']['panel_width_mm'])} × {num(v['configuration']['panel_height_mm'])}",
             num(v["configuration"]["panel_thickness_mm"]),
             next(iter(set(v["panel_colors"].values()))).upper() if len(set(v["panel_colors"].values())) == 1 else "По раскладке", str(len(v["panels"])),
             num(sum(p["area_m2"] for p in v["panels"]))] for v in scene["views"]]
    table(sheets, "Ведомость раскладок и отделки", ["Вид", "Этаж / помещение", "Стена", "Модуль, мм", "t, мм", "Цвет", "Частей", "м²"], rows,
          [1, 3.5, 2, 2.3, 1, 1.4, 1, 1.3], "client_schedule", paper=(594, 420))
    for index, sheet in enumerate(sheets, 1):
        _frame(sheet, project, index, len(sheets), "PANELS")
    report = {"source_snapshot_sha256": hashlib.sha256(project.model_dump_json().encode()).hexdigest(),
        "project_id": project.id, "project_revision": project.revision, "presentation": scene,
        "readiness": {"geometry_valid": True, "purpose": "client_presentation", "production_ready": False}, "issues": []}
    return ClientDocumentSet(project, report, sheets, "PANELS")
