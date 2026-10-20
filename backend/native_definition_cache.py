"""Per-build proof reuse for immutable, identity-located native definitions.

The caller must not mutate a cached TShape, its children, or their tolerances.
Room booleans use SetNonDestructive(True); changed geometry therefore has a new
native identity. This is not a cache for world occurrences, bounding boxes,
rounded coordinates, or geometrically similar parts. It never strips Location
or changes the caller's solid-count, placement, contact or collision checks.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class _BuildCache:
    definitions: dict = field(default_factory=dict)
    stats: dict = field(default_factory=lambda: {
        "validity_checks": 0, "validity_hits": 0,
        "volume_checks": 0, "volume_hits": 0,
    })


_DEFINITIONS = ContextVar("immutable_native_definitions", default=None)


@contextmanager
def native_definition_cache():
    """Create an isolated build cache and yield its local-definition counters.

    Nested builds get fresh caches. On exit (including exceptions), release
    native handles and restore the enclosing context. Outside this scope both
    helpers behave as the original native checks, without persistent caching.
    """
    cache = _BuildCache()
    token = _DEFINITIONS.set(cache)
    try:
        yield cache.stats
    finally:
        cache.definitions.clear()
        _DEFINITIONS.reset(token)


def _definition_key(shape, cache):
    if cache is None or shape.IsNull() or not shape.Location().IsIdentity():
        return None
    # TShape handle equality is exact native identity, not a Python wrapper's
    # identity or an approximate geometry hash. Orientation remains distinct.
    return shape.TShape(), shape.Orientation()


def definition_is_valid(shape, oc):
    """Reuse successful BRepCheck only for immutable identity definitions."""
    cache = _DEFINITIONS.get()
    key = _definition_key(shape, cache)
    if key is not None:
        if key in cache.definitions:
            cache.stats["validity_hits"] += 1
            return True
        cache.stats["validity_checks"] += 1
    valid = bool(oc["BRepCheck_Analyzer"](shape).IsValid())
    if valid and key is not None:
        cache.definitions[key] = {}
    return valid


class _ValidityAnalyzer:
    """The exact IsValid interface used by ag._mass, scoped to one call."""
    def __init__(self, shape, oc):
        self.shape, self.oc = shape, oc

    def IsValid(self):
        return definition_is_valid(self.shape, self.oc)


def definition_volume(shape, oc):
    """Reuse a volume only after ag._mass actually proved native validity.

    ag._mass remains the authority for empty/null shapes, solid enumeration,
    finite nonnegative volume and errors. A zero returned before its validity
    check must not turn an empty or invalid non-solid shape into cached proof.
    The analyzer adapter modifies only a private copy of ``oc``.
    """
    from . import assembly_geometry as ag

    cache = _DEFINITIONS.get()
    key = _definition_key(shape, cache)
    if key is None:
        return ag._mass(shape, oc)
    known = cache.definitions.get(key)
    if known is not None and "volume" in known:
        cache.stats["volume_hits"] += 1
        return known["volume"]
    cache.stats["volume_checks"] += 1
    checking_oc = {**oc, "BRepCheck_Analyzer": lambda value: _ValidityAnalyzer(value, oc)}
    volume = ag._mass(shape, checking_oc)
    # An invalid result raises or remains unproven; never create a successful
    # validity entry merely because _mass returned (especially for zero).
    known = cache.definitions.get(key)
    if known is not None:
        known["volume"] = volume
    return volume
