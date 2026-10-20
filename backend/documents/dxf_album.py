"""Editable DXF of the *same* paper scene used by the PDF and SVG album.

Each sheet is a native paper-space layout in millimetres, with no model windows,
images, external references or flattened text. Model contains an overview in
sheet blocks, not a fictitious single-scale 1:1 building model. Dimensions keep
their measured anchors and measurement factor, plus the exact scene picture
in a standard anonymous dimension block. AutoCAD can regenerate/edit them.
"""
from __future__ import annotations

from hashlib import sha256
from io import StringIO
import json
import math
import re

import ezdxf
from ezdxf.colors import rgb2int
from ezdxf.enums import TextEntityAlignment
from reportlab.pdfbase import pdfmetrics

from .scene import BLUE, GRAY, INK, RED, Sheet, fonts, width as text_width

FONT_FILES = ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf")
APP_ID = "VIOLET_RD"
LINEWEIGHTS = (0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80,
               90, 100, 106, 120, 140, 158, 200, 211)
NUMBER = re.compile(r"(?<![\w.])([-+]?\d+(?:[.,]\d+)?)(?![\w.])")


def _rgb(color):
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(char*2 for char in color)
    return tuple(int(color[index:index+2], 16) for index in (0, 2, 4))


def _layout_name(index, title):
    # Prefix makes truncated, duplicate and case-insensitive names unique.
    name = re.sub(r'[<>/\\":;?*|=\x00-\x1f]', "-", str(title)).strip()
    return f"{index:03d} {name}"[:250].rstrip()


def _layer(primitive, *, dimension=False, fill=False):
    if dimension:
        return "RD_DIMENSIONS"
    if fill:
        return "RD_FILLS"
    if primitive["type"] == "text":
        return "RD_TEXT_BOLD" if primitive.get("bold") else "RD_TEXT"
    return {BLUE: "RD_ANNOTATIONS", GRAY: "RD_SECONDARY", RED: "RD_WARNINGS"}.get(
        primitive.get("color", INK), "RD_GEOMETRY")


def _hatch_ring_flags(rings):
    """Classify every ring without comparing each disconnected loop to all others.

    STRtree first filters by envelope and runs the exact covered-by predicate
    only on spatial candidates. Query one point at a time, so densely nested
    contours do not allocate an all-pairs matrix. The first-vertex containment
    rule is intentionally identical to the previous hatch representation.
    """
    from shapely.geometry import Point, Polygon
    from shapely.strtree import STRtree

    polygons = [Polygon(ring) for ring in rings]
    tree = STRtree(polygons)
    for index, ring in enumerate(rings):
        containing = tree.query(Point(ring[0]), predicate="covered_by")
        depth = int((containing != index).sum())
        yield 1 if depth == 0 else 16 if depth == 1 else 0


