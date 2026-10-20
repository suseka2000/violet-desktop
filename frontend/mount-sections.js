(function (root, factory) {
  const common = typeof module === "object" && module.exports;
  const api = factory(common ? require("./wall-mount.js") : root.VioletWallMount,
    common ? require("./project-history.js") : root.VioletProjectHistory);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletMountSections = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (W, H) {
  "use strict";
  const copy = value => JSON.parse(JSON.stringify(value)), list = value => Array.isArray(value) ? value : [];
  const fingerprint = project => H.capture(project);
  const matches = (project, expected) => H.equal(fingerprint(project), expected);
  function mergeSection(assets, result, expectedSource) {
    if (result.status !== "ready") throw new Error("Сечение ещё не построено.");
    const section = result.section, asset = result.asset, snapshot = section?.geometry_snapshot;
    if (result.source_id !== expectedSource || asset?.source_id !== expectedSource || asset?.format !== "step" || snapshot?.source_id !== expectedSource || snapshot?.result?.source_id !== expectedSource || !section.id || !snapshot.geometry_sha256 || !(snapshot.result.point_count > 0)) throw new Error("Ответ не содержит непустое сечение выбранного STEP-файла.");
    const output = copy(assets), found = output.find(item => item.source_id === expectedSource);
    if (found && list(found.sections).some(item => item.id === section.id)) throw new Error("Сечение с таким идентификатором уже существует. Ничего не заменено.");
    if (found) {found.sections ||= []; found.sections.push(copy(section));}
    else output.push({source_id: expectedSource, filename: asset.filename, format: "step", sections: [copy(section)]});
    if (output.length > 50 || output.some(item => list(item.sections).length > 30) || output.reduce((total, item) => total + list(item.sections).reduce((sum, section) => sum + (section.geometry_snapshot?.result?.point_count || 0), 0), 0) > 120000) throw new Error("Превышен размер библиотеки сечений проекта. Другие сечения не изменены.");
    return output;
  }
  async function prepare(project, scope, request, progress = () => {}, cancelled = () => false) {
    const working = W.requestProject(project), initial = fingerprint(project), outcomes = [];
    const plan = await request("/wall-mount/section-plan", {method: "POST", body: {project: working, scope}});
    if (cancelled()) return {cancelled: true};
    if (plan?.schema_version !== "mount-section-plan/1.0" || !Array.isArray(plan.items) || plan.items.length > 8 || new Set(plan.items.map(item => item.source_id)).size !== plan.items.length) throw new Error("Получен некорректный план построения сечений.");
    progress({plan, index: 0, total: plan.items.length, outcomes});
    for (const [index, item] of plan.items.entries()) {
      if (cancelled()) return {cancelled: true};
      progress({plan, index, total: plan.items.length, item, outcomes});
      let response;
      try {
        response = await request("/wall-mount/section-build-one", {method: "POST", body: {project: working, scope, source_id: item.source_id}});
        if (cancelled()) return {cancelled: true};
        if (response.status === "ready") {
          working.properties ||= {};
          working.properties.cad_assets = mergeSection(list(working.properties.cad_assets), response, item.source_id);
          outcomes.push({item, status: "ready", section: response.section, warnings: response.warnings || []});
        } else outcomes.push({item, status: "skipped", reason: response.message || list(response.warnings)[0] || "Автоматическое сечение для этой детали не определено. Откройте модель в библиотеке и выберите плоскость вручную.", warnings: list(response.warnings).slice(1)});
      } catch (error) {
        if (cancelled()) return {cancelled: true};
        outcomes.push({item, status: "error", reason: error.message});
      }
      progress({plan, index: index + 1, total: plan.items.length, outcomes});
    }
    return {plan, outcomes, initial, assets: list(working.properties?.cad_assets), ready: outcomes.filter(item => item.status === "ready").length};
  }
  function create(host, options) {
    const make = (tag, cls, text) => {const element = document.createElement(tag); if (cls) element.className = cls; if (text !== undefined) element.textContent = text; return element;};
    const button = (text, action, cls = "button button-secondary") => {const element = make("button", cls, text); element.type = "button"; element.addEventListener("click", action); return element;};
    let sequence = 0, opened = false, busy = false, result = null, initial = "", applying = false;
    const shell = make("section", "mount-sections-workspace"), header = make("header", "wm-header"), title = make("div");
    title.append(make("h2", "", "Сечения STEP-профилей"), make("p", "wm-help", "Поперечные сечения отдельных деталей выбранного монтажа. Собранные узлы доступны в окне «Сборки узлов STEP»."));
    const closeButton = button("Закрыть", () => close()); header.append(title, closeButton);
    const content = make("div", "ms-content"), status = make("p", "ms-status"), notes = make("details", "wm-requirement-list"), cards = make("div", "ms-cards");
    notes.append(make("summary", "", "Примечания к выбору сечений"), make("p", "", "Для вытянутого однотелого профиля выбирается поперечная плоскость посередине. При нескольких вариантах сначала берётся файл без суффикса; остальные перечислены ниже. Сверьте выбранный вариант и положение плоскости. Произвольное сечение крепежа или сборки можно построить в библиотеке."));
    const footer = make("footer", "ms-footer"), discard = make("div", "wm-confirm"); discard.hidden = true;
    discard.append(make("span", "", "Закрыть без добавления построенных сечений?"), button("Закрыть без добавления", () => close({force: true})), button("Остаться", () => {discard.hidden = true;}));
    const applyButton = button("Добавить сечения в проект", apply, "button button-primary"), cancelButton = button("Остановить очередь", () => {if (!busy) return; sequence++; busy = false; result = null; status.textContent = "Очередь остановлена. Текущая деталь ещё может обрабатываться до минуты; её результат не будет добавлен. Проект не изменён."; update();});
    footer.append(cancelButton, applyButton); content.append(notes, status, cards); shell.append(header, content, discard, footer); host.replaceChildren(shell);
    function current() {return options.getProject();}
    function hasDraft() {return opened && (busy || !!result?.ready);}
    function update() {cancelButton.hidden = !busy; applyButton.disabled = busy || !result?.ready || !matches(current(), initial);}
    function renderProgress(data) {
      status.textContent = data.item ? `Строится ${data.index + 1} из ${data.total}: ${data.item.filename}` : `Обработано ${data.index} из ${data.total}.`;
      cards.replaceChildren();
      for (const outcome of data.outcomes) {
        const card = make("article", "ms-card"); card.append(make("h3", "", outcome.item.filename));
        const offset = outcome.section?.offset_mm;
        card.append(make("p", "", outcome.status === "ready" ? `${outcome.section.label} · ${outcome.section.axis.toUpperCase()} = ${(Math.abs(offset)<.0005 ? 0 : Number(offset)).toLocaleString("ru-RU")} мм` : outcome.reason));
        if (outcome.item.selection_reason) card.append(make("p", "wm-help", outcome.item.selection_reason));
        if (outcome.status === "ready") card.append(picture(outcome.section));
        if (list(outcome.warnings).length) {const remarks = make("details", "wm-requirement-list"); remarks.append(make("summary", "", `Примечания · ${outcome.warnings.length}`)); outcome.warnings.forEach(warning => remarks.append(make("p", "wm-help", typeof warning === "string" ? warning : warning.message || String(warning)))); card.append(remarks);} cards.append(card);
      }
      if (list(data.plan?.skipped).length) {const box = make("details", "ms-skipped"); box.append(make("summary", "", `Не включено в автоматическое построение: ${data.plan.skipped.length}`)); data.plan.skipped.forEach(item => box.append(make("p", "wm-help", `${item.component_code || item.filename || "Деталь"}: ${item.message || "Требуется ручной выбор модели или плоскости сечения."}`))); cards.append(box);}
    }
    function picture(section) {
      const geometry = section.geometry_snapshot.result, bounds = geometry.bounds_mm, NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
      svg.setAttribute("role", "img"); svg.setAttribute("aria-label", section.label); svg.setAttribute("class", "ms-preview");
      if (!bounds) return svg;
      const margin = Math.max(...bounds.size, 1) * .08, lineWidth = Math.max(...bounds.size, 1) * .004;
      svg.setAttribute("viewBox", `${bounds.min[0]-margin} ${-bounds.max[1]-margin} ${Math.max(bounds.size[0], .01)+margin*2} ${Math.max(bounds.size[1], .01)+margin*2}`);
      list(geometry.components).forEach(component => list(component.paths).forEach(points => {const path = document.createElementNS(NS, "path"); path.setAttribute("d", points.map((point, index) => `${index ? "L" : "M"}${point[0]} ${-point[1]}`).join(" ")); path.setAttribute("fill", "none"); path.setAttribute("stroke", "#283650"); path.setAttribute("stroke-width", String(lineWidth)); svg.append(path);}));
      return svg;
    }
    async function open(scope) {
      if (hasDraft()) return false;
      const token = ++sequence, project = H.clone(current()); opened = true; busy = true; result = null; initial = fingerprint(project); discard.hidden = true; cards.replaceChildren(); status.textContent = "Подбираю STEP-профили по сохранённому монтажу…"; update();
      try {
        const prepared = await prepare(project, scope, options.request, data => {if (token === sequence) renderProgress(data);}, () => token !== sequence || !opened);
        if (token !== sequence || !opened || prepared.cancelled) return false;
        result = prepared; status.textContent = prepared.ready ? `Готово сечений: ${prepared.ready}. Проверьте рисунки и добавьте в проект одной правкой.` : "Новых сечений нет. Посмотрите причины пропусков; существующие сечения не изменены.";
      } catch (error) {if (token === sequence) status.textContent = error.message;}
      finally {if (token === sequence) {busy = false; update();}}
      return true;
    }
    function apply() {
      if (busy || !result?.ready || !opened) return false;
      if (!matches(current(), initial)) {status.textContent = "Проект изменился. Закройте окно и постройте сечения заново."; update(); return false;}
      try {
        applying = true; const assets = copy(result.assets), expected = initial;
        const changed = options.mutate("добавление сечений STEP-профилей монтажа", project => {if (!matches(project, expected)) throw new Error("Проект изменился во время построения."); project.properties ||= {}; project.properties.cad_assets = assets;});
        if (changed === false) return false;
        const count = result.ready; result = null; initial = fingerprint(current()); status.textContent = `Добавлено сечений: ${count}. После сохранения они попадут в документацию. Операцию можно отменить.`; options.toast?.("STEP-сечения добавлены в черновик", "success"); update(); return true;
      } catch (error) {status.textContent = error.message; return false;} finally {applying = false;}
    }
    function refresh() {if (opened && !applying && !matches(current(), initial)) {sequence++; busy = false; result = null; status.textContent = "Проект изменился. Старый расчёт отменён: закройте окно и запустите построение заново."; update();}}
    function close(settings = {}) {if (hasDraft() && !settings.force) {discard.hidden = false; return false;} opened = false; sequence++; busy = false; result = null; options.onClose?.(); return true;}
    update(); return {open, apply, close, refresh, hasDraft, dispose() {opened = false; sequence++;}};
  }
  return {mergeSection, prepare, fingerprint, create};
});
