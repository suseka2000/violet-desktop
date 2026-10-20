"""Bounded, read-only inspection of reference-document ASCII DXF files.

Unlike the room importer, this module never interprets geometry or creates model
objects. It streams groups, inventories source entities and extracts literal
text. Successful parsing does not prove DWG conversion fidelity or engineering
approval. Callers must authorize the local file path; this is not an HTTP API.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re


class ReferenceDrawingError(ValueError):
    """The reference is malformed, unsupported, or exceeds inspection limits."""


@dataclass(frozen=True)
class ReferenceInspectionLimits:
    max_input_bytes: int = 256 * 1024**2
    max_line_bytes: int = 65536
    max_output_bytes: int = 4 * 1024**2
    max_text_records: int = 5000
    max_text_bytes: int = 1536 * 1024
    max_text_chars_per_record: int = 8192
    max_names: int = 4096


TOPIC_PATTERNS = {
    "panel_layout": ("Раскладка панелей", r"расклад\w*\s+панел"),
    "wall_elevations": ("Развёртки стен", r"развертк|развёртк"),
    "junction_details": ("Узлы и примыкания", r"\bуз(?:ел|лы|ла|лов)\b|примыкани"),
    "specification": ("Спецификации и ведомости", r"спецификац|ведомост"),
    "general_notes": ("Общие данные и указания", r"общие\s+(?:данные|указания)|содержание"),
    "openings": ("Проёмы, двери и окна", r"про[её]м|двер|окн|фрамуг"),
    "lighting": ("Подсветка", r"подсветк"),
}
_TOPICS = {key: re.compile(pattern, re.I) for key, (_, pattern) in TOPIC_PATTERNS.items()}
_TEXT_KINDS = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION"}


def strip_dxf_text_formatting(value: str) -> str:
    """Remove known DXF presentation controls, not arbitrary text or markup.

    Returned content remains untrusted plain text: UI consumers must use text
    rendering, not innerHTML. It is not a full AutoCAD text-layout engine.
    """
    value = re.sub(r"\\U\+([0-9a-fA-F]{4})",
                   lambda m: "\ufffd" if 0xD800 <= int(m[1], 16) <= 0xDFFF else chr(int(m[1], 16)), value)
    value = re.sub(r"\\S([^;]*);", lambda m: re.sub(r"[\^#]", "/", m[1]), value)
    value = re.sub(r"\\[ACcFfHhQqTtWw][^;\r\n]*;", "", value)
    value = re.sub(r"\\p[^;\r\n]*;", "", value)
    value = re.sub(r"\\[LlOoKk]", "", value)
    value = value.replace("\\P", "\n").replace("\\N", "\n").replace("\\~", " ")
    value = re.sub(r"\\([{}\\])|[{}]", lambda m: m[1] or "", value)
    value = re.sub(r"%%[dD]", "°", value)
    value = re.sub(r"%%[pP]", "±", value)
    value = re.sub(r"%%[cC]", "Ø", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return value.strip()


def inspect_reference_dxf(path: str | Path, *, limits: ReferenceInspectionLimits | None = None) -> dict:
    """Inspect a local ASCII DXF without loading or following its geometry/XREFs."""
    limits = limits or ReferenceInspectionLimits()
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in vars(limits).values()):
        raise ReferenceDrawingError("Inspection limits must be positive integers")
    if limits.max_input_bytes > 256 * 1024**2 or limits.max_output_bytes > 4 * 1024**2:
        raise ReferenceDrawingError("Inspection cannot exceed 256 MiB input or 4 MiB output")
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ReferenceDrawingError("Expected a regular non-symlink DXF file")
    if path.stat().st_size > limits.max_input_bytes:
        raise ReferenceDrawingError("Reference DXF exceeds the input size limit")

    sections = Counter()
    types = Counter()
    layers = Counter()
    definitions: set[str] = set()
    inserts: set[str] = set()
    xrefs = []
    layouts = []
    texts = []
    topics = Counter()
    warnings: set[str] = set()
    headers = {}
    digest = hashlib.sha256()
    section = ""
    current_kind = ""
    fields = {}
    text_chunks = {1: [], 3: []}
    entity_chars = 0
    current_text_truncated = False
    current_block = None
    active_header = None
    total_bytes = 0
    tag_count = 0
    text_count = 0
    retained_text_bytes = 0
    complete_eof = False
    has_draft_text = False
    encoding = "utf-8"

    def bounded_name(value):
        if len(value) > 512:
            warnings.add("name_truncated")
        return value[:512]

    def count_name(counter, name):
        name = bounded_name(name)
        if name in counter or len(counter) < limits.max_names:
            counter[name] += 1
        else:
            counter["<additional names omitted>"] += 1
            warnings.add("name_inventory_truncated")

    def add_name(names, name):
        name = bounded_name(name)
        if len(names) < limits.max_names or name in names:
            names.add(name)
        else:
            warnings.add("block_reference_inventory_truncated")

    def flush():
        nonlocal section, current_block, complete_eof, text_count, retained_text_bytes, has_draft_text
        kind = current_kind
        if not kind:
            return
        if kind == "SECTION":
            if section:
                raise ReferenceDrawingError("Nested DXF sections are not supported")
            section = fields.get(2, "")
            if not section:
                raise ReferenceDrawingError("DXF section name is missing")
            count_name(sections, section)
            return
        if kind == "ENDSEC":
            section = ""
            current_block = None
            return
        if kind == "EOF":
            if section:
                raise ReferenceDrawingError("EOF inside a DXF section")
            complete_eof = True
            return
        if not section:
            raise ReferenceDrawingError("DXF entity outside a section")
        count_name(types, f"{section}:{kind}")
        if section in {"BLOCKS", "ENTITIES"}:
            count_name(layers, fields.get(8, "0"))
        if section == "BLOCKS" and kind == "BLOCK":
            current_block = fields.get(2)
            if current_block:
                add_name(definitions, current_block)
            try:
                flags = int(fields.get(70, "0"))
            except ValueError:
                flags = 0
                warnings.add("invalid_block_flags")
            if flags & 12:
                if len(xrefs) < limits.max_names:
                    xrefs.append({"name": current_block, "reference_path": fields.get(1, ""),
                                  "overlay": bool(flags & 8), "status": "not_loaded"})
                else:
                    warnings.add("xref_inventory_truncated")
        if kind == "INSERT" and section in {"BLOCKS", "ENTITIES"} and fields.get(2):
            add_name(inserts, fields[2])
        if kind == "LAYOUT" and section == "OBJECTS":
            if len(layouts) < min(limits.max_names, 256):
                layouts.append({"name": fields.get(1, ""), "handle": fields.get(5),
                                "paper_width_raw": fields.get(44), "paper_height_raw": fields.get(45)})
            else:
                warnings.add("layout_inventory_truncated")
        if kind in _TEXT_KINDS and section in {"BLOCKS", "ENTITIES"}:
            raw = "".join(text_chunks[3] + text_chunks[1])
            plain = strip_dxf_text_formatting(raw)
            if plain and plain != "<>":
                text_count += 1
                for key, pattern in _TOPICS.items():
                    if pattern.search(plain):
                        topics[key] += 1
                if re.search(r"в\s+разработке|предварительн|не\s+для\s+производства", plain, re.I):
                    has_draft_text = True
                size = len(plain.encode("utf-8", errors="replace"))
                if len(texts) < limits.max_text_records and retained_text_bytes + size <= limits.max_text_bytes:
                    texts.append({"type": kind, "handle": fields.get(5), "section": section,
                                  "block": current_block, "layer": fields.get(8, "0"),
                                  "layout": fields.get(410), "text": plain,
                                  "text_truncated": current_text_truncated})
                    retained_text_bytes += size
                else:
                    warnings.add("text_output_truncated")
                if current_text_truncated:
                    warnings.add("long_text_truncated")
        if kind == "ENDBLK":
            current_block = None

    with path.open("rb") as stream:
        def read_line():
            nonlocal total_bytes
            line = stream.readline(limits.max_line_bytes + 1)
            if len(line) > limits.max_line_bytes:
                raise ReferenceDrawingError("DXF line exceeds inspection limit")
            total_bytes += len(line)
            if total_bytes > limits.max_input_bytes:
                raise ReferenceDrawingError("Reference DXF grew beyond the input size limit")
            digest.update(line)
            return line

        while True:
            raw_code = read_line()
            if not raw_code:
                break
            if tag_count == 0 and raw_code.startswith(b"AutoCAD Binary DXF"):
                raise ReferenceDrawingError("Reference inspection accepts ASCII DXF, not binary DXF")
            raw_value = read_line()
            if not raw_value:
                raise ReferenceDrawingError("Unpaired DXF group code")
            try:
                code = int(raw_code.strip())
            except ValueError as exc:
                raise ReferenceDrawingError("Invalid DXF group code") from exc
            if not 0 <= code <= 1071:
                raise ReferenceDrawingError("DXF group code out of range")
            try:
                value = raw_value.rstrip(b"\r\n").decode(encoding)
            except UnicodeDecodeError:
                warnings.add("undecodable_text_replaced")
                value = raw_value.rstrip(b"\r\n").decode(encoding, errors="replace")
            tag_count += 1
            if complete_eof:
                raise ReferenceDrawingError("Unexpected DXF data after EOF")
            if code == 0:
                flush()
                if complete_eof:
                    raise ReferenceDrawingError("Unexpected DXF data after EOF")
                current_kind = bounded_name(value.strip())
                fields = {}
                text_chunks = {1: [], 3: []}
                entity_chars = 0
                current_text_truncated = False
                active_header = None
                continue
            if current_kind == "SECTION" and fields.get(2) == "HEADER":
                if code == 9:
                    active_header = value.strip()
                elif active_header in {"$ACADVER", "$INSUNITS", "$MEASUREMENT", "$DWGCODEPAGE"}:
                    headers[active_header] = value.strip()
                    active_header = None
                    if headers.get("$ACADVER", "AC1021") < "AC1021":
                        match = re.fullmatch(r"ANSI_(\d+)", headers.get("$DWGCODEPAGE", ""))
                        if match and match[1] in {"1250", "1251", "1252", "1253", "1254", "1255", "1256", "1257", "1258"}:
                            encoding = "cp" + match[1]
            if code in {1, 2, 5, 8, 44, 45, 70, 410}:
                # Last group 1 is the actual LAYOUT name after PlotSettings.
                if not ((current_kind == "SECTION" and code == 2 and 2 in fields)
                        or (current_kind in _TEXT_KINDS and code == 1)):
                    fields[code] = bounded_name(value)
            if current_kind in _TEXT_KINDS and code in {1, 3}:
                remaining = max(0, limits.max_text_chars_per_record - entity_chars)
                chunk = value[:remaining]
                if chunk:
                    text_chunks[code].append(chunk)
                entity_chars += len(chunk)
                current_text_truncated |= len(value) > remaining
        flush()

    if not complete_eof or "HEADER" not in sections or not headers.get("$ACADVER", "").startswith("AC"):
        raise ReferenceDrawingError("Incomplete DXF or missing version header")
    definition_keys = {item.casefold() for item in definitions}
    result = {
        "schema_version": 1, "kind": "reference_document", "filename": path.name,
        "source_sha256": digest.hexdigest(), "size_bytes": total_bytes,
        "geometry_interpreted": False, "engineering_approved": False,
        "complete_eof": complete_eof, "tag_count": tag_count,
        "headers": headers, "section_counts": dict(sections),
        "entity_type_counts": dict(types.most_common()), "layer_entity_counts": dict(layers.most_common()),
        "block_definition_count": types.get("BLOCKS:BLOCK", 0),
        "block_names": sorted(definitions), "layouts": layouts,
        "external_references": xrefs,
        "missing_block_definitions": sorted(name for name in inserts
                                            if name.casefold() not in definition_keys),
        "text_record_count": text_count, "retained_text_record_count": len(texts), "texts": texts,
        "topics": [{"id": key, "label": TOPIC_PATTERNS[key][0], "text_match_count": count}
                   for key, count in topics.most_common()],
        "draft_content_detected": has_draft_text,
        "warnings": sorted(warnings),
        "limitations": ["Text and inventories are extracted evidence, not interpreted drawing geometry.",
                        "External references are inventoried but never opened.",
                        "Printed text is not proof of a sheet layout, scale, or engineering approval."],
    }
    def output_size():
        # Match backend.__main__.emit, including print's terminating newline.
        return len((json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))
    while output_size() > limits.max_output_bytes:
        if "output_size_truncated" not in result["warnings"]:
            result["warnings"].append("output_size_truncated")
        choices = [key for key in ("texts", "block_names", "external_references", "missing_block_definitions", "layouts") if result[key]]
        if choices:
            key = max(choices, key=lambda k: len(json.dumps(result[k], ensure_ascii=False).encode("utf-8")))
            result[key] = result[key][:len(result[key]) // 2]
        elif result["layer_entity_counts"]:
            entries = list(result["layer_entity_counts"].items())
            result["layer_entity_counts"] = dict(entries[:len(entries) // 2])
        else:
            raise ReferenceDrawingError("Output budget is too small for inventory metadata")
        result["retained_text_record_count"] = len(result["texts"])
    return result
