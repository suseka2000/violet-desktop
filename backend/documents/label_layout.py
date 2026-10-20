"""Paper-mm label placement shared by all vector document formats."""
from __future__ import annotations

from .scene import width


def text_box(text, size, x, baseline, *, bold=False, anchor="middle", padding=.6):
    length = width(text, size, bold)
    left = x - (length/2 if anchor == "middle" else length if anchor == "end" else 0)
    return (left-padding, baseline-size-padding, length+2*padding, size+2*padding+.35)


def overlap(a, b):
    return max(0., min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0])) * max(0., min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))


def segment_hits(rect, start, end, padding=.25):
    """Liang-Barsky clipping, including vertical/oblique construction lines."""
    x, y, w, h = rect
    x -= padding; y -= padding; w += 2*padding; h += 2*padding
    if max(start[0],end[0]) < x or min(start[0],end[0]) > x+w or max(start[1],end[1]) < y or min(start[1],end[1]) > y+h:
        return False
    dx, dy = end[0]-start[0], end[1]-start[1]
    lo, hi = 0., 1.
    for p, q in ((-dx, start[0]-x), (dx, x+w-start[0]), (-dy, start[1]-y), (dy, y+h-start[1])):
        if abs(p) < 1e-12:
            if q < 0: return False
            continue
        value = q/p
        if p < 0: lo = max(lo, value)
        else: hi = min(hi, value)
        if lo > hi: return False
    return True


def obstacles(primitives):
    """Keep strokes separate from text; a room boundary is not its whole AABB."""
    boxes, segments = [], []
    for item in primitives:
        kind = item["type"]
        if kind == "text":
            boxes.append(text_box(item["value"], item["size"], item["x"], item["y"], bold=item["bold"], anchor=item["anchor"]))
        elif kind == "circle":
            r = item["r"]+.5
            boxes.append((item["x"]-r, item["y"]-r, 2*r, 2*r))
        elif kind == "line":
            segments.append(((item["x1"], item["y1"]), (item["x2"], item["y2"])))
        elif item.get("color") != "none" and kind in {"polygon", "region", "rect"}:
            if kind == "rect":
                x, y, w, h = (item[k] for k in ("x", "y", "w", "h"))
                rings = [[(x,y),(x+w,y),(x+w,y+h),(x,y+h)]]
            else:
                rings = item["rings"] if kind == "region" else [item["points"]]
            for ring in rings:
                segments.extend(zip(ring, ring[1:]))
                if item.get("closed", True) and len(ring) > 1:
                    segments.append((ring[-1], ring[0]))
    return boxes, segments


def collision(rect, boxes, segments):
    # A label may mask a small stroke when a tiny panel is very dense, but it
    # must never prefer covering another label or a fastener marker.
    return (sum(overlap(rect, box) for box in boxes), sum(segment_hits(rect, a, b) for a, b in segments))


def contains(bounds, rect):
    x, y, w, h = bounds
    return rect[0] >= x and rect[1] >= y and rect[0]+rect[2] <= x+w and rect[1]+rect[3] <= y+h


def draw_panel_labels(sheet, panels, transform, scale, *, color, fill="#ffffff"):
    """Number each original panel once, after the frame/fastener overlay.

    Prefer clear space next to a post rather than the exact geometric centre
    where a post often lies. The small vector mask also protects the label
    from underlying hatch/dash patterns in SVG, PDF and the editable DXF.
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for panel in panels:
        groups[panel.get("panel_id", panel.get("id", panel.get("index")))].append(panel)
    boxes, segments = obstacles(sheet.primitives)
    for sequence, pieces in enumerate(groups.values(), 1):
        label = str(pieces[0].get("index", sequence))
        candidates = []
        for piece in sorted(pieces, key=lambda item: -item["width_mm"]*item["height_mm"]):
            if piece["width_mm"]/scale < 8 or piece["height_mm"]/scale < 7:
                continue
            top_left = transform(piece["x_mm"], piece["y_mm"]+piece["height_mm"])
            bounds = (*top_left, piece["width_mm"]/scale, piece["height_mm"]/scale)
            for fx in (.5,.32,.68,.2,.8):
                for fy in (.5,.38,.62,.25,.75):
                    px, py = transform(piece["x_mm"]+piece["width_mm"]*fx, piece["y_mm"]+piece["height_mm"]*fy)
                    baseline = py+.9
                    rect = text_box(label, 2.5, px, baseline)
                    if contains(bounds, rect):
                        candidates.append((abs(fx-.5)+abs(fy-.5), px, baseline, rect))
        options = []
        for distance, px, baseline, rect in sorted(candidates,key=lambda item:item[0]):
            covered, strokes = collision(rect, boxes, segments)
            options.append((covered,strokes,distance,px,baseline,rect))
            if not covered and not strokes:
                break
        if not options:
            continue  # Narrow fragments retain their panel identity in the schedule.
        covered, strokes, _, px, baseline, rect = min(options, key=lambda item: item[:3])
        # Do not conceal a screw/anchor or another identifier on tiny fragments.
        if covered:
            continue
        sheet.rect(*rect, "none", fill, 0)
        sheet.text(px, baseline, label, 2.5, color, anchor="middle")
        boxes.append(rect)
        sheet.layout.setdefault("panel_labels", []).append({"panel_id": pieces[0].get("panel_id", pieces[0].get("id")),
            "label": label, "box": rect, "masked_strokes": strokes, "primitive_index": len(sheet.primitives)-1})
