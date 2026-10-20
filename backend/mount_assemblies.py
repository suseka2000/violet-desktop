"""Portable actual assembly cuts, with explicit unreviewed placement recipes.

Hashes protect round-trip integrity, not engineering approval or authenticity.
Stored assemblies are reusable local details, not wall geometry. A document may
use one only when its current detail/recipe identity still matches exactly.
"""
from __future__ import annotations

import base64
import binascii
from collections import Counter
import hashlib
import gzip
import json
import lzma
import math
import zlib
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .cad_sections import SHA256, SectionSnapshot, raw_point_count
from .domain.models import Model, Name
from .step_geometry import validate_step_content

# Count distinct *current* details, not historical versions of their recipes.
# Geometry/encoded-byte budgets remain independently enforced below.
MAX_ASSEMBLIES = 256
MAX_ASSEMBLY_POINTS = 120_000
MAX_STEP_BASE64 = 8 * 1024 * 1024
MAX_DECODED_ASSEMBLY_STEP = 8 * 1024 * 1024
POLICY = "actual-node-assembly/1.0"


def digest(value):
    # JavaScript does not distinguish integer-valued floats or negative zero.
    def normalize(v):
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, (int, float)):
            return 0.0 if v == 0 else float(v)
        if isinstance(v, dict):
            return {k: normalize(x) for k, x in v.items()}
        if isinstance(v, list) or isinstance(v, tuple):
            return [normalize(x) for x in v]
        return v
    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def detail_identity(preview, node):
    from .documents.mount_details import detail_key
    return digest(list(detail_key(preview, node)))


class AssemblyStep(Model):
    source_id: SHA256
    byte_length: int = Field(strict=True, ge=1, le=MAX_DECODED_ASSEMBLY_STEP)
    filename: Name
    base64: str = Field(min_length=1, max_length=MAX_STEP_BASE64)
    # Omitted for old records: their portable JSON and fingerprints stay exact.
    encoding: Literal["gzip+base64", "xz+base64"] | None = Field(default=None, exclude_if=lambda value: value is None)

    def content(self):
        try:
            payload = base64.b64decode(self.base64, validate=True)
            if self.encoding is None:
                if len(payload) > MAX_DECODED_ASSEMBLY_STEP:
                    raise ValueError("STEP превышает допустимый размер")
                return payload
            if self.encoding == "xz+base64":
                decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=64*1024*1024)
                content = decoder.decompress(payload, max_length=min(self.byte_length, MAX_DECODED_ASSEMBLY_STEP) + 1)
                if len(content) != self.byte_length or not decoder.eof or decoder.unused_data:
                    raise ValueError("Неверный размер или неполный сжатый STEP")
                return content
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            # Bound allocation before decompression, not after; reject trailing
            # streams, truncated data and compression bombs independently of SHA.
            content = decoder.decompress(payload, min(self.byte_length, MAX_DECODED_ASSEMBLY_STEP) + 1)
            if len(content) != self.byte_length or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
                raise ValueError("Неверный размер или неполный сжатый STEP")
            return content
        except (binascii.Error, ValueError, zlib.error, lzma.LZMAError) as error:
            raise ValueError("Некорректный STEP собранного узла") from error

    @model_validator(mode="after")
    def coherent(self):
        if Path(self.filename).name != self.filename or Path(self.filename).suffix.lower() not in {".step", ".stp"}:
            raise ValueError("Имя сборки должно быть простым именем STEP без пути")
        content = self.content()
        if len(content) != self.byte_length or hashlib.sha256(content).hexdigest() != self.source_id:
            raise ValueError("Байты собранного STEP не соответствуют контрольной сумме")
        validate_step_content(content)
        return self