class _Writer:
    def __init__(self, document, sheet, layout):
        self.doc, self.sheet, self.layout = document, sheet, layout

    def point(self, point):
        return (float(point[0]), self.sheet.height-float(point[1]))

    def attributes(self, primitive, *, dimension=False, fill=False):
        color = primitive.get("fill") if fill else primitive.get("color", INK)
        weight = max(0., float(primitive.get("weight", .25)))*100
        return {"layer": _layer(primitive, dimension=dimension, fill=fill),
                "true_color": rgb2int(_rgb(color)),
                "lineweight": 0 if fill else min(LINEWEIGHTS, key=lambda item: abs(item-weight)),
                "linetype": "RD_DASHED" if primitive.get("dash") else "CONTINUOUS"}

    def fill(self, primitive, rings=None, circle=None, *, dimension=False):
        if not primitive.get("fill") or primitive["fill"] == "none":
            return
        hatch = self.layout.add_hatch(dxfattribs=self.attributes(primitive, dimension=dimension, fill=True))
        hatch.set_solid_fill(color=7, style=0, rgb=_rgb(primitive["fill"]))
        if circle:
            center, radius = circle
            hatch.paths.add_edge_path(flags=1).add_arc(center, radius, 0, 360)
        else:
            # NESTED parity includes disconnected exteriors and arbitrary holes.
            # Flags describe containment, but do not replace the actual rings.
            for ring, flags in zip(rings, _hatch_ring_flags(rings)):
                hatch.paths.add_polyline_path(ring, is_closed=True, flags=flags)

    def primitive(self, primitive, *, dimension=False):
        typ = primitive["type"]
        stroke = primitive.get("color", INK) != "none"
        attrs = self.attributes(primitive, dimension=dimension) if stroke else None
        if typ == "text":
            if not primitive["value"] or not stroke:
                return
            bold = primitive.get("bold", False)
            cap = pdfmetrics.getFont("RDBold" if bold else "RDRegular").face.capHeight/1000
            value, anchor = primitive["value"], primitive.get("anchor", "start")
            alignment = {"start": TextEntityAlignment.LEFT, "middle": TextEntityAlignment.CENTER,
                         "end": TextEntityAlignment.RIGHT}[anchor]
            x = float(primitive["x"])
            # CAD TEXT interprets %%d/%%u/%%nnn, caret notation and \U+xxxx,
            # whereas the PDF intentionally shows user labels literally.
            # Escaping with %%% or Unicode codes is interpreted differently by
            # CAD engines (and ezdxf). Isolate only hazardous sequences into
            # precisely positioned editable TEXT runs; no interpreter can
            # combine a control code across separate native entities.
            literal_runs = "%%" in value or "\\" in value or "^" in value
            runs = [value]
            if literal_runs:
                runs = [run for run in re.split(r"([%\\^])", value) if run]
                x -= text_width(value, primitive["size"], bold)*{"start": 0, "middle": .5, "end": 1}[anchor]
                alignment = TextEntityAlignment.LEFT
                run_group = sha256(json.dumps({key: primitive[key] for key in
                    ("x", "y", "value", "size", "bold", "anchor") if key in primitive},
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            for index, run in enumerate(runs):
                # A terminal caret is stripped by DXF TEXT validators. "^ "
                # is the standard literal-caret notation; one trailing blank
                # keeps the decoded caret non-terminal and is invisible. The
                # next run is positioned after the actual caret width only.
                encoded_run = "^  " if literal_runs and run == "^" else run
                text = self.layout.add_text(encoded_run, dxfattribs={
                    **attrs, "style": "RD_NOTO_BOLD" if bold else "RD_NOTO",
                    # DXF TEXT height is cap-height, whereas PDF/SVG size is em.
                    "height": float(primitive["size"])*cap,
                })
                text.set_placement(self.point((x, primitive["y"])), align=alignment)
                if literal_runs:
                    text.set_xdata(APP_ID, [(1000, "literal-text-run"), (1000, run_group),
                                            (1070, index), (1070, len(runs)),
                                            (1000, "caret-padding" if run == "^" else "")])
                    x += text_width(run, primitive["size"], bold)
        elif typ == "line":
            if stroke:
                self.layout.add_line(self.point((primitive["x1"], primitive["y1"])),
                                     self.point((primitive["x2"], primitive["y2"])), dxfattribs=attrs)
        elif typ == "circle":
            radius = float(primitive["r"])
            if radius <= 0:
                return
            center = self.point((primitive["x"], primitive["y"]))
            self.fill(primitive, circle=(center, radius), dimension=dimension)
            if stroke:
                self.layout.add_circle(center, radius, dxfattribs=attrs)
        elif typ in {"rect", "polygon", "region"}:
            closed = primitive.get("closed", True)
            if typ == "rect":
                x, y, w, h = (primitive[key] for key in ("x", "y", "w", "h"))
                rings = [[(x, y), (x+w, y), (x+w, y+h), (x, y+h)]]
            else:
                rings = primitive["rings"] if typ == "region" else [primitive["points"]]
            rings = [[self.point(point) for point in ring] for ring in rings if len(ring) >= 2]
            # PDF closes a filled polygon even if its stroke is an open path.
            fill_rings = [ring for ring in rings if len(ring) >= 3]
            if fill_rings:
                self.fill(primitive, fill_rings, dimension=dimension)
            if stroke:
                for ring in rings:
                    self.layout.add_lwpolyline(ring, close=closed, dxfattribs=attrs)
        else:
            raise ValueError(f"Unsupported album DXF primitive: {typ}")

    def dimension(self, item):
        picture = [self.sheet.primitives[index] for index in item["indices"]]
        text_primitive = next((p for p in reversed(picture) if p["type"] == "text"), None)
        if text_primitive is None:
            raise ValueError("DXF dimension has no scene label")
        label = item["label"]
        matches = list(NUMBER.finditer(label))
        value = item.get("value_mm")
        if value is None and len(matches) == 1:
            value = abs(float(matches[0].group().replace(",", ".")))
        dx, dy = item["direction"]
        norm = math.hypot(dx, dy)
        start, end = item["start"], item["end"]
        paper_measurement = abs(((end[0]-start[0])*dx+(end[1]-start[1])*dy)/norm)
        if paper_measurement <= 1e-10:
            raise ValueError("DXF dimension has zero measured length")
        factor = float(value)/paper_measurement if value is not None else 1.
        dynamic_text = (label[:matches[0].start()]+"<>"+label[matches[0].end():]
                        if len(matches) == 1 else label)
        color = text_primitive.get("color", BLUE)
        cap = pdfmetrics.getFont("RDRegular").face.capHeight/1000
        override = {"dimlfac": factor, "dimtxt": text_primitive["size"]*cap,
                    "dimdec": 3, "dimzin": 8, "dimtih": 1, "dimtoh": 1,
                    "dimtad": 1, "dimtsz": .7, "dimexo": 0., "dimexe": 1.2,
                    "dimgap": 1.2, "dimtxsty": "RD_NOTO"}
        dimension = self.layout.add_linear_dim(
            base=self.point(item["base"]), p1=self.point(start), p2=self.point(end),
            text=dynamic_text, angle=math.degrees(math.atan2(-dy, dx)),
            dimstyle="RD_DIM", override=override,
            dxfattribs={"layer": "RD_DIMENSIONS", "true_color": rgb2int(_rgb(color)),
                        "lineweight": 18})
        dimension.commit()
        entity = dimension.dimension
        entity.dxf.text_midpoint = self.point((text_primitive["x"], text_primitive["y"]))
        entity.dxf.dimtype |= 128  # preserve the deliberately positioned text
        entity.dxf.actual_measurement = paper_measurement
        block = self.doc.blocks.new_anonymous_block("D")
        writer = _Writer(self.doc, self.sheet, block)
        for primitive in picture:
            writer.primitive(primitive, dimension=True)
        entity.dxf.geometry = block.name
        entity.set_xdata(APP_ID, [(1000, "paper-mm; DIMLFAC converts to model-mm"),
                                 (1040, factor), (1040, paper_measurement*factor)])

    def draw(self):
        by_last, claimed = {}, set()
        groups = {}
        for index, primitive in enumerate(self.sheet.primitives):
            item = primitive.get("_dimension")
            if item is not None:
                # Content keys also survive JSON round trips/deep copies.
                key = json.dumps(item, sort_keys=True, separators=(",", ":"))
                group = groups.setdefault(key, {**item, "indices": []})
                group["indices"].append(index)
        for item in groups.values():
            indices = item["indices"]
            claimed.update(indices)
            by_last[max(indices)] = item
        for index, primitive in enumerate(self.sheet.primitives):
            if index not in claimed:
                self.primitive(primitive)
            if index in by_last:
                self.dimension(by_last[index])


def _stable_bytes(document, digest):
    """Normalize generated timestamps/UUIDs locally, never ezdxf global options."""
    # ezdxf discovers optional CLASS entries through a set, so their insertion
    # order otherwise varies with PYTHONHASHSEED between server processes.
    # The map belongs to this new document only; no global state is changed.
    document.commit_pending_changes()
    document.classes.add_required_classes(document.dxfversion)
    for key in sorted(document.classes.classes):
        document.classes.classes.move_to_end(key)
    stream = StringIO()
    document.write(stream)
    # DXF pairs are separated by literal LF, not Unicode's wider concept of
    # line boundaries. A valid name/TEXT can contain U+2028, U+2029 or U+0085;
    # str.splitlines() would turn its value into extra, invalid DXF tags.
    lines = stream.getvalue().rstrip("\n").split("\n")
    guid = "{"+"-".join((digest[:8], digest[8:12], digest[12:16], digest[16:20], digest[20:32]))+"}"
    for index in range(0, len(lines)-1, 2):
        if lines[index].strip() == "9" and index+3 < len(lines):
            variable = lines[index+1].strip()
            if variable in {"$TDCREATE", "$TDUCREATE", "$TDUPDATE", "$TDUUPDATE"}:
                lines[index+3] = "2451544.5"
            elif variable in {"$VERSIONGUID", "$FINGERPRINTGUID"}:
                lines[index+3] = guid
        if lines[index].strip() == "1" and re.fullmatch(re.escape(ezdxf.__version__)+r" @ \d{4}-\d{2}-\d{2}T[^\n]+", lines[index+1]):
            lines[index+1] = ezdxf.__version__+" / Interior RD reproducible album"
    return ("\n".join(lines)+"\n").encode("utf-8")


def album_dxf_bytes(sheets: list[Sheet], title: str, *, project_id: str = "", revision: int | None = None) -> bytes:
    """Export complete album, with editable per-sheet layouts and Model overview.

    Geometry is in paper mm; plans and details intentionally retain their
    different PDF scales. Numerical dimensions carry real model-mm values.
    Font files listed in ``FONT_FILES`` should accompany/install with the DXF.
    """
    if not sheets:
        raise ValueError("DXF album must have at least one sheet")
    fonts()
    digest = sha256(json.dumps({"title": title, "project_id": project_id, "revision": revision,
        "sheets": [vars(sheet) for sheet in sheets]}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest().upper()
    doc = ezdxf.new("R2013", units=ezdxf.units.MM)
    doc.appids.new(APP_ID)
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LWDISPLAY"] = True
    doc.header["$DIMASSOC"] = 1  # native dimensions, not nonexistent geometric associations
    doc.header["$INSBASE"] = (0., 0., 0.)
    doc.linetypes.new("RD_DASHED", dxfattribs={"description": "2 mm / 1 mm", "pattern": [3., 2., -1.]})
    for name, color in (("RD_GEOMETRY", INK), ("RD_DIMENSIONS", BLUE), ("RD_TEXT", INK),
                        ("RD_TEXT_BOLD", INK), ("RD_FILLS", "#ffffff"), ("RD_ANNOTATIONS", BLUE),
                        ("RD_SECONDARY", GRAY), ("RD_WARNINGS", RED), ("RD_SHEETS", INK)):
        doc.layers.new(name, dxfattribs={"color": 7, "true_color": rgb2int(_rgb(color))})
    for name, filename, bold in (("RD_NOTO", FONT_FILES[0], False), ("RD_NOTO_BOLD", FONT_FILES[1], True)):
        style = doc.styles.new(name, dxfattribs={"font": filename})
        style.set_extended_font_data("Noto Sans", bold=bold, italic=False)
    doc.dimstyles.new("RD_DIM", dxfattribs={"dimtxsty": "RD_NOTO", "dimtxt": 2., "dimlfac": 1.,
        "dimdec": 3, "dimzin": 8, "dimtsz": .7, "dimscale": 1., "dimclrd": 0, "dimclre": 0, "dimclrt": 0})
    metadata = doc.ezdxf_metadata()
    metadata["VIOLET_TITLE"] = str(title)[:255]
    metadata["VIOLET_PROJECT"] = str(project_id)[:255]
    metadata["VIOLET_REVISION"] = "" if revision is None else str(revision)
    metadata["VIOLET_UNITS"] = "Paper millimetres; dimensions use DIMLFAC for model millimetres"
    model = doc.modelspace()
    cell_width = max(sheet.width for sheet in sheets)+25
    cell_height = max(sheet.height for sheet in sheets)+25
    columns = min(3, len(sheets))
    first_layout = None
    for index, sheet in enumerate(sheets, 1):
        name = _layout_name(index, sheet.title)
        if first_layout is None:
            doc.layouts.rename("Layout1", name)
            first_layout = name
            layout = doc.layouts.get(name)
        else:
            layout = doc.layouts.new(name)
        layout.page_setup(size=(sheet.width, sheet.height), margins=(0, 0, 0, 0),
                          units="mm", scale=(1, 1), name=f"RD {sheet.width} x {sheet.height} mm")
        # Keep the main viewport (ID=1): it defines the initial paper view in
        # AutoCAD, not a model window. Actual scene entities remain direct and
        # no floating/model viewport is needed to display any of the drawings.
        layout.dxf_layout.set_xdata(APP_ID, [(1000, sheet.kind), (1070, index),
                                            (1000, str(sheet.title)[:250])])
        _Writer(doc, sheet, layout).draw()
        overview = doc.blocks.new(f"RD_SHEET_{index:03d}")
        writer = _Writer(doc, sheet, overview)
        writer.primitive({"type": "rect", "x": 0, "y": 0, "w": sheet.width,
                          "h": sheet.height, "color": GRAY, "fill": "#ffffff", "weight": .13})
        writer.draw()
        model.add_blockref(overview.name, (((index-1) % columns)*cell_width,
                                          -((index-1)//columns)*cell_height), dxfattribs={"layer": "RD_SHEETS"})
    doc.layouts.set_active_layout(first_layout)
    doc.header["$TILEMODE"] = 0
    rows = math.ceil(len(sheets)/columns)
    doc.set_modelspace_vport(height=rows*cell_height+25,
                            center=(columns*cell_width/2, (cell_height-(rows-1)*cell_height)/2))
    return _stable_bytes(doc, digest)
