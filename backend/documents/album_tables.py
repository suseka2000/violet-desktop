"""Flow readable schedule sections without sacrificing rows or paper text size."""
from .scene import GRAY, INK, LIGHT, Sheet, wrap

A3 = (420, 297)
BOTTOM = 225
SHORT_NAMES = {
    "wall_mount_quantities": "Облицовка", "openings": "Проёмы",
    "special_products": "Специальные изделия", "special_components": "Состав изделий",
    "special_assumptions": "Решения по изделиям", "assembly_machining": "Пазы и выборки",
    "wall_mount_nodes": "Реестр узлов", "issues": "Замечания", "mount_checks": "Проверки",
    "assembly_notes": "Исходные профили", "assembly_decisions": "Посадки узлов",
    "assembly_stale": "Повторная сборка",
    "whole_wall_quantities": "Каркас и крепёж", "whole_wall_members": "Заготовки каркаса",
    "whole_wall_components": "Состав стен", "whole_wall_notes": "Решения по основанию", "whole_wall_stale": "Пересчёт стен",
}


def table(sheets, title, headers, rows, proportions, kind, *, reuse=True, paper=A3):
    if not rows:
        return
    usable_width = paper[0] - 50
    bottom = paper[1] - 72
    widths = [usable_width * value / sum(proportions) for value in proportions]
    prepared = [[wrap(cell, column - 4, 2.75) for cell, column in zip(row, widths)] for row in rows]
    header_height = max(len(wrap(header, column - 4, 2.8, True)) for header, column in zip(headers, widths)) * 4.2 + 5
    row_heights = [max(map(len, row)) * 4.2 + 3.3 for row in prepared]
    section_height = header_height + sum(row_heights)
    sheet, y = None, bottom

    def page(try_reuse=False):
        nonlocal sheet, y
        previous = sheets[-1] if sheets else None
        can_reuse = (try_reuse and previous is not None and previous.layout.get("flow_table")
                     and previous.width == paper[0] and previous.height == paper[1])
        header_y = previous.layout["table_end_y"] + 13 if can_reuse else 34
        # Keep a short section together; a long one can use remaining space
        # only if a header and at least two complete rows fit there.
        minimum = section_height if section_height <= 60 else header_height + sum(row_heights[:2])
        if can_reuse and header_y + minimum <= bottom:
            sheet = previous
            title_lines = wrap(title, usable_width, 3, True)
            header_y = previous.layout["table_end_y"] + 9 + len(title_lines) * 4.4
            if header_y + minimum > bottom:
                can_reuse = False
        else:
            can_reuse = False
        if can_reuse:
            for index, line in enumerate(title_lines):
                sheet.text(25, previous.layout["table_end_y"] + 8 + index * 4.4, line, 3, INK, True)
            sheet.title = "Ведомости и примечания"
        else:
            sheet = Sheet(title, kind, *paper)
            sheets.append(sheet)
            header_y = 34
        sheet.layout["flow_table"] = True
        sheet.layout.setdefault("table_sections", []).append(title)
        sheet.layout.setdefault("table_kinds", []).append(kind)
        if len(sheet.layout["table_kinds"]) > 1:
            labels = [SHORT_NAMES.get(value, "Ведомости") for value in dict.fromkeys(sheet.layout["table_kinds"])]
            sheet.title = " / ".join(labels)
            if len(wrap(sheet.title, paper[0] - 69, 3.6, True)) > 2:
                sheet.title = labels[0] + " / ведомости и примечания"
        start = len(sheet.primitives)
        x = 25
        for header, column in zip(headers, widths):
            sheet.rect(x, header_y, column, header_height, GRAY, LIGHT, .15)
            sheet.paragraph(x + 2, header_y + 5, header, column - 4, 2.8, INK, True, leading=4.2)
            x += column
        y = header_y + header_height
        sheet.layout.setdefault("table_regions", []).append({"title": title, "kind": kind,
            "x": 25, "y": header_y, "width": usable_width, "bottom": y, "primitive_start": start})

    for lines in prepared:
        total = max(map(len, lines))
        offset = 0
        # Do not split a row that fits on a fresh sheet merely to fill a tail.
        full_height = total * 4.2 + 3.3
        if sheet is not None and y + full_height > bottom and full_height + header_height <= bottom - 34:
            page()
        while offset < total:
            if sheet is None or y + 7.6 > bottom:
                page(try_reuse=reuse and sheet is None)
            count = min(total - offset, max(1, int((bottom - y - 3.3) / 4.2)))
            row_height = count * 4.2 + 3.3
            x = 25
            for cell, column in zip(lines, widths):
                sheet.rect(x, y, column, row_height, GRAY, weight=.12)
                for index, line in enumerate(cell[offset:offset + count]):
                    sheet.text(x + 2, y + 4.8 + index * 4.2, line, 2.75)
                x += column
            y += row_height
            sheet.layout["table_end_y"] = y
            sheet.layout["table_regions"][-1]["bottom"] = y
            offset += count
            if offset < total:
                page()


