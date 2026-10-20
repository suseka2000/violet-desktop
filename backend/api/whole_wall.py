"""Explicit, read-only preparation of complete wall assemblies."""
import hashlib
from pydantic import Field

from ..cad_sections import SHA256
from ..domain.models import Model, Project, Identifier
from ..mount_assemblies import digest
from ..whole_wall_assemblies import (WholeWallRecord, make_plan, pack_step,
    record_identity, validate_whole_walls, merge_record, check_geometry)


class WallRequest(Model):
    project: Project
    wall_id: Identifier
    face_id: Identifier
    configuration: dict = Field(default_factory=dict)


class ExportRequest(Model):
    assembly: WholeWallRecord


def build_wall(project, wall_id, face_id, configuration, model_library, source_store, *,
               derived=False, existing_records=None, geometry_cache=None, resolved_plan=None, cancelled=None):
    from ..workspace import validate_project_properties_budget
    from ..wall_assembly_geometry import required_source_ids
    from ..wall_assembly_worker import run_whole_wall_build, wall_build_timeout
    from .cad_assets import CAD_SLOTS
    from .server import HttpError
    if derived:
        from ..assembly_jobs import source_project
        validate_project_properties_budget(source_project(project))
    else:
        validate_project_properties_budget(project)
    plan = resolved_plan if resolved_plan is not None else make_plan(project, wall_id, face_id, configuration)
    if plan.get("status") != "ready" or any(issue.get("severity") == "error" for issue in plan.get("issues", [])):
        raise ValueError("Расстановка содержит ошибки: " + "; ".join(issue["message"] for issue in plan.get("issues", []) if issue.get("severity") == "error"))
    inventory = {entry["source_id"]: entry for entry in model_library.inventory()["assets"]}
    sources, source_models = {}, []
    for identifier in required_source_ids(plan):
        asset = inventory.get(identifier)
        if asset is None:
            raise ValueError("В библиотеке отсутствует точный исходный STEP для сборки стены")
        aliases = asset.get("aliases", [])
        filename = aliases[0]["filename"] if aliases else asset.get("filename", "part.step")
        try:
            content = source_store.get_bytes(identifier)
        except FileNotFoundError:
            content, _ = model_library.original(identifier)
        if hashlib.sha256(content).hexdigest() != identifier:
            raise ValueError("Изменились байты исходной STEP-детали")
        sources[identifier] = content
        source_models.append({"source_id": identifier, "filename": filename})
    identifier = record_identity(plan, source_models)
    records = existing_records if existing_records is not None else validate_whole_walls(project).items
    existing = next((record for record in records if record.id == identifier), None)
    if existing:
        return {"status": "ready", "reused": True, "assembly": existing.model_dump(mode="json")}
    if geometry_cache is not None:
        cached = geometry_cache.get(plan, source_models)
        if cached is not None:
            return {"status": "ready", "reused": True, "assembly": cached.model_dump(mode="json")}
    if not CAD_SLOTS.acquire(blocking=False):
        raise HttpError(429, "cad_busy", "Обработчик CAD занят; дождитесь текущего расчёта")
    try:
        built = run_whole_wall_build(sources, plan, timeout_seconds=wall_build_timeout(plan),
                                    **({"cancelled": cancelled} if cancelled is not None else {}))
    finally:
        CAD_SLOTS.release()
    geometry = {key: value for key, value in built.items() if key != "step"}
    if geometry.get("collisions"):
        from ..step_geometry import StepGeometryError
        by_id = {body["id"]:body for body in geometry.get("bodies", [])}
        conflicts = []
        for collision in geometry["collisions"][:3]:
            a, b = (by_id.get(collision.get(key), {}) for key in ("body_a", "body_b"))
            conflicts.append(f"{a.get('label', 'Деталь')} ({a.get('instance_id', '?')}) / "
                             f"{b.get('label', 'деталь')} ({b.get('instance_id', '?')})")
        raise StepGeometryError("Сборка не сохранена: обнаружены пересечения деталей — "+"; ".join(conflicts),
                                code="WALL_ASSEMBLY_COLLISION", details={"collisions":geometry["collisions"][:20]})
    check_geometry(geometry, plan)
    value = {"id": identifier, "policy": "whole-wall-physical/1.0", "wall_id": wall_id, "face_id": face_id,
        "label": plan["wall"]["name"], "configuration": plan["configuration"], "plan": plan,
        "source_models": source_models, "geometry": geometry, "assembled_step": pack_step(built["step"]), "approval_status": "unreviewed"}
    value["fingerprint"] = digest(value)
    record = WholeWallRecord.model_validate(value)
    # Keep expensive successful geometry even if the caller's album/transport
    # budget is later reached. A repeated request must not rebuild that wall.
    if geometry_cache is not None:
        geometry_cache.put(record)
    if derived:
        from ..whole_wall_assemblies import merge_validated_records
        merge_validated_records(records, record)
    else:
        merge_record(project, record)  # Portable draft transport boundary.
    for source in source_models:
        source_store.put(source["filename"], sources[source["source_id"]])
    return {"status": "ready", "reused": False, "assembly": record.model_dump(mode="json")}


def handle_whole_wall(handler, method, path, model_library, source_store, *, geometry_cache=None):
    if path not in {"/api/v1/whole-wall/plan", "/api/v1/whole-wall/build", "/api/v1/whole-wall/export-step"}:
        return False
    if method != "POST":
        from .server import HttpError
        raise HttpError(405, "method_not_allowed", "Используйте POST")
    if path.endswith("export-step"):
        value = ExportRequest.model_validate(handler.body())
        handler.send_bytes(value.assembly.assembled_step.content(), "application/step", "wall.step")
        return True
    value = WallRequest.model_validate(handler.body())
    from ..workspace import validate_project_properties_budget
    validate_project_properties_budget(value.project)
    if path.endswith("/plan"):
        from ..wall_systems import wall_system_capabilities
        handler.send_json({"plan": make_plan(value.project, value.wall_id, value.face_id, value.configuration),
                           "capabilities": wall_system_capabilities()})
    else:
        handler.send_json(build_wall(value.project, value.wall_id, value.face_id, value.configuration,
                                    model_library, source_store, geometry_cache=geometry_cache))
    return True
