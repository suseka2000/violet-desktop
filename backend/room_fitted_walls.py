"""Rebuild complete wall fixing rows at coordinated room corner boundaries."""
from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json
import logging
from pathlib import Path
import threading

from .mount_assemblies import digest
from .room_joints import fitted_preview
from .wall_framing import plan_wall_framing
from .whole_wall_assemblies import WholeWallRecord, record_identity, pack_step, check_geometry


def _source_bytes(identifier, source_store, model_library):
    try:
        content = source_store.get_bytes(identifier)
    except FileNotFoundError:
        content, _ = model_library.original(identifier)
    if hashlib.sha256(content).hexdigest() != identifier:
        raise ValueError("Контрольная сумма исходного профиля для стыка комнаты изменилась")
    return content


def _check_fitted_geometry(geometry, plan, original, index):
    """Keep native rejection evidence tied to the exact failed wall/face."""
    try:
        check_geometry(geometry, plan)
    except ValueError as error:
        checks = geometry.get("checks", {})
        checks = checks if isinstance(checks, dict) else {}
        invalid = {key: [item for item in geometry.get(key, [])
                         if isinstance(item, dict) and item.get("status") == "invalid"]
                   for key in ("fastening_checks", "contact_checks", "anchor_checks")}
        logging.error("Fitted wall %s rejected: label=%s wall_id=%s face_id=%s plan_id=%s checks=%s invalid=%s",
                      index+1, original.label, original.wall_id, original.face_id, plan.get("id"),
                      json.dumps(checks, ensure_ascii=False), json.dumps(invalid, ensure_ascii=False))
        summary = []
        for key in ("issues", "disconnected_instances"):
            values = checks.get(key, [])
            if isinstance(values, list) and values:
                summary.append(key+"="+", ".join(str(value) for value in values[:20])
                               +(f" (всего {len(values)})" if len(values)>20 else ""))
        detail = "; ".join(summary)
        raise ValueError(f"Не прошла проверку согласованная стена №{index+1}: {original.label} "
                         f"[wall={original.wall_id}, face={original.face_id}]. {error}"
                         +(f"; {detail}" if detail else "")) from error


def boundary_profile_breaks(result):
    """World joint extents become local profile cuts, without moving a floor."""
    output = {}
    edges = {}
    for joint in result.get("joints", []):
        for side in joint["sides"]:
            if side.get("replace_boundary_profile") is False:
                continue
            index = side["wall_index"]
            wall = result["walls"][index]
            base = wall["translation_mm"][2]
            span = wall.get("room_span", {})
            if span.get("sections") and "panel_end_local_x_mm" in side:
                edge = round(side["panel_end_local_x_mm"]-span["start_mm"], 6)
                values = edges.setdefault(index, {}).setdefault(edge, set())
            else:
                values = output.setdefault(index, {}).setdefault(side["end"], set())
            values.update(round(value-base, 6) for interval in joint["segments_z_mm"] for value in interval)
    output = {index: {side: sorted(values) for side, values in sides.items()} for index, sides in output.items()}
    for index, by_edge in edges.items():
        output.setdefault(index, {})["edges"] = [{"edge_mm": edge, "z_mm": sorted(values)}
                                                   for edge, values in sorted(by_edge.items())]
    return output


