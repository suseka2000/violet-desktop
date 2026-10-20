(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports ? require("./wall-mount.js") : root.VioletWallMount);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletBatchMount = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (W) {
  "use strict";
  const NS = "http://www.w3.org/2000/svg", list = value => Array.isArray(value) ? value : [], copy = value => JSON.parse(JSON.stringify(value));
  const equal = W.equal, key = entry => `${entry.wall_id}\u0000${entry.face_id}`;
  const mm = value => Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits: 2});
  function scopes(project, {includeExterior=false}={}) {
    const result = [];
    for (const building of list(project?.buildings)) for (const floor of list(building.floors)) {
      result.push({kind: "floor", id: floor.id, floor, label: `${building.name || "Здание"} · ${floor.name} · весь этаж`, height_mm: floor.default_height_mm || 3000});
      for (const room of list(floor.spaces)) result.push({kind: "space", id: room.id, floor, room, label: `${floor.name} · ${room.code || ""} ${room.name || "Помещение"}`.trim(), height_mm: room.height_mm || floor.default_height_mm || 3000});
    }
    if (includeExterior) for (const building of list(project?.buildings)) {
      result.push({kind:"exterior_building",id:building.id,building,floors:list(building.floors),label:`${building.name || "Здание"} · снаружи · все этажи`,height_mm:3000});
      for (const floor of list(building.floors)) result.push({kind:"exterior_floor",id:floor.id,floor,floors:[floor],label:`${floor.name || "Этаж"} · снаружи здания`,height_mm:floor.default_height_mm || 3000});
    }
    return result;
  }
  function findScope(project, scope) {return scopes(project,{includeExterior:true}).find(item => item.kind === scope?.kind && item.id === scope?.id) || null;}
  const point = value => [Number(value?.x), Number(value?.y)], distance = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1]), cross = (a, b) => a[0] * b[1] - a[1] * b[0], subtract = (a, b) => [a[0] - b[0], a[1] - b[1]];
  function pointDistance(p, a, b) {const delta = subtract(b, a), size = delta[0] ** 2 + delta[1] ** 2, t = size ? Math.max(0, Math.min(1, ((p[0] - a[0]) * delta[0] + (p[1] - a[1]) * delta[1]) / size)) : 0; return distance(p, [a[0] + t * delta[0], a[1] + t * delta[1]]);}
  function segmentKind(a, b, c, d, tolerance) {
    const ab = subtract(b, a), cd = subtract(d, c), al = distance(a, b), cl = distance(c, d), denominator = cross(ab, cd);
    if (al <= tolerance || cl <= tolerance) return "none";
    if (Math.abs(denominator) <= tolerance * Math.max(al, cl, 1)) {
      if ([c, d].some(p => Math.abs(cross(subtract(p, a), ab)) / al > tolerance) || [a, b].some(p => Math.abs(cross(subtract(p, c), cd)) / cl > tolerance)) return "none";
      const projection = p => ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / al, overlap = Math.min(al, Math.max(projection(c), projection(d))) - Math.max(0, Math.min(projection(c), projection(d)));
      return overlap > tolerance ? "collinear_overlap" : overlap >= -tolerance ? "touch" : "none";
    }
    const ac = subtract(c, a), t = cross(ac, cd) / denominator, u = cross(ac, ab) / denominator, te = tolerance / al, ue = tolerance / cl;
    if (t < -te || t > 1 + te || u < -ue || u > 1 + ue) return "none";
    return t > te && t < 1 - te && u > ue && u < 1 - ue ? "proper" : "touch";
  }
  function pointInRoom(p, vertices, tolerance) {
    let inside = false;
    for (let i = 0; i < vertices.length; i++) {const a = vertices[i], b = vertices[(i + 1) % vertices.length]; if (pointDistance(p, a, b) <= tolerance) return -1; if ((a[1] > p[1]) !== (b[1] > p[1]) && a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]) > p[0]) inside = !inside;}
    return inside ? 1 : 0;
  }
  function roomGeometry(floor, room, tolerance) {
    const edges = list(room.boundary), walls = edges.map(edge => list(floor.walls).find(wall => wall.id === edge.wall_id));
    if (edges.length < 3 || edges.length > 1000 || walls.some(wall => !wall) || new Set(walls.map(wall => wall.id)).size !== walls.length) throw Error("Некорректный контур");
    const segments = walls.map((wall, i) => edges[i].reversed ? [point(wall.end), point(wall.start)] : [point(wall.start), point(wall.end)]), vertices = segments.map(segment => segment[0]);
    if (segments.some(([a, b], i) => ![...a, ...b].every(Number.isFinite) || distance(a, b) <= tolerance || distance(b, segments[(i + 1) % segments.length][0]) > tolerance)) throw Error("Незамкнутый контур");
    const area = vertices.reduce((sum, a, i) => sum + cross(subtract(a, vertices[0]), subtract(vertices[(i + 1) % vertices.length], vertices[0])), 0) / 2;
    if (Math.abs(area) <= tolerance * tolerance) throw Error("Нулевая площадь");
    segments.forEach(([a, b], i) => segments.forEach(([c, d], j) => {if (j > i + 1 && !(i === 0 && j === segments.length - 1) && segmentKind(a, b, c, d, tolerance) !== "none") throw Error("Пересекающийся контур");}));
    const faces = walls.map((wall, i) => {const side = (area > 0) !== Boolean(edges[i].reversed) ? "left" : "right", matches = list(wall.faces).filter(face => face.side === side && face.space_id === room.id); if (matches.length !== 1) throw Error("Не определена внутренняя сторона"); return matches[0];});
    return {walls, faces, vertices};
  }
  function wallRoomRelation(wall, geometry, tolerance) {
    const a = point(wall.start), b = point(wall.end), delta = subtract(b, a), length = distance(a, b);
    if (![...a, ...b].every(Number.isFinite) || length <= tolerance) return "degenerate";
    const parameters = new Set([0, 1]), vertices = geometry.vertices; let touching = false;
    for (let i = 0; i < vertices.length; i++) {const c = vertices[i], d = vertices[(i + 1) % vertices.length], kind = segmentKind(a, b, c, d, tolerance); if (kind === "collinear_overlap") return "boundary_overlap"; if (kind === "proper") return "crosses_boundary"; touching ||= kind === "touch"; if (pointDistance(c, a, b) <= tolerance) parameters.add(Math.max(0, Math.min(1, ((c[0] - a[0]) * delta[0] + (c[1] - a[1]) * delta[1]) / (length * length))));}
    const ordered = [...parameters].sort((a, b) => a - b), samples = [];
    for (let i = 1; i < ordered.length; i++) if ((ordered[i] - ordered[i - 1]) * length > tolerance) {const t = (ordered[i] + ordered[i - 1]) / 2; samples.push(pointInRoom([a[0] + delta[0] * t, a[1] + delta[1] * t], vertices, tolerance));}
    const ends = [a, b].map(p => pointInRoom(p, vertices, tolerance));
    if (samples.length && samples.every(value => value === 1) && ends.every(value => value !== 0)) return "inside";
    return !touching && [...samples, ...ends].every(value => value === 0) ? "outside" : "crosses_boundary";
  }
  function targetKeys(project, scope) {
    const found = findScope(project, scope), result = new Set(); if (!found) return result;
    if (scope.kind.startsWith("exterior_")) {
      // This is only a physical scope guard. The server classifies the closed
      // envelope and returns exact scope_targets; free faces are NOT facades.
      for (const floor of found.floors) for (const wall of list(floor.walls)) for (const face of list(wall.faces)) if (!face.space_id) result.add(key({wall_id:wall.id,face_id:face.id}));
      return result;
    }
    const rooms = found.kind === "space" ? [found.room] : list(found.floor.spaces), tolerance = Number(project.settings?.geometry_tolerance_mm ?? .1), walls = list(found.floor.walls);
    if (!Number.isFinite(tolerance) || tolerance <= 0 || new Set(walls.map(wall => wall.id)).size !== walls.length) return result;
    let geometries; try {geometries = rooms.map(room => ({room, geometry: roomGeometry(found.floor, room, tolerance)}));} catch (_error) {return result;}
    geometries.forEach(({geometry}) => geometry.walls.forEach((wall, i) => result.add(key({wall_id: wall.id, face_id: geometry.faces[i].id}))));
    const boundaryIds = new Set(list(found.floor.spaces).flatMap(room => list(room.boundary).map(edge => edge.wall_id)));
    walls.filter(wall => !boundaryIds.has(wall.id)).forEach(wall => {
      const relations = geometries.map(({room, geometry}) => ({room, relation: wallRoomRelation(wall, geometry, tolerance)}));
      list(wall.faces).forEach(face => {
        const matches = relations.filter(item => item.relation === "inside" && (!face.space_id || face.space_id === item.room.id));
        if (matches.length === 1 || (!matches.length && relations.every(item => item.relation === "outside") && found.kind === "floor" && wall.properties?.editor_kind === "partition" && !face.space_id && distance(point(wall.start), point(wall.end)) > tolerance)) result.add(key({wall_id: wall.id, face_id: face.id}));
      });
    });
    return result;
  }
  function removalTargets(project, scope) {
    const found = findScope(project, scope); if (!found) return [];
    if (scope.kind.startsWith("exterior_")) {
      const wallIds=new Set(found.floors.flatMap(floor=>list(floor.walls).map(wall=>wall.id)));
      return list(project.properties?.wall_mounting?.walls).filter(entry=>wallIds.has(entry.wall_id)&&entry.configuration?.installation_zone==="exterior").map(entry=>({wall_id:entry.wall_id,face_id:entry.face_id}));
    }
    const wallIds = new Set(list(found.floor.walls).map(wall => wall.id)), geometricTargets = targetKeys(project, scope);
    return list(project.properties?.wall_mounting?.walls).filter(entry => {
      if (!wallIds.has(entry.wall_id)) return false;
      // Floor reset includes manually assigned exterior faces and partitions too,
      // even when a changed room contour no longer passes the mounting planner.
      if (scope.kind === "floor") return true;
      const wall = list(found.floor.walls).find(wall => wall.id === entry.wall_id), face = list(wall?.faces).find(face => face.id === entry.face_id);
      return face?.space_id === scope.id || geometricTargets.has(key(entry));
    }).map(entry => ({wall_id: entry.wall_id, face_id: entry.face_id}));
  }
  function simpleRoomPreset(height = 3000) {return W.configuration({material: "veneer", panel_thickness_mm: 12, panel_width_mm: 600, panel_height_mm: height, joint_mm: 2, mounting_system: "open_mount", panel_edge: "V-0", floor_gap_mm: 0, ceiling_gap_mm: 0, allow_project_assumptions: true});}
  function stepRoomPreset(height = 3000) {return W.configuration({...simpleRoomPreset(height), floor_gap_mm: 59, ceiling_gap_mm: 1});}
  function concealedRoomPreset(height = 3000, wideJoint = false) {return W.configuration({...simpleRoomPreset(height), mounting_system: "concealed_mount", panel_edge: "Vb", joint_mm: wideJoint ? 17 : 4, floor_gap_mm: 2, ceiling_gap_mm: 8});}
  function projectKey(project) {return W.stableKey(project);}
  function calculationKey(project) {return projectKey({...project, properties: {...project?.properties, mount_assemblies: undefined}});}
  function preparedMounting(project, data, scope, replaceExisting) {
    if (data?.schema_version !== "wall-mount-batch/1.0" || !equal(data.scope, scope) || !data.snapshot?.snapshot_id || !Array.isArray(data.entries) || !Array.isArray(data.retained_entries)) throw new Error("Расчёт не соответствует выбранной области или не содержит методичек.");
    const old = project.properties?.wall_mounting;
    if (old && (old.schema_version !== "wall-mounting/1.0" || !Array.isArray(old.walls))) throw new Error("У сохранённого монтажа неизвестный формат. Ничего не заменено.");
    if (old && !equal(old.snapshot, data.snapshot)) throw new Error("Зафиксированные методички изменились. Пересчитайте монтаж.");
    const valid = targetKeys(project, scope), incoming = new Set(), previous = new Map(list(old?.walls).map(entry => [key(entry), entry]));
    if (scope.kind.startsWith("exterior_")) {
      if (!Array.isArray(data.scope_targets) || data.scope_targets.some(entry=>!valid.has(key(entry))||entry.inclusion_reason!=="exterior_envelope")) throw Error("Не подтверждён наружный контур. Повторите расчёт.");
      const classified=new Set(data.scope_targets.map(key));
      if (classified.size!==data.scope_targets.length || data.entries.some(entry=>!classified.has(key(entry))||entry.configuration?.installation_zone!=="exterior")) throw Error("Расчёт содержит сторону вне наружного контура.");
    }
    data.entries.forEach(entry => {const id = key(entry); if (!valid.has(id) || incoming.has(id) || !entry.configuration || !entry.geometry_signature) throw new Error("В расчёте есть повторная, неизвестная или неполная сторона стены."); if (previous.has(id) && !replaceExisting) throw new Error("Замена ранее настроенных стен не разрешена. Они не изменены."); incoming.add(id);});
    const expected = list(old?.walls).filter(entry => !incoming.has(key(entry))), retained = new Map(data.retained_entries.map(entry => [key(entry), entry]));
    const semanticEntry = entry => entry && ({wall_id: entry.wall_id, face_id: entry.face_id, configuration: W.configuration(entry.configuration), geometry_signature: entry.geometry_signature ?? null});
    if (retained.size !== data.retained_entries.length || expected.length !== retained.size || expected.some(entry => !equal(semanticEntry(retained.get(key(entry))), semanticEntry(entry)))) throw new Error("Другие назначения монтажа изменились. Пересчитайте монтаж, чтобы ничего не потерять.");
    return {schema_version: "wall-mounting/1.0", snapshot: copy(data.snapshot), walls: [...data.retained_entries, ...data.entries].map(copy)};
  }

  function create(host, options) {
    const make = (tag, cls, text) => {const element = document.createElement(tag); if (cls) element.className = cls; if (text !== undefined) element.textContent = text; return element;};
    const svg = (tag, attrs) => {const element = document.createElementNS(NS, tag); Object.entries(attrs || {}).forEach(([name, value]) => element.setAttribute(name, String(value))); return element;};
    const button = (text, action, cls = "button button-secondary") => {const element = make("button", cls, text); element.type = "button"; element.addEventListener("click", action); return element;};
    const field = (text, control, hint) => {const element = make("label", "wm-field"); element.append(make("span", "", text), control); control.setAttribute("aria-label", text); if (hint) element.append(make("small", "wm-help", hint)); return element;};
    const select = items => {const element = make("select"); items.forEach(([value, text]) => {const option = make("option", "", text); option.value = value; element.append(option);}); return element;};
    const input = (min, max, step = 1) => {const element = make("input"); element.type = "number"; element.min = min; element.max = max; element.step = step; return element;};
    let alive = true, opened = false, projectId = null, sequence = 0, timer = null, pending = false, applying = false, baseline = null, knownProject = "", knownCalculation = "", resultKey = "", result = null, applied = false, inspectLinks = [];
    const shell = make("section", "batch-mount-workspace"), header = make("header", "wm-header"), heading = make("div");
    heading.append(make("h2", "", "Монтаж · внутри и снаружи"), make("p", "wm-help", "Единые параметры панелей, раскладка всех стен и применение одной правкой."));
    header.append(heading, button("Закрыть", () => close()));
    const grid = make("div", "bm-grid"), rail = make("div", "wm-rail"), fields = make("div", "wm-fields"), main = make("div", "wm-main"), scope = select([]);
    const material = select([["veneer", "Шпон"], ["hpl", "HPL"], ["painted", "Окрашенная панель"], ["unspecified", "Материал не выбран"]]);
    const thickness = input(1, 200, .1), width = input(10, 100000), height = input(10, 100000), joint = input(0, 100, .1), floorGap = input(0, 10000), ceilingGap = input(0, 10000);
    const system = select([["open_mount", "Открытое крепление"], ["concealed_mount", "Скрытое крепление"]]), edge = select([["", "Не выбрана"], ["V-0", "V-0 · прямой край"], ["Va", "Va · прямой край"], ["Vb", "Vb · паз"], ["Vc", "Vc · паз"], ["Vd", "Vd · паз"]]);
    const mode = select([["project", "С проектными настройками"], ["strict", "По условиям методички"]]), replace = make("input"), presetNote = make("p", "bm-preset-note"); replace.type = "checkbox"; replace.checked = false; replace.setAttribute("aria-label", "Заменить уже настроенные стороны стен");
    const replaceLabel = make("label", "bm-replace"); replaceLabel.append(replace, make("span", "", "Заменить уже настроенные стороны стен"));
    const gaps = make("details", "wm-panel-extra"); gaps.append(make("summary", "", "Зазоры у пола и потолка"), field("Зазор у пола, мм", floorGap), field("Зазор у потолка, мм", ceilingGap));
    fields.append(make("h3", "wm-step", "1. Где выполняем монтаж"), field("Область монтажа", scope), make("p", "wm-help", "Помещение / весь этаж — монтаж внутри, включая перегородки. «Снаружи здания» — только наружные стороны замкнутого контура стен; внутренний монтаж не изменится. Можно выбрать один этаж или всё здание. Для двора и незамкнутого контура сторону укажите вручную в монтаже стены."), make("h3", "wm-step", "2. Общие параметры панелей"), button("Простая комната", loadPreset, "mini-button"), presetNote, field("Материал панели", material), field("Толщина панели, мм", thickness), field("Ширина панели, мм", width), field("Высота панели, мм", height), field("Шов между панелями, мм", joint), field("Кромка / паз панели", edge), gaps, make("h3", "wm-step", "3. Автоматический подбор"), field("Способ крепления", system), field("Режим подбора", mode), make("p", "wm-help", "Проектные настройки дополняют условия методички при выборе узла. Основания выбора и необходимые уточнения — в примечаниях справа."), replaceLabel, make("p", "wm-help", "По умолчанию существующие назначения пропускаются. Флажок явно разрешает заменить их в выбранной области."));
    const stepPresetButton = button("Комната со STEP-узлами", () => loadPreset("step"), "mini-button");
    fields.insertBefore(stepPresetButton, presetNote);
    fields.insertBefore(button("Скрытый монтаж · VH-2", () => loadPreset("concealed2"), "mini-button"), presetNote);
    fields.insertBefore(button("Скрытый монтаж · VH-3", () => loadPreset("concealed3"), "mini-button"), presetNote);
    const applyButton = button("Применить ко всем стенам", apply, "button button-primary"), reset = button("Сбросить правки", resetChanges, "mini-button"), prepareSections = options.onPrepareSections ? button("Сечения STEP", () => {if (!canPrepareSections()) {status.textContent = "Сначала примените монтаж в выбранной области и завершите местные правки."; return;} const selectedScope = scopeValue(); if (close()) options.onPrepareSections(selectedScope);}, "mini-button") : null, actions = make("div", "wm-actions"); actions.append(applyButton, reset); if (prepareSections) actions.append(prepareSections); rail.append(fields, actions, make("p", "wm-help wm-save-help", "Одна отменяемая правка. После применения сохраните проект."));
    const prepareAssemblies = options.onPrepareAssemblies ? button("Сборки узлов STEP", () => {if (!canPrepareAssemblies()) {status.textContent = "Сначала примените показанный монтаж и завершите местные правки. Сборки строятся по применённым параметрам."; return;} const selectedScope = scopeValue(); if (close()) options.onPrepareAssemblies(selectedScope);}, "mini-button") : null;
    if (prepareAssemblies) {actions.append(prepareAssemblies); prepareAssemblies.title = "Реальные профили и панели в одной сборке; сечения отдельных STEP остаются доступны отдельно";}
    const controls = make("div", "wm-view-header"), total = make("strong", "bm-total"), recalculate = button("Пересчитать", preview, "mini-button"); controls.append(total, recalculate);
    const status = make("p", "wm-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const summary = make("div", "bm-summary"), cards = make("div", "bm-cards"), warnings = make("div", "bm-warnings");
    const notes = make("details", "wm-requirement-list"); notes.append(make("summary", "", "Примечания к раскладке"), make("p", "wm-help", "На стенах показаны участки панелей и места узлов. Сечения и состав смотрите в «Сборках узлов STEP». Размеры заготовок, крепёж и нагрузки уточняются отдельно."));
    main.append(controls, status, summary, notes, cards, warnings); grid.append(rail, main);
    const confirm = make("div", "wm-close-confirm"); confirm.hidden = true; confirm.setAttribute("role", "alert"); confirm.append(make("p", "", "Параметры изменены, но массовый монтаж ещё не применён."), button("Вернуться к настройке", () => {confirm.hidden = true;}), button("Закрыть без применения", () => close({force: true})));
    shell.append(header, grid, confirm); host.replaceChildren(shell);
    const current = () => options.getProject?.();
    const removal = W.createRemovalControl(actions, {
      getProject: current, getTargets: () => removalTargets(current(), scopeValue()),
      label: "Удалить монтаж выбранной области…", description: "Удалить весь монтаж выбранного помещения / этажа",
      mutationLabel: "удаление монтажа помещения или этажа", mutate: options.mutate, toast: options.toast,
      blockReason: () => !opened || current()?.id !== projectId ? "Откройте монтаж нужного проекта заново." : "",
      onRemoved: count => {invalidate(); baseline = values(); knownProject = projectKey(current()); knownCalculation = calculationKey(current()); cards.replaceChildren(); summary.replaceChildren(); warnings.replaceChildren(); total.textContent = "Монтаж удалён"; cards.setAttribute("aria-busy", "false"); status.textContent = `Монтаж снят со сторон стен: ${count}. Для нового монтажа выберите параметры и нажмите «Пересчитать».`; updateActions();},
    });
    function scopeValue() {const value = scope.value, colon = value.indexOf(":"); return {kind: value.slice(0, colon), id: value.slice(colon + 1)};}
    function values() {return [scope.value, material.value, thickness.value, width.value, height.value, joint.value, edge.value, system.value, mode.value, floorGap.value, ceilingGap.value, replace.checked];}
    function readConfiguration() {
      for (const element of [thickness, width, height, joint, floorGap, ceilingGap]) if (!String(element.value).trim() || !Number.isFinite(Number(element.value))) throw new Error("Заполните размеры числами в миллиметрах.");
      return W.configuration({installation_zone:scopeValue().kind.startsWith("exterior_")?"exterior":"interior", material: material.value, panel_thickness_mm: Number(thickness.value), panel_width_mm: Number(width.value), panel_height_mm: Number(height.value), joint_mm: Number(joint.value), mounting_system: system.value, panel_edge: edge.value || null, floor_gap_mm: Number(floorGap.value), ceiling_gap_mm: Number(ceilingGap.value), assembly_rule_id: null, allow_project_assumptions: mode.value === "project"});
    }
    function fill(config) {material.value = config.material; thickness.value = String(config.panel_thickness_mm); width.value = String(config.panel_width_mm); height.value = String(config.panel_height_mm); joint.value = String(config.joint_mm); edge.value = config.panel_edge || ""; system.value = config.mounting_system; mode.value = config.allow_project_assumptions ? "project" : "strict"; floorGap.value = String(config.floor_gap_mm); ceilingGap.value = String(config.ceiling_gap_mm);}
    function hasDraft() {return opened && baseline !== null && !equal(values(), baseline);}
    function canPrepareSections() {if (!opened || pending || hasDraft() || current()?.id !== projectId) return false; const targets = targetKeys(current(), scopeValue()); return list(current()?.properties?.wall_mounting?.walls).some(entry => targets.has(key(entry)));}
    function canPrepareAssemblies() {return canPrepareSections() && Boolean(result) && (applied || !list(result.entries).length);}
    function inspectBlockReason(wallId, faceId) {
      if (!opened || current()?.id !== projectId) return "Откройте монтаж текущего проекта заново.";
      if (pending) return "Дождитесь завершения расчёта.";
      if (hasDraft()) return "Сначала примените массовый монтаж или сбросьте изменения параметров.";
      if ((!applied && list(result?.entries).some(entry => entry.wall_id === wallId && entry.face_id === faceId)) || !W.storedEntry(current(), wallId, faceId)) return "Сначала примените раскладку: показанный монтаж ещё не записан на эту сторону стены.";
      return "";
    }
    function inspectLink(wallId, faceId) {
      const wrap = make("div", "bm-inspect"), hint = make("p", "wm-help"), control = button("Открыть стену", () => {const reason = inspectBlockReason(wallId, faceId); if (reason) {status.textContent = reason; return;} if (close()) options.onInspectWall(wallId, faceId);}, "mini-button");
      inspectLinks.push({wallId, faceId, control, hint}); wrap.append(control, hint); return wrap;
    }
    function updateActions() {applyButton.disabled = !result || pending || applied || !list(result.entries).length; reset.disabled = !hasDraft(); recalculate.disabled = pending; if (prepareSections) prepareSections.disabled = !canPrepareSections(); if (prepareAssemblies) prepareAssemblies.disabled = !canPrepareAssemblies(); removal.refresh(); inspectLinks.forEach(item => {const reason = inspectBlockReason(item.wallId, item.faceId); item.control.disabled = Boolean(reason); item.control.title = reason || "Открыть применённый монтаж этой стороны стены"; item.hint.textContent = reason; item.hint.hidden = !reason;});}
    function invalidate() {sequence++; clearTimeout(timer); pending = false; result = null; applied = false; confirm.hidden = true; cards.setAttribute("aria-busy", "true"); updateActions();}
    function changed(event) {if (!opened) return; removal.cancel(); invalidate(); if (event?.target !== scope) presetNote.textContent = "Свои параметры · размеры заданы пользователем, не взяты из методички."; status.textContent = "Обновляем все стены…"; timer = setTimeout(preview, 350);}
    [scope, material, edge, system, mode, replace].forEach(element => element.addEventListener("change", changed));
    [thickness, width, height, joint, floorGap, ceilingGap].forEach(element => element.addEventListener("input", changed));
    function loadPreset(kind) {
      const found = findScope(current(), scopeValue()), roomHeight = found?.height_mm || 3000, stepPreset = kind === "step", concealed = kind === "concealed2" || kind === "concealed3";
      fill(concealed ? concealedRoomPreset(roomHeight, kind === "concealed3") : (stepPreset ? stepRoomPreset : simpleRoomPreset)(roomHeight));
      presetNote.textContent = concealed ? `«Скрытый монтаж · ${kind === "concealed3" ? "VH-3" : "VH-2"}»: шпон 12 мм, паз Vb, шов ${kind === "concealed3" ? "17" : "4"} мм; зазор у пола 2 мм и у потолка 8 мм. Проектные настройки под имеющиеся STEP. После применения монтажа проверьте пазы и посадки в окне сборок.` : stepPreset ? "«Комната со STEP-узлами»: шпон 12 мм, шов 2 мм, V-0, открытое крепление; зазор у пола 59 мм и у потолка 1 мм. Зазоры подобраны для имеющихся STEP-профилей. Примените монтаж перед построением сборок." : "«Простая комната»: шпон, 12 × 600 мм, высота помещения, шов 2 мм, V-0, открытое крепление, нулевые зазоры. Стартовые настройки можно изменить под проект.";
      if (stepPreset || concealed) gaps.open = true; if (opened) {invalidate(); preview();}
    }
    function resetChanges() {if (!baseline) return; const old = baseline; scope.value = old[0]; material.value = old[1]; thickness.value = old[2]; width.value = old[3]; height.value = old[4]; joint.value = old[5]; edge.value = old[6]; system.value = old[7]; mode.value = old[8]; floorGap.value = old[9]; ceilingGap.value = old[10]; replace.checked = old[11]; invalidate(); preview();}
    async function preview() {
      clearTimeout(timer); if (!alive || !opened) return;
      let config; try {config = readConfiguration();} catch (error) {invalidate(); cards.replaceChildren(); cards.setAttribute("aria-busy", "false"); status.textContent = error.message; return;}
      const project = current(), selectedScope = scopeValue(); if (project?.id !== projectId || !findScope(project, selectedScope)) {invalidate(); status.textContent = "Проект или область монтажа изменились. Откройте окно заново."; return;}
      const token = ++sequence, fingerprint = projectKey(project), inputKey = JSON.stringify(values()), replaceValue = replace.checked;
      result = null; pending = true; applied = false; knownProject = fingerprint; knownCalculation = calculationKey(project); updateActions(); cards.setAttribute("aria-busy", "true"); status.textContent = "Раскладываем панели, подбираем узлы и собираем допущения по всем стенам…";
      try {
        const data = await options.request("/wall-mount/batch-preview", {method: "POST", body: {project: W.requestProject(project), scope: selectedScope, configuration: config, replace_existing: replaceValue}});
        if (!alive || !opened || token !== sequence || current()?.id !== projectId || projectKey(current()) !== fingerprint || JSON.stringify(values()) !== inputKey) return;
        preparedMounting(project, data, selectedScope, replaceValue); result = data; resultKey = fingerprint; render(data); status.textContent = list(data.entries).length ? "Предпросмотр готов. Проверьте стены и допущения, затем примените всё одной правкой." : "Новых назначений нет: все подходящие стороны уже настроены либо область не содержит подходящих стен.";
      } catch (error) {if (alive && opened && token === sequence) {result = null; cards.replaceChildren(); summary.replaceChildren(); warnings.replaceChildren(); total.textContent = "Расчёт не завершён"; status.textContent = `Монтаж не применён: ${error.message}`;}}
      finally {if (alive && opened && token === sequence) {pending = false; cards.setAttribute("aria-busy", "false"); updateActions();}}
    }
    function render(data) {
      inspectLinks = [];
      const info = data.summary || {}; total.textContent = `Сторон стен: ${info.target_count ?? list(data.previews).length} · к применению: ${data.entries.length}`;
      summary.replaceChildren(); [["Панелей", info.panel_count], ["Площадь", `${mm(info.area_m2)} м²`], ["Условных узлов", info.node_count], ["Сохранено прежних", info.skipped_count ?? list(data.skipped).length]].forEach(([label, value]) => {const item = make("div", "bm-stat"); item.append(make("strong", "", String(value ?? "—")), make("span", "", label)); summary.append(item);});
      cards.replaceChildren(); list(data.previews).forEach(item => {
        const card = make("article", "bm-wall-card"), wall = item.wall || {}, title = make("div", "bm-wall-title"), context = W.wallContext(current(), wall.id), room = list(context?.floor.spaces).find(room => room.id === (item.scope_context?.space_id || wall.space_id));
        const inclusion = item.scope_context?.inclusion_reason, place = inclusion === "exterior_envelope" ? `${context?.floor.name || "Этаж"} · Наружная облицовка` : room ? `${room.code || ""} ${room.name || ""}`.trim() : inclusion === "standalone_partition" ? "Отдельная перегородка этажа" : "Без привязки к помещению";
        title.append(make("strong", "", wall.name || context?.wall.name || "Стена"), make("span", "wm-help", `${place}${inclusion === "partition_inside_room" ? " · внутренняя перегородка" : ""} · ${wall.side === "right" ? "правая сторона · B → A" : "левая сторона · A → B"}`)); card.append(title, miniPreview(item));
        card.append(make("p", "bm-wall-count", `${mm(wall.length_mm)} × ${mm(wall.height_mm)} мм · панелей: ${item.summary?.panel_count ?? list(item.panels).length} · ${mm(item.summary?.area_m2)} м²`));
        const candidate = list(item.candidates).find(candidate => candidate.rule_id === item.selected_rule_id); card.append(make("p", "bm-wall-rule", candidate ? `Узел: ${candidate.title}` : "Тип узла требует выбора"));
        if (item.selection_reason) card.append(make("p", "wm-help", item.selection_reason));
        const assumptionCount = list(item.assumptions).length; card.append(make("span", assumptionCount ? "bm-assumption-badge" : "wm-help", assumptionCount ? `Проектных допущений: ${assumptionCount}` : "Без дополнительных проектных допущений"));
        if (options.onInspectWall) card.append(inspectLink(wall.id, wall.face_id));
        cards.append(card);
      });
      warnings.replaceChildren();
      if (data.scope?.kind?.startsWith("exterior_")) warnings.append(make("p","wm-help",`Наружных сторон: ${info.exterior_faces || 0}. Внутренний монтаж сохранён. Дворы и незамкнутые контуры: назначьте нужную сторону через «Монтаж стены → Снаружи здания». Наружная применимость системы требует проверки.`));
      if (info.room_boundary_faces !== undefined) warnings.append(make("p", "wm-help", `Включены: стороны стен комнат — ${info.room_boundary_faces}; стороны внутренних перегородок — ${info.interior_partition_faces || 0}; стороны отдельных перегородок этажа — ${info.standalone_partition_faces || 0}.`));
      if (list(data.assumptions).length) {const box = make("details", "wm-assumptions"); box.append(make("summary", "", `Примечания к подбору · ${data.assumptions.length}`)); data.assumptions.forEach(item => {box.append(make("p", "", typeof item === "string" ? item : item.message)); if (item.required_check) box.append(make("p", "wm-help", `Проверить: ${item.required_check}`));}); warnings.append(box);}
      if (list(data.skipped).length) {const box = make("details", "wm-requirement-list"); box.append(make("summary", "", `Оставлены без изменений · ${data.skipped.length}`)); data.skipped.forEach(item => {const wall = W.wallContext(current(), item.wall_id)?.wall; box.append(make("p", "", `${wall?.name || "Стена"}: ${item.reason === "existing_preserved" ? "существующее назначение сохранено" : item.reason || "уже настроена"}`)); if (options.onInspectWall && W.storedEntry(current(), item.wall_id, item.face_id)) box.append(inspectLink(item.wall_id, item.face_id));}); warnings.append(box);}
      if (list(data.issues).length) {const grouped = new Map(); data.issues.forEach(issue => {if (!issue.message) return; const id = `${issue.code}:${issue.message}`, entry = grouped.get(id) || {message: issue.message, walls: new Set()}; list(issue.entity_ids).forEach(id => {if (W.wallContext(current(), id)) entry.walls.add(id);}); grouped.set(id, entry);}); const box = make("details", "wm-warning"); box.append(make("summary", "", `Требования и замечания · ${grouped.size}`)); grouped.forEach(item => box.append(make("p", "", `${item.message}${item.walls.size > 1 ? ` Стен: ${item.walls.size}.` : ""}`))); warnings.append(box);}
      if (list(data.excluded).length) {const reasons = {open_or_internal_outline: "Внутренняя сторона или незамкнутый наружный контур: автоматический фасад не назначен.", overlapping_exterior_wall: "Стены накладываются друг на друга. Устраните дубли или выберите сторону вручную.", exterior_space_assignment_conflict: "Наружная сторона уже связана с помещением. Проверьте назначение стороны.", crosses_room_boundary: "Стена пересекает или повторяет контур комнаты. Разделите её на участки перед монтажом.", ambiguous_room: "Стена попала в несколько перекрывающихся помещений. Уточните контуры или привязку.", space_assignment_conflict: "Привязка стороны к помещению противоречит её положению.", degenerate_wall: "Длина стены меньше допуска.", outside_scope: "Вне выбранной комнаты.", unclassified_outside_wall: "Отдельная стена вне комнат: назначьте монтаж вручную."}, box = make("details", "wm-requirement-list"); box.append(make("summary", "", `Не включены отдельные стороны · ${data.excluded.length}`)); data.excluded.forEach(item => {const wall = W.wallContext(current(), item.wall_id)?.wall; box.append(make("p", "wm-help", `${wall?.name || "Стена"} · ${item.side === "right" ? "правая" : "левая"}: ${reasons[item.reason] || item.reason}`));}); warnings.append(box);}
    }
    function miniPreview(data) {
      const length = Number(data.wall.length_mm) || 1, height = Number(data.wall.height_mm) || 1, padding = Math.max(length, height) * .035, picture = svg("svg", {class: "bm-mini-preview", role: "img", "aria-label": `Предварительная раскладка: ${data.wall.name || "стена"}`, viewBox: `${-padding} ${-height - padding} ${length + padding * 2} ${height + padding * 2}`});
      picture.append(svg("rect", {x: 0, y: -height, width: length, height, class: "wm-wall-background"}));
      list(data.panels).forEach(panel => picture.append(svg("path", {d: W.polygonPath(panel.polygon || [[panel.x_mm, panel.y_mm], [panel.x_mm + panel.width_mm, panel.y_mm], [panel.x_mm + panel.width_mm, panel.y_mm + panel.height_mm], [panel.x_mm, panel.y_mm + panel.height_mm]], panel.holes), class: `wm-panel wm-material-${data.configuration?.material || "unspecified"}`, "fill-rule": "evenodd"})));
      list(data.openings).forEach(opening => picture.append(svg("rect", {x: opening.x_mm, y: -(opening.y_mm + opening.height_mm), width: opening.width_mm, height: opening.height_mm, class: "wm-opening"})));
      list(data.nodes).slice(0, 100).forEach(node => {if (![node.x_mm, node.y_mm].every(Number.isFinite)) return; const marker = svg("g", {class: `wm-node${node.decision_status === "project_assumption" ? " is-project-assumption" : node.decision_status === "source_reference" || node.status === "reference_candidate" ? "" : " is-unresolved"}`}); marker.append(svg("circle", {cx: node.x_mm, cy: -node.y_mm, r: Math.max(length, height) * .011})); picture.append(marker);}); return picture;
    }
    function apply() {
      if (!result || pending || applied || !opened || !list(result.entries).length) return false;
      if (current()?.id !== projectId || projectKey(current()) !== resultKey) {invalidate(); status.textContent = "Проект изменился. Пересчитайте, чтобы не потерять другие назначения монтажа."; return false;}
      try {
        const prepared = preparedMounting(current(), result, scopeValue(), replace.checked), expected = resultKey;
        applying = true; const changed = options.mutate("монтаж всех стен выбранной области", project => {if (project.id !== projectId || projectKey(project) !== expected) throw new Error("Проект изменился во время расчёта."); project.properties ||= {}; project.properties.wall_mounting = prepared;});
        if (changed === false) return false;
        baseline = values(); knownProject = projectKey(current()); knownCalculation = calculationKey(current()); resultKey = knownProject; applied = true; confirm.hidden = true; status.textContent = `Монтаж применён к ${result.entries.length} сторонам стен одной правкой. Сохраните проект; всю операцию можно отменить.`; options.toast?.("Монтаж выбранной области применён к черновику", "success"); updateActions();
        if (options.onApplied) {const appliedScope = copy(scopeValue()), appliedProject = projectId; Promise.resolve(options.onApplied(appliedScope, message => {if (alive && opened && projectId === appliedProject && equal(scopeValue(), appliedScope)) status.textContent = message;})).catch(error => {if (alive && opened && projectId === appliedProject) status.textContent = error.message;});}
        return true;
      } catch (error) {status.textContent = error.message; return false;} finally {applying = false;}
    }
    function refresh() {
      if (!alive || !opened || applying) return;
      const project = current(); if (project?.id !== projectId || !findScope(project, scopeValue())) {invalidate(); status.textContent = "Проект или выбранное помещение удалены/изменены. Закройте окно и выберите область заново."; return;}
      const fingerprint = projectKey(project); if (fingerprint !== knownProject) {
        const calculation = calculationKey(project);
        if (!pending && result && calculation === knownCalculation) {knownProject = fingerprint; resultKey = fingerprint; updateActions(); return;}
        invalidate(); knownProject = fingerprint; knownCalculation = calculation; status.textContent = "Проект изменился. Параметры сохранены в окне; нажмите «Пересчитать» перед применением.";
      }
    }
    function open(selectedScope) {
      if (!alive || (opened && hasDraft())) {if (opened) status.textContent = "Сначала примените или сбросьте текущие параметры."; return false;}
      const project = current(), found = findScope(project, selectedScope); if (!found) {options.toast?.("Выберите помещение или этаж для монтажа.", "error"); return false;}
      sequence++; clearTimeout(timer); removal.cancel(); opened = true; projectId = project.id; scope.replaceChildren(); scopes(project,{includeExterior:true}).forEach(item => {const option = make("option", "", item.label); option.value = `${item.kind}:${item.id}`; scope.append(option);}); scope.value = `${selectedScope.kind}:${selectedScope.id}`; replace.checked = false;
      fill(simpleRoomPreset(found.height_mm)); presetNote.textContent = "«Простая комната»: стартовые настройки размеров и материала. Измените их под свой проект; основания подбора перечислены в примечаниях."; baseline = values(); knownProject = projectKey(project); knownCalculation = calculationKey(project); result = null; applied = false; confirm.hidden = true; updateActions(); preview(); return true;
    }
    function close(settings = {}) {if (hasDraft() && !settings.force) {confirm.hidden = false; return false;} opened = false; sequence++; clearTimeout(timer); pending = false; confirm.hidden = true; options.onClose?.(); return true;}
    updateActions(); return {open, refresh, close, hasDraft, preview, apply, dispose() {alive = false; opened = false; sequence++; clearTimeout(timer);}};
  }
  return {scopes, findScope, targetKeys, removalTargets, simpleRoomPreset, stepRoomPreset, concealedRoomPreset, projectKey, preparedMounting, create};
});
