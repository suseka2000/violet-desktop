"""Exact, bounded rigid assemblies. Invoke through assembly_worker from HTTP.

Source dimensions are never stretched. Generated panels are explicitly separate
solids. A geometric assembly is not approval of its engineering or installation.
All optional native imports live inside the build/export routines.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import re

from . import step_geometry as step

MAX_SOURCES = 8
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_BODIES = 32
MAX_CONTACTS = 96
MAX_CUTOUTS_PER_BOX = 8
MAX_EXPORT_BYTES = 2 * 1024 * 1024
MAX_FASTENED_EXPORT_BYTES = 8 * 1024 * 1024
MAX_COMBINED_POINTS = 120_000
IDENTITY = [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]


def _invalid(message, *, code="ASSEMBLY_INVALID", details=None):
    raise step.StepGeometryError(message, code=code, details=details)


def _object(value, allowed, required, where):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        _invalid(f"Некорректные поля {where}")


def _number(value, where, *, minimum=-1e9, maximum=1e9):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum or not math.isfinite(value):
        _invalid(f"{where}: требуется конечное число от {minimum:g} до {maximum:g}")
    return float(value) if value else 0.


def _vector(value, where, *, minimum=-1e9, maximum=1e9):
    if not isinstance(value, list) or len(value) != 3:
        _invalid(f"{where}: требуются три координаты")
    return [_number(v, where, minimum=minimum, maximum=maximum) for v in value]


def _rotation(value):
    if not isinstance(value, list) or len(value) != 3:
        _invalid("Поворот должен быть матрицей 3×3")
    rows = [_vector(row, "Матрица поворота", minimum=-1., maximum=1.) for row in value]
    for i in range(3):
        for j in range(3):
            if abs(sum(rows[i][k] * rows[j][k] for k in range(3)) - (1 if i == j else 0)) > 1e-9:
                _invalid("Допустим только жёсткий поворот: масштабирование и сдвиг формы запрещены")
    a, b, c = rows
    determinant = a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])
    if abs(determinant - 1.) > 1e-9:
        _invalid("Отражение STEP запрещено; используйте жёсткий поворот")
    return rows


def _cutouts(value, box_size):
    if not isinstance(value, list) or len(value) > MAX_CUTOUTS_PER_BOX:
        _invalid("Допускается не более восьми прямоугольных вырезов на тело", code="ASSEMBLY_LIMIT")
    result, identifiers = [], set()
    for cutout in value:
        _object(cutout, {"id", "origin_mm", "size_mm"}, {"id", "origin_mm", "size_mm"}, "выреза панели")
        identifier = cutout["id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier) or identifier in identifiers:
            _invalid("Идентификаторы вырезов должны быть уникальными внутри тела")
        identifiers.add(identifier)
        origin = _vector(cutout["origin_mm"], "Начало выреза")
        size = _vector(cutout["size_mm"], "Размер выреза", minimum=1e-5, maximum=1e6)
        # A cutter may extend past the panel for an open groove or through-hole.
        # Mere face/edge contact is not a volumetric cut. Later native checks
        # additionally require a real intersection with the *remaining* solid.
        if any(min(origin[i] + size[i], box_size[i]) <= max(origin[i], 0.) for i in range(3)):
            _invalid("Вырез должен пересекать объём исходного тела", code="ASSEMBLY_CUTOUT_INVALID")
        result.append({"id": identifier, "origin_mm": origin, "size_mm": size})
    return sorted(result, key=lambda cutout: cutout["id"])


def validate_assembly_spec(spec: dict) -> dict:
    """Pure validation/canonicalization, safe before native worker allocation."""
    _object(spec, {"schema_version", "placements", "boxes", "section", "collision_tolerance_mm3", "fastenings", "contacts"}, {"schema_version", "placements", "boxes", "section"}, "сборки")
    if spec["schema_version"] != "assembled-section-request/1.0":
        _invalid("Неизвестная версия запроса сборки")
    _object(spec["section"], {"axis", "offset_mm"}, {"axis", "offset_mm"}, "плоскости сечения")
    axis, offset, _ = step.validate_section_parameters(spec["section"]["axis"], _number(spec["section"]["offset_mm"], "Смещение сечения"), None)
    canonical = {"schema_version": spec["schema_version"], "placements": [], "boxes": [], "section": {"axis": axis, "offset_mm": offset}, "collision_tolerance_mm3": _number(spec.get("collision_tolerance_mm3", .001), "Допуск пересечения", minimum=0., maximum=1e6)}
    identifiers, count = set(), 0
    for group in ("placements", "boxes"):
        values = spec[group]
        if not isinstance(values, list) or len(values) > MAX_BODIES:
            _invalid("Сборка должна содержать не более 32 тел", code="ASSEMBLY_LIMIT")
        for value in values:
            common = {"id", "name", "label", "rotation", "translation_mm"}
            extra = {"source_id", "component_ids"} if group == "placements" else {"role", "size_mm"}
            optional = {"cutouts"} if group == "boxes" else {"role"}
            _object(value, common | extra | optional, {"id", "rotation", "translation_mm"} | extra, "элемента сборки")
            identifier = value["id"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier) or identifier in identifiers:
                _invalid("Идентификаторы элементов сборки должны быть уникальными")
            identifiers.add(identifier)
            item = {"id": identifier, "rotation": _rotation(value["rotation"]), "translation_mm": _vector(value["translation_mm"], "Перемещение")}
            names = [value[key] for key in ("name", "label") if key in value]
            if names:
                if any(not isinstance(name, str) or not name.strip() or len(name) > 300 or any(ord(char) < 32 for char in name) for name in names) or len(set(names)) != 1:
                    _invalid("Название элемента должно быть непустым текстом до 300 символов; name и label должны совпадать")
                item["name"] = names[0].strip()
            if group == "placements":
                if not isinstance(value["source_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["source_id"]):
                    _invalid("Некорректный SHA-256 исходного STEP")
                _, _, ids = step.validate_section_parameters("z", 0, value["component_ids"])
                if ids is None:
                    _invalid("Укажите конкретные тела исходного STEP")
                item.update(source_id=value["source_id"], component_ids=sorted(ids))
                if "role" in value:
                    if value["role"] not in ("profile", "frame", "fastener", "substrate", "hardware", "leaf", "lighting_housing", "diffuser"):
                        _invalid("Неизвестная роль исходного тела сборки")
                    item["role"] = value["role"]
                count += len(ids)
            else:
                if value["role"] not in ("panel", "substrate", "frame", "leaf", "hardware_envelope", "lighting_housing", "diffuser", "circuit_envelope", "adhesive"):
                    _invalid("Неизвестная роль параметрического тела сборки")
                item.update(role=value["role"], size_mm=_vector(value["size_mm"], "Размер тела", minimum=1e-5, maximum=1e6))
                if "cutouts" in value:
                    cutouts = _cutouts(value["cutouts"], item["size_mm"])
                    # Preserve the exact legacy canonical request and hashes.
                    if cutouts:
                        item["cutouts"] = cutouts
                count += 1
            canonical[group].append(item)
        canonical[group].sort(key=lambda item: item["id"])
    if not 1 <= count <= MAX_BODIES:
        _invalid("Сборка должна содержать от 1 до 32 тел", code="ASSEMBLY_LIMIT")
    if "fastenings" in spec:
        fastenings = _validate_fastenings(spec["fastenings"], canonical)
        if fastenings:
            canonical["fastenings"] = fastenings
    declared = {item["fastener_id"] for item in canonical.get("fastenings", [])}
    physical = {item["id"] for item in canonical["placements"] if item.get("role") == "fastener"}
    if declared != physical:
        _invalid("Каждый STEP-крепёж должен иметь одно явное проверяемое соединение")
    if "contacts" in spec:
        contacts = _validate_contacts(spec["contacts"], canonical)
        if contacts:
            canonical["contacts"] = contacts
    return canonical


def _validate_contacts(values, spec):
    if not isinstance(values, list) or len(values) > MAX_CONTACTS:
        _invalid("Допускается не более 96 проверяемых опорных контактов", code="ASSEMBLY_LIMIT")
    instances = {item["id"]: item for group in ("placements", "boxes") for item in spec[group]}
    identifiers, pairs, result = set(), set(), []
    for value in values:
        fields = {"id", "body_a", "body_b", "min_area_mm2"}
        _object(value, fields, fields, "опорного контакта")
        identifier = value["id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier) or identifier in identifiers:
            _invalid("Идентификаторы опорных контактов должны быть уникальными")
        identifiers.add(identifier)
        a, b = value["body_a"], value["body_b"]
        if not isinstance(a, str) or not isinstance(b, str) or a == b:
            _invalid("Контакт должен связывать два разных тела")
        for key in (a, b):
            if key not in instances or len(instances[key].get("component_ids", [1])) != 1 or instances[key].get("role") == "fastener":
                _invalid("Опорный контакт должен ссылаться на одиночные тела; крепёж проверяется отдельным соединением")
        pair = tuple(sorted((a, b)))
        if pair in pairs:
            _invalid("Опорный контакт этой пары уже указан")
        pairs.add(pair)
        result.append({"id": identifier, "body_a": a, "body_b": b,
                       "min_area_mm2": _number(value["min_area_mm2"], "Минимальная площадь контакта", minimum=.1, maximum=1e9)})
    return sorted(result, key=lambda item: item["id"])


def _validate_fastenings(values, spec):
    """Explicit seats and finite shaft zones, never a body-pair whitelist."""
    if not isinstance(values, list) or len(values) > MAX_BODIES:
        _invalid("Допускается не более 32 соединений крепежом", code="ASSEMBLY_LIMIT")
    instances = {item["id"]: item for group in ("placements", "boxes") for item in spec[group]}
    seen, fasteners, result = set(), set(), []
    fields = {"id", "fastener_id", "seat_id", "axis_origin_mm", "axis_direction", "shaft_radius_mm", "head_radius_mm", "head_height_mm", "shaft_length_mm", "min_engagement_mm", "engagement_targets"}
    for value in values:
        _object(value, fields, fields, "соединения крепежом")
        identifier = value["id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier) or identifier in seen:
            _invalid("Идентификаторы соединений должны быть уникальными")
        seen.add(identifier)
        fastener_id, seat_id = value["fastener_id"], value["seat_id"]
        targets = value["engagement_targets"]
        if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_BODIES or any(not isinstance(key, str) for key in targets) or len(set(targets)) != len(targets):
            _invalid("Укажите уникальные тела, в которые входит стержень крепежа")
        if not isinstance(fastener_id, str) or not isinstance(seat_id, str) or fastener_id in fasteners or fastener_id in targets or seat_id not in targets:
            _invalid("Крепёж и опорное тело должны однозначно задавать соединение")
        for key in [fastener_id, seat_id, *targets]:
            if key not in instances or len(instances[key].get("component_ids", [1])) != 1:
                _invalid("Соединение должно ссылаться на одно существующее тело каждого элемента")
        if instances[fastener_id].get("role") != "fastener" or "source_id" not in instances[fastener_id] or any(instances[key].get("role") == "fastener" for key in targets):
            _invalid("Крепёж должен быть исходным STEP-телом с ролью fastener; крепёж не может крепиться в другой крепёж")
        fasteners.add(fastener_id)
        direction = _vector(value["axis_direction"], "Ось крепежа", minimum=-1., maximum=1.)
        if abs(sum(x*x for x in direction) - 1.) > 1e-9:
            _invalid("Ось крепежа должна быть единичным вектором")
        item = {"id": identifier, "fastener_id": fastener_id, "seat_id": seat_id,
                "axis_origin_mm": _vector(value["axis_origin_mm"], "Плоскость опоры головки"),
                "axis_direction": direction, "engagement_targets": sorted(targets)}
        for field in ("shaft_radius_mm", "head_radius_mm", "head_height_mm", "shaft_length_mm", "min_engagement_mm"):
            item[field] = _number(value[field], field, minimum=.001, maximum=1000.)
        if item["head_radius_mm"] <= item["shaft_radius_mm"] + .1 or item["min_engagement_mm"] > item["shaft_length_mm"]:
            _invalid("Головка должна быть шире стержня, а глубина зацепления — не длиннее стержня")
        result.append(item)
    return sorted(result, key=lambda item: item["id"])


def validate_assembly_sources(sources: dict[str, bytes], spec: dict) -> None:
    if not isinstance(sources, dict) or len(sources) > MAX_SOURCES:
        _invalid("Допускается не более восьми исходных STEP", code="ASSEMBLY_LIMIT")
    expected = {placement["source_id"] for placement in spec["placements"]}
    if set(sources) != expected:
        _invalid("Исходные STEP должны точно соответствовать источникам сборки")
    total = 0
    for source_id, content in sources.items():
        if not isinstance(content, bytes):
            _invalid("Исходный STEP должен передаваться байтами")
        total += len(content)
        if total > MAX_INPUT_BYTES:
            _invalid("Общий размер исходных STEP превышает 64 МиБ", code="ASSEMBLY_LIMIT")
        if hashlib.sha256(content).hexdigest() != source_id:
            _invalid("SHA-256 исходного STEP не соответствует переданным байтам")
        step.validate_step_content(content)


def _export_step(shapes, oc, *, fastened=False):
    from OCP.BRep import BRep_Builder
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    from OCP.TopoDS import TopoDS_Compound
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    writer = STEPControl_Writer()
    from OCP.Interface import Interface_Static
    previous_pcurves = Interface_Static.IVal_s("write.surfacecurve.mode")
    try:
        if fastened and not Interface_Static.SetIVal_s("write.surfacecurve.mode", 0):
            _invalid("Не удалось настроить точный компактный экспорт STEP", code="ASSEMBLY_EXPORT_FAILED")
        # Official OCCT STEP mode 0 omits redundant 2D pcurves, not 3D edges,
        # surfaces, threads, solids or placements. The reader reconstructs the
        # pcurves; all contacts/collisions/cuts below use that portable result.
        # https://github.com/Open-Cascade-SAS/OCCT/wiki/step#write.surfacecurvemode
        if writer.Transfer(compound, STEPControl_AsIs) != oc["IFSelect_RetDone"]:
            _invalid("Не удалось подготовить STEP сборки", code="ASSEMBLY_EXPORT_FAILED")
        output = io.BytesIO()
        if writer.WriteStream(output) != oc["IFSelect_RetDone"]:
            _invalid("Не удалось экспортировать STEP сборки", code="ASSEMBLY_EXPORT_FAILED")
    finally:
        if fastened:
            Interface_Static.SetIVal_s("write.surfacecurve.mode", previous_pcurves)
    content = output.getvalue()
    maximum = MAX_FASTENED_EXPORT_BYTES if fastened else MAX_EXPORT_BYTES
    if len(content) > maximum:
        _invalid(f"STEP сборки превышает переносимый лимит {maximum // (1024 * 1024)} МиБ", code="ASSEMBLY_LIMIT")
    # OCCT's only clock-dependent value is HEADER.FILE_NAME's timestamp. Fix
    # precisely that field, never strings/entities in the actual CAD data.
    header, separator, data = content.partition(b"ENDSEC;")
    header, count = re.subn(rb"(FILE_NAME\s*\(\s*'(?:''|[^'])*'\s*,\s*)'(?:''|[^'])*'", rb"\1'2000-01-01T00:00:00'", header, count=1)
    if not separator or count != 1:
        _invalid("Неизвестный заголовок экспорта STEP", code="ASSEMBLY_EXPORT_FAILED")
    content = header + separator + data
    step.validate_step_content(content)
    return content


def _distance(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _loops(wires, axes, oc, budget):
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.TopAbs import TopAbs_REVERSED
    loops = []
    for index in range(1, wires.Length() + 1):
        wire = oc["TopoDS"].Wire_s(wires.Value(index))
        if not wire.Closed():
            _invalid("Сечение сборки содержит незамкнутый контур. Измените плоскость или проверьте исходную модель.", code="ASSEMBLY_OPEN_SECTION")
        explorer, points, visited = BRepTools_WireExplorer(wire), [], 0
        while explorer.More():
            edge = explorer.Current()
            sampled, _ = step._sample_edge(edge, oc, preview=False)
            if edge.Orientation() == TopAbs_REVERSED:
                sampled.reverse()
            if sampled:
                if points and _distance(points[-1], sampled[0]) > step.CONNECT_TOLERANCE_MM:
                    _invalid("Рёбра сечения не образуют непрерывный контур", code="ASSEMBLY_OPEN_SECTION")
                points.extend(sampled[1:] if points else sampled)
            visited += 1
            explorer.Next()
        if visited != len(step._shapes(wire, oc["TopAbs_EDGE"], oc)):
            _invalid("Контур сечения содержит неупорядоченные или разветвлённые рёбра", code="ASSEMBLY_OPEN_SECTION")
        if len(points) < 4 or _distance(points[-1], points[0]) > step.CONNECT_TOLERANCE_MM:
            _invalid("Сечение сборки не содержит замкнутого контура достаточной длины", code="ASSEMBLY_OPEN_SECTION")
        points[-1] = points[0]
        budget[0] += len(points)
        if budget[0] > MAX_COMBINED_POINTS:
            _invalid("Контуры и рёбра сборки превышают лимит 120 000 точек", code="ASSEMBLY_LIMIT")
        loops.append([[point[axes[0]], point[axes[1]]] for point in points])
    return loops


def _boolean_shape(operation_type, a, b):
    from OCP.TopTools import TopTools_ListOfShape
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(a)
    tools.Append(b)
    operation = operation_type()
    operation.SetArguments(arguments)
    operation.SetTools(tools)
    operation.SetNonDestructive(True)
    operation.Build()
    if not operation.IsDone():
        _invalid("Не удалось проверить геометрию соединения", code="ASSEMBLY_COLLISION_FAILED")
    return operation.Shape()


def _mass(shape, oc, *, surface=False):
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    kind = oc["TopAbs_FACE"] if surface else oc["TopAbs_SOLID"]
    if shape.IsNull() or not step._shapes(shape, kind, oc):
        return 0.
    if not oc["BRepCheck_Analyzer"](shape).IsValid():
        _invalid("Проверка соединения вернула некорректную геометрию", code="ASSEMBLY_COLLISION_FAILED")
    properties = GProp_GProps()
    if surface:
        BRepGProp.SurfaceProperties_s(shape, properties)
    else:
        BRepGProp.VolumeProperties_s(shape, properties, True, False, False)
    value = properties.Mass()
    if not math.isfinite(value) or value < -1e-7:
        _invalid("Некорректная мера геометрии соединения", code="ASSEMBLY_COLLISION_FAILED")
    return max(0., value)


def _cylinder(fastening, radius, start, length):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    origin, direction = fastening["axis_origin_mm"], fastening["axis_direction"]
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*[origin[k] + start * direction[k] for k in range(3)]), gp_Dir(*direction)), radius, length).Shape()


def _axial_span(shape, fastening, oc):
    """Measure along the actual connection axis, not its world AABB diagonal."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf
    z = fastening["axis_direction"]
    reference = [1., 0., 0.] if abs(z[0]) < .8 else [0., 1., 0.]
    x = [reference[1]*z[2]-reference[2]*z[1], reference[2]*z[0]-reference[0]*z[2], reference[0]*z[1]-reference[1]*z[0]]
    length = math.sqrt(sum(v*v for v in x))
    x = [v/length for v in x]
    y = [z[1]*x[2]-z[2]*x[1], z[2]*x[0]-z[0]*x[2], z[0]*x[1]-z[1]*x[0]]
    origin = fastening["axis_origin_mm"]
    transform = gp_Trsf()
    transform.SetValues(*[value for row in (x, y, z) for value in (*row, -sum(row[k]*origin[k] for k in range(3)))])
    bounds = step._bbox(BRepBuilderAPI_Transform(shape, transform, False, False).Shape(), oc)
    return [bounds["min"][2], bounds["max"][2]] if bounds else [0., 0.]


