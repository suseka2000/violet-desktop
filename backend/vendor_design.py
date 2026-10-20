"""Conservative, snapshot-only application of supplier references to cladding.

A source hash proves which document was used, not that an assembly is suitable.
This module never changes geometry or upgrades a node/assembly to approved.
"""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from .domain.models import Model, Project, Identifier
from .vendor_library import Digest, ReferenceSnapshot


class ZoneBinding(Model):
    zone_id: Identifier
    system: Literal["open_mount", "concealed_mount"]
    assembly_rule_id: Annotated[str, Field(min_length=1, max_length=200)]
    note: Annotated[str, Field(max_length=2000)] = ""
    zone_signature: Digest | None = None


class VendorDesign(Model):
    schema_version: Literal["vendor-design/1.0"] = "vendor-design/1.0"
    snapshot: ReferenceSnapshot
    zone_bindings: list[ZoneBinding] = Field(default_factory=list, max_length=2000)


def _issue(code, message, path="/properties/vendor_design", zone_id=None, **details):
    return {"code": code, "severity": "error", "message": message, "path": path,
            "entity_ids": [zone_id] if zone_id else [], "details": details}


def _index(project):
    return {zone.id: (zone, floor) for building in project.buildings
            for floor in building.floors for zone in floor.cladding_zones}


def is_assembly_rule(rule):
    """Catalog profile sections and base-frame figures are not wall selections."""
    return (rule.kind == "detail" and bool(rule.assembly_id)
            and rule.topic in {"mounting_assembly", "clip_installation"})


def validate_vendor_design(project: Project):
    raw = project.properties.get("vendor_design")
    if raw is None:
        return {"valid": True, "issues": [], "normalized": None}
    try:
        design = VendorDesign.model_validate(raw)
    except (ValidationError, ValueError, TypeError) as exc:
        return {"valid": False, "normalized": None, "issues": [_issue(
            "VENDOR_DESIGN_INVALID", "Привязки методичек или контрольная сумма снимка некорректны.",
            reason=str(exc)[:2000])]}
    zones, rules, seen, issues = _index(project), {r.id: r for r in design.snapshot.rules}, set(), []
    for n, binding in enumerate(design.zone_bindings):
        path = f"/properties/vendor_design/zone_bindings/{n}"
        if binding.zone_id in seen:
            issues.append(_issue("VENDOR_BINDING_DUPLICATE", "Зона имеет несколько выбранных сборок; оставьте один явно выбранный вариант.", path, binding.zone_id))
        seen.add(binding.zone_id)
        if binding.zone_id not in zones:
            issues.append(_issue("VENDOR_BINDING_ZONE_MISSING", "Привязка методички относится к удалённой или неизвестной зоне.", path, binding.zone_id))
        rule = rules.get(binding.assembly_rule_id)
        if rule is None or not is_assembly_rule(rule):
            issues.append(_issue("VENDOR_BINDING_ASSEMBLY_INVALID", "Выбрана не сборка монтажа из прикреплённого снимка методичек.", path, binding.zone_id))
        elif rule.system != binding.system:
            issues.append(_issue("VENDOR_BINDING_SYSTEM_MISMATCH", "Тип монтажа привязки не совпадает с выбранной сборкой методички.", path, binding.zone_id))
    return {"valid": not issues, "issues": issues, "normalized": design.model_dump(mode="json")}


def _without_labels(entity):
    return entity.model_dump(mode="json", exclude={"name", "properties"})


