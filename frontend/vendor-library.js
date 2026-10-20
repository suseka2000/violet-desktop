/* Supplier evidence is a reference, never an implicit engineering approval. */
(function (root, factory) {
  const common = typeof module === "object" && module.exports;
  const api = factory(common ? require("./project-history.js") : root.VioletProjectHistory,
    common ? () => require("./wall-mount.js") : () => root.VioletWallMount);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.VioletVendorLibrary = api;
})(typeof window !== "undefined" ? window : null, function (H, wallMount) {
  "use strict";
  const modes = {open_mount: "Открытый монтаж", concealed_mount: "Скрытый монтаж", general: "Общие сведения"};
  const kinds = {model: "Модели деталей", manual: "Методички и иллюстрации", reference_drawing: "Образцы РД"};
  const copy = value => JSON.parse(JSON.stringify(value));
  const fingerprint = project => H.capture(project);
  const matches = (project, expected) => H.equal(fingerprint(project), expected);
  const sha = value => /^[a-f0-9]{64}$/.test(value || "");
  function assemblyRules(rules, system) {
    return (rules || []).filter(rule => rule.kind === "detail" && rule.assembly_id && ["mounting_assembly", "clip_installation"].includes(rule.topic) && (!system || rule.system === system));
  }
  function zones(project) {
    return (project?.buildings || []).flatMap(b => (b.floors || []).flatMap(f => (f.cladding_zones || []).map(z => ({...z, label: `${b.name} / ${f.name} / ${z.name}`}))));
  }
  function filteredAssets(assets, query, kind) {
    const q = String(query || "").normalize("NFC").toLocaleLowerCase("ru");
    return (assets || []).filter(a => (!kind || a.kind === kind) && `${a.filename} ${a.category_label || ""} ${a.category}`.normalize("NFC").toLocaleLowerCase("ru").includes(q));
  }
  function setBinding(project, value) {
    const design = project.properties?.vendor_design;
    if (!design?.snapshot) throw new Error("Сначала зафиксируйте библиотеку в проекте.");
    if (!zones(project).some(z => z.id === value.zone_id)) throw new Error("Зона облицовки больше не существует.");
    const rule = assemblyRules(design.snapshot.rules, value.system).find(r => r.id === value.assembly_rule_id);
    if (!rule || rule.source_status !== "verified") throw new Error("У сборки нет проверяемой страницы источника.");
    const item = {zone_id: value.zone_id, system: value.system, assembly_rule_id: rule.id, note: String(value.note || "").trim()};
    if (!sha(value.zone_signature)) throw new Error("Нужно заново проверить геометрию зоны перед назначением.");
    item.zone_signature = value.zone_signature;
    if (item.note.length > 2000) throw new Error("Примечание должно быть не длиннее 2000 символов.");
    design.zone_bindings = (design.zone_bindings || []).filter(b => b.zone_id !== item.zone_id);
    design.zone_bindings.push(item);
  }

  function create({host, request, getProject, mutate, toast, canMutate}) {
    const ui = {inventory: null, loading: false, selectedAsset: null, selectedRule: null, page: 1, mode: "assets", projectId: null, review: null, reviewLoading: false, reviewError: null, pendingAttach: null, attachLoading: false, attachMessage: null};
    const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
    const button = (text, fn, primary=false) => {const b=el("button",text,`button ${primary ? "button-primary" : "button-secondary"}`);b.type="button";b.addEventListener("click",fn);return b;};
    const field = (label, input) => {const l=el("label",undefined,"field");l.append(el("span",label),input);return l;};
    const select = (id, choices) => {const s=el("select");s.id=id;for(const [value,label] of choices){const o=el("option",label);o.value=value;s.append(o);}return s;};
    const fail = error => {status.textContent=error.message || String(error);toast(status.textContent,"error",8000);};
    const link = (label, asset) => {const a=el("a",label,"button button-secondary");a.href=`/api/v1/vendor-library/${asset.id || asset.sha256}/original`;a.download=asset.filename;return a;};
    const heading=el("div",undefined,"section-heading");
    const title=el("div");title.append(el("p","Материалы инженера","panel-kicker"),el("h2","Библиотека Violet"),el("p","Исходные модели, методички и схемы монтажа для зон облицовки."));
    heading.append(title,button("Обновить файлы",()=>load(true)));
    const status=el("p","Чтение локальной библиотеки…","inline-result");status.id="vendorLibraryStatus";status.setAttribute("role","status");
    const tabs=el("div",undefined,"vendor-modebar");
    const assetsButton=button("Все материалы",()=>{ui.mode="assets";renderList();});
    const detailsButton=button("Схемы монтажа",()=>{ui.mode="details";renderList();});
    tabs.append(assetsButton,detailsButton);
    const search=el("input");search.id="vendorSearch";search.type="search";search.placeholder="Название, профиль или код сборки";
    const kind=select("vendorKind",[["","Все файлы"],...Object.entries(kinds)]);
    const system=select("vendorSystem",[["","Все способы монтажа"],...Object.entries(modes).filter(([v])=>v!=="general")]);
    const filters=el("div",undefined,"vendor-filters");filters.append(field("Поиск",search),field("Тип материала",kind),field("Монтаж",system));
    const list=el("div",undefined,"vendor-list");list.id="vendorAssetList";
    const viewer=el("section",undefined,"panel work-card vendor-viewer");viewer.id="vendorAssetViewer";
    const split=el("div",undefined,"vendor-grid");const rail=el("section",undefined,"panel work-card vendor-rail");rail.append(tabs,filters,list);split.append(rail,viewer);
    const projectCard=el("section",undefined,"panel work-card vendor-project");projectCard.id="vendorProjectCard";
    host.replaceChildren(heading,status,split,projectCard);
    search.addEventListener("input",renderList);kind.addEventListener("change",renderList);system.addEventListener("change",renderList);

    function facts() {return ui.inventory?.manual_facts || {documents:[],rules:[]};}
    function renderList() {
      assetsButton.setAttribute("aria-pressed",String(ui.mode==="assets"));detailsButton.setAttribute("aria-pressed",String(ui.mode==="details"));
      kind.closest("label").hidden=ui.mode!=="assets";system.closest("label").hidden=ui.mode!=="details";
      list.replaceChildren();
      if(!ui.inventory){list.append(el("p","Библиотека ещё не загружена."));return;}
      let entries;
      if(ui.mode==="assets") entries=filteredAssets(ui.inventory.assets,search.value,kind.value);
      else {const q=search.value.normalize("NFC").toLowerCase();entries=assemblyRules(facts().rules,system.value).filter(r=>`${r.title} ${r.assembly_id} ${(r.component_codes||[]).join(" ")}`.normalize("NFC").toLowerCase().includes(q));}
      list.append(el("p",`Найдено: ${entries.length}`,"form-hint"));
      for(const item of entries){const b=button(ui.mode==="assets"?item.filename:`${item.assembly_id} · ${item.title}`,()=>ui.mode==="assets"?showAsset(item,1):showRule(item));b.className="vendor-list-item";
        b.append(el("span",ui.mode==="assets"?`${String(item.format).toUpperCase()} · ${item.category_label || kinds[item.kind]} · ${(item.byte_size/1048576).toFixed(2)} МиБ`: `${modes[item.system]} · стр. ${item.source.page}`));list.append(b);}
      if(!entries.length) list.append(el("p","Нет совпадений. Измените фильтр или обновите библиотеку.","empty-note"));
    }
    function showAsset(asset, page=1) {
      ui.selectedAsset=asset;ui.selectedRule=null;ui.page=page;
      viewer.replaceChildren(el("h3",asset.filename));
      const version=typeof asset.format_version==="object" ? asset.format_version?.description : asset.format_version;
      const info=el("p",`${kinds[asset.kind] || asset.kind} · ${version || String(asset.format).toUpperCase()}`,"form-hint");viewer.append(info);
      const notes=el("details",undefined,"vendor-export-note"); notes.append(el("summary","Примечания к исходнику"), el("p",asset.kind==="model"?"Категория определена по имени файла. Исполнение детали и материал уточняются по модели и спецификации.":"Обозначения и размеры приведены на исходной странице.","form-hint"));
      if(sha(asset.id)) viewer.append(link("Скачать оригинал",asset));
      for(const warning of asset.warnings||[]) notes.append(el("p",warning,"form-hint")); viewer.append(notes);
      if(asset.reference_drawing?.status==="experimental_reference") {
        const ref=asset.reference_drawing, summary=ref.summary || {}, diagnostics=ref.diagnostics || {};
        const box=el("details",undefined,"vendor-export-note");box.append(el("summary","Примечания к чтению образца РД"));
        box.append(el("p","DWG прочитан экспериментальным конвертером. Геометрию и состав листов нужно сверить с оригинальным PDF.","form-hint"));
        box.append(el("p",`Предупреждений конвертера: ${diagnostics.warning_count ?? "неизвестно"}; строк ошибок: ${diagnostics.error_line_count ?? "неизвестно"}. Диагностика чтения сохранена.`));
        const layouts=(summary.layouts || []).slice(0,20).map(item=>item.name).join(", ");
        box.append(el("p",`Пространств LAYOUT: ${summary.layout_count ?? "неизвестно"}${layouts?` (${layouts})`:""}. Это не количество печатных листов.`));
        box.append(el("p",`Слоёв: ${summary.layer_count ?? "неизвестно"}; определений блоков: ${summary.block_definition_count ?? "неизвестно"}; объектов в модели: ${summary.modelspace_entity_count ?? "неизвестно"}.`,"form-hint"));
        const topics=(summary.topics || []).slice(0,10);if(topics.length){box.append(el("h4","Найденные темы"));for(const topic of topics)box.append(el("p",`${topic.label} · совпадений в тексте: ${topic.text_match_count}`));}
        const titles=(summary.printed_sheet_titles || []).slice(0,10);if(titles.length){box.append(el("h4","Надписи, похожие на названия листов"));for(const item of titles)box.append(el("p",item.text));box.append(el("p","Это извлечённые надписи, не проверенный состав документации.","form-hint"));}
        for(const text of (ref.limitations || []).slice(0,10))box.append(el("p",text,"form-hint"));viewer.append(box);
      }
      if(asset.page_count && asset.preview_url && sha(asset.id)) {
        const pageSelect=select("vendorPageSelect",Array.from({length:asset.page_count},(_,i)=>[String(i+1),`Страница ${i+1} из ${asset.page_count}`]));pageSelect.value=String(page);
        pageSelect.addEventListener("change",()=>showAsset(asset,Number(pageSelect.value)));viewer.append(field("Страница документа",pageSelect));
        const image=el("img");image.className="vendor-page-preview";image.alt=`${asset.filename}, страница ${page}`;image.src=`/api/v1/vendor-library/${asset.id}/preview.png?page=${page}`;
        image.addEventListener("error",()=>{if(image.isConnected)image.replaceWith(el("p","Страница недоступна: исходник мог измениться. Обновите библиотеку; старую ссылку не подменяем другим файлом.","vendor-caution"));});viewer.append(image);
      } else {
        const box=el("div",undefined,"vendor-export-note");box.append(el("h4","Что нужно для дальнейшей работы"));
        for(const note of asset.export_recommendations||[]) box.append(el("p",note));
        if(!(asset.export_recommendations||[]).length)box.append(el("p","Для этого формата пока доступна только регистрация оригинала. Геометрия не импортирована."));viewer.append(box);
      }
      const doc=facts().documents.find(d=>d.sha256===asset.sha256);
      const related=doc?facts().rules.filter(r=>r.source.document_id===doc.id && r.source.page===page):[];
      if(related.length){const section=el("div",undefined,"vendor-fact-list");section.append(el("h4","Выписки с этой страницы"));for(const r of related){section.append(el("strong",r.title),el("p",r.requirement),el("p",r.scope,"form-hint"));}viewer.append(section);}
    }
    function showRule(rule, snapshot=null) {
      const data=snapshot || facts();const doc=data.documents.find(d=>d.id===rule.source.document_id);
      const asset=ui.inventory?.assets.find(a=>a.sha256===doc?.sha256);
      if(asset) showAsset(asset,rule.source.page);else viewer.replaceChildren(el("p","Оригинал этой зафиксированной версии сейчас недоступен.","vendor-caution"));
      ui.selectedRule=rule;
      const intro=el("div",undefined,"vendor-detail-intro"),scopeNotes=el("details",undefined,"vendor-export-note");scopeNotes.append(el("summary","Примечания к применению"),el("p",rule.scope));intro.append(el("h3",`${rule.assembly_id} · ${rule.title}`),el("p",modes[rule.system]),el("p",rule.requirement),scopeNotes,el("p",`Компоненты: ${(rule.component_codes||[]).join(", ") || "не перечислены"}`));
      intro.append(el("p","Обозначения сохраняются как в данном документе. Совпадение названия в другом каталоге не подтверждает тождество профилей.","form-hint"));viewer.prepend(intro);
    }
    async function attach() {
      if(!canMutate() || ui.attachLoading)return;
      const project=getProject();if(!project)return;
      const expected={id:project.id,before:fingerprint(project)};ui.attachMessage=null;
      if(project.properties?.vendor_design){ui.pendingAttach=expected;refreshProject();return;}
      await performAttach(expected);
    }
    async function confirmAttach(expected) {
      if(ui.pendingAttach!==expected || ui.attachLoading)return;
      ui.pendingAttach=null;
      if(getProject()?.id!==expected.id || !matches(getProject(),expected.before)){ui.attachMessage="Проект изменился. Подтверждение отменено; начните обновление библиотеки заново.";refreshProject();return;}
      if(!canMutate()){refreshProject();return;}
      await performAttach(expected);
    }
    async function performAttach(expected) {
      if(getProject()?.id!==expected.id || !matches(getProject(),expected.before))return;
      ui.attachLoading=true;refreshProject();
      try {const snapshot=await request("/vendor-library/snapshot");if(getProject()?.id!==expected.id)throw new Error("Проект сменился. Повторите прикрепление для нужного проекта.");
        if(!matches(getProject(),expected.before))throw new Error("Проект изменился во время загрузки библиотеки. Повторите прикрепление, чтобы не потерять новые правки.");
        if(!canMutate())return;
        const changed=mutate("фиксация библиотеки материалов",p=>{p.properties.vendor_design={schema_version:"vendor-design/1.0",snapshot:copy(snapshot),zone_bindings:[]};},{rethrow:true});
        if(changed){refreshProject();toast("Версия библиотеки добавлена в черновик. Сохраните проект.","success");}
      }catch(error){ui.attachMessage=error.message || String(error);fail(error);}
      finally {ui.attachLoading=false;refreshProject();}
    }
    async function reviewSolutions() {
      const project=getProject();if(!project || ui.reviewLoading)return;
      const before=fingerprint(project);ui.reviewLoading=true;ui.reviewError=null;refreshProject();
      try {
        const report=await request("/vendor-design/review",{method:"POST",body:{project:wallMount().requestProject(project)}});
        if(!matches(getProject(),before))throw new Error("Проект изменился во время сверки. Выполните проверку ещё раз для текущей версии.");
        ui.review={project:before,report};
      }catch(error){ui.reviewError=error.message || String(error);fail(error);}
      finally {ui.reviewLoading=false;refreshProject();}
    }
    function reviewCard(project) {
      const section=el("section",undefined,"vendor-fact-list");section.id="vendorReviewResults";
      const check=button(ui.reviewLoading?"Проверяю решения…":"Проверить решения",reviewSolutions);check.disabled=ui.reviewLoading;section.append(check);
      section.append(el("p","Сверка размеров текущего черновика с закреплёнными методичками. Модель при проверке не изменяется.","form-hint"));
      if(ui.reviewError)section.append(el("p",ui.reviewError,"vendor-caution"));
      if(!ui.review)return section;
      if(!matches(project,ui.review.project)){section.append(el("p","После проверки проект изменился. Старые результаты скрыты — проверьте решения заново.","vendor-caution"));return section;}
      const report=ui.review.report, bindings=report.bindings || [], issues=report.issues || [];
      section.append(el("h4",`Проверено назначений: ${bindings.length}. Замечаний: ${issues.length}.`));
      if(!bindings.length)section.append(el("p","В проекте пока нет назначенных схем монтажа. Выберите сборку для зоны и повторите проверку."));
      const labels={matches_transcribed_value:"Размер совпадает с выпиской",mismatch:"Размер не соответствует выписке",blocked:"Для сверки нужны дополнительные данные",not_specified:"В выбранной сборке нет числового требования"};
      const zoneList=zones(project);
      for(const binding of bindings.slice(0,100)) {
        section.append(el("strong",`${zoneList.find(z=>z.id===binding.zone_id)?.label || binding.zone_id} · сборка ${binding.assembly_id}`));
        for(const item of (binding.checks || []).slice(0,30)) {
          const parameter=item.parameter==="panel_joint_width_mm"?"Межпанельный шов":item.parameter;
          const expected=item.minimum_mm!=null && item.maximum_mm!=null?(item.minimum_mm===item.maximum_mm?`${item.minimum_mm} мм`:`${item.minimum_mm}–${item.maximum_mm} мм`):null;
          const actual=item.actual_mm!=null?`${item.actual_mm} мм`:null;
          section.append(el("p",`${parameter}: ${labels[item.status] || "Не проверено"}.${actual?` В проекте: ${actual}.`:""}${expected?` В методичке: ${expected}.`:""}`,item.status==="mismatch" || item.status==="blocked"?"vendor-caution":"form-hint"));
        }
      }
      if(issues.length){const notes=el("details",undefined,"vendor-export-note");notes.append(el("summary",`Примечания к проверке · ${issues.length}`));for(const issue of issues.slice(0,200))notes.append(el("p",issue.message,"form-hint"));section.append(notes);}
      if(bindings.length>100 || issues.length>200)section.append(el("p","Показаны первые 100 назначений и 200 замечаний. Полный отчёт доступен в документации.","form-hint"));
      return section;
    }
    function refreshProject() {
      const project=getProject();const design=project?.properties?.vendor_design;projectCard.replaceChildren();
      if(ui.pendingAttach && (project?.id!==ui.pendingAttach.id || !matches(project,ui.pendingAttach.before))){
        ui.pendingAttach=null;ui.attachMessage="Проект изменился. Подтверждение отменено; начните обновление библиотеки заново.";
      }
      projectCard.append(el("h3","Решения для зон облицовки"),el("p","Закрепите версию материалов и выберите сборку для каждой зоны. Затем сверяйте её размеры с проектом. Размеры модели не изменяются автоматически."));
      const attachButton=button(ui.attachLoading?"Загружаю снимок библиотеки…":design?"Обновить зафиксированную библиотеку":"Зафиксировать библиотеку в проекте",attach,true);attachButton.disabled=!ui.inventory?.available || ui.attachLoading;projectCard.append(attachButton);
      if(ui.attachMessage)projectCard.append(el("p",ui.attachMessage,"vendor-caution"));
      if(ui.pendingAttach){
        const expected=ui.pendingAttach;const confirmation=el("section",undefined,"vendor-export-note");confirmation.id="vendorAttachConfirmation";confirmation.setAttribute("role","group");confirmation.setAttribute("aria-label","Подтверждение обновления библиотеки");
        confirmation.append(el("p","Обновить закреплённые материалы? Все выбранные сборки для зон будут сброшены только в черновике. Геометрия и сохранённая версия проекта не изменятся до сохранения. Отмена редактора позволит вернуть прежний выбор.","vendor-caution"));
        const actions=el("div",undefined,"vendor-modebar");actions.append(button("Обновить библиотеку и сбросить выбор",()=>confirmAttach(expected),true),button("Не обновлять",()=>{if(ui.pendingAttach===expected){ui.pendingAttach=null;ui.attachMessage=null;refreshProject();}}));confirmation.append(actions);projectCard.append(confirmation);
      }
      if(!design){projectCard.append(el("p","Материалы доступны для просмотра без прикрепления. Выбор сборок появится после фиксации библиотеки.","form-hint"));return;}
      projectCard.append(el("p",`Версия источников: ${design.snapshot.snapshot_id.slice(0,16)}… · Сохранение выполняется общей кнопкой проекта.`,"form-hint"));
      projectCard.append(reviewCard(project));
      const zoneList=zones(project);
      if((design.zone_bindings||[]).some(binding=>!zoneList.some(z=>z.id===binding.zone_id))) {
        projectCard.append(button("Снять выбор для удалённых зон",()=>{if(!canMutate())return;mutate("очистка привязок удалённых зон",p=>{const ids=new Set(zones(p).map(z=>z.id));p.properties.vendor_design.zone_bindings=p.properties.vendor_design.zone_bindings.filter(b=>ids.has(b.zone_id));});}));
      }
      if(!zoneList.length){projectCard.append(el("p","Сначала добавьте панельную систему и зону облицовки через «Добавить в проект». Комната сама по себе не задаёт состав отделки.","vendor-caution"));return;}
      const zone=select("vendorZoneSelect",zoneList.map(z=>[z.id,z.label]));const mounting=select("vendorBindingSystem",Object.entries(modes).filter(([k])=>k!=="general"));const assembly=select("vendorAssemblySelect",[]);const note=el("textarea");note.id="vendorBindingNote";note.maxLength=2000;note.rows=2;
      function populate(restore=true){const binding=design.zone_bindings.find(b=>b.zone_id===zone.value);if(restore){mounting.value=binding?.system || "concealed_mount";note.value=binding?.note || "";}
        assembly.replaceChildren();const blank=el("option","Выберите схему по условиям места");blank.value="";assembly.append(blank);
        for(const rule of assemblyRules(design.snapshot.rules,mounting.value)){const o=el("option",`${rule.assembly_id} · ${rule.title}${rule.source_status!=="verified"?" — источник не подтверждён":""}`);o.value=rule.id;o.disabled=rule.source_status!=="verified";assembly.append(o);}
        assembly.value=restore?(binding?.assembly_rule_id || ""):"";
      }
      zone.addEventListener("change",()=>populate());mounting.addEventListener("change",()=>populate(false));populate();
      const form=el("div",undefined,"vendor-binding-fields");form.append(field("Зона облицовки",zone),field("Способ монтажа",mounting),field("Сборка из зафиксированного альбома",assembly),field("Что проверить / условия применения",note));projectCard.append(form);
      const actions=el("div",undefined,"vendor-modebar");actions.append(button("Посмотреть выбранную схему",()=>{const r=design.snapshot.rules.find(r=>r.id===assembly.value);if(r){showRule(r,design.snapshot);viewer.scrollIntoView({block:"start"});}else toast("Сначала выберите схему.");}),button("Применить к зоне",async()=>{
        if(!canMutate())return;try{if(!assembly.value)throw new Error("Выберите сборку.");const project=getProject(),before=fingerprint(project);const choice={zone_id:zone.value,system:mounting.value,assembly_rule_id:assembly.value,note:note.value};
          const report=await request("/vendor-design/review",{method:"POST",body:{project:wallMount().requestProject(project)}});
          if(!matches(getProject(),before))throw new Error("Модель изменилась во время проверки. Повторите назначение.");if(!canMutate())return;
          choice.zone_signature=report.available_zones?.find(z=>z.zone_id===choice.zone_id)?.zone_signature;
          const changed=mutate("выбор сборки для зоны",p=>setBinding(p,choice),{rethrow:true});if(changed)toast("Решение добавлено в черновик. После сохранения проверки и ссылки появятся в документации.","success");}catch(error){fail(error);}
      },true));projectCard.append(actions);
      const assigned=el("div",undefined,"vendor-assigned");for(const binding of design.zone_bindings){const r=design.snapshot.rules.find(r=>r.id===binding.assembly_rule_id);const row=el("div",undefined,"vendor-assigned-row");row.append(el("span",`${zoneList.find(z=>z.id===binding.zone_id)?.label || "Удалённая зона"}: ${r?.assembly_id || "?"} · требуется проверка`),button("Снять выбор",()=>{if(canMutate())mutate("снятие выбора сборки",p=>{p.properties.vendor_design.zone_bindings=p.properties.vendor_design.zone_bindings.filter(b=>b.zone_id!==binding.zone_id);});}));assigned.append(row);}projectCard.append(assigned);
    }
    async function load(force=false){
      if(ui.loading)return;if(ui.inventory && !force){refreshProject();return;}ui.loading=true;status.textContent="Читаю локальные материалы и проверяю ссылки на первоисточники…";
      try{const inventory=await request("/vendor-library");ui.inventory=inventory;const count=inventory.assets?.length||0;const details=assemblyRules(facts().rules).length;
        status.textContent=`${count} файлов · ${details} схем сборок. ${inventory.available?"Оригиналы не изменяются и никуда не отправляются.":"Папка материалов недоступна."} ${(inventory.warnings||[]).join(" ")} ${(facts().warnings||[]).join(" ")}`;
        renderList();refreshProject();
        const first=inventory.assets?.find(a=>a.filename==="closed.pdf")||inventory.assets?.[0];if(first)showAsset(first,1);else viewer.replaceChildren(el("p","Материалы пока не найдены."));
      }catch(error){fail(error);}finally{ui.loading=false;}
    }
    return {load,refreshProject};
  }
  return {create,assemblyRules,zones,filteredAssets,setBinding};
});