def _engagement_depth(shape, fastening, oc):
    # A hollow target may meet the shaft in separate thin skins. Do not count
    # the air between those skins as material engagement.
    intervals = sorted(_axial_span(solid, fastening, oc) for solid in step._shapes(shape, oc["TopAbs_SOLID"], oc))
    merged = []
    for start, end in intervals:
        start, end = max(0., start), min(fastening["shaft_length_mm"], end)
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end-start for start, end in merged)


def _fastening_geometry(solids, bodies, fastenings, oc):
    """Verify seats against exported solids before granting any shaft engagement.

    The head-bearing section is an exact planar common with both real solids,
    excluding the shaft disk. This cannot pass a floating head merely because
    the screw axis happens to cross the named target's bounding box.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
    by_instance = {body["instance_id"]: (body, solid) for body, solid in zip(bodies, solids)}
    prepared, checks = [], []
    for fastening in fastenings:
        body, solid = by_instance[fastening["fastener_id"]]
        seat_body, seat = by_instance[fastening["seat_id"]]
        radius, length = fastening["shaft_radius_mm"], fastening["shaft_length_mm"]
        head_radius, head_height = fastening["head_radius_mm"], fastening["head_height_mm"]
        # Numerical allowance is independent of the caller's collision tolerance.
        # It only protects STEP round-tripping, not grossly wrong axes or seats.
        epsilon = .002
        shaft = _cylinder(fastening, radius, 0., length + epsilon)
        head = _cylinder(fastening, head_radius + epsilon, -head_height-epsilon, head_height+epsilon)
        envelope = _boolean_shape(BRepAlgoAPI_Fuse, head, shaft)
        outside = _mass(_boolean_shape(BRepAlgoAPI_Cut, solid, envelope), oc)
        screw_volume = _mass(solid, oc)
        shape_tolerance = .001
        issues = []
        if outside > shape_tolerance:
            issues.append("FASTENER_AXIS_OR_ENVELOPE_MISMATCH")
        head_volume = _mass(_boolean_shape(BRepAlgoAPI_Common, solid, head), oc)
        shaft_shape = _boolean_shape(BRepAlgoAPI_Common, solid, shaft)
        shaft_volume = _mass(shaft_shape, oc)
        if head_volume <= .001 or shaft_volume <= .001:
            issues.append("FASTENER_HEAD_OR_SHAFT_MISSING")
        actual_span = _axial_span(shaft_shape, fastening, oc) if shaft_volume > 0 else [0., 0.]
        if abs(actual_span[1] - length) > .05 or actual_span[0] > .05:
            issues.append("FASTENER_LENGTH_MISMATCH")
        plane = gp_Pln(gp_Pnt(*fastening["axis_origin_mm"]), gp_Dir(*fastening["axis_direction"]))
        face = BRepBuilderAPI_MakeFace(plane, -head_radius*1.1, head_radius*1.1, -head_radius*1.1, head_radius*1.1).Face()
        bearing = _boolean_shape(BRepAlgoAPI_Common, solid, face)
        bearing = _boolean_shape(BRepAlgoAPI_Cut, bearing, _cylinder(fastening, radius+.05, -.01, .02))
        head_bearing_area = _mass(bearing, oc, surface=True)
        contact = _boolean_shape(BRepAlgoAPI_Common, bearing, seat)
        contact_area = _mass(contact, oc, surface=True)
        # Require substantial genuine bearing outside the shaft. The exported
        # source controls the available face area; the cylinder is no fake head.
        required_area = max(.1, head_bearing_area * .25)
        if head_bearing_area < .1 or contact_area + 1e-5 < required_area:
            issues.append("FASTENER_HEAD_NOT_SEATED")
        check = {"id": fastening["id"], "fastener_id": fastening["fastener_id"], "fastener_body_id": body["id"],
                 "seat_id": fastening["seat_id"], "seat_body_id": seat_body["id"], "status": "invalid" if issues else "valid",
                 "axis_origin_mm": fastening["axis_origin_mm"], "axis_direction": fastening["axis_direction"],
                 "head_bearing_area_mm2": head_bearing_area, "bearing_contact_area_mm2": contact_area,
                 "required_bearing_area_mm2": required_area, "outside_envelope_volume_mm3": outside,
                 "envelope_tolerance_mm3": shape_tolerance,
                 "shaft_span_mm": actual_span, "engagements": [], "issues": issues}
        checks.append(check)
        prepared.append({"spec": fastening, "shaft": shaft, "check": check})
    return prepared, checks


def _collisions(solids, bodies, tolerance, oc, fastenings=None):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
    prepared, checks = _fastening_geometry(solids, bodies, fastenings or [], oc)
    collisions, engagements = [], []
    pairs = {}
    for connection in prepared:
        spec = connection["spec"]
        for target in spec["engagement_targets"]:
            pairs[frozenset((spec["fastener_id"], target))] = (connection, target)
    for i in range(len(solids)):
        for j in range(i + 1, len(solids)):
            a, b = bodies[i]["bounds_mm"], bodies[j]["bounds_mm"]
            if any(min(a["max"][k], b["max"][k]) <= max(a["min"][k], b["min"][k]) for k in range(3)):
                continue
            common = _boolean_shape(BRepAlgoAPI_Common, solids[i], solids[j])
            volume = _mass(common, oc)
            if volume <= min(tolerance, .001):
                continue
            declaration = pairs.get(frozenset((bodies[i]["instance_id"], bodies[j]["instance_id"])))
            illegal_volume = volume
            if declaration:
                connection, target = declaration
                spec, check = connection["spec"], connection["check"]
                engaged = _boolean_shape(BRepAlgoAPI_Common, common, connection["shaft"])
                engagement_volume = _mass(engaged, oc)
                outside_shaft = _mass(_boolean_shape(BRepAlgoAPI_Cut, common, connection["shaft"]), oc)
                span = _axial_span(engaged, spec, oc) if engagement_volume > 0 else [0., 0.]
                depth = _engagement_depth(engaged, spec, oc) if engagement_volume > 0 else 0.
                valid = not check["issues"] and outside_shaft <= .001 and depth + 1e-5 >= spec["min_engagement_mm"] and engagement_volume > .001
                measured = {"fastening_id": spec["id"], "fastener_id": spec["fastener_id"], "target_id": target,
                            "body_a": i + 1, "body_b": j + 1, "volume_mm3": engagement_volume,
                            "axial_span_mm": span, "depth_mm": depth, "min_engagement_mm": spec["min_engagement_mm"],
                            "outside_shaft_volume_mm3": outside_shaft, "status": "valid" if valid else "invalid"}
                check["engagements"].append(measured)
                if outside_shaft > .001:
                    check["issues"].append("FASTENER_HEAD_OR_OFF_AXIS_COLLISION")
                if depth + 1e-5 < spec["min_engagement_mm"] or engagement_volume <= .001:
                    check["issues"].append("FASTENER_INSUFFICIENT_ENGAGEMENT")
                if valid:
                    engagements.append(measured)
                    illegal_volume = outside_shaft
            if illegal_volume > (min(tolerance, .001) if fastenings else tolerance):
                collisions.append({"body_a": i + 1, "body_b": j + 1, "volume_mm3": illegal_volume})
    for connection in prepared:
        check, spec = connection["check"], connection["spec"]
        measured = {item["target_id"] for item in check["engagements"]}
        if measured != set(spec["engagement_targets"]):
            check["issues"].append("FASTENER_TARGET_NOT_REACHED")
        check["issues"] = sorted(set(check["issues"]))
        check["status"] = "invalid" if check["issues"] else "valid"
    return collisions, checks, engagements


def _contact_checks(solids, bodies, contacts, oc):
    """Actual finite-area contact; overlapping or floating bodies fail closed."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    by_instance = {body["instance_id"]: (body, solid) for body, solid in zip(bodies, solids)}
    checks = []
    for contact in contacts:
        a, shape_a = by_instance[contact["body_a"]]
        b, shape_b = by_instance[contact["body_b"]]
        # Solid/solid Common deliberately omits a merely shared face in OCCT.
        # Face/solid Common preserves it, including a partial bearing patch.
        # Prefer the simpler body, typically six backing/adhesive-box faces.
        faces_a = step._shapes(shape_a, oc["TopAbs_FACE"], oc)
        faces_b = step._shapes(shape_b, oc["TopAbs_FACE"], oc)
        faces, other = (faces_a, shape_b) if len(faces_a) <= len(faces_b) else (faces_b, shape_a)
        area = sum(_mass(_boolean_shape(BRepAlgoAPI_Common, face, other), oc, surface=True) for face in faces)
        overlap = _mass(_boolean_shape(BRepAlgoAPI_Common, shape_a, shape_b), oc)
        issues = []
        if area + 1e-5 < contact["min_area_mm2"]:
            issues.append("ASSEMBLY_CONTACT_NOT_SEATED")
        if overlap > .001:
            issues.append("ASSEMBLY_CONTACT_HAS_OVERLAP")
        checks.append({"id": contact["id"], "body_a": contact["body_a"], "body_b": contact["body_b"],
                       "body_a_id": a["id"], "body_b_id": b["id"], "contact_area_mm2": area,
                       "min_area_mm2": contact["min_area_mm2"], "overlap_volume_mm3": overlap,
                       "status": "invalid" if issues else "valid", "issues": issues})
    return checks


