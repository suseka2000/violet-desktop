"""Server-owned exact node records without portable-album aggregate quotas.

Every individual record retains the portable STEP/point limits, identity,
source provenance and geometric evidence checks. Only the sum across distinct
nodes is unbounded. Client imports and HTTP build/prepare bodies do not select
this boundary; saved-project background jobs opt in explicitly in Python.
"""
from __future__ import annotations

from typing import Literal
from pydantic import Field, field_validator, model_validator

from .domain.models import Model
from .mount_assemblies import AssemblyCollection, AssemblyRecord


class ServerNodeCollection(Model):
    schema_version: Literal["mount-assemblies/1.0"] = "mount-assemblies/1.0"
    items: list[AssemblyRecord] = Field(default_factory=list)

    @field_validator("items", mode="before")
    @classmethod
    def individual_budgets(cls, values):
        if not isinstance(values, list):
            raise ValueError("Сборки должны быть списком")
        for value in values:
            raw = value.model_dump(mode="json", by_alias=True) if isinstance(value, AssemblyRecord) else value
            # Same raw allocation guard as a one-node portable import. This
            # does not pool unrelated detail points or STEP bytes together.
            AssemblyCollection.bounded({"items": [raw]})
        return values

    @model_validator(mode="after")
    def unique(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("Идентификаторы сборок не должны повторяться")
        return self


def validate_server_nodes(project):
    return ServerNodeCollection.model_validate(project.properties.get("mount_assemblies", {"items": []}))


def merge_server_node(collection, raw_record):
    """Validate one new worker result, retaining already checked records.

    Rechecking/decompressing the entire growing list after every node makes a
    building quadratic. The collection originates at the strict boundary above;
    neither it nor its records are mutated by this merge.
    """
    if not isinstance(collection, ServerNodeCollection):
        raise TypeError("Нужна проверенная серверная коллекция узлов")
    record = AssemblyCollection.model_validate({"items": [raw_record]}).items[0]
    return collection.model_copy(update={"items": [item for item in collection.items if item.id != record.id] + [record]})
