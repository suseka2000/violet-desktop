(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.VioletSnapping = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MAX_GUIDES = 200;
  const MAX_POINTS = 50_000;
  const MAX_SEGMENTS = 50_000;
  const MAX_INTERSECTION_PAIRS = 100_000;
  const PARALLEL_SINE_EPSILON = 1e-12;

  const KIND_RANK = Object.freeze({
    point: 0,
    corner: 0,
    wall_axis: 0,
    opening_edge: 0,
    midpoint: 0,
    endpoint: 1,
    intersection: 2,
    guide: 3,
    segment: 4,
    grid: 5,
  });

  function finiteNumber(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function finitePoint(value) {
    if (!value || !finiteNumber(value.x) || !finiteNumber(value.y)) return null;
    return { x: value.x, y: value.y };
  }

  function requiredPoint(value, name) {
    const point = finitePoint(value);
    if (!point) throw new TypeError(`${name} должен содержать конечные x и y.`);
    return point;
  }

  function cleanZero(value) {
    return Object.is(value, -0) ? 0 : value;
  }

  function cleanPoint(point) {
    return { x: cleanZero(point.x), y: cleanZero(point.y) };
  }

  function entityId(value) {
    return value && typeof value.id === "string" && value.id ? value.id : null;
  }

  function targetIds(values) {
    return [...new Set(values.filter((value) => typeof value === "string" && value))].sort();
  }

  function distance(first, second) {
    return Math.hypot(first.x - second.x, first.y - second.y);
  }

  function cross(firstX, firstY, secondX, secondY) {
    return firstX * secondY - firstY * secondX;
  }

  function lineFor(entity, finite) {
    if (!entity || typeof entity !== "object") return null;
    const start = finitePoint(entity.a || entity.start);
    const end = finitePoint(entity.b || entity.end);
    if (!start || !end) return null;
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const length = Math.hypot(dx, dy);
    if (!(length > 0) || !Number.isFinite(length)) return null;
    return {
      id: entityId(entity),
      start,
      end,
      ux: dx / length,
      uy: dy / length,
      length,
      finite,
    };
  }

  function projectedPoint(point, line) {
    const along = (point.x - line.start.x) * line.ux + (point.y - line.start.y) * line.uy;
    const parameter = line.finite ? Math.max(0, Math.min(line.length, along)) : along;
    const projected = {
      x: line.start.x + parameter * line.ux,
      y: line.start.y + parameter * line.uy,
    };
    return finitePoint(projected);
  }

  function parameterInside(parameter, length) {
    const slack = 128 * Number.EPSILON * Math.max(1, length, Math.abs(parameter));
    return parameter >= -slack && parameter <= length + slack;
  }

  function lineIntersection(first, second) {
    const denominator = cross(first.ux, first.uy, second.ux, second.uy);
    if (!Number.isFinite(denominator) || Math.abs(denominator) <= PARALLEL_SINE_EPSILON) {
      return null;
    }
    const dx = second.start.x - first.start.x;
    const dy = second.start.y - first.start.y;
    let firstParameter = cross(dx, dy, second.ux, second.uy) / denominator;
    let secondParameter = cross(dx, dy, first.ux, first.uy) / denominator;
    if (!Number.isFinite(firstParameter) || !Number.isFinite(secondParameter)) return null;
    if (first.finite && !parameterInside(firstParameter, first.length)) return null;
    if (second.finite && !parameterInside(secondParameter, second.length)) return null;
    if (first.finite) firstParameter = Math.max(0, Math.min(first.length, firstParameter));
    if (second.finite) secondParameter = Math.max(0, Math.min(second.length, secondParameter));
    const fromFirst = {
      x: first.start.x + firstParameter * first.ux,
      y: first.start.y + firstParameter * first.uy,
    };
    const fromSecond = {
      x: second.start.x + secondParameter * second.ux,
      y: second.start.y + secondParameter * second.uy,
    };
    const result = {
      x: (fromFirst.x + fromSecond.x) / 2,
      y: (fromFirst.y + fromSecond.y) / 2,
    };
    return finitePoint(result);
  }

  function candidate(point, kind, ids, query, tolerance, tier) {
    const valid = finitePoint(point);
    if (!valid) return null;
    const candidateDistance = distance(query, valid);
    const slack = 32 * Number.EPSILON * Math.max(1, tolerance, candidateDistance);
    if (!Number.isFinite(candidateDistance) || candidateDistance > tolerance + slack) return null;
    return {
      point: valid,
      kind,
      target_ids: targetIds(ids),
      distance: candidateDistance,
      tier,
      rank: KIND_RANK[kind],
    };
  }

  function compareCandidates(first, second) {
    if (first.tier !== second.tier) return first.tier - second.tier;
    const slack = 64 * Number.EPSILON * Math.max(1, first.distance, second.distance);
    if (first.distance < second.distance - slack) return -1;
    if (first.distance > second.distance + slack) return 1;
    if (first.rank !== second.rank) return first.rank - second.rank;
    const firstIds = first.target_ids.join("\u0000");
    const secondIds = second.target_ids.join("\u0000");
    if (firstIds !== secondIds) return firstIds < secondIds ? -1 : 1;
    if (first.point.x !== second.point.x) return first.point.x - second.point.x;
    return first.point.y - second.point.y;
  }

  function roundHalfAwayFromZero(value) {
    return value < 0 ? -Math.round(-value) : Math.round(value);
  }

  function gridCandidate(query, options, tolerance) {
    if (options.grid_enabled !== true) return null;
    const step = options.grid_mm;
    if (!finiteNumber(step) || !(step > 0)) {
      throw new RangeError("grid_mm должен быть конечным числом больше нуля.");
    }
    const origin = options.grid_origin === undefined
      ? { x: 0, y: 0 }
      : requiredPoint(options.grid_origin, "grid_origin");
    const xIndex = roundHalfAwayFromZero((query.x - origin.x) / step);
    const yIndex = roundHalfAwayFromZero((query.y - origin.y) / step);
    return candidate({
      x: origin.x + xIndex * step,
      y: origin.y + yIndex * step,
    }, "grid", [], query, tolerance, 2);
  }

  /**
   * Snap a world-coordinate point without applying any implicit tolerance.
   *
   * Discrete object snaps (points, segment endpoints and intersections) are
   * the first tier.  Infinite-guide and finite-segment projections are the
   * second tier.  The grid is a fallback and never masks an object snap.
   */
  function snapPoint(value, options) {
    const query = requiredPoint(value, "point");
    const settings = options && typeof options === "object" ? options : {};
    const tolerance = settings.tolerance_mm === undefined ? 0 : settings.tolerance_mm;
    if (!finiteNumber(tolerance) || tolerance < 0) {
      throw new RangeError("tolerance_mm должен быть конечным неотрицательным числом.");
    }
    const points = Array.isArray(settings.points) ? settings.points : [];
    const segments = Array.isArray(settings.segments) ? settings.segments : [];
    const guides = Array.isArray(settings.guides) ? settings.guides : [];
    if (points.length > MAX_POINTS) throw new RangeError(`Допустимо не более ${MAX_POINTS} точек привязки.`);
    if (segments.length > MAX_SEGMENTS) throw new RangeError(`Допустимо не более ${MAX_SEGMENTS} отрезков.`);
    if (guides.length > MAX_GUIDES) throw new RangeError(`Допустимо не более ${MAX_GUIDES} направляющих.`);

    const excluded = new Set(
      (Array.isArray(settings.exclude_ids) ? settings.exclude_ids : [])
        .filter((id) => typeof id === "string" && id),
    );
    const candidates = [];
    const add = (valueToAdd) => { if (valueToAdd) candidates.push(valueToAdd); };

    for (const item of points) {
      const id = entityId(item);
      if ((id && excluded.has(id)) || (Array.isArray(item?.owner_ids) && item.owner_ids.some(owner => excluded.has(owner)))) continue;
      const point = finitePoint(item && (item.point || item));
      const kind = ["corner", "wall_axis", "opening_edge", "midpoint"].includes(item?.kind) ? item.kind : "point";
      if (point) add(candidate(point, kind, [id], query, tolerance, 0));
    }

    const activeSegments = [];
    for (const item of segments) {
      const id = entityId(item);
      if (id && excluded.has(id)) continue;
      const start = finitePoint(item && item.start);
      const end = finitePoint(item && item.end);
      if (start) add(candidate(start, "endpoint", [id], query, tolerance, 0));
      if (end) add(candidate(end, "endpoint", [id], query, tolerance, 0));
      const line = lineFor(item, true);
      if (!line) continue;
      const projection = projectedPoint(query, line);
      const projected = candidate(projection, "segment", [id], query, tolerance, 1);
      if (projected) {
        candidates.push(projected);
        activeSegments.push(line);
      }
    }

    const activeGuides = [];
    for (const item of guides) {
      const id = entityId(item);
      if (id && excluded.has(id)) continue;
      const line = lineFor(item, false);
      if (!line) continue;
      const projection = projectedPoint(query, line);
      const projected = candidate(projection, "guide", [id], query, tolerance, 1);
      if (projected) {
        candidates.push(projected);
        activeGuides.push(line);
      }
    }

    const activeLines = activeGuides.concat(activeSegments);
    let pairCount = 0;
    for (let firstIndex = 0; firstIndex < activeLines.length; firstIndex += 1) {
      for (let secondIndex = firstIndex + 1; secondIndex < activeLines.length; secondIndex += 1) {
        pairCount += 1;
        if (pairCount > MAX_INTERSECTION_PAIRS) {
          throw new RangeError(`Локальная проверка пересечений превышает предел ${MAX_INTERSECTION_PAIRS}.`);
        }
        const first = activeLines[firstIndex];
        const second = activeLines[secondIndex];
        if (first.id && first.id === second.id) continue;
        add(candidate(
          lineIntersection(first, second),
          "intersection",
          [first.id, second.id],
          query,
          tolerance,
          0,
        ));
      }
    }

    add(gridCandidate(query, settings, tolerance));
    if (!candidates.length) {
      return { point: cleanPoint(query), snapped: false, kind: "none", target_ids: [] };
    }
    candidates.sort(compareCandidates);
    const selected = candidates[0];
    return {
      point: cleanPoint(selected.point),
      snapped: true,
      kind: selected.kind,
      target_ids: selected.target_ids,
    };
  }

  function worldTolerance(screenPx, pixelsPerMm) {
    if (!finiteNumber(screenPx) || screenPx < 0) {
      throw new RangeError("screenPx должен быть конечным неотрицательным числом.");
    }
    if (!finiteNumber(pixelsPerMm) || !(pixelsPerMm > 0)) {
      throw new RangeError("pixelsPerMm должен быть конечным числом больше нуля.");
    }
    const result = screenPx / pixelsPerMm;
    if (!Number.isFinite(result)) throw new RangeError("Мировой допуск привязки вышел за допустимый диапазон.");
    return result;
  }

  /** Explicit Shift-style constraint: no distance or angular tolerance. */
  function orthogonalPoint(anchorValue, cursorValue) {
    const anchor = requiredPoint(anchorValue, "anchor");
    const cursor = requiredPoint(cursorValue, "cursor");
    const dx = cursor.x - anchor.x;
    const dy = cursor.y - anchor.y;
    if (Math.abs(dx) >= Math.abs(dy)) {
      return { point: cleanPoint({ x: cursor.x, y: anchor.y }), axis: "horizontal" };
    }
    return { point: cleanPoint({ x: anchor.x, y: cursor.y }), axis: "vertical" };
  }

  return Object.freeze({
    snapPoint,
    worldTolerance,
    orthogonalPoint,
    limits: Object.freeze({
      max_guides: MAX_GUIDES,
      max_points: MAX_POINTS,
      max_segments: MAX_SEGMENTS,
      max_intersection_pairs: MAX_INTERSECTION_PAIRS,
    }),
  });
});
