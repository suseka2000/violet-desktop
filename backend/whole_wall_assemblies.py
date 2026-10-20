"""Portable whole-wall solids, independent of the local detail snapshots.

Geometry evidence is reproducible, not a load rating or a product approval.
Saved walls are used only while their exact framing plan still matches.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import lzma
import math
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .cad_sections import SHA256
from .domain.models import Identifier, Model, Name
from .mount_assemblies import digest
from .step_geometry import validate_step_content

POLICY = "whole-wall-physical/1.0"


class WholeWallStep(Model):
    source_id: SHA256
    byte_length: int = Field(strict=True, ge=1)
    filename: Name = "wall.step"
    encoding: Literal["xz+base64"] = "xz+base64"
    base64: str = Field(min_length=1)

    def content(self):
        try:
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=64*1024*1024)
            value = decoder.decompress(base64.b64decode(self.base64, validate=True), max_length=self.byte_length+1)
            if len(value) != self.byte_length or not decoder.eof or decoder.unused_data:
                raise ValueError("Неполный или слишком большой STEP стены")
            if hashlib.sha256(value).hexdigest() != self.source_id:
                raise ValueError("Контрольная сумма STEP стены не совпадает")
            return value
        except (ValueError, binascii.Error, lzma.LZMAError) as error:
            raise ValueError("Повреждён сжатый STEP стены") from error

    @model_validator(mode="after")
    def coherent(self):
        if "\\" in self.filename or Path(self.filename).name != self.filename or Path(self.filename).suffix.lower() not in {".step", ".stp"}:
            raise ValueError("Нужно простое имя файла STEP без пути")
        validate_step_content(self.content(), max_bytes=None)
        return self


def pack_step(value):
    encoded = value.get("content_base64")
    if not isinstance(encoded, str):
        raise ValueError("Недопустимый размер STEP стены")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Повреждён результат STEP стены") from error
    if not content or len(content) != value.get("byte_length") or hashlib.sha256(content).hexdigest() != value.get("sha256"):
        raise ValueError("Неверные размер или контрольная сумма STEP стены")
    return WholeWallStep(source_id=value["sha256"], byte_length=len(content),
        base64=base64.b64encode(lzma.compress(content, preset=6)).decode()).model_dump(mode="json")


def record_identity(plan, source_models):
    return digest({"policy": POLICY, "plan": plan, "source_models": source_models})


def _bounded_numbers(value, depth=0):
    """Reject hostile/non-finite JSON before a float conversion or hashing."""
    if depth > 60:
        raise ValueError("Снимок стены содержит слишком глубокую вложенность")
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not -1e18 <= value <= 1e18 or not math.isfinite(value):
            raise ValueError("Снимок стены содержит некорректное число")
    elif isinstance(value, dict):
        for item in value.values():
            _bounded_numbers(item, depth+1)
    elif isinstance(value, list):
        for item in value:
            _bounded_numbers(item, depth+1)


def _finite(value, low=-1e12, high=1e12):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and low <= value <= high and math.isfinite(value))


def _records(value, name, maximum=None):
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise ValueError("Некорректный список: " + name)
    identifiers = [item.get("id") for item in value]
    if any(not isinstance(item, str) or not item for item in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("Повторяющиеся или некорректные идентификаторы: " + name)
    return value


def _same_vector(value, expected):
    return isinstance(value, list) and len(value) == len(expected) and all(_finite(v) for v in value) and value == expected


def check_geometry(geometry, plan):
    """Validate saved evidence coherence; native tests happen in the worker."""
    from .fastening_records import validate_fastening_record, validate_contact_record
    if not isinstance(geometry, dict) or not isinstance(plan, dict):
        raise ValueError("Неверный формат объёмной стены")
    _bounded_numbers(geometry)
    _bounded_numbers(plan)
    if any(not isinstance(plan.get(key), list) for key in ("posts","rails","screws","anchors")) or any(not isinstance(plan.get(key), dict) for key in ("layers","configuration","mount_configuration","wall")):
        raise ValueError("Некорректная структура расстановки стены")
    if plan.get("system") is not None and not isinstance(plan["system"], dict):
        raise ValueError("Некорректное описание системы стены")
    if geometry.get("schema") != "whole-wall-assembly-build/1" or geometry.get("collisions") != []:
        raise ValueError("Сборка стены содержит пересечения или неизвестный формат")
    summary = geometry.get("checks", {})
    if not isinstance(summary, dict) or summary.get("status") != "valid" or summary.get("issues") != [] or summary.get("disconnected_instances") != []:
        raise ValueError("Проверка объёмной стены не завершена")
    if geometry.get("plan_id") != plan.get("id") or geometry.get("spec_sha256") != digest(plan):
        raise ValueError("Объёмная сборка относится к другой расстановке")
    bodies = geometry.get("bodies")
    if not isinstance(bodies, list) or not bodies or any(not isinstance(body, dict) for body in bodies):
        raise ValueError("Неверный состав объёмной стены")
    identifiers = [body.get("instance_id") for body in bodies]
    if any(not isinstance(value, str) or not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("Повторяющиеся тела стены")
    by_id = {body["instance_id"]:body for body in bodies}
    indices = [body.get("id") for body in bodies]
    if any(type(index) is not int for index in indices) or set(indices) != set(range(1, len(bodies)+1)):
        raise ValueError("Некорректные номера тел стены")
    for body in bodies:
        bounds = body.get("bounds_mm", {})
        if (not isinstance(body.get("definition_id"), str) or not body["definition_id"]
                or not isinstance(body.get("label"), str) or not isinstance(body.get("provenance"), dict)):
            raise ValueError("Некорректное описание или источник детали стены")
        if not isinstance(body.get("role"), str) or body["role"] not in {"substrate","panel","frame","backing","adhesive","fastener","anchor_bolt","anchor_sleeve","spacer","profile"}:
            raise ValueError("Некорректная роль тела стены")
        if not isinstance(bounds, dict) or not _finite(body.get("volume_mm3"), 0, 1e18) or body["volume_mm3"] <= 0 or any(
            not isinstance(bounds.get(key), list) or len(bounds[key]) != 3 or not all(_finite(value) for value in bounds[key])
            for key in ("min", "max")):
            raise ValueError("Некорректные габариты или объём тела стены")
        if any(bounds["min"][axis] >= bounds["max"][axis] for axis in range(3)):
            raise ValueError("Пустое тело стены")
        size = bounds.get("size")
        if size is not None and (not isinstance(size, list) or len(size) != 3 or not all(_finite(v, 0) for v in size) or any(abs(size[i]-(bounds["max"][i]-bounds["min"][i])) > 1e-4 for i in range(3))):
            raise ValueError("Размеры тела не соответствуют его габаритам")
    concealed = None
    if plan.get("system") is not None:
        # Family-dependent seats and backing are derived from the frozen plan,
        # never from the checks that a saved record asks us to accept.
        from .wall_assembly_concealed import hidden_evidence_spec
        concealed = hidden_evidence_spec(plan, bodies)
    expected_roles = {"substrate":"substrate"}
    members = [*plan.get("posts", []), *plan.get("rails", [])]
    _records(members, "каркас", 500)
    screws = _records(plan.get("screws"), "расстановка саморезов", 6000)
    anchors = _records(plan.get("anchors"), "расстановка анкеров", 6000)
    member_ids = {member["id"] for member in members}
    if any(not isinstance(point.get("post_id"), str) or point["post_id"] not in member_ids or not _finite(point.get("x_mm")) or not _finite(point.get("z_mm")) for point in screws+anchors):
        raise ValueError("Крепление относится к неизвестному элементу каркаса")
    for member in members:
        expected_roles["frame-"+member["id"]] = "frame"
        if concealed is None:
            expected_roles["backing-"+member["id"]] = "backing"
    for point in plan.get("screws", []):
        expected_roles["screw-"+point["id"]] = "fastener"
    for point in plan.get("anchors", []):
        expected_roles.update({"bolt-"+point["id"]:"anchor_bolt", "sleeve-"+point["id"]:"anchor_sleeve", "spacer-"+point["id"]:"spacer"})
    if concealed is not None:
        expected_roles.update(concealed["required_roles"])
    fastening_count = len(screws) if concealed is None else len(concealed["fastenings"])
    if any(by_id.get(identifier, {}).get("role") != role for identifier,role in expected_roles.items()) or not any(body.get("role") == "panel" for body in bodies):
        raise ValueError("В объёмной стене отсутствуют запланированные детали")
    if any(type(summary.get(key)) is not int or summary.get(key) != value for key,value in {"body_count":len(bodies), "connected_body_count":len(bodies),
        "fastener_count":fastening_count, "anchor_count":len(plan.get("anchors", []))}.items()):
        raise ValueError("Сводные количества не соответствуют составу стены")
    for key, expected in (("fastening_checks", fastening_count), ("anchor_checks", len(plan.get("anchors", [])))):
        checks = _records(geometry.get(key), key)
        if len(checks) != expected:
            raise ValueError("Не все соединения стены проверены")
        if any(check.get("status") != "valid" or check.get("issues") != [] for check in checks):
            raise ValueError("Крепёж или анкеры стены не прошли проверку")
    fastening_ids = ({"fix-"+point["id"] for point in screws} if concealed is None
                     else {spec["id"] for spec in concealed["fastenings"]})
    if {check["id"] for check in geometry["fastening_checks"]} != fastening_ids or {check["id"] for check in geometry["anchor_checks"]} != {point["id"] for point in plan["anchors"]}:
        raise ValueError("Проверки крепежа относятся к другой расстановке")
    contacts = _records(geometry.get("contact_checks"), "опорные контакты")
    if any(check.get("status") != "valid" or check.get("issues") != [] for check in contacts):
        raise ValueError("Опорные контакты стены не подтверждены")
    if type(summary.get("contact_count")) is not int or summary.get("contact_count") != len(contacts):
        raise ValueError("Опорные контакты стены повторяются или потеряны")
    graph = {identifier:set() for identifier in by_id}
    contact_spec = []
    contacts_by_pair = {}
    for contact in contacts:
        a,b = contact.get("body_a"), contact.get("body_b")
        if not isinstance(a, str) or not isinstance(b, str) or a not in graph or b not in graph or a == b:
            raise ValueError("Некорректный контакт деталей стены")
        if type(contact.get("body_a_id")) is not int or type(contact.get("body_b_id")) is not int:
            raise ValueError("Некорректные номера контактирующих тел")
        contact_spec.append({"id":contact["id"], "body_a":a, "body_b":b, "min_area_mm2":.1})
        contacts_by_pair[frozenset((a,b))] = contact
        graph[a].add(b); graph[b].add(a)
    validate_contact_record({"contacts":contact_spec}, bodies, contacts)
    thickness = plan.get("layers", {}).get("panel_thickness_mm")
    if not _finite(thickness, 8, 13):
        raise ValueError("Некорректная толщина панели стены")
    fastening_spec = [{"id":"fix-"+point["id"], "fastener_id":"screw-"+point["id"],
        "seat_id":"backing-"+point["post_id"], "axis_origin_mm":[point["x_mm"],thickness+3,point["z_mm"]],
        "axis_direction":[0.,1.,0.], "shaft_radius_mm":2.11, "head_radius_mm":5.4, "head_height_mm":2.45,
        "shaft_length_mm":25., "min_engagement_mm":1., "engagement_targets":["backing-"+point["post_id"],"frame-"+point["post_id"]]}
        for point in screws]
    if concealed is not None:
        fastening_spec = concealed["fastenings"]
    for checked in geometry["fastening_checks"]:
        if any(type(checked.get(key)) is not int for key in ("fastener_body_id", "seat_body_id")) or not _same_vector(checked.get("axis_direction"),[0.,1.,0.]) or not _same_vector(checked.get("axis_origin_mm"), next(spec["axis_origin_mm"] for spec in fastening_spec if spec["id"]==checked["id"])):
            raise ValueError("Некорректные номера или ось самореза стены")
    engagements = geometry.get("intentional_engagements")
    if not isinstance(engagements, list) or any(not isinstance(item, dict) for item in engagements):
        raise ValueError("Некорректный реестр зацеплений стены")
    validate_fastening_record({"fastenings":fastening_spec}, bodies, geometry["fastening_checks"], engagements)
    for engagement in engagements:
        a,b = engagement["fastener_id"], engagement["target_id"]
        graph[a].add(b); graph[b].add(a)
    required_pairs = ({frozenset(("frame-"+member["id"],"backing-"+member["id"])) for member in members}
                      if concealed is None else {frozenset(pair) for pair in concealed["required_pairs"]})
    frame_front = thickness+15 if concealed is None else concealed["frame_front_mm"]
    anchor_points = {point["id"]:point for point in anchors}
    embedment = plan.get("configuration", {}).get("anchor_embedment_mm")
    if not _finite(embedment, 30, 150):
        raise ValueError("Некорректная заданная глубина анкеровки")
    for anchor in geometry["anchor_checks"]:
        point = anchor_points[anchor["id"]]
        expected = {"bolt_id":"bolt-"+point["id"], "sleeve_id":"sleeve-"+point["id"],
                    "spacer_id":"spacer-"+point["id"], "frame_id":"frame-"+point["post_id"], "substrate_id":"substrate"}
        if any(anchor.get(key) != value for key,value in expected.items()) or not _same_vector(anchor.get("axis_origin_mm"),[point["x_mm"],frame_front,point["z_mm"]]) or not _same_vector(anchor.get("axis_direction"),[0.,1.,0.]):
            raise ValueError("Проверка анкера относится к другой оси или деталям")
        if any(not _finite(anchor.get(key), 0) for key in ("embedment_mm", "min_embedment_mm", "bearing_area_mm2", "head_bearing_area_mm2", "required_bearing_area_mm2", "sleeve_substrate_contact_mm2", "required_sleeve_contact_mm2")):
            raise ValueError("Некорректные измерения анкера")
        if anchor["embedment_mm"] != embedment or anchor["min_embedment_mm"] != embedment:
            raise ValueError("Глубина анкера не соответствует расстановке")
        head, bearing, required = (anchor[key] for key in ("head_bearing_area_mm2", "bearing_area_mm2", "required_bearing_area_mm2"))
        if head < 1 or abs(required-head*.9) > 1e-5 or bearing+1e-5 < required or bearing > head+1e-4:
            raise ValueError("Головка анкера не имеет достаточной геометрической опоры")
        sleeve_area = anchor["sleeve_substrate_contact_mm2"]
        required_sleeve = 2*math.pi*4*embedment
        if abs(anchor["required_sleeve_contact_mm2"]-required_sleeve) > 1e-4 or sleeve_area+1e-4 < required_sleeve:
            raise ValueError("Втулка анкера не имеет подтверждённой глубины контакта с основанием")
        bolt, sleeve, spacer, frame = (expected[key] for key in ("bolt_id","sleeve_id","spacer_id","frame_id"))
        required_pairs.update(frozenset(pair) for pair in ((bolt,frame),(bolt,spacer),(bolt,sleeve),(spacer,frame),(spacer,"substrate"),(sleeve,"substrate")))
        measured_contact = contacts_by_pair.get(frozenset((sleeve,"substrate")))
        if measured_contact is None or abs(measured_contact["contact_area_mm2"]-sleeve_area) > 1e-4:
            raise ValueError("Проверка втулки не совпадает с измеренным контактом")
    if not required_pairs.issubset(contacts_by_pair):
        raise ValueError("Пропущен обязательный контакт каркаса или анкеровки")
    for body in bodies:
        neighbours = {by_id[item]["role"] for item in graph[body["instance_id"]]}
        if concealed is not None:
            if body["role"] == "adhesive":
                profile=concealed.get("perimeter_bond_profiles",{}).get(body["instance_id"])
                if profile not in graph[body["instance_id"]] or "backing" not in neighbours or "panel" in neighbours:
                    raise ValueError("Только выбранный профиль V-Sky/V-Start1 может соединяться с подосновой проектным слоем; панели им не приклеиваются")
            if body["role"] == "panel" and "profile" not in neighbours:
                raise ValueError("Панель скрытого монтажа не имеет проверенной посадки на профиль")
            if body["role"] == "profile":
                run=next((r for r in plan["profile_runs"] if "profile-"+r["id"]==body["instance_id"]),{})
                expected=({"panel","adhesive"} if run.get("family")=="sky" else
                          {"panel","adhesive","fastener"} if run.get("family")=="start" else {"panel","backing","fastener"})
                if not expected.issubset(neighbours):
                    raise ValueError("Профиль скрытого монтажа не имеет всех соединений своей измеренной посадки")
            continue
        if body.get("role") == "adhesive" and not {"panel","backing"}.issubset(neighbours):
            raise ValueError("Клеевой слой не соединяет панель с подосновой")
        if body.get("role") == "panel" and "adhesive" not in neighbours:
            raise ValueError("Панель не имеет крепления к подоснове")
    visited, queue = set(), ["substrate"]
    while queue:
        identifier = queue.pop()
        if identifier not in visited:
            visited.add(identifier); queue.extend(graph[identifier]-visited)
    if visited != set(graph):
        raise ValueError("В стене есть детали без соединения с основанием")
    if concealed is not None:
        from .whole_wall_material_records import validate_hidden_material
        validate_hidden_material(plan, geometry)


class WholeWallRecord(Model):
    id: SHA256
    fingerprint: SHA256
    wall_id: Identifier
    face_id: Identifier
    label: Name
    policy: Literal["whole-wall-physical/1.0"] = POLICY
    configuration: dict[str, JsonValue]
    plan: dict[str, JsonValue]
    source_models: list[dict[str, JsonValue]] = Field(min_length=1, max_length=8)
    geometry: dict[str, JsonValue]
    assembled_step: WholeWallStep
    approval_status: Literal["unreviewed"] = "unreviewed"

    @model_validator(mode="after")
    def coherent(self):
        from .wall_framing import WallFramingConfiguration
        _bounded_numbers(self.plan)
        _bounded_numbers(self.configuration)
        _bounded_numbers(self.source_models)
        WallFramingConfiguration.model_validate(self.configuration)
        if self.plan.get("schema_version") != "wall-framing-plan/1.0" or self.plan.get("configuration") != self.configuration:
            raise ValueError("Настройки стены и план расстановки не совпадают")
        if self.plan.get("status") != "ready" or self.plan.get("id") != digest({key:value for key,value in self.plan.items() if key != "id"}):
            raise ValueError("План стены повреждён или не прошёл проверку")
        sources = [source.get("source_id") for source in self.source_models]
        if any(not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in sources) or len(set(sources)) != len(sources):
            raise ValueError("Некорректные ссылки на исходные STEP стены")
        if self.plan.get("system") is not None:
            from .wall_assembly_geometry import required_source_ids
            if set(sources) != set(required_source_ids(self.plan)):
                raise ValueError("Исходные STEP не соответствуют выбранной системе скрытого крепления")
        wall_context = self.plan.get("wall")
        if not isinstance(wall_context, dict) or wall_context.get("id") != self.wall_id or wall_context.get("face_id") != self.face_id:
            raise ValueError("План расстановки относится к другой стороне стены")
        if self.id != record_identity(self.plan, self.source_models):
            raise ValueError("Изменена идентичность сборки стены")
        check_geometry(self.geometry, self.plan)
        if self.fingerprint != digest(self.model_dump(mode="json", exclude={"fingerprint"})):
            raise ValueError("Изменён снимок объёмной стены")
        return self


class WholeWallCollection(Model):
    schema_version: Literal["whole-wall-assemblies/1.0"] = "whole-wall-assemblies/1.0"
    items: list[WholeWallRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        keys = [(record.wall_id, record.face_id) for record in self.items]
        if len(set(keys)) != len(keys):
            raise ValueError("Для стороны стены допускается одна текущая объёмная сборка")
        return self


def validate_whole_walls(project):
    return WholeWallCollection.model_validate(project.properties.get("whole_wall_assemblies", {"items": []}))


def merge_validated_records(records, record):
    """Append one checked record without decompressing every preceding STEP.

    Internal background-build boundary only. Portable dictionaries still enter
    through WholeWallCollection/WholeWallRecord and receive full validation.
    """
    if not isinstance(record, WholeWallRecord) or any(not isinstance(item, WholeWallRecord) for item in records):
        raise TypeError("Нужны проверенные сборки стен")
    result = [item for item in records if (item.wall_id, item.face_id) != (record.wall_id, record.face_id)] + [record]
    return result


def validate_wall_settings(project):
    """Editable framing parameters survive removal/rebuilding of derived solids."""
    from .wall_framing import WallFramingConfiguration
    value = project.properties.get("whole_wall_settings", {"schema_version": "whole-wall-settings/1.0", "walls": []})
    if not isinstance(value, dict) or value.get("schema_version") != "whole-wall-settings/1.0" or not isinstance(value.get("walls"), list):
        raise ValueError("Некорректные настройки сборки стен")
    faces = {(wall.id, face.id) for building in project.buildings for floor in building.floors for wall in floor.walls for face in wall.faces}
    result = {}
    for item in value["walls"]:
        if not isinstance(item, dict) or set(item) != {"wall_id", "face_id", "configuration"}:
            raise ValueError("Некорректная запись настроек сборки стены")
        if not isinstance(item["wall_id"], str) or not isinstance(item["face_id"], str):
            raise ValueError("Нужны идентификаторы стороны стены")
        key = (item["wall_id"], item["face_id"])
        if key not in faces or key in result:
            raise ValueError("Настройки сборки относятся к отсутствующей или повторяющейся стороне стены")
        result[key] = WallFramingConfiguration.model_validate(item["configuration"]).model_dump(mode="json")
    return result


def make_plan(project, wall_id, face_id, configuration=None):
    from .wall_framing import plan_wall_framing
    from .wall_mount import validate_wall_mounting, preview_wall_mount, pinned_step_assets
    mounting = validate_wall_mounting(project)
    binding = next((entry for entry in mounting.walls if (entry.wall_id, entry.face_id) == (wall_id, face_id)), None) if mounting else None
    if binding is None:
        raise ValueError("Сначала примените монтаж к выбранной стороне стены")
    preview = preview_wall_mount(project, wall_id, face_id, binding.configuration, mounting.snapshot, pinned_step_assets(mounting.snapshot))
    if binding.geometry_signature != preview["geometry_signature"]:
        raise ValueError("Геометрия стены изменилась; сначала пересчитайте монтаж")
    wall = next((wall for building in project.buildings for floor in building.floors for wall in floor.walls if wall.id == wall_id), None)
    settings = dict(configuration or {})
    if wall is not None:
        settings.setdefault("substrate_thickness_mm", wall.thickness_mm)
        preview = {**preview,"wall":{**preview["wall"],"modeled_substrate":wall.substrate,"modeled_wall_thickness_mm":wall.thickness_mm}}
    return plan_wall_framing(preview, settings)


def whole_wall_usage(project, *, server_owned=False):
    active, stale = [], []
    if server_owned:
        from .server_wall_records import validate_server_whole_walls, create_wall_planner
        records = validate_server_whole_walls(project).items
        planner = create_wall_planner(project)
    else:
        records = validate_whole_walls(project).items
        planner = lambda wall_id, face_id, configuration: make_plan(project, wall_id, face_id, configuration)
    for record in records:
        try:
            current = planner(record.wall_id, record.face_id, record.configuration)
            if digest(current) != digest(record.plan):
                raise ValueError("Изменились размеры, проёмы, монтаж или настройки расстановки")
            active.append(record)
        except ValueError as error:
            stale.append({"id": record.id, "label": record.label, "message": str(error)})
    return active, stale


def merge_record(project, record):
    existing = validate_whole_walls(project)
    result = WholeWallCollection(items=[*(item for item in existing.items if (item.wall_id, item.face_id) != (record.wall_id, record.face_id)), record])
    copy = project.model_copy(deep=True)
    copy.properties["whole_wall_assemblies"] = result.model_dump(mode="json")
    from .workspace import validate_project_properties_budget
    validate_project_properties_budget(copy)
    return result.model_dump(mode="json")
