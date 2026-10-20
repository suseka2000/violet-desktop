"""Renderer-specific preflight on top of the UI-independent catalog schema."""
from .scene import Sheet, width


def preflight_catalog(catalog):
    from ..engineering import validate_catalog
    result = validate_catalog(catalog)
    if not result["valid"]:
        return result
    for ni, node in enumerate(result["normalized"]["nodes"]):
        bx, by, bw, bh = node["drawing"]["view_box"]
        for pi, p in enumerate(node["drawing"]["primitives"]):
            if p["type"] != "text":
                continue
            path = f"/catalog/nodes/{ni}/drawing/primitives/{pi}"
            try:
                if any(ord(char) < 32 for char in p["text"]):
                    raise ValueError("Используйте отдельные текстовые примитивы вместо переносов строк и табуляций")
                Sheet("preflight", "preflight").text(0, 0, p["text"])
                text_width = width(p["text"], p["font_size"])
                x = p["x"] - text_width * {"start": 0, "middle": .5, "end": 1}[p.get("anchor", "start")]
                # Conservative font ascent/descent bounds avoid cropped labels.
                if x < bx or x + text_width > bx + bw or p["y"] - p["font_size"] < by or p["y"] + p["font_size"] * .3 > by + bh:
                    raise ValueError("Подпись узла выходит за view_box с учётом размера и привязки шрифта")
            except ValueError as error:
                result["issues"].append({"code": "CATALOG_TEXT_RENDER_INVALID", "severity": "error", "message": str(error),
                                         "path": path, "entity_ids": [node["id"]], "details": {}})
    if any(issue["severity"] == "error" for issue in result["issues"]):
        result["valid"] = False
        result["normalized"] = None
    return result
