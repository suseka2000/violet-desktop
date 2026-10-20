"""Revision-bound, reproducible draft documentation from canonical geometry.

This is deliberately NOT a certified/GOST manufacturing release. Its package
contains the exact source snapshot, engineering diagnostics and file checksums.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass, field
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from ..domain.models import Project
from .scene import BLUE, GRAY, INK, LIGHT, RED, Sheet, pdf_bytes, wrap
from .scene import width as text_width

GENERATOR_VERSION = "0.4.0"
MAX_SHEETS = 400
FORMATS = {"A3": (420, 297), "A2": (594, 420), "ALBUM": (594, 420)}
CONTEXT_NAMES = {"floor": "Пол", "ceiling": "Потолок", "wall_start": "Начало стены", "wall_end": "Конец стены",
                 "opening_head": "Верх проёма", "opening_sill": "Низ проёма", "opening_jamb_left": "Левый откос",
                 "opening_jamb_right": "Правый откос", "corner": "Угол"}


class DocumentError(ValueError):
    pass


def num(value):
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def node_locations(requirement):
    """A corner belongs to two views; never invent zero coordinates."""
    location = requirement.get("location", {})
    if requirement["kind"] == "corner":
        return [(location["primary_elevation_id"], location["primary_x_mm"], location["bottom_mm"]),
                (location["adjacent_elevation_id"], location["adjacent_x_mm"], location["bottom_mm"])]
    return [(requirement["elevation_id"], location["x_mm"], location["y_mm"])]


def scale_for(width, height, box_width, box_height):
    ratio = max(width / box_width, height / box_height)
    for scale in (5, 10, 20, 25, 40, 50, 75, 100, 150, 200, 250, 500, 1000, 2000, 5000):
        if ratio <= scale:
            return scale
    raise DocumentError("Вид не помещается в допустимые масштабы. Разбейте проект на фрагменты.")


def _plan_dimensions(sheet, floor, transform, scale):
    """Compatibility entry point for dimensions of the visible plan faces."""
    from .plan_dimensions import draw_plan_dimensions
    draw_plan_dimensions(sheet, floor, transform, scale)


@dataclass
class DocumentSet:
    project: Project
    report: dict
    sheets: list[Sheet]
    paper_format: str
    derived_assemblies: bool = False
    _validated_nodes: object = field(default=None, repr=False)
    _validated_wall_usage: object = field(default=None, repr=False)

    def pdf(self):
        self._check_snapshot()
        return pdf_bytes(self.sheets, self.project.metadata.name)

    def dxf(self):
        """Editable, whole-album CAD output from the exact PDF/SVG scene."""
        self._check_snapshot()
        from .dxf_album import album_dxf_bytes
        return album_dxf_bytes(self.sheets, self.project.metadata.name,
                               project_id=self.project.id, revision=self.project.revision)

    def _check_snapshot(self):
        if hashlib.sha256(self.project.model_dump_json().encode()).hexdigest() != self.report.get("source_snapshot_sha256"):
            raise DocumentError("Снимок комплекта был изменён после расчёта; постройте его заново")

    def index(self):
        self._check_snapshot()
        return {"project_id": self.project.id, "project_revision": self.project.revision,
                "generator_version": GENERATOR_VERSION, "format": self.paper_format,
                "status": "draft_not_for_construction", "readiness": self.report["readiness"],
                "sheets": [{"number": i, "sheet_number": None if s.kind == "cover" else i - (self.sheets[0].kind == "cover"),
                            "title": s.title, "kind": s.kind,
                            "paper_format": "A2" if s.width == 594 else "A3",
                            "width_mm": s.width, "height_mm": s.height,
                            "layout": s.layout,
                            "svg_url": f"/api/v1/projects/{self.project.id}/sheets/{i}.svg?format={self.paper_format}&revision={self.project.revision}"}
                           for i, s in enumerate(self.sheets, 1)]}

    def package(self):
        self._check_snapshot()
        files = {"documentation.pdf": self.pdf(), "documentation.dxf": self.dxf(),
                 "project.json": json_bytes(self.project.model_dump(mode="json")),
                 "engineering.json": json_bytes(self.report), "sheets.json": json_bytes(self.index()),
                 "panel-pieces.csv": self.pieces_csv(),
                 "README.txt": ("КОМПЛЕКТ ЧЕРТЕЖЕЙ И ДАННЫХ ПРОЕКТА\n"
                     "SVG/PDF/DXF построены из одного снимка project.json. Файлы исходников хранятся отдельно в data/sources.\n"
                     "documentation.dxf: весь альбом, отдельная вкладка листа для каждой страницы PDF и обзор в Model.\n"
                     "Геометрия листов в миллиметрах бумаги; реальные размеры видов указаны размерами и масштабами.\n"
                     "Линии, контуры, заливки, подписи и размеры в DXF редактируются; растровых страниц нет.\n"
                     "Правки в AutoCAD не синхронизируются обратно с моделью проекта.\n"
                     "Для одинаковых шрифтов установите Noto Sans из fonts/; лицензия OFL включена.\n"
                     "Для полного переноса базы, истории и исходников используйте команду backup-workspace.\n"
                     "Конкретные вопросы и недостающие данные приведены в замечаниях и engineering.json.\n"
                     "whole-walls/: полные сборки облицовки с каркасом, крепежом и анкеровкой; количества относятся к сохранённым сторонам стен.\n"
                     "Раскладка предварительная: вычтены только явно исключённые проёмы; невырезанные проёмы выделены как ошибки.\n"
                     "Прямоугольные части не являются картой раскроя.\n"
                     "sections/: сохранённые сечения STEP в JSON и DXF 1:1 (мм), с отдельными слоями тел.\n"
                     "Отдельные сечения состоят из приближённых отрезков и сами по себе не определяют монтажную сборку.\n"
                     "assemblies/: реальные геометрические сборки STEP-профилей с параметрическими панелями.\n"
                     "Для каждой актуальной сборки сохранены STEP тел, DXF разреза 1:1 (мм) и JSON с составом, посадками и источниками.\n"
                     "Устаревшие сборки исключены из листов и перечислены в assemblies/index.json.\n"
                     "Штриховки показывают фактические замкнутые сечения тел; панели являются геометрическими образцами, не заготовками.\n"
                     "Посадки и параметрические анкеры проектные. Проверки геометрии не являются расчётом несущей способности.\n"
                     "Контрольные суммы сечений проверяют целостность, но не подтверждают авторство и применимость.\n"
                     "special-assemblies/: STEP специальных изделий, каждый сохранённый разрез в DXF 1:1 и JSON, состав и проектные допущения.\n"
                     "Параметрические двери, люки и подсветка не подменяют каталожные изделия; происхождение каждого тела указано в ведомости.\n"
                     "Печать PDF: фактический размер 100%, без подгонки к странице.\n").encode()}
        from pathlib import Path
        font_dir = Path(__file__).parent / "fonts"
        for name in ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "OFL.txt"):
            files[f"fonts/{name}"] = (font_dir / name).read_bytes()
        vendor = self.report.get("vendor_reference")
        if vendor and vendor.get("attached"):
            files["vendor-reference.json"] = json_bytes(self.project.properties.get("vendor_design", {}).get("snapshot"))
            files["vendor-review.json"] = json_bytes(vendor)
        if self.report.get("wall_mounting"):
            files["wall-mounting.json"] = json_bytes(self.report["wall_mounting"])
            files["wall-mounting-reference.json"] = json_bytes(self.project.properties["wall_mounting"]["snapshot"])
            files["wall-mount-panels.csv"] = self.wall_mount_csv("panels")
            files["wall-mount-junctions.csv"] = self.wall_mount_csv("nodes")
        for index, sheet in enumerate(self.sheets, 1):
            files[f"sheets/{index:03d}.svg"] = sheet.svg()
        from .cad_sections import entries, section_dxf_bytes
        section_files = []
        for index, (asset, section) in enumerate(entries(self.project), 1):
            snapshot = section.geometry_snapshot
            entry = {"number": index, "section_id": section.id, "source_id": asset.source_id,
                     "filename": asset.filename, "engineering_approved": False,
                     "geometry_sha256": snapshot.geometry_sha256 if snapshot else None,
                     "status": "geometry_saved" if snapshot else "geometry_missing"}
            if snapshot:
                prefix = f"sections/{index:03d}"
                entry.update(snapshot_path=prefix + ".json", dxf_path=prefix + ".dxf")
                files[entry["snapshot_path"]] = json_bytes(section.model_dump(mode="json", by_alias=True))
                files[entry["dxf_path"]] = section_dxf_bytes(snapshot)
            section_files.append(entry)
        if section_files:
            files["sections/index.json"] = json_bytes(section_files)
        from ..mount_assemblies import assembly_usage
        active_assemblies, unused_assemblies = assembly_usage(self.project, self.report.get("wall_mounting"), saved=self._validated_nodes, server_owned=self.derived_assemblies)
        assembly_index = []
        for index, record in enumerate(active_assemblies.values(), 1):
            prefix = f"assemblies/{index:03d}"
            files[prefix + ".step"] = record.assembled_step.content()
            files[prefix + ".dxf"] = section_dxf_bytes(record.geometry_snapshot)
            files[prefix + ".json"] = json_bytes(record.model_dump(mode="json", by_alias=True))
            item = {"id":record.id,"label":record.label,"step_path":prefix+".step","dxf_path":prefix+".dxf",
                    "snapshot_path":prefix+".json","status":"actual_geometry_unreviewed_placement"}
            if record.fastening_checks:
                checks_path = prefix + "-checks.json"
                files[checks_path] = json_bytes({"schema_version":"assembly-geometric-checks/1.0",
                    "assembly_id":record.id,"assembled_step_sha256":record.assembled_step.source_id,
                    "units":{"length":"mm","area":"mm2","volume":"mm3"},
                    "fastening_checks":record.fastening_checks,"contact_checks":record.contact_checks or [],
                    "intentional_engagements":record.intentional_engagements,
                    "unexpected_collisions":record.collisions,
                    "scope":"local_3d_geometry_not_load_calculation_or_building_anchor_design"})
                item.update(status="fastened_3d_geometry_verified",checks_path=checks_path,
                            fasteners=len(record.fastening_checks),contacts=len(record.contact_checks or []))
            assembly_index.append(item)
        if assembly_index or unused_assemblies:
            files["assemblies/index.json"] = json_bytes({"active":assembly_index,"excluded_stale_ids":[item.id for item in unused_assemblies],
                "notice":"Реальные STEP-детали и параметрические панели. Для сборок с крепежом сохранены проверки посадок, опорных контактов и допустимых зацеплений стержня. Количество относится к местному узлу, не к закупочной ведомости здания; прочность и анкеровка к стене не рассчитаны."})
        if active_assemblies:
            from .wall_mount import build_mount_catalog
            from .node_construction import construction_graphics
            catalog = build_mount_catalog(self.project, self.report.get("wall_mounting") or {}, assembly_records=self._validated_nodes, server_owned=self.derived_assemblies)
            contexts = []
            for entry in catalog.values():
                if not entry.get("assembly"):
                    continue
                graphics = construction_graphics(entry)
                symbols = entry.get("context_symbols", {})
                used_sources = {part["source_id"] for part in graphics["parts"] if part["origin"] == "context" and part["source_id"]}
                contexts.append({"mark": entry["mark"], "assembly_id": entry["assembly"].id,
                    "parts": [part for part in graphics["parts"] if part["origin"] == "context"],
                    "sources": [value for value in symbols.values() if value["source_id"] in used_sources]})
            files["mount-drawing-context.json"] = json_bytes({"schema_version": "mount-drawing-context/1.0",
                "units": "mm", "coordinates": "local section XY, before paper placement",
                "placement": "project_illustration_not_validated_3d_assembly", "included_in_quantities": False,
                "notice": "К: только элементы окружения разреза, перечисленные в parts. В новых 3D-сборках крепёж, каркас, подоснова и клеевые участки находятся в приложенном STEP и не дублируются окружением. Фрагменты пола и отметки потолка не задают фактическую толщину перекрытия; контекст не входит в закупочный расчёт.",
                "details": contexts})
        from .special_sections import package_special
        special_index=package_special(self.project,files,json_bytes,derived_assemblies=self.derived_assemblies)
        from ..whole_wall_assemblies import whole_wall_usage
        whole_walls, stale_whole_walls = self._validated_wall_usage if self._validated_wall_usage is not None else whole_wall_usage(self.project, server_owned=self.derived_assemblies)
        wall_index = []
        for index, record in enumerate(whole_walls, 1):
            prefix = f"whole-walls/{index:03d}"
            files[prefix+".step"] = record.assembled_step.content()
            files[prefix+".json"] = json_bytes(record.model_dump(mode="json"))
            files[prefix+"-checks.json"] = json_bytes({key:record.geometry.get(key) for key in
                ("checks", "fastening_checks", "anchor_checks", "contact_checks", "collisions", "intentional_engagements")})
            coordinates = StringIO()
            writer = csv.writer(coordinates, delimiter=";")
            writer.writerow(["kind", "id", "member_id", "x_mm", "z_mm", "y_mm"])
            positions = {check["fastener_id"]:check["axis_origin_mm"] for check in record.geometry["fastening_checks"]}
            anchor_positions = {check["id"]:check["axis_origin_mm"] for check in record.geometry["anchor_checks"]}
            for kind in ("screws", "anchors", "profile_screws"):
                for point in record.plan.get(kind, []):
                    axis = anchor_positions[point["id"]] if kind == "anchors" else positions["screw-"+point["id"]]
                    writer.writerow([kind,point["id"],point.get("post_id",point.get("profile_id","")),point["x_mm"],point["z_mm"],axis[1]])
            files[prefix+"-coordinates.csv"] = coordinates.getvalue().encode("utf-8-sig")
            entry = {"id":record.id,"wall_id":record.wall_id,"face_id":record.face_id,
                "label":record.label,"step_path":prefix+".step","record_path":prefix+".json",
                "checks_path":prefix+"-checks.json","coordinates_path":prefix+"-coordinates.csv",
                "screw_count":len(record.geometry["fastening_checks"]),"anchor_count":len(record.plan["anchors"])}
            if record.plan.get("system"):
                entry.update(system=record.plan["system"], frame_screw_count=len(record.plan["screws"]),
                             profile_screw_count=len(record.plan.get("profile_screws", [])))
                files[prefix+"-profiles.json"] = json_bytes({"units":"mm", "system":record.plan["system"],
                    "runs":record.plan.get("profile_runs", []), "machining":record.geometry.get("machining", []),
                    "source_models":record.source_models})
                entry["profiles_path"] = prefix+"-profiles.json"
            wall_index.append(entry)
        if wall_index or stale_whole_walls:
            files["whole-walls/index.json"] = json_bytes({"active":wall_index,"excluded_stale":stale_whole_walls,
                "coordinates":"X along the elevation, Y into the substrate, Z upwards; mm",
                "scope":"Independent face assemblies. Geometric checks do not calculate loads or certify an anchor product."})
        manifest = {"format": "rd-package/1.0", "generator_version": GENERATOR_VERSION,
                    "project_id": self.project.id, "revision": self.project.revision,
                    "status": "draft_not_for_construction", "source_snapshot_sha256": self.report["source_snapshot_sha256"],
                    "project_json_sha256": hashlib.sha256(files["project.json"]).hexdigest(),
                    "cad_sections": section_files,
                    "special_assemblies":special_index,
                    "whole_wall_assemblies":wall_index,
                    "files": [{"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                              for name, content in sorted(files.items())]}
        files["manifest.json"] = json_bytes(manifest)
        output = BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for name, content in sorted(files.items()):
                info = ZipInfo(name, (2000, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, content)
        return output.getvalue()

    def wall_mount_csv(self, kind):
        """Geometry takeoff, not purchase/fabrication quantities. Escape formulas."""
        stream = StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        def cell(value):
            text = str(value if value is not None else "")
            return "'"+text if text.lstrip().startswith(("=","+","-","@")) else text
        if kind == "panels":
            writer.writerow(["view","wall_id","face_id","panel_id","piece_index","x_mm","y_mm","width_mm","height_mm","thickness_mm","area_m2","status"])
        else:
            writer.writerow(["view","wall_id","face_id","node_id","kind","x_mm","y_mm","rule_id","status"])
        for index, preview in enumerate(self.report.get("wall_mounting",{}).get("walls",[]),1):
            prefix=[f"М{index:03d}",preview["wall"]["id"],preview["wall"]["face_id"]]
            for item in preview.get(kind,[]):
                if kind == "panels":
                    row=prefix+[item["panel_id"],item["index"],item["x_mm"],item["y_mm"],item["width_mm"],item["height_mm"],preview["configuration"]["panel_thickness_mm"],item["area_m2"],"geometry_not_fabrication_blank"]
                else:
                    row=prefix+[item["id"],item["kind"],item["x_mm"],item["y_mm"],item.get("rule_id"),item.get("decision_status",item["status"])]
                writer.writerow([cell(value) for value in row])
        return ("\ufeff"+stream.getvalue()).encode("utf-8")

    def pieces_csv(self):
        stream = StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["zone_id", "panel_id", "piece_id", "x_mm", "bottom_mm", "width_mm", "height_mm", "area_mm2", "status"])
        # IDs are generator-controlled; numeric values only, no formula injection.
        for layout in self.report["layouts"]:
            for panel in layout.get("panels", []):
                for piece in panel.get("material_pieces", []):
                    writer.writerow([layout["zone_id"], panel["id"], piece["id"],
                                     *[num(piece[k]) for k in ("x_mm", "y_mm", "width_mm", "height_mm", "area_mm2")], "DRAFT"])
        return ("\ufeff" + stream.getvalue()).encode("utf-8")


def build_documents(project: Project, *, paper_format="A3", report=None, derived_assemblies=False) -> DocumentSet:
    from ..engineering import build_engineering
    if paper_format not in FORMATS:
        raise DocumentError("Допустимы форматы ALBUM, A3 и A2")
    project = Project.model_validate_json(project.model_dump_json())
    from ..workspace import validate_workspace
    whole_walls, stale_whole_walls, node_records = validate_workspace(
        project,derived_assemblies=derived_assemblies,resolve_whole_walls=True,resolve_node_records=True)
    if report is not None:
        if (report.get("schema_version") != "engineering-report/1.0" or report.get("project_id") != project.id
            or report.get("project_revision") != project.revision
            or report.get("source_snapshot_sha256") != hashlib.sha256(project.model_dump_json().encode()).hexdigest()):
            raise DocumentError("Расчёт относится к другому снимку проекта. Пересчитайте документацию.")
        report = deepcopy(report)
    else:
        report = build_engineering(project, catalog=project.properties.get("node_catalog"))
    if not report["readiness"]["geometry_valid"]:
        raise DocumentError("Перед построением листов исправьте ошибки геометрии")
    width, height = FORMATS[paper_format]
    sheets = []
    names = {}
    floors = []
    for building in project.buildings:
        names[building.id] = building.name
        for floor in building.floors:
            floors.append(floor)
            names[floor.id] = floor.name
            for space in floor.spaces:
                names[space.id] = f"{space.code} / {space.name}"
            for wall in floor.walls:
                names[wall.id] = wall.name
            for zone in floor.cladding_zones:
                names[zone.id] = zone.name
            for opening in floor.openings:
                names[opening.id] = opening.name
            for obstacle in floor.obstacles:
                names[obstacle.id] = obstacle.name
    for system in project.panel_systems:
        names[system.id] = system.name
    elev_marks = {e["id"]: f"Р{n:02d}" for n, e in enumerate(report["elevations"], 1)}
    zone_marks = {l["zone_id"]: f"З{n:02d}" for n, l in enumerate(report["layouts"], 1)}
    layouts = {l["zone_id"]: l for l in report["layouts"]}
    requirements = report.get("node_requirements", [])
    node_marks = {r["id"]: f"У{n:03d}" for n, r in enumerate(requirements, 1)}

    def new(title, kind):
        if len(sheets) >= MAX_SHEETS:
            raise DocumentError(f"Больше {MAX_SHEETS} листов. Выпускайте комплект по частям.")
        s = Sheet(title, kind, width, height)
        sheets.append(s)
        return s

    def table(title, headers, rows, proportions, kind="table"):
        if not rows and kind in {"schedule","nodes"} and report.get("wall_mounting",{}).get("walls"):
            return  # The mounting schedules replace unused legacy-zone tables.
        maximum = width - 40
        widths = [maximum * p / sum(proportions) for p in proportions]
        sheet = None
        y = height
        for row in rows or [["Нет данных"] + [""] * (len(headers) - 1)]:
            lines = [wrap(value, col_width - 4, 2.9) for value, col_width in zip(row, widths)]
            row_height = max(map(len, lines)) * 4.6 + 4
            bottom_margin = 73 if paper_format == "ALBUM" else 55
            if row_height > height - bottom_margin - 53:
                raise DocumentError("Текст строки превышает доступный лист. Сократите примечание; полный текст сохранён в модели.")
            if y + row_height > height - bottom_margin:
                sheet = new(title, kind)
                y = 39
                x = 20
                for header, col_width in zip(headers, widths):
                    sheet.rect(x, y - 6, col_width, 10, GRAY, LIGHT, .15)
                    sheet.text(x + 2, y, header, 2.9, bold=True)
                    x += col_width
                y += 4
            x = 20
            for cell, col_width in zip(lines, widths):
                sheet.rect(x, y, col_width, row_height, GRAY, weight=.12)
                for i, line in enumerate(cell):
                    sheet.text(x + 2, y + 5 + i * 4.6, line, 2.9)
                x += col_width
            y += row_height

    cover = new("Общие данные", "general")
    y = cover.paragraph(23, 49, project.metadata.name, width - 50, size=8, bold=True, leading=11)
    y = cover.paragraph(23, y + 7, project.metadata.address or "Адрес объекта не задан", width - 50, size=3.8)
    cover.rect(23, y + 5, width - 46, 22, GRAY, LIGHT, .2)
    cover.text(28, y + 14, "ВНУТРЕННЯЯ ОТДЕЛКА СТЕН / ПЛАНЫ, РАЗВЁРТКИ И УЗЛЫ", 4, INK, True)
    cover.text(28, y + 22, f"Стадия {project.metadata.stage}. Версия модели {project.revision}. Дата {project.updated_at.date().isoformat()}.", 3.1)
    y += 41
    facts = [f"Зданий: {len(project.buildings)}. Этажей: {len(floors)}. Развёрток: {len(report['elevations'])}. Зон облицовки: {len(report['layouts'])}.",
             "Размеры в миллиметрах; отметки - в метрах. На видах Рxx отметки абсолютные, на монтажных видах Мxxx - от пола этажа.",
             "Контуры помещений идут по опорным линиям стен. Это не размеры и не площади по чистовой отделке.",
             "Синие линии - расчётная облицовка. Чёрные - основа. Красные надписи - нерешённые вопросы.",
             "Вычитаются только явно исключённые проёмы. Остальные пересечения с облицовкой требуют исправления.",
             "Ведомость содержит геометрические фрагменты облицовки; технологические припуски задаются отдельно.",
             "Принятые решения, недостающие данные и необходимые проверки приведены в замечаниях.",
             "PDF и SVG имеют фиксированный физический масштаб. Печатать 100%, без подгонки."]
    for fact in facts:
        y = cover.paragraph(23, y, fact, width - 48, 3.4, leading=5.2) + 3
    if y > height - 57:
        raise DocumentError("Общие данные не помещаются: используйте A2 или сократите название/адрес")

    # Reserve a contents sheet; populated after all pages and numbering exist.
    contents = new("Содержание комплекта", "contents")

    from .openings import opening_marks, opening_schedule, draw_plan_opening
    from .plan_walls import wall_footprint, draw_wall_footprint
    from .plan_dimensions import draw_plan_overall_dimension
    opening_labels = opening_marks(project)
    for floor in floors:
        s = new(f"План / {floor.name}", "plan")
        if not floor.walls:
            s.text(25, 60, "На этаже пока нет стен. Постройте или импортируйте контуры.", 4, GRAY)
            continue
        joined = wall_footprint(floor, openings=False)
        minx, miny, maxx, maxy = joined.bounds
        scale = scale_for(maxx - minx, maxy - miny, width - 115, height - 185)
        ox, oy = 52 + ((width - 115) - (maxx - minx) / scale) / 2, 68
        transform = lambda x, y: (ox + (x - minx) / scale, oy + (maxy - y) / scale)
        s.text(23, 38, f"М 1:{scale}. Отметка этажа {num(floor.elevation_mm)} мм. Размеры по граням стен, без отделки.", 3.2, GRAY)
        walls = {w.id: w for w in floor.walls}
        draw_wall_footprint(s,wall_footprint(floor),transform,INK,LIGHT)
        for opening in floor.openings:
            wall = walls[opening.wall_id]
            draw_plan_opening(s, opening, wall, transform, scale, opening_labels[opening.id],void_already_cut=True,draw_label=False)
        for obstacle in floor.obstacles:
            s.polygon([transform(p.x, p.y) for p in obstacle.footprint], GRAY, "#dde3e6", .25)
        marked_faces=set()
        plan_view_labels=[]
        for e in report["elevations"]:
            if e["floor_id"] != floor.id:
                continue
            wall = walls[e["wall_id"]]
            mounting_marks=[f"М{i:03d}" for i,p in enumerate(report.get("wall_mounting",{}).get("walls",[]),1)
                            if p["wall"]["id"]==wall.id and p["wall"]["side"]==e["face_side"]]
            # The album replaces an unzoned legacy elevation with its mounting
            # view. Do not leave an Rxx link to a drawing removed by composition.
            marks = mounting_marks if paper_format == "ALBUM" and mounting_marks and not e.get("zones") else [elev_marks[e["id"]], *mounting_marks]
            plan_view_labels.append((wall.id,e["face_side"]," / ".join(marks)))
            marked_faces.add((wall.id,e["face_side"]))
        # Interior freestanding partitions have no canonical room elevation,
        # but both assigned mounting faces still need links on this plan.
        for index,preview in enumerate(report.get("wall_mounting",{}).get("walls",[]),1):
            record=preview["wall"]
            if record["floor_id"]!=floor.id or (record["id"],record["side"]) in marked_faces:continue
            wall=walls[record["id"]]
            plan_view_labels.append((wall.id,record["side"],f"М{index:03d}"))
        _plan_dimensions(s, floor, transform, scale)
        draw_plan_overall_dimension(s, joined, transform)
        from .plan_labels import draw_plan_labels, draw_space_labels
        draw_plan_labels(s,floor,transform,scale,opening_labels,plan_view_labels)
        draw_space_labels(s,floor,transform)
        s.text(23, height - 68, "Рxx - развёртка; Мxxx - монтаж. Размеры плана - по изображённым граням стен, без отделки.", 3, GRAY)
        s.text(23, height - 61, "Цепочки проёмов отсчитываются от видимых углов. Габарит включает толщину стен и угловые сопряжения.", 3, GRAY)

    elevations = report["elevations"]
    for start in range(0, len(elevations), 2):
        s = new("Развёртки / " + ", ".join(elev_marks[e["id"]] for e in elevations[start:start + 2]), "elevations")
        row_height = (height - (108 if paper_format == "ALBUM" else 89)) / 2
        for row, e in enumerate(elevations[start:start + 2]):
            y = 39 + row * row_height
            title = f"{elev_marks[e['id']]} / {names.get(e['space_id'], '')} / {names.get(e['wall_id'], '')}"
            title_lines = wrap(title, width - 46, 3.5, True)
            if len(title_lines) > 2:
                raise DocumentError("Название развёртки слишком длинное. Сократите название помещения/стены.")
            for j, line in enumerate(title_lines):
                s.text(23, y + j * 5, line, 3.5, BLUE, True)
            scale = scale_for(e["width_mm"], e["wall_height_mm"], width - 99, row_height - 38)
            x0 = 55
            top = y + 13
            floor_bottom = e["wall_base_mm"]
            bottom = top + e["wall_height_mm"] / scale
            transform = lambda x, z: (x0 + x / scale, bottom - (z - floor_bottom) / scale)
            s.text(width - 23, y, f"М 1:{scale}", 3, GRAY, anchor="end")
            s.rect(x0, top, e["width_mm"] / scale, e["wall_height_mm"] / scale, INK, "#f6f8fa", .25)
            for zone in e.get("zones", []):
                layout = layouts.get(zone["id"])
                if not layout:
                    continue
                for panel in layout.get("panels", []):
                    for piece in panel.get("material_pieces", []):
                        x, py = transform(piece["x_mm"], piece["y_mm"] + piece["height_mm"])
                        s.rect(x, py, piece["width_mm"] / scale, piece["height_mm"] / scale, BLUE, "#e9f2fb", .18)
                    px, _ = transform(panel["x_mm"] + panel["width_mm"] / 2, floor_bottom)
                    if len(e.get("zones", [])) == 1 and panel["width_mm"] / scale >= text_width(f"П{panel['index'] + 1}", 2.3) + 1:
                        s.text(px, top + 4, f"П{panel['index'] + 1}", 2.3, BLUE, anchor="middle")
                    if len(e.get("zones", [])) == 1 and panel["width_mm"] / scale >= text_width(num(panel["width_mm"]), 2.8) + 1:
                        left, _ = transform(panel["x_mm"], floor_bottom)
                        s.dimension(left, left + panel["width_mm"] / scale, top - 3, top,
                                    num(panel["width_mm"]), BLUE)
                if not layout.get("valid"):
                    r = layout["zone_rect"]
                    zx, zy = transform(r["x_mm"], r["y_mm"] + r["height_mm"])
                    s.rect(zx, zy, r["width_mm"] / scale, r["height_mm"] / scale, RED, weight=.4)
            if len(e.get("zones", [])) == 1:
                zone = e["zones"][0]
                layout = layouts.get(zone["id"])
                if layout:
                    note = "Расчётная раскладка" if layout.get("valid") else "ТРЕБУЕТ ИСПРАВЛЕНИЯ / см. проверки"
                    s.text(x0, bottom + 25, f"{zone_marks[zone['id']]}: {len(layout.get('panels', []))} полос; шов {num(layout['constraints']['joint_mm'])} мм. {note}.", 2.8, BLUE if layout.get("valid") else RED)
            elif e.get("zones"):
                s.text(x0, bottom + 25, f"Зон: {len(e['zones'])}. Все размеры и привязки - в ведомости зон и полос для {elev_marks[e['id']]}.", 2.8, BLUE)
            for opening in e.get("openings", []):
                x, py = transform(opening["view_offset_mm"], opening["top_mm"])
                ow, oh = opening["width_mm"] / scale, opening["height_mm"] / scale
                unexcluded = False
                for zone in e.get("zones", []):
                    l = layouts.get(zone["id"])
                    if not l:
                        continue
                    r = l["zone_rect"]
                    overlaps = (min(opening["view_offset_mm"] + opening["width_mm"], r["x_mm"] + r["width_mm"]) > max(opening["view_offset_mm"], r["x_mm"])
                                and min(opening["top_mm"], r["y_mm"] + r["height_mm"]) > max(opening["bottom_mm"], r["y_mm"]))
                    if overlaps and opening["id"] not in {v["id"] for v in l.get("excluded_openings", [])}:
                        unexcluded = True
                s.rect(x, py, ow, oh, RED if unexcluded else INK, None if unexcluded else "#ffffff", .3)
                s.line(x, py, x + ow, py + oh, GRAY, .15)
                s.line(x, py + oh, x + ow, py, GRAY, .15)
                if ow >= 17:
                    s.text(x + ow / 2, py + oh / 2, num(opening["width_mm"]), 2.7, INK, anchor="middle")
                if unexcluded:
                    s.text(x + 1, py + oh - 2, "НЕ ВЫЧТЕН", 2.6, RED, True)
            view_requirements = [(r, xx, zz) for r in requirements for eid, xx, zz in node_locations(r) if eid == e["id"]]
            if len(view_requirements) <= 16:
                label_x = min(width - 40, x0 + e["width_mm"] / scale + 22)
                for ni, (requirement, xx, zz) in enumerate(view_requirements):
                    nx, ny = transform(xx, zz)
                    label_y = top + 5 + ni * 4.3
                    s.circle(nx, ny, .6, RED, "#ffffff", .15)
                    s.line(nx + 1, ny, label_x - 2, label_y - 1, "#b98a85", .1)
                    s.text(label_x, label_y, node_marks[requirement["id"]], 2.5, RED)
            elif view_requirements:
                s.text(x0, bottom + 20, f"Мест примыкания: {len(view_requirements)}. Координаты всех мест - в реестре узлов.", 2.8, RED)
            s.dimension(x0, x0 + e["width_mm"] / scale, bottom + 12, bottom, num(e["width_mm"]), INK)
            s.text(x0 - 4, bottom + 1, f"абс. {e['absolute_bottom_mm'] / 1000:+.3f} м", 2.8, GRAY, anchor="end")
            s.text(x0 - 4, top + 1, f"абс. {e['absolute_top_mm'] / 1000:+.3f} м", 2.8, GRAY, anchor="end")
            from .levels import draw_levels, view_levels
            level_notes=draw_levels(s,view_levels(project,e["floor_id"],e["space_id"]),
                lambda xx,zz:transform(xx,zz+e["wall_base_mm"]),e["width_mm"],e["wall_base_mm"],e["wall_height_mm"],absolute_offset_mm=e["floor_elevation_mm"])
            if level_notes:
                s.paragraph(x0, bottom+30,"; ".join(level_notes),width-x0-23,2.5,GRAY)
            if row == 0:
                s.line(23, 39 + row_height - 5, width - 23, 39 + row_height - 5, GRAY, .12)
            s.references.append(e["id"])

    panel_rows = []
    for layout in report["layouts"]:
        for panel in layout.get("panels", []):
            panel_rows.append([zone_marks[layout["zone_id"]], f"П{panel['index'] + 1}", num(panel["width_mm"]), num(panel["height_mm"]),
                               num(panel["stock_width_mm"]), str(len(panel.get("material_pieces", []))),
                               num(panel["net_area_mm2"] / 1e6), "Проверить раскрой" if len(panel.get("material_pieces", [])) > 1 else "Расчёт"])
    table("Ведомость расчётных полос (не заказная спецификация)", ["Зона", "Полоса", "Ширина, мм", "Высота, мм", "Модуль, мм", "Частей", "Нетто, м²", "Примечание"],
          panel_rows, [1, 1, 1.5, 1.5, 1.5, 1, 1.4, 2.3], "schedule")
    table("Зоны и системы облицовки", ["Марка / вид", "Зона", "Система", "Размер, мм", "X; низ, мм", "Статус"],
          [[f"{zone_marks[l['zone_id']]} / {elev_marks.get(l['elevation_id'], '')}", names.get(l["zone_id"], ""), names.get(l["panel_system_id"], ""),
            f"{num(l['zone_rect']['width_mm'])} x {num(l['zone_rect']['height_mm'])}",
            f"{num(l['zone_rect']['x_mm'])}; {num(l['zone_rect']['y_mm'])}", "Предварительно" if l.get("valid") else "Есть ошибки"] for l in report["layouts"]], [1.5, 3, 3, 2, 1.5, 2], "schedule")
    node_rows = []
    for i, requirement in enumerate(requirements, 1):
        locations = node_locations(requirement)
        candidates = requirement.get("candidates", [])
        node_rows.append([f"У{i:03d}", "\n".join(elev_marks[eid] for eid, _, _ in locations), CONTEXT_NAMES.get(requirement["kind"], requirement["kind"]),
                          "\n".join(f"{num(xx)}; {num(zz)}" for _, xx, zz in locations),
                          ", ".join(c["node_id"] for c in candidates) or "Не найден",
                          "Требуется выбор и подтверждение"])
    table("Реестр необходимых узлов", ["Место", "Развёртка", "Примыкание", "X; отметка, мм", "Кандидаты библиотеки", "Решение"], node_rows, [1, 1.3, 2.1, 2, 3, 3], "nodes")

    vendor = report.get("vendor_reference")
    if vendor and vendor.get("attached"):
        def excerpt(value, maximum=650):
            value = str(value)
            return value if len(value) <= maximum else value[:maximum] + "... Полный текст: vendor-review.json."

        def reference_label(source):
            return f"{source['filename']}, стр. {source['page']}\nSHA256: {source['sha256']}"

        table("Выбранные сборки методичек",
              ["Зона", "Монтаж", "Выбранная сборка", "Первоисточник", "Статус"],
              [[zone_marks.get(b["zone_id"], b["zone_id"]), "Открытый" if b["system"] == "open_mount" else "Скрытый",
                excerpt(b["title"], 400), reference_label(b["source"]), "Сопоставить с условиями примыкания."]
               for b in vendor.get("bindings", [])], [1, 1.1, 3.3, 3.5, 2.1], "vendor_assemblies")
        vendor_rows = []
        for binding in vendor.get("bindings", []):
            mark = zone_marks.get(binding["zone_id"], binding["zone_id"])
            vendor_rows.append([mark, "Обозначенные компоненты", excerpt(", ".join(binding["component_codes"]) or "Нет артикульных выносок"),
                                "Соответствие одноимённым профилям других альбомов и 3D-моделям не подтверждено."])
            for requirement in binding["requirements"]:
                vendor_rows.append([mark, excerpt(requirement["title"], 180) + "\n" + reference_label(requirement["source"]),
                                    excerpt(requirement["requirement"]), excerpt(requirement["scope"], 400)])
        table("Условные требования выбранных сборок", ["Зона", "Выписка / источник", "Требование", "Граница применимости"],
              vendor_rows, [1, 3.4, 5, 3.6], "vendor_requirements")
        check_names = {"matches_transcribed_value": "Число совпадает; применимость проверяется отдельно", "mismatch": "Несоответствие",
                       "blocked": "Проверка заблокирована", "not_specified": "В сборке нет числового правила"}
        check_rows = []
        for binding in vendor.get("bindings", []):
            for check in binding["checks"]:
                bounds = "Не задано" if "minimum_mm" not in check else f"{num(check['minimum_mm'])} - {num(check['maximum_mm'])} мм"
                check_rows.append([zone_marks.get(binding["zone_id"], binding["zone_id"]), "Межпанельный шов",
                                   "Не задано" if check.get("actual_mm") is None else num(check["actual_mm"]) + " мм",
                                   bounds, check_names.get(check["status"], check["status"])])
        table("Сверка размеров с методичками", ["Зона", "Параметр", "В модели", "По сборке", "Результат"],
              check_rows, [1, 2, 1.6, 2, 4], "vendor_checks")

    # No construction geometry is invented. Only explicitly supplied primitives
    # are illustrated, with a prominent candidate/unapproved label.
    catalog = report.get("catalog") or project.properties.get("node_catalog")
    if catalog:
        from .catalog import preflight_catalog
        validation = preflight_catalog(catalog)
        if not validation["valid"]:
            raise DocumentError("Каталог не может быть выведен на листы: " + str(validation["issues"][:2]))
        if validation["valid"]:
            nodes = validation["normalized"]["nodes"]
            candidate_ids = {c["node_id"] for r in requirements for c in r.get("candidates", [])}
            for node in nodes:
                if node["id"] in candidate_ids:
                    _node_sheet(new, node, width, height)

    from .cad_sections import add_section_sheets
    add_section_sheets(project, new, table, names, width, height, num)
    if paper_format != "ALBUM":
        # The compact composer draws its own mounting views, details and
        # schedules. Legacy copies would be built here only to be discarded.
        from .wall_mount import add_wall_mount_sheets
        add_wall_mount_sheets(report.get("wall_mounting", {}), new, table, width, height, num, project=project, whole_walls=whole_walls,assembly_records=node_records,server_owned=derived_assemblies)

    opening_rows = opening_schedule(project, num)
    if opening_rows:
        table("Ведомость дверей, окон и проёмов", ["Марка", "Этаж / помещение", "Вид / наименование", "Ш x В, мм", "Абс. низ, мм", "Абс. верх, мм", "Примечание"],
              opening_rows, [1, 2, 2, 1.4, 1.2, 1.2, 3.2], "openings")

    measurement_properties = {"length_mm": "Длина", "width_mm": "Ширина", "height_mm": "Высота", "thickness_mm": "Толщина", "elevation_mm": "Отметка"}
    measurement_statuses = {"raw": "Исходный", "accepted": "Принят", "rejected": "Отклонён", "superseded": "Заменён"}
    table("Реестр замеров и источников", ["Объект", "Параметр", "Значение, мм", "Источник", "Статус"],
          [[names.get(m.target_id, m.target_id), measurement_properties.get(m.property_name, m.property_name), num(m.value_mm),
            "; ".join(value for value in (m.source, m.author) if value), measurement_statuses.get(m.status, m.status)] for m in project.measurements], [3, 2, 1.7, 4, 1.5], "measurements")
    issues = report.get("issues", [])
    table("Проверка готовности / открытые вопросы", ["Код", "Уровень", "Объект / зона", "Описание"],
          [[i["code"], "Ошибка" if i["severity"] == "error" else "Внимание",
            "; ".join(dict.fromkeys(zone_marks.get(eid, names.get(eid, "")) for eid in i.get("entity_ids", []) if eid in names or eid in zone_marks)) or "Комплект",
            i["message"]] for i in issues], [3, 1.2, 2, 6], "issues")

    from .special_sections import special_entries,section_entries,draw_special_view,add_special_tables
    special=special_entries(project,derived_assemblies=derived_assemblies)
    if special:
        report["special_products"]={"schema_version":"special-document-report/1.0","entries":[
            {k:entry[k] for k in ("id","mark","label","kind","wall_id","face_id","opening_id","status","message")}
            for entry in special]}
    if paper_format != "ALBUM":
        from .whole_wall import add_whole_wall_tables
        wall_marks = {(view["wall"]["id"],view["wall"]["face_id"]):f"М{index:03d}" for index,view in enumerate(report.get("wall_mounting",{}).get("walls",[]),1)}
        add_whole_wall_tables(whole_walls, table, num, wall_marks)
        if stale_whole_walls:
            table("Сборки стен, требующие пересчёта", ["Стена", "Причина"],
                  [[item["label"], item["message"]] for item in stale_whole_walls], [2,8], "whole_wall_stale")
        for entry in section_entries(special):
            sheet=new(entry["mark"]+" / "+entry["special"]["label"],"special_section")
            draw_special_view(sheet,entry,(23,34,width-46,height-107),num)
        add_special_tables(special,table,num)
    if paper_format == "ALBUM":
        from .album import compose_album
        try:
            sheets = compose_album(sheets, project, report, whole_walls, stale_whole_walls,derived_assemblies=derived_assemblies,assembly_records=node_records)
        except ValueError as error:
            raise DocumentError(str(error)) from error
    _populate_contents(sheets)
    if paper_format == "ALBUM":
        from .album_graphics import resolve_album_graphic_links
        resolve_album_graphic_links(sheets)
    if len(sheets) > MAX_SHEETS:
        raise DocumentError(f"Комплект превышает лимит {MAX_SHEETS} листов. Разделите проект.")
    offset = int(bool(sheets and sheets[0].kind == "cover"))
    for i, sheet in enumerate(sheets, 1):
        _frame(sheet, project, i - offset, len(sheets) - offset, paper_format)
    return DocumentSet(project, report, sheets, paper_format,derived_assemblies=derived_assemblies,
                       _validated_nodes=node_records,_validated_wall_usage=(whole_walls,stale_whole_walls))


def _populate_contents(sheets):
    """Paginate complete titles after composition, before any sheet is framed."""
    contents = [sheet for sheet in sheets if sheet.kind == "contents"]
    if not contents:
        return
    offset = int(sheets[0].kind == "cover")
    width, height = contents[0].width, contents[0].height
    while True:
        chunks, chunk, y = [], [], 43
        for index, sheet in enumerate(sheets, 1):
            if sheet.kind == "cover":
                continue
            lines = wrap(sheet.title, width - 97, 2.9)
            row_height = max(7., 4.4 * len(lines) + 2.4)
            if row_height > height - 116:
                raise DocumentError("Наименование не помещается в содержание. Сократите заголовок листа.")
            if y + row_height > height - 73 and chunk:
                chunks.append(chunk)
                chunk, y = [], 43
            chunk.append((index - offset, sheet, lines, y, row_height))
            y += row_height
        if chunk:
            chunks.append(chunk)
        if len(chunks) <= len(contents):
            break
        # Dataclass equality is insufficient here: two still-empty continued
        # contents sheets compare equal. Find the exact page object.
        position = next(i for i, sheet in enumerate(sheets) if sheet is contents[-1]) + 1
        continuation = Sheet("Содержание комплекта / продолжение", "contents", width, height)
        sheets.insert(position, continuation)
        contents.append(continuation)
    for content, rows in zip(contents, chunks):
        content.text(24, 36, "Лист", 2.9, bold=True)
        content.text(42, 36, "Наименование", 2.9, bold=True)
        content.text(width - 28, 36, "Формат", 2.9, bold=True, anchor="end")
        content.line(23, 39, width - 23, 39, INK, .2)
        for number, sheet, lines, y, row_height in rows:
            content.text(24, y, str(number), 2.9, BLUE, True)
            for line_number, line in enumerate(lines):
                content.text(42, y + line_number * 4.4, line, 2.9)
            content.text(width - 28, y, "A2" if sheet.width == 594 else "A3", 2.9, anchor="end")
            content.line(23, y + row_height - 3, width - 23, y + row_height - 3, "#d9e1e7", .1)


def _frame(sheet, project, number, total, paper_format):
    w, h = sheet.width, sheet.height
    sheet.rect(20, 5, w - 25, h - 10, INK, weight=.35)
    if sheet.kind == "cover":
        sheet.text(w - 13, h - 12, "A3", 2.8, GRAY, anchor="end")
        return
    title_lines = wrap(sheet.title, w - 69, 3.6, True)
    if len(title_lines) > 2:
        raise DocumentError("Заголовок листа длиннее двух строк. Сократите наименование листа.")
    for row, line in enumerate(title_lines):
        sheet.text(24, 17 + row * 5.1, line, 3.6, INK, True)
    sheet.line(20, 27, w - 5, 27, INK, .2)
    sheet.text(w - 12, 17, "A2" if w == 594 else "A3", 2.8, GRAY, anchor="end")
    stamp_height = 55 if paper_format == "ALBUM" else 37
    x, y = w - 190, h - 5 - stamp_height
    sheet.rect(x, y, 185, stamp_height, INK, weight=.3)
    # A familiar title block, with explicit source metadata and empty approval
    # fields. No signature, certification or document number is fabricated.
    project_lines = wrap(project.metadata.name, 125, 3.0, True)
    if len(project_lines) > 2:
        project_lines = wrap(project.metadata.name, 125, 2.6, True)
    if len(project_lines) > 4:
        project_lines = ["Наименование объекта:", "см. обложку" if paper_format == "ALBUM" else "см. общие данные"]
    for row, line in enumerate(project_lines):
        sheet.text(x + 3, y + 6 + row * 4.2, line, 2.6 if len(project_lines) > 2 else 3, bold=True)
    sheet.line(x + 132, y, x + 132, h - 5, INK, .15)
    sheet.line(x + 132, y + 12, w - 5, y + 12, INK, .15)
    sheet.line(x + 148, y, x + 148, y + 24, INK, .15)
    sheet.line(x + 166, y, x + 166, y + 24, INK, .15)
    for xx, label, value in [(x + 140, "Стадия", project.metadata.stage), (x + 157, "Лист", number), (x + 176, "Листов", total)]:
        sheet.text(xx, y + 5, label, 2.5, anchor="middle")
        sheet.text(xx, y + 20, str(value), 3.1, bold=True, anchor="middle")
    sheet.line(x, y + 24, w - 5, y + 24, INK, .15)
    sheet.text(x + 3, y + 30, "Отделка стен панелями", 2.9)
    sheet.text(x + 135, y + 30, f"Версия {project.revision}", 2.6)
    if stamp_height > 37:
        sheet.line(x, y + 35, w - 5, y + 35, INK, .15)
        sheet.text(x + 3, y + 42, "Разработал", 2.7)
        if project.metadata.author:
            author = wrap(project.metadata.author, 98, 2.7)
            sheet.text(x + 29, y + 42, author[0] if len(author) == 1 else "см. обложку", 2.7)
        sheet.text(x + 3, y + 49, "Проверил", 2.7)
        sheet.text(x + 135, y + 43, project.updated_at.date().isoformat(), 2.6)
    else:
        sheet.text(x + 3, y + 35, project.updated_at.date().isoformat(), 2.6, GRAY)
    sheet.text(24, h - 12, "Размеры в мм. Печать 100%.", 2.7, GRAY)


def _node_sheet(new, node, width, height):
    """Render safe catalog primitives, not executable SVG strings."""
    s = new(f"Библиотечный кандидат / {node['id']}", "node_candidate")
    s.paragraph(23, 41, node["name"], width - 46, 4, bold=True)
    s.text(23, 54, "КАНДИДАТ ИЗ БИБЛИОТЕКИ. НЕ ПОДТВЕРЖДЁН ДЛЯ ДАННОГО МЕСТА.", 3.3, RED, True)
    drawing = node.get("drawing", {})
    viewbox = drawing.get("view_box", [0, 0, 100, 100])
    bx, by, bw, bh = viewbox
    factor = min((width - 90) / bw, (height - 145) / bh)
    ox = 40 + ((width - 90) - bw * factor) / 2
    transform = lambda x, y: (ox + (x - bx) * factor, 66 + (y - by) * factor)
    for p in drawing.get("primitives", []):
        typ = p.get("type")
        color = p.get("stroke", INK)
        weight = p.get("stroke_width_mm", .25) * factor
        fill = p.get("fill")
        if fill == "none":
            fill = None
        if typ == "line":
            s.line(*transform(p["x1"], p["y1"]), *transform(p["x2"], p["y2"]), color, weight)
        elif typ == "rect":
            s.rect(*transform(p["x"], p["y"]), p["width"] * factor, p["height"] * factor, color, fill, weight)
        elif typ in {"polyline", "polygon"}:
            s.polygon([transform(*point) for point in p["points"]], color, fill, weight, closed=typ == "polygon")
        elif typ == "circle":
            s.circle(*transform(p["cx"], p["cy"]), p["r"] * factor, color, fill, weight)
        elif typ == "text":
            if p.get("fill", INK) != "none":
                s.text(*transform(p["x"], p["y"]), p["text"], p["font_size"] * factor, p.get("fill", INK), anchor=p.get("anchor", "start"))
    s.text(23, height - 64, "Схематичный вид из библиотеки; без заданного монтажного масштаба. Правила применимости - в engineering.json.", 2.8, GRAY)
