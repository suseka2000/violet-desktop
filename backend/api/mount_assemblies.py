"""Explicit plan/build endpoints for real, collision-checked local node cuts."""
from __future__ import annotations

import time
from collections import OrderedDict
import json

from .mount_sections import SectionPlanRequest, _targets
from ..cad_sections import SHA256, make_section_snapshot
from ..domain.models import Model, Project
from ..mount_assemblies import (AssemblyCollection, AssemblyRecord, MAX_ASSEMBLIES, POLICY, assembly_identity,
                                detail_identity, digest, pack_assembly_step, validate_assemblies)
from ..wall_mount import MountScope, pinned_step_assets, preview_wall_mount, validate_wall_mounting

WARNING = "Разрез получен из реальной сборки STEP и параметрических деталей. Посадки и пересечения проверяются геометрически; прочность и монтажная последовательность требуют отдельного расчёта."


class GeometryResultCache:
    """Reuse any repeated detail, not just the first eight encountered recipes."""
    def __init__(self, max_bytes=64*1024*1024):
        self.max_bytes = max_bytes
        self.bytes = 0
        self.values = OrderedDict()

    def get(self, key):
        found = self.values.get(key)
        if found is None:
            return None
        self.values.move_to_end(key)
        return found[0]

    def put(self, key, value):
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
        if size > self.max_bytes:
            return
        old = self.values.pop(key, None)
        if old:
            self.bytes -= old[1]
        while self.values and self.bytes + size > self.max_bytes:
            _, (_, removed_size) = self.values.popitem(last=False)
            self.bytes -= removed_size
        self.values[key] = (value, size)
        self.bytes += size


class AssemblyBuildRequest(SectionPlanRequest):
    assembly_id: SHA256


class AssemblyPrepareRequest(Model):
    project: Project
    scope: MountScope | None = None


def _descriptors(project, scope):
    from ..assembly_recipes import recipe_for
    mounting = validate_wall_mounting(project)
    if mounting is None:
        raise ValueError("Сначала примените монтаж помещения или этажа")
    targets = _targets(project, scope) if scope is not None else {(binding.wall_id, binding.face_id) for binding in mounting.walls}
    assets = pinned_step_assets(mounting.snapshot)
    floors = {floor.id: floor for building in project.buildings for floor in building.floors}
    items, skipped = {}, []
    for binding in mounting.walls:
        if (binding.wall_id, binding.face_id) not in targets:
            continue
        try:
            view = preview_wall_mount(project, binding.wall_id, binding.face_id, binding.configuration, mounting.snapshot, assets)
        except ValueError as error:
            skipped.append({"label": binding.wall_id, "message": str(error)})
            continue
        if view["geometry_signature"] != binding.geometry_signature:
            skipped.append({"label": view["wall"]["name"], "message": "Геометрия стены изменилась. Сначала пересчитайте монтаж."})
            continue
        for node in view["nodes"]:
            descriptor = recipe_for(view, node, assets)
            if not descriptor or not descriptor.get("spec"):
                skipped.append({"label": node["label"], "message": (descriptor or {}).get("message", "Для этого сочетания пока нет проверяемого рецепта сборки из имеющихся STEP.")})
                continue
            detail = detail_identity(view, node)
            identifier = assembly_identity(detail, descriptor)
            entry = items.setdefault(identifier, {"id": identifier, "label": descriptor["label"], "recipe_id": descriptor["recipe_id"],
                "detail_key": detail, "component_codes": [s["component_code"] for s in descriptor["source_models"]],
                "source_ids": [s["source_id"] for s in descriptor["source_models"]], "locations": [],
                "assumptions": descriptor["assumptions"], "descriptor": descriptor,
                "fastener_assembly": descriptor.get("fastener_assembly"),
                "configuration": view["configuration"], "source_refs": [*descriptor.get("source_refs",[]),
                    *(node.get("source_refs") or ([node["source"]] if node.get("source") else []))]})
            floor = floors.get(view["wall"]["floor_id"])
            room = next((space for space in floor.spaces if space.id == view["wall"].get("space_id")), None) if floor else None
            context = [floor.name if floor else "", f"{room.code} · {room.name}" if room else "", view["wall"]["name"], node["label"]]
            position = f"X={node['x_mm']:g}, Y={node['y_mm']:g} мм от начала развёртки"
            entry["locations"].append(" / ".join(part for part in context if part) + " · " + position)
    unique_skips = list({(s["label"], s["message"]): s for s in skipped}.values())
    return items, unique_skips


