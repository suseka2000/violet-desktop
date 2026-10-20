(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports ? require("./wall-mount.js") : root.VioletWallMount);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletMountAssemblies = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (W) {
  "use strict";
  const copy = value => JSON.parse(JSON.stringify(value)), list = value => Array.isArray(value) ? value : [];
  const SHA = /^[a-f0-9]{64}$/, SCHEMA = "mount-assemblies/1.0", MAX_ITEMS = 256, MAX_POINTS = 120000, MAX_BASE64 = 8 * 1024 * 1024;
  const fingerprint = W.stableKey, mm = value => Number(value).toLocaleString("ru-RU", {maximumFractionDigits: 3});
  function fasteningSummary(record) {
    const fasteners = list(record?.bodies).filter(body => body.kind === "step" && body.role === "fastener"),
      definitions = list(record?.spec?.fastenings), checks = list(record?.fastening_checks), engagements = list(record?.intentional_engagements);
    const valid = fasteners.filter(body => {
      const matching = checks.filter(check => check.fastener_id === body.instance_id && check.fastener_body_id === body.id);
      if (matching.length !== 1) return false;
      const check = matching[0], definition = definitions.find(value => value.id === check.id && value.fastener_id === body.instance_id),
        seat = list(record.bodies).find(value => value.instance_id === definition?.seat_id),
        connections = engagements.filter(value => value.fastening_id === check.id), targets = list(definition?.engagement_targets);
      return check.status === "valid" && !list(check.issues).length && definition && seat && check.seat_id === seat.instance_id && check.seat_body_id === seat.id &&
        Number.isFinite(check.required_bearing_area_mm2) && check.required_bearing_area_mm2 > 0 && Number.isFinite(check.bearing_contact_area_mm2) && check.bearing_contact_area_mm2 + 1e-5 >= check.required_bearing_area_mm2 &&
        Number.isFinite(check.outside_envelope_volume_mm3) && check.outside_envelope_volume_mm3 >= 0 && check.outside_envelope_volume_mm3 <= .001 &&
        targets.length > 0 && W.equal(connections, list(check.engagements)) && W.equal(connections.map(value => value.target_id).sort(), [...targets].sort()) &&
        connections.every(value => {
          const target = list(record.bodies).find(target => target.instance_id === value.target_id);
          return target && value.status === "valid" && value.fastener_id === body.instance_id &&
            [value.body_a, value.body_b].includes(body.id) && [value.body_a, value.body_b].includes(target.id) &&
            Number.isFinite(value.volume_mm3) && value.volume_mm3 > 0 && Number.isFinite(value.depth_mm) && Number.isFinite(value.min_engagement_mm) && value.min_engagement_mm > 0 && value.depth_mm + 1e-5 >= value.min_engagement_mm &&
            Number.isFinite(value.outside_shaft_volume_mm3) && value.outside_shaft_volume_mm3 >= 0 && value.outside_shaft_volume_mm3 <= .001;
        });
    }).length;
    return {count: fasteners.length, valid, complete: fasteners.length > 0 && valid === fasteners.length && checks.length === fasteners.length && definitions.length === fasteners.length && engagements.length === checks.reduce((total, check) => total + list(check.engagements).length, 0),
      engagements: engagements.filter(value => value.status === "valid"), collisions: list(record?.collisions).length};
  }
  function contactSummary(record) {
    const definitions = list(record?.spec?.contacts), checks = list(record?.contact_checks), bodies = list(record?.bodies);
    const valid = definitions.filter(definition => {
      const matches = checks.filter(check => check.id === definition.id);
      if (matches.length !== 1) return false;
      const check = matches[0], a = bodies.find(body => body.instance_id === check.body_a), b = bodies.find(body => body.instance_id === check.body_b);
      return a && b && a !== b && a.role !== "fastener" && b.role !== "fastener" && a.id === check.body_a_id && b.id === check.body_b_id &&
        W.equal([check.body_a, check.body_b].sort(), [definition.body_a, definition.body_b].sort()) &&
        check.status === "valid" && !list(check.issues).length && Number.isFinite(check.min_area_mm2) && check.min_area_mm2 > 0 && check.min_area_mm2 === definition.min_area_mm2 &&
        Number.isFinite(check.contact_area_mm2) && check.contact_area_mm2 + 1e-5 >= check.min_area_mm2 &&
        Number.isFinite(check.overlap_volume_mm3) && check.overlap_volume_mm3 >= 0 && check.overlap_volume_mm3 <= .001;
    }).length;
    return {count: definitions.length, valid, complete: valid === definitions.length && checks.length === definitions.length};
  }
  function assemblyConnectivity(record) {
    const bodies = list(record?.bodies), graph = new Map(bodies.map(body => [body.id, new Set()]));
    const connect = (a, b) => {if (a !== b && graph.has(a) && graph.has(b)) {graph.get(a).add(b); graph.get(b).add(a);}};
    for (const check of list(record?.contact_checks)) if (check.status === "valid" && check.contact_area_mm2 > 0 && check.overlap_volume_mm3 <= .001) connect(check.body_a_id, check.body_b_id);
    for (const value of list(record?.intentional_engagements)) if (value.status === "valid" && value.volume_mm3 > 0) connect(value.body_a, value.body_b);
    const reached = new Set(), pending = bodies.length ? [bodies[0].id] : [];
    while (pending.length) {const id = pending.pop(); if (reached.has(id)) continue; reached.add(id); for (const other of graph.get(id) || []) if (!reached.has(other)) pending.push(other);}
    return {connected: bodies.length > 0 && graph.size === bodies.length && reached.size === bodies.length, reached: reached.size, total: bodies.length};
  }
  function collection(project) {
    const stored = project?.properties?.mount_assemblies;
    if (stored === undefined) return {schema_version: SCHEMA, items: []};
    if (stored?.schema_version !== SCHEMA || !Array.isArray(stored.items)) throw new Error("Неизвестный формат сохранённых сборок. Существующие данные не изменены.");
    return stored;
  }
  function checkedPlan(plan) {
    if (plan?.schema_version !== "mount-assembly-plan/1.0" || !Array.isArray(plan.items) || plan.items.length > MAX_ITEMS || plan.items.some(item => !SHA.test(item.id)) || new Set(plan.items.map(item => item.id)).size !== plan.items.length) throw new Error("Получен некорректный план сборок. Проект не изменён.");
    return plan;
  }
  function checkedRecord(record, expectedId) {
    const snapshot = record?.geometry_snapshot, step = record?.assembled_step;
    if (!record || record.id !== expectedId || !SHA.test(record.id) || !SHA.test(record.fingerprint) || !SHA.test(record.detail_key) || record.approval_status !== "unreviewed") throw new Error("Сборка не соответствует выбранному узлу или не содержит проверяемого снимка.");
    if (!SHA.test(step?.source_id) || snapshot?.source_id !== step.source_id || snapshot?.result?.source_id !== step.source_id || !SHA.test(snapshot?.geometry_sha256) || !(snapshot?.result?.point_count > 0) || !Array.isArray(snapshot?.result?.components)) throw new Error("Нет непустого сечения, связанного с точным STEP-снимком сборки.");
    if (typeof step.base64 !== "string" || !step.base64.length || !Number.isInteger(step.byte_length) || step.byte_length < 1 || !Array.isArray(record.source_models) || !record.source_models.length || record.source_models.some(model => !SHA.test(model.source_id))) throw new Error("Не сохранены исходные модели или STEP собранного узла.");
    if (step.encoding !== undefined && !["gzip+base64", "xz+base64"].includes(step.encoding)) throw new Error("Неизвестный способ хранения STEP-сборки. Проект не изменён.");
    if (!Array.isArray(record.bodies) || !record.bodies.length || record.bodies.some(body => !["step", "generated_box"].includes(body.kind)) || !W.equal(record.bodies.map(body => body.id), snapshot.result.components.map(component => component.id))) throw new Error("В сборке не определён согласованный состав тел и сечения.");
    if (!record.bodies.some(body => body.kind === "step") || !record.bodies.some(body => body.role === "panel") || record.bodies.some(body => body.kind === "step" && !record.source_models.some(source => source.source_id === body.source_id))) throw new Error("Нужны исходные STEP-компоненты и панель с указанным происхождением.");
    if (snapshot.result.components.some(component => (!list(component.paths).length && !list(record.spec?.fastenings).length) || component.open_wire_count > 0) || !snapshot.result.components.some(component => list(component.paths).length)) throw new Error("Нужно непустое замкнутое сечение; тела вне плоскости допускаются только в полной сборке с крепежом.");
    if (list(record.collisions).length) throw new Error("У тел сборки обнаружено объёмное пересечение. Такая сборка не будет добавлена в проект.");
    const fastening = fasteningSummary(record);
    if ((fastening.count || list(record.spec?.fastenings).length || list(record.fastening_checks).length || list(record.intentional_engagements).length) &&
        (!fastening.complete || !fastening.engagements.length || fastening.engagements.length !== list(record.intentional_engagements).length)) throw new Error("Не все соединения 3D-крепежа прошли проверку посадки и зацепления. Такая сборка не будет добавлена в проект.");
    if (!contactSummary(record).complete) throw new Error("Не все опорные контакты 3D-сборки прошли проверку. Такая сборка не будет добавлена в проект.");
    if (fastening.count && !assemblyConnectivity(record).connected) throw new Error("В 3D-сборке есть несвязанные детали без проверенной опоры. Такая сборка не будет добавлена в проект.");
    if (record.fastener_assembly && (fastening.count
      ? record.fastener_assembly.status !== "geometry_verified" || record.fastener_assembly.fastening_count !== fastening.count
      : record.fastener_assembly.status !== "unsupported")) throw new Error("Статус 3D-крепежа не соответствует проверенной сборке.");
    return record;
  }
  function mergeAssembly(stored, record, expectedId) {
    if (stored?.schema_version !== SCHEMA || !Array.isArray(stored.items)) throw new Error("Неизвестный формат сохранённых сборок.");
    checkedRecord(record, expectedId);
    if (stored.items.some(item => item.id === record.id)) throw new Error("Сборка с таким идентификатором уже сохранена. Ничего не заменено.");
    const items = [...stored.items, record];
    if (new Set(items.map(item => item.id)).size !== items.length || items.length > MAX_ITEMS) throw new Error(`В проекте допускается не более ${MAX_ITEMS} различных сборок без повторяющихся идентификаторов.`);
    if (items.reduce((n, item) => n + list(item.geometry_snapshot?.result?.components).reduce((sum, component) => sum + list(component.paths).reduce((count, path) => count + list(path).length, 0), 0) + list(item.bodies).reduce((sum, body) => sum + list(body.section_loops).reduce((count, path) => count + list(path).length, 0), 0), 0) > MAX_POINTS) throw new Error("Сечения сборок превышают лимит 120 000 точек.");
    if (items.reduce((n, item) => n + String(item.assembled_step?.base64 || "").length, 0) > MAX_BASE64) throw new Error("Снимки STEP-сборок превышают лимит 8 МиБ. Существующие сборки сохранены.");
    return {schema_version: SCHEMA, items: items.map(copy)};
  }
  function preparedAssemblies(project, response) {
    if (response?.schema_version !== "mount-assembly-preparation/1.0" || response.assemblies?.schema_version !== SCHEMA || !Array.isArray(response.assemblies.items)) throw new Error("Получен неизвестный результат автоматической сборки. Проект не изменён.");
    const previous = collection(project), incoming = response.assemblies.items, ids = new Set(incoming.map(item => item.id)), pruned = new Set(response.pruned_ids || []);
    if (ids.size !== incoming.length || [...pruned].some(id => !SHA.test(id) || ids.has(id) || !previous.items.some(item => item.id === id)) || previous.items.some(item => !pruned.has(item.id) && !W.equal(item, incoming.find(candidate => candidate.id === item.id)))) throw new Error("Расчёт пытается заменить или удалить сохранённые STEP-сборки без подтверждения их устаревания. Проект не изменён.");
    let prepared = {schema_version: SCHEMA, items: []};
    for (const record of incoming) prepared = mergeAssembly(prepared, record, record.id);
    return prepared;
  }
  async function prepare(project, scope, plan, request, progress = () => {}, cancelled = () => false) {
    checkedPlan(plan);
    const working = W.requestProject(project,{keepLocalAssemblies:true}), outcomes = []; collection(working);
    if (list(plan.pruned_ids).length) {
      working.properties ||= {};
      working.properties.mount_assemblies = preparedAssemblies(working, {schema_version:"mount-assembly-preparation/1.0",pruned_ids:plan.pruned_ids,
        assemblies:{schema_version:SCHEMA,items:collection(working).items.filter(item=>!plan.pruned_ids.includes(item.id))}});
    }
    for (const [index, item] of plan.items.entries()) {
      if (cancelled()) return {cancelled: true};
      progress({plan, outcomes, index, total: plan.items.length, item});
      try {
        const data = await request("/wall-mount/assembly-build-one", {method: "POST", body: {project: copy(working), scope: copy(scope), assembly_id: item.id}});
        if (cancelled()) return {cancelled: true};
        if (data?.status === "ready") {
          const next = mergeAssembly(collection(working), data.assembly, item.id); working.properties ||= {}; working.properties.mount_assemblies = next;
          outcomes.push({item, status: "ready", assembly: copy(data.assembly), warnings: list(data.warnings)});
        } else outcomes.push({item, status: "skipped", message: data?.message || list(data?.warnings)[0] || "Для этой сборки требуется ручное уточнение. Другие узлы не изменены.", warnings: list(data?.warnings).slice(1)});
      } catch (error) {
        if (cancelled()) return {cancelled: true};
        outcomes.push({item, status: "error", message: error.message});
      }
      progress({plan, outcomes, index: index + 1, total: plan.items.length});
    }
    return {plan, outcomes, ready: outcomes.filter(outcome => outcome.status === "ready").length, assemblies: copy(collection(working)), initial: fingerprint(project)};
  }
  function create(host, options) {
    const NS = "http://www.w3.org/2000/svg";
    const make = (tag, cls, text) => {const element = document.createElement(tag); if (cls) element.className = cls; if (text !== undefined) element.textContent = text; return element;};
    const svg = (tag, attrs = {}) => {const element = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, String(value))); return element;};
    const button = (text, action, cls = "button button-secondary") => {const element = make("button", cls, text); element.type = "button"; element.addEventListener("click", action); return element;};
    let alive = true, opened = false, busy = false, applying = false, sequence = 0, initial = "", scope = null, plan = null, result = null, outcomes = [], selectedId = null, deleteArmed = null, deleteControls = [], showAllSaved = false;
    const shell = make("section", "mount-assemblies-workspace"), header = make("header", "wm-header"), heading = make("div");
    heading.append(make("h2", "", "Сборки узлов STEP"), make("p", "wm-help", "Профили, панели, каркас и крепёж в одной 3D-сборке. Выберите узел, чтобы увидеть сечение, состав и проверку соединений.")); header.append(heading, button("Закрыть", () => close()));
    const status = make("p", "ma-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const intro = make("details", "ma-notes"); intro.append(make("summary", "", "Примечания к сборкам"), make("p", "wm-help", "Профили берутся из исходных STEP, панели строятся по заданным размерам. Доступные сочетания и условия посадки перечислены у каждого узла. Крепление и нагрузки рассматриваются отдельно от геометрии сечения."));
    const grid = make("div", "ma-grid"), rail = make("aside", "ma-rail"), listHost = make("div", "ma-list"), main = make("div", "ma-main"); rail.append(make("h3", "", "Узлы выбранной области"), listHost); grid.append(rail, main);
    const footer = make("footer", "ma-footer"), buildButton = button("Построить сборки", build, "button button-primary"), applyButton = button("Добавить сборки в проект", apply, "button button-primary"), stopButton = button("Остановить очередь", stop), restartButton = button("Обновить план", () => {if (hasDraft()) {status.textContent = "Сначала добавьте построенные сборки или закройте окно без добавления."; return;} open(scope);}, "mini-button");
    footer.append(restartButton, stopButton, buildButton, applyButton);
    const discard = make("div", "ma-discard"); discard.hidden = true; discard.setAttribute("role", "alert"); discard.append(make("p", "", "Сборки ещё не добавлены в проект. Закрыть окно и отбросить результаты текущего построения?"), button("Закрыть без добавления", () => close({force: true})), button("Продолжить работу", () => {discard.hidden = true;}));
    shell.append(header, intro, status, grid, discard, footer); host.replaceChildren(shell);
    const current = () => options.getProject?.();
    function projectRecords() {try {return list(collection(current()).items);} catch (_) {return [];}}
    function savedRecords() {return projectRecords().filter(item => list(plan?.saved_ids).includes(item.id));}
    function hasDraft() {return opened && (busy || Boolean(result?.ready));}
    function stale() {return fingerprint(current()) !== initial;}
    function canDelete() {return alive && opened && !busy && !result?.ready && !stale();}
    function update() {
      buildButton.disabled = !opened || busy || !plan?.items?.length || Boolean(result) || stale();
      applyButton.hidden = !result?.ready; applyButton.disabled = busy || !result?.ready || stale();
      stopButton.hidden = !busy; restartButton.disabled = busy || Boolean(result?.ready); listHost.setAttribute("aria-busy", String(busy));
      deleteControls.forEach(control => {control.disabled = !canDelete();});
    }
    function message(value) {return typeof value === "string" ? value : value?.message || "Требуется проверка.";}
    function locationHint(item) {
      const locations = list(item?.locations);
      return locations.length ? make("span", "ma-item-location", locations[0] + (locations.length > 1 ? ` · ещё ${locations.length-1}` : "")) : null;
    }
    function renderList(activeItem) {
      listHost.replaceChildren(); deleteControls = [];
      for (const item of list(plan?.items)) {
        const outcome = outcomes.find(value => value.item.id === item.id), control = button(item.label || "Сборка узла", () => {selectedId = item.id; renderList(activeItem); renderDetail();}, "ma-item");
        control.setAttribute("aria-pressed", String(item.id === selectedId)); control.setAttribute("data-assembly-id", item.id);
        control.append(make("span", "ma-item-status", activeItem?.id === item.id ? "Построение…" : outcome?.status === "ready" ? "Сечение готово" : outcome?.status === "error" ? "Ошибка построения" : outcome?.status === "skipped" ? "Требует уточнения" : "Ожидает построения")); const hint=locationHint(item); if(hint) control.append(hint); listHost.append(control);
      }
      for (const record of savedRecords()) {const control = button(record.label || "Сборка узла", () => {selectedId = record.id; renderList(activeItem); renderDetail();}, "ma-item"); control.setAttribute("aria-pressed", String(record.id === selectedId)); control.append(make("span", "ma-item-status", record.fastener_assembly?.status === "unsupported" ? "Профили и панели · крепёж не собран" : "Сохранена в проекте")); const hint=locationHint(list(plan?.saved_items).find(item=>item.id===record.id)); if(hint) control.append(hint); listHost.append(control);}
      if (!list(plan?.items).length && !savedRecords().length) listHost.append(make("p", "wm-help", "Новых поддерживаемых сборок в этой области нет."));
      if (list(plan?.skipped).length) {const box = make("details", "ma-skipped"); box.open = !list(plan?.items).length && !list(plan?.saved_ids).length; box.append(make("summary", "", `Не требуется или невозможно перестроить · ${plan.skipped.length}`)); plan.skipped.forEach(item => box.append(make("p", "wm-help", `${item.label || "Узел"}: ${item.message || "Уже сохранён либо требует ручного уточнения."}`))); listHost.append(box);}
      const records = projectRecords();
      if (records.length) {
        const all = make("details", "ma-saved-manager"); all.open = showAllSaved || records.length >= MAX_ITEMS || Boolean(deleteArmed); all.addEventListener("toggle", () => {showAllSaved = all.open;});
        all.append(make("summary", "", `Все сохранённые сборки проекта · ${records.length} / ${MAX_ITEMS}`), make("p", "wm-help", "Можно освободить место, удалив отдельную сохранённую геометрию. Монтаж стен, исходники и сечения отдельных деталей не удаляются. Правку можно отменить."));
        records.forEach(record => {
          const row = make("div", "ma-saved-row"), view = button(record.label || "Сборка узла", () => {selectedId = record.id; showAllSaved = true; renderList(activeItem); renderDetail();}, "ma-saved-title"), matching = list(plan?.saved_ids).includes(record.id);
          view.setAttribute("aria-label", `Посмотреть сохранённую сборку ${record.label || record.id}`); row.append(view, make("span", "ma-saved-context", matching ? "Соответствует текущим параметрам и области" : "Не относится к текущим параметрам или выбранной области"));
          const remove = button("Удалить геометрию…", () => removeSaved(record.id), "mini-button ma-remove"); remove.setAttribute("data-remove-assembly-id", record.id); remove.disabled = !canDelete(); deleteControls.push(remove); row.append(remove);
          if (deleteArmed?.id === record.id) {
            const confirmation = make("div", "ma-delete-confirm"); confirmation.setAttribute("role", "alert"); confirmation.append(make("p", "", "Удалить только эту сохранённую сборку? Монтаж стен, остальные сборки, исходные STEP и отдельные сечения останутся."));
            const confirm = button("Подтвердить удаление сборки", () => removeSaved(record.id, true), "mini-button ma-remove"); confirm.disabled = !canDelete(); deleteControls.push(confirm); confirmation.append(confirm, button("Оставить сборку", () => {deleteArmed = null; renderList(activeItem);}, "mini-button")); row.append(confirmation);
          }
          all.append(row);
        });
        listHost.append(all);
      }
    }
    function renderNotes(title, values) {
      if (!list(values).length) return;
      const box = make("details", "ma-notes"); box.append(make("summary", "", `${title} · ${values.length}`)); values.forEach(value => box.append(make("p", "wm-help", message(value)))); main.append(box);
    }
    function renderDetail() {
      main.replaceChildren(); const outcome = outcomes.find(value => value.item.id === selectedId), record = outcome?.assembly || projectRecords().find(value => value.id === selectedId), item = list(plan?.items).find(value => value.id === selectedId) || list(plan?.saved_items).find(value => value.id === selectedId) || record;
      if (!item) {main.append(make("p", "ma-empty", "Здесь появится сечение выбранной сборки. Существующие сечения отдельных деталей не изменяются.")); renderNotes("Примечания", plan?.warnings); return;}
      main.append(make("h3", "", item.label || "Сборка узла"));
      if (record && !outcome?.assembly && !list(plan?.saved_ids).includes(record.id)) main.append(make("p", "wm-help", "Сохранённая сборка не относится к текущим параметрам или выбранной области. Она может применяться в другой части проекта."));
      if (record) {
        const fastening = fasteningSummary(record), contacts = contactSummary(record), connectivity = assemblyConnectivity(record);
        main.append(picture(record), make("p", "ma-legend", "Фиолетовый — STEP-профили и каркас. Золотистый — панели и подоснова. Зелёный — клей. Тёмный — физический STEP-крепёж."));
        const config = record.configuration || {}; main.append(make("p", "ma-dimensions", `Толщина панели: ${mm(config.panel_thickness_mm || 0)} мм · шов: ${mm(config.joint_mm || 0)} мм · плоскость ${record.geometry_snapshot.request?.axis?.toUpperCase() || "—"} = ${mm(record.geometry_snapshot.request?.offset_mm || 0)} мм`));
        if (fastening.count) {
          const visibleFasteners = record.bodies.filter(body => body.role === "fastener" && list(record.geometry_snapshot.result.components.find(component => component.id === body.id)?.paths).length).length;
          main.append(make("p", "ma-fastening-summary", `Крепёж в 3D: ${fastening.count} шт. · В этом разрезе: ${visibleFasteners} · Посадки проверены: ${fastening.valid} из ${fastening.count}`));
          main.append(make("p", fastening.complete && !fastening.collisions ? "ma-proof" : "ma-error", fastening.complete
            ? `Запрещённые пересечения: ${fastening.collisions}. Вход стержня в материал проверен отдельно: ${fastening.engagements.length} зон зацепления. Опора головок и глубина зацепления проверены по телам сборки.`
            : "Проверка части соединений отсутствует или не пройдена. Откройте результаты ниже и перестройте узел."));
          const checks = list(record.fastening_checks).map(check => {
            const body = record.bodies.find(value => value.id === check.fastener_body_id), engagements = fastening.engagements.filter(value => value.fastening_id === check.id);
            return `${body?.label || check.fastener_id || "Крепёж"}, поз. ${check.fastener_body_id}: ${check.status === "valid" ? "посадка проверена" : "требует исправления"}; опора головки ${mm(check.bearing_contact_area_mm2 || 0)} мм². ` + engagements.map(value => {
              const target = record.bodies.find(body => body.instance_id === value.target_id);
              return `${target?.label || value.target_id}: глубина ${mm(value.depth_mm)} мм, минимум ${mm(value.min_engagement_mm)} мм`;
            }).join("; ");
          });
          renderNotes("Проверка 3D-крепежа", checks);
          main.append(make("p", connectivity.connected ? "ma-proof" : "ma-error", connectivity.connected
            ? `Все ${connectivity.total} тел входят в одну сборку с проверенными контактами и зацеплениями.`
            : `Связность сборки не подтверждена: соединено ${connectivity.reached} из ${connectivity.total} тел.`));
          main.append(make("p", "wm-help", "Количество относится к местному узлу, не ко всей стене. Проверяется геометрия соединений; расчёт несущей способности этим не заменяется."));
        } else {
          main.append(make("p", "ma-proof", "Проверка геометрии: пересечений свыше 0,001 мм³ нет."));
          main.append(make("p", record.fastener_assembly?.status === "unsupported" ? "ma-error" : "wm-help", record.fastener_assembly?.status === "unsupported"
            ? `Крепёж не собран. ${record.fastener_assembly.message || "Для этого сочетания ещё нет проверяемой автоматической посадки. Сохранены только известные панели и профили."}`
            : "В этой сохранённой сборке ещё нет физических тел крепежа. Обновите план и постройте доступную сборку с крепежом."));
        }
        if (contacts.count || list(record.contact_checks).length) {
          main.append(make("p", contacts.complete ? "ma-proof" : "ma-error", `Опорные контакты проверены: ${contacts.valid} из ${contacts.count}. Проверяется, что детали соприкасаются заданной площадью, а не висят в воздухе или пересекаются.`));
          renderNotes("Контакты деталей", list(record.contact_checks).map(check => {
            const a = record.bodies.find(body => body.id === check.body_a_id), b = record.bodies.find(body => body.id === check.body_b_id);
            return `${a?.label || check.body_a} ↔ ${b?.label || check.body_b}: контакт ${mm(check.contact_area_mm2)} мм², минимум ${mm(check.min_area_mm2)} мм² · ${check.status === "valid" ? "проверено" : "требует исправления"}`;
          }));
        }
        const bodies = make("div", "ma-bodies"); record.bodies.forEach(body => {const row = make("div", "ma-body"); row.append(make("span", body.kind === "step" ? "ma-chip ma-step" : "ma-chip ma-generated", body.kind === "step" ? "STEP" : "По размерам"), make("span", "", `${body.id}. ${body.label || body.instance_id || "Тело сборки"}`)); if (!list(record.geometry_snapshot.result.components.find(component => component.id === body.id)?.paths).length) row.append(make("span", "wm-help", "Вне плоскости разреза · есть в 3D и составе")); if (list(body.cutouts).length) row.append(make("span", "wm-help", `Пазы и выборки: ${body.cutouts.length} · вырезаны из объёма панели`)); bodies.append(row);}); main.append(bodies);
        renderNotes("Примечания к посадке", record.assumptions); renderNotes("Примечания к построению", outcome?.warnings);
        const sources = make("details", "ma-notes"); sources.append(make("summary", "", "Исходные модели и происхождение")); record.source_models.forEach(model => sources.append(make("p", "wm-help", `${model.component_code || "Профиль"} · ${model.filename} · SHA256 ${model.source_id}`))); list(record.source_refs).forEach(source => sources.append(make("p", "wm-help", `${source.title || source.filename || source.document_id || "Методичка"}${source.page ? ` · стр. ${source.page}` : ""}`))); sources.append(make("p", "wm-help", `Снимок сборки: ${record.fingerprint}`)); main.append(sources);
      } else {
        main.append(make("p", outcome ? "ma-error" : "ma-empty", outcome?.message || "Сечение будет получено из собранных твёрдых тел. Нажмите «Построить сборки»."));
        if (list(item.component_codes).length) main.append(make("p", "wm-help", `Профили: ${item.component_codes.join(", ")}`));
        if (item.fastener_assembly?.status === "unsupported") main.append(make("p", "ma-error", `Автоматическая сборка крепежа недоступна. ${item.fastener_assembly.message || "Будут собраны только известные панели и профили."}`));
        renderNotes("Примечания к посадке", item.assumptions); renderNotes("Примечания к построению", outcome?.warnings);
      }
      if (list(item.locations).length) renderNotes("Где применяется", item.locations);
      main.append(make("p", "wm-help", "После сохранения эта геометрия попадёт в документацию для узлов с совпадающими параметрами и исходниками."));
    }
    function picture(record) {
      const geometry = record.geometry_snapshot.result, bounds = geometry.bounds_mm, picture = svg("svg", {class: "ma-preview", role: "img", "aria-label": `Сечение сборки: ${record.label}`});
      if (!bounds) return picture;
      const margin = Math.max(...bounds.size, 1) * .08;
      picture.setAttribute("viewBox", `${bounds.min[0] - margin} ${-bounds.max[1] - margin} ${Math.max(bounds.size[0], .01) + margin * 2} ${Math.max(bounds.size[1], .01) + margin * 2}`);
      [...geometry.components].sort((a, b) => Number(record.bodies.find(body => body.id === a.id)?.role === "fastener") - Number(record.bodies.find(body => body.id === b.id)?.role === "fastener")).forEach(component => {
        const body = record.bodies.find(body => body.id === component.id), kind = body?.kind || "step", role = body?.role;
        const pathData = points => points.map((point, index) => `${index ? "L" : "M"}${point[0]} ${-point[1]}`).join(" ");
        const attrs = {class: role === "fastener" ? "ma-contour-fastener" : role === "adhesive" ? "ma-contour-adhesive" : kind === "generated_box" ? "ma-contour-generated" : "ma-contour-step", "stroke-width": 1.6, "vector-effect": "non-scaling-stroke"};
        if (list(body?.section_loops).length) picture.append(svg("path", {...attrs, d: body.section_loops.map(points => `${pathData(points)} Z`).join(" "), fill: role === "fastener" ? "#ffffff" : role === "adhesive" ? "#dde9d8" : kind === "generated_box" ? "#f1e5c9" : "#e8dff8", "fill-rule": "evenodd"}));
        else list(component.paths).forEach(points => picture.append(svg("path", {...attrs, d: pathData(points), fill: "none"})));
      });
      return picture;
    }
    async function open(selectedScope) {
      if (!alive || hasDraft()) return false;
      if (!current() || !["space", "floor"].includes(selectedScope?.kind) || !selectedScope.id) {options.toast?.("Выберите помещение или этаж для сборок.", "error"); return false;}
      const token = ++sequence; opened = true; busy = true; scope = copy(selectedScope); initial = fingerprint(current()); plan = null; result = null; outcomes = []; selectedId = null; deleteArmed = null; discard.hidden = true; status.textContent = "Подбираем поддерживаемые сборки по сохранённому монтажу…"; renderList(); renderDetail(); update();
      try {
        collection(current()); const data = await options.request("/wall-mount/assembly-plan", {method: "POST", body: {project: W.requestProject(current(),{keepLocalAssemblies:true}), scope: copy(scope)}});
        if (!alive || !opened || token !== sequence) return false;
        if (stale()) {refresh(); return false;}
        plan = copy(checkedPlan(data)); selectedId = plan.items[0]?.id || savedRecords()[0]?.id || null; status.textContent = plan.items.length ? `Подготовлено типов узлов: ${plan.items.length}. Проверьте допущения и запустите построение.` : savedRecords().length ? "Точные сборки уже сохранены. Выберите узел слева для просмотра." : "Новых поддерживаемых сборок нет. Посмотрите причины в списке слева."; renderList(); renderDetail();
      } catch (error) {if (alive && opened && token === sequence) status.textContent = error.message;}
      finally {if (alive && opened && token === sequence) {busy = false; update();}}
      return true;
    }
    async function build() {
      if (!alive || !opened || busy || !plan?.items?.length || result || stale()) return false;
      const token = ++sequence, project = copy(current()); busy = true; outcomes = []; deleteArmed = null; update();
      try {
        const prepared = await prepare(project, scope, plan, options.request, data => {if (alive && opened && token === sequence && !stale()) {outcomes = data.outcomes; status.textContent = data.item ? `Строится ${data.index + 1} из ${data.total}: ${data.item.label}` : `Обработано ${data.index} из ${data.total}.`; renderList(data.item); renderDetail();}}, () => !alive || !opened || token !== sequence || stale());
        if (!alive || !opened || token !== sequence) return false;
        if (stale()) {refresh(); return false;}
        if (prepared.cancelled) return false;
        result = prepared; outcomes = prepared.outcomes; status.textContent = prepared.ready ? `Готово сборок: ${prepared.ready}. Проверьте сечения и добавьте их в проект одной правкой.` : "Ни одна новая сборка не построена. Существующие данные не изменены; посмотрите причины в списке."; renderList(); renderDetail();
      } catch (error) {if (alive && opened && token === sequence) status.textContent = error.message;}
      finally {if (alive && opened && token === sequence) {busy = false; update();}}
      return true;
    }
    function apply() {
      if (!opened || busy || !result?.ready || stale()) {if (opened && stale()) refresh(); return false;}
      try {
        const expected = initial; let next = copy(collection(current())); outcomes.filter(outcome => outcome.status === "ready").forEach(outcome => {next = mergeAssembly(next, outcome.assembly, outcome.item.id);});
        applying = true; const changed = options.mutate("добавление сборок узлов STEP", project => {if (fingerprint(project) !== expected) throw new Error("Проект изменился во время построения сборок."); project.properties ||= {}; project.properties.mount_assemblies = copy(next);});
        if (changed === false) return false;
        const count = result.ready; result = null; plan = {...plan, saved_ids: [...new Set([...list(plan.saved_ids), ...outcomes.filter(outcome => outcome.status === "ready").map(outcome => outcome.assembly.id)])], saved_items: [...list(plan.saved_items), ...outcomes.filter(outcome => outcome.status === "ready").map(outcome => outcome.item)], items: plan.items.filter(item => !outcomes.some(outcome => outcome.status === "ready" && outcome.item.id === item.id))}; initial = fingerprint(current()); status.textContent = `Добавлено сборок: ${count}. Сохраните проект, чтобы включить их в документацию. Всю правку можно отменить.`; options.toast?.("Сборки узлов добавлены в черновик", "success"); renderList(); renderDetail(); update(); return true;
      } catch (error) {status.textContent = error.message; return false;} finally {applying = false;}
    }
    function removeSaved(id, confirmed = false) {
      if (!canDelete()) {if (opened) status.textContent = "Сначала завершите расчёт и добавьте новые сборки или закройте окно без добавления. При изменении проекта обновите план."; return false;}
      const record = projectRecords().find(item => item.id === id); if (!record) return false;
      if (!confirmed || deleteArmed?.id !== id || deleteArmed.key !== fingerprint(record)) {deleteArmed = {id, key: fingerprint(record)}; showAllSaved = true; renderList(); return false;}
      const expected = initial, recordKey = deleteArmed.key;
      try {
        applying = true;
        const changed = options.mutate("удаление сохранённой геометрии STEP-сборки", project => {
          if (fingerprint(project) !== expected) throw new Error("Проект изменился. Удаление отменено; обновите план.");
          const stored = collection(project), exact = stored.items.find(item => item.id === id);
          if (!exact || fingerprint(exact) !== recordKey) throw new Error("Сборка изменилась. Подтвердите удаление заново.");
          project.properties.mount_assemblies = {schema_version: SCHEMA, items: stored.items.filter(item => item.id !== id).map(copy)};
        });
        if (changed === false) return false;
        initial = fingerprint(current()); deleteArmed = null; result = null; outcomes = []; options.toast?.("Удалена только сохранённая сборка. Можно отменить правку.", "success");
      } catch (error) {status.textContent = error.message; deleteArmed = null; return false;} finally {applying = false;}
      open(scope); return true;
    }
    function stop() {if (!busy) return; sequence++; busy = false; result = null; outcomes = []; status.textContent = "Очередь остановлена. Текущий расчёт ещё может завершаться на сервере; его результат не будет добавлен. Проект не изменён."; renderList(); renderDetail(); update();}
    function refresh() {if (opened && !applying && stale()) {sequence++; busy = false; result = null; plan = null; outcomes = []; deleteArmed = null; status.textContent = "Проект изменился. Старый расчёт отменён. Обновите план перед построением."; renderList(); renderDetail(); update();}}
    function close(settings = {}) {if (hasDraft() && !settings.force) {discard.hidden = false; return false;} opened = false; sequence++; busy = false; result = null; deleteArmed = null; discard.hidden = true; options.onClose?.(); return true;}
    update(); return {open, build, apply, refresh, close, hasDraft, dispose() {alive = false; opened = false; sequence++;}};
  }
  return {collection, checkedPlan, checkedRecord, fasteningSummary, contactSummary, assemblyConnectivity, mergeAssembly, preparedAssemblies, prepare, fingerprint, create};
});
