"""Integrity of hidden-wall material evidence after native STEP verification.

These are portable coherence checks, not a second geometric calculation. They
keep source identities, subtractive machining and quantities tied to the plan.
"""
from collections import defaultdict
import math

from .assembly_fasteners import CALIBRATIONS, PROFILE_SOURCES
from .mount_assemblies import digest


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _intersection(a, b):
    low = [max(a[0][i], b[0][i]) for i in range(3)]
    high = [min(a[1][i], b[1][i]) for i in range(3)]
    return (low, high) if all(high[i] > low[i] for i in range(3)) else None


def _subtract_box(box, cutter):
    overlap = _intersection(box, cutter)
    if overlap is None:
        return [box]
    low, high = list(box[0]), list(box[1])
    pieces = []
    # Six disjoint slabs surrounding the shared material, with no duplicate
    # volume at corners. All panel operations use rectangular cutters.
    for axis in range(3):
        if low[axis] < overlap[0][axis]:
            end = high.copy(); end[axis] = overlap[0][axis]
            pieces.append((low.copy(), end)); low[axis] = end[axis]
        if high[axis] > overlap[1][axis]:
            start = low.copy(); start[axis] = overlap[1][axis]
            pieces.append((start, high.copy())); high[axis] = start[axis]
    return pieces


def expected_panel_cuts(parts, thickness, tools):
    """Analytic independent evidence for every ordered rectangular cut.

    The native engine cuts BRep solids; this reconstructs the expected removed
    material without CAD or trusting its exported machining ledger.
    """
    remaining = [([p["x_mm"], 0., p["y_mm"]],
                  [p["x_mm"]+p["width_mm"], thickness, p["y_mm"]+p["height_mm"]]) for p in parts]
    expected = {}
    for tool in tools:
        low = tool["origin_mm"]
        cutter = (low, [a+b for a,b in zip(low, tool["size_mm"])])
        removed, after = 0., []
        for box in remaining:
            common = _intersection(box, cutter)
            if common:
                removed += math.prod(b-a for a,b in zip(*common))
                after.extend(_subtract_box(box, cutter))
            else:
                after.append(box)
        remaining = after
        if removed > .001:
            expected[digest(tool)] = removed
    return expected