def pack_assembly_step(value):
    """Lossless portable storage of heavy exact screw geometry, not a proxy.

    The source digest and byte_length always describe the original STEP. Native
    workers and downloaded .step files continue to use the uncompressed bytes.
    """
    if value.get("encoding") is not None:
        return AssemblyStep.model_validate(value).model_dump(mode="json")
    encoded = value.get("base64")
    if not isinstance(encoded, str) or not 0 < len(encoded) <= ((MAX_DECODED_ASSEMBLY_STEP + 2) // 3) * 4:
        raise ValueError("Некорректный размер STEP перед сжатием")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Некорректный STEP перед сжатием") from error
    if not 0 < len(content) <= MAX_DECODED_ASSEMBLY_STEP or len(content) != value.get("byte_length") or hashlib.sha256(content).hexdigest() != value.get("source_id"):
        raise ValueError("Байты STEP перед сжатием не соответствуют контрольной сумме")
    compressed = gzip.compress(content, compresslevel=6, mtime=0)
    if len(content) < 128 * 1024 or len(compressed) >= len(content) * .9:
        return AssemblyStep.model_validate(value).model_dump(mode="json")
    # XZ's dictionary compresses repetitive exact STEP entities substantially
    # better. Preset 6 uses an 8 MiB dictionary; the decoder is capped at 64 MiB.
    xz = lzma.compress(content, format=lzma.FORMAT_XZ, preset=6)
    encoding = "gzip+base64"
    if len(xz) < len(compressed):
        compressed, encoding = xz, "xz+base64"
    packed = {**value, "encoding": encoding, "base64": base64.b64encode(compressed).decode("ascii")}
    return AssemblyStep.model_validate(packed).model_dump(mode="json")


class SourceModel(Model):
    source_id: SHA256
    filename: Name
    component_code: Name


class AssemblyRecord(Model):
    id: SHA256
    label: Name
    recipe_id: Name
    detail_key: SHA256
    configuration: dict[str, JsonValue]
    assumptions: list[str] = Field(min_length=1, max_length=40)
    source_refs: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=20)
    source_models: list[SourceModel] = Field(min_length=1, max_length=8)
    spec: dict[str, JsonValue]
    geometry_snapshot: SectionSnapshot
    bodies: list[dict[str, JsonValue]] = Field(min_length=2, max_length=32)
    collisions: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=496)
    fastening_checks: list[dict[str, JsonValue]] | None = Field(default=None, max_length=32, exclude_if=lambda value: value is None)
    intentional_engagements: list[dict[str, JsonValue]] | None = Field(default=None, max_length=496, exclude_if=lambda value: value is None)
    fastener_assembly: dict[str, JsonValue] | None = Field(default=None, exclude_if=lambda value: value is None)
    contact_checks: list[dict[str, JsonValue]] | None = Field(default=None, max_length=96, exclude_if=lambda value: value is None)
    assembled_step: AssemblyStep
    approval_status: Literal["unreviewed"] = "unreviewed"
    fingerprint: SHA256

    @model_validator(mode="after")
    def coherent(self):
        from .assembly_geometry import validate_assembly_spec
        canonical_spec = validate_assembly_spec(self.spec)
        if canonical_spec["section"]["axis"] != self.geometry_snapshot.request.axis or canonical_spec["section"]["offset_mm"] != self.geometry_snapshot.request.offset_mm:
            raise ValueError("Плоскость разреза не соответствует рецепту сборки")
        expected_id = assembly_identity(self.detail_key, {"recipe_id": self.recipe_id, "spec": self.spec,
            "source_models": [source.model_dump(mode="json") for source in self.source_models], "assumptions": self.assumptions})
        if self.id != expected_id:
            raise ValueError("Идентификатор сборки не соответствует рецепту, источникам и посадкам")
        ids = [body.get("id") for body in self.bodies]
        if ids != [component.id for component in self.geometry_snapshot.result.components]:
            raise ValueError("Состав сборки не соответствует телам сечения")
        if not any(body.get("kind") == "step" for body in self.bodies) or not any(body.get("role") == "panel" for body in self.bodies):
            raise ValueError("Нужны реальные STEP-компоненты и панель")
        sources = {source.source_id for source in self.source_models}
        if any(body.get("source_id") not in sources for body in self.bodies if body.get("kind") == "step"):
            raise ValueError("Тело сборки ссылается на неизвестный исходник")
        def segments(paths):
            found = Counter()
            for path in paths:
                for a,b in zip(path,path[1:]):
                    pair = tuple(sorted((tuple(round(v,4) for v in a),tuple(round(v,4) for v in b))))
                    if pair[0] != pair[1]:
                        found[pair] += 1
            return found
        expected_bodies = []
        for placement in canonical_spec["placements"]:
            expected_bodies.extend((placement, component) for component in placement["component_ids"])
        expected_bodies.extend((box,None) for box in canonical_spec["boxes"])
        if len(expected_bodies) != len(self.bodies):
            raise ValueError("Число тел не соответствует рецепту сборки")
        for body, component, (instance, source_component) in zip(self.bodies,self.geometry_snapshot.result.components,expected_bodies):
            if body.get("instance_id") != instance["id"] or body.get("rotation") != instance["rotation"] or body.get("translation_mm") != instance["translation_mm"]:
                raise ValueError("Положение тела не соответствует рецепту сборки")
            if source_component is not None and (body.get("kind") != "step" or body.get("source_id") != instance["source_id"] or body.get("source_component_id") != source_component):
                raise ValueError("STEP-тело не соответствует выбранному компоненту")
            if source_component is not None and body.get("role") != instance.get("role"):
                raise ValueError("Роль STEP-тела не соответствует рецепту сборки")
            if source_component is None and (body.get("kind") != "generated_box" or body.get("role") != instance["role"] or body.get("size_mm") != instance["size_mm"]):
                raise ValueError("Параметрическая панель не соответствует рецепту")
            if source_component is None and body.get("cutouts", []) != instance.get("cutouts", []):
                raise ValueError("Обработка панели не соответствует вырезам рецепта")
            loops = body.get("section_loops", [])
            if not isinstance(loops,list) or len(loops) != component.closed_wire_count:
                raise ValueError("Замкнутые контуры не соответствуют телу разреза")
            for loop in loops:
                if not isinstance(loop, list) or len(loop) < 4:
                    raise ValueError("Неверный замкнутый контур тела сборки")
                for point in loop:
                    if not isinstance(point, list) or len(point) != 2 or any(isinstance(v, bool) or not isinstance(v, (int,float)) or not math.isfinite(v) or abs(v)>1e9 for v in point):
                        raise ValueError("Некорректная точка контура сборки")
                if loop[0] != loop[-1]:
                    raise ValueError("Контур штриховки не замкнут")
            if segments(loops) != segments(component.paths):
                raise ValueError("Штриховка не соответствует рёбрам действительного сечения")
        fastened = bool(canonical_spec.get("fastenings"))
        if any(c.open_wire_count or (not c.paths and not fastened) for c in self.geometry_snapshot.result.components):
            raise ValueError("Каждое тело узла должно иметь непустой замкнутый разрез")
        if fastened:
            from .fastening_records import validate_fastening_record
            validate_fastening_record(canonical_spec, self.bodies, self.fastening_checks, self.intentional_engagements)
            if self.fastener_assembly is not None and (self.fastener_assembly.get("status") != "geometry_verified" or self.fastener_assembly.get("fastening_count") != len(canonical_spec["fastenings"])):
                raise ValueError("Статус автоматической сборки не соответствует проверенному крепежу")
            if not any(c.paths for c in self.geometry_snapshot.result.components):
                raise ValueError("Плоскость не пересекает собранный узел")
        elif self.fastening_checks is not None or self.intentional_engagements is not None:
            raise ValueError("Проверки крепежа указаны без соединений в рецепте")
        elif self.fastener_assembly is not None and self.fastener_assembly.get("status") != "unsupported":
            raise ValueError("Без проверяемых соединений нельзя объявлять крепёж собранным")
        if canonical_spec.get("contacts"):
            from .fastening_records import validate_contact_record
            validate_contact_record(canonical_spec, self.bodies, self.contact_checks)
        elif self.contact_checks is not None:
            raise ValueError("Проверки опорных контактов указаны без контактов в рецепте")
        if fastened:
            from .fastening_records import validate_connected_assembly
            validate_connected_assembly(self.bodies, self.contact_checks, self.intentional_engagements)
        if self.collisions:
            raise ValueError("Сборка с объёмными пересечениями тел не может быть сохранена как готовый узел")
        if self.assembled_step.source_id != self.geometry_snapshot.source_id or self.assembled_step.byte_length != self.geometry_snapshot.result.byte_length:
            raise ValueError("Разрез относится к другому STEP сборки")
        data = self.model_dump(mode="json", by_alias=True)
        fingerprint = data.pop("fingerprint")
        if fingerprint != digest(data):
            raise ValueError("Контрольная сумма состава/посадок сборки не совпадает")
        return self


