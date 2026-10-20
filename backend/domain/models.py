"""Version 1 project contract. All geometric lengths are millimetres."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

SCHEMA_VERSION = "1.0"
Number = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-1_000_000_000, le=1_000_000_000)]
Positive = Annotated[Number, Field(gt=0)]
NonNegative = Annotated[Number, Field(ge=0)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Name = Annotated[str, Field(min_length=1, max_length=300)]


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=True, allow_inf_nan=False)


class Entity(Model):
    id: Identifier = Field(default_factory=new_id)
    properties: dict[str, JsonValue] = Field(default_factory=dict)


class Point2(Model):
    x: Number
    y: Number


class ProjectMetadata(Model):
    name: Name
    address: str = ""
    customer: str = ""
    author: str = ""
    stage: str = "Р"
    description: str = ""


class ProjectSettings(Model):
    geometry_tolerance_mm: Annotated[Positive, Field(le=10)] = 0.1
    measurement_tolerance_mm: NonNegative = 1.0


class LevelDatum(Entity):
    name: Name
    kind: Literal["finished_floor", "structural_floor", "finished_ceiling", "custom"] = "custom"
    elevation_mm: Number


class WallFace(Entity):
    side: Literal["left", "right"]
    space_id: Identifier | None = None
    finish: str = ""


class Wall(Entity):
    name: Name = "Стена"
    start: Point2
    end: Point2
    thickness_mm: Positive = 150
    base_mm: Number = 0
    height_mm: Positive = 3000
    substrate: str = "unspecified"
    faces: list[WallFace] = Field(default_factory=lambda: [WallFace(side="left"), WallFace(side="right")])

    @model_validator(mode="after")
    def distinct_sides(self):
        if len(self.faces) != 2 or {face.side for face in self.faces} != {"left", "right"}:
            raise ValueError("Стена должна иметь две стороны: left и right")
        return self


class BoundaryEdge(Model):
    wall_id: Identifier
    reversed: bool = False


class Space(Entity):
    code: Name
    name: Name
    height_mm: Positive = 3000
    boundary: list[BoundaryEdge] = Field(min_length=3)
    # Boundary follows wall reference lines; never claim this is a finished-face area.
    boundary_basis: Literal["wall_reference_line"] = "wall_reference_line"


class DoorParameters(Model):
    leaf_width_mm: Positive | None = Field(default=None, description="Суммарная ширина всех полотен; для двухстворчатой двери это сумма, не размер одной створки")
    frame_width_mm: Positive | None = Field(default=None, description="Наружная общая ширина коробки, не ширина её стойки")
    handing: Literal["left", "right", "double", "unspecified"] = "unspecified"
    opening_side: Literal["left", "right", "unspecified"] = "unspecified"
    concealed_frame: bool = False

class Opening(Entity):
    name: Name = "Проём"
    wall_id: Identifier
    kind: Literal["door", "window", "passage", "hatch", "custom"]
    offset_mm: NonNegative
    width_mm: Positive
    sill_mm: NonNegative = 0
    height_mm: Positive
    reveal_finish: str = ""
    door: DoorParameters | None = None

    @model_validator(mode="after")
    def door_type(self):
        if self.door is not None and self.kind != "door":
            raise ValueError("Параметры двери допустимы только для проёма door")
        return self


class Obstacle(Entity):
    name: Name
    kind: Literal["column", "shaft", "niche", "projection", "equipment", "custom"]
    space_id: Identifier | None = None
    footprint: list[Point2] = Field(min_length=3)
    base_mm: Number = 0
    height_mm: Positive


class CladdingZone(Entity):
    name: Name
    wall_face_id: Identifier
    panel_system_id: Identifier
    offset_mm: NonNegative = 0
    width_mm: Positive
    base_mm: NonNegative = 0
    height_mm: Positive
    excluded_opening_ids: list[Identifier] = Field(default_factory=list)


class Floor(Entity):
    name: Name
    elevation_mm: Number = 0
    default_height_mm: Positive = 3000
    origin: Point2 = Field(default_factory=lambda: Point2(x=0, y=0))
    levels: list[LevelDatum] = Field(default_factory=list)
    walls: list[Wall] = Field(default_factory=list)
    spaces: list[Space] = Field(default_factory=list)
    openings: list[Opening] = Field(default_factory=list)
    obstacles: list[Obstacle] = Field(default_factory=list)
    cladding_zones: list[CladdingZone] = Field(default_factory=list)


class Building(Entity):
    name: Name
    floors: list[Floor] = Field(default_factory=list)


class PanelSystem(Entity):
    name: Name
    version: Name = "1"
    panel_thickness_mm: Positive
    backing_thickness_mm: NonNegative = 0
    joint_mm: NonNegative = 10
    standard_widths_mm: list[Positive] = Field(default_factory=list)
    maximum_height_mm: Positive | None = None
    minimum_cut_width_mm: Positive = 100
    profile_codes: list[str] = Field(default_factory=list)
    status: Literal["draft", "approved", "retired"] = "draft"


class Measurement(Entity):
    target_id: Identifier
    property_name: Literal["length_mm", "width_mm", "height_mm", "thickness_mm", "offset_mm", "elevation_mm", "sill_mm"]
    value_mm: Number
    source: Name
    author: str = ""
    measured_at: AwareDatetime = Field(default_factory=utc_now)
    tolerance_mm: NonNegative = 1
    status: Literal["raw", "verified", "accepted", "rejected", "superseded"] = "raw"
    comment: str = ""


class DesignDimension(Entity):
    target_id: Identifier
    property_name: Literal["length_mm", "width_mm", "height_mm", "thickness_mm", "offset_mm", "elevation_mm", "sill_mm"]
    value_mm: Number
    measurement_ids: list[Identifier] = Field(default_factory=list)
    accepted_by: Name
    accepted_at: AwareDatetime = Field(default_factory=utc_now)
    rationale: Name


class Project(Entity):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    units: Literal["mm"] = "mm"
    revision: Annotated[int, Field(strict=True, ge=0)] = 0
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: ProjectMetadata
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    buildings: list[Building] = Field(default_factory=list)
    panel_systems: list[PanelSystem] = Field(default_factory=list)
    measurements: list[Measurement] = Field(default_factory=list)
    design_dimensions: list[DesignDimension] = Field(default_factory=list)

    @model_validator(mode="after")
    def chronology(self):
        if self.updated_at < self.created_at:
            raise ValueError("updated_at не может быть раньше created_at")
        return self


class Issue(Model):
    code: str
    severity: Literal["error", "warning"]
    message: str
    path: str
    entity_ids: list[str] = Field(default_factory=list)
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ValidationReport(Model):
    valid: bool
    issues: list[Issue] = Field(default_factory=list)
    statistics: dict[str, JsonValue] = Field(default_factory=dict)