def validate_hidden_material(plan, geometry):
    from .wall_assembly_concealed import machining_tools
    from .wall_assembly_geometry import _panel_groups

    bodies = geometry["bodies"]
    by_id = {body["instance_id"]: body for body in bodies}
    if geometry["checks"].get("collision_pair_scope") != "all_instances_aabb_then_exact_brep":
        raise ValueError("Скрытая стена не прошла полную проверку пар деталей")
    profile_ids = set()
    for run in plan["profile_runs"]:
        identifier = "profile-"+run["id"]
        profile_ids.add(identifier)
        body = by_id.get(identifier, {})
        provenance = body.get("provenance", {})
        length = run["end_mm"]-run["start_mm"]
        if (body.get("role") != "profile" or provenance.get("kind") != "exact_section_extrusion"
                or provenance.get("source_id") != PROFILE_SOURCES[run["family"]]
                or provenance.get("section_axis") != ("x" if run["family"] in {"vh2", "sky", "start"} else "y")
                or provenance.get("section_offset_mm") != 0
                or not _positive(provenance.get("length_mm"))
                or abs(provenance["length_mm"]-length) > 1e-5):
            raise ValueError("Профиль стены не соответствует исходному STEP и длине прогона")
    if {body["instance_id"] for body in bodies if body["role"] == "profile"} != profile_ids:
        raise ValueError("Состав профилей не соответствует плану скрытой стены")
    member_lengths = {"frame-"+member["id"]: (
        member["x_end_mm"]-member["x_start_mm"] if member.get("orientation") == "horizontal"
        else member["z_end_mm"]-member["z_start_mm"])
        for member in [*plan["posts"], *plan["rails"]]}
    for body in bodies:
        if body["role"] in {"frame", "fastener"}:
            kind = "frame" if body["role"] == "frame" else "screw"
            expected_kind = "exact_section_extrusion" if kind == "frame" else "source_step"
            provenance = body.get("provenance", {})
            if provenance.get("source_id") != CALIBRATIONS[kind]["source_id"] or provenance.get("kind") != expected_kind:
                raise ValueError("Каркас или крепёж скрытой стены подменён другой исходной деталью")
            if kind == "frame" and (provenance.get("section_axis") != "y" or provenance.get("section_offset_mm") != 0
                    or body["instance_id"] not in member_lengths or not _positive(provenance.get("length_mm"))
                    or abs(provenance["length_mm"]-member_lengths[body["instance_id"]]) > 1e-5):
                raise ValueError("Сечение или длина стойки не соответствуют плану каркаса")

    tools = machining_tools(plan)
    cutters = {digest(tool) for tool in tools}
    machining = geometry.get("machining")
    if not isinstance(machining, list) or not machining:
        raise ValueError("Отсутствует ведомость реальной обработки скрытых панелей")
    removed, grooved, seen = defaultdict(float), set(), set()
    for cut in machining:
        if not isinstance(cut, dict):
            raise ValueError("Некорректная запись обработки панели")
        panel_id = cut.get("panel_id")
        tool = {key: cut.get(key) for key in ("profile_id", "kind", "origin_mm", "size_mm")}
        tool_id = digest(tool)
        identity = (panel_id, tool_id)
        if (not isinstance(panel_id, str) or by_id.get(panel_id, {}).get("role") != "panel"
                or tool_id not in cutters or identity in seen or not _positive(cut.get("removed_volume_mm3"))):
            raise ValueError("Обработка не соответствует панели и рассчитанному пазу")
        seen.add(identity)
        removed[panel_id] += cut["removed_volume_mm3"]
        if cut["kind"] == "edge_groove":
            grooved.add(panel_id)
    expected_panels = set()
    thickness = plan["layers"]["panel_thickness_mm"]
    for index, parts in enumerate(_panel_groups(plan["panels"]), 1):
        identifier = f"panel-{index}"
        expected_panels.add(identifier)
        body = by_id.get(identifier, {})
        nominal = sum(part["width_mm"]*part["height_mm"]*thickness for part in parts)
        expected = expected_panel_cuts(parts, thickness, tools)
        actual = {digest({key:cut[key] for key in ("profile_id", "kind", "origin_mm", "size_mm")}):cut["removed_volume_mm3"]
                  for cut in machining if cut["panel_id"] == identifier}
        if set(actual) != set(expected) or any(abs(actual[key]-volume) > max(.01, volume*1e-8) for key,volume in expected.items()):
            raise ValueError("Не все рассчитанные пазы и подрезки подтверждены фактическим снятием материала")
        if (body.get("role") != "panel" or identifier not in grooved
                or body.get("provenance", {}).get("kind") != "project_machined_panel"
                or body["provenance"].get("edge") != plan["system"]["panel_edge"]
                or abs(nominal-removed[identifier]-body["volume_mm3"]) > max(.01, nominal*1e-8)):
            raise ValueError("Пазы не подтверждены изменением фактического объёма панели")
    if {body["instance_id"] for body in bodies if body["role"] == "panel"} != expected_panels:
        raise ValueError("Состав панелей не соответствует раскладке скрытой стены")

    definitions = defaultdict(list)
    for body in bodies:
        definitions[body["definition_id"]].append(body)
    bom = geometry.get("bom")
    if (not isinstance(bom, list) or any(not isinstance(row, dict) for row in bom)
            or len(bom) != len(definitions)
            or {row.get("definition_id") for row in bom} != set(definitions)):
        raise ValueError("Спецификация скрытой стены не соответствует её реальным деталям")
    for row in bom:
        entries = definitions[row["definition_id"]]
        if type(row.get("count")) is not int or row["count"] != len(entries) or not _positive(row.get("volume_mm3")):
            raise ValueError("Количество или объём в спецификации скрытой стены неверны")
        for body in entries:
            if (any(row.get(key) != body.get(key) for key in ("role", "label", "provenance"))
                    or abs(row["volume_mm3"]-body["volume_mm3"]) > max(.01, body["volume_mm3"]*1e-8)):
                raise ValueError("Спецификация ссылается на другую деталь или исходный STEP")