def _generated_box(item, oc):
    """Cut material locally before applying the instance's rigid transform."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    shape = BRepPrimAPI_MakeBox(*item["size_mm"]).Shape()
    if not item.get("cutouts"):
        return shape
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopTools import TopTools_ListOfShape

    def boolean(operation_type, source, tool, cutout):
        arguments, cutters = TopTools_ListOfShape(), TopTools_ListOfShape()
        arguments.Append(source)
        cutters.Append(tool)
        operation = operation_type()
        operation.SetArguments(arguments)
        operation.SetTools(cutters)
        operation.SetNonDestructive(True)
        operation.Build()
        if not operation.IsDone():
            _invalid("Не удалось выполнить объёмный вырез панели", code="ASSEMBLY_CUTOUT_FAILED", details={"instance_id": item["id"], "cutout_id": cutout["id"]})
        return operation.Shape()

    def volume(solid):
        properties = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, properties, True, False, False)
        mass = properties.Mass()
        if not math.isfinite(mass) or mass < 0:
            _invalid("Некорректный объём обработанной панели", code="ASSEMBLY_CUTOUT_FAILED")
        return mass

    for cutout in item["cutouts"]:
        details = {"instance_id": item["id"], "cutout_id": cutout["id"]}
        cutter = BRepPrimAPI_MakeBox(oc["gp_Pnt"](*cutout["origin_mm"]), *cutout["size_mm"]).Shape()
        common = boolean(BRepAlgoAPI_Common, shape, cutter, cutout)
        if common.IsNull() or not step._shapes(common, oc["TopAbs_SOLID"], oc) or not oc["BRepCheck_Analyzer"](common).IsValid() or volume(common) <= 0:
            _invalid("Вырез не удаляет материал оставшейся панели", code="ASSEMBLY_CUTOUT_INVALID", details=details)
        cut = boolean(BRepAlgoAPI_Cut, shape, cutter, cutout)
        solids = [] if cut.IsNull() else step._shapes(cut, oc["TopAbs_SOLID"], oc)
        if len(solids) != 1 or not oc["BRepCheck_Analyzer"](cut).IsValid() or volume(solids[0]) <= 0:
            _invalid("После выреза панель должна оставаться одним непустым корректным телом", code="ASSEMBLY_CUTOUT_INVALID", details=details)
        if volume(solids[0]) >= volume(shape):
            _invalid("Вырез не уменьшил объём панели", code="ASSEMBLY_CUTOUT_INVALID", details=details)
        # Do not silently discard free faces or edges outside that one solid.
        if any(len(step._shapes(cut, kind, oc)) != len(step._shapes(solids[0], kind, oc)) for kind in (oc["TopAbs_FACE"], oc["TopAbs_EDGE"])):
            _invalid("Вырез оставил несвязанные поверхности или рёбра", code="ASSEMBLY_CUTOUT_INVALID", details=details)
        shape = solids[0]
    return shape


def build_assembly(sources: dict[str, bytes], spec: dict) -> dict:
    """Native work only. HTTP callers must use run_assembly_build isolation."""
    spec = validate_assembly_spec(spec)
    validate_assembly_sources(sources, spec)
    oc = step._occt()
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf
    loaded = {source_id: step._load(sources[source_id], oc)[1] for source_id in sorted(sources)}
    solids, bodies = [], []
    complexity = {"faces": 0, "edges": 0}
    for group in ("placements", "boxes"):
        for item in spec[group]:
            if group == "placements":
                source_solids = loaded[item["source_id"]]
                if max(item["component_ids"]) > len(source_solids):
                    _invalid("Выбранного тела нет в исходном STEP", code="STEP_SELECTION")
                selected = [(index, source_solids[index - 1]) for index in item["component_ids"]]
            else:
                selected = [(None, _generated_box(item, oc))]
            for source_index, shape in selected:
                trsf = gp_Trsf()
                trsf.SetValues(*[value for row, shift in zip(item["rotation"], item["translation_mm"]) for value in (*row, shift)])
                # Retain shared source topology for repeated rigid STEP instances.
                # Legacy assemblies keep their byte-identical export path.
                shape = BRepBuilderAPI_Transform(shape, trsf, not bool(spec.get("fastenings")), False).Shape()
                if shape.IsNull() or not oc["BRepCheck_Analyzer"](shape).IsValid():
                    _invalid("Сборка содержит некорректное исходное или преобразованное тело", code="ASSEMBLY_INVALID_SHAPE")
                complexity["faces"] += len(step._shapes(shape, oc["TopAbs_FACE"], oc))
                complexity["edges"] += len(step._shapes(shape, oc["TopAbs_EDGE"], oc))
                if complexity["faces"] > step.MAX_FACES or complexity["edges"] > step.MAX_EDGES:
                    _invalid("Суммарная геометрия сборки превышает лимит сложности", code="ASSEMBLY_LIMIT")
                body = {"id": len(solids) + 1, "instance_id": item["id"], "label": item.get("name", item["id"]), "kind": "step" if source_index is not None else "generated_box", "rotation": item["rotation"], "translation_mm": item["translation_mm"], "bounds_mm": step._bbox(shape, oc)}
                if source_index is not None:
                    body.update(source_id=item["source_id"], source_component_id=source_index)
                    if item.get("role"):
                        body["role"] = item["role"]
                else:
                    body.update(role=item["role"], size_mm=item["size_mm"])
                    if item.get("cutouts"):
                        body["cutouts"] = item["cutouts"]
                solids.append(shape)
                bodies.append(body)
    content = _export_step(solids, oc, fastened=bool(spec.get("fastenings")))
    # Cut the actual portable STEP, not a subtly different pre-export shape.
    # Verify stable body order before associating source provenance with it.
    _, reloaded, _ = step._load(content, oc)
    if len(reloaded) != len(solids):
        _invalid("Экспорт STEP изменил состав тел сборки", code="ASSEMBLY_EXPORT_FAILED")
    for body, original, shape in zip(bodies, solids, reloaded):
        bounds = step._bbox(shape, oc)
        if any(abs(bounds[key][k] - body["bounds_mm"][key][k]) > 1e-5 for key in ("min", "max") for k in range(3)):
            _invalid("Экспорт STEP изменил порядок или положение тел", code="ASSEMBLY_EXPORT_FAILED")
        if spec.get("fastenings"):
            before, after = _mass(original, oc), _mass(shape, oc)
            if (abs(before-after) > max(1e-5, before*1e-8)
                    or len(step._shapes(original, oc["TopAbs_FACE"], oc)) != len(step._shapes(shape, oc["TopAbs_FACE"], oc))):
                _invalid("Компактный STEP изменил объём или поверхности исходных тел", code="ASSEMBLY_EXPORT_FAILED")
        body["bounds_mm"] = bounds
    solids = reloaded
    collisions, fastening_checks, intentional_engagements = _collisions(solids, bodies, spec["collision_tolerance_mm3"], oc, spec.get("fastenings"))
    contact_checks = _contact_checks(solids, bodies, spec.get("contacts", []), oc)
    axis, offset = spec["section"]["axis"], spec["section"]["offset_mm"]
    axis_index = "xyz".index(axis)
    axes = [i for i in range(3) if i != axis_index]
    origin, normal = [0., 0., 0.], [0., 0., 0.]
    origin[axis_index], normal[axis_index] = offset, 1.
    plane = oc["gp_Pln"](oc["gp_Pnt"](*origin), oc["gp_Dir"](*normal))
    components, all_points, budget, combined = [], [], [0], [0]
    warnings = ["Составная геометрия не является утверждённым монтажным решением. Проверьте посадки, материалы и крепёж по методичке.", "Кривые сечения аппроксимированы: 0,02 мм — цель дискретизации, а не производственный допуск."]
    for collision in collisions:
        warnings.append(f"Пересечение тел {collision['body_a']} и {collision['body_b']}: {collision['volume_mm3']:.6g} мм³. Требуется проверка посадки.")
    for check in fastening_checks:
        if check["status"] == "invalid":
            warnings.append(f"Крепёж {check['fastener_id']}: соединение не прошло проверку ({', '.join(check['issues'])}).")
    for check in contact_checks:
        if check["status"] == "invalid":
            warnings.append(f"Опора {check['body_a']} / {check['body_b']}: контакт не прошёл проверку ({', '.join(check['issues'])}).")
    for body, solid in zip(bodies, solids):
        bounds, local_warnings = body["bounds_mm"], []
        paths, loops, edge_count, wire_count = [], [], 0, 0
        if bounds["min"][axis_index] - step.CONNECT_TOLERANCE_MM <= offset <= bounds["max"][axis_index] + step.CONNECT_TOLERANCE_MM:
            operation = oc["BRepAlgoAPI_Section"](solid, plane, False)
            operation.SetNonDestructive(True)
            operation.Build()
            if not operation.IsDone():
                _invalid("Не удалось построить сечение тела сборки", code="STEP_SECTION_FAILED")
            cut = operation.Shape()
            old_budget = budget[0]
            paths3, _, edge_count = step._sample_paths(cut, oc, preview=False, budget=budget)
            combined[0] += budget[0] - old_budget
            sequence, wires = oc["TopTools_HSequenceOfShape"](), oc["TopTools_HSequenceOfShape"]()
            for edge in step._shapes(cut, oc["TopAbs_EDGE"], oc):
                sequence.Append(edge)
            oc["ShapeAnalysis_FreeBounds"].ConnectEdgesToWires_s(sequence, step.CONNECT_TOLERANCE_MM, False, wires)
            if sum(len(step._shapes(wires.Value(i), oc["TopAbs_EDGE"], oc)) for i in range(1, wires.Length() + 1)) != sequence.Length():
                _invalid("Не все рёбра сечения удалось соединить в контуры", code="ASSEMBLY_OPEN_SECTION")
            loops = _loops(wires, axes, oc, combined)
            wire_count = wires.Length()
            paths = [[[p[axes[0]], p[axes[1]]] for p in path] for path in paths3]
        if not paths:
            local_warnings.append("Плоскость не пересекает это тело; на сечении оно не показано.")
        if combined[0] > MAX_COMBINED_POINTS:
            _invalid("Контуры и рёбра сборки превышают лимит 120 000 точек", code="ASSEMBLY_LIMIT")
        body["section_loops"] = loops
        all_points.extend(point for path in paths for point in path)
        components.append({"id": body["id"], "label": body["label"], "edge_count": edge_count, "wire_count": wire_count, "closed_wire_count": wire_count, "open_wire_count": 0, "paths": paths, "warnings": local_warnings})
        warnings.extend(f"{body['label']}: {warning}" for warning in local_warnings)
    bounds = None
    if all_points:
        minimum = [min(point[i] for point in all_points) for i in range(2)]
        maximum = [max(point[i] for point in all_points) for i in range(2)]
        bounds = {"min": minimum, "max": maximum, "size": [maximum[i] - minimum[i] for i in range(2)]}
    result = {**step._base(content, oc), "schema": "step-section/1", "axis": axis, "offset_mm": offset, "projection_axes": ["xyz"[i] for i in axes], "components": components, "bounds_mm": bounds, "point_count": budget[0], "warnings": warnings, "approximate": True, "deflection_mm": step.SECTION_DEFLECTION_MM, "connection_tolerance_mm": step.CONNECT_TOLERANCE_MM}
    from .cad_sections import SectionResult
    result = SectionResult.model_validate(result).portable()
    built = {"schema": "assembled-section-build/1", "request": spec, "result": result, "bodies": bodies, "collisions": collisions, "warnings": warnings, "assembled_step": {"source_id": result["source_id"], "byte_length": len(content), "filename": f"assembly-{result['source_id'][:12]}.step", "base64": base64.b64encode(content).decode("ascii")}}
    if spec.get("fastenings"):
        built.update(fastening_checks=fastening_checks, intentional_engagements=intentional_engagements)
    if spec.get("contacts"):
        built["contact_checks"] = contact_checks
    return built
