(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports ? require("./wall-mount.js") : root.VioletWallMount,
    typeof module === "object" && module.exports ? require("./batch-mount.js") : root.VioletBatchMount,
    () => typeof module === "object" && module.exports ? require("./room-assembly.js") : root.VioletRoomAssembly);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletWholeWall = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (W, B, roomViewer) {
  "use strict";
  const list = value => Array.isArray(value) ? value : [], copy = value => JSON.parse(JSON.stringify(value)), key = value => `${value.wall_id}\u0000${value.face_id}`;
  const SCHEMA = "whole-wall-assemblies/1.0", SHA = /^[a-f0-9]{64}$/, mm = value => Number(value).toLocaleString("ru-RU", {maximumFractionDigits: 2});
  const defaults = Object.freeze({frame_pitch_mm: 600, screw_pitch_mm: 400, anchor_pitch_mm: 600,
    screw_edge_mm: 50, anchor_edge_mm: 100, opening_edge_mm: 100, cavity_mm: 80,
    substrate_type: "concrete", substrate_thickness_mm: 160, anchor_embedment_mm: 50, allow_project_assumptions: true});
  const MATERIALS={veneer:"Шпон",hpl:"HPL",painted:"Окрашенные панели",unspecified:"Материал не задан"};
  const ROLES={panel:"Панели",frame:"Каркас",profile:"Монтажный профиль",backing:"Подоснова",adhesive:"Клеевой слой",fastener:"Саморез",anchor_bolt:"Анкерный болт",anchor_sleeve:"Анкерная гильза",spacer:"Дистанционная опора"};
  const PROFILE_NAMES={vh2:"VH-2",vh3:"VH-3",lstart:"V-L-start (адаптация)"};
  function checkDescription(check) {
    if(typeof check === "string") return check;
    const names={actual_frame_support_coverage:"Покрытие стены каркасом",frame_support_pitch:"Шаг стоек",frame_members_nonoverlap:"Пересечения элементов каркаса",global_fastener_head_clearance:"Расстояние между головками крепежа",groove_remaining_web:"Материал между встречными пазами",panel_profile_engagement:"Опора панели на профиль",profile_fasteners_not_empty:"Крепление каждого профиля",profile_fasteners_pitch:"Шаг саморезов профиля",profiles_nonoverlap:"Пересечения профилей",profiles_not_empty:"Наличие монтажных профилей"};
    let text=check.message||check.label||names[check.code]||check.code||"Геометрическая проверка";
    if(check.code==="groove_remaining_web"&&check.status==="invalid")text=`${check.entity_id}: между пазами остаётся ${mm(check.minimum_mm)} мм. Увеличьте ширину добора или измените раскладку панелей; требуется не менее ${mm(check.required_mm)} мм материала.`;
    if(check.code==="actual_frame_support_coverage"&&check.status==="invalid")text=`Недостаточно опор на высоте ${mm(check.z_start_mm)}–${mm(check.z_end_mm)} мм. Промежуток ${mm(check.maximum_gap_mm)} мм при допустимом ${mm(check.gap_limit_mm)} мм; отступ крайней опоры ${mm(check.maximum_overhang_mm)} мм. Измените шаг или отступы каркаса.`;
    if(check.code==="profile_fasteners_not_empty"&&check.status==="invalid")text=`${check.entity_id}: не осталось места для крепежа профиля. Измените шаг или отступы каркаса.`;
    return (check.status==="invalid"?"Не пройдено: ":check.status==="valid"?"Проверено: ":"")+text;
  }
  function mountingDescription(config) {
    if (!config || !config.mounting_system) return "Монтажная система не задана";
    const parts=[{open_mount:"Открытый монтаж",concealed_mount:"Скрытый монтаж"}[config.mounting_system]||config.mounting_system];
    if(config.panel_edge)parts.push(`торец ${config.panel_edge}`);
    if(config.material)parts.push(MATERIALS[config.material]||config.material);
    if(Number.isFinite(config.panel_thickness_mm))parts.push(`панель ${mm(config.panel_thickness_mm)} мм`);
    if(Number.isFinite(config.joint_mm))parts.push(`шов ${mm(config.joint_mm)} мм`);
    return parts.join(" · ");
  }
  function configuration(value) {return Object.fromEntries(Object.keys(defaults).map(name => [name, value?.[name] ?? defaults[name]]));}
  function savedSettings(project) {
    const value = project?.properties?.whole_wall_settings;
    if (value === undefined) return new Map();
    if (value?.schema_version !== "whole-wall-settings/1.0" || !Array.isArray(value.walls)) throw Error("Неизвестный формат настроек сборки стен. Сохранённые параметры не изменены.");
    const result = new Map();
    for (const item of value.walls) {
      if (!item || typeof item.wall_id !== "string" || typeof item.face_id !== "string" || !item.configuration || typeof item.configuration !== "object" || Array.isArray(item.configuration) || result.has(key(item))) throw Error("Повреждены или повторяются настройки стороны стены. Сохранённые параметры не изменены.");
      result.set(key(item), copy(item));
    }
    return result;
  }
  function mergeSettings(project, records, {preferExisting=false}={}) {
    const settings = savedSettings(project);
    for (const record of records) if (!preferExisting || !settings.has(key(record))) settings.set(key(record), {wall_id:record.wall_id,face_id:record.face_id,configuration:copy(record.configuration)});
    return {schema_version:"whole-wall-settings/1.0",walls:[...settings.values()]};
  }
  function collection(project) {
    const saved = project?.properties?.whole_wall_assemblies;
    if (saved === undefined) return {schema_version: SCHEMA, items: []};
    if (saved?.schema_version !== SCHEMA || !Array.isArray(saved.items)) throw Error("Неизвестный формат сборок стен. Сохранённые данные не изменены.");
    return saved;
  }
  function requestProject(project) {
    const properties = {...project.properties, whole_wall_settings:mergeSettings(project,collection(project).items,{preferExisting:true})};
    delete properties.mount_assemblies;
    delete properties.whole_wall_assemblies;
    return copy({...project,properties});
  }
  function targets(project, scope = null) {
    const allowed = scope?.kind === "space" ? B.targetKeys(project, scope) : null;
    return list(project?.properties?.wall_mounting?.walls).flatMap(entry => {
      const ctx = W.wallContext(project, entry.wall_id), face = list(ctx?.wall?.faces).find(value => value.id === entry.face_id);
      if (!ctx || !face) return [];
      if (scope?.kind?.startsWith("exterior_") && (entry.configuration?.installation_zone!=="exterior" || (scope.kind==="exterior_floor" ? ctx.floor.id!==scope.id : ctx.building.id!==scope.id))) return [];
      if (scope?.wall_id && (scope.wall_id !== entry.wall_id || scope.face_id && scope.face_id !== entry.face_id)) return [];
      if (scope?.kind === "floor" && ctx.floor.id !== scope.id || allowed && !allowed.has(key(entry))) return [];
      const room = list(ctx.floor.spaces).find(value => value.id === face.space_id);
      return [{wall_id: entry.wall_id, face_id: entry.face_id, floor_id: ctx.floor.id,
        label: `${ctx.floor.name || "Этаж"} · ${room?.name || "Без помещения"} · ${ctx.wall.name || "Стена"} · ${face.side === "right" ? "правая сторона" : "левая сторона"}`}];
    });
  }
  function checkedPlan(value, target) {
    const plan = value?.plan || value;
    if (plan?.schema_version !== "wall-framing-plan/1.0" || !["ready", "invalid", "unsupported"].includes(plan.status) ||
      plan.wall?.id !== target.wall_id || plan.wall?.face_id !== target.face_id || !Number.isFinite(plan.wall?.length_mm) || plan.wall.length_mm <= 0 || !Number.isFinite(plan.wall?.height_mm) || plan.wall.height_mm <= 0 ||
      !["panels", "openings", "posts", "screws", "anchors"].every(name => Array.isArray(plan[name]))) throw Error("Раскладка не соответствует выбранной стороне стены.");
    if (plan.posts.some(post => ![post.x_mm, post.z_start_mm, post.z_end_mm, post.width_mm].every(Number.isFinite) || post.z_end_mm <= post.z_start_mm || post.width_mm <= 0) ||
      list(plan.rails).some(rail => ![rail.z_mm, rail.x_start_mm, rail.x_end_mm, rail.width_mm].every(Number.isFinite) || rail.x_end_mm <= rail.x_start_mm || rail.width_mm <= 0) ||
      [...plan.screws, ...plan.anchors].some(point => ![point.x_mm, point.z_mm].every(Number.isFinite) || ![...plan.posts,...list(plan.rails)].some(post => post.id === point.post_id))) throw Error("В раскладке есть некорректные координаты каркаса или крепежа.");
    if (["profile_runs","profile_screws"].some(name=>plan[name]!==undefined&&!Array.isArray(plan[name])) ||
      list(plan.profile_runs).some(run=>!run.id||!["vertical","horizontal"].includes(run.axis)||![run.start_mm,run.end_mm,run.edge_mm].every(Number.isFinite)||run.end_mm<=run.start_mm) ||
      new Set(list(plan.profile_runs).map(run=>run.id)).size!==list(plan.profile_runs).length ||
      list(plan.profile_screws).some(point=>![point.x_mm,point.z_mm,point.seat_y_mm].every(Number.isFinite)||!list(plan.profile_runs).some(run=>run.id===point.profile_id))) throw Error("В раскладке есть некорректные профили или точки их крепления.");
    return plan;
  }
  function checkedRecord(record, target) {
    if (!record || !SHA.test(record.id) || !SHA.test(record.fingerprint) || record.wall_id !== target.wall_id || record.face_id !== target.face_id) throw Error("3D-сборка не соответствует выбранной стене.");
    const plan = checkedPlan(record.plan, target), step = record.assembled_step, geometry = record.geometry;
    if (plan.status !== "ready" || !SHA.test(step?.source_id) || typeof step.base64 !== "string" || !step.base64 || !Number.isInteger(step.byte_length) || step.byte_length <= 0 ||
      step.encoding !== "xz+base64") throw Error("Нет проверяемого STEP-снимка всей стены.");
    if (!W.equal(record.configuration,plan.configuration) || record.approval_status !== "unreviewed" || geometry?.schema !== "whole-wall-assembly-build/1" || !Array.isArray(geometry.collisions) || geometry.collisions.length || geometry.checks?.status !== "valid") throw Error("Проверка 3D-сборки не завершена или обнаружены недопустимые пересечения.");
    const valid = checks => Array.isArray(checks) && checks.length > 0 && checks.every(check => check.status === "valid" && !list(check.issues).length);
    if (!valid(geometry.fastening_checks) || geometry.fastening_checks.length !== plan.screws.length+list(plan.profile_screws).length || !valid(geometry.anchor_checks) || geometry.anchor_checks.length !== plan.anchors.length || !valid(geometry.contact_checks)) throw Error("Не все соединения каркаса, крепёж и анкеры прошли проверку.");
    return record;
  }
  function mergeRecords(project, records, expected) {
    if (expected !== undefined && W.stableKey(project) !== expected) throw Error("Проект изменился во время расчёта. Сборки не заменены; повторите построение.");
    const stored = collection(project), byFace = new Map(stored.items.map(item => [key(item), copy(item)]));
    if (byFace.size !== stored.items.length) throw Error("В сохранённых сборках повторяются стороны стен. Данные не изменены.");
    for (const record of records) {checkedRecord(record, record); byFace.set(key(record), copy(record));}
    const items = [...byFace.values()];
    return {schema_version: SCHEMA, items};
  }
  function wireGeometry(preview, {hideBase=true, hidePanels=false}={}) {
    if (preview?.schema !== "wall-assembly-wire-preview/1" || !Array.isArray(preview.definitions) || !Array.isArray(preview.instances)) throw Error("Нет поддерживаемого 3D-просмотра сборки.");
    const definitions=new Map(preview.definitions.map(value=>[value.id,value])), paths=[], min=[Infinity,Infinity,Infinity], max=[-Infinity,-Infinity,-Infinity]; let points=0;
    const finitePoint=value=>Array.isArray(value)&&value.length===3&&value.every(Number.isFinite);
    function boundPaths(bounds) {
      if (!finitePoint(bounds?.min)||!finitePoint(bounds?.max)) throw Error("Повреждены габариты 3D-детали.");
      const vertices=Array.from({length:8},(_,index)=>[0,1,2].map(axis=>bounds[(index>>axis)&1?"max":"min"][axis]));
      return vertices.flatMap((value,index)=>[0,1,2].filter(axis=>!(index&(1<<axis))).map(axis=>[value,vertices[index|(1<<axis)]]));
    }
    for (const instance of preview.instances) {
      if (hideBase&&instance.role==="substrate"||hidePanels&&["panel","adhesive"].includes(instance.role)) continue;
      const definition=definitions.get(instance.definition_id), rotation=instance.rotation, translation=instance.translation_mm;
      if (!definition||!Array.isArray(rotation)||rotation.length!==3||!rotation.every(finitePoint)||!finitePoint(translation)) throw Error("Повреждено положение 3D-детали.");
      const source=definition.representation==="bounds_only"?boundPaths(definition.bounds_mm):list(definition.paths);
      for(const path of source) {
        if (!Array.isArray(path)||path.length<2||!path.every(finitePoint)) throw Error("Повреждены контуры 3D-детали.");
        points+=path.length;
        const transformed=path.map(point=>rotation.map((row,axis)=>{const value=row.reduce((sum,factor,index)=>sum+factor*point[index],translation[axis]);min[axis]=Math.min(min[axis],value);max[axis]=Math.max(max[axis],value);return value;}));
        paths.push({role:instance.role,instance_id:instance.id,points:transformed});
      }
    }
    if(!paths.length)throw Error("В выбранном режиме нет видимых 3D-деталей.");
    return {paths,min,max,point_count:points};
  }
  function projectWirePoint(point, center, yaw, pitch) {
    const [x,y,z]=point.map((value,axis)=>value-center[axis]),h=x*Math.cos(yaw)+y*Math.sin(yaw),depth=-x*Math.sin(yaw)+y*Math.cos(yaw);
    return [h,-z*Math.cos(pitch)+depth*Math.sin(pitch)];
  }
  function create(host, options) {
    // room-assembly.js may load after this module. Resolve the shared renderer
    // when the UI is created, after all deferred scripts have been evaluated.
    const R=roomViewer();
    const make = (tag, cls, text) => {const item = document.createElement(tag); if (cls) item.className = cls; if (text !== undefined) item.textContent = text; return item;};
    const svg = (tag, attrs) => {const item = document.createElementNS("http://www.w3.org/2000/svg", tag); Object.entries(attrs || {}).forEach(([name,value]) => item.setAttribute(name,String(value))); return item;};
    const button = (label, action, cls = "button button-secondary") => {const item = make("button", cls, label); item.type = "button"; item.addEventListener("click", action); return item;};
    let opened = false, alive = true, busy = false, sequence = 0, initial = "", scope = null, available = [], plan = null, staged = [], selectedRecord = null, savedState = "unchecked", applying = false, viewMode="plan", thicknessEdited=false, capabilities=[];
    let viewer=null,geometryStore=null,previewSource=null;
    const current = () => options.getProject(), stale = () => W.stableKey(current()) !== initial;
    const shell = make("section", "whole-wall-workspace"), header = make("header", "wm-header"), heading = make("div");
    heading.append(make("h2", "", "Сборка стены целиком"), make("p", "wm-help", "Панели, стойки, саморезы и анкеровка к основанию. Крепёж распределяется по всей стене с обходом проёмов."));
    header.append(heading, button("Закрыть", () => close()));
    const status = make("p", "ww-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const grid = make("div", "ww-grid"), settings = make("aside", "ww-settings"), drawing = make("div", "ww-drawing"), controls = {};
    const selector = make("select"), field = (title, input, hint) => {const label = make("label", "wm-field"); input.setAttribute("aria-label", title); label.append(make("span", "", title), input); if (hint) label.append(make("small", "wm-help", hint)); return label;};
    selector.addEventListener("change", () => {if (busy || staged.length) return; sequence++; plan = null; selectedRecord = null; loadSaved(); update(); render(); verifySaved();}); settings.append(field("Сторона стены", selector));
    const parameterBox = make("div", "ww-parameters");
    const numeric = (name, label, hint, min = 1) => {const input = make("input"); input.type = "number"; input.min = String(min); input.step = "1"; input.value = String(defaults[name]); controls[name] = input; input.addEventListener("input", invalidatePlan); parameterBox.append(field(label, input, hint));};
    numeric("frame_pitch_mm", "Шаг стоек, мм", "Максимум; возле края панели и проёма стойки добавятся автоматически.");
    numeric("screw_pitch_mm", "Шаг саморезов, мм", "Максимальное расстояние по высоте стойки.");
    numeric("anchor_pitch_mm", "Шаг анкеров, мм", "Максимальное расстояние между точками крепления к основанию.");
    numeric("cavity_mm", "От панели до основания, мм", "От задней поверхности панели до передней грани основания.");
    const substrate = make("select"); for (const [value,label] of [["concrete","Бетон"],["solid_masonry","Полнотелая кладка"],["hollow_masonry","Пустотелая кладка"],["gypsum","Гипсовое основание"],["unknown","Основание не определено"]]) {const option = make("option", "", label); option.value = value; substrate.append(option);} substrate.value = defaults.substrate_type; controls.substrate_type = substrate; substrate.addEventListener("change", invalidatePlan); parameterBox.append(field("Принятое основание", substrate, "Материал основания задаётся отдельно: геометрия стены сама по себе не определяет материал."));
    numeric("substrate_thickness_mm", "Толщина основания, мм"); numeric("anchor_embedment_mm", "Заделка анкера, мм", "Проектная геометрия. Несущая способность зависит от выбранного анкера и основания.");
    const advanced = make("details", "ww-advanced"), advancedFields = make("div", "ww-parameters"); advanced.append(make("summary", "", "Отступы от краёв"));
    for (const [name,label] of [["screw_edge_mm","Саморез от края, мм"],["anchor_edge_mm","Анкер от края стены, мм"],["opening_edge_mm","Анкер от проёма, мм"]]) {numeric(name,label); advancedFields.append(parameterBox.children[parameterBox.children.length-1]);} advanced.append(advancedFields);
    const allow = make("input"); allow.type = "checkbox"; allow.checked = true; controls.allow_project_assumptions = allow; allow.addEventListener("change", invalidatePlan);
    const assumptions = field("Использовать проектные размеры крепления", allow, "Сохраняются вместе со сборкой. Это геометрическая модель соединений, без расчёта нагрузок и выбора марки анкера."); assumptions.className += " ww-check";
    settings.append(parameterBox, advanced, assumptions,make("p","wm-help","Для всех стен: шаг крепежа общий; толщина основания берётся из каждой стены. Если изменить толщину вручную, введённое значение применится ко всей области."));
    const actions = make("div", "ww-actions"), previewButton = button("Показать раскладку", preview), buildButton = button("Собрать стену в 3D", () => build(false), "button button-primary"), allButton = button("Собрать все стены области", () => build(true)); actions.append(previewButton, buildButton, allButton); settings.append(actions);
    const viewControls=make("div","ww-view-controls"),planViewButton=button("Раскладка",()=>{viewMode="plan";render();update();},"mini-button"),solidViewButton=button("3D-сборка",()=>{viewMode="3d";render();update();},"mini-button"),hideBase=make("input"),hidePanels=make("input"),hideFrame=make("input"),hideNodes=make("input");
    const viewLabels=[];
    for(const [input,label,checked]of[[hideBase,"Скрыть основание",true],[hidePanels,"Скрыть панели",false],[hideFrame,"Скрыть каркас",false],[hideNodes,"Скрыть узлы",false]]){
      input.type="checkbox";input.checked=checked;const row=field(label,input);row.className="ww-view-check";viewLabels.push(row);input.addEventListener("change",()=>{viewer?.setVisible(visibleGroups());});
    }
    viewControls.append(planViewButton,solidViewButton,...viewLabels);
    const viewTools=make("div","ww-3d-tools");
    for(const [label,mode]of[["Изометрия","iso"],["Спереди","front"],["Сверху","top"]])viewTools.append(button(label,()=>viewer?.view(mode),"mini-button"));
    viewTools.append(button("Вписать",()=>viewer?.fit(),"mini-button"),button("Только узлы",()=>setGroups({panels:false,frame:false,nodes:true,base:false}),"mini-button"),button("Вся отделка",()=>setGroups({panels:true,frame:true,nodes:true,base:false}),"mini-button"));
    // The shared viewer paints its quality notice inside the canvas. Keep the
    // same live notice available to assistive technology, without repeating it.
    const solidViewport=make("div","ww-solid-viewport"),solidCanvas=make("canvas","ww-solid-canvas"),solidEmpty=make("p","ww-solid-empty"),solidDetail=make("p","ww-solid-detail sr-only");
    solidCanvas.tabIndex=0;solidCanvas.setAttribute("role","img");solidCanvas.setAttribute("data-view","3d");solidCanvas.setAttribute("aria-label","3D-просмотр STEP-сборки стены. Тяните для вращения, Shift для сдвига, колесо для масштаба. Стрелки — вращение, Home — вписать.");solidViewport.append(solidCanvas,solidEmpty);solidDetail.setAttribute("role","status");
    const systemCard=make("section","ww-system-card"),legend = make("p", "ww-legend", "Синие полосы — стойки · чёрные кресты — саморезы · оранжевые кольца — анкеры"), canvas = make("div", "ww-canvas"), facts = make("div", "ww-facts"), notes = make("div", "ww-notes");
    const inspection=make("details","ww-inspection"),inspectionSummary=make("summary","","Система, состав и проверки"),systemDetails=make("div","ww-system-details");inspection.append(inspectionSummary,systemDetails);inspection.hidden=true;
    drawing.append(systemCard,viewControls,viewTools,legend, canvas,solidDetail, facts, notes,inspection); grid.append(settings, drawing);
    const footer = make("footer", "ma-footer"), exportButton = button("Скачать STEP стены", download), removeButton = button("Удалить сборку стены", armRemove), applyButton = button("Добавить сборки в проект", apply, "button button-primary"), stopButton = button("Остановить очередь", stop); footer.append(exportButton, removeButton, stopButton, applyButton);
    const confirmation = make("div", "ma-discard"); confirmation.hidden = true;
    shell.append(header, status, grid, confirmation, footer); host.replaceChildren(shell);
    function selected() {return available.find(target => key(target) === selector.value) || available[0] || null;}
    function values() {return Object.fromEntries(Object.keys(defaults).map(name => [name, name === "allow_project_assumptions" ? controls[name].checked : name === "substrate_type" ? controls[name].value : Number(controls[name].value)]));}
    function fill(config) {for (const [name,value] of Object.entries(configuration(config))) {if (name === "allow_project_assumptions") controls[name].checked = value; else controls[name].value = String(value);}}
    function invalidatePlan(event) {if (busy) return; if(event?.target===controls.substrate_thickness_mm)thicknessEdited=true; sequence++; plan = null; selectedRecord = null; savedState = "unchecked"; viewMode="plan"; confirmation.hidden = true; status.textContent = "Параметры изменены. Обновите раскладку или постройте сборку заново."; render(); update();}
    function loadSaved() {
      const target = selected(), settings = target && savedSettings(current()).get(key(target));
      selectedRecord = target && list(collection(current()).items).find(record => key(record) === key(target)) || null;
      savedState = "unchecked"; thicknessEdited = false;
      if (selectedRecord) checkedRecord(selectedRecord,target);
      const config = settings?.configuration || selectedRecord?.configuration;
      fill(config || defaults);
      if (!config && target) {const thickness=W.wallContext(current(),target.wall_id)?.wall?.thickness_mm;if(Number.isFinite(thickness)&&thickness>0)controls.substrate_thickness_mm.value=String(thickness);}
      plan = selectedRecord?.plan || null;
      status.textContent = selectedRecord ? "Показан сохранённый снимок. Проверяем соответствие текущим параметрам, стене и монтажу…" : settings ? "Сохранённые настройки загружены. Геометрия рассчитывается отдельно; можно проверить раскладку или построить сборку." : available.length ? "Начните с раскладки: проверьте основание, шаг крепежа и положение стоек." : "Сначала назначьте монтаж выбранной стене, помещению или этажу.";
    }
    async function verifySaved() {
      if (!selectedRecord || !opened || busy) return;
      const token=sequence, record=selectedRecord, target=selected();
      try {const data=await options.request("/whole-wall/plan",{method:"POST",body:{project:requestProject(current()),wall_id:target.wall_id,face_id:target.face_id,configuration:values()}});
        if (!alive || !opened || sequence!==token || selectedRecord!==record || stale()) return;
        if(Array.isArray(data.capabilities))capabilities=copy(data.capabilities);
        savedState=W.equal(checkedPlan(data,target),record.plan)?"current":"stale";
        status.textContent=savedState==="current"?"Сохранённая 3D-сборка соответствует текущей стене и монтажу.":"Сохранённая сборка устарела: размеры, проёмы или монтаж изменились. Постройте стену заново.";
      } catch(error) {if (!alive || !opened || sequence!==token || selectedRecord!==record || stale()) return; savedState="stale"; status.textContent=`Сборка не подтверждена для текущей стены: ${error.message}`;}
      render(); update();
    }
    function update() {
      const blocked = !opened || busy || stale() || !available.length;
      previewButton.disabled = blocked || Boolean(staged.length); buildButton.disabled = blocked || Boolean(staged.length) || Boolean(plan && plan.status!=="ready"); allButton.disabled = blocked || Boolean(staged.length) || available.length < 2;
      allButton.textContent = `Собрать все стены области (${available.length})`; selector.disabled = busy || Boolean(staged.length);
      Object.values(controls).forEach(control => {control.disabled = busy || Boolean(staged.length);});
      applyButton.hidden = !staged.length; applyButton.disabled = busy || !staged.length || stale(); stopButton.hidden = !busy;
      exportButton.textContent = savedState === "current" ? "Скачать STEP стены" : "Скачать сохранённый STEP";
      exportButton.disabled = !selectedRecord || busy || stale() || !options.downloadStep; removeButton.disabled = !selectedRecord || busy || stale() || Boolean(staged.length) || !list(collection(current()).items).some(item => item.id === selectedRecord.id);
      solidViewButton.disabled=!selectedRecord?.geometry?.preview;planViewButton.setAttribute("aria-pressed",String(viewMode==="plan"));solidViewButton.setAttribute("aria-pressed",String(viewMode==="3d"));for(const label of viewLabels)label.hidden=viewMode!=="3d";viewTools.hidden=solidDetail.hidden=viewMode!=="3d";
    }
    function render() {
      canvas.replaceChildren(); facts.replaceChildren(); notes.replaceChildren();systemCard.replaceChildren();systemDetails.replaceChildren();
      if(viewMode==="3d"&&!selectedRecord?.geometry?.preview)viewMode="plan";
      const solid=viewMode==="3d";
      shell.setAttribute("data-view",viewMode);inspection.hidden=!solid;
      if(solid){inspection.append(facts,notes);drawing.append(inspection);}
      else drawing.append(facts,notes,inspection);
      const target=selected(),assigned=target?W.storedEntry(current(),target.wall_id,target.face_id)?.configuration:null;
      const systemInfo=solid?systemDetails:systemCard;
      systemInfo.append(make("strong","","Назначено стене"),make("p","",mountingDescription(assigned)),make("small","wm-help","Сборка использует назначенный монтаж и не меняет его на другую систему. Тип панелей и монтажа изменяется в настройках монтажа стены."));
      if(solid){
        const brief=assigned?.mounting_system?[{open_mount:"Открытый монтаж",concealed_mount:"Скрытый монтаж"}[assigned.mounting_system]||assigned.mounting_system,MATERIALS[assigned.material]||assigned.material,Number.isFinite(assigned.panel_thickness_mm)?`${mm(assigned.panel_thickness_mm)} мм`:null].filter(Boolean).join(" · "):"Монтажная система не задана";
        systemCard.append(make("p","ww-assigned-system",`Назначено: ${brief}`));
      }
      if(selectedRecord?.plan?.mount_configuration&&!W.equal(selectedRecord.plan.mount_configuration,assigned)){
        systemInfo.append(make("p","ww-snapshot-system",`В сохранённом снимке: ${mountingDescription(selectedRecord.plan.mount_configuration)}`));
        if(solid)systemCard.append(make("p","ww-snapshot-system","Сохранённый снимок отличается от назначения. Требуется пересборка."));
      }
      if(capabilities.length){const supported=make("details");supported.append(make("summary","","Поддерживаемые системы"));for(const item of capabilities){const row=make("p","wm-help",`${item.label||item.key}: ${item.status==="supported"?"поддерживается при указанных условиях":"недоступна для полной сборки"}. ${item.conditions||""} ${item.reason||""}`);supported.append(row);}supported.append(make("small","wm-help","Это справочник возможностей, а не смена монтажа. Сборка всегда следует назначению выбранной стены."));systemInfo.append(supported);}
      if (!plan) {clear3d();canvas.append(make("p", "ww-empty", "Здесь появится раскладка всей стены: панели, стойки и точки крепления.")); return;}
      if(previewSource&&previewSource!==selectedRecord?.geometry?.preview)clear3d();
      if(plan.system?.label)systemCard.append(make("p","ww-resolved-system",`В сборке: ${plan.system.label}${list(plan.profile_runs).length?` · ${[...new Set(plan.profile_runs.map(run=>PROFILE_NAMES[run.family]||run.family))].join(" / ")}`:""}`));
      if(plan.system?.scope)systemInfo.append(make("small","wm-help",plan.system.scope));
      if(plan.system?.project_adaptation)systemInfo.append(make("small","wm-help","Профили из библиотеки адаптированы к этой раскладке. Принятые размеры и обработка сохраняются в составе сборки."));
      const wall = plan.wall;
      if(viewMode==="3d"&&selectedRecord?.geometry?.preview){render3d(selectedRecord.geometry.preview);legend.textContent="Тяните — вращение · Shift + перетягивание — сдвиг · колесо — масштаб. Синий — каркас, фиолетовый — узлы и крепёж.";}
      else{
      viewMode="plan";viewer?.setGeometry(null);solidDetail.hidden=true;
      const margin = Math.max(wall.length_mm, wall.height_mm) * .045, size = Math.max(wall.length_mm, wall.height_mm), graph = svg("svg", {viewBox:`${-margin} ${-margin} ${wall.length_mm+2*margin} ${wall.height_mm+2*margin}`,role:"img","aria-label":"Монтажная раскладка стены: панели, стойки, саморезы и анкеры"});
      const rect = (x,z,w,h,attrs) => graph.append(svg("rect", {x,y:wall.height_mm-z-h,width:w,height:h,...attrs}));
      for (const panel of plan.panels) rect(panel.x_mm,panel.y_mm,panel.width_mm,panel.height_mm,{fill:"#f2eadc",stroke:"#9e927f","stroke-width":size/1500});
      for (const post of plan.posts) rect(post.x_mm-post.width_mm/2,post.z_start_mm,post.width_mm,post.z_end_mm-post.z_start_mm,{fill:"#668fc4","fill-opacity":.38,stroke:"#215d9c","stroke-width":size/1800});
      for (const rail of list(plan.rails)) rect(rail.x_start_mm,rail.z_mm-rail.width_mm/2,rail.x_end_mm-rail.x_start_mm,rail.width_mm,{fill:"#668fc4","fill-opacity":.38,stroke:"#215d9c","stroke-width":size/1800});
      for (const opening of plan.openings) rect(opening.x_mm,opening.y_mm,opening.width_mm,opening.height_mm,{fill:"#fff",stroke:"#303944","stroke-width":size/1000});
      for(const run of list(plan.profile_runs)){const vertical=run.axis==="vertical";graph.append(svg("line",{x1:vertical?run.edge_mm:run.start_mm,y1:wall.height_mm-(vertical?run.start_mm:run.edge_mm),x2:vertical?run.edge_mm:run.end_mm,y2:wall.height_mm-(vertical?run.end_mm:run.edge_mm),stroke:"#783dab","stroke-width":size/700,"data-profile-id":run.id}));}
      const radius = size/380;
      for (const point of plan.screws) {const x=point.x_mm,y=wall.height_mm-point.z_mm; graph.append(svg("path", {d:`M${x-radius},${y}H${x+radius}M${x},${y-radius}V${y+radius}`,fill:"none",stroke:"#20252d","stroke-width":size/1600}));}
      for(const point of list(plan.profile_screws)){const x=point.x_mm,y=wall.height_mm-point.z_mm;graph.append(svg("path",{d:`M${x-radius},${y-radius}L${x+radius},${y+radius}M${x-radius},${y+radius}L${x+radius},${y-radius}`,fill:"none",stroke:"#783dab","stroke-width":size/1200,"data-profile-screw-id":point.id}));}
      for (const point of plan.anchors) graph.append(svg("circle",{cx:point.x_mm,cy:wall.height_mm-point.z_mm,r:radius*1.7,fill:"none",stroke:"#a75216","stroke-width":size/1200}));
      rect(0,0,wall.length_mm,wall.height_mm,{fill:"none",stroke:"#303944","stroke-width":size/900}); canvas.append(graph);
      legend.textContent="Синие полосы — каркас · чёрные кресты — его саморезы · оранжевые кольца — анкеры"+(list(plan.profile_runs).length?" · фиолетовые линии и кресты — профили и их крепёж":"");
      }
      const badge = (label,value) => {const item=make("div","ww-fact"); item.append(make("strong","",String(value)),make("span","",label)); facts.append(item);};
      badge("панелей / частей",`${new Set(plan.panels.map((item,index) => item.panel_id || item.id || item.index || index)).size} / ${plan.panels.length}`); badge("стойки / перемычки",`${plan.posts.length} / ${list(plan.rails).length}`); badge("саморезов всего",plan.screws.length+list(plan.profile_screws).length); badge("анкеров",plan.anchors.length);
      if(list(plan.profile_runs).length){badge("отрезков профиля",plan.profile_runs.length);badge("саморезы каркаса / профилей",`${plan.screws.length} / ${list(plan.profile_screws).length}`);}
      const attachment=plan.attachment_summary;
      if(attachment){for(const [name,label]of[["mechanical_panel_count","панелей на профилях"],["adhesive_panel_count","панелей на клею"],["clip_count","клипс"]])if(Number.isInteger(attachment[name])&&attachment[name]>=0)badge(label,attachment[name]);}
      const machining=list(selectedRecord?.geometry?.machining);
      if(machining.length){badge("пазы / выборки",`${machining.filter(item=>item.kind==="edge_groove").length} / ${machining.filter(item=>item.kind==="rear_edge_relief").length}`);}
      notes.append(make("p","wm-help",`${mm(wall.length_mm)} × ${mm(wall.height_mm)} мм. Вид со стороны помещения; начало координат — левый нижний угол.`));
      if (selectedRecord) notes.append(make("p",savedState === "current" ? "ww-verified" : "wm-help",savedState === "current" ? "3D-сборка соответствует текущей стене: панели, каркас и крепёж входят в STEP. Геометрические проверки сохранены вместе с моделью." : "Показан ранее сохранённый STEP-снимок. Соответствие текущей стене пока не подтверждено; при изменениях потребуется пересборка."));
      if(plan.status!=="ready")notes.append(make("p","ww-issue",plan.status==="unsupported"?"Для этой системы или выбранных условий полная 3D-сборка недоступна. Назначенный монтаж сохранён без изменений.":"Расстановка не прошла проверку. Исправьте указанные условия перед сборкой."));
      for (const issue of list(plan.issues)) notes.append(make("p","ww-issue",typeof issue === "string" ? issue : issue.message || issue.code));
      for(const check of list(plan.checks).filter(check=>check.status==="invalid").slice(0,12))notes.append(make("p","ww-issue",checkDescription(check)));
      const details=make("details"); details.append(make("summary","","Принятые размеры и проверки"));
      for (const value of [...list(plan.assumptions),...list(plan.checks)]) details.append(make("p","wm-help",checkDescription(value)));
      notes.append(details);
      const components=list(selectedRecord?.geometry?.bom).filter(component=>Number.isFinite(component.count)&&component.count>0);
      if(components.length){const composition=make("details");composition.append(make("summary","","Состав 3D-сборки"));for(const component of components){const row=make("p","wm-help",`${component.label} — ${component.count} шт.`),source=list(selectedRecord.source_models).find(item=>item.source_id===component.provenance?.source_id),origin=source?.filename||source?.name; if(component.role||origin)row.append(make("small","ww-component-origin",[ROLES[component.role]||component.role,origin].filter(Boolean).join(" · ")));composition.append(row);}notes.append(composition);}
      if(machining.length){const operations=make("details");operations.append(make("summary","","Обработка панелей в STEP"));for(const item of machining){const kind={edge_groove:"Торцевой паз",rear_edge_relief:"Выборка с тыльной стороны"}[item.kind]||item.kind;operations.append(make("p","wm-help",`${item.panel_id}: ${kind}; профиль ${item.profile_id}. Размер X × Y × Z: ${list(item.size_mm).map(mm).join(" × ")} мм; начало ${list(item.origin_mm).map(mm).join(" / ")} мм.`));}operations.append(make("p","wm-help","Координаты сборки: X — вдоль стены, Y — глубина, Z — высота. Показаны реально выполненные операции, а не условные линии."));notes.append(operations);}
      const issueCount=list(plan.issues).length+list(plan.checks).filter(check=>check.status==="invalid").length;
      inspectionSummary.textContent=issueCount?`Система, состав и проверки · замечаний: ${issueCount}`:savedState!=="current"&&selectedRecord?"Система, состав и проверки · актуальность не подтверждена":"Система, состав и проверки";
      inspection.setAttribute("data-warning",String(Boolean(issueCount||savedState!=="current"&&selectedRecord)));
    }
    function visibleGroups(){return {panels:!hidePanels.checked,frame:!hideFrame.checked,nodes:!hideNodes.checked,base:!hideBase.checked};}
    function setGroups(value){hidePanels.checked=!value.panels;hideFrame.checked=!value.frame;hideNodes.checked=!value.nodes;hideBase.checked=!value.base;viewer?.setVisible(visibleGroups());}
    function clear3d(){viewer?.setGeometry(null);geometryStore?.clear();geometryStore=null;previewSource=null;solidDetail.textContent="";}
    function render3d(preview) {
      try{
        if(!R?.createViewer||!R?.createGeometryStore)throw Error("Модуль 3D-просмотра не загрузился. Обновите страницу; STEP доступен для скачивания.");
        canvas.append(solidViewport);
        if(!viewer)viewer=(options.createViewer||R.createViewer)(solidCanvas,solidEmpty,{onDetail:detail=>{solidDetail.textContent=detail.message;}});
        const changed=previewSource!==preview;
        if(changed){clear3d();const next=(options.createGeometryStore||R.createGeometryStore)();try{next.appendChunk({id:"wall",wire_preview:preview});}catch(error){next.clear();throw error;}geometryStore=next;previewSource=preview;}
        solidDetail.textContent=(preview.note?preview.note+" ":"")+(geometryStore.bounded_count?`${geometryStore.bounded_count} деталей показаны габаритами; STEP содержит точную геометрию.`:"Контуры исходных деталей; STEP содержит точную геометрию.");
        viewer.setGeometry(geometryStore,{fit:changed});viewer.setVisible(visibleGroups());viewer.resize();
      }catch(error){clear3d();canvas.replaceChildren(make("p","ww-empty",error.message));}
    }
    async function preview() {
      if (!opened || busy || stale() || staged.length || !selected()) return false;
      const target=copy(selected()), token=++sequence; busy=true; plan=null; selectedRecord=null; confirmation.hidden=true; update(); status.textContent="Распределяем стойки и крепёж по всей стене…";
      try {const result=await options.request("/whole-wall/plan",{method:"POST",body:{project:requestProject(current()),wall_id:target.wall_id,face_id:target.face_id,configuration:values()}}); if (!alive || !opened || sequence!==token || stale()) return false; plan=checkedPlan(result,target);if(Array.isArray(result.capabilities))capabilities=copy(result.capabilities); status.textContent=plan.status === "ready" ? "Раскладка готова. Теперь можно построить и проверить 3D-сборку." : "Раскладка требует уточнения. Причины указаны под чертежом."; render(); grid.scrollTop = 0; return true;}
      catch(error) {if (alive && opened && sequence===token) status.textContent=error.message; return false;}
      finally {if (alive && opened && sequence===token) {busy=false; update();}}
    }
    async function build(all=false) {
      if (!opened || busy || stale() || staged.length || !selected() || !all && plan && plan.status!=="ready") return false;
      const targetList=(all ? available : [selected()]).map(copy), config=values(), source=copy(current()), token=++sequence, errors=[]; busy=true; staged=[]; confirmation.hidden=true; update();
      for (const [index,target] of targetList.entries()) {
        if (!alive || !opened || sequence!==token || stale()) return false;
        status.textContent=`Собираем стену ${index+1} из ${targetList.length}: ${target.label}. Проверяем соединения и пересечения…`;
        try {const targetConfig=copy(config),legacy=collection(source).items.find(record=>key(record)===key(target)),savedThickness=savedSettings(source).get(key(target))?.configuration?.substrate_thickness_mm ?? legacy?.configuration?.substrate_thickness_mm,thickness=savedThickness ?? W.wallContext(source,target.wall_id)?.wall?.thickness_mm;if(all&&!thicknessEdited&&Number.isFinite(thickness)&&thickness>0)targetConfig.substrate_thickness_mm=thickness;
          const data=await options.request("/whole-wall/build",{method:"POST",body:{project:requestProject(source),wall_id:target.wall_id,face_id:target.face_id,configuration:targetConfig}}); if (!alive || !opened || sequence!==token || stale()) return false;
          const record=checkedRecord(data.assembly,target), merged=mergeRecords(source,[record]); source.properties ||= {}; source.properties.whole_wall_assemblies=merged; staged.push(copy(record)); selectedRecord=record; savedState="current"; plan=record.plan; selector.value=key(target); render(); grid.scrollTop = 0;
        } catch(error) {if(!alive||!opened||sequence!==token||stale())return false;errors.push(`${target.label}: ${error.message}`);}
      }
      if(!alive||!opened||sequence!==token||stale())return false;
      busy=false; status.textContent=`Готово сборок: ${staged.length} из ${targetList.length}. ${staged.length ? "Нажмите «Добавить сборки в проект», чтобы сохранить их в черновике одним действием." : "Проверьте причины ниже."}`;
      for (const error of errors) notes.append(make("p","ww-issue",error)); update(); return staged.length>0;
    }
    function apply() {
      if (!opened || busy || !staged.length || stale()) return false;
      try {const expected=initial, next=mergeRecords(current(),staged,expected), settings=mergeSettings(current(),staged), count=staged.length; applying=true;
        if (options.mutate("добавление полных 3D-сборок стен",project=>{if (W.stableKey(project)!==expected) throw Error("Проект изменился. Повторите расчёт."); project.properties ||= {}; project.properties.whole_wall_assemblies=copy(next); project.properties.whole_wall_settings=copy(settings);})===false) return false;
        staged=[]; initial=W.stableKey(current()); status.textContent=`Добавлено сборок стен: ${count}. Сохраните проект. Действие можно отменить.`; options.toast?.("3D-сборки стен добавлены в черновик","success"); update(); return true;
      } catch(error) {status.textContent=error.message; return false;} finally {applying=false;}
    }
    async function download() {if (!selectedRecord || busy || stale()) return; try {await options.downloadStep?.(copy(selectedRecord));} catch(error) {status.textContent=error.message;}}
    function armRemove() {
      if (!selectedRecord || busy || staged.length || stale()) return false;
      const id=selectedRecord.id, expected=initial; confirmation.replaceChildren(make("p","","Удалить сохранённую 3D-сборку этой стороны стены? Панели, монтажные настройки, стены и проёмы останутся. Действие можно отменить."),button("Да, удалить сборку",()=>{
        try {if (busy || staged.length || stale()) throw Error("Проект изменился. Подтвердите удаление заново."); applying=true;
          if (options.mutate("удаление полной 3D-сборки стены",project=>{if (W.stableKey(project)!==expected) throw Error("Проект изменился."); const records=collection(project).items; project.properties.whole_wall_settings=mergeSettings(project,records.filter(record=>record.id===id),{preferExisting:true}); project.properties.whole_wall_assemblies={schema_version:SCHEMA,items:records.filter(record=>record.id!==id).map(copy)};})===false) return false;
          initial=W.stableKey(current()); confirmation.hidden=true; selectedRecord=null; plan=null; render(); update(); status.textContent="Сборка удалена. Монтаж стен и исходные STEP сохранены."; return true;
        } catch(error) {status.textContent=error.message; return false;} finally {applying=false;}
      }),button("Оставить сборку",()=>{confirmation.hidden=true;})); confirmation.hidden=false; return true;
    }
    function stop() {sequence++; busy=false; staged=[]; selectedRecord=null; plan=null; status.textContent="Очередь остановлена. Результаты не добавлены в проект."; render(); update();}
    function refresh() {if (opened && !applying && stale()) {sequence++; busy=false; staged=[]; selectedRecord=null; plan=null; confirmation.hidden=true; status.textContent="Проект изменился. Закройте и откройте сборку стен заново, чтобы не потерять новые правки."; render(); update();}}
    function hasDraft() {return opened && (busy || staged.length>0);}
    function open(selectedScope=null) {
      if (!alive || hasDraft()) return false;
      try {collection(current()); available=targets(current(),selectedScope); scope=copy(selectedScope); initial=W.stableKey(current()); opened=true; sequence++; plan=null; staged=[]; busy=false;thicknessEdited=false;viewMode="plan"; confirmation.hidden=true; selector.replaceChildren(); for (const target of available) {const option=make("option","",target.label); option.value=key(target); selector.append(option);} selector.value=available[0] ? key(available[0]) : ""; fill(defaults); loadSaved(); render(); update(); verifySaved(); return true;}
      catch(error) {status.textContent=error.message; options.toast?.(error.message,"error"); return false;}
    }
    function close(settings={}) {if (hasDraft() && !settings.force) {confirmation.replaceChildren(make("p","","Закрыть без добавления построенных сборок в проект?"),button("Закрыть без добавления",()=>close({force:true})),button("Продолжить работу",()=>{confirmation.hidden=true;})); confirmation.hidden=false; return false;} opened=false; sequence++; busy=false; staged=[]; confirmation.hidden=true;clear3d();options.onClose?.(); return true;}
    return {open,close,preview,build,apply,refresh,hasDraft,dispose(){alive=false;opened=false;sequence++;clear3d();viewer?.dispose();viewer=null;}};
  }
  return {defaults,configuration,mountingDescription,checkDescription,collection,targets,checkedPlan,checkedRecord,mergeRecords,wireGeometry,projectWirePoint,create};
});
