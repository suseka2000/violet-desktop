"""Draft section sheets from preserved sampled STEP geometry, never invented nodes."""
from __future__ import annotations

import re

from ..workspace import validate_cad_assets
from .scene import BLUE, GRAY, INK, RED, wrap


def entries(project):
    return [(asset, section) for asset in validate_cad_assets(project.properties.get("cad_assets", [])) for section in asset.sections]


def section_dxf_bytes(snapshot):
    """Reproducible section DXF without mutating process-global ezdxf options.

Only generated metadata is normalized, never model coordinates. These are
derived documents, so their IDs follow the geometry digest, not wall-clock time.
"""
    from ..step_geometry import section_dxf
    import ezdxf
    text = section_dxf(snapshot.result.portable()).decode("utf-8")
    lines = text.splitlines()
    digest = snapshot.geometry_sha256.upper()
    guid = "{" + "-".join((digest[:8], digest[8:12], digest[12:16], digest[16:20], digest[20:32])) + "}"
    for index in range(0, len(lines) - 1, 2):
        if lines[index].strip() == "9" and index + 3 < len(lines):
            variable = lines[index + 1].strip()
            if variable in {"$TDCREATE", "$TDUCREATE", "$TDUPDATE", "$TDUUPDATE"}:
                lines[index + 3] = "2451544.5"
            elif variable in {"$VERSIONGUID", "$FINGERPRINTGUID"}:
                lines[index + 3] = guid
        if lines[index].strip() == "1" and re.fullmatch(re.escape(ezdxf.__version__) + r" @ \d{4}-\d{2}-\d{2}T[^\n]+", lines[index + 1]):
            lines[index + 1] = ezdxf.__version__ + " / Interior RD reproducible section"
    provenance = ["999", "SOURCE SHA256 " + snapshot.source_id, "999", "GEOMETRY SHA256 " + snapshot.geometry_sha256,
                  "999", f"SECTION {snapshot.request.axis.upper()}={snapshot.request.offset_mm} MM / NOT ENGINEERING APPROVED"]
    return ("\n".join(provenance + lines) + "\n").encode("utf-8")