def combine_general_contents(sheets):
    """Embed a short two-column index below the general notes, if it fits.

    The complete index is retained, with original paper font sizes. For a
    large album the ordinary paginated contents path is left untouched.
    This runs before ordinary contents pagination and final frame numbering.
    """
    contents = [sheet for sheet in sheets if sheet.kind == "contents"]
    general = next((sheet for sheet in sheets if sheet.kind == "general"), None)
    if len(contents) != 1 or general is None or contents[0].primitives:
        return False
    candidate = [sheet for sheet in sheets if sheet is not contents[0]]
    offset = int(bool(candidate and candidate[0].kind == "cover"))
    end_y = max(p["y"] + .6 for p in general.primitives if p["type"] == "text")
    top = end_y + 12
    bottom = 225
    column_width, gap = 179, 12
    rows = []
    for index, sheet in enumerate(candidate, 1):
        if sheet.kind == "cover":
            continue
        label = "Общие данные и состав комплекта" if sheet is general else sheet.title
        lines = wrap(label, column_width - 32, 2.9)
        rows.append((index - offset, sheet, lines, max(7., len(lines) * 4.4 + 2.4)))
    # Split in reading order, balance physical heights rather than row count.
    choices = []
    for split in range(1, len(rows) + 1):
        heights = [sum(row[3] for row in group) for group in (rows[:split], rows[split:])]
        if top + 8 + max(heights) <= bottom:
            choices.append((max(heights), abs(heights[0] - heights[1]), split))
    if not choices:
        return False
    split = min(choices)[2]
    general.title = "Общие данные и состав комплекта"
    general.layout["embedded_contents"] = True
    general.layout["contents_entries"] = []
    general.text(25, top - 4, "Состав комплекта", 3.8, INK, True)
    for column, group in enumerate((rows[:split], rows[split:])):
        x = 25 + column * (column_width + gap)
        y = top + 8
        general.text(x, top + 2, "Лист", 2.9, INK, True)
        general.text(x + 15, top + 2, "Наименование", 2.9, INK, True)
        general.text(x + column_width, top + 2, "Формат", 2.9, INK, True, "end")
        general.line(x, top + 4.5, x + column_width, top + 4.5, GRAY, .15)
        for number, sheet, lines, height in group:
            general.text(x, y, str(number), 2.9, "#215d9c", True)
            for line_index, line in enumerate(lines):
                general.text(x + 15, y + line_index * 4.4, line, 2.9)
            general.text(x + column_width, y, "A2" if sheet.width == 594 else "A3", 2.9, anchor="end")
            general.line(x, y + height - 3, x + column_width, y + height - 3, "#d9e1e7", .1)
            general.layout["contents_entries"].append({"sheet_number": number, "title": sheet.title})
            y += height
    sheets[:] = candidate
    return True
