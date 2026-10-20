(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletWallMount = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const list = value => Array.isArray(value) ? value : [];
  const copy = value => JSON.parse(JSON.stringify(value));
  function requestProject(project, {keepLocalAssemblies=false}={}) {
    // A preview needs editable dimensions/settings, not every saved wall STEP.
    // Preserve explicit framing edits before dropping the disposable geometry.
    const properties={...project?.properties};
    const mounted=new Set(list(properties.wall_mounting?.walls).map(item=>`${item.wall_id}:${item.face_id}`));
    const settings=new Map(list(properties.whole_wall_settings?.walls).filter(item=>mounted.has(`${item.wall_id}:${item.face_id}`)).map(item=>[`${item.wall_id}:${item.face_id}`,item]));
    for(const item of list(properties.whole_wall_assemblies?.items)) {
      const key=`${item.wall_id}:${item.face_id}`;
      if(mounted.has(key)&&item.configuration&&!settings.has(key))settings.set(key,{wall_id:item.wall_id,face_id:item.face_id,configuration:item.configuration});
    }
    if(settings.size||properties.whole_wall_settings)properties.whole_wall_settings={schema_version:"whole-wall-settings/1.0",walls:[...settings.values()]};
    delete properties.whole_wall_assemblies;
    if(!keepLocalAssemblies)delete properties.mount_assemblies;
    return copy({...project,properties});
  }
  // SQLite writes canonical JSON while API models emit their declared field order.
  // Object order is not evidence identity; array order and every value still are.
  function stableKey(value) {
    if (globalThis.VioletProjectHistory) return globalThis.VioletProjectHistory.stableKey(value);
    return JSON.stringify(value, (_key, item) => item && typeof item === "object" && !Array.isArray(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  }
  const equal = (a, b) => stableKey(a) === stableKey(b);
  const number = value => Number(value).toLocaleString("ru-RU", {maximumFractionDigits: 2});
  const defaults = Object.freeze({material: "unspecified", panel_thickness_mm: 12, panel_width_mm: 600, panel_height_mm: 3000, joint_mm: 2, mounting_system: "open_mount", assembly_rule_id: null, panel_edge: null, floor_gap_mm: 0, ceiling_gap_mm: 0, allow_project_assumptions: false});
  const materialLabels = {veneer: "Шпон", hpl: "HPL", painted: "Окрашенная панель", unspecified: "Материал пока не выбран"};
  const kindLabels = {floor: "Примыкание к полу", ceiling: "Примыкание к потолку", joint: "Стык панелей", vertical_joint: "Вертикальный стык", horizontal_joint: "Горизонтальный стык", opening: "Обрамление проёма", opening_head: "Верх проёма", opening_sill: "Низ проёма", opening_jamb: "Бок проёма", left: "Левый край", right: "Правый край"};
  const groupLabels = Object.freeze({joints: "Стыки панелей", floor: "Пол", ceiling: "Потолок", corners: "Углы", openings: "Проёмы"});
  function nodeRules(value) {
    return Object.fromEntries(Object.keys(groupLabels).filter(key => typeof value?.[key] === "string" && value[key].trim()).map(key => [key, value[key]]));
  }
  function nodeGroup(node) {
    if (Object.hasOwn(groupLabels, node?.group_id)) return node.group_id;
    if (/joint/.test(node?.kind || "")) return "joints";
    if (/opening|door|window/.test(node?.kind || "")) return "openings";
    if (node?.kind === "floor" || node?.kind === "ceiling") return node.kind;
    return "corners";
  }
  function groupCandidateLabel(candidate, group) {
    const codes = [...new Set(list(candidate.component_codes).filter(code => typeof code === "string" && code))];
    // Joint pages still list the complete supporting frame as well as the joint
    // profile. Do not present that full-page BOM as an isolated joint kit.
    if (group === "joints" || !codes.length) return `${groupLabels[group]} · ${candidate.title || candidate.rule_id}`;
    return `${groupLabels[group]}: ${codes.join(", ")}${candidate.assembly_id ? ` · схема ${candidate.assembly_id}` : ""}`;
  }

  function wallContext(project, id) {
    for (const building of list(project?.buildings)) for (const floor of list(building.floors)) {
      const wall = list(floor.walls).find(item => item.id === id);
      if (wall) return {project, building, floor, wall, openings: list(floor.openings).filter(item => item.wall_id === id)};
    }
    return null;
  }
  function contextKey(context) {
    return context ? stableKey([context.project.id, context.wall, context.floor.walls, context.floor.openings, context.floor.spaces, context.floor.origin, context.floor.elevation_mm, context.floor.default_height_mm, context.project.properties?.wall_mounting?.snapshot?.snapshot_id || null]) : "";
  }
  function storedEntry(project, wallId, faceId) {
    return list(project?.properties?.wall_mounting?.walls).find(item => item.wall_id === wallId && item.face_id === faceId) || null;
  }
  function configuration(value, height = 3000) {
    const result = {...defaults, panel_height_mm: height, ...(value || {})};
    // Keep the wire contract explicit: never forward arbitrary project properties.
    const normalized = Object.fromEntries(Object.keys(defaults).map(key => [key, result[key]])), groups = nodeRules(value?.node_rule_ids);
    if (Object.keys(groups).length) normalized.node_rule_ids = groups;
    if (value?.installation_zone === "exterior") normalized.installation_zone = "exterior";
    return normalized;
  }
  function applyEntry(project, entry, snapshot) {
    if (!wallContext(project, entry.wall_id)?.wall.faces?.some(face => face.id === entry.face_id)) throw new Error("Стена или её сторона удалена. Откройте монтаж заново.");
    const old = project.properties?.wall_mounting;
    if (old && (old.schema_version !== "wall-mounting/1.0" || !Array.isArray(old.walls))) throw new Error("Сохранённый монтаж имеет неизвестный формат. Он не был заменён.");
    if (!snapshot || typeof snapshot !== "object" || !snapshot.snapshot_id) throw new Error("Расчёт не содержит зафиксированных методичек. Повторите построение.");
    if (old && !equal(old.snapshot, snapshot)) throw new Error("Зафиксированные методички изменились. Пересчитайте монтаж.");
    const walls = list(old?.walls).map(copy), index = walls.findIndex(item => item.wall_id === entry.wall_id && item.face_id === entry.face_id);
    if (index < 0) walls.push(copy(entry)); else walls[index] = copy(entry);
    project.properties ||= {};
    project.properties.wall_mounting = {schema_version: "wall-mounting/1.0", snapshot: copy(snapshot), walls};
  }
  const bindingKey = value => `${value.wall_id}\u0000${value.face_id}`;
  function removalPlan(project, targets) {
    const saved = project?.properties?.wall_mounting;
    if (saved && (saved.schema_version !== "wall-mounting/1.0" || !Array.isArray(saved.walls))) throw new Error("Неизвестный формат монтажа. Ничего не удалено.");
    const selected = new Set(list(targets).map(bindingKey));
    return {fingerprint: stableKey(project), entries: list(saved?.walls).filter(entry => selected.has(bindingKey(entry))).map(copy)};
  }
  function wallRemovalTargets(project, wallId, faceId) {
    if (!wallContext(project, wallId)) return [];
    return list(project.properties?.wall_mounting?.walls).filter(entry => entry.wall_id === wallId && (!faceId || entry.face_id === faceId)).map(entry => ({wall_id: entry.wall_id, face_id: entry.face_id}));
  }
  function removeEntries(project, targets, fingerprint) {
    const plan = removalPlan(project, targets);
    if (fingerprint !== undefined && plan.fingerprint !== fingerprint) throw new Error("Проект изменился. Проверьте область и подтвердите удаление ещё раз.");
    if (!plan.entries.length) return 0;
    const selected = new Set(plan.entries.map(bindingKey));
    // Assemblies and STEP sections are shared library geometry, not wall-owned
    // objects. Keep them; the document generator excludes unused assemblies.
    // Keep the pinned manual snapshot as well, including when no bindings remain.
    project.properties.wall_mounting = {...project.properties.wall_mounting, walls: project.properties.wall_mounting.walls.filter(entry => !selected.has(bindingKey(entry)))};
    return plan.entries.length;
  }
  function createRemovalControl(host, options) {
    const make = (tag, cls, text) => {const item = document.createElement(tag); if (cls) item.className = cls; if (text !== undefined) item.textContent = text; return item;};
    const button = (text, action, cls = "mini-button") => {const item = make("button", cls, text); item.type = "button"; item.addEventListener("click", action); return item;};
    let armed = null;
    const wrapper = make("div", "wm-removal"), panel = make("div", "wm-remove-confirm"), description = make("p"), error = make("p", "wm-remove-error");
    const trigger = button(options.label || "Удалить монтаж…", arm, "button button-secondary button-wide wm-remove");
    const accept = button(options.confirmLabel || "Да, удалить монтаж", remove, "button button-secondary wm-remove"), cancel = button("Оставить монтаж", () => {armed = null; panel.hidden = true; error.textContent = ""; trigger.focus?.();});
    panel.hidden = true; panel.setAttribute("role", "alert"); error.setAttribute("role", "status");
    panel.append(description, make("p", "wm-help", "Снимутся панели, раскладка и назначения узлов. Стены, проёмы, двери, люки, подсветка и исходные STEP останутся. Сборки сохранятся в библиотеке; неиспользуемые монтажные узлы не попадут в альбом. Правку можно вернуть кнопкой «Отменить»."), accept, cancel);
    wrapper.append(trigger, panel, error); host.append(wrapper);
    function plan() {return removalPlan(options.getProject(), options.getTargets());}
    function refresh() {
      try {const current = plan(); trigger.disabled = !current.entries.length; wrapper.hidden = options.hideEmpty === true && !current.entries.length; if (armed && armed.fingerprint !== current.fingerprint) {armed = null; panel.hidden = true; error.textContent = "Проект изменился. Подтвердите удаление заново.";}}
      catch (reason) {trigger.disabled = true; error.textContent = reason.message;}
    }
    function arm() {
      try {const reason = options.blockReason?.(); if (reason) throw new Error(reason); const current = plan(); if (!current.entries.length) return false; armed = current; description.textContent = `${options.description || "Удалить монтаж выбранной области"}? Сторон стен: ${armed.entries.length}.`; error.textContent = ""; panel.hidden = false; accept.focus?.(); return true;}
      catch (reason) {error.textContent = reason.message; return false;}
    }
    function remove() {
      if (!armed) return false;
      try {
        const reason = options.blockReason?.(); if (reason) throw new Error(reason);
        const expected = armed, count = expected.entries.length;
        const changed = options.mutate(options.mutationLabel || "удаление монтажа", project => removeEntries(project, expected.entries, expected.fingerprint));
        if (changed === false) return false;
        armed = null; panel.hidden = true; error.textContent = ""; options.onRemoved?.(count); options.toast?.(`Монтаж удалён со сторон стен: ${count}. Можно отменить правку.`, "success"); refresh(); return true;
      } catch (reason) {armed = null; panel.hidden = true; error.textContent = reason.message; return false;}
    }
    refresh(); return {refresh, cancel() {armed = null; panel.hidden = true; error.textContent = "";}, arm, remove};
  }
  function point(value) { return Array.isArray(value) ? {x: Number(value[0]), y: Number(value[1])} : {x: Number(value.x), y: Number(value.y)}; }
  function polygonPath(polygon, holes = []) {
    return [polygon, ...list(holes)].filter(ring => list(ring).length >= 3).map(ring => ring.map(point).map((p, index) => `${index ? "L" : "M"}${p.x},${-p.y}`).join(" ") + " Z").join(" ");
  }
  function previewIdentity(data, wallId, faceId) {
    return !!data && data.wall?.face_id === faceId && (!data.wall?.id || data.wall.id === wallId) && typeof data.geometry_signature === "string" && data.geometry_signature.length > 0 && !!data.snapshot?.snapshot_id;
  }

  function create(host, options) {
    const make = (tag, cls, text) => {const element = document.createElement(tag); if (cls) element.className = cls; if (text !== undefined) element.textContent = text; return element;};
    const svg = (tag, attributes = {}) => {const element = document.createElementNS(NS, tag); Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value))); return element;};
    const button = (text, action, cls = "button button-secondary") => {const element = make("button", cls, text); element.type = "button"; element.addEventListener("click", action); return element;};
    const label = (text, input, hint) => {const element = make("label", "wm-field"); element.append(make("span", "", text), input); input.setAttribute("aria-label", text); if (hint) element.append(make("small", "wm-help", hint)); return element;};
    const select = entries => {const element = make("select"); entries.forEach(([value, text]) => {const option = make("option", "", text); option.value = value; element.append(option);}); return element;};
    const input = (min, max, step) => {const element = make("input"); element.type = "number"; element.min = min; element.max = max; element.step = step; return element;};
    let alive = true, opened = false, wallId = null, faceId = null, projectId = null, sequence = 0, timer = null;
    let baseline = null, baselineSaved = null, currentContext = "", result = null, resultContext = "", pending = false, applying = false, conflict = false, selectedPanel = null, selectedNode = null, selectedGroup = "joints", groupRuleIds = {}, closeArmed = false;
    const shell = make("section", "wall-mount-workspace"), heading = make("header", "wm-header"), title = make("h2", "", "Монтаж стены"), subtitle = make("p", "wm-help"), headText = make("div");
    const closeButton = button("Закрыть", () => close()); closeButton.setAttribute("aria-label", "Закрыть монтаж стены");
    headText.append(title, subtitle); heading.append(headText, closeButton);
    const grid = make("div", "wm-grid"), rail = make("div", "wm-rail"), fields = make("div", "wm-fields"), main = make("div", "wm-main"), face = select([]);
    const material = select(Object.entries(materialLabels)), thickness = input(1, 200, .1), width = input(10, 100000, 1), height = input(10, 100000, 1), joint = input(0, 100, .1);
    const edge = select([["", "Не выбрана"], ["V-0", "V-0 · прямой край"], ["Va", "Va · прямой край"], ["Vb", "Vb · паз"], ["Vc", "Vc · паз"], ["Vd", "Vd · паз"]]), floorGap = input(0, 10000, 1), ceilingGap = input(0, 10000, 1);
    const system = select([["open_mount", "Открытое крепление"], ["concealed_mount", "Скрытое крепление"]]);
    const zone = select([["interior", "Внутри здания"], ["exterior", "Снаружи здания"]]);
    const decisionMode = select([["strict", "По условиям методички"], ["project", "С проектными настройками"]]);
    const assembly = select([["", "Подобрать по методичке"]]);
    const additional = make("details", "wm-panel-extra"); additional.append(make("summary", "", "Кромка и зазоры"), label("Кромка / паз панели", edge), label("Зазор у пола, мм", floorGap), label("Зазор у потолка, мм", ceilingGap));
    fields.append(make("h3", "wm-step", "1. Сторона стены"), label("Сторона облицовки", face), label("Расположение отделки", zone, "Для двора или незамкнутого контура выберите наружную сторону вручную. Внутренний монтаж другой стороны не изменится."), make("h3", "wm-step", "2. Панели"), label("Материал панели", material), label("Толщина панели, мм", thickness), label("Ширина панели, мм", width), label("Высота панели, мм", height), label("Шов между панелями, мм", joint), additional, make("h3", "wm-step", "3. Монтаж"), label("Способ крепления", system), label("Автоподбор узлов", decisionMode, "Проектные настройки дополняют условия методички; основания выбора — в примечаниях."), label("Базовая система стены", assembly, "Общая основа для автоматического подбора. Стыки, пол, потолок, углы и проёмы назначаются отдельно над развёрткой. Их ручной выбор не меняет базовую систему."));
    const reset = button("Сбросить правки", resetChanges, "mini-button"), apply = button("Применить к стене", applyConfiguration, "button button-primary"), railActions = make("div", "wm-actions");
    railActions.append(apply, reset); rail.append(fields, railActions, make("p", "wm-help wm-save-help", "«Применить» меняет черновик одной отменяемой правкой. Затем сохраните проект."));
    const prepareAssemblies = options.onPrepareAssemblies ? button("Сборки узлов помещения", () => {const scope = assemblyScope(); if (!scope) {status.textContent = "Сначала примените монтаж текущей стороны, связанной с помещением, и завершите местные правки."; return;} if (close()) options.onPrepareAssemblies(scope);}, "mini-button") : null;
    if (prepareAssemblies) {prepareAssemblies.title = "Построить сборки по применённому монтажу всех стен этого помещения"; railActions.append(prepareAssemblies);}
    const viewHeader = make("div", "wm-view-header"), count = make("p", "wm-stats"), rebuild = button("Пересчитать", () => preview(), "mini-button"); viewHeader.append(count, rebuild);
    const status = make("p", "wm-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const groupBox = make("section", "wm-group-picker"), groupTabs = make("div", "wm-group-tabs"), groupTitle = make("strong", "wm-group-title"), groupScope = make("p", "wm-help wm-group-scope"), groupWarnings = make("div", "wm-group-compatibility"), groupChoice = select([]), groupAutomatic = button("Автоподбор только этой группы", () => chooseGroupRule(null), "mini-button"), groupControls = make("div", "wm-group-controls");
    groupWarnings.setAttribute("role", "status"); groupWarnings.setAttribute("aria-live", "polite"); groupWarnings.hidden = true;
    groupTabs.setAttribute("role", "group"); groupTabs.setAttribute("aria-label", "Группы монтажных узлов");
    const groupButtons = {};
    Object.entries(groupLabels).forEach(([id, caption]) => {const tab = button(caption, () => selectGroup(id), "wm-group-tab"); tab.setAttribute("data-node-group", id); tab.setAttribute("aria-pressed", "false"); groupButtons[id] = tab; groupTabs.append(tab);});
    groupChoice.setAttribute("aria-label", "Вариант узлов выбранной группы"); groupChoice.addEventListener("change", () => chooseGroupRule(groupChoice.value || null));
    groupControls.append(groupChoice, groupAutomatic); groupBox.append(groupTabs, groupTitle, groupScope, groupControls, groupWarnings);
    const canvas = svg("svg", {class: "wm-elevation", role: "group", "aria-label": "Развёртка стены с раскладкой панелей и проёмами", viewBox: "0 0 1000 700"});
    const canvasBox = make("div", "wm-canvas-box"), legend = make("div", "wm-legend");
    legend.append(make("span", "wm-legend-panel", "Панели"), make("span", "wm-legend-opening", "Проёмы"), make("span", "wm-legend-node", "Узел по методичке"), make("span", "wm-legend-project", "Проектное допущение"), make("span", "wm-legend-unresolved", "Узел не определён"));
    canvasBox.append(canvas, legend);
    const selected = make("div", "wm-selection"); selected.setAttribute("aria-live", "polite");
    const disclaimer = make("details", "wm-requirement-list"); disclaimer.append(make("summary", "", "Примечания к развёртке"), make("p", "wm-help", "Кружки обозначают места узлов. Сечения профилей и сборок открываются отдельно. Показаны размеры участков облицовки с учётом проёмов; размеры заготовок и шаг крепежа уточняются отдельно."));
    const details = make("div", "wm-details"), rules = make("div", "wm-rules"), issues = make("div", "wm-issues"), requirements = make("div", "wm-requirements");
    details.append(rules, issues, requirements); main.append(viewHeader, status, groupBox, canvasBox, selected, disclaimer, details); grid.append(rail, main);
    const confirm = make("div", "wm-close-confirm"); confirm.hidden = true; confirm.setAttribute("role", "alert");
    confirm.append(make("p", "", "Параметры монтажа ещё не применены к стене."), button("Вернуться к монтажу", () => {closeArmed = false; confirm.hidden = true;}), button("Закрыть без применения", () => close({force: true})));
    shell.append(heading, grid, confirm); host.replaceChildren(shell);
    const removal = createRemovalControl(railActions, {
      getProject: current, getTargets: () => wallRemovalTargets(current(), wallId, faceId),
      label: "Убрать монтаж с этой стороны", confirmLabel: "Подтвердить снятие монтажа", description: "Удалить монтаж только выбранной стороны стены", mutationLabel: "снятие монтажа со стороны стены", toast: options.toast,
      mutate: (label, action) => {applying = true; try {return options.mutate(label, action);} finally {applying = false;}},
      blockReason: () => !opened || current()?.id !== projectId ? "Откройте монтаж нужного проекта заново." : "",
      onRemoved: () => {sequence++; clearTimeout(timer); pending = false; result = null; selectedPanel = null; selectedNode = null; selectedGroup = "joints"; conflict = false; closeArmed = false; confirm.hidden = true; baselineSaved = stableKey(null); currentContext = contextKey(wallContext(current(), wallId)); fillConfiguration(configuration(null, wallContext(current(), wallId)?.wall.height_mm)); canvas.replaceChildren(); selected.replaceChildren(); rules.replaceChildren(); issues.replaceChildren(); requirements.replaceChildren(); renderGroups(null); canvas.setAttribute("aria-busy", "false"); count.textContent = "Монтаж не задан"; status.textContent = "Монтаж с этой стороны удалён. Для новой раскладки задайте параметры и нажмите «Пересчитать»."; updateActions();},
    });

    function readConfiguration() {
      for (const element of [thickness, width, height, joint, floorGap, ceilingGap]) if (!String(element.value).trim() || !Number.isFinite(Number(element.value))) throw new Error("Заполните все размеры панели и шва числами в миллиметрах.");
      return configuration({installation_zone:zone.value, material: material.value, panel_thickness_mm: Number(thickness.value), panel_width_mm: Number(width.value), panel_height_mm: Number(height.value), joint_mm: Number(joint.value), mounting_system: system.value, assembly_rule_id: assembly.value || null, panel_edge: edge.value || null, floor_gap_mm: Number(floorGap.value), ceiling_gap_mm: Number(ceilingGap.value), allow_project_assumptions: decisionMode.value === "project", node_rule_ids: groupRuleIds});
    }
    function draftValue() { return [material.value, thickness.value, width.value, height.value, joint.value, system.value, assembly.value, edge.value, floorGap.value, ceilingGap.value, decisionMode.value, nodeRules(groupRuleIds), zone.value]; }
    function fillConfiguration(value) {
      zone.value=value.installation_zone || "interior";
      material.value = value.material; thickness.value = String(value.panel_thickness_mm); width.value = String(value.panel_width_mm); height.value = String(value.panel_height_mm); joint.value = String(value.joint_mm); system.value = value.mounting_system; edge.value = value.panel_edge || ""; floorGap.value = String(value.floor_gap_mm); ceilingGap.value = String(value.ceiling_gap_mm); decisionMode.value = value.allow_project_assumptions ? "project" : "strict";
      groupRuleIds = nodeRules(value.node_rule_ids); setAssemblyChoices([], value.assembly_rule_id); baseline = draftValue();
    }
    function setAssemblyChoices(candidates, selectedId) {
      assembly.replaceChildren();
      const automatic = make("option", "", "Подобрать по методичке"); automatic.value = ""; assembly.append(automatic);
      list(candidates).forEach(candidate => {const option = make("option", "", `${candidate.title || candidate.rule_id}${candidate.status === "incompatible" ? " · не подходит" : candidate.status === "unknown" ? " · нужна проверка" : ""}`); option.value = candidate.rule_id; assembly.append(option);});
      if (selectedId && !list(candidates).some(candidate => candidate.rule_id === selectedId)) {const missing = make("option", "", `Сохранённый узел · ${selectedId}`); missing.value = selectedId; assembly.append(missing);}
      assembly.value = selectedId || "";
    }
    function hasDraft() { return opened && baseline !== null && !equal(draftValue(), baseline); }
    function current() { return options.getProject?.(); }
    function assemblyScope() {if (!opened || pending || conflict || hasDraft() || current()?.id !== projectId || !storedEntry(current(), wallId, faceId)) return null; const context = wallContext(current(), wallId), id = list(context?.wall.faces).find(face => face.id === faceId)?.space_id; return id && list(context.floor.spaces).some(space => space.id === id) ? {kind: "space", id} : null;}
    function updateActions() {apply.disabled = !result || pending || conflict; reset.disabled = !hasDraft() && !conflict; rebuild.disabled = pending; groupChoice.disabled = !result || pending || conflict || !activeGroup(result)?.node_ids?.length; groupAutomatic.disabled = !result || pending || conflict || !groupRuleIds[selectedGroup]; rules.querySelectorAll("button").forEach(item => {if (item.wmGroupChoice) item.disabled = !result || pending || conflict || item.wmIncompatible;}); removal.refresh(); if (prepareAssemblies) prepareAssemblies.disabled = !assemblyScope();}
    function changed() {
      if (!opened || !alive) return;
      closeArmed = false; removal.cancel(); confirm.hidden = true; result = null; sequence++; pending = false;
      canvas.setAttribute("aria-busy", "true"); status.textContent = "Обновляем раскладку…"; updateActions();
      clearTimeout(timer); timer = setTimeout(() => preview(), 250);
    }
    [zone, material, thickness, width, height, joint, system, assembly, edge, floorGap, ceilingGap, decisionMode].forEach(element => element.addEventListener(element.tagName === "SELECT" ? "change" : "input", changed));
    face.addEventListener("change", () => {
      if (hasDraft()) {face.value = faceId; status.textContent = "Сначала примените или сбросьте параметры текущей стороны стены."; return;}
      faceId = face.value; loadFace();
    });
    function loadFace() {
      const context = wallContext(current(), wallId); if (!context) return;
      const saved = storedEntry(current(), wallId, faceId); baselineSaved = stableKey(saved);
      fillConfiguration(configuration(saved?.configuration, context.wall.height_mm)); currentContext = contextKey(context); conflict = false;
      result = null; selectedPanel = null; selectedNode = null; selectedGroup = "joints"; closeArmed = false; removal.cancel(); confirm.hidden = true; renderGroups(null); rules.replaceChildren(); selected.replaceChildren(); updateActions(); preview();
    }
    function resetChanges() {clearTimeout(timer); loadFace();}
    async function preview() {
      clearTimeout(timer); if (!alive || !opened || conflict) return;
      let config; try {config = readConfiguration();} catch (error) {sequence++; pending = false; status.textContent = error.message; result = null; canvas.replaceChildren(); canvas.setAttribute("aria-busy", "false"); updateActions(); return;}
      const project = current(), context = wallContext(project, wallId); if (!context || project.id !== projectId) {result = null; status.textContent = "Стена или проект изменились. Откройте монтаж нужной стены заново."; updateActions(); return;}
      const token = ++sequence, key = contextKey(context), requestWall = wallId, requestFace = faceId;
      pending = true; result = null; updateActions(); canvas.setAttribute("aria-busy", "true"); status.textContent = "Раскладываем панели и проверяем методички…";
      try {
        const data = await options.request("/wall-mount/preview", {method: "POST", body: {project: requestProject(project), wall_id: requestWall, face_id: requestFace, configuration: config}});
        if (!alive || !opened || token !== sequence || current()?.id !== projectId || key !== contextKey(wallContext(current(), wallId))) return;
        if (!previewIdentity(data, requestWall, requestFace)) throw new Error("Расчёт относится к другой стороне стены. Повторите построение.");
        result = data; resultContext = key; currentContext = key; setAssemblyChoices(data.candidates, config.assembly_rule_id); renderPreview(data);
        status.textContent = "Раскладка обновлена. Нажмите на панель или место узла, чтобы посмотреть подробности.";
      } catch (error) {if (alive && opened && token === sequence) {result = null; status.textContent = `Не удалось построить монтаж: ${error.message}`; canvas.replaceChildren(); count.textContent = "Раскладка не рассчитана";}}
      finally {if (alive && opened && token === sequence) {pending = false; canvas.setAttribute("aria-busy", "false"); updateActions();}}
    }
    function renderPreview(data) {
      if (selectedNode && !list(data.nodes).some(node => node.id === selectedNode && nodeGroup(node) === selectedGroup)) selectedNode = null;
      if (selectedPanel && !list(data.panels).some(panel => panel.id === selectedPanel)) selectedPanel = null;
      renderGroups(data); drawElevation(data); renderSelected(data); renderCandidates(data); renderIssues(data); renderRequirements(data);
      const area = list(data.panels).reduce((sum, panel) => sum + Number(panel.area_m2 || 0), 0);
      count.textContent = `${number(data.wall.length_mm)} × ${number(data.wall.height_mm)} мм · панелей: ${data.summary?.panel_count ?? list(data.panels).length}${data.summary?.piece_count !== data.summary?.panel_count ? ` · участков: ${list(data.panels).length}` : ""}${area ? ` · ${number(area)} м²` : ""}`;
    }
    function activeGroup(data) {return list(data?.node_groups).find(group => group.id === selectedGroup) || null;}
    function selectGroup(id, nodeId = null) {
      if (!opened || !alive || !Object.hasOwn(groupLabels, id)) return;
      selectedGroup = id; selectedNode = nodeId; selectedPanel = null;
      if (result) {renderGroups(result); drawElevation(result); renderSelected(result); renderCandidates(result);}
      updateActions();
    }
    function chooseGroupRule(ruleId) {
      if (!opened || !alive || !result || pending || conflict) return false;
      const group = activeGroup(result);
      if (!group?.node_ids?.length || (ruleId && !list(group.candidates).some(candidate => candidate.rule_id === ruleId && candidate.status !== "incompatible"))) return false;
      if (ruleId) groupRuleIds = {...groupRuleIds, [selectedGroup]: ruleId};
      else {groupRuleIds = {...groupRuleIds}; delete groupRuleIds[selectedGroup];}
      groupChoice.value = ruleId || ""; groupChoice.focus?.({preventScroll: true}); changed(); return true;
    }
    function renderGroups(data) {
      const active = activeGroup(data);
      Object.entries(groupButtons).forEach(([id, tab]) => {
        const group = list(data?.node_groups).find(item => item.id === id), size = list(group?.node_ids).length;
        tab.textContent = `${groupLabels[id]} · ${size}`; tab.setAttribute("aria-label", `${groupLabels[id]} · узлов: ${size}`); tab.setAttribute("aria-pressed", String(id === selectedGroup)); tab.className = `wm-group-tab${id === selectedGroup ? " is-selected" : ""}`;
        tab.title = size ? `Выбрать решение для всех узлов группы «${groupLabels[id]}» этой стороны стены` : "На этой стороне стены пока нет таких узлов";
      });
      groupTitle.textContent = `Узлы: ${groupLabels[selectedGroup]}`;
      groupScope.textContent = active?.node_ids?.length ? `Выбор действует только на эту группу (${active.node_ids.length}) на текущей стороне стены. Остальные группы и базовая система не изменятся.` : data ? "На этой стороне нет узлов этой группы. Выберите другую группу или измените геометрию стены." : "Нажмите на кружок узла или выберите группу. Пол, потолок, стыки, углы и проёмы настраиваются независимо.";
      groupWarnings.replaceChildren();
      const warnings = list(active?.compatibility_warnings).filter(warning => typeof warning?.message === "string" && warning.message);
      groupWarnings.hidden = !warnings.length;
      if (warnings.length) {
        groupWarnings.append(make("strong", "", "Проверьте совместимость выбранных групп"));
        warnings.forEach(warning => groupWarnings.append(make("p", "", warning.message)));
        groupWarnings.append(make("p", "wm-help", "Назначения других групп сохранены. При необходимости выберите совместимый вариант отдельно."));
      }
      const automatic = make("option", "", "Автоподбор этой группы"); automatic.value = ""; groupChoice.replaceChildren(automatic);
      list(active?.candidates).forEach(candidate => {const option = make("option", "", `${groupCandidateLabel(candidate, selectedGroup)}${candidate.status === "incompatible" ? " · не подходит" : candidate.requires_project_adaptation ? " · проектная адаптация" : candidate.status === "unknown" ? " · нужна проверка" : ""}`); option.value = candidate.rule_id; option.title = candidate.title || candidate.rule_id; option.disabled = candidate.status === "incompatible"; groupChoice.append(option);});
      const requested = groupRuleIds[selectedGroup];
      if (requested && !list(active?.candidates).some(candidate => candidate.rule_id === requested)) {const missing = make("option", "", `Сохранённое назначение недоступно · ${requested}`); missing.value = requested; missing.disabled = true; groupChoice.append(missing);}
      groupChoice.value = requested || ""; groupChoice.disabled = !active?.node_ids?.length || !data; groupAutomatic.disabled = !requested || !data;
      groupBox.setAttribute("data-active-group", selectedGroup);
    }
    function interactive(element, labelText, action) {
      element.setAttribute("tabindex", "0"); element.setAttribute("role", "button"); element.setAttribute("aria-label", labelText);
      element.addEventListener("click", action); element.addEventListener("keydown", event => {if (event.key === "Enter" || event.key === " ") {event.preventDefault(); action();}});
    }
    function drawElevation(data) {
      canvas.replaceChildren(); const length = data.wall.length_mm, wallHeight = data.wall.height_mm, pad = Math.max(length, wallHeight) * .075, textSize = Math.max(length, wallHeight) * .022;
      canvas.setAttribute("viewBox", `${-pad} ${-wallHeight - pad} ${length + pad * 2} ${wallHeight + pad * 2.35}`);
      canvas.append(svg("rect", {x: 0, y: -wallHeight, width: length, height: wallHeight, class: "wm-wall-background"}));
      const panelLayer = svg("g"); canvas.append(panelLayer);
      list(data.panels).forEach(panel => {
        const polygon = panel.polygon || [[panel.x_mm, panel.y_mm], [panel.x_mm + panel.width_mm, panel.y_mm], [panel.x_mm + panel.width_mm, panel.y_mm + panel.height_mm], [panel.x_mm, panel.y_mm + panel.height_mm]];
        const shape = svg("path", {d: polygonPath(polygon, panel.holes), "fill-rule": "evenodd", class: `wm-panel wm-material-${data.configuration.material}${selectedPanel === panel.id ? " is-selected" : ""}`, "data-panel-id": panel.id});
        interactive(shape, `Панель ${panel.index ?? panel.id}: ${number(panel.width_mm)} на ${number(panel.height_mm)} мм`, () => {if (result !== data || pending) return; selectedPanel = panel.id; selectedNode = null; drawElevation(data); renderSelected(data);}); panelLayer.append(shape);
        if (panel.width_mm > textSize * 3 && panel.height_mm > textSize * 3) {const text = svg("text", {x: panel.x_mm + panel.width_mm / 2, y: -(panel.y_mm + panel.height_mm / 2), "font-size": textSize, class: "wm-panel-number"}); text.textContent = String(panel.index ?? ""); panelLayer.append(text);}
      });
      list(data.openings).forEach(opening => {
        const x = opening.x_mm ?? opening.offset_mm, y = opening.y_mm ?? opening.sill_mm ?? 0;
        canvas.append(svg("rect", {x, y: -(y + opening.height_mm), width: opening.width_mm, height: opening.height_mm, class: "wm-opening"}));
        const text = svg("text", {x: x + opening.width_mm / 2, y: -(y + opening.height_mm / 2), "font-size": textSize * .8, class: "wm-opening-label"}); text.textContent = opening.name || "Проём"; canvas.append(text);
      });
      list(data.nodes).slice(0, 160).forEach((node, index) => {
        if (![node.x_mm, node.y_mm].every(Number.isFinite)) return;
        const group = svg("g", {class: `wm-node${node.decision_status === "project_assumption" ? " is-project-assumption" : node.status !== "reference_candidate" && node.decision_status !== "source_reference" ? " is-unresolved" : ""}${nodeGroup(node) === selectedGroup ? " is-group-selected" : ""}${selectedNode === node.id ? " is-selected" : ""}`, "data-node-id": node.id, "data-node-group": nodeGroup(node)}), circle = svg("circle", {cx: node.x_mm, cy: -node.y_mm, r: textSize * .7});
        const text = svg("text", {x: node.x_mm, y: -node.y_mm, "font-size": textSize * .7}); text.textContent = String(index + 1); group.append(circle, text);
        interactive(group, `${node.label || kindLabels[node.kind] || "Место узла"} ${index + 1}`, () => {if (result !== data || pending) return; selectGroup(nodeGroup(node), node.id);}); canvas.append(group);
      });
      const dimensionY = pad * .48; canvas.append(svg("line", {x1: 0, y1: dimensionY, x2: length, y2: dimensionY, class: "wm-dimension"}));
      [0, length].forEach(x => canvas.append(svg("line", {x1: x, y1: pad * .1, x2: x, y2: pad * .65, class: "wm-dimension"})));
      const dimension = svg("text", {x: length / 2, y: dimensionY - textSize * .35, "font-size": textSize, class: "wm-dimension-text"}); dimension.textContent = `${number(length)} мм`; canvas.append(dimension);
      const origin = svg("text", {x: 0, y: pad * 1.12, "font-size": textSize * .72, class: "wm-origin-text"}); origin.textContent = data.wall.side === "right" ? "0 · от конца стены B → A" : "0 · от начала стены A → B"; canvas.append(origin);
    }
    function renderSelected(data) {
      selected.replaceChildren();
      const panel = list(data.panels).find(item => item.id === selectedPanel), node = list(data.nodes).find(item => item.id === selectedNode);
      if (panel) {selected.append(make("strong", "", `Участок панели ${panel.index ?? panel.id}`), make("span", "", `${materialLabels[data.configuration.material] || data.configuration.material} · ${number(panel.width_mm)} × ${number(panel.height_mm)} × ${number(data.configuration.panel_thickness_mm)} мм`), make("span", "wm-help", `От начала развёртки: ${number(panel.x_mm)} мм; от низа стены: ${number(panel.y_mm)} мм. Размеры участка после вычета проёмов; это не карта раскроя заготовки.`));}
      else if (node) {
        selected.append(make("strong", "", node.label || kindLabels[node.kind] || "Место узла"), make("span", "", `От начала развёртки: ${number(node.x_mm)} мм; от низа стены: ${number(node.y_mm)} мм.`), make("span", "wm-help", node.decision_status === "project_assumption" ? "Подобран с проектными настройками. Основание выбора и условия — ниже." : "Место примыкания. Состав и размеры — в карточке выбранного узла."));
        const candidate = list(activeGroup(data)?.candidates).find(item => item.rule_id === node.rule_id); if (candidate) selected.append(make("span", "", groupCandidateLabel(candidate, nodeGroup(node))));
        selected.append(make("span", "wm-help", `Группа «${groupLabels[nodeGroup(node)]}»: вариант выше применяется ко всем её узлам на этой стороне стены.`));
        if (node.decision_reason) selected.append(make("span", "wm-help", node.decision_reason));
        if (list(node.component_codes).length) selected.append(make("span", "wm-help", `Компоненты по источнику: ${node.component_codes.join(", ")}`));
        if (list(node.required_checks).length) {const notes = make("details", "wm-requirement-list"); notes.append(make("summary", "", `Примечания к узлу · ${node.required_checks.length}`)); node.required_checks.forEach(check => notes.append(make("p", "wm-help", typeof check === "string" ? check : check.message || "Уточните параметры узла"))); selected.append(notes);}
        const source = list(node.source_refs)[0] || node.source;
        if (source) {const adaptation = node.decision_status === "project_assumption" || source.reference_kind === "project_adaptation", caption = adaptation ? "Схема из методички для адаптации" : "Схема примыкания из методички"; const reference = make("details", "wm-manual-illustration wm-node-source"); reference.append(make("summary", "", caption), sourceBlock(source)); const url = source.preview_url; if (typeof url === "string" && /^\/api\/v1\/vendor-library\/[a-f0-9]{64}\/preview\.png\?page=\d+$/.test(url)) {const picture = make("img"); picture.src = url; picture.loading = "lazy"; picture.alt = `${caption}: ${source.title || node.label}, страница ${source.page}`; reference.append(picture);} selected.append(reference);}
      }
      else selected.append(make("span", "wm-help", "Панели кликабельны: выберите любую, чтобы увидеть размеры и положение. Цифры в кружках — места примыканий."));
    }
    function sourceBlock(source, fallback) {
      const box = make("div", "wm-source"), value = source || {}, page = value.page || fallback?.source?.page;
      const name = value.filename || value.title || value.document_id || "Методичка поставщика";
      box.append(make("span", "", `${name}${page ? ` · стр. ${page}` : ""}`));
      if (value.source_status && value.source_status !== "verified") box.append(make("span", "", "Первоисточник не подтверждён или изменён — нужна проверка."));
      const hash = value.sha256 || value.source_sha256 || fallback?.source_sha256;
      if (hash) box.append(make("small", "wm-hash", `SHA-256: ${hash}`));
      const url = value.preview_url || fallback?.preview_url;
      if (typeof url === "string" && /^\/api\/v1\/vendor-library\/[a-f0-9]{64}\/preview\.png\?page=\d+$/.test(url)) {const link = make("a", "", "Открыть страницу методички"); link.href = url; link.target = "_blank"; link.rel = "noopener"; box.append(link);}
      return box;
    }
    function renderCandidates(data) {
      rules.replaceChildren(make("h3", "", `Варианты группы «${groupLabels[selectedGroup]}»`));
      const group = activeGroup(data), candidates = list(group?.candidates), chosen = group?.selected_rule_id, requested = groupRuleIds[selectedGroup];
      if (!group) {rules.append(make("p", "wm-help", "Раздельный подбор групп пока не получен. Нажмите «Пересчитать». Базовая система стены не будет изменена.")); return;}
      if (!group.node_ids?.length) {rules.append(make("p", "wm-help", "На этой стороне стены нет узлов этой группы.")); return;}
      rules.append(make("p", "wm-help", `Меняется только группа «${groupLabels[selectedGroup]}» (${group.node_ids.length}). Другие назначения сохраняются.`));
      if (group.selection_reason) rules.append(make("p", "wm-selection-reason", group.selection_reason));
      if (requested && !candidates.some(candidate => candidate.rule_id === requested)) rules.append(make("p", "wm-choice-needed", "Сохранённый вариант недоступен для этой группы. Выберите другой или верните автоподбор только этой группы."));
      if (!candidates.length) rules.append(make("p", "wm-help", "Для этих параметров подходящий узел в методичках пока не найден. Измените параметры или уточните решение у конструктора."));
      else if (!chosen) rules.append(make("p", "wm-choice-needed", "Единственного варианта нет. Выберите тип узла ниже; неподходящие варианты собраны отдельно."));
      const other = make("details", "wm-other-rules"), incompatible = candidates.filter(candidate => candidate.status === "incompatible" && candidate.rule_id !== chosen); other.append(make("summary", "", `Неподходящие варианты · ${incompatible.length}`));
      const ordered = [...candidates.filter(candidate => candidate.rule_id === chosen), ...candidates.filter(candidate => candidate.rule_id !== chosen && candidate.status !== "incompatible"), ...incompatible];
      ordered.forEach(candidate => {
        const isChosen = candidate.rule_id === chosen, card = make("article", `wm-rule${isChosen ? " is-chosen" : ""}`), headline = make("div", "wm-rule-title");
        headline.append(make("strong", "", groupCandidateLabel(candidate, selectedGroup)), make("span", `wm-badge wm-badge-${candidate.status}`, candidate.status === "incompatible" ? "Не подходит" : candidate.requires_project_adaptation ? "Проектная адаптация" : candidate.status === "candidate" ? "Подходит по указанным параметрам" : "Недостаточно данных")); card.append(headline);
        if (selectedGroup !== "joints" && list(candidate.component_codes).length) card.append(make("p", "wm-help wm-group-source-title", `Источник: ${candidate.title || candidate.rule_id}`));
        if (isChosen) card.append(make("p", "wm-chosen-label", requested ? "Выбран вручную для этой группы" : "Предложен автоматически для этой группы"));
        if (requested === candidate.rule_id && !isChosen) card.append(make("p", "wm-choice-needed", "Назначен этой группе, но не может быть применён при текущих параметрах."));
        if (candidate.requires_project_adaptation) card.append(make("p", "wm-help", "Выбор принимает проектную адаптацию только для этой группы. Режим автоподбора и остальные группы не изменятся; основание адаптации сохраняется в проекте."));
        const coverage = candidate.coverage;
        const nodeChecks = list(candidate.node_checks), rejected = nodeChecks.filter(entry => !entry.applicable);
        const nodeCaption = entry => {const index = list(data.nodes).findIndex(node => node.id === entry.node_id); return `${entry.label || kindLabels[entry.kind] || "Место узла"}${index >= 0 ? ` · №${index + 1}` : ""}`;};
        if (coverage && coverage.applicable_count < coverage.total_count) {
          card.append(make("p", "wm-choice-needed", `Подходит для ${coverage.applicable_count} из ${coverage.total_count} узлов группы. Остальные останутся неопределёнными, а не получат другой узел автоматически.`));
          if (rejected.length) card.append(make("p", "wm-choice-needed", `Не применяется: ${rejected.slice(0, 6).map(nodeCaption).join("; ")}${rejected.length > 6 ? `; ещё ${rejected.length - 6}` : ""}.`));
        }
        if (nodeChecks.length) {
          const applicability = make("details", "wm-group-coverage"); applicability.append(make("summary", "", `Применимость по местам · ${coverage?.applicable_count ?? nodeChecks.filter(entry => entry.applicable).length} из ${coverage?.total_count ?? nodeChecks.length}`));
          const orderedChecks = [...rejected, ...nodeChecks.filter(entry => entry.applicable)], limit = 80;
          orderedChecks.slice(0, limit).forEach(entry => {
            const row = make("div", "wm-group-node-check"); row.append(make("strong", "", `${entry.applicable ? entry.project_adaptation ? "Адаптация" : "Подходит" : "Не применяется"}: ${nodeCaption(entry)}`));
            list(entry.checks).forEach(check => {const message = typeof check === "string" ? check : check.message; if (message) row.append(make("p", `wm-check wm-check-${check.status || "unknown"}`, message));}); applicability.append(row);
          });
          if (orderedChecks.length > limit) applicability.append(make("p", "wm-help", `Показаны первые ${limit} мест; всего ${orderedChecks.length}. Неподходящие места показаны первыми. Причину для каждого узла можно посмотреть, нажав на него на развёртке.`));
          card.append(applicability);
        }
        list(candidate.checks).forEach(check => {const message = typeof check === "string" ? check : check.message || check.label || check.requirement || check.parameter; if (message) card.append(make("p", `wm-check wm-check-${check.status || "unknown"}`, `${check.status === "match" ? "✓ " : check.status === "mismatch" ? "✕ " : "? "}${message}`));});
        if (list(candidate.requirements).length) {
          const conditions = make("details", "wm-group-source-conditions"); conditions.append(make("summary", "", "Условия и область применения источника"));
          candidate.requirements.forEach(requirement => {const row = make("article", "wm-requirement"); row.append(make("strong", "", requirement.title || "Условие источника")); if (requirement.requirement) row.append(make("p", "", requirement.requirement)); if (requirement.scope) row.append(make("p", "wm-help", `Область применения: ${requirement.scope}`)); if (requirement.source) row.append(sourceBlock(requirement.source, requirement)); conditions.append(row);}); card.append(conditions);
        }
        if (candidate.source) card.append(sourceBlock(candidate.source, candidate));
        const previewUrl = candidate.source?.preview_url;
        if (isChosen && typeof previewUrl === "string" && /^\/api\/v1\/vendor-library\/[a-f0-9]{64}\/preview\.png\?page=\d+$/.test(previewUrl)) {
          const illustration = make("details", "wm-manual-illustration"), picture = make("img"); illustration.open = true; picture.src = previewUrl; picture.loading = "lazy"; picture.alt = `Схема из методички: ${candidate.title || candidate.rule_id}, страница ${candidate.source.page}`;
          illustration.append(make("summary", "", "Схема узла из методички"), picture, make("p", "wm-help", "Подлинная страница методички, не персональный разрез STEP. Масштаб изображения экранный; размеры — по подписям первоисточника.")); card.append(illustration);
        }
        const stepMatches = list(candidate.step_matches);
        if (stepMatches.length) {
          const files = make("details", "wm-step-matches"); files.append(make("summary", "", `Связанные STEP / кандидаты: ${stepMatches.length}`));
          stepMatches.forEach(match => {
            const assets = list(match.matches).length ? match.matches : list(match.assets).length ? match.assets : [match];
            if (match.component_code || match.code) files.append(make("strong", "", match.component_code || match.code));
            assets.forEach(asset => {const row = make("div", "wm-model-row"), id = asset.source_id || asset.asset_id || asset.id; row.append(make("span", "", asset.filename || asset.relative_path || asset.label || asset.component_code || "Файл модели")); if (options.openModel && id) row.append(button("Открыть STEP", () => {if (hasDraft()) {status.textContent = "Примените или сбросьте параметры перед переходом к STEP."; return;} options.openModel(asset);}, "mini-button")); files.append(row);});
          });
          files.append(make("p", "wm-help", "Совпадение кода в названии не подтверждает исполнение детали и комплектность узла.")); card.append(files);
        } else card.append(make("p", "wm-help", "Связанный STEP пока не выбран. Доступна схема из методички."));
        if (!isChosen) {const choose = button(candidate.requires_project_adaptation ? "Выбрать проектный вариант" : "Выбрать для этой группы", () => {if (result === data && selectedGroup === group.id) chooseGroupRule(candidate.rule_id);}, "mini-button"); choose.wmGroupChoice = true; choose.wmIncompatible = candidate.status === "incompatible"; choose.disabled = choose.wmIncompatible; choose.setAttribute("aria-label", `${candidate.requires_project_adaptation ? "Выбрать проектный вариант" : "Выбрать для группы"} «${groupLabels[group.id]}»: ${candidate.title || candidate.rule_id}`); card.append(choose);}
        if (candidate.status === "incompatible" && !isChosen) other.append(card); else rules.append(card);
      });
      if (incompatible.length) rules.append(other);
    }
    function renderIssues(data) {
      issues.replaceChildren(); const messages = list(data.issues).map(issue => typeof issue === "string" ? issue : issue.message).filter(Boolean);
      if(data.configuration?.installation_zone==="exterior")issues.append(make("p","wm-help","Наружная облицовка · наружная применимость системы пока не подтверждена. Проверить влагостойкость, защиту от коррозии и ветровую нагрузку."));
      if (list(data.assumptions).length) {const assumptions = make("details", "wm-assumptions"); assumptions.append(make("summary", "", `Примечания к подбору · ${data.assumptions.length}`)); data.assumptions.forEach(item => {assumptions.append(make("p", "", item.message)); if (item.required_check) assumptions.append(make("p", "wm-help", `Проверить: ${item.required_check}`));}); issues.append(assumptions);}
      list(data.warnings).forEach(warning => {if (!messages.includes(warning)) messages.push(warning);});
      if (!messages.length) return;
      const box = make("details", "wm-warning"); box.append(make("summary", "", `Примечания и проверки · ${messages.length}`)); messages.forEach(message => box.append(make("p", "", message))); issues.append(box);
    }
    function renderRequirements(data) {
      requirements.replaceChildren(); if (!list(data.requirements).length) return;
      const box = make("details", "wm-requirement-list"); box.append(make("summary", "", `Требования методичек · ${data.requirements.length}`));
      list(data.requirements).forEach(item => {const article = make("article", "wm-requirement"); article.append(make("strong", "", item.title || item.topic || "Требование"), make("p", "", item.requirement || item.message || "")); if (item.source) article.append(sourceBlock(item.source, item)); box.append(article);}); requirements.append(box);
    }
    function applyConfiguration() {
      if (!result || pending || conflict || !opened) return false;
      const project = current(), context = wallContext(project, wallId);
      if (project?.id !== projectId || contextKey(context) !== resultContext) {result = null; status.textContent = "Геометрия стены изменилась. Пересчитайте раскладку перед применением."; updateActions(); return false;}
      let config; try {config = readConfiguration();} catch (error) {status.textContent = error.message; return false;}
      if (!equal(configuration(result.configuration), configuration(config))) {result = null; status.textContent = "Параметры изменились. Дождитесь нового расчёта."; updateActions(); return false;}
      const entry = {wall_id: wallId, face_id: faceId, configuration: copy(result.configuration), geometry_signature: result.geometry_signature};
      applying = true;
      try {
        const changed = options.mutate("настройка монтажа стены", target => {if (target.id !== projectId || contextKey(wallContext(target, wallId)) !== resultContext) throw new Error("Стена изменилась во время расчёта."); applyEntry(target, entry, result.snapshot);});
        if (changed === false && !equal(storedEntry(current(), wallId, faceId), entry)) return false;
        baseline = draftValue(); baselineSaved = stableKey(storedEntry(current(), wallId, faceId)); currentContext = contextKey(wallContext(current(), wallId)); resultContext = currentContext; closeArmed = false; confirm.hidden = true; status.textContent = "Монтаж применён к черновику. Сохраните проект; правку можно отменить одной кнопкой «Отменить»."; options.toast?.("Монтаж стены применён к черновику", "success"); updateActions();
        if (options.onApplied) {const appliedWall = wallId, appliedFace = faceId, appliedProject = projectId; Promise.resolve(options.onApplied({kind: "wall", id: wallId}, message => {if (alive && opened && projectId === appliedProject && wallId === appliedWall && faceId === appliedFace) status.textContent = message;})).catch(error => {if (alive && opened && wallId === appliedWall) status.textContent = error.message;});}
        return true;
      } catch (error) {status.textContent = error.message; return false;} finally {applying = false;}
    }
    function refresh() {
      if (!opened || !alive || applying) return;
      const project = current(), context = wallContext(project, wallId);
      if (!context || project.id !== projectId || !list(context.wall.faces).some(item => item.id === faceId)) {sequence++; clearTimeout(timer); pending = false; result = null; conflict = true; status.textContent = "Стена или проект больше недоступны. Эти параметры не будут применены. Закройте монтаж и выберите стену заново."; updateActions(); return;}
      const saved = stableKey(storedEntry(project, wallId, faceId));
      if (saved !== baselineSaved) {if (hasDraft()) {conflict = true; result = null; sequence++; pending = false; status.textContent = "Монтаж этой стороны изменён вне окна. Сбросьте местные правки, чтобы загрузить актуальный вариант."; updateActions(); return;} loadFace(); return;}
      const key = contextKey(context); if (key !== currentContext) {currentContext = key; selectedPanel = null; selectedNode = null; preview();}
    }
    function open(id, preferredFaceId) {
      if (!alive) return false;
      const wanted = id || (options.getSelection?.()?.type === "wall" ? options.getSelection().id : null), project = current(), context = wallContext(project, wanted);
      if (!context) {options.toast?.("Сначала выберите стену на чертеже.", "error"); return false;}
      if (opened && hasDraft()) {status.textContent = "Сначала примените или сбросьте правки текущей стены."; return false;}
      sequence++; clearTimeout(timer); opened = true; wallId = wanted; projectId = project.id;
      face.replaceChildren(); list(context.wall.faces).forEach(item => {const space = list(context.floor.spaces).find(room => room.id === item.space_id), option = make("option", "", `${storedEntry(project,wanted,item.id)?.configuration?.installation_zone==="exterior" ? "Снаружи здания" : space ? `${space.code || ""} ${space.name}`.trim() : "Без помещения"} · ${item.side === "left" ? "левая A → B" : "правая B → A"}${storedEntry(project, wanted, item.id) ? " · настроена" : ""}`); option.value = item.id; face.append(option);});
      faceId = list(context.wall.faces).find(item => item.id === preferredFaceId)?.id || list(context.wall.faces).find(item => item.space_id)?.id || context.wall.faces?.[0]?.id;
      if (!faceId) {opened = false; options.toast?.("У стены нет доступной стороны облицовки.", "error"); return false;}
      face.value = faceId; title.textContent = `Монтаж · ${context.wall.name || "стена"}`; subtitle.textContent = `${context.floor.name || "Этаж"} · размеры в миллиметрах · раскладка и подбор по методичкам`;
      loadFace(); return true;
    }
    function close(settings = {}) {
      if (hasDraft() && !settings.force) {closeArmed = true; confirm.hidden = false; return false;}
      opened = false; sequence++; clearTimeout(timer); pending = false; closeArmed = false; confirm.hidden = true; options.onClose?.(); return true;
    }
    updateActions();
    return {open, refresh, close, hasDraft, apply: applyConfiguration, preview, dispose() {alive = false; opened = false; sequence++; clearTimeout(timer);}};
  }
  return {defaults, configuration, nodeRules, nodeGroup, groupLabels, groupCandidateLabel, stableKey, equal, wallContext, contextKey, storedEntry, applyEntry, removalPlan, wallRemovalTargets, removeEntries, createRemovalControl, polygonPath, previewIdentity, requestProject, create};
});