def assembly_plan(project, scope):
    scope = MountScope.model_validate(scope)
    existing = current_assemblies(project)
    descriptors, skipped = _descriptors(project, scope)
    ids = {entry.id for entry in existing.items}
    pending = []
    for identifier, item in descriptors.items():
        if identifier in ids:
            skipped.append({"label": item["label"], "message": "Точная сборка с этими параметрами уже сохранена."})
        elif len(pending) >= MAX_ASSEMBLIES - len(existing.items):
            skipped.append({"label": item["label"], "message": f"Достигнут лимит {MAX_ASSEMBLIES} сборок в проекте."})
        else:
            pending.append({key: value for key, value in item.items() if key not in {"descriptor", "configuration", "source_refs"}})
    return {"schema_version": "mount-assembly-plan/1.0", "policy": POLICY, "items": pending, "skipped": skipped,
            "current_ids": sorted(ids), "pruned_ids": _pruned_ids(project, existing),
            "saved_ids": [identifier for identifier in descriptors if identifier in ids],
            "saved_items": [{key: value for key, value in item.items() if key not in {"descriptor", "configuration", "source_refs"}}
                            for identifier, item in descriptors.items() if identifier in ids],
            "warnings": [WARNING], "context_signature": digest({"project": project.model_dump(mode="json"), "scope": scope.model_dump(mode="json")}),
            "approval_status": "unreviewed"}


def assembly_build_one(project, scope, assembly_id, model_library, source_store):
    from ..workspace import validate_project_properties_budget
    validate_project_properties_budget(project)
    scope = MountScope.model_validate(scope)
    existing = current_assemblies(project)
    if any(entry.id == assembly_id for entry in existing.items):
        return {"status": "skipped", "message": "Эта сборка уже сохранена; ничего не заменено."}
    items, _ = _descriptors(project, scope)
    item = items.get(assembly_id)
    if item is None:
        raise ValueError("Сборка не соответствует текущему монтажу и исходникам")
    return {**_build_item(project, existing, item, model_library, source_store), "pruned_ids": _pruned_ids(project, existing)}


