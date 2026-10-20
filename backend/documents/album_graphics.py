"""One elevation and one globally marked detail per compact album.

Packing changes paper-space positions only. It never changes saved sections,
manufacturing dimensions, component labels or their physical text sizes.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import re

from .scene import BLUE, GRAY, Sheet, wrap

LEFT, TOP, WIDTH, BOTTOM = 23., 34., 548., 342.
GAP = 8.


def coalesce_identical_details(details):
    """Draw equal cuts once, retaining every mark, location and assembly ID.

    Equality includes actual source contours, orientation and all construction
    context. Opposite-facing cuts and different stock sections never coalesce.
    Only the picture is shared; each engineering decision stays in the register.
    """
    from ..mount_assemblies import digest
    from .node_construction import construction_graphics
    from .album import compact_marks
    groups, result = {}, []
    for entry in details:
        record = entry.get("assembly")
        if not record:
            result.append(entry)
            continue
        graphic = construction_graphics(entry)
        key = digest({"recipe": record.recipe_id, "graphic": graphic,
                      "parameters": {key: record.configuration.get(key) for key in ("panel_thickness_mm", "joint_mm", "panel_edge")}})
        if key not in groups:
            copy = {**entry, "aliases": [entry["mark"]], "assembly_ids": [record.id],
                    "locations": list(entry["locations"]), "_construction_graphics": graphic,
                    "node_labels": [entry["node"]["label"]]}
            groups[key] = copy
            result.append(copy)
        else:
            copy = groups[key]
            copy["aliases"].append(entry["mark"])
            copy["assembly_ids"].append(record.id)
            copy["locations"] = list(dict.fromkeys([*copy["locations"], *entry["locations"]]))
            copy["node_labels"] = list(dict.fromkeys([*copy["node_labels"], entry["node"]["label"]]))
        copy["display_mark"] = compact_marks(copy["aliases"])
        copy["display_label"] = " / ".join(copy["node_labels"])
    return result


def _scale(sheet):
    for primitive in sheet.primitives:
        if primitive["type"] != "text":
            continue
        match = re.fullmatch(r"М ([\d.]+):([\d.]+)", primitive["value"])
        if match:
            return float(match[2]) / float(match[1])
    return None


def _append(page, trial):
    offset = len(page.primitives)
    page.primitives.extend(trial.primitives)
    page.references.extend(trial.references)
    for key in ("mount_dimension_runs",):
        page.layout.setdefault(key, []).extend(trial.layout.get(key, []))
    for overlay in trial.layout.get("whole_wall_overlays", []):
        page.layout.setdefault("whole_wall_overlays", []).append({**overlay,
            "primitive_start": overlay["primitive_start"]+offset,
            "primitive_end": overlay["primitive_end"]+offset})


def mount_packets(sheets, views, catalog, project, marks, num, title, specials=()):
    from .album import _bounds_ok
    from .assemblies import draw_assembly_view
    from .levels import mounting_view_levels
    from .mount_details import draw_schematic_view
    from .special_sections import section_entries, draw_special_view, draw_special_context
    from .wall_mount import draw_mount_view

    details = section_entries(specials) + list(catalog.values())
    by_view = {}
    for mark, view in views:
        key = view["wall"]["id"], view["wall"]["face_id"]
        by_view[mark] = [entry for entry in details if (
            (entry["special"]["wall_id"], entry["special"]["face_id"]) == key
            if entry.get("special") else any(place.startswith(mark + "/") for place in entry["locations"]))]
    used = {mark for sheet in sheets for mark in sheet.layout.get("detail_marks", [])}
    wanted = {entry["mark"] for entries in by_view.values() for entry in entries}
    grouped_details = coalesce_identical_details(details)
    aliases = lambda entry: entry.get("aliases", [entry["mark"]])
    pending = [entry for entry in grouped_details if wanted.intersection(aliases(entry)) and not used.intersection(aliases(entry))]
    all_locations = defaultdict(list)
    for entry in details:
        if entry.get("special"):
            all_locations[entry["mark"]] = [mark for mark, entries in by_view.items() if entry in entries]
        else:
            all_locations[entry["mark"]] = list(dict.fromkeys(place.split("/")[0] for place in entry["locations"]))

    def draw_view(sheet, item, box):
        mark, view = item
        if view.get("special_context"):
            draw_special_context(sheet, item, box, project, marks, num)
            return
        wall = view["wall"]
        related = [entry for entry in specials if (entry["wall_id"], entry["face_id"]) == (wall["id"], wall["face_id"])]
        draw_mount_view(sheet, {**view, "document_opening_marks": marks, "document_specials": related,
            "document_levels": mounting_view_levels(project, view)}, mark, box, num, catalog)

    def trial_draw(draw, item, box):
        trial = Sheet(title, "wall_mount", 594, 420)
        try:
            draw(trial, item, box)
        except ValueError as error:
            if "помещ" in str(error):
                return None
            raise
        return trial if _bounds_ok(trial, box) else None

    def draw_detail(sheet, entry, box):
        (draw_special_view if entry.get("special") else draw_assembly_view if entry.get("assembly") else draw_schematic_view)(sheet, entry, box, num)

    # The former three-across cells used 186 mm for special products and
    # roughly 95 mm for regular nodes in two rows. Preserve those scales as
    # a lower bound on geometric readability, independently of packing.
    # Thin horizontal cuts can then use less whitespace; tall cuts cannot be
    # made smaller merely to fit a short cell.
    reference_scales = {}
    for entry in pending:
        reference = trial_draw(draw_detail, entry, (0, 0, (WIDTH - 2 * GAP) / 3, 186 if entry.get("special") else 95))
        if reference is None:
            reference = trial_draw(draw_detail, entry, (0, 0, (WIDTH - GAP) / 2, 186))
        if reference is None:
            reference = trial_draw(draw_detail, entry, (0, 0, WIDTH, BOTTOM - TOP))
        if reference is None:
            raise ValueError("Узел не помещается на A2; разделите длинный состав узла")
        reference_scales[entry["mark"]] = _scale(reference)
        if entry.get("assembly"):
            # Architectural context must not shrink the real profile compared
            # with the former three-across component-only cut. Give it space.
            record = entry["assembly"]
            width = (WIDTH-2*GAP)/3
            title_lines = wrap(f"Узел {entry['mark']}. {record.label}", width-8, 3, True)
            bw, bh = record.geometry_snapshot.result.bounds_mm.size
            required = max(bw/(width*.61-24), bh/(95-(8+len(title_lines)*4.4)-38), .05)
            original_scale = next((v for v in (.05,.1,.2,.5,1,2,4,5,10,20,50,100,1000) if v >= required), required)
            reference_scales[entry["mark"]] = min(reference_scales[entry["mark"]], original_scale)

    cache = {}
    def minimum_cell(entry, width):
        key = entry["mark"], width
        if key in cache:
            return cache[key]
        # A symbolic sketch has fixed offsets, unlike a fitted solid section.
        # Its minimum height protects internal labels, not just outer bounds.
        minimum = 150 if not (entry.get("assembly") or entry.get("special")) else 90
        for height in range(minimum, 309, 2):
            trial = trial_draw(draw_detail, entry, (0, 0, width, height))
            if trial is None:
                continue
            scale, maximum = _scale(trial), reference_scales[entry["mark"]]
            if scale is not None and maximum is not None and scale > maximum + 1e-9:
                continue
            cache[key] = height
            return height
        cache[key] = None
        return None

    def new_page(packet=()):
        page = Sheet(title if packet else title + " / узлы", "wall_mount", 594, 420)
        page.layout = {"view_marks": [mark for mark, _ in packet], "related_view_marks": [],
            "detail_marks": [], "assembly_ids": [], "special_section_marks": [], "special_assembly_ids": [],
            "detail_reference_marks": list(dict.fromkeys(entry["mark"] for mark, _ in packet for entry in by_view[mark])),
            "graphic_cells": []}
        return page

    def place_detail(page, entry, box):
        trial = trial_draw(draw_detail, entry, box)
        if trial is None:
            raise ValueError("Узел не помещается после переноса в ячейку A2")
        _append(page, trial)
        layout = page.layout
        layout["detail_marks"].extend(aliases(entry))
        layout["related_view_marks"] = list(dict.fromkeys([*layout["related_view_marks"],
            *(location for alias in aliases(entry) for location in all_locations[alias])]))
        if entry.get("assembly"):
            layout["assembly_ids"].extend(entry.get("assembly_ids", [entry["assembly"].id]))
            page.references.extend(entry.get("assembly_ids", [entry["assembly"].id]))
        if entry.get("special"):
            layout["special_section_marks"].append(entry["mark"])
            layout["special_assembly_ids"] = list(dict.fromkeys([*layout["special_assembly_ids"], entry["special"]["assembly"]["id"]]))
        layout["graphic_cells"].append({"kind": "detail", "mark": entry["mark"], "marks": aliases(entry), "box": list(box), "scale": _scale(trial)})

    def fill(page, top, candidates, columns=3):
        width = (WIDTH - GAP * (columns - 1)) / columns
        remaining = list(candidates)
        # Independent vertical shelves avoid leaving a full empty row beneath
        # a tall door section. Scan past a tall item when a shorter one fits.
        for column in range(columns):
            y = top
            while remaining:
                chosen = next(((entry, height) for entry in remaining
                    if (height := minimum_cell(entry, width)) is not None and y + height <= BOTTOM), None)
                if chosen is None:
                    break
                entry, height = chosen
                place_detail(page, entry, (LEFT + column * (width + GAP), y, width, height))
                remaining.remove(entry)
                y += height + GAP
        return remaining

    remaining_views = list(views)
    while remaining_views:
        # When this room only references details already printed, the lower
        # band no longer needs to be reserved for junctions. Use two rows of
        # the same wide elevation cells, never smaller text or poorer scale.
        # Any view that fails the usual fit/scale guards keeps the old fallback.
        if not pending and len(remaining_views) >= 3:
            packet = remaining_views[:4]
            page = new_page(packet)
            width, height = (WIDTH - GAP) / 2, (BOTTOM - TOP - GAP) / 2
            for index, item in enumerate(packet):
                baseline = trial_draw(draw_view, item, (0, 0, 269, 124))
                if baseline is None:
                    baseline = trial_draw(draw_view, item, (0, 0, WIDTH, 308))
                box = (LEFT + index % 2 * (width + GAP), TOP + index // 2 * (height + GAP), width, height)
                trial = trial_draw(draw_view, item, box)
                if trial is None or (baseline and _scale(trial) and _scale(baseline) and _scale(trial) > _scale(baseline)):
                    break
                _append(page, trial)
                page.layout["graphic_cells"].append({"kind": "view", "mark": item[0], "box": list(box), "scale": _scale(trial)})
            else:
                sheets.append(page)
                remaining_views = remaining_views[len(packet):]
                continue
        accepted = None
        # Three elevations across remain at their old scale if they fit. Long
        # walls or large callout sets automatically use two or one wider cell.
        for count in range(min(3, len(remaining_views)), 0, -1):
            packet = remaining_views[:count]
            for height in (124, 154, 184, 224, 308):
                page = new_page(packet)
                width = (WIDTH - GAP * (count - 1)) / count
                for item in packet:
                    baseline = trial_draw(draw_view, item, (0, 0, 269, 124))
                    if baseline is None:
                        baseline = trial_draw(draw_view, item, (0, 0, WIDTH, 308))
                    box = (LEFT + len(page.layout["graphic_cells"]) * (width + GAP), TOP, width, height)
                    trial = trial_draw(draw_view, item, box)
                    if trial is None or (baseline and _scale(trial) and _scale(baseline) and _scale(trial) > _scale(baseline)):
                        break
                    _append(page, trial)
                    page.layout["graphic_cells"].append({"kind": "view", "mark": item[0], "box": list(box), "scale": _scale(trial)})
                else:
                    accepted = page, packet, height
                    break
            if accepted:
                break
        if not accepted:
            raise ValueError("Развёртка не помещается даже на A2; разделите длинную стену на виды")
        page, packet, height = accepted
        related = {entry["mark"] for mark, _ in packet for entry in by_view[mark]}
        candidates = [entry for entry in pending if related.intersection(aliases(entry))]
        # A product-heavy wall must not push every floor/ceiling junction to
        # another sheet. Put a construction junction beside the elevations and
        # use their lower band for tall product cuts. Short cuts then pack
        # efficiently on continuations instead of stranding one final node.
        products = [entry for entry in candidates if entry.get("special")]
        ordinary = [entry for entry in candidates if not entry.get("special")]
        rank = {"floor": 0, "ceiling": 1, "left_edge": 2, "right_edge": 3}
        ordinary.sort(key=lambda entry: rank.get(entry["node"]["kind"], 4))
        products.sort(key=lambda entry: minimum_cell(entry, (WIDTH - 2 * GAP) / 3) or 1000, reverse=True)
        candidates = [*products[:1], *ordinary[:1], *products[1:], *ordinary[1:]]
        priority_marks = {entry["mark"] for entry in candidates}
        if len(packet) == len(remaining_views):
            # On this room's last elevation sheet, use spare cells for its
            # earlier deferred cuts. Pending is already restricted to views
            # in this group; never borrow details from another room. Related
            # cuts remain first both in the shelf order and the layout score.
            candidates.extend(entry for entry in pending if entry["mark"] not in priority_marks)
        choices = []
        main_junctions = {entry['mark'] for entry in ordinary if entry['node']['kind'] in {'floor', 'ceiling'}}
        for columns in (3, 2, 1):
            trial_page = deepcopy(page)
            rest = fill(trial_page, TOP + height + GAP, candidates, columns)
            drawn = set(trial_page.layout['detail_marks'])
            choices.append((bool(drawn.intersection(main_junctions)), len(drawn.intersection(priority_marks)), len(candidates)-len(rest), columns, trial_page))
        page = max(choices, key=lambda choice: choice[:4])[4]
        if page.layout["detail_marks"]:
            page.line(LEFT, TOP + height + GAP / 2, LEFT + WIDTH, TOP + height + GAP / 2, GRAY, .12)
        drawn = set(page.layout["detail_marks"])
        pending = [entry for entry in pending if entry["mark"] not in drawn]
        sheets.append(page)
        remaining_views = remaining_views[len(packet):]

    while pending:
        # Try wider columns only when their additional usable space fits more
        # complete details. No font scaling and no partial legends are allowed.
        alternatives = []
        for columns in (3, 2, 1):
            page = new_page()
            remaining = fill(page, TOP, pending, columns)
            alternatives.append((len(pending) - len(remaining), columns, page, remaining))
        count, _, page, remaining = max(alternatives, key=lambda item: (item[0], item[1]))
        if not count:
            raise ValueError("Узел не помещается на листе продолжения A2")
        sheets.append(page)
        pending = remaining


def resolve_album_graphic_links(sheets):
    """Resolve after contents pagination, before frames assign final numbers."""
    from .album import compact_marks
    offset = int(bool(sheets) and sheets[0].kind == "cover")
    views, details = {}, {}
    for index, sheet in enumerate(sheets, 1):
        number = index - offset
        for mark in sheet.layout.get("view_marks", []):
            if mark in views:
                raise ValueError("Повторная развёртка в компактном альбоме: " + mark)
            views[mark] = number
        for mark in sheet.layout.get("detail_marks", []):
            if mark in details:
                raise ValueError("Повторный узел в компактном альбоме: " + mark)
            details[mark] = number
    for index, sheet in enumerate(sheets, 1):
        if "detail_reference_marks" not in sheet.layout:
            continue
        number = index - offset
        lines = []
        resolved = {}
        for label, marks, lookup in (("Развёртки", sheet.layout.get("related_view_marks", []), views),
                                     ("Узлы", sheet.layout.get("detail_reference_marks", []), details)):
            grouped = defaultdict(list)
            for mark in marks:
                if mark not in lookup:
                    raise ValueError("Не найден лист для ссылки: " + mark)
                grouped[lookup[mark]].append(mark)
            if grouped:
                lines.append(label + ": " + "; ".join(compact_marks(values) + (" - на этом листе" if page == number else f" - л. {page}")
                                                      for page, values in sorted(grouped.items())) + ".")
                resolved[label] = {mark: lookup[mark] for mark in marks}
        y = 349
        for line in lines:
            y = sheet.paragraph(LEFT, y, line, WIDTH, 2.5, BLUE, leading=3.6) + 1.4
        if y > 359:
            raise ValueError("Ссылки графического листа не помещаются; разделите группу развёрток")
        sheet.layout["resolved_graphic_links"] = resolved
