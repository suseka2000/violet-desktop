"""Rooms use the same disk-backed exact assembly pipeline as whole buildings.

There is no room-specific inventory quota. Preview is loaded in independent
chunks and downloads stream from disk; native verification remains complete.
"""
from .building_assembly_jobs import BuildingAssemblyJobs
from .room_assembly import POLICY, prepare_room, room_targets, refresh_room_manifest


class StreamRoomAssemblyJobs(BuildingAssemblyJobs):
    policy = POLICY
    scope_key = "space_id"
    result_schema = "room-assembly/1.0"
    full_step_filename = "room.step"
    api_prefix = "/api/v1/room-assembly"

    def __init__(self, repository, assembly_jobs, *, path=None, start=True):
        path = path or repository.db_path.with_name(repository.db_path.stem + ".room-assemblies.sqlite3")
        super().__init__(repository, assembly_jobs, path=path, start=start)

    def _targets(self, project, identifier):
        return room_targets(project, identifier)

    def _prepare(self, project, identifier):
        result, records = prepare_room(project, identifier, include_preview=False)
        if result.get("missing"):
            return result, records
        floor = next(floor for building in project.buildings for floor in building.floors
                     if any(space.id == identifier for space in floor.spaces))
        result["scope_kind"] = "room"
        result["floors"] = [{"id": floor.id, "name": floor.name, "elevation_mm": floor.elevation_mm}]
        for wall in result["walls"]:
            wall["floor_id"] = floor.id
        # Never retain a second, unfitted preview while native definitions are
        # being generated. Only the verified streamed preview is published.
        result.pop("wire_preview", None)
        return result, records

    def _refresh_manifest(self, result, walls, records):
        return refresh_room_manifest(result, walls, records)

    def _message(self, value):
        return value.replace("здания", "помещения").replace("здание", "помещение").replace("этажи", "стороны")

    def _update(self, identifier, **values):
        if isinstance(values.get("message"), str):
            values["message"] = self._message(values["message"])
        super()._update(identifier, **values)
