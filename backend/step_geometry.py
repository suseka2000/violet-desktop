"""Bounded STEP geometry, deliberately separate from room-plan semantics.

OCCT is optional and loaded only inside the isolated worker. Curved previews
and section DXFs are sampled polylines, not certified manufacturing contours.
No native model, material or installation approval is inferred from a solid.
"""
from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path
import re
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar

MAX_STEP_BYTES = 64 * 1024 * 1024
MAX_COMPONENTS = 256
MAX_FACES = 20_000
MAX_EDGES = 30_000
MAX_POINTS = 60_000
MAX_SECTION_EDGES = 12_000
PREVIEW_DEFLECTION_MM = 0.1
SECTION_DEFLECTION_MM = 0.02
CONNECT_TOLERANCE_MM = 1e-5
_RIGID_BOUNDS = ContextVar("native_rigid_bounds", default=None)


@contextmanager
def rigid_bounds_cache():
    """Reuse exact native bounds only within one immutable assembly build.

    Isolated workers create geometry with non-destructive Boolean operations.
    Equal TShapes at the SAME rotation differ only by rigid translation: their
    exact bounds translate by that same vector. No rounded geometry signature,
    cross-request state, tessellation, or transformed-box approximation is used.
    """
    existing=_RIGID_BOUNDS.get()
    if existing is not None:
        yield existing
        return
    cache={"bounds":{},"hits":0,"misses":0}
    token=_RIGID_BOUNDS.set(cache)
    try:yield cache
    finally:_RIGID_BOUNDS.reset(token)


