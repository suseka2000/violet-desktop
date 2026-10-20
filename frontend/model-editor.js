(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.VioletModel = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function clone(value) {
    if (value && Array.isArray(value.buildings) && value.properties && globalThis.VioletProjectHistory) {
      return globalThis.VioletProjectHistory.clone(value);
    }
    if (typeof structuredClone === "function") return structuredClone(value);
    return JSON.parse(JSON.stringify(value));
  }

  function id() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    return `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  }

  function arrays(value) {
    return Array.isArray(value) ? value : [];
  }

  function floorEntries(project) {
    const entries = [];
    arrays(project && project.buildings).forEach((building, buildingIndex) => {
      arrays(building.floors).forEach((floor, floorIndex) => {
        entries.push({ building, floor, buildingIndex, floorIndex });
      });
    });
    return entries;
  }

  function findFloor(project, floorId) {
    return floorEntries(project).find((entry) => entry.floor.id === floorId) || null;
  }

  function floorCreationBuildings(project) {
    if (!project || typeof project !== "object" || Array.isArray(project)) {
      throw new TypeError("Проект не найден.");
    }
    if (project.buildings !== undefined && !Array.isArray(project.buildings)) {
      throw new TypeError("Список зданий проекта повреждён.");
    }
    return arrays(project.buildings);
  }

  function floorCreationBuilding(buildings, buildingId) {
    const matches = buildings.filter((building) => building && building.id === buildingId);
    if (matches.length !== 1) throw new Error("Здание не найдено. Выберите здание для нового этажа.");
    const building = matches[0];
    if (building.floors !== undefined && !Array.isArray(building.floors)) {
      throw new TypeError("Список этажей здания повреждён.");
    }
    return building;
  }

  function floorCreationName(value, label) {
    if (typeof value !== "string" || !value.trim()) throw new TypeError(`${label}: укажите название.`);
    const name = value.trim();
    if (Array.from(name).length > 300) throw new RangeError(`${label}: не более 300 символов.`);
    return name;
  }

  function floorCreationDefaults(project, preferredBuildingId) {
    const buildings = floorCreationBuildings(project);
    const hasPreferred = preferredBuildingId !== undefined && preferredBuildingId !== null && preferredBuildingId !== "";
    const building = hasPreferred
      ? floorCreationBuilding(buildings, preferredBuildingId)
      : buildings.length === 1 ? floorCreationBuilding(buildings, buildings[0]?.id) : null;
    const floors = arrays(building && building.floors);
    const names = new Set(floors.map((floor) => String(floor.name || "").trim().toLocaleLowerCase("ru")));
    let index = 1;
    while (names.has(`этаж ${index}`)) index += 1;
    let elevation = 0;
    let height = 3000;
    if (floors.length) {
      let highest = null;
      for (const floor of floors) {
        const value = modelNumber(floor.elevation_mm ?? 0, "Отметка существующего этажа");
        if (highest === null || value > highest.elevation) highest = { floor, elevation: value };
      }
      height = modelNumber(highest.floor.default_height_mm ?? 3000, "Высота существующего этажа", 0, true);
      const proposed = highest.elevation + height;
      // An existing floor may legitimately be at the domain boundary. Keep
      // creation available, but require a manual elevation instead of clipping.
      elevation = proposed > 1e9 ? null : proposed;
    }
    return {
      building_id: building ? building.id : null,
      name: `Этаж ${index}`,
      elevation_mm: elevation,
      default_height_mm: height,
      creates_building: buildings.length === 0,
    };
  }

  function addFloor(project, options) {
    const buildings = floorCreationBuildings(project);
    if (!options || typeof options !== "object" || Array.isArray(options)) {
      throw new TypeError("Укажите параметры нового этажа.");
    }
    const hasBuilding = options.building_id !== undefined && options.building_id !== null && options.building_id !== "";
    if (buildings.length && !hasBuilding) throw new Error("Выберите здание для нового этажа.");
    const existingBuilding = hasBuilding ? floorCreationBuilding(buildings, options.building_id) : null;
    const name = floorCreationName(options.name, "Название этажа");
    const elevation = modelNumber(options.elevation_mm, "Отметка этажа");
    const height = modelNumber(options.default_height_mm, "Высота этажа", 0, true);
    const buildingName = existingBuilding ? null : floorCreationName(options.building_name ?? "Здание", "Название здания");

    // Validate every input before creating or attaching any entity. A failed
    // first-floor form must not leave an empty building behind in the project.
    const floor = {
      id: id(), properties: {}, name, elevation_mm: elevation, default_height_mm: height,
      origin: { x: 0, y: 0 }, levels: [], walls: [], spaces: [], openings: [], obstacles: [], cladding_zones: [],
    };
    const building = existingBuilding || { id: id(), properties: {}, name: buildingName, floors: [] };
    if (existingBuilding) {
      if (building.floors) building.floors.push(floor);
      else building.floors = [floor];
    } else {
      building.floors.push(floor);
      if (project.buildings) project.buildings.push(building);
      else project.buildings = [building];
    }
    return { building, floor };
  }

  function allEntities(project) {
    const entities = [];
    arrays(project && project.buildings).forEach((building, buildingIndex) => {
      entities.push({ type: "building", entity: building, buildingIndex });
      arrays(building.floors).forEach((floor, floorIndex) => {
        const base = { building, floor, buildingIndex, floorIndex };
        entities.push({ type: "floor", entity: floor, ...base });
        [
          ["level", "levels"], ["wall", "walls"], ["room", "spaces"],
          ["opening", "openings"], ["obstacle", "obstacles"], ["zone", "cladding_zones"],
        ].forEach(([type, key]) => {
          arrays(floor[key]).forEach((entity, index) => entities.push({ type, entity, index, key, ...base }));
        });
      });
    });
    arrays(project && project.panel_systems).forEach((entity, index) => entities.push({ type: "panelSystem", entity, index }));
    arrays(project && project.measurements).forEach((entity, index) => entities.push({ type: "measurement", entity, index }));
    arrays(project && project.design_dimensions).forEach((entity, index) => entities.push({ type: "designDimension", entity, index }));
    return entities;
  }

  function findEntity(project, entityId, expectedType) {
    if (!entityId) return null;
    return allEntities(project).find((item) => item.entity.id === entityId && (!expectedType || item.type === expectedType)) || null;
  }

  function signedArea(points) {
    if (!Array.isArray(points) || points.length < 3) return 0;
    // Translate before the shoelace sum so small rooms at survey-scale
    // coordinates do not lose their area through cancellation.
    const originX = Number(points[0].x);
    const originY = Number(points[0].y);
    let twice = 0;
    for (let index = 0; index < points.length; index += 1) {
      const point = points[index];
      const next = points[(index + 1) % points.length];
      const x1 = Number(point.x) - originX;
      const y1 = Number(point.y) - originY;
      const x2 = Number(next.x) - originX;
      const y2 = Number(next.y) - originY;
      twice += x1 * y2 - x2 * y1;
    }
    return twice / 2;
  }

  function area(points) {
    return Math.abs(signedArea(points));
  }

  function perimeter(points) {
    let total = 0;
    for (let index = 0; index < points.length; index += 1) {
      const point = points[index];
      const next = points[(index + 1) % points.length];
      total += Math.hypot(Number(next.x) - Number(point.x), Number(next.y) - Number(point.y));
    }
    return total;
  }

  function ccw(points) {
    const result = points.map((point) => ({ x: Number(point.x), y: Number(point.y) }));
    return signedArea(result) < 0 ? result.reverse() : result;
  }

  function roomPolygon(floor, room) {
    const walls = new Map(arrays(floor && floor.walls).map((wall) => [wall.id, wall]));
    const result = [];
    for (const edge of arrays(room && room.boundary)) {
      const wall = walls.get(edge.wall_id);
      if (!wall) return null;
      result.push(clone(edge.reversed ? wall.end : wall.start));
    }
    return result;
  }

  function newWallFace(side, roomId) {
    return {
      id: id(),
      properties: {},
      side,
      space_id: side === "left" ? roomId : null,
      finish: "",
    };
  }

  function pointsWithin(first, second, tolerance) {
    return Math.hypot(Number(first.x) - Number(second.x), Number(first.y) - Number(second.y)) <= tolerance;
  }

  function fullEdgeMatch(wall, start, end, tolerance) {
    if (pointsWithin(wall.start, start, tolerance) && pointsWithin(wall.end, end, tolerance)) return { reversed: false, side: "left" };
    if (pointsWithin(wall.start, end, tolerance) && pointsWithin(wall.end, start, tolerance)) return { reversed: true, side: "right" };
    return null;
  }

  function collinearOverlapLength(wall, start, end, tolerance) {
    const dx = Number(end.x) - Number(start.x);
    const dy = Number(end.y) - Number(start.y);
    const length = Math.hypot(dx, dy);
    const wallDx = Number(wall.end.x) - Number(wall.start.x);
    const wallDy = Number(wall.end.y) - Number(wall.start.y);
    const wallLengthValue = Math.hypot(wallDx, wallDy);
    if (!(length > 0) || !(wallLengthValue > 0)) return 0;
    const ux = dx / length;
    const uy = dy / length;
    const wallUx = wallDx / wallLengthValue;
    const wallUy = wallDy / wallLengthValue;
    const sine = Math.abs(ux * wallUy - uy * wallUx);
    const angularSlack = Math.max(64 * Number.EPSILON, tolerance / Math.max(1, length, wallLengthValue));
    if (sine > angularSlack) return 0;
    const perpendicular = (point) => Math.abs((Number(point.x) - Number(start.x)) * uy - (Number(point.y) - Number(start.y)) * ux);
    if (perpendicular(wall.start) > tolerance || perpendicular(wall.end) > tolerance) return 0;
    const project = (point) => (Number(point.x) - Number(start.x)) * ux + (Number(point.y) - Number(start.y)) * uy;
    const first = project(wall.start);
    const second = project(wall.end);
    return Math.max(0, Math.min(length, Math.max(first, second)) - Math.max(0, Math.min(first, second)));
  }

  function createPolygonRoom(project, floorId, options, inputPoints) {
    const entry = findFloor(project, floorId);
    if (!entry) throw new Error("Этаж не найден.");
    if (!Array.isArray(inputPoints) || inputPoints.length < 3) {
      throw new Error("Контур должен содержать не менее трёх точек.");
    }
    if (inputPoints.length > 2000 || inputPoints.length * arrays(entry.floor.walls).length > 2000000) {
      throw new Error("Слишком сложный контур для автоматического согласования стен. Упростите контур или разделите модель по этажам.");
    }
    const points = ccw(inputPoints.map((point, index) => modelPoint(point, `Вершина ${index + 1}`)));
    const working = clone(project);
    const floor = findFloor(working, floorId).floor;
    const tolerance = geometryTolerance(working);
    const edges = points.map((start, index) => ({ start, end: points[(index + 1) % points.length] }));

    // A shorter adjacent room can occupy only part of a long existing wall.
    // Segment that wall first, using the same guarded migration as manual cuts.
    for (const wall of [...arrays(floor.walls)]) {
      const cuts = [];
      for (const edge of edges) {
        if (collinearOverlapLength(wall, edge.start, edge.end, tolerance) <= tolerance) continue;
        for (const point of [edge.start, edge.end]) {
          const hit = projectOnWall(wall, point);
          if (hit.distance_mm <= tolerance && hit.offset > tolerance && hit.offset < hit.length - tolerance) cuts.push(hit.offset);
        }
      }
      if (cuts.length) splitWallInPlace(working, floor, wall.id, cuts);
    }

    // Conversely, a long incoming edge may span several existing pieces.
    // Insert their endpoints in traversal order so exact reuse works on every
    // piece, including boundaries referenced in reverse by the adjacent room.
    const expanded = [];
    for (const edge of edges) {
      const projection = projectOnWall(edge, edge.start);
      const interior = [];
      for (const wall of arrays(floor.walls)) {
        if (collinearOverlapLength(wall, edge.start, edge.end, tolerance) <= tolerance) continue;
        for (const point of [wall.start, wall.end]) {
          const hit = projectOnWall(edge, point);
          if (hit.distance_mm <= tolerance && hit.offset > tolerance && hit.offset < projection.length - tolerance) interior.push(hit);
        }
      }
      interior.sort((a, b) => a.offset - b.offset);
      expanded.push(clone(edge.start));
      let previous = 0;
      for (const hit of interior) {
        if (hit.offset - previous <= tolerance) continue;
        expanded.push(hit.point);
        previous = hit.offset;
      }
      if (expanded.length > 2000) throw new Error("После согласования контур содержит более 2000 рёбер. Упростите исходные оси стен.");
    }
    const room = createPolygonRoomInPlace(working, floorId, options, expanded);
    const committedFloor = commitFloorGeometry(project, floorId, working);
    return committedFloor.spaces.find((item) => item.id === room.id);
  }

  function createPolygonRoomInPlace(project, floorId, options, inputPoints) {
    const entry = findFloor(project, floorId);
    if (!entry) throw new Error("Этаж не найден.");
    if (!Array.isArray(inputPoints) || inputPoints.length < 3) {
      throw new Error("Контур должен содержать не менее трёх точек.");
    }
    const points = ccw(inputPoints.map((point, index) => modelPoint(point, `Вершина ${index + 1}`)));
    const height = modelNumber(options.height_mm ?? entry.floor.default_height_mm ?? 3000, "Высота помещения", 0, true);
    const thickness = modelNumber(options.thickness_mm ?? 150, "Толщина стен", 0, true);
    const roomCode = String(options.code || "").trim();
    const roomName = String(options.name || "").trim();
    if (!roomCode || !roomName) throw new Error("Код и название помещения обязательны.");
    const configuredTolerance = Number(project && project.settings && project.settings.geometry_tolerance_mm);
    const tolerance = Number.isFinite(configuredTolerance) && configuredTolerance >= 0 ? configuredTolerance : 0.1;
    if (area(points) <= tolerance * tolerance) throw new Error("Контур вырожден: вершины должны образовывать помещение с ненулевой площадью.");
    const segments = points.map((start, index) => ({ start, end: points[(index + 1) % points.length] }));
    for (let first = 0; first < segments.length; first += 1) {
      for (let second = first + 1; second < segments.length; second += 1) {
        const adjacent = second === first + 1 || (first === 0 && second === segments.length - 1);
        if (partitionOverlapsWall(segments[first], segments[second], tolerance)
          || (!adjacent && segmentsTouch(segments[first], segments[second], tolerance))) {
          throw new Error(`Контур пересекает или касается сам себя: проверьте рёбра ${first + 1} и ${second + 1}.`);
        }
      }
    }
    const roomId = id();
    const existingWalls = arrays(entry.floor.walls);
    const newWalls = [];
    const reusedFaces = [];
    const boundary = [];
    points.forEach((start, index) => {
      const end = points[(index + 1) % points.length];
      if (!(Math.hypot(end.x - start.x, end.y - start.y) > tolerance)) {
        throw new Error(`Ребро ${index + 1} короче геометрического допуска.`);
      }
      const matches = existingWalls.map((wall) => ({ wall, match: fullEdgeMatch(wall, start, end, tolerance) })).filter((item) => item.match);
      if (matches.length > 1) throw new Error(`Ребро ${index + 1} совпадает с несколькими стенами; сначала устраните дубликаты.`);
      if (matches.length === 1) {
        const { wall, match } = matches[0];
        const dimensionEpsilon = 1e-8;
        if (Math.abs(Number(wall.base_mm)) > dimensionEpsilon
          || Math.abs(Number(wall.thickness_mm) - thickness) > dimensionEpsilon
          || Math.abs(Number(wall.height_mm) - height) > dimensionEpsilon) {
          throw new Error(`Ребро ${index + 1} совпадает со стеной, но её низ, толщина или высота отличается.`);
        }
        const face = arrays(wall.faces).find((item) => item.side === match.side);
        if (!face) throw new Error(`У совпадающей стены нет стороны ${match.side}.`);
        if (face.space_id !== null && face.space_id !== undefined) {
          throw new Error(`Сторона совпадающей стены для ребра ${index + 1} уже занята помещением.`);
        }
        guardSpecialInstallationChange(project, item => item.face_id === face.id, "назначить стороне стены новое помещение");
        reusedFaces.push(face);
        boundary.push({ wall_id: wall.id, reversed: match.reversed });
        return;
      }
      const partial = existingWalls.some((wall) => collinearOverlapLength(wall, start, end, tolerance) > tolerance);
      if (partial) throw new Error(`Ребро ${index + 1} частично перекрывает существующую стену: не удалось согласовать участки. Проверьте совпадение осей и дубликаты стен.`);
      const wall = {
        id: id(),
        properties: {},
        name: `${roomCode} / ${index + 1}`,
        start: clone(start),
        end: clone(end),
        thickness_mm: thickness,
        base_mm: 0,
        height_mm: height,
        substrate: "unspecified",
        faces: [newWallFace("left", roomId), newWallFace("right", null)],
      };
      newWalls.push(wall);
      boundary.push({ wall_id: wall.id, reversed: false });
    });
    const room = {
      id: roomId,
      properties: {},
      code: roomCode,
      name: roomName,
      height_mm: height,
      boundary,
      boundary_basis: "wall_reference_line",
    };
    entry.floor.walls.push(...newWalls);
    reusedFaces.forEach((face) => { face.space_id = roomId; });
    entry.floor.spaces.push(room);
    return room;
  }

  function wallLength(wall) {
    return Math.hypot(Number(wall.end.x) - Number(wall.start.x), Number(wall.end.y) - Number(wall.start.y));
  }

  function finitePoint(value) {
    return Boolean(value && Number.isFinite(Number(value.x)) && Number.isFinite(Number(value.y)));
  }

  function pointSegmentDistance(point, start, end) {
    const dx = Number(end.x) - Number(start.x);
    const dy = Number(end.y) - Number(start.y);
    const lengthSquared = dx * dx + dy * dy;
    if (!(lengthSquared > 0) || !Number.isFinite(lengthSquared)) {
      return Math.hypot(Number(point.x) - Number(start.x), Number(point.y) - Number(start.y));
    }
    const along = ((Number(point.x) - Number(start.x)) * dx + (Number(point.y) - Number(start.y)) * dy) / lengthSquared;
    const parameter = Math.max(0, Math.min(1, along));
    return Math.hypot(
      Number(point.x) - (Number(start.x) + parameter * dx),
      Number(point.y) - (Number(start.y) + parameter * dy),
    );
  }

  function segmentsIntersect(first, second) {
    const firstDx = Number(first.end.x) - Number(first.start.x);
    const firstDy = Number(first.end.y) - Number(first.start.y);
    const secondDx = Number(second.end.x) - Number(second.start.x);
    const secondDy = Number(second.end.y) - Number(second.start.y);
    const firstLength = Math.hypot(firstDx, firstDy);
    const secondLength = Math.hypot(secondDx, secondDy);
    if (!(firstLength > 0) || !(secondLength > 0)) return false;
    const firstUx = firstDx / firstLength;
    const firstUy = firstDy / firstLength;
    const secondUx = secondDx / secondLength;
    const secondUy = secondDy / secondLength;
    const denominator = firstUx * secondUy - firstUy * secondUx;
    if (!Number.isFinite(denominator) || Math.abs(denominator) <= 64 * Number.EPSILON) return false;
    const originDx = Number(second.start.x) - Number(first.start.x);
    const originDy = Number(second.start.y) - Number(first.start.y);
    const firstParameter = (originDx * secondUy - originDy * secondUx) / denominator;
    const secondParameter = (originDx * firstUy - originDy * firstUx) / denominator;
    const slack = 128 * Number.EPSILON * Math.max(1, firstLength, secondLength, Math.abs(firstParameter), Math.abs(secondParameter));
    return firstParameter >= -slack && firstParameter <= firstLength + slack
      && secondParameter >= -slack && secondParameter <= secondLength + slack;
  }

  function segmentBoundsOverlap(first, second, tolerance) {
    const firstMinX = Math.min(Number(first.start.x), Number(first.end.x));
    const firstMaxX = Math.max(Number(first.start.x), Number(first.end.x));
    const firstMinY = Math.min(Number(first.start.y), Number(first.end.y));
    const firstMaxY = Math.max(Number(first.start.y), Number(first.end.y));
    const secondMinX = Math.min(Number(second.start.x), Number(second.end.x));
    const secondMaxX = Math.max(Number(second.start.x), Number(second.end.x));
    const secondMinY = Math.min(Number(second.start.y), Number(second.end.y));
    const secondMaxY = Math.max(Number(second.start.y), Number(second.end.y));
    return firstMinX <= secondMaxX + tolerance && firstMaxX + tolerance >= secondMinX
      && firstMinY <= secondMaxY + tolerance && firstMaxY + tolerance >= secondMinY;
  }

  function segmentsTouch(first, second, tolerance) {
    if (!segmentBoundsOverlap(first, second, tolerance)) return false;
    if (segmentsIntersect(first, second)) return true;
    const minimum = Math.min(
      pointSegmentDistance(first.start, second.start, second.end),
      pointSegmentDistance(first.end, second.start, second.end),
      pointSegmentDistance(second.start, first.start, first.end),
      pointSegmentDistance(second.end, first.start, first.end),
    );
    const slack = 128 * Number.EPSILON * Math.max(1, tolerance, minimum);
    return minimum <= tolerance + slack;
  }

  function modelNumber(value, label, minimum = -1e9, strictMinimum = false) {
    if ((typeof value !== "number" && typeof value !== "string") || (typeof value === "string" && !value.trim())) {
      throw new TypeError(`${label}: требуется конечное число.`);
    }
    const number = Number(value);
    if (!Number.isFinite(number) || number > 1e9 || number < minimum || (strictMinimum && number === minimum)) {
      throw new RangeError(`${label}: число вне допустимого диапазона${minimum === 0 ? " (должно быть " + (strictMinimum ? "больше нуля" : "неотрицательным") + ")" : ""}.`);
    }
    return number;
  }

  function modelPoint(point, label) {
    if (!point || typeof point !== "object") throw new TypeError(`${label}: укажите координаты X и Y.`);
    return { x: modelNumber(point.x, `${label}, X`), y: modelNumber(point.y, `${label}, Y`) };
  }

  function geometryTolerance(project) {
    const value = project && project.settings && project.settings.geometry_tolerance_mm;
    return value === undefined ? 0.1 : modelNumber(value, "Геометрический допуск", 0, true);
  }

  function projectOnWall(wall, point) {
    const start = modelPoint(wall.start, "Начало стены");
    const end = modelPoint(wall.end, "Конец стены");
    const length = Math.hypot(end.x - start.x, end.y - start.y);
    if (!(length > 1e-8)) throw new Error("Стена имеет нулевую длину.");
    const ux = (end.x - start.x) / length;
    const uy = (end.y - start.y) / length;
    const along = (point.x - start.x) * ux + (point.y - start.y) * uy;
    const offset = Math.max(0, Math.min(length, along));
    const projected = { x: start.x + ux * offset, y: start.y + uy * offset };
    return { start, end, length, ux, uy, along, offset, point: projected, distance_mm: Math.hypot(projected.x - point.x, projected.y - point.y) };
  }

  function snappedPartitionPoint(point, walls, tolerance) {
    let best = { point, distance: Infinity, priority: Infinity };
    for (const wall of walls) {
      const projected = projectOnWall(wall, point);
      const candidates = [
        { point: projected.start, priority: 0 },
        { point: projected.end, priority: 0 },
        { point: projected.point, priority: 1 },
      ];
      for (const candidate of candidates) {
        const distance = Math.hypot(candidate.point.x - point.x, candidate.point.y - point.y);
        if (distance <= tolerance && (candidate.priority < best.priority
          || (candidate.priority === best.priority && distance < best.distance))) {
          best = { point: candidate.point, priority: candidate.priority, distance };
        }
      }
    }
    return clone(best.point);
  }

  function partitionOverlapsWall(first, second, tolerance) {
    const ax = first.end.x - first.start.x;
    const ay = first.end.y - first.start.y;
    const bx = Number(second.end.x) - Number(second.start.x);
    const by = Number(second.end.y) - Number(second.start.y);
    const lengthA = Math.hypot(ax, ay);
    const lengthB = Math.hypot(bx, by);
    if (lengthA <= tolerance || lengthB <= tolerance) return false;
    if (Math.abs(ax * by - ay * bx) > tolerance * Math.max(lengthA, lengthB, 1)) return false;
    const lineDistance = (point, start, dx, dy, length) => Math.abs(dx * (Number(point.y) - Number(start.y)) - dy * (Number(point.x) - Number(start.x))) / length;
    if (lineDistance(second.start, first.start, ax, ay, lengthA) > tolerance
      || lineDistance(second.end, first.start, ax, ay, lengthA) > tolerance
      || lineDistance(first.start, second.start, bx, by, lengthB) > tolerance
      || lineDistance(first.end, second.start, bx, by, lengthB) > tolerance) return false;
    const along = (point) => ((Number(point.x) - first.start.x) * ax + (Number(point.y) - first.start.y) * ay) / lengthA;
    const startB = along(second.start);
    const endB = along(second.end);
    return Math.min(lengthA, Math.max(startB, endB)) - Math.max(0, Math.min(startB, endB)) > tolerance;
  }

  // Preserve object identities used by inspectors while committing only after
  // every geometric and reference migration has succeeded on an isolated copy.
  function commitFloorGeometry(project, floorId, working) {
    const original = findFloor(project, floorId).floor;
    const changed = findFloor(working, floorId).floor;
    const reconcile = (before, after) => {
      const byId = new Map(arrays(before).map((item) => [item.id, item]));
      return arrays(after).map((item) => {
        const existing = byId.get(item.id);
        if (!existing) return item;
        if (item.faces) item.faces = reconcile(existing.faces, item.faces);
        Object.assign(existing, item);
        return existing;
      });
    };
    for (const key of ["walls", "spaces", "openings", "cladding_zones"]) {
      original[key] = reconcile(original[key], changed[key]);
    }
    if (working.properties?.wall_mounting) {
      project.properties ||= {};
      project.properties.wall_mounting = working.properties.wall_mounting;
    }
    return original;
  }

  function guardDimensionChange(project, entityIds, propertyNames, label, action = "разделить") {
    const ids = new Set(entityIds);
    const properties = new Set(propertyNames);
    const reference = [...arrays(project.measurements), ...arrays(project.design_dimensions)]
      .find((item) => ids.has(item.target_id) && properties.has(item.property_name));
    if (reference) {
      throw new Error(`Нельзя ${action} ${label}: изменится размер, связанный с обмером или принятым проектным значением. Сначала пересмотрите связанные размеры; исходные данные сохранены.`);
    }
  }

  // Installation identities include their wall/face/opening IDs and are hashed
  // by the server. Do not invent replacement identities during topology edits.
  function specialItems(project, key) {
    const value = project.properties?.[key];
    if (value === undefined) return [];
    if (!value || typeof value !== "object" || !Array.isArray(value.items)) {
      throw new Error("Список специальных изделий некорректен. Сначала проверьте его в окне «Двери, люки и подсветка»; исходные данные сохранены.");
    }
    return value.items;
  }

  function guardSpecialInstallationChange(project, predicate, action) {
    const affected = specialItems(project, "special_installations").find(predicate);
    if (affected) throw new Error(`Нельзя ${action}: здесь назначено изделие «${affected.label || "Дверь, люк или подсветка"}». Сначала уберите его в окне «Двери, люки и подсветка», измените геометрию и соберите изделие заново. Настройки сохранены.`);
  }

  function splitWallInPlace(project, floor, wallId, requestedCuts) {
    const wall = arrays(floor.walls).find((item) => item.id === wallId);
    if (!wall || idOccurrences(project, wallId) !== 1) throw new Error("Стена для деления не найдена или её ID неоднозначен.");
    const tolerance = geometryTolerance(project);
    const projection = projectOnWall(wall, modelPoint(wall.start, "Начало стены"));
    const cuts = [];
    for (const value of requestedCuts) {
      let offset;
      if (value && typeof value === "object") {
        const hit = projectOnWall(wall, modelPoint(value, "Точка деления"));
        if (hit.distance_mm > tolerance || hit.along < -tolerance || hit.along > projection.length + tolerance) {
          throw new Error("Точка деления должна лежать на выбранной стене.");
        }
        offset = hit.offset;
      } else {
        offset = modelNumber(value, "Отступ точки деления", 0);
        if (offset > projection.length + tolerance) throw new Error("Точка деления находится за концом стены.");
      }
      // End contacts and near-duplicate cuts do not create zero-length walls.
      if (offset > tolerance && offset < projection.length - tolerance) cuts.push(offset);
    }
    cuts.sort((a, b) => a - b);
    const uniqueCuts = [];
    cuts.forEach((value) => {
      if (!uniqueCuts.length || value - uniqueCuts[uniqueCuts.length - 1] > tolerance) uniqueCuts.push(value);
    });
    if (!uniqueCuts.length) return { original_wall_id: wallId, segment_ids: [wallId] };
    guardSpecialInstallationChange(project, item => item.wall_id === wall.id, "разделить стену");
    if (arrays(wall.faces).length !== 2 || new Set(wall.faces.map((face) => face.side)).size !== 2
      || wall.faces.some((face) => !["left", "right"].includes(face.side) || idOccurrences(project, face.id) !== 1)) {
      throw new Error("Нельзя разделить стену: её стороны отсутствуют или имеют неоднозначные ID.");
    }
    guardDimensionChange(project, [wall.id, ...wall.faces.map((face) => face.id)], ["length_mm"], `стену «${wall.name}»`);
    const offsets = [0, ...uniqueCuts, projection.length];
    const pointAt = (offset) => ({ x: projection.start.x + projection.ux * offset, y: projection.start.y + projection.uy * offset });
    const segments = offsets.slice(0, -1).map((offset, index) => {
      const segment = clone(wall);
      if (index > 0) {
        segment.id = id();
        segment.name = `${String(wall.name || "Стена").slice(0, 285)} / ${index + 1}`;
        segment.faces.forEach((face) => { face.id = id(); });
      }
      segment.start = index === 0 ? clone(wall.start) : pointAt(offset);
      segment.end = index === offsets.length - 2 ? clone(wall.end) : pointAt(offsets[index + 1]);
      segment.properties = { ...segment.properties,
        split_origin_wall_id: wall.properties?.split_origin_wall_id || wall.id,
        split_origin_offset_mm: Number(wall.properties?.split_origin_offset_mm || 0) + offset };
      return segment;
    });
    const epsilon = Math.max(1e-8, projection.length * Number.EPSILON * 32);
    const hostOpenings = arrays(floor.openings).filter((item) => item.wall_id === wallId);
    for (const opening of hostOpenings) {
      const low = modelNumber(opening.offset_mm, "Отступ проёма", 0);
      const high = low + modelNumber(opening.width_mm, "Ширина проёма", 0, true);
      if (high > projection.length + epsilon) throw new Error(`Проём «${opening.name}» выходит за стену; сначала исправьте его размеры.`);
      if (uniqueCuts.some((cut) => cut > low + epsilon && cut < high - epsilon)) {
        throw new Error(`Точка деления попадает в проём «${opening.name || "Проём"}». Перенесите примыкание за границу проёма.`);
      }
      const index = segments.findIndex((_, segmentIndex) => low >= offsets[segmentIndex] - epsilon && high <= offsets[segmentIndex + 1] + epsilon);
      if (index < 0) throw new Error(`Не удалось безопасно перенести проём «${opening.name}» после деления.`);
      if (offsets[index] > epsilon) guardDimensionChange(project, [opening.id], ["offset_mm"], `стену у проёма «${opening.name}»`);
      opening.wall_id = segments[index].id;
      opening.offset_mm = Math.max(0, low - offsets[index]);
    }
    const oldFaceById = new Map(wall.faces.map((face) => [face.id, face]));
    const zones = [];
    for (const zone of arrays(floor.cladding_zones)) {
      const oldFace = oldFaceById.get(zone.wall_face_id);
      if (!oldFace) { zones.push(zone); continue; }
      const low = modelNumber(zone.offset_mm ?? 0, "Отступ облицовки", 0);
      const high = low + modelNumber(zone.width_mm, "Ширина облицовки", 0, true);
      if (high > projection.length + epsilon) throw new Error(`Облицовка «${zone.name}» выходит за стену; сначала исправьте её размеры.`);
      let emitted = 0;
      segments.forEach((segment, index) => {
        const from = Math.max(low, offsets[index]);
        const to = Math.min(high, offsets[index + 1]);
        if (to - from <= epsilon) return;
        const part = clone(zone);
        part.offset_mm = Math.max(0, from - offsets[index]);
        part.width_mm = to - from;
        const changedProperties = [];
        if (Math.abs(part.offset_mm - low) > epsilon) changedProperties.push("offset_mm");
        if (Math.abs(part.width_mm - Number(zone.width_mm)) > epsilon) changedProperties.push("width_mm");
        guardDimensionChange(project, [zone.id], changedProperties, `облицовку «${zone.name}»`);
        if (emitted > 0) {
          part.id = id();
          part.name = `${String(zone.name || "Облицовка").slice(0, 285)} / ${emitted + 1}`;
        }
        part.wall_face_id = segment.faces.find((face) => face.side === oldFace.side).id;
        part.excluded_opening_ids = arrays(zone.excluded_opening_ids)
          .filter((openingId) => hostOpenings.some((opening) => opening.id === openingId && opening.wall_id === segment.id));
        zones.push(part);
        emitted += 1;
      });
      if (!emitted) throw new Error(`Не удалось безопасно перенести облицовку «${zone.name}».`);
    }
    floor.cladding_zones = zones;
    const mounting = project.properties?.wall_mounting;
    if (mounting && Array.isArray(mounting.walls)) {
      const bindings = mounting.walls.flatMap(binding => {
        if (binding.wall_id !== wallId) return [binding];
        const oldFace = oldFaceById.get(binding.face_id);
        if (!oldFace) throw new Error("Сторона монтажа не найдена; деление остановлено без изменения проекта.");
        return segments.map(segment => {
          const part = clone(binding);
          part.wall_id = segment.id;
          part.face_id = segment.faces.find(face => face.side === oldFace.side).id;
          // Panel origins now belong to each segment. Recheck the resulting
          // layout explicitly, rather than retaining approval of the old wall.
          delete part.geometry_signature;
          return part;
        });
      });
      if (bindings.length > 100) throw new Error("После деления получится больше 100 монтажных сторон; разделите проект на комплекты.");
      mounting.walls = bindings;
    }
    arrays(floor.spaces).forEach((room) => {
      room.boundary = arrays(room.boundary).flatMap((edge) => {
        if (edge.wall_id !== wallId) return [edge];
        const ordered = edge.reversed ? [...segments].reverse() : segments;
        return ordered.map((segment) => ({ ...edge, wall_id: segment.id }));
      });
    });
    floor.walls.splice(floor.walls.findIndex((item) => item.id === wallId), 1, ...segments);
    return { original_wall_id: wallId, segment_ids: segments.map((segment) => segment.id) };
  }

  function wallSplitReport(walls) {
    const divided = walls.filter((item) => item.segment_ids.length > 1);
    return { split_count: divided.length,
      added_wall_count: divided.reduce((total, item) => total + item.segment_ids.length - 1, 0), walls: divided };
  }

  /** Split one wall at axis points or millimetre offsets from its start. */
  function splitWall(project, floorId, wallId, pointsOrOffsets) {
    if (!findFloor(project, floorId)) throw new Error("Этаж не найден.");
    const requestedCuts = Array.isArray(pointsOrOffsets) ? pointsOrOffsets : [pointsOrOffsets];
    const working = clone(project);
    const report = wallSplitReport([splitWallInPlace(working, findFloor(working, floorId).floor, wallId, requestedCuts)]);
    if (report.split_count) commitFloorGeometry(project, floorId, working);
    return report;
  }

  function splitIntersectionsInPlace(project, floor, options = {}) {
    const tolerance = geometryTolerance(project);
    const walls = arrays(floor.walls);
    const selected = options.wall_ids === undefined ? null : new Set(options.wall_ids);
    if (selected && [...selected].some((wallId) => !walls.some((wall) => wall.id === wallId))) throw new Error("Стена для автоматического деления не найдена.");
    const selectedIndices = selected ? walls.map((wall, index) => selected.has(wall.id) ? index : -1).filter(index => index >= 0) : null;
    const count = walls.length;
    const selectedCount = selectedIndices?.length;
    const comparisons = selected ? selectedCount * (count - selectedCount) + selectedCount * (selectedCount - 1) / 2 : count * (count - 1) / 2;
    if (count > 20000 || comparisons > 2000000) {
      throw new Error("Слишком много стен для одновременного автоматического деления. Разделите чертёж на этажи или выберите меньшую группу стен (не более 2 000 000 пар для проверки).");
    }
    if (selected && !selectedCount) return wallSplitReport([]);
    const cuts = new Map(walls.map((wall) => [wall.id, []]));
    for (let firstIndex = 0; firstIndex < walls.length; firstIndex += 1) {
      const first = walls[firstIndex];
      const a = projectOnWall(first, modelPoint(first.start, "Начало стены"));
      // A new single partition only needs O(n) comparisons against the floor,
      // not an O(n²) loop which merely skips every unrelated pair.
      const candidates = selected && !selected.has(first.id) ? selectedIndices : null;
      for (let cursor = candidates ? 0 : firstIndex + 1; cursor < (candidates ? candidates.length : walls.length); cursor += 1) {
        const secondIndex = candidates ? candidates[cursor] : cursor;
        if (secondIndex <= firstIndex) continue;
        const second = walls[secondIndex];
        if (!segmentBoundsOverlap(first, second, tolerance)) continue;
        if (partitionOverlapsWall(first, second, tolerance)) {
          throw new Error(`Стены «${first.name}» и «${second.name}» перекрываются. Удалите дубликат или уточните их оси перед делением.`);
        }
        const b = projectOnWall(second, modelPoint(second.start, "Начало стены"));
        const denominator = a.ux * b.uy - a.uy * b.ux;
        if (Math.abs(denominator) <= 64 * Number.EPSILON) continue;
        const dx = b.start.x - a.start.x;
        const dy = b.start.y - a.start.y;
        const alongA = (dx * b.uy - dy * b.ux) / denominator;
        const alongB = (dx * a.uy - dy * a.ux) / denominator;
        if (alongA < -tolerance || alongA > a.length + tolerance || alongB < -tolerance || alongB > b.length + tolerance) continue;
        cuts.get(first.id).push(Math.max(0, Math.min(a.length, alongA)));
        cuts.get(second.id).push(Math.max(0, Math.min(b.length, alongB)));
      }
    }
    return wallSplitReport([...cuts].filter(([, values]) => values.length)
      .map(([wallId, values]) => splitWallInPlace(project, floor, wallId, values)));
  }

  /** Atomically segment T/X axis contacts, optionally only around selected walls. */
  function splitWallsAtIntersections(project, floorId, options = {}) {
    if (!findFloor(project, floorId)) throw new Error("Этаж не найден.");
    if (options.wall_ids !== undefined && !Array.isArray(options.wall_ids)) throw new Error("Укажите список стен для автоматического деления.");
    const working = clone(project);
    const report = splitIntersectionsInPlace(working, findFloor(working, floorId).floor, options);
    if (report.split_count) commitFloorGeometry(project, floorId, working);
    return report;
  }

  /**
   * Append a partition and segment all contacted walls in one transaction.
   * Its faces remain unassigned: splitting walls does not invent new rooms.
   */
  function createPartition(project, floorId, inputStart, inputEnd, options = {}) {
    const entry = findFloor(project, floorId);
    if (!entry) throw new Error("Этаж не найден.");
    const floor = entry.floor;
    const tolerance = geometryTolerance(project);
    const snapTolerance = options.snap_tolerance_mm === undefined ? tolerance
      : modelNumber(options.snap_tolerance_mm, "Допуск привязки", 0);
    const start = snappedPartitionPoint(modelPoint(inputStart, "Начало перегородки"), arrays(floor.walls), snapTolerance);
    const end = snappedPartitionPoint(modelPoint(inputEnd, "Конец перегородки"), arrays(floor.walls), snapTolerance);
    if (!(Math.hypot(end.x - start.x, end.y - start.y) > tolerance)) {
      throw new Error("Укажите разные концы перегородки: её длина должна превышать геометрический допуск.");
    }
    const height = modelNumber(options.height_mm ?? floor.default_height_mm ?? 3000, "Высота перегородки", 0, true);
    const thickness = modelNumber(options.thickness_mm ?? 100, "Толщина перегородки", 0, true);
    const base = modelNumber(options.base_mm ?? 0, "Низ перегородки");
    const name = String(options.name ?? "Перегородка").trim();
    if (!name || name.length > 300) throw new Error("Название перегородки должно содержать от 1 до 300 символов.");
    const wall = {
      id: id(), properties: { editor_kind: "partition" }, name,
      start, end, thickness_mm: thickness, base_mm: base, height_mm: height,
      substrate: "unspecified", faces: [newWallFace("left", null), newWallFace("right", null)],
    };
    for (const existing of arrays(floor.walls)) {
      if (partitionOverlapsWall(wall, existing, tolerance)) {
        throw new Error(`Перегородка перекрывает стену «${existing.name || "Стена"}». Выберите другой участок или измените существующую стену.`);
      }
      for (const endpoint of [start, end]) {
        const hit = projectOnWall(existing, endpoint);
        if (hit.distance_mm > tolerance) continue;
        for (const opening of arrays(floor.openings).filter((item) => item.wall_id === existing.id)) {
          const low = Number(existing.base_mm || 0) + Number(opening.sill_mm || 0);
          const high = low + Number(opening.height_mm);
          if (hit.offset > Number(opening.offset_mm) + tolerance
            && hit.offset < Number(opening.offset_mm) + Number(opening.width_mm) - tolerance
            && Math.min(base + height, high) - Math.max(base, low) > tolerance) {
            throw new Error(`Конец перегородки попадает в проём «${opening.name || "Проём"}». Перенесите примыкание за границу проёма.`);
          }
        }
      }
    }
    const working = clone(project);
    const workingFloor = findFloor(working, floorId).floor;
    workingFloor.walls.push(wall);
    splitIntersectionsInPlace(working, workingFloor, { wall_ids: [wall.id] });
    const committedFloor = commitFloorGeometry(project, floorId, working);
    return committedFloor.walls.find((item) => item.id === wall.id);
  }

  /** Resolve a click to the centre of a bounded opening; never mutates floor. */
  function openingPlacement(floor, wallId, inputPoint, options = {}) {
    const matches = arrays(floor && floor.walls).filter((wall) => wall.id === wallId);
    if (matches.length !== 1) throw new Error("Стена для проёма не найдена или её ID неоднозначен.");
    const wall = matches[0];
    const point = modelPoint(inputPoint, "Место проёма");
    const projection = projectOnWall(wall, point);
    const kind = options.kind ?? "door";
    if (!["door", "window", "passage", "hatch", "custom"].includes(kind)) throw new Error("Неизвестный тип проёма.");
    const width = modelNumber(options.width_mm ?? (kind === "window" ? 1200 : 900), "Ширина проёма", 0, true);
    const sill = modelNumber(options.sill_mm ?? (kind === "window" ? 900 : 0), "Низ проёма", 0);
    const height = modelNumber(options.height_mm ?? (kind === "window" ? 1400 : 2100), "Высота проёма", 0, true);
    const wallHeight = modelNumber(wall.height_mm, "Высота стены", 0, true);
    if (width > projection.length + 1e-8) throw new Error("Проём шире выбранной стены. Задайте меньшую ширину.");
    if (sill + height > wallHeight + 1e-8) throw new Error("Проём выходит выше стены. Уменьшите высоту или отметку низа.");
    const offset = modelNumber(Math.max(0, Math.min(projection.length - width, projection.along - width / 2)), "Отступ проёма", 0);
    for (const existing of arrays(floor.openings)) {
      if (existing.wall_id !== wallId || existing.id === options.exclude_opening_id) continue;
      const overlapX = Math.min(offset + width, Number(existing.offset_mm) + Number(existing.width_mm)) - Math.max(offset, Number(existing.offset_mm));
      const overlapZ = Math.min(sill + height, Number(existing.sill_mm) + Number(existing.height_mm)) - Math.max(sill, Number(existing.sill_mm));
      if (overlapX > 1e-8 && overlapZ > 1e-8) throw new Error(`Проём пересекается с «${existing.name || "другим проёмом"}». Укажите свободное место.`);
    }
    return {
      wall_id: wallId, offset_mm: offset, width_mm: width, height_mm: height, sill_mm: sill, kind,
      center: { x: projection.start.x + projection.ux * (offset + width / 2), y: projection.start.y + projection.uy * (offset + width / 2) },
      distance_mm: projection.distance_mm,
      clamped: Math.abs(offset + width / 2 - projection.along) > 1e-8,
    };
  }

  function createOpeningAt(project, floorId, wallId, point, options = {}) {
    const entry = findFloor(project, floorId);
    if (!entry) throw new Error("Этаж не найден.");
    // Excluding an existing opening is for editing only, never for creation.
    const placement = openingPlacement(entry.floor, wallId, point, { ...options, exclude_opening_id: undefined });
    const name = String(options.name ?? ({ door: "Дверь", window: "Окно", passage: "Проход", hatch: "Люк", custom: "Проём" })[placement.kind]).trim();
    if (!name || name.length > 300) throw new Error("Название проёма должно содержать от 1 до 300 символов.");
    const { center, distance_mm, clamped, ...geometry } = placement;
    const opening = { id: id(), properties: {}, name, ...geometry, reveal_finish: "", door: null };
    entry.floor.openings.push(opening);
    return opening;
  }

  /** Update opening geometry atomically; a numeric offset never silently clamps. */
  function updateOpening(project, floorId, openingId, changes = {}) {
    const entry = findFloor(project, floorId);
    if (!entry) throw new Error("Этаж не найден.");
    const opening = arrays(entry.floor.openings).find((item) => item.id === openingId);
    if (!opening) throw new Error("Проём не найден.");
    const allowed = new Set(["name", "wall_id", "kind", "offset_mm", "width_mm", "height_mm", "sill_mm", "reveal_finish", "door"]);
    if (Object.keys(changes).some((key) => !allowed.has(key))) throw new Error("Изменение содержит неподдерживаемое поле проёма.");
    const candidate = { ...opening, ...changes };
    const wall = arrays(entry.floor.walls).find((item) => item.id === candidate.wall_id);
    if (!wall) throw new Error("Стена для проёма не найдена.");
    const offset = modelNumber(candidate.offset_mm, "Отступ проёма", 0);
    const width = modelNumber(candidate.width_mm, "Ширина проёма", 0, true);
    const projection = projectOnWall(wall, modelPoint(wall.start, "Начало стены"));
    if (offset + width > projection.length + 1e-8) throw new Error("Проём выходит за конец стены. Уменьшите отступ или ширину.");
    const centre = { x: projection.start.x + projection.ux * (offset + width / 2), y: projection.start.y + projection.uy * (offset + width / 2) };
    const placement = openingPlacement(entry.floor, wall.id, centre, { ...candidate, exclude_opening_id: opening.id });
    const name = String(candidate.name).trim();
    if (!name || name.length > 300) throw new Error("Название проёма должно содержать от 1 до 300 символов.");
    if (candidate.reveal_finish !== undefined && typeof candidate.reveal_finish !== "string") throw new Error("Отделка откосов должна быть текстом.");
    let door = null;
    if (candidate.kind !== "door" && changes.door != null) throw new Error("Параметры двери доступны только для проёма вида «Дверь».");
    if (candidate.kind === "door" && candidate.door != null) {
      const source = candidate.door;
      const doorFields = new Set(["leaf_width_mm", "frame_width_mm", "handing", "opening_side", "concealed_frame"]);
      if (typeof source !== "object" || Array.isArray(source) || Object.keys(source).some((key) => !doorFields.has(key))) throw new Error("Параметры двери содержат неподдерживаемое поле.");
      door = {
        leaf_width_mm: source.leaf_width_mm == null ? null : modelNumber(source.leaf_width_mm, "Суммарная ширина полотен", 0, true),
        frame_width_mm: source.frame_width_mm == null ? null : modelNumber(source.frame_width_mm, "Наружная ширина коробки", 0, true),
        handing: source.handing === undefined ? "unspecified" : source.handing,
        opening_side: source.opening_side === undefined ? "unspecified" : source.opening_side,
        concealed_frame: source.concealed_frame === undefined ? false : source.concealed_frame,
      };
      if (!["left", "right", "double", "unspecified"].includes(door.handing)) throw new Error("Укажите положение петель двери.");
      if (!["left", "right", "unspecified"].includes(door.opening_side)) throw new Error("Укажите сторону открывания двери.");
      if (typeof door.concealed_frame !== "boolean") throw new Error("Признак скрытой коробки должен быть логическим значением.");
    }
    const { center, distance_mm, clamped, ...geometry } = placement;
    const previousWall = opening.wall_id;
    if (candidate.wall_id !== previousWall || candidate.kind !== opening.kind) {
      guardSpecialInstallationChange(project, item => item.opening_id === opening.id, "перенести проём на другую стену или изменить его вид");
    }
    if (!door?.concealed_frame) {
      guardSpecialInstallationChange(project, item => item.opening_id === opening.id && item.kind === "concealed_door", "убрать скрытую коробку двери");
    }
    Object.assign(opening, candidate, geometry, { name, offset_mm: offset, door });
    if (previousWall !== opening.wall_id) {
      const faceIds = new Set(arrays(wall.faces).map((face) => face.id));
      arrays(entry.floor.cladding_zones).forEach((zone) => {
        if (!faceIds.has(zone.wall_face_id)) zone.excluded_opening_ids = arrays(zone.excluded_opening_ids).filter((idValue) => idValue !== opening.id);
      });
    }
    return opening;
  }

  /** Older files may use different frame conventions; surface discrepancies without rewriting them. */
  function doorDimensionWarnings(opening) {
    if (opening?.kind !== "door" || !opening.door) return [];
    const width = Number(opening.width_mm), leaf = Number(opening.door.leaf_width_mm), frame = Number(opening.door.frame_width_mm), warnings = [];
    if (Number.isFinite(width) && width > 0 && Number.isFinite(leaf) && leaf > width) warnings.push("Суммарная ширина полотен больше ширины проёма. Уточните размеры.");
    if (Number.isFinite(width) && width > 0 && Number.isFinite(frame) && frame > width) warnings.push("Наружная ширина коробки больше ширины проёма. Уточните размер или принятую в исходнике трактовку коробки.");
    if (Number.isFinite(frame) && frame > 0 && Number.isFinite(leaf) && leaf > frame) warnings.push("Суммарная ширина полотен больше наружной ширины коробки. Уточните размеры.");
    return warnings;
  }

  /** Infer a rectangle from its current boundary, including collinear cuts.
   * No creation metadata is required: imported and subsequently split rooms
   * behave exactly like rectangles drawn with the editor tool.
   */
  function rectangleRoomGeometry(floor, room, tolerance = 0.1) {
    if (!Number.isFinite(tolerance) || tolerance < 0) return null;
    const boundary = arrays(room?.boundary), walls = arrays(floor?.walls);
    if (boundary.length < 4 || boundary.length > 2000) return null;
    const byId = new Map(walls.map(wall => [wall.id, wall]));
    if (byId.size !== walls.length || new Set(boundary.map(edge => edge?.wall_id)).size !== boundary.length) return null;
    const segments = boundary.map(edge => {
      const wall = byId.get(edge?.wall_id);
      return wall && {wall, start: edge.reversed ? wall.end : wall.start, end: edge.reversed ? wall.start : wall.end};
    });
    if (segments.some(edge => !edge || ![edge.start, edge.end].every(point => point && typeof point.x === "number" && typeof point.y === "number" && Number.isFinite(point.x) && Number.isFinite(point.y)))) return null;
    if (segments.some((edge, index) => !pointsWithin(edge.end, segments[(index + 1) % segments.length].start, tolerance))) return null;
    const points = segments.flatMap(edge => [edge.start, edge.end]);
    const minX = Math.min(...points.map(point => point.x)), minY = Math.min(...points.map(point => point.y));
    const maxX = Math.max(...points.map(point => point.x)), maxY = Math.max(...points.map(point => point.y));
    const width = maxX - minX, depth = maxY - minY;
    if (Math.min(width, depth) <= tolerance) return null;
    const orientation = Math.sign(signedArea(segments.map(edge => edge.start))), sides = new Set();
    const near = (a, b) => Math.abs(a - b) <= tolerance;
    for (const {start, end} of segments) {
      const dx = end.x - start.x, dy = end.y - start.y;
      let side = null, direction = 0;
      if (near(start.y, minY) && near(end.y, minY) && Math.abs(dx) > tolerance) { side = "bottom"; direction = dx; }
      else if (near(start.x, maxX) && near(end.x, maxX) && Math.abs(dy) > tolerance) { side = "right"; direction = dy; }
      else if (near(start.y, maxY) && near(end.y, maxY) && Math.abs(dx) > tolerance) { side = "top"; direction = -dx; }
      else if (near(start.x, minX) && near(end.x, minX) && Math.abs(dy) > tolerance) { side = "left"; direction = -dy; }
      if (!side || Math.sign(direction) !== orientation) return null;
      sides.add(side);
    }
    if (sides.size !== 4) return null;
    return {width_mm: width, depth_mm: depth, anchor: {x: minX, y: minY}, bounds: {minX, minY, maxX, maxY}, wall_ids: segments.map(edge => edge.wall.id)};
  }

  /** Resize in reference-line coordinates, keeping the minimum X/Y corner.
   * IDs and locally referenced objects remain stable. Openings and partial
   * cladding keep their millimetre offsets; only full-face cladding stretches.
   * Work on a copy so an invalid dependency can never leave half a rectangle.
   */
  function resizeRectangleRoom(project, floorId, roomId, changes) {
    const entry = findFloor(project, floorId), room = arrays(entry?.floor.spaces).find(item => item.id === roomId);
    if (!entry || !room || idOccurrences(project, roomId) !== 1 || idOccurrences(project, floorId) !== 1) throw new Error("Помещение или этаж не найдены либо их ID неоднозначны.");
    if (!changes || typeof changes !== "object" || Array.isArray(changes) || Object.keys(changes).some(key => !["width_mm", "depth_mm", "height_mm", "thickness_mm"].includes(key))) throw new Error("Укажите ширину, длину и параметры прямоугольника.");
    const tolerance = geometryTolerance(project), shape = rectangleRoomGeometry(entry.floor, room, tolerance);
    if (!shape) throw new Error("Ширину и длину можно менять только у прямоугольного помещения, стороны которого параллельны осям X и Y.");
    const width = modelNumber(changes.width_mm, "Ширина", 0, true), depth = modelNumber(changes.depth_mm, "Длина", 0, true);
    const height = changes.height_mm === undefined ? null : modelNumber(changes.height_mm, "Высота помещения", 0, true);
    const thickness = changes.thickness_mm === undefined ? null : modelNumber(changes.thickness_mm, "Толщина стен", 0, true);
    if (Math.min(width, depth) <= tolerance) throw new Error("Ширина и длина должны быть больше геометрического допуска.");
    modelNumber(shape.anchor.x + width, "Правая граница помещения");
    modelNumber(shape.anchor.y + depth, "Верхняя граница помещения");
    const epsilon = 1e-8, changed = (a, b) => Math.abs(a - b) > epsilon;
    const planarChange = changed(width, shape.width_mm) || changed(depth, shape.depth_mm);
    const working = clone(project), floor = findFloor(working, floorId).floor, target = floor.spaces.find(item => item.id === roomId);
    const walls = shape.wall_ids.map(wallId => floor.walls.find(wall => wall.id === wallId)), boundaryIds = new Set(shape.wall_ids);
    const roomHeightChange = height !== null && changed(height, Number(target.height_mm));
    const wallChange = wall => planarChange || (height !== null && changed(height, Number(wall.height_mm))) || (thickness !== null && changed(thickness, Number(wall.thickness_mm)));
    if (!roomHeightChange && !walls.some(wallChange)) return room;
    if (target.properties?.locked === true || walls.some(wall => wallChange(wall) && (wall.properties?.locked === true || arrays(wall.faces).some(face => face.properties?.locked === true)))) throw new Error("Помещение или его стена заблокированы. Сначала снимите блокировку; геометрия сохранена.");
    if (walls.some(wall => idOccurrences(working, wall.id) !== 1 || arrays(wall.faces).some(face => idOccurrences(working, face.id) !== 1))) throw new Error("ID стены или стороны неоднозначен; размеры не изменены.");
    const shared = walls.find(wall => wallChange(wall) && (arrays(floor.spaces).some(other => other.id !== roomId && arrays(other.boundary).some(edge => edge.wall_id === wall.id)) || arrays(wall.faces).some(face => face.space_id && face.space_id !== roomId)));
    if (shared) throw new Error("У помещения есть общая стена с соседним помещением. Измените общую геометрию отдельно: размеры соседнего помещения не менялись.");
    if (planarChange) {
      const mobility = analyzeRoomMobility(floor, target, tolerance);
      if (!mobility.movable) throw new Error(`Нельзя изменить размеры прямоугольника: ${mobility.reason} Сначала измените примыкающие стены отдельно.`);
      const dimensions = [];
      if (changed(width, shape.width_mm)) dimensions.push("width_mm");
      if (changed(depth, shape.depth_mm)) dimensions.push("length_mm");
      guardDimensionChange(working, [roomId], dimensions, "помещение", "изменить размеры");
    }
    if (roomHeightChange) guardDimensionChange(working, [roomId], ["height_mm"], "помещение", "изменить высоту");
    const faceOwners = new Map(), previous = new Map(), affected = new Set();
    const transform = point => ({x: shape.anchor.x + (point.x - shape.anchor.x) * width / shape.width_mm, y: shape.anchor.y + (point.y - shape.anchor.y) * depth / shape.depth_mm});
    for (const wall of walls) {
      previous.set(wall.id, {length: wallLength(wall), height: Number(wall.height_mm)});
      arrays(wall.faces).forEach(face => faceOwners.set(face.id, wall));
      const start = planarChange ? transform(wall.start) : wall.start, end = planarChange ? transform(wall.end) : wall.end;
      const length = Math.hypot(end.x - start.x, end.y - start.y), before = previous.get(wall.id);
      if (!(length > tolerance)) throw new Error("После изменения одна из частей стены станет короче геометрического допуска. Сначала уберите лишнюю точку деления.");
      const dimensions = [];
      if (changed(length, before.length)) dimensions.push("length_mm");
      if (height !== null && changed(height, before.height)) dimensions.push("height_mm");
      if (thickness !== null && changed(thickness, Number(wall.thickness_mm))) dimensions.push("thickness_mm");
      guardDimensionChange(working, [wall.id, ...arrays(wall.faces).map(face => face.id)], dimensions, `стену «${wall.name}»`, "изменить");
      if (wallChange(wall)) affected.add(wall.id);
      wall.start = start; wall.end = end;
      if (height !== null) wall.height_mm = height;
      if (thickness !== null) wall.thickness_mm = thickness;
    }
    if (height !== null) target.height_mm = height;
    const affectedFaces = new Set(walls.filter(wall => affected.has(wall.id)).flatMap(wall => arrays(wall.faces).map(face => face.id)));
    const affectedOpenings = new Set(arrays(floor.openings).filter(opening => affected.has(opening.wall_id)).map(opening => opening.id));
    guardSpecialInstallationChange(working, item => affected.has(item.wall_id) || affectedFaces.has(item.face_id) || affectedOpenings.has(item.opening_id), "изменить размеры стены со специальным изделием");
    for (const opening of arrays(floor.openings).filter(item => boundaryIds.has(item.wall_id))) {
      const wall = walls.find(item => item.id === opening.wall_id);
      const offset = modelNumber(opening.offset_mm, "Отступ проёма", 0), openingWidth = modelNumber(opening.width_mm, "Ширина проёма", 0, true);
      const sill = modelNumber(opening.sill_mm ?? 0, "Низ проёма", 0), openingHeight = modelNumber(opening.height_mm, "Высота проёма", 0, true);
      if (offset + openingWidth > wallLength(wall) + epsilon || sill + openingHeight > wall.height_mm + epsilon) throw new Error(`Проём «${opening.name || "Проём"}» не помещается в новую стену. Уменьшите или перенесите проём; размеры помещения не изменены.`);
    }
    for (const zone of arrays(floor.cladding_zones).filter(item => faceOwners.has(item.wall_face_id))) {
      const wall = faceOwners.get(zone.wall_face_id), before = previous.get(wall.id);
      const offset = modelNumber(zone.offset_mm ?? 0, "Отступ облицовки", 0), zoneWidth = modelNumber(zone.width_mm, "Ширина облицовки", 0, true);
      const base = modelNumber(zone.base_mm ?? 0, "Низ облицовки", 0), zoneHeight = modelNumber(zone.height_mm, "Высота облицовки", 0, true);
      const fullWidth = Math.abs(offset) <= epsilon && !changed(zoneWidth, before.length), fullHeight = Math.abs(base) <= epsilon && !changed(zoneHeight, before.height);
      const nextWidth = fullWidth ? wallLength(wall) : zoneWidth, nextHeight = fullHeight ? wall.height_mm : zoneHeight;
      const dimensions = [];
      if (changed(nextWidth, zoneWidth)) dimensions.push("width_mm");
      if (changed(nextHeight, zoneHeight)) dimensions.push("height_mm");
      guardDimensionChange(working, [zone.id], dimensions, `облицовку «${zone.name}»`, "изменить");
      if (dimensions.length && zone.properties?.locked === true) throw new Error(`Облицовка «${zone.name}» заблокирована. Размеры помещения не изменены.`);
      if (offset + nextWidth > wallLength(wall) + epsilon || base + nextHeight > wall.height_mm + epsilon) throw new Error(`Облицовка «${zone.name}» не помещается в новую стену. Сначала измените границы облицовки.`);
      zone.width_mm = nextWidth; zone.height_mm = nextHeight;
    }
    if (planarChange) {
      const inside = (point, bounds) => point.x > bounds.minX + tolerance && point.x < bounds.maxX - tolerance && point.y > bounds.minY + tolerance && point.y < bounds.maxY - tolerance;
      const nextBounds = {minX: shape.anchor.x, minY: shape.anchor.y, maxX: shape.anchor.x + width, maxY: shape.anchor.y + depth};
      for (const outside of arrays(floor.walls).filter(wall => !boundaryIds.has(wall.id))) {
        if (walls.some(wall => segmentsTouch(wall, outside, tolerance)) || [outside.start, outside.end].some(point => inside(point, shape.bounds) !== inside(point, nextBounds))) throw new Error("Новый размер пересекает другую стену, соседнее помещение или оставляет перегородку за пределами комнаты. Сначала измените примыкающую геометрию.");
      }
      for (const obstacle of arrays(floor.obstacles).filter(item => item.space_id === roomId)) {
        if (arrays(obstacle.footprint).some(point => !finitePoint(point) || point.x < nextBounds.minX - tolerance || point.x > nextBounds.maxX + tolerance || point.y < nextBounds.minY - tolerance || point.y > nextBounds.maxY + tolerance)) throw new Error(`Объект «${obstacle.name || "Препятствие"}» окажется вне помещения. Сначала перенесите его.`);
      }
    }
    const mounting = working.properties?.wall_mounting;
    if (mounting && !Array.isArray(mounting.walls)) throw new Error("Формат монтажных привязок некорректен. Размеры помещения не изменены.");
    arrays(mounting?.walls).forEach(binding => { if (affected.has(binding.wall_id)) delete binding.geometry_signature; });
    const committed = commitFloorGeometry(project, floorId, working);
    return committed.spaces.find(item => item.id === roomId);
  }

  /**
   * Resolve whether a room can be translated without tearing model topology.
   * Walls shared by ID and every geometric contact with a non-boundary wall
   * are deliberately conservative blockers.
   */
  function analyzeRoomMobility(floor, room, tolerance = 0.1) {
    const numericTolerance = Number(tolerance);
    if (!Number.isFinite(numericTolerance) || numericTolerance < 0) {
      throw new RangeError("Допуск перемещения должен быть конечным неотрицательным числом.");
    }
    if (!floor || !room) return { movable: false, reason: "Помещение или этаж не найдены.", walls: [], obstacles: [] };
    const boundary = arrays(room.boundary);
    const wallById = new Map(arrays(floor.walls).map((wall) => [wall.id, wall]));
    const boundaryIds = new Set();
    const walls = [];
    for (const edge of boundary) {
      const wall = edge && wallById.get(edge.wall_id);
      if (!wall || boundaryIds.has(edge.wall_id)) {
        return { movable: false, reason: "Контур помещения содержит отсутствующую или повторную стену.", walls: [], obstacles: [] };
      }
      if (![wall.start, wall.end].every(finitePoint) || !(wallLength(wall) > 0)) {
        return { movable: false, reason: "Контур помещения содержит стену с некорректной геометрией.", walls: [], obstacles: [] };
      }
      boundaryIds.add(edge.wall_id);
      walls.push(wall);
    }
    if (walls.length < 3) return { movable: false, reason: "Контур помещения должен содержать минимум три стены.", walls: [], obstacles: [] };

    const shared = arrays(floor.spaces).some((other) => other !== room && other.id !== room.id
      && arrays(other.boundary).some((edge) => edge && boundaryIds.has(edge.wall_id)));
    const obstacles = arrays(floor.obstacles).filter((obstacle) => obstacle.space_id === room.id);
    if (shared) {
      return { movable: false, reason: "Помещение использует общую стену с соседним помещением.", walls, obstacles };
    }

    const outsideWalls = arrays(floor.walls).filter((wall) => !boundaryIds.has(wall.id));
    for (const outside of outsideWalls) {
      if (![outside.start, outside.end].every(finitePoint) || !(wallLength(outside) > 0)) {
        return { movable: false, reason: "На этаже есть стена с некорректной геометрией; безопасный перенос не определён.", walls, obstacles };
      }
      for (const boundaryWall of walls) {
        if (segmentsTouch(boundaryWall, outside, numericTolerance)) {
          return { movable: false, reason: "Контур соприкасается или пересекается с другой геометрией стен.", walls, obstacles };
        }
      }
    }
    return { movable: true, reason: "", walls, obstacles };
  }

  /** Translate one previously analysed isolated room as a single transaction. */
  function translateRoomGeometry(analysis, dx, dy) {
    if (!analysis || analysis.movable !== true) throw new Error("Перемещение помещения заблокировано проверкой топологии.");
    const shiftX = Number(dx);
    const shiftY = Number(dy);
    if (!Number.isFinite(shiftX) || !Number.isFinite(shiftY)) throw new TypeError("Смещение должно быть конечным числом.");
    const points = [];
    const seen = new Set();
    const collect = (point) => {
      if (seen.has(point)) return;
      seen.add(point);
      if (!finitePoint(point)) throw new TypeError("Геометрия содержит некорректную точку.");
      const x = Number(point.x) + shiftX;
      const y = Number(point.y) + shiftY;
      if (!Number.isFinite(x) || !Number.isFinite(y)) throw new RangeError("Результат перемещения вышел за числовой диапазон.");
      points.push({ point, x, y });
    };
    arrays(analysis.walls).forEach((wall) => { collect(wall.start); collect(wall.end); });
    arrays(analysis.obstacles).forEach((obstacle) => arrays(obstacle.footprint).forEach(collect));
    points.forEach((item) => { item.point.x = item.x; item.point.y = item.y; });
    return analysis;
  }

  function idOccurrences(project, entityId) {
    let count = allEntities(project).filter((item) => item.entity.id === entityId).length;
    floorEntries(project).forEach(({ floor }) => arrays(floor.walls).forEach((wall) => {
      count += arrays(wall.faces).filter((face) => face.id === entityId).length;
    }));
    return count;
  }

  function supportedMatches(project, type, entityId) {
    if (type === "measurement") return arrays(project.measurements).filter((item) => item.id === entityId).map((entity) => ({ entity }));
    const key = { opening: "openings", zone: "cladding_zones", room: "spaces", wall: "walls" }[type];
    if (!key) return [];
    return floorEntries(project).flatMap(({ floor }) => arrays(floor[key]).filter((item) => item.id === entityId).map((entity) => ({ entity, floor })));
  }

  function cleanRemovedReferences(project, removedIds, removedOpeningIds) {
    let changed = true;
    while (changed) {
      changed = false;
      arrays(project.measurements).forEach((measurement) => {
        if (!removedIds.has(measurement.id) && removedIds.has(measurement.target_id)) {
          removedIds.add(measurement.id);
          changed = true;
        }
      });
      arrays(project.design_dimensions).forEach((dimension) => {
        if (!removedIds.has(dimension.id) && removedIds.has(dimension.target_id)) {
          removedIds.add(dimension.id);
          changed = true;
        }
      });
    }
    project.measurements = arrays(project.measurements).filter((item) => !removedIds.has(item.id));
    project.design_dimensions = arrays(project.design_dimensions).filter((item) => !removedIds.has(item.id));
    project.design_dimensions.forEach((dimension) => {
      dimension.measurement_ids = arrays(dimension.measurement_ids).filter((measurementId) => !removedIds.has(measurementId));
    });
    floorEntries(project).forEach(({ floor }) => arrays(floor.cladding_zones).forEach((zone) => {
      zone.excluded_opening_ids = arrays(zone.excluded_opening_ids).filter((openingId) => !removedOpeningIds.has(openingId));
    }));
    // Removing a location does not remove the reusable section or its source.
    // Only explicit references to the deleted room/zone are detached.
    arrays(project.properties?.cad_assets).forEach(asset => arrays(asset.sections).forEach(section => {
      for (const key of ["target_space_id", "target_zone_id"]) {
        if (removedIds.has(section[key])) section[key] = null;
      }
    }));
    const wallMounting = project.properties?.wall_mounting;
    if (wallMounting && Array.isArray(wallMounting.walls)) {
      wallMounting.walls = wallMounting.walls.filter(binding =>
        !removedIds.has(binding.wall_id) && !removedIds.has(binding.face_id));
    }
    const installations = specialItems(project, "special_installations");
    const assemblies = specialItems(project, "special_assemblies");
    const removedInstallations = new Set(installations.filter(item =>
      ["wall_id", "face_id", "opening_id", "floor_id", "space_id"].some(key => removedIds.has(item[key])))
      .map(item => item.id));
    if (removedInstallations.size) {
      project.properties.special_installations.items = installations.filter(item => !removedInstallations.has(item.id));
      if (project.properties.special_assemblies) project.properties.special_assemblies.items = assemblies.filter(item => !removedInstallations.has(item.installation_id));
    }
  }

  /**
   * Delete one supported entity and every reference that would otherwise
   * dangle. The project is replaced only after the complete operation has
   * succeeded, so an `{ok:false}` result never changes the caller's model.
   */
  function deleteEntityCascade(project, type, entityId, options = {}) {
    const supported = new Set(["opening", "zone", "measurement", "room", "wall"]);
    if (!supported.has(type)) return { ok: false, message: "Этот тип объекта нельзя безопасно удалить каскадно.", removed_ids: [] };
    if (!project || typeof project !== "object" || typeof entityId !== "string" || !entityId) {
      return { ok: false, message: "Не указан проект или ID удаляемого объекта.", removed_ids: [] };
    }
    if (idOccurrences(project, entityId) !== 1) {
      return { ok: false, message: "Объект не найден или его ID неоднозначен.", removed_ids: [] };
    }
    const working = clone(project);
    const matches = supportedMatches(working, type, entityId);
    if (matches.length !== 1) return { ok: false, message: "Объект не найден или его тип не совпадает.", removed_ids: [] };

    const removedIds = new Set([entityId]);
    const removedOpeningIds = new Set();
    const target = matches[0];
    const cleanupPoints = target.floor && ["room","wall"].includes(type) ? arrays(target.floor.walls).filter(w=>type==="wall"?w.id===entityId:arrays(target.entity.boundary).some(e=>e.wall_id===w.id)).flatMap(w=>[clone(w.start),clone(w.end)]) : [];
    if (type === "opening") {
      target.floor.openings = arrays(target.floor.openings).filter((item) => item.id !== entityId);
      removedOpeningIds.add(entityId);
    } else if (type === "zone") {
      target.floor.cladding_zones = arrays(target.floor.cladding_zones).filter((item) => item.id !== entityId);
    } else if (type === "measurement") {
      working.measurements = arrays(working.measurements).filter((item) => item.id !== entityId);
    } else if (type === "wall") {
      const referencedByRoom = floorEntries(working).some(({ floor }) => arrays(floor.spaces)
        .some((space) => arrays(space.boundary).some((edge) => edge.wall_id === entityId)));
      if (referencedByRoom) {
        return { ok: false, message: "Стена входит в контур помещения. Отдельно можно удалить только свободную перегородку; удаление этой стены разорвёт контур.", removed_ids: [] };
      }
      const faceIds = new Set(arrays(target.entity.faces).map((face) => face.id));
      faceIds.forEach((faceId) => removedIds.add(faceId));
      floorEntries(working).forEach(({ floor }) => {
        arrays(floor.openings).forEach((opening) => {
          if (opening.wall_id === entityId) { removedIds.add(opening.id); removedOpeningIds.add(opening.id); }
        });
        arrays(floor.cladding_zones).forEach((zone) => {
          if (faceIds.has(zone.wall_face_id)) removedIds.add(zone.id);
        });
        floor.walls = arrays(floor.walls).filter((wall) => wall.id !== entityId);
        floor.openings = arrays(floor.openings).filter((opening) => !removedOpeningIds.has(opening.id));
        floor.cladding_zones = arrays(floor.cladding_zones).filter((zone) => !removedIds.has(zone.id));
      });
    } else if (type === "room") {
      const room = target.entity;
      const boundaryIds = arrays(room.boundary).map((edge) => edge && edge.wall_id);
      if (boundaryIds.length < 3 || boundaryIds.some((idValue) => typeof idValue !== "string") || new Set(boundaryIds).size !== boundaryIds.length) {
        return { ok: false, message: "Контур помещения некорректен; удаление остановлено.", removed_ids: [] };
      }
      const floorWallIds = new Set(arrays(target.floor.walls).map((wall) => wall.id));
      if (boundaryIds.some((wallId) => !floorWallIds.has(wallId))) {
        return { ok: false, message: "Не все стены контура принадлежат этажу; удаление остановлено.", removed_ids: [] };
      }
      const wallOccurrences = new Map(boundaryIds.map((wallId) => [wallId, 0]));
      floorEntries(working).forEach(({ floor }) => arrays(floor.walls).forEach((wall) => {
        if (wallOccurrences.has(wall.id)) wallOccurrences.set(wall.id, wallOccurrences.get(wall.id) + 1);
      }));
      if ([...wallOccurrences.values()].some((count) => count !== 1)) {
        return { ok: false, message: "ID стены контура неоднозначен; удаление остановлено.", removed_ids: [] };
      }
      const referencedByOtherRooms = new Set();
      floorEntries(working).forEach(({ floor }) => arrays(floor.spaces).forEach((space) => {
        if (space.id === room.id) return;
        arrays(space.boundary).forEach((edge) => { if (edge && boundaryIds.includes(edge.wall_id)) referencedByOtherRooms.add(edge.wall_id); });
      }));
      const removedWallIds = new Set(boundaryIds.filter((wallId) => !referencedByOtherRooms.has(wallId)));
      const vacatedFaceIds = new Set();
      const removedFaceIds = new Set();
      floorEntries(working).forEach(({ floor }) => arrays(floor.walls).forEach((wall) => arrays(wall.faces).forEach((face) => {
        if (face.space_id === room.id) vacatedFaceIds.add(face.id);
        if (removedWallIds.has(wall.id)) removedFaceIds.add(face.id);
      })));
      const removedZoneIds = new Set();
      const removedRoomOpeningIds = new Set();
      floorEntries(working).forEach(({ floor }) => {
        arrays(floor.openings).forEach((opening) => { if (removedWallIds.has(opening.wall_id)) removedRoomOpeningIds.add(opening.id); });
        arrays(floor.cladding_zones).forEach((zone) => {
          if (vacatedFaceIds.has(zone.wall_face_id) || removedFaceIds.has(zone.wall_face_id)) removedZoneIds.add(zone.id);
        });
      });
      [room.id, ...removedWallIds, ...removedFaceIds, ...removedRoomOpeningIds, ...removedZoneIds].forEach((idValue) => removedIds.add(idValue));
      removedRoomOpeningIds.forEach((idValue) => removedOpeningIds.add(idValue));
      floorEntries(working).forEach(({ floor }) => {
        floor.spaces = arrays(floor.spaces).filter((space) => space.id !== room.id);
        floor.walls.forEach((wall) => arrays(wall.faces).forEach((face) => { if (face.space_id === room.id) face.space_id = null; }));
        floor.walls = arrays(floor.walls).filter((wall) => !removedWallIds.has(wall.id));
        floor.openings = arrays(floor.openings).filter((opening) => !removedRoomOpeningIds.has(opening.id));
        floor.cladding_zones = arrays(floor.cladding_zones).filter((zone) => !removedZoneIds.has(zone.id));
        arrays(floor.obstacles).forEach((obstacle) => { if (obstacle.space_id === room.id) obstacle.space_id = null; });
      });
      const mounting = working.properties?.wall_mounting;
      if (mounting && Array.isArray(mounting.walls)) {
        // An occupied side belongs to the room being deleted even when the
        // physical wall survives as its neighbour's boundary.
        mounting.walls = mounting.walls.filter(binding => !vacatedFaceIds.has(binding.face_id));
      }
      // The surviving neighbour may keep this physical wall, but assignments
      // on the deleted room's side belong to the deleted room, not its neighbour.
      const installations = specialItems(working, "special_installations");
      const assemblies = specialItems(working, "special_assemblies");
      const vacatedInstallations = new Set(installations.filter(item => vacatedFaceIds.has(item.face_id)).map(item => item.id));
      if (vacatedInstallations.size) {
        working.properties.special_installations.items = installations.filter(item => !vacatedInstallations.has(item.id));
        if (working.properties.special_assemblies) working.properties.special_assemblies.items = assemblies.filter(item => !vacatedInstallations.has(item.installation_id));
      }
    }

    cleanRemovedReferences(working, removedIds, removedOpeningIds);
    const cleanup = !options.deferCleanup && cleanupPoints.length ? simplifyAtPoints(working,target.floor.id,cleanupPoints) : {merged_count:0,skipped:[]};
    Object.keys(project).forEach((key) => { delete project[key]; });
    Object.assign(project, working);
    return { ok: true, removed_ids: [...removedIds].sort(),cleanup_points:cleanupPoints,cleanup_floor_id:target.floor?.id,...cleanup };
  }

  // Merging is deliberately conservative: no guessed transforms of measured
  // lengths, room ownership, finishes, or different mounting configurations.
  function mergeAtPoint(project,floorId,value) {
    const floor=findFloor(project,floorId)?.floor;if(!floor||idOccurrences(project,floorId)!==1)throw new Error("Этаж не найден или его ID неоднозначен.");
    const p=modelPoint(value,"Вершина"),tol=geometryTolerance(project);
    const incident=arrays(floor.walls).filter(w=>pointsWithin(w.start,p,tol)||pointsWithin(w.end,p,tol));
    if(incident.length!==2)throw new Error("Вершину можно убрать только между двумя стенами: угол или примыкание нужно сохранить.");
    const [a,b]=incident,atAStart=pointsWithin(a.start,p,tol),atBStart=pointsWithin(b.start,p,tol),farA=atAStart?a.end:a.start,farB=atBStart?b.end:b.start;
    guardSpecialInstallationChange(project, item => incident.some(wall => wall.id === item.wall_id), "объединить стены");
    if(incident.some(w=>idOccurrences(project,w.id)!==1||arrays(w.faces).some(f=>idOccurrences(project,f.id)!==1)))throw new Error("ID стены или стороны неоднозначен; объединение остановлено.");
    const va={x:farA.x-p.x,y:farA.y-p.y},vb={x:farB.x-p.x,y:farB.y-p.y},la=Math.hypot(va.x,va.y),lb=Math.hypot(vb.x,vb.y);
    if(Math.min(la,lb)<=tol||Math.abs(va.x*vb.y-va.y*vb.x)>tol*Math.min(la,lb)||va.x*vb.x+va.y*vb.y>=0)throw new Error("Это угол, а не лишняя точка на прямой. Геометрия сохранена.");
    if(arrays(floor.walls).some(w=>!incident.includes(w)&&projectOnWall(w,p).distance_mm<=tol&&projectOnWall(w,p).along>=-tol&&projectOnWall(w,p).along<=wallLength(w)+tol))throw new Error("В этой точке проходит другая стена; примыкание нельзя удалить.");
    const strip=(obj,keys)=>Object.fromEntries(Object.entries(obj||{}).filter(([key])=>!keys.includes(key)));
    const sameValue=(left,right)=>JSON.stringify(left)===JSON.stringify(right);
    for(const key of ["thickness_mm","height_mm","base_mm","substrate"])if(a[key]!==b[key])throw new Error("У соседних стен разные толщины, высоты или основание. Сначала согласуйте свойства.");
    const ap=strip(a.properties,["split_origin_wall_id","split_origin_offset_mm","merged_wall_ids","merged_wall_names"]),bp=strip(b.properties,["split_origin_wall_id","split_origin_offset_mm","merged_wall_ids","merged_wall_names"]);
    if(!sameValue(ap,bp))throw new Error("У стен разные дополнительные свойства или происхождение. Автоматическое объединение небезопасно.");
    const start=clone(atAStart?farB:a.start),end=clone(atAStart?a.end:farB),total=Math.hypot(end.x-start.x,end.y-start.y),ux=(end.x-start.x)/total,uy=(end.y-start.y)/total;
    const forward=w=>(w.end.x-w.start.x)*ux+(w.end.y-w.start.y)*uy>0;
    const faceMaps=new Map();
    for(const wall of incident){if(arrays(wall.faces).length!==2)throw new Error("У стены отсутствуют две стороны.");for(const face of wall.faces){const side=forward(wall)?face.side:face.side==="left"?"right":"left",kept=a.faces.find(f=>f.side===side);if(!kept)throw new Error("Сторона стены не найдена.");faceMaps.set(face.id,kept.id);if(!sameValue(strip(face,["id","side"]),strip(kept,["id","side"])))throw new Error("Стороны стен относятся к разным помещениям или имеют разную отделку.");}}
    const touched=new Set([a.id,b.id,...a.faces.map(f=>f.id),...b.faces.map(f=>f.id)]);
    if([...arrays(project.measurements),...arrays(project.design_dimensions)].some(item=>touched.has(item.target_id)))throw new Error("На стены или их стороны ссылаются обмеры / проектные размеры. Объединение изменит смысл ссылки; точка сохранена.");
    for(const room of arrays(floor.spaces)){
      const indices=arrays(room.boundary).map((edge,index)=>incident.some(w=>w.id===edge.wall_id)?index:-1).filter(i=>i>=0);
      if(indices.length && (indices.length!==2 || !((indices[1]-indices[0]===1)||(indices[0]===0&&indices[1]===room.boundary.length-1))))throw new Error("Объединение разорвёт или изменит контур помещения.");
      if(indices.length && room.boundary.length<=3)throw new Error("У помещения должно остаться не менее трёх сторон.");
    }
    const mounting=project.properties?.wall_mounting;
    const keptBindings=[];
    if(mounting&&!Array.isArray(mounting.walls))throw new Error("Формат монтажных привязок некорректен; сначала исправьте монтаж проекта.");
    if(mounting){const relevant=arrays(mounting.walls).filter(binding=>incident.some(w=>w.id===binding.wall_id));const keys=new Set();for(const binding of relevant){const owner=incident.find(w=>w.id===binding.wall_id);if(!owner.faces.some(f=>f.id===binding.face_id)||keys.has(binding.face_id))throw new Error("На стенах некорректные или повторные монтажные привязки; сначала исправьте их.");keys.add(binding.face_id);}}
    if(mounting){for(const face of a.faces){const bindings=incident.map(w=>arrays(mounting.walls).find(binding=>binding.wall_id===w.id&&faceMaps.get(binding.face_id)===face.id));if(Boolean(bindings[0])!==Boolean(bindings[1]))throw new Error("Монтаж назначен только на часть стены. Объединение расширило бы его область — точка сохранена.");if(bindings[0]){if(!sameValue(strip(bindings[0],["wall_id","face_id","geometry_signature"]),strip(bindings[1],["wall_id","face_id","geometry_signature"])))throw new Error("На частях стены разные монтажные решения. Их нельзя заменить одним.");const binding=clone(bindings[0]);binding.wall_id=a.id;binding.face_id=face.id;delete binding.geometry_signature;keptBindings.push(binding);}}}
    const along=point=>(point.x-start.x)*ux+(point.y-start.y)*uy;
    const remapOffset=(wall,offset,width)=>forward(wall)?along(wall.start)+offset:along(wall.start)-offset-width;
    const changes=[];
    for(const opening of arrays(floor.openings).filter(o=>incident.some(w=>w.id===o.wall_id))){const wall=incident.find(w=>w.id===opening.wall_id),oldOffset=modelNumber(opening.offset_mm,"Отступ проёма",0),width=modelNumber(opening.width_mm,"Ширина проёма",0,true);if(oldOffset+width>wallLength(wall)+tol)throw new Error("Проём выходит за стену; сначала исправьте его размеры.");if(!forward(wall)&&opening.door&&Object.values(opening.door).some(value=>typeof value==="string"&&value!=="unspecified"&&value!==""))throw new Error("При развороте стены изменится направление двери. Сначала проверьте её исполнение; объединение отменено.");const offset=remapOffset(wall,oldOffset,width);if(Math.abs(offset-oldOffset)>1e-8)guardDimensionChange(project,[opening.id],["offset_mm"],`стены у проёма «${opening.name}»`,"объединить");changes.push(()=>{opening.wall_id=a.id;opening.offset_mm=Math.max(0,offset);});}
    for(const zone of arrays(floor.cladding_zones).filter(z=>faceMaps.has(z.wall_face_id))){const wall=incident.find(w=>w.faces.some(f=>f.id===zone.wall_face_id)),oldOffset=modelNumber(zone.offset_mm??0,"Отступ облицовки",0),width=modelNumber(zone.width_mm,"Ширина облицовки",0,true);if(oldOffset+width>wallLength(wall)+tol)throw new Error("Облицовка выходит за стену; сначала исправьте её размеры.");const offset=remapOffset(wall,oldOffset,width);if(Math.abs(offset-oldOffset)>1e-8)guardDimensionChange(project,[zone.id],["offset_mm"],`облицовку «${zone.name}»`,"объединить");changes.push(()=>{zone.wall_face_id=faceMaps.get(zone.wall_face_id);zone.offset_mm=Math.max(0,offset);});}
    // The same physical source span may now reference the surviving wall ID.
    // Keep its previous IDs in wall provenance; never reactivate a source mark.
    const sourceMarks=floorEntries(project).flatMap(({floor:f})=>arrays(f.properties?.source_markup?.sources).flatMap(s=>arrays(s.marks)));
    const aRoot=a.properties?.split_origin_wall_id||a.id,bRoot=b.properties?.split_origin_wall_id||b.id;
    if(aRoot!==bRoot&&sourceMarks.some(mark=>arrays(mark.model_ids).some(id=>touched.has(id))))throw new Error("Сегменты связаны с разными исходными линиями. Объединение могло бы изменить разметку исходника; точка сохранена.");
    for(const change of changes)change();
    for(const room of arrays(floor.spaces))room.boundary=arrays(room.boundary).filter(edge=>edge.wall_id!==b.id);
    for(const mark of sourceMarks)if(Array.isArray(mark.model_ids)&&mark.model_ids.some(id=>touched.has(id)))mark.model_ids=[...new Set(mark.model_ids.map(id=>id===b.id?a.id:faceMaps.get(id)||id))];
    if(mounting)mounting.walls=[...arrays(mounting.walls).filter(binding=>!incident.some(w=>w.id===binding.wall_id)),...keptBindings];
    a.start=start;a.end=end;
    a.properties={...a.properties,merged_wall_ids:[...new Set([a.id,b.id,...arrays(a.properties?.merged_wall_ids),...arrays(b.properties?.merged_wall_ids)])],merged_wall_names:[...new Set([a.name,b.name,...arrays(a.properties?.merged_wall_names),...arrays(b.properties?.merged_wall_names)])]};
    if(aRoot===bRoot)a.properties.split_origin_offset_mm=Math.min(Number(a.properties?.split_origin_offset_mm||0),Number(b.properties?.split_origin_offset_mm||0));else {delete a.properties.split_origin_wall_id;delete a.properties.split_origin_offset_mm;}
    floor.walls=floor.walls.filter(w=>w.id!==b.id);
    return {ok:true,wall_id:a.id,removed_wall_id:b.id};
  }
  function simplifyAtPoints(project,floorId,points){let merged=0;const skipped=[];const seen=[];for(const p of points){if(seen.some(q=>pointsWithin(p,q,geometryTolerance(project))))continue;seen.push(p);try{mergeAtPoint(project,floorId,p);merged++;}catch(error){skipped.push({point:clone(p),message:error.message});}}return {merged_count:merged,skipped};}
  function removeCollinearVertex(project,floorId,point){const work=clone(project);try{const report=mergeAtPoint(work,floorId,point);Object.keys(project).forEach(key=>delete project[key]);Object.assign(project,work);return report;}catch(error){return {ok:false,message:error.message};}}
  function deleteEntitiesCascade(project,selections){
    if(!Array.isArray(selections)||!selections.length)return {ok:false,message:"Ничего не выбрано для удаления.",removed_ids:[]};
    const unique=[...new Map(selections.map(item=>[`${item.type}:${item.id}`,item])).values()],work=clone(project),removed=new Set(),cleanup=new Map();
    for(const item of unique)if(!["room","wall","opening","zone","measurement"].includes(item.type)||supportedMatches(work,item.type,item.id).length!==1||idOccurrences(work,item.id)!==1)return {ok:false,message:"В выделении есть отсутствующий или неоднозначный объект. Ничего не удалено.",removed_ids:[]};
    unique.sort((a,b)=>(a.type==="room"?0:a.type==="wall"?1:2)-(b.type==="room"?0:b.type==="wall"?1:2));
    for(const item of unique){if(removed.has(item.id))continue;const result=deleteEntityCascade(work,item.type,item.id,{deferCleanup:true});if(!result.ok)return result;result.removed_ids.forEach(id=>removed.add(id));if(result.cleanup_floor_id)cleanup.set(result.cleanup_floor_id,[...cleanup.get(result.cleanup_floor_id)||[],...result.cleanup_points]);}
    let merged=0;for(const [floorId,points]of cleanup)merged+=simplifyAtPoints(work,floorId,points).merged_count;
    Object.keys(project).forEach(key=>delete project[key]);Object.assign(project,work);return {ok:true,removed_ids:[...removed],merged_count:merged};
  }

  function boundsForFloor(floor) {
    const points = [];
    arrays(floor && floor.walls).forEach((wall) => points.push(wall.start, wall.end));
    arrays(floor && floor.obstacles).forEach((item) => points.push(...arrays(item.footprint)));
    if (!points.length) return { minX: -5000, minY: -3500, maxX: 5000, maxY: 3500, width: 10000, height: 7000 };
    const xs = points.map((point) => Number(point.x)).filter(Number.isFinite);
    const ys = points.map((point) => Number(point.y)).filter(Number.isFinite);
    if (!xs.length || !ys.length) return { minX: -5000, minY: -3500, maxX: 5000, maxY: 3500, width: 10000, height: 7000 };
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    return { minX, minY, maxX, maxY, width: Math.max(1000, maxX - minX), height: Math.max(1000, maxY - minY) };
  }

  function sourceToWorld(calibration, sourcePoint) {
    if (!calibration) return null;
    const a = calibration.source_a;
    const b = calibration.source_b;
    const origin = calibration.origin_mm;
    const dx = Number(b.x) - Number(a.x);
    const dyRaw = Number(b.y) - Number(a.y);
    const dy = calibration.y_axis_down ? -dyRaw : dyRaw;
    const pixelDistance = Math.hypot(dx, dy);
    if (!(pixelDistance > 0) || !(Number(calibration.distance_mm) > 0)) return null;
    const scale = Number(calibration.distance_mm) / pixelDistance;
    const cos = dx / pixelDistance;
    const sin = dy / pixelDistance;
    const px = Number(sourcePoint.x) - Number(a.x);
    const py = calibration.y_axis_down ? -(Number(sourcePoint.y) - Number(a.y)) : Number(sourcePoint.y) - Number(a.y);
    return {
      x: Number(origin.x) + (px * cos + py * sin) * scale,
      y: Number(origin.y) + (-px * sin + py * cos) * scale,
    };
  }

  function finishedLevels(floor) {
    const get=kind=>arrays(floor?.levels).filter(level=>level.kind===kind);
    const floorLevels=get("finished_floor"),ceilingLevels=get("finished_ceiling");
    return {floor:floorLevels.length===1?floorLevels[0].elevation_mm:null,ceiling:ceilingLevels.length===1?ceilingLevels[0].elevation_mm:null,ambiguous:floorLevels.length>1||ceilingLevels.length>1};
  }
  function setFinishedLevels(project,floorId,values) {
    const floor=findFloor(project,floorId)?.floor;if(!floor)throw new Error("Этаж не найден.");
    if(finishedLevels(floor).ambiguous)throw new Error("На этаже несколько одноимённых чистовых уровней. Уточните их в JSON, чтобы не потерять разные отметки.");
    for(const key of ["floor","ceiling"])if(values[key]!==null&&(typeof values[key]!=="number"||!Number.isFinite(values[key])||Math.abs(values[key])>1e9))throw new Error("Чистовая отметка должна быть числом или пустым значением.");
    if(values.floor!==null&&values.ceiling!==null&&values.ceiling<=values.floor)throw new Error("Чистый потолок должен находиться выше чистого пола.");
    const next=arrays(floor.levels).map(clone);
    for(const [key,kind,name] of [["floor","finished_floor","у.ч.п."],["ceiling","finished_ceiling","ур. чист. пот."]]){
      const existing=next.find(level=>level.kind===kind),value=values[key];
      if(existing&&existing.elevation_mm!==value)guardDimensionChange(project,[existing.id],["elevation_mm"],name,"изменить");
      if(value===null){if(existing)next.splice(next.indexOf(existing),1);}
      else if(existing)existing.elevation_mm=value;
      else next.push({id:id(),properties:{},name,kind,elevation_mm:value});
    }
    floor.levels=next;return floor;
  }

  function sourceToDisplay(point, coordinateKind) {
    return coordinateKind === "dxf"
      ? { x: Number(point.x), y: -Number(point.y) }
      : { x: Number(point.x), y: Number(point.y) };
  }

  function displayToSource(point, coordinateKind) {
    return sourceToDisplay(point, coordinateKind);
  }

  return {
    clone,
    id,
    arrays,
    floorEntries,
    findFloor,
    floorCreationDefaults,
    addFloor,
    allEntities,
    findEntity,
    signedArea,
    area,
    perimeter,
    ccw,
    roomPolygon,
    createPolygonRoom,
    createPartition,
    splitWall,
    splitWallsAtIntersections,
    openingPlacement,
    createOpeningAt,
    updateOpening,
    doorDimensionWarnings,
    rectangleRoomGeometry,
    resizeRectangleRoom,
    finishedLevels,
    setFinishedLevels,
    wallLength,
    analyzeRoomMobility,
    translateRoomGeometry,
    deleteEntityCascade,
    deleteEntitiesCascade,
    removeCollinearVertex,
    boundsForFloor,
    sourceToWorld,
    sourceToDisplay,
    displayToSource,
  };
});