class AssemblyCollection(Model):
    schema_version: Literal["mount-assemblies/1.0"] = "mount-assemblies/1.0"
    items: list[AssemblyRecord] = Field(default_factory=list, max_length=MAX_ASSEMBLIES)

    @model_validator(mode="before")
    @classmethod
    def bounded(cls, value):
        if isinstance(value, dict):
            if not isinstance(value.get("items", []), list):
                raise ValueError("Сборки должны быть списком")
            points = encoded = 0
            for item in value.get("items", []):
                if isinstance(item, dict):
                    if not isinstance(item.get("geometry_snapshot"),dict) or not isinstance(item.get("assembled_step"),dict) or not isinstance(item.get("bodies",[]),list):
                        raise ValueError("Некорректный состав снимка сборки")
                    encoded_step = item["assembled_step"].get("base64", "")
                    if not isinstance(encoded_step,str):
                        raise ValueError("Байты STEP сборки должны быть строкой base64")
                    points += raw_point_count(item["geometry_snapshot"].get("result"))
                    encoded += len(encoded_step)
                    for body in item.get("bodies", []):
                        if isinstance(body, dict):
                            if not isinstance(body.get("section_loops",[]),list):
                                raise ValueError("Контуры сборки должны быть списком")
                            points += sum(len(loop) for loop in body.get("section_loops", []) if isinstance(loop, list))
                if points > MAX_ASSEMBLY_POINTS or encoded > MAX_STEP_BASE64:
                    raise ValueError("Сборки превышают лимит 120 000 точек или 8 МиБ сохранённых STEP")
        return value

    @model_validator(mode="after")
    def distinct(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("Идентификаторы сборок не должны повторяться")
        return self


def validate_assemblies(project):
    return AssemblyCollection.model_validate(project.properties.get("mount_assemblies", {"items": []}))


def matching_assembly(project, preview, node):
    """Use exact current geometry, preferring its new physical-fastener recipe."""
    from .wall_mount import validate_wall_mounting, pinned_step_assets
    mounting = validate_wall_mounting(project)
    if mounting is None:
        return None
    key = detail_identity(preview, node)
    return _resolve_saved_assembly({item.id:item for item in validate_assemblies(project).items}, key,
        {**preview, "snapshot": mounting.snapshot.model_dump(mode="json")}, node, pinned_step_assets(mounting.snapshot))


def assembly_identity(detail_key, descriptor):
    return digest({"policy": POLICY, "detail_key": detail_key, "recipe_id": descriptor["recipe_id"],
                   "spec": descriptor["spec"], "source_models": descriptor["source_models"],
                   "assumptions": descriptor["assumptions"]})


def _resolve_saved_assembly(by_id, key, preview, node, assets):
    """Keep an exact pre-hardware cut portable until its upgrade is built.

    This is not general legacy-recipe acceptance: both candidates are derived
    from current geometry and pinned sources. No old placement, assumption or
    source may differ from the current profile-only recipe. Plan/preparation
    still request the new hardware identity; only read-only document use falls
    back, and a saved physical assembly always takes priority.
    """
    from .assembly_recipes import _profile_recipe_for, recipe_for
    descriptor = recipe_for(preview, node, assets)
    if not descriptor or not descriptor.get("spec"):
        return None
    current = by_id.get(assembly_identity(key, descriptor))
    if current is not None:
        return current
    if descriptor.get("fastener_assembly") is None:
        return None
    base = _profile_recipe_for(preview, node, assets)
    if not base or not base.get("spec"):
        return None
    legacy = by_id.get(assembly_identity(key, base))
    if legacy is not None and legacy.fastener_assembly is None and not legacy.spec.get("fastenings"):
        return legacy
    return None


def assembly_usage(project, mounting_report, *, saved=None, server_owned=False):
    """Resolve saved portable details against current mounting, once per report."""
    if saved is None:
        if server_owned:
            from .server_node_records import validate_server_nodes
            saved = validate_server_nodes(project)
        else:
            saved = validate_assemblies(project)
    else:
        from .server_node_records import ServerNodeCollection
        if not isinstance(saved, (AssemblyCollection, ServerNodeCollection)):
            raise TypeError("Нужна проверенная коллекция узлов")
        if isinstance(saved, ServerNodeCollection) and not server_owned:
            raise TypeError("Серверная коллекция используется только для сохранённого проекта")
    if not saved.items:
        return {}, []
    from .assembly_recipes import _profile_recipe_for
    from .wall_mount import validate_wall_mounting, pinned_step_assets
    mounting = validate_wall_mounting(project)
    assets = pinned_step_assets(mounting.snapshot) if mounting else []
    by_id = {item.id: item for item in saved.items}
    matched, checked, superseded = {}, set(), set()
    for preview in (mounting_report or {}).get("walls", []):
        for node in preview["nodes"]:
            key = detail_identity(preview, node)
            if key in checked:
                continue
            checked.add(key)
            current_preview = {**preview, "snapshot": mounting.snapshot.model_dump(mode="json")} if mounting else preview
            record = _resolve_saved_assembly(by_id, key, current_preview, node, assets)
            if record:
                matched[key] = record
                if record.spec.get("fastenings"):
                    # A preserved exact predecessor is history, not a failed
                    # assembly requiring another build. Only the current base
                    # identity is retired, and only after its actual physical
                    # upgrade has been validated and selected for this detail.
                    base = _profile_recipe_for(current_preview, node, assets)
                    previous = by_id.get(assembly_identity(key, base)) if base and base.get("spec") else None
                    if previous is not None and previous.id != record.id and previous.fastener_assembly is None and not previous.spec.get("fastenings"):
                        superseded.add(previous.id)
    used = {item.id for item in matched.values()}
    return matched, [item for item in saved.items if item.id not in used | superseded]