def add_section_sheets(project, new, table, names, width, height, num):
    from .section_dimensions import draw_section_dimensions
    rows, legend, notes = [], [], []
    for index, (asset, section) in enumerate(entries(project), 1):
        mark = f"С{index:03d}"
        snapshot = section.geometry_snapshot
        context = "; ".join(names.get(value, value) for value in (section.target_space_id, section.target_zone_id) if value) or "Не назначено"
        rows.append([mark, section.label, asset.filename, f"{section.axis.upper()} = {num(section.offset_mm)} мм",
                     context, "Геометрия сохранена" if snapshot else "Только параметры; постройте геометрию"])
        # Complete labels and notes belong to a paginated table, not a cramped drawing caption.
        if section.note:
            for start in range(0, len(section.note), 900):
                notes.append([mark, "Примечание пользователя" + (" / продолжение" if start else ""), section.note[start:start + 900]])
        if snapshot is None:
            continue
        result = snapshot.result
        sheet = new(f"STEP-сечение {mark} / {section.label}", "cad_section")
        sheet.references.extend([section.id, asset.source_id])
        sheet.text(23, 37, f"{mark}. Плоскость {section.axis.upper()} = {num(section.offset_mm)} мм. Проекция: {' / '.join(result.projection_axes).upper()}.", 3.2, BLUE, True)
        filename_lines = wrap(asset.filename, width - 48, 3)
        for line_index, line in enumerate(filename_lines[:2]):
            sheet.text(23, 44 + line_index * 4.5, line, 3, GRAY)
        sheet.text(23, 57, "SHA256 исходника: " + asset.source_id, 2.6, GRAY)
        sheet.text(23, 63, "SHA256 геометрии: " + snapshot.geometry_sha256, 2.6, GRAY)
        sheet.text(23, 70, "Сохранённый снимок. Контрольная сумма проверяет целостность, но не подтверждает авторство или применимость.", 2.7, GRAY)
        if result.bounds_mm is None:
            sheet.text(width / 2, 125, "Плоскость не пересекает выбранные тела", 4, RED, anchor="middle")
        else:
            bounds = result.bounds_mm
            bw, bh = bounds.size
            box_width, box_height = width - 130, height - 181
            required = max(bw / box_width, bh / box_height, .02)
            scales = (.02, .05, .1, .2, .5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 2000, 5000,
                      10_000, 50_000, 100_000, 1_000_000, 10_000_000, 100_000_000)
            scale = next(value for value in scales if value >= required)
            ox, oy = 72 + (box_width - bw / scale) / 2, 83 + (box_height - bh / scale) / 2
            transform = lambda p: (ox + (p[0] - bounds.min[0]) / scale, oy + (bounds.max[1] - p[1]) / scale)
            sheet.text(width - 23, 78, f"М {num(1 / scale)}:1" if scale < 1 else f"М 1:{num(scale)}", 3, BLUE, anchor="end")
            for component in result.components:
                for path in component.paths:
                    sheet.polygon([transform(point) for point in path], INK, weight=.25, closed=False)
            visible_components = [component for component in result.components if component.paths]
            if len(visible_components) <= 12:
                # Body numbers are source-local IDs, never inferred product codes.
                visible_components.sort(key=lambda c: transform(c.paths[0][0])[1])
                for ci, component in enumerate(visible_components):
                    px, py = transform(component.paths[0][0])
                    lx, ly = width - 32, 88 + ci * 8
                    sheet.line(px, py, lx - 4, ly, GRAY, .1)
                    sheet.circle(lx, ly, 3.3, BLUE, "#ffffff", .2)
                    sheet.text(lx, ly + 1, str(component.id), 2.5, BLUE, True, "middle")
            draw_section_dimensions(sheet, [[transform(p) for p in path] for component in result.components
                                           for path in component.paths], num(bw)+" мм", num(bh)+" мм",
                                    horizontal_gap=9, vertical_gap=8, vertical_size=2.8,
                                    bounds=(23, 74, width-44, height-79))
            sheet.text(23, height - 75, "Габарит по контурам сечения. Параметры построения приведены в ведомости.", 3, GRAY)
        sheet.text(23, height - 68, f"Кривые представлены отрезками; параметр дискретизации {num(result.deflection_mm)} мм.", 3, GRAY)
        sheet.text(23, height - 61, "Состав и замечания по геометрии приведены в ведомостях сечений.", 3, GRAY)
        notes.append([mark, "Происхождение", f"OCCT {result.occt_version}; исходный размер {result.byte_length} байт; единицы: мм. "
                      f"Точки: {result.point_count}. SHA256 исходника: {asset.source_id}. SHA256 геометрии: {snapshot.geometry_sha256}."])
        for warning in result.warnings:
            notes.append([mark, "Предупреждение построения", warning])
        for component in result.components:
            for warning in component.warnings:
                notes.append([mark, f"Тело {component.id}", warning])
            legend.append([mark, f"{component.id} / {component.label}", f"STEP_BODY_{component.id}", str(component.edge_count),
                           f"{component.closed_wire_count} / {component.open_wire_count}",
                           (f"Предупреждений: {len(component.warnings)}; см. ограничения" if component.warnings else
                            "Не пересекает плоскость" if not component.paths else "Назначение и материал не заданы")])
    if rows:
        table("Реестр STEP-сечений", ["Марка", "Подпись", "Исходник", "Плоскость", "Привязка", "Статус"], rows, [1, 3, 3, 1.7, 2.5, 3], "cad_sections")
    if legend:
        table("Компоненты STEP-сечений / номера тел исходника", ["Марка", "Тело", "Слой DXF", "Рёбер", "Замкн. / откр.", "Примечания"], legend, [1, 2.4, 2.4, 1, 1.6, 4], "cad_components")
    if notes:
        table("STEP-сечения / происхождение и ограничения", ["Марка", "Тип", "Описание"], notes, [1, 2.4, 9], "cad_section_notes")