def _pruned_ids(project, current):
    kept = {entry.id for entry in current.items}
    return sorted({entry.get("id") for entry in project.properties.get("mount_assemblies", {}).get("items", [])
                   if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"] not in kept})


def current_assemblies(project, descriptors=None, *, server_owned=False):
    """Drop obsolete recipe/system versions, retaining all active project scopes.

    Individual details are keyed by their complete geometric recipe, therefore
    a shared cut is retained once even when several walls use it. The caller's
    project and immutable revision journal are never changed here.
    """
    if descriptors is None:
        mounting = validate_wall_mounting(project)
        descriptors = _descriptors(project, None)[0] if mounting and mounting.walls else {}
    value = project.properties.get("mount_assemblies", {"items": []})
    if not isinstance(value, dict) or not isinstance(value.get("items", []), list):
        raise ValueError("Сборки должны быть коллекцией со списком записей")
    if value.get("schema_version", "mount-assemblies/1.0") != "mount-assemblies/1.0":
        raise ValueError("Неизвестная версия коллекции сборок")
    raw = value.get("items", [])
    if any(not isinstance(entry, dict) or not isinstance(entry.get("id"), str) for entry in raw):
        raise ValueError("Некорректная запись сборки")
    # Filter before heavy STEP validation: old cache entries are not input to a
    # new assembly and must not consume its record or geometry budgets.
    items = [entry for entry in raw if entry.get("id") in descriptors]
    if server_owned:
        from ..server_node_records import ServerNodeCollection
        return ServerNodeCollection.model_validate({"items": items})
    return AssemblyCollection.model_validate({"items": items})


def _build_item(project, existing, item, model_library, source_store, *, timeout_seconds=90, geometry_cache=None, server_owned=False):
    """Build one already resolved exact descriptor; never mutate its project.

    An automatic run may reuse identical source/spec geometry across distinct
    detail identities. The record, provenance and fingerprint are still rebuilt
    and validated for each location/configuration; no prior wall's identity is
    borrowed. The cache exists only for the bounded current request.
    """
    import hashlib
    from ..assembly_worker import run_assembly_build
    from .cad_assets import CAD_SLOTS
    from .server import HttpError
    from ..workspace import validate_project_properties_budget

    if server_owned:
        from ..server_node_records import ServerNodeCollection
        if not isinstance(existing, ServerNodeCollection):
            raise TypeError("Нужна проверенная серверная коллекция узлов")
    descriptor = item["descriptor"]
    sources = {}
    for source in descriptor["source_models"]:
        identifier = source["source_id"]
        try:
            content = source_store.get_bytes(identifier)
        except FileNotFoundError:
            content, _ = model_library.original(identifier)
        if hashlib.sha256(content).hexdigest() != identifier:
            raise ValueError("Изменился исходный STEP; сборка остановлена")
        sources[identifier] = content
    cache_key = digest({"sources": sorted(sources), "spec": descriptor["spec"]})
    built = geometry_cache.get(cache_key) if geometry_cache is not None else None
    if built is None:
        if not CAD_SLOTS.acquire(blocking=False):
            raise HttpError(429, "cad_busy", "Обработчик CAD занят. Дождитесь завершения текущей операции.")
        try:
            built = run_assembly_build(sources, descriptor["spec"], timeout_seconds=timeout_seconds)
        finally:
            CAD_SLOTS.release()
        if isinstance(geometry_cache, GeometryResultCache):
            geometry_cache.put(cache_key, built)
        elif geometry_cache is not None and len(geometry_cache) < 8:
            geometry_cache[cache_key] = built
    fastened = bool(descriptor["spec"].get("fastenings"))
    if descriptor["spec"].get("contacts") and (len(built.get("contact_checks", [])) != len(descriptor["spec"]["contacts"]) or any(check.get("status") != "valid" or check.get("issues") for check in built.get("contact_checks", []))):
        return {"status": "skipped", "message": "Не подтверждён контакт деталей: подоснова, профиль или клеевой участок не опирается на соседнюю деталь.",
                "contact_checks": built.get("contact_checks", []), "collisions": built["collisions"], "warnings": [WARNING]}
    if fastened and (len(built.get("fastening_checks", [])) != len(descriptor["spec"]["fastenings"]) or any(check.get("status") != "valid" or check.get("issues") for check in built.get("fastening_checks", []))):
        return {"status": "skipped", "message": "Крепёж не прошёл проверку: головка не опирается на деталь, стержень не достигает крепления или обнаружено постороннее пересечение.",
                "fastening_checks": built.get("fastening_checks", []), "collisions": built["collisions"], "warnings": [WARNING]}
    if built["collisions"]:
        return {"status": "skipped", "message": "Сборка отклонена: реальные тела пересекаются по объёму. Измените толщину/шов или выберите другую систему.",
                "collisions": built["collisions"], "warnings": [WARNING]}
    if any(body["open_wire_count"] or (not body["paths"] and not fastened) for body in built["result"]["components"]) or not any(body["paths"] for body in built["result"]["components"]):
        return {"status": "skipped", "message": "Разрез не пересекает все компоненты замкнутыми контурами; сборка не добавлена.", "warnings": [WARNING]}
    section = built["request"]["section"]
    snapshot = make_section_snapshot(built["assembled_step"]["source_id"], {**section, "component_ids": None}, built["result"])
    record = {key: item[key] for key in ("id", "label", "recipe_id", "detail_key", "configuration", "assumptions", "source_refs")}
    record.update(source_models=descriptor["source_models"], spec=descriptor["spec"], geometry_snapshot=snapshot,
                  bodies=built["bodies"], collisions=built["collisions"], assembled_step=pack_assembly_step(built["assembled_step"]) if fastened else built["assembled_step"], approval_status="unreviewed")
    if fastened:
        record.update(fastening_checks=built.get("fastening_checks"), intentional_engagements=built.get("intentional_engagements"))
    if descriptor["spec"].get("contacts"):
        record["contact_checks"] = built.get("contact_checks")
    if descriptor.get("fastener_assembly") is not None:
        record["fastener_assembly"] = {**descriptor["fastener_assembly"], **({"status": "geometry_verified"} if fastened else {})}
    record["fingerprint"] = digest(record)
    validated = AssemblyCollection.model_validate({"items": [record]}).items[0].model_dump(mode="json", by_alias=True)
    if not server_owned:
        AssemblyCollection.model_validate({"items": [*(entry.model_dump(mode="json", by_alias=True) for entry in existing.items), validated]})
        projected=project.model_copy(deep=True)
        projected.properties["mount_assemblies"]={"schema_version":"mount-assemblies/1.0","items":[*(entry.model_dump(mode="json",by_alias=True) for entry in existing.items),validated]}
        validate_project_properties_budget(projected)
    # Exact original bytes may be cached. The user's project is never saved here.
    for source in descriptor["source_models"]:
        source_store.put(source["filename"], sources[source["source_id"]])
    return {"status": "ready", "assembly": validated, "warnings": [WARNING, *built.get("warnings", [])]}


def assembly_prepare(project, model_library, source_store, scope=None, *, timeout_seconds=90):
    """Prepare all applicable missing real cuts on an isolated project copy.

    This is a read-only project operation: the UI atomically applies the returned
    collection after checking that its draft did not change. Exact current
    records are reused; superseded recipe/system versions are replaced. Missing or unsupported
    details remain visible in the result and can use a non-STEP detail drawing.
    """
    from ..workspace import validate_project_properties_budget
    from .server import HttpError

    validate_project_properties_budget(project)
    scope = MountScope.model_validate(scope) if scope is not None else None
    initial = digest(project.model_dump(mode="json"))
    working = project.model_copy(deep=True)
    all_descriptors, _ = _descriptors(working, None) if validate_wall_mounting(working) else ({}, [])
    existing = current_assemblies(working, all_descriptors)
    working.properties["mount_assemblies"] = existing.model_dump(mode="json", by_alias=True)
    result = {"schema_version": "mount-assembly-preparation/1.0", "initial_signature": initial,
              "scope": scope.model_dump(mode="json") if scope else None, "ready": 0,
              "reused": 0, "outcomes": [], "skipped": [], "warnings": [],
              "pruned_ids": _pruned_ids(project, existing)}
    mounting = validate_wall_mounting(working)
    if mounting is None or not mounting.walls:
        result["assemblies"] = existing.model_dump(mode="json", by_alias=True)
        return result
    deadline = time.monotonic() + min(90, max(.1, timeout_seconds))
    descriptors, skipped = _descriptors(working, scope)
    result["skipped"] = skipped
    known = {record.id for record in existing.items}
    cache = GeometryResultCache()
    for identifier, item in descriptors.items():
        if identifier in known:
            result["reused"] += 1
            continue
        outcome = {"id": identifier, "label": item["label"]}
        if len(existing.items) >= MAX_ASSEMBLIES:
            outcome.update(status="skipped", message=f"Достигнут лимит {MAX_ASSEMBLIES} различных актуальных сборок. Устаревшие версии уже исключены.")
        elif time.monotonic() >= deadline:
            outcome.update(status="skipped", reason="preparation_deadline", message="Истёк лимит одного прохода подготовки. Уже построенные сборки сохранены; оставшиеся будут подготовлены следующим проходом.")
        else:
            try:
                # Threaded screws and direct corner fixings can need more than
                # 30 seconds. Give one worker the remaining bounded request
                # budget; later details continue in the next atomic UI pass.
                build_timeout = min(90, max(.1, deadline-time.monotonic()))
                built = _build_item(working, existing, item, model_library, source_store,
                                    timeout_seconds=build_timeout, geometry_cache=cache)
                outcome.update({key: built[key] for key in ("status", "message") if key in built})
                if built["status"] == "ready":
                    collection = {"schema_version": "mount-assemblies/1.0", "items": [
                        *(entry.model_dump(mode="json", by_alias=True) for entry in existing.items), built["assembly"]]}
                    existing = AssemblyCollection.model_validate(collection)
                    working.properties["mount_assemblies"] = collection
                    known.add(identifier)
                    result["ready"] += 1
            except (FileNotFoundError, ValueError, HttpError) as error:
                # Failed details do not discard earlier valid details. Do not
                # conceal programming errors with an unrestricted exception.
                outcome.update(status="skipped", message=getattr(error, "message", None) or str(error))
                if getattr(error, "code", None) == "STEP_TIMEOUT" and build_timeout < 89.9:
                    outcome["reason"] = "preparation_deadline"
        result["outcomes"].append(outcome)
    result["assemblies"] = existing.model_dump(mode="json", by_alias=True)
    result["continuation_required"] = result["ready"] > 0 and any(item.get("reason") == "preparation_deadline" for item in result["outcomes"])
    if result["ready"]:
        result["warnings"] = [WARNING]
    return result


def handle_mount_assemblies(handler, method, path, model_library, source_store):
    if path not in {"/api/v1/wall-mount/assembly-plan", "/api/v1/wall-mount/assembly-build-one", "/api/v1/wall-mount/assembly-prepare"}:
        return False
    from .server import HttpError
    if method != "POST":
        raise HttpError(405, "method_not_allowed", "Подготовка сборок принимает POST")
    if path.endswith("/assembly-prepare"):
        request = AssemblyPrepareRequest.model_validate(handler.body())
        result = assembly_prepare(request.project, model_library, source_store, request.scope)
    elif path.endswith("/assembly-plan"):
        request = SectionPlanRequest.model_validate(handler.body())
        result = assembly_plan(request.project, request.scope)
    else:
        request = AssemblyBuildRequest.model_validate(handler.body())
        result = assembly_build_one(request.project, request.scope, request.assembly_id, model_library, source_store)
    handler.send_json(result)
    return True