def rebuild_room_walls(project, result, records, model_library, source_store, *, directory=None, progress=None, current=lambda: True, geometry_cache=None, checkpoint_lock=None, shared_checkpoints=None, checkpoint_context=None):
    """Return derived records; never insert a fitted wall in the source project.

    Per-job checkpoints permit restart without recalculating completed walls.
    Their identity is derived from the entire fitted plan and original sources.
    """
    from .wall_assembly_geometry import required_source_ids
    from .wall_assembly_worker import run_whole_wall_build, wall_build_timeout
    from .assembly_jobs import source_project
    from .wall_mount import validate_wall_mounting,pinned_step_assets
    # Fitting changes only source geometry and mounting inputs. Carrying every
    # derived wall into each virtual-project deepcopy costs O(walls²) memory.
    project=source_project(project)
    mounting=validate_wall_mounting(project)
    assets=pinned_step_assets(mounting.snapshot) if mounting else []
    from .wall_geometry_cache import geometry_key,rebind_record
    from .step_geometry import StepGeometryError
    fitted = [None]*len(records)
    walls = deepcopy(result["walls"])
    # Set out finish-only transition clearances before cutting corner stock.
    # The actual fitted panel bands, not the measured structural doorway, are
    # authoritative for the lengths of the complete replacement corner cells.
    previews = {}
    joint_walls = {side['wall_index'] for joint in result.get('joints', []) for side in joint['sides']}
    for index, (placement, original) in enumerate(zip(walls, records)):
        if not current():return None
        span = placement.get('room_span', {'start_mm': 0., 'end_mm': original.plan['wall']['length_mm']})
        changed = (placement.get('rebuild') or span.get('sections') or abs(span['start_mm']) >= 1e-6
                   or abs(span['end_mm']-original.plan['wall']['length_mm']) >= 1e-6)
        if changed or index in joint_walls:
            previews[index] = fitted_preview(project, original.wall_id, original.face_id, span,
                                              mounting=mounting, assets=assets)
        else:
            previews[index] = original.plan
    from .room_transition_profiles import transition_profile_overrides
    overrides=transition_profile_overrides(result,previews,{i:record.plan.get('system',{}) for i,record in enumerate(records)})
    for index,values in overrides.items():
        if not current():return None
        original=records[index]
        previews[index]=fitted_preview(project,original.wall_id,original.face_id,walls[index]['room_span'],
                                       mounting=mounting,assets=assets,transition_overrides=values)
    from .room_fitted_joint_heights import refine_fitted_joints
    result['joints'] = refine_fitted_joints(result, previews)['joints']
    profile_breaks = boundary_profile_breaks(result)
    from .room_corner_envelopes import corner_transition_clearances
    profile_clearances = corner_transition_clearances(result)
    sources_cache = {}
    folder = Path(directory) if directory else None
    tasks=[];abort=threading.Event()
    def live():return not abort.is_set() and current()
    def install(task,record):
        if not live():return False
        index=task["index"];original=records[index];placement=walls[index]
        if geometry_cache is not None:geometry_cache.put(record)
        checkpoint=task["checkpoint"]
        if checkpoint:
            with checkpoint_lock if checkpoint_lock is not None else nullcontext():
                if not live():return False
                folder.mkdir(parents=True,exist_ok=True)
                temporary=checkpoint.with_suffix(".tmp")
                temporary.write_text(record.model_dump_json(by_alias=True));temporary.replace(checkpoint)
                if checkpoint_context is not None:
                    checkpoint_context.remember(checkpoint.name, record)
        placement["original_fingerprint"]=original.fingerprint
        placement["original_length_mm"]=original.plan["wall"]["length_mm"]
        placement["record_id"],placement["fingerprint"]=record.id,record.fingerprint
        placement["translation_mm"]=[placement["translation_mm"][k]+placement["rotation"][k][0]*task["start"] for k in range(3)]
        fitted[index]=record
        return True
    for index, (placement, original) in enumerate(zip(walls, records)):
        if not current():
            return None
        span = placement.get("room_span", {"start_mm": 0., "end_mm": original.plan["wall"]["length_mm"]})
        start, end = span["start_mm"], span["end_mm"]
        if not placement.get("rebuild") and not profile_breaks.get(index) and not profile_clearances.get(index) and abs(start) < 1e-6 and abs(end-original.plan["wall"]["length_mm"]) < 1e-6:
            if geometry_cache is not None:geometry_cache.put(original)
            fitted[index]=original
            continue
        preview = previews[index]
        if profile_breaks.get(index):
            preview["boundary_breaks_z_mm"] = profile_breaks[index]
        if profile_clearances.get(index):
            preview['corner_transition_clearances'] = profile_clearances[index]
        configuration=deepcopy(original.configuration)
        # The derived preview carries the actual substrate interval. Anchor
        # margins are measured independently from its two ends; extending one
        # finish edge must not inflate every horizontal and vertical margin.
        plan = plan_wall_framing(preview, configuration)
        if plan["status"] != "ready":
            reasons = "; ".join(item["message"] for item in plan.get("issues", []) if item.get("severity") == "error")
            raise ValueError(f"Не удалось согласовать сторону {original.label}: {reasons}")
        source_models = []
        for identifier in required_source_ids(plan):
            if identifier not in sources_cache:
                sources_cache[identifier] = _source_bytes(identifier, source_store, model_library)
            known = next((item for item in original.source_models if item["source_id"] == identifier), None)
            source_models.append(known or {"source_id": identifier, "filename": "profile.step"})
        identity = record_identity(plan, source_models)
        checkpoint = folder/f"fitted-{index+1}.json" if folder else None
        record = None
        if checkpoint and checkpoint.is_file() and not checkpoint.is_symlink():
            try:
                cached = WholeWallRecord.model_validate_json(checkpoint.read_bytes())
                if cached.id == identity and (checkpoint_context is None or checkpoint_context.accepts(checkpoint.name, cached)):
                    record = cached
            except (ValueError, OSError):
                pass
        if record is None and geometry_cache is not None:
            record=geometry_cache.get(plan,source_models)
        if record is None and shared_checkpoints is not None:
            record=shared_checkpoints.get(plan,source_models)
        task={"index":index,"start":start,"plan":plan,"sources":source_models,"identity":identity,"checkpoint":checkpoint}
        if record is not None:
            if not install(task,record):return None
            if progress:progress(max(0,sum(item is not None for item in fitted)-1),len(records),original.label,True)
        else:tasks.append(task)
    if not current():return None
    # Identical walls on another floor need distinct placement/record identity,
    # not another native build. Group before dispatch, not after both finish.
    groups={}
    for task in tasks:groups.setdefault(geometry_key(task["plan"],task["sources"]),[]).append(task)
    def build(group):
        task=group[0];plan=task["plan"];original=records[task["index"]]
        while live():
            try:
                built=run_whole_wall_build({key:sources_cache[key] for key in required_source_ids(plan)},plan,
                    timeout_seconds=wall_build_timeout(plan),cancelled=lambda:not live())
                break
            except StepGeometryError as error:
                if error.code!="STEP_BUSY":raise
                # The shared native semaphore may be occupied by another job.
                # Wait cancellably; never create an extra native process.
                if abort.wait(.1):return None
        else:return None
        if not live():return None
        geometry={key:value for key,value in built.items() if key!="step"}
        _check_fitted_geometry(geometry,plan,original,task["index"])
        value={"id":task["identity"],"policy":"whole-wall-physical/1.0","wall_id":original.wall_id,
            "face_id":original.face_id,"label":original.label,"configuration":plan["configuration"],"plan":plan,
            "source_models":task["sources"],"geometry":geometry,"assembled_step":pack_step(built["step"]),"approval_status":"unreviewed"}
        value["fingerprint"]=digest(value)
        return WholeWallRecord.model_validate(value)
    pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix="fitted-wall")
    pending={};remaining=iter(groups.values())
    def submit():
        group=next(remaining,None)
        if group is not None:
            if progress:progress(sum(item is not None for item in fitted),len(records),records[group[0]["index"]].label,False)
            pending[pool.submit(build,group)]=group
    try:
        submit();submit()
        while pending:
            if not live():raise StepGeometryError("Расчёт согласованных стен отменён",code="STEP_CANCELLED")
            done,_=wait(pending,timeout=.1,return_when=FIRST_COMPLETED)
            for future in done:
                group=pending.pop(future)
                record=future.result()
                if not live():raise StepGeometryError("Расчёт согласованных стен отменён",code="STEP_CANCELLED")
                if record is None:return None
                for task in group:
                    rebound=record if task is group[0] else rebind_record(record,task["plan"],task["sources"])
                    if not install(task,rebound):return None
                    if progress:progress(max(0,sum(item is not None for item in fitted)-1),len(records),records[task["index"]].label,task is not group[0])
                submit()
    finally:
        abort.set()
        for future in pending:future.cancel()
        pool.shutdown(wait=True,cancel_futures=True)
    return walls, fitted
