"""Paper-space drawing primitives, shared by SVG and vector PDF (units: mm).

Text is placed with an explicit baseline. No HTML, executable SVG, or external
resources are accepted. Font metrics used by layout and PDF are identical.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from io import BytesIO
from pathlib import Path
from threading import RLock

from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

MM = 72 / 25.4
INK = "#202020"
BLUE = "#215d9c"
RED = "#b63b37"
GRAY = "#687b89"
LIGHT = "#edf2f5"
FONT_LOCK = RLock()


def fonts():
    with FONT_LOCK:
        if "RDRegular" not in pdfmetrics.getRegisteredFontNames():
            folder = Path(__file__).parent / "fonts"
            for name, file in (("RDRegular", "NotoSans-Regular.ttf"), ("RDBold", "NotoSans-Bold.ttf")):
                pdfmetrics.registerFont(TTFont(name, str(folder / file)))


def clean_text(value):
    return str(value).translate(str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00a0": " "}))


def width(text, size=3.1, bold=False):
    fonts()
    return pdfmetrics.stringWidth(clean_text(text), "RDBold" if bold else "RDRegular", size * MM) / MM


def wrap(value, maximum, size=3.1, bold=False):
    """Wrap even unbroken identifiers. Never truncate technical table content."""
    if len(str(value)) > 4000:
        raise ValueError("Текст документа слишком длинный (более 4000 символов в одном поле)")
    result = []
    for paragraph in clean_text(value).split("\n"):
        line = ""
        for word in paragraph.split():
            if width((line + " " + word).strip(), size, bold) <= maximum:
                line = (line + " " + word).strip()
                continue
            if line:
                result.append(line)
                line = ""
            for char in word:
                if line and width(line + char, size, bold) > maximum:
                    result.append(line)
                    line = ""
                line += char
        result.append(line)
    return result or [""]


@dataclass
class Sheet:
    title: str
    kind: str
    width: float = 420
    height: float = 297
    primitives: list[dict] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    layout: dict = field(default_factory=dict)

    def line(self, x1, y1, x2, y2, color=INK, weight=.25, dash=False):
        self.primitives.append(dict(type="line", x1=x1, y1=y1, x2=x2, y2=y2, color=color, weight=weight, dash=dash))

    def rect(self, x, y, w, h, color=INK, fill=None, weight=.25):
        self.primitives.append(dict(type="rect", x=x, y=y, w=w, h=h, color=color, fill=fill, weight=weight))

    def polygon(self, points, color=INK, fill=None, weight=.25, closed=True):
        self.primitives.append(dict(type="polygon", points=points, color=color, fill=fill, weight=weight, closed=closed))

    def region(self, rings, color=INK, fill=None, weight=.25):
        """A filled planar region with holes, shared by SVG/PDF even-odd paths."""
        self.primitives.append(dict(type="region", rings=rings, color=color, fill=fill, weight=weight))

    def circle(self, x, y, r, color=INK, fill=None, weight=.25):
        self.primitives.append(dict(type="circle", x=x, y=y, r=r, color=color, fill=fill, weight=weight))

    def text(self, x, y, value, size=3.1, color=INK, bold=False, anchor="start"):
        fonts()
        value = clean_text(value)
        font = pdfmetrics.getFont("RDBold" if bold else "RDRegular")
        missing = {character for character in value if ord(character) not in font.face.charToGlyph}
        if missing:
            codes = ", ".join(f"U+{ord(character):04X}" for character in sorted(missing))
            raise ValueError(f"В шрифте документа нет символов {codes}. Замените их в подписи или подключите подходящий шрифт.")
        self.primitives.append(dict(type="text", x=x, y=y, value=value, size=size, color=color, bold=bold, anchor=anchor))

    def paragraph(self, x, y, value, maximum, size=3.1, color=INK, bold=False, leading=None):
        lines = wrap(value, maximum, size, bold)
        for line in lines:
            self.text(x, y, line, size, color, bold)
            y += leading or size * 1.55
        return y

    def dimension(self, x1, x2, y, object_y, label, color=BLUE):
        if x2 - x1 < .01:
            return
        first = len(self.primitives)
        self.line(x1, object_y, x1, y + 1.2, color, .15)
        self.line(x2, object_y, x2, y + 1.2, color, .15)
        self.line(x1, y, x2, y, color, .18)
        for x in (x1, x2):
            self.line(x - .7, y + .7, x + .7, y - .7, color, .25)
        self.text((x1 + x2) / 2, y - 1.2, label, 2.8, color, anchor="middle")
        self.register_dimension(range(first, len(self.primitives)), (x1, object_y),
                                (x2, object_y), (x1, y), label)

    def register_dimension(self, indices, start, end, base, label, *, direction=(1., 0.), value_mm=None):
        """Keep dimension semantics without changing the shared PDF/SVG picture.

        Anchors/base/direction use the scene's paper-mm, y-down coordinates.
        The DXF exporter groups the exact indexed picture into one editable
        DIMENSION; the dimension measurement is scaled to model millimetres.
        ``indices`` can be non-contiguous (plan labels are drawn last).
        """
        dimension = {
            "start": tuple(start), "end": tuple(end),
            "base": tuple(base), "direction": tuple(direction),
            "label": str(label), "value_mm": value_mm,
        }
        # Packing copies/extends primitives from trial sheets. Store the group
        # on the primitives themselves so semantics survive that operation,
        # independent of source-sheet indices, dimensions or layout metadata.
        for index in indices:
            self.primitives[index]["_dimension"] = dimension

    def svg(self):
        body = []
        for p in self.primitives:
            typ = p["type"]
            color = p.get("color", INK)
            stroke = f'stroke="{color}" stroke-width="{p.get("weight", .25)}" fill="{p.get("fill") or "none"}"'
            if typ == "line":
                dash = ' stroke-dasharray="2 1"' if p["dash"] else ""
                body.append(f'<line x1="{p["x1"]}" y1="{p["y1"]}" x2="{p["x2"]}" y2="{p["y2"]}" {stroke}{dash}/>')
            elif typ == "rect":
                body.append(f'<rect x="{p["x"]}" y="{p["y"]}" width="{p["w"]}" height="{p["h"]}" {stroke}/>')
            elif typ == "circle":
                body.append(f'<circle cx="{p["x"]}" cy="{p["y"]}" r="{p["r"]}" {stroke}/>')
            elif typ == "polygon":
                tag = "polygon" if p["closed"] else "polyline"
                pts = " ".join(f"{x},{y}" for x, y in p["points"])
                body.append(f'<{tag} points="{pts}" {stroke}/>')
            elif typ == "region":
                path = " ".join("M " + " L ".join(f"{x},{y}" for x,y in ring) + " Z" for ring in p["rings"] if ring)
                body.append(f'<path d="{path}" {stroke} fill-rule="evenodd"/>')
            elif typ == "text":
                weight = 700 if p["bold"] else 400
                body.append(f'<text x="{p["x"]}" y="{p["y"]}" font-size="{p["size"]}" font-family="Noto Sans,Arial,sans-serif" font-weight="{weight}" text-anchor="{p["anchor"]}" fill="{color}">{escape(p["value"])}</text>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}mm" height="{self.height}mm" viewBox="0 0 {self.width} {self.height}" role="img"><title>{escape(self.title)}</title><rect width="100%" height="100%" fill="white"/>' + "".join(body) + "</svg>").encode()


def pdf_bytes(sheets: list[Sheet], title: str) -> bytes:
    fonts()
    stream = BytesIO()
    c = Canvas(stream, pagesize=(420 * MM, 297 * MM), invariant=1, pageCompression=1)
    c.setTitle(clean_text(title))
    c.setAuthor("Interior RD Generator")
    c.setSubject("Внутренняя отделка стен: планы, развёртки, узлы и ведомости.")
    for index, sheet in enumerate(sheets, 1):
        c.setPageSize((sheet.width * MM, sheet.height * MM))
        c.bookmarkPage(f"sheet-{index}")
        c.addOutlineEntry(sheet.title, f"sheet-{index}", level=0)
        yy = lambda y: (sheet.height - y) * MM
        for p in sheet.primitives:
            stroke = p.get("color", INK) != "none"
            c.setStrokeColor(HexColor(p.get("color", INK) if stroke else INK))
            c.setLineWidth(p.get("weight", .25) * MM)
            c.setDash([2 * MM, MM] if p.get("dash") else [])
            fill_color = p.get("fill") or p.get("color", INK)
            c.setFillColor(HexColor(fill_color if fill_color != "none" else INK))
            typ = p["type"]
            if typ == "line":
                if stroke:
                    c.line(p["x1"] * MM, yy(p["y1"]), p["x2"] * MM, yy(p["y2"]))
            elif typ == "rect":
                c.rect(p["x"] * MM, yy(p["y"] + p["h"]), p["w"] * MM, p["h"] * MM, stroke=int(stroke), fill=int(bool(p["fill"])))
            elif typ == "circle":
                c.circle(p["x"] * MM, yy(p["y"]), p["r"] * MM, stroke=int(stroke), fill=int(bool(p["fill"])))
            elif typ == "region":
                path = c.beginPath()
                for ring in p["rings"]:
                    if not ring:
                        continue
                    path.moveTo(ring[0][0]*MM, yy(ring[0][1]))
                    for x,y in ring[1:]:
                        path.lineTo(x*MM,yy(y))
                    path.close()
                c.drawPath(path,stroke=int(stroke),fill=int(bool(p["fill"])),fillMode=0)
            elif typ == "polygon" and p["points"]:
                path = c.beginPath()
                path.moveTo(p["points"][0][0] * MM, yy(p["points"][0][1]))
                for x, y in p["points"][1:]:
                    path.lineTo(x * MM, yy(y))
                if p["closed"]:
                    path.close()
                c.drawPath(path, stroke=int(stroke), fill=int(bool(p["fill"])))
            elif typ == "text":
                c.setFont("RDBold" if p["bold"] else "RDRegular", p["size"] * MM)
                draw = {"start": c.drawString, "middle": c.drawCentredString, "end": c.drawRightString}[p["anchor"]]
                draw(p["x"] * MM, yy(p["y"]), p["value"])
        c.showPage()
    c.save()
    return stream.getvalue()