class StepGeometryError(ValueError):
    def __init__(self, message: str, *, code: str = "STEP_INVALID", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


def validate_step_content(content: bytes, *, max_bytes="source") -> None:
    if max_bytes == "source":max_bytes = MAX_STEP_BYTES
    if not isinstance(content, bytes):
        raise StepGeometryError("STEP должен передаваться как байты файла")
    if not content or (max_bytes is not None and len(content) > max_bytes):
        raise StepGeometryError("Размер STEP должен быть от 1 байта до 64 МиБ", code="STEP_LIMIT")
    if not content.lstrip().startswith(b"ISO-10303-21;") or not content.rstrip().endswith(b"END-ISO-10303-21;"):
        raise StepGeometryError("Нужен текстовый STEP (.step/.stp) с целым заголовком и окончанием")
    # Exclude quoted strings and comments before looking for STEP entities.
    # External-reference reads must be refused before invoking the native CAD reader.
    tokens = re.sub(rb"'(?:''|[^'])*'|/\*.*?\*/", b"''", content, flags=re.S).upper()
    forbidden = re.search(rb"\b(?:EXTERNAL_[A-Z0-9_]+|EXTERNALLY_DEFINED_[A-Z0-9_]+|DOCUMENT_FILE)\s*\(|\b(?:REFERENCE|ANCHOR)\s*;", tokens)
    if forbidden:
        raise StepGeometryError("STEP с внешними ссылками не поддерживается. Экспортируйте самодостаточную сборку со всеми телами.", code="STEP_EXTERNAL_REFERENCE")
    if max_bytes is not None and len(re.findall(rb"#\d+\s*=", tokens)) > 1_000_000:
        raise StepGeometryError("В STEP слишком много записей", code="STEP_LIMIT")


def validate_section_parameters(axis: str, offset_mm: float, component_ids: list[int] | None) -> tuple[str, float, list[int] | None]:
    if not isinstance(axis, str) or axis not in {"x", "y", "z"}:
        raise StepGeometryError("Ось сечения должна быть x, y или z", code="STEP_SELECTION")
    if isinstance(offset_mm, bool) or not isinstance(offset_mm, (int, float)) or not math.isfinite(offset_mm) or abs(offset_mm) > 1e9:
        raise StepGeometryError("Смещение сечения должно быть конечным числом миллиметров в пределах ±1 000 000 000", code="STEP_SELECTION")
    if component_ids is not None:
        if not isinstance(component_ids, list) or not component_ids or len(component_ids) > MAX_COMPONENTS:
            raise StepGeometryError("Выберите от 1 до 256 тел либо все тела", code="STEP_SELECTION")
        if any(isinstance(i, bool) or not isinstance(i, int) or not 1 <= i <= MAX_COMPONENTS for i in component_ids) or len(set(component_ids)) != len(component_ids):
            raise StepGeometryError("Номера тел должны быть уникальными целыми числами от 1 до 256", code="STEP_SELECTION")
    return axis, float(offset_mm), component_ids


def _occt():
    try:
        from OCP import __version__
        from OCP.Bnd import Bnd_Box
        from OCP.BRep import BRep_Tool
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepCheck import BRepCheck_Analyzer
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TColStd import TColStd_SequenceOfAsciiString
        from OCP.TopAbs import TopAbs_SOLID, TopAbs_FACE, TopAbs_EDGE
        from OCP.TopExp import TopExp
        from OCP.TopTools import TopTools_IndexedMapOfShape, TopTools_HSequenceOfShape
        from OCP.TopoDS import TopoDS
        from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
    except (ImportError, OSError) as exc:
        raise StepGeometryError("Модуль STEP не установлен. Установите необязательную зависимость cadquery-ocp-novtk==7.9.3.0 в окружение приложения.", code="STEP_UNAVAILABLE") from exc
    return locals()


def _shapes(shape, kind, oc):
    items = oc["TopTools_IndexedMapOfShape"]()
    oc["TopExp"].MapShapes_s(shape, kind, items)
    return [items.FindKey(i) for i in range(1, items.Extent() + 1)]


def _bbox(shape, oc):
    cache=_RIGID_BOUNDS.get()
    key=None;shift=None;measured=shape
    if cache is not None and not shape.IsNull():
        transform=shape.Location().Transformation()
        if not transform.IsNegative() and abs(transform.ScaleFactor()-1.)<1e-12:
            from OCP.TopLoc import TopLoc_Location
            from OCP.gp import gp_Trsf
            local=shape.Located(TopLoc_Location())
            rotation=tuple(transform.Value(i,j) for i in range(1,4) for j in range(1,4))
            # TShape handle equality is native identity; orientation is kept
            # separately (TopoDS Python wrappers themselves use object ==).
            # Exact
            # rotation doubles are retained; nearby rotations never alias.
            key=(local.TShape(),local.Orientation(),rotation)
            shift=[transform.Value(i,4) for i in range(1,4)]
            known=cache["bounds"].get(key)
            if known is not None:
                cache["hits"]+=1
                values=[known[i]+shift[i%3] for i in range(6)]
                if not all(math.isfinite(v) and abs(v)<=1e9 for v in values):
                    raise StepGeometryError("Габарит STEP неограничен или слишком велик",code="STEP_LIMIT")
                return {"min":values[:3],"max":values[3:],"size":[values[i+3]-values[i] for i in range(3)]}
            pose=gp_Trsf();pose.SetValues(*[v for row in range(3) for v in (*rotation[row*3:row*3+3],0.)])
            measured=local.Located(TopLoc_Location(pose));cache["misses"]+=1
    box = oc["Bnd_Box"]()
    oc["BRepBndLib"].AddOptimal_s(measured, box, False, False)
    if box.IsVoid():
        return None
    values = box.Get()
    if key is not None:
        # Bound memory even for intentionally complex source definitions.
        if len(cache["bounds"])<50000:cache["bounds"][key]=values
        values=tuple(values[i]+shift[i%3] for i in range(6))
    if not all(math.isfinite(v) and abs(v) <= 1e9 for v in values):
        raise StepGeometryError("Габарит STEP неограничен или слишком велик", code="STEP_LIMIT")
    return {"min": list(values[:3]), "max": list(values[3:]), "size": [values[i + 3] - values[i] for i in range(3)]}


def _load(content, oc, *, max_bytes="source"):
    validate_step_content(content, max_bytes=max_bytes)
    # The input name is generated by us. STEP content never chooses a path.
    with tempfile.TemporaryDirectory(prefix="interior-step-") as directory:
        source = Path(directory) / "source.step"
        source.write_bytes(content)
        reader = oc["STEPControl_Reader"]()
        if reader.ReadFile(str(source)) != oc["IFSelect_RetDone"]:
            raise StepGeometryError("OCCT не смог прочитать STEP")
        unit_sequences = [oc["TColStd_SequenceOfAsciiString"]() for _ in range(3)]
        reader.FileUnits(*unit_sequences)
        declared_units = [[seq.Value(i).ToCString() for i in range(1, seq.Length() + 1)] for seq in unit_sequences]
        reader.SetSystemLengthUnit(1.0)  # target millimetres, including inch-based STEP
        if reader.TransferRoots() <= 0:
            raise StepGeometryError("В STEP нет поддерживаемой геометрии")
        shape = reader.OneShape()
        if shape.IsNull():
            raise StepGeometryError("STEP содержит пустую геометрию")
        solids = _shapes(shape, oc["TopAbs_SOLID"], oc)
        if not solids:
            raise StepGeometryError("В STEP нет объёмных тел. Экспортируйте твёрдые тела, а не только поверхности.", code="STEP_NO_SOLIDS")
        if len(solids) > MAX_COMPONENTS:
            raise StepGeometryError("В STEP больше 256 тел. Разделите библиотеку на отдельные узлы.", code="STEP_LIMIT")
        if len(_shapes(shape, oc["TopAbs_FACE"], oc)) > MAX_FACES or len(_shapes(shape, oc["TopAbs_EDGE"], oc)) > MAX_EDGES:
            raise StepGeometryError("Геометрия STEP превышает лимит сложности", code="STEP_LIMIT")
        # Location-aware TopExp mapping preserves repeated assembly instances.
        return shape, solids, declared_units


def _point(curve, parameter):
    p = curve.Value(parameter)
    point = (p.X(), p.Y(), p.Z())
    if not all(math.isfinite(v) and abs(v) <= 1e9 for v in point):
        raise StepGeometryError("Кривая содержит неограниченную координату", code="STEP_LIMIT")
    return point


def _chord_distance(point, start, end):
    delta = [end[i] - start[i] for i in range(3)]
    length = sum(v * v for v in delta)
    t = max(0., min(1., sum((point[i] - start[i]) * delta[i] for i in range(3)) / length)) if length else 0.
    return math.sqrt(sum((point[i] - start[i] - t * delta[i]) ** 2 for i in range(3)))


def _sample_edge(edge, oc, *, preview):
    edge = oc["TopoDS"].Edge_s(edge)
    if oc["BRep_Tool"].Degenerated_s(edge):
        return [], False
    curve = oc["BRepAdaptor_Curve"](edge)
    start, end = curve.FirstParameter(), curve.LastParameter()
    if not all(math.isfinite(v) for v in (start, end)) or abs(start) > 1e12 or abs(end) > 1e12:
        raise StepGeometryError("Кривая STEP не имеет конечных границ", code="STEP_LIMIT")
    p0, p1 = _point(curve, start), _point(curve, end)
    if str(curve.GetType()).endswith("GeomAbs_Line"):
        return [list(p0), list(p1)], False
    target = PREVIEW_DEFLECTION_MM if preview else SECTION_DEFLECTION_MM
    max_points = 129 if preview else 2049
    # Seed 16 intervals rather than relying on a single midpoint: loops can
    # have identical endpoints/midpoints. Every evaluation is budgeted.
    nodes = [(start + (end - start) * i / 16, _point(curve, start + (end - start) * i / 16)) for i in range(17)]
    changed, approximate = True, False
    while changed:
        changed = False
        refined = [nodes[0]]
        for index in range(1, len(nodes)):
            a, pa = nodes[index - 1]
            b, pb = nodes[index]
            mid = (a + b) / 2
            pm = _point(curve, mid)
            if _chord_distance(pm, pa, pb) > target:
                if len(refined) + len(nodes) - index + 1 > max_points or mid == a or mid == b:
                    approximate = True
                else:
                    refined.append((mid, pm))
                    changed = True
            refined.append((b, pb))
        nodes = refined
        if len(nodes) >= max_points:
            approximate = True
            break
    if approximate and not preview:
        raise StepGeometryError("Кривая сечения слишком сложна для заданной детализации. Выберите отдельную деталь или другую плоскость.", code="STEP_SECTION_COMPLEX", details={"max_edge_points": max_points, "target_deflection_mm": target})
    return [list(point) for _, point in nodes], approximate


def _sample_paths(shape, oc, *, preview, budget):
    paths, coarsened = [], False
    edges = _shapes(shape, oc["TopAbs_EDGE"], oc)
    if not preview and len(edges) > MAX_SECTION_EDGES:
        raise StepGeometryError("Сечение содержит слишком много рёбер", code="STEP_LIMIT")
    for edge in edges:
        points, approximate = _sample_edge(edge, oc, preview=preview)
        coarsened |= approximate
        budget[0] += len(points)
        if budget[0] > MAX_POINTS:
            raise StepGeometryError("Превышен лимит 60 000 точек. Выберите меньше деталей или упростите модель.", code="STEP_LIMIT")
        if len(points) >= 2:
            paths.append(points)
    return paths, coarsened, len(edges)


def _base(content, oc):
    return {"source_id": hashlib.sha256(content).hexdigest(), "byte_length": len(content), "units": "mm", "occt_version": oc["__version__"], "engineering_approved": False}


def inspect_step(content: bytes) -> dict:
    """Native work: call run_step_inspect from HTTP, never this function directly."""
    validate_step_content(content)
    oc = _occt()
    shape, solids, declared_units = _load(content, oc)
    components, budget = [], [0]
    warnings = ["Тела STEP не определяют назначение узла или материал. Проверьте и подпишите компоненты вручную.", "Предпросмотр — приближённый каркас. Он не предназначен для измерений или изготовления."]
    for index, solid in enumerate(solids, 1):
        paths, coarsened, edge_count = _sample_paths(solid, oc, preview=True, budget=budget)
        valid = oc["BRepCheck_Analyzer"](solid).IsValid()
        components.append({"id": index, "index": index, "label": f"Тело {index}", "label_source": "generated", "valid": valid, "bounds_mm": _bbox(solid, oc), "edge_count": edge_count, "preview_paths": paths})
        if coarsened:
            warnings.append(f"Тело {index}: мелкие кривые каркаса упрощены до 129 точек на ребро; заданное отклонение 0,1 мм не гарантируется.")
        if not valid:
            warnings.append(f"Тело {index}: OCCT обнаружил проблемы геометрии. Сечение требует проверки.")
    return {**_base(content, oc), "schema": "step-inspection/1", "declared_units": declared_units, "bounds_mm": _bbox(shape, oc), "components": components, "component_count": len(components), "preview_point_count": budget[0], "preview_deflection_mm": PREVIEW_DEFLECTION_MM, "preview_approximate": True, "warnings": warnings}


def section_step(content: bytes, axis: str, offset_mm: float, component_ids: list[int] | None = None) -> dict:
    """Section each instance separately so contacting solids cannot cross-join."""
    axis, offset_mm, component_ids = validate_section_parameters(axis, offset_mm, component_ids)
    validate_step_content(content)
    oc = _occt()
    _, solids, _ = _load(content, oc)
    ids = component_ids if component_ids is not None else list(range(1, len(solids) + 1))
    if any(index > len(solids) for index in ids):
        raise StepGeometryError("Выбранного тела нет в этой модели", code="STEP_SELECTION")
    axis_index = "xyz".index(axis)
    axes = [i for i in range(3) if i != axis_index]
    normal, origin = [0., 0., 0.], [0., 0., 0.]
    normal[axis_index], origin[axis_index] = 1., offset_mm
    plane = oc["gp_Pln"](oc["gp_Pnt"](*origin), oc["gp_Dir"](*normal))
    components, budget, all_points = [], [0], []
    warnings = ["Геометрическое сечение — не утверждённый монтажный узел. Штриховки, материалы и размеры требуют проверки.", "Кривые представлены приближёнными отрезками; 0,02 мм — цель дискретизации, а не сертифицированный допуск."]
    for index in sorted(ids):
        solid = solids[index - 1]
        bounds = _bbox(solid, oc)
        local_warnings = []
        if not oc["BRepCheck_Analyzer"](solid).IsValid():
            local_warnings.append("Исходное тело имеет ошибки геометрии.")
        if offset_mm < bounds["min"][axis_index] - CONNECT_TOLERANCE_MM or offset_mm > bounds["max"][axis_index] + CONNECT_TOLERANCE_MM:
            components.append({"id": index, "label": f"Тело {index}", "edge_count": 0, "wire_count": 0, "closed_wire_count": 0, "open_wire_count": 0, "paths": [], "warnings": local_warnings})
            continue
        operation = oc["BRepAlgoAPI_Section"](solid, plane, False)
        operation.SetNonDestructive(True)
        operation.Build()
        if not operation.IsDone():
            raise StepGeometryError(f"Не удалось построить сечение тела {index}", code="STEP_SECTION_FAILED")
        cut = operation.Shape()
        paths3, _, edge_count = _sample_paths(cut, oc, preview=False, budget=budget)
        edges = _shapes(cut, oc["TopAbs_EDGE"], oc)
        edge_sequence, wires = oc["TopTools_HSequenceOfShape"](), oc["TopTools_HSequenceOfShape"]()
        for edge in edges:
            edge_sequence.Append(edge)
        oc["ShapeAnalysis_FreeBounds"].ConnectEdgesToWires_s(edge_sequence, CONNECT_TOLERANCE_MM, False, wires)
        closed = sum(bool(wires.Value(i).Closed()) for i in range(1, wires.Length() + 1))
        if closed < wires.Length():
            local_warnings.append(f"Незамкнутых контуров: {wires.Length() - closed}. Они не замыкаются автоматически и не подходят для автоматической штриховки.")
        paths = [[[point[axes[0]], point[axes[1]]] for point in path] for path in paths3]
        all_points.extend(point for path in paths for point in path)
        components.append({"id": index, "label": f"Тело {index}", "edge_count": edge_count, "wire_count": wires.Length(), "closed_wire_count": closed, "open_wire_count": wires.Length() - closed, "paths": paths, "warnings": local_warnings})
        warnings.extend(f"Тело {index}: {warning}" for warning in local_warnings)
    bounds = None
    if all_points:
        minimum = [min(p[i] for p in all_points) for i in range(2)]
        maximum = [max(p[i] for p in all_points) for i in range(2)]
        bounds = {"min": minimum, "max": maximum, "size": [maximum[i] - minimum[i] for i in range(2)]}
    else:
        warnings.append("Выбранная плоскость не пересекает выбранные тела.")
    return {**_base(content, oc), "schema": "step-section/1", "axis": axis, "offset_mm": offset_mm, "projection_axes": ["xyz"[i] for i in axes], "components": components, "bounds_mm": bounds, "point_count": budget[0], "warnings": warnings, "approximate": True, "deflection_mm": SECTION_DEFLECTION_MM, "connection_tolerance_mm": CONNECT_TOLERANCE_MM}


def section_dxf(result: dict) -> bytes:
    """Export server-generated sections only; preserves separate body layers."""
    import ezdxf
    if not isinstance(result, dict) or result.get("schema") != "step-section/1":
        raise StepGeometryError("Нужен результат построения STEP-сечения")
    components = result.get("components")
    if not isinstance(components, list) or len(components) > MAX_COMPONENTS:
        raise StepGeometryError("Некорректные компоненты сечения")
    doc = ezdxf.new("R2013", units=ezdxf.units.MM)
    doc.header["$MEASUREMENT"] = 1
    points_seen = 0
    for component in components:
        if not isinstance(component, dict):
            raise StepGeometryError("Некорректный компонент сечения")
        identifier = component.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or not 1 <= identifier <= MAX_COMPONENTS:
            raise StepGeometryError("Некорректный номер тела")
        layer = f"STEP_BODY_{identifier}"
        if layer in doc.layers:
            raise StepGeometryError("Тело повторено в сечении")
        doc.layers.new(layer, dxfattribs={"color": 1 + identifier % 6})
        paths = component.get("paths")
        if not isinstance(paths, list) or len(paths) > MAX_SECTION_EDGES:
            raise StepGeometryError("Некорректные пути сечения")
        for path in paths:
            if not isinstance(path, list) or len(path) < 2:
                raise StepGeometryError("Некорректный контур сечения")
            points_seen += len(path)
            if points_seen > MAX_POINTS or any(not isinstance(p, list) or len(p) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 1e9 for v in p) for p in path):
                raise StepGeometryError("Сечение содержит слишком много или некорректные точки", code="STEP_LIMIT")
            doc.modelspace().add_lwpolyline(path, dxfattribs={"layer": layer})
    doc.layers.new("STEP_DRAFT_NOTE", dxfattribs={"color": 8})
    # This note explicitly survives export into another CAD application.
    y = max((p[1] for c in components for path in c["paths"] for p in path), default=0.)
    doc.modelspace().add_text("DRAFT STEP SECTION / mm / APPROXIMATE POLYLINES / NOT ENGINEERING APPROVED", dxfattribs={"layer": "STEP_DRAFT_NOTE", "height": 1.5, "insert": (0, y + 5)})
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")