def _context_hash(value):
    # Number accepts integer and float JSON input. A serialize/parse roundtrip
    # must not invalidate the same 3 mm geometry merely because 3 became 3.0.
    def canonical(item):
        if isinstance(item, bool):
            return item
        if isinstance(item, (int, float)):
            return 0.0 if item == 0 else float(item)
        if isinstance(item, dict):
            return {key: canonical(value) for key, value in item.items()}
        if isinstance(item, list):
            return [canonical(value) for value in item]
        return item
    return sha256(json.dumps(canonical(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def zone_signatures(project: Project):
    """Pin engineering context, not a transient project revision or UI label.

    All geometry on the floor is included conservatively, because a neighbouring
    wall/opening can change a corner requirement. No provenance approval is
    implied by recording this digest.
    """
    systems = {p.id: _without_labels(p) for p in project.panel_systems}
    result = []
    for building in project.buildings:
        for floor in building.floors:
            context = {"floor_id": floor.id, "elevation_mm": floor.elevation_mm,
                       "origin": floor.origin.model_dump(mode="json"),
                       "walls": sorted((_without_labels(w) for w in floor.walls), key=lambda x: x["id"]),
                       "openings": sorted((_without_labels(o) for o in floor.openings), key=lambda x: x["id"]),
                       "spaces": sorted((_without_labels(s) for s in floor.spaces), key=lambda x: x["id"]),
                       "levels": sorted((_without_labels(l) for l in floor.levels), key=lambda x: x["id"]),
                       "obstacles": sorted((_without_labels(o) for o in floor.obstacles), key=lambda x: x["id"])}
            context_digest = _context_hash(context)
            for zone in floor.cladding_zones:
                data = {"zone": _without_labels(zone), "system": systems.get(zone.panel_system_id), "context_digest": context_digest}
                digest = _context_hash(data)
                result.append({"zone_id": zone.id, "zone_name": zone.name, "floor_id": floor.id,
                               "panel_system_id": zone.panel_system_id, "zone_signature": digest})
    return sorted(result, key=lambda z: z["zone_id"])


def _source(rule, documents):
    doc = documents[rule.source.document_id]
    return {"document_id": doc.id, "filename": doc.filename, "page": rule.source.page,
            "sha256": doc.sha256, "source_status": rule.source_status, "preview_url": rule.preview_url}


def build_vendor_report(project: Project, node_requirements):
    """Pure report from the saved snapshot; no live library/network/file reads."""
    signatures = zone_signatures(project)
    result = {"schema_version": "vendor-review/1.0", "attached": False, "snapshot_id": None,
              "approval_status": "unreviewed", "implicit_approval": False,
              "available_zones": signatures, "bindings": [], "issues": [], "node_references": [],
              "limitations": ["Выбор страницы методички не утверждает узел или геометрию.",
                              "Сопоставляется только явно заданный межпанельный шов выбранной сборки; остальные требования проверяются проектировщиком.",
                              "Названия профилей из разных альбомов/3D-моделей не считаются автоматически взаимозаменяемыми."]}
    if project.properties.get("vendor_design") is None:
        return result
    result["attached"] = True
    validation = validate_vendor_design(project)
    result["issues"].extend(validation["issues"])
    if not validation["valid"]:
        return result
    design = VendorDesign.model_validate(validation["normalized"])
    snapshot = design.snapshot
    result["snapshot_id"] = snapshot.snapshot_id
    zones, systems = _index(project), {p.id: p for p in project.panel_systems}
    rules, documents = {r.id: r for r in snapshot.rules}, {d.id: d for d in snapshot.documents}
    signatures = {z["zone_id"]: z["zone_signature"] for z in signatures}
    for binding in sorted(design.zone_bindings, key=lambda b: b.zone_id):
        zone, _ = zones[binding.zone_id]
        rule = rules[binding.assembly_rule_id]
        numeric = sorted((r for r in snapshot.rules if r.kind == "numeric" and r.assembly_id == rule.assembly_id
                          and r.source.document_id == rule.source.document_id and r.system == rule.system), key=lambda r: r.id)
        related = sorted((r for r in snapshot.rules if r.id != rule.id and r.assembly_id == rule.assembly_id
                          and r.source.document_id == rule.source.document_id and r.system == rule.system), key=lambda r: r.id)
        item = {"zone_id": zone.id, "system": binding.system, "rule_id": rule.id, "title": rule.title,
                "assembly_id": rule.assembly_id, "source": _source(rule, documents),
                "component_codes": list(rule.component_codes), "contexts": list(rule.contexts),
                "note": binding.note, "approval_status": "unreviewed", "implicit_approval": False,
                "zone_signature": signatures[zone.id], "binding_signature": binding.zone_signature,
                "requirements": [{"rule_id": r.id, "title": r.title, "requirement": r.requirement,
                                  "scope": r.scope, "kind": r.kind, "source": _source(r, documents)} for r in [rule, *related]],
                "checks": []}
        path = "/properties/vendor_design/zone_bindings"
        if not binding.zone_signature:
            result["issues"].append(_issue("VENDOR_BINDING_CONTEXT_UNRECORDED", "Контекст зоны при выборе сборки не зафиксирован: выберите сборку повторно и проверьте применимость.", path, zone.id))
        elif binding.zone_signature != signatures[zone.id]:
            result["issues"].append(_issue("VENDOR_BINDING_STALE", "После выбора сборки изменилась зона, система или геометрия этажа. Требуется повторная проверка привязки.", path, zone.id))
        if rule.source_status != "verified":
            result["issues"].append(_issue("VENDOR_SOURCE_UNVERIFIED", "Первоисточник выбранной сборки не подтверждён контрольной суммой и страницей.", path, zone.id, rule_id=rule.id))
        # Every selected document remains an engineering candidate even if every
        # automatically measurable scalar happens to satisfy its printed range.
        result["issues"].append(_issue("VENDOR_DESIGN_REVIEW_REQUIRED", "Выбранная сборка методички требует проверки применимости, состава, основания и размеров; автоматического утверждения нет.", path, zone.id, rule_id=rule.id))
        panel = systems.get(zone.panel_system_id)
        joint_rules = [r for r in numeric if r.parameter == "panel_joint_width_mm" and r.unit == "mm"]
        bounds = []
        for nr in joint_rules:
            low = nr.value if nr.value is not None else nr.value_min
            high = nr.value if nr.value is not None else nr.value_max
            check = {"rule_id": nr.id, "parameter": nr.parameter, "minimum_mm": low, "maximum_mm": high,
                     "actual_mm": float(panel.joint_mm) if panel else None, "source": _source(nr, documents),
                     "scope": nr.scope, "approval_status": "unreviewed"}
            if rule.source_status != "verified" or nr.source_status != "verified" or panel is None:
                check["status"] = "blocked"
                result["issues"].append(_issue("VENDOR_NUMERIC_CHECK_BLOCKED", "Числовая сверка шва невозможна: не подтверждён источник или система зоны.", path, zone.id, rule_id=nr.id))
            else:
                bounds.append((low, high))
                matches = low - 1e-9 <= float(panel.joint_mm) <= high + 1e-9
                check["status"] = "matches_transcribed_value" if matches else "mismatch"
                if not matches:
                    result["issues"].append(_issue("VENDOR_JOINT_MISMATCH", f"Шов системы {float(panel.joint_mm):g} мм не соответствует выбранной сборке {rule.assembly_id}: {low:g}–{high:g} мм. Геометрия автоматически не изменена.", path, zone.id, rule_id=nr.id))
            item["checks"].append(check)
        if bounds and max(b[0] for b in bounds) > min(b[1] for b in bounds):
            result["issues"].append(_issue("VENDOR_REQUIREMENTS_CONFLICT", "В привязанных выписках одной сборки несовместимые размеры шва. Нельзя выбрать одно значение без решения проектировщика.", path, zone.id))
        if not joint_rules:
            item["checks"].append({"parameter": "panel_joint_width_mm", "status": "not_specified",
                                   "actual_mm": float(panel.joint_mm) if panel else None,
                                   "approval_status": "unreviewed", "message": "В выбранной сборке нет структурированного правила ширины шва. Значение не проверено."})
        result["bindings"].append(item)

    by_zone = {b["zone_id"]: b for b in result["bindings"]}
    for need in node_requirements:
        binding = by_zone.get(need.get("zone_id"))
        if not binding or binding["source"]["source_status"] != "verified":
            continue
        # No rule in these atlases resolves a door/window reveal. Corner
        # pictures are only associated with corners, never opening jambs.
        allowed = {"floor": {"floor_junction", "floor"}, "ceiling": {"ceiling_junction", "ceiling"},
                   "corner": {"internal_corner", "external_corner", "corner"}}
        if not allowed.get(need.get("kind"), set()).intersection(binding["contexts"]):
            continue
        result["node_references"].append({"node_requirement_id": need["id"], "zone_id": binding["zone_id"],
                                           "rule_id": binding["rule_id"], "source": binding["source"],
                                           "status": "reference_only", "implicit_approval": False})
    return result
