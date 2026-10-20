(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletCadWorkspace = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const SVG = "http://www.w3.org/2000/svg";
  const array = value => Array.isArray(value) ? value : [];
  const clone = value => JSON.parse(JSON.stringify(value));
  const mm = value => Number(value).toLocaleString("ru-RU", {maximumFractionDigits: 3});
  function regionFromPoints(a, b) {
    const result = [Math.min(a.x,b.x),Math.min(a.y,b.y),Math.max(a.x,b.x),Math.max(a.y,b.y)];
    return result.every(Number.isFinite) && result[2] > result[0] && result[3] > result[1] ? result : null;
  }
  function rasterBox(bounds) {
    const b = bounds || {};
    if (![b.min_x,b.min_y,b.max_x,b.max_y].every(Number.isFinite) || b.max_x <= b.min_x || b.max_y <= b.min_y) throw new Error("Нет корректных границ предпросмотра.");
    return {x:b.min_x,y:-b.max_y,w:b.max_x-b.min_x,h:b.max_y-b.min_y};
  }
  function projectPoint(point, center, yaw, pitch) {
    const x=point[0]-center[0],y=point[1]-center[1],z=point[2]-center[2];
    const u=x*Math.cos(yaw)-y*Math.sin(yaw),v=x*Math.sin(yaw)+y*Math.cos(yaw);
    return [u,-(v*Math.sin(pitch)+z*Math.cos(pitch))];
  }
  function sectionRequest(axis, offset, ids) {
    const value=Number(offset);
    if (!["x","y","z"].includes(axis) || !["number","string"].includes(typeof offset) || !String(offset).trim() || !Number.isFinite(value) || Math.abs(value)>1e9) throw new Error("Укажите ось и положение сечения в пределах ±1 000 000 000 мм.");
    if (!Array.isArray(ids) || !ids.length || ids.length>256) throw new Error("Выберите от 1 до 256 тел для сечения.");
    if(!ids.every(id=>Number.isInteger(id)&&id>0&&id<=256))throw new Error("Некорректный идентификатор тела.");
    return {axis,offset_mm:value,component_ids:[...new Set(ids)].sort((a,b)=>a-b)};
  }
  function attachmentConflict(attachment) {
    return attachment && (attachment.backend!=="cad_workspace" || attachment.coordinate_space!=="dxf_source_units" || attachment.requires_calibration!==true)
      ? "У файла есть разметка старого импорта с другой системой координат. Откройте её через «Уже приложен к этажу»; для новой разметки выберите другой этаж. Старые отметки не изменены." : null;
  }
  function sectionSnapshotMatches(snapshot, sourceId, request, allIds=[]) {
    if (!snapshot || snapshot.schema_version!=="cad-section-snapshot/1.0" || snapshot.source_id!==sourceId || !snapshot.result || !snapshot.request) return false;
    const normalize = ids => [...new Set(ids == null ? allIds : array(ids))].sort((a,b)=>a-b);
    return snapshot.request.axis===request.axis && snapshot.request.offset_mm===request.offset_mm && JSON.stringify(normalize(snapshot.request.component_ids))===JSON.stringify(normalize(request.component_ids));
  }
  function filterModels(assets, query="", mode="all", attachedIds=[]) {
    const terms=String(query).normalize("NFKC").toLocaleLowerCase("ru-RU").trim().split(/\s+/).filter(Boolean),attached=new Set(attachedIds);
    return array(assets).filter(asset=>{
      if(mode==="attached"&&!attached.has(asset.source_id || asset.id))return false;
      if(mode==="unattached"&&attached.has(asset.source_id || asset.id))return false;
      if(mode==="duplicates"&&!(asset.duplicate_count>0 || array(asset.aliases).length>1))return false;
      const haystack=[asset.filename,asset.relative_path,...array(asset.aliases).flatMap(a=>[a.filename,a.relative_path])].join(" ").normalize("NFKC").toLocaleLowerCase("ru-RU");
      return terms.every(term=>haystack.includes(term));
    }).sort((a,b)=>String(a.filename).localeCompare(String(b.filename),"ru",{numeric:true}));
  }
  function elements() {
    const make=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
    const svg=(tag,attrs={})=>{const n=document.createElementNS(SVG,tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,String(v)));return n;};
    const button=(label,fn,cls="button button-secondary")=>{const b=make("button",cls,label);b.type="button";b.addEventListener("click",fn);return b;};
    const field=(label,input)=>{const n=make("label","field");n.append(make("span","",label),input);input.setAttribute("aria-label",label);return n;};
    return {make,svg,button,field};
  }
  function mountLibrary(host, options) {
    const {make,button,field}=elements();
    let alive=true,sequence=0,assets=[],opening=false;
    const shell=make("div","model-library"),bar=make("div","model-library-bar"),search=make("input"),filter=make("select"),status=make("p","form-hint"),list=make("div","model-library-list");
    search.type="search";search.placeholder="Код, название детали или папка…";
    [["all","Вся библиотека"],["attached","В этом проекте"],["unattached","Не приложены"],["duplicates","Есть одинаковые копии"]].forEach(([value,label])=>{const o=make("option","",label);o.value=value;filter.append(o);});
    status.setAttribute("role","status");list.setAttribute("aria-label","Модели поставщика");
    const reload=button("Обновить папку",load,"mini-button");bar.append(field("Поиск модели",search),field("Показать",filter),reload);
    shell.append(bar,status,list,make("p","form-hint","Открытие не добавляет модель в проект. Приложите только нужные STEP или сохраните сечения; оригиналы не изменяются. Номера в названиях — имена файлов, не подтверждённые исполнения."));host.replaceChildren(shell);
    function render() {
      const attached=array(options.getAttachedIds?.()),rows=filterModels(assets,search.value,filter.value,attached);list.replaceChildren();
      if(!rows.length)list.append(make("p","model-library-empty",assets.length?"По этим условиям моделей нет. Измените поиск или фильтр.":"В папке пока нет доступных STEP-моделей."));
      rows.forEach(asset=>{
        const row=make("article","model-library-item"),text=make("div","model-library-item-text"),id=asset.source_id || asset.id;
        text.append(make("strong","",asset.filename),make("span","model-library-path",asset.relative_path));
        const copies=Number(asset.duplicate_count)||Math.max(0,array(asset.aliases).length-1);
        text.append(make("span","form-hint",`${mm(asset.byte_size/1024)} КБ${attached.includes(id)?" · приложена к проекту":""}${copies?` · одинаковых копий: ${copies}`:""}`));
        if(asset.inspection)text.append(make("span",asset.geometry_status==="invalid_geometry"?"warning-text":"form-hint",`${asset.geometry_status==="invalid_geometry"?"Есть ошибки геометрии":"Геометрия проверена"} · ${asset.inspection.component_count} тел${asset.inspection.bounds_mm?.size?` · ${asset.inspection.bounds_mm.size.map(mm).join(" × ")} мм`:""}`));
        if(asset.signature_status==="mismatch")text.append(make("span","warning-text","Содержимое не похоже на STEP — проверьте экспорт."));
        if(array(asset.aliases).length>1){const details=make("details");details.append(make("summary","","Где находятся копии"));asset.aliases.forEach(a=>details.append(make("p","model-library-path",a.relative_path)));text.append(details);}
        const open=button("Открыть",async()=>{
          if(opening)return;opening=true;render();status.textContent=`Открываем ${asset.filename}… Геометрия проверяется локально.`;
          try {const accepted=await options.open(asset);if(alive)status.textContent=accepted===false?"Открытие отменено. Текущий исходник сохранён.":`Открыта модель ${asset.filename}.`;}catch(error){if(alive)status.textContent=error.message;}finally{opening=false;if(alive)render();}
        },"mini-button");open.disabled=opening || asset.signature_status==="mismatch";open.setAttribute("aria-label",`Открыть ${asset.filename}`);row.append(text,open);list.append(row);
      });
    }
    async function load() {
      const token=++sequence;reload.disabled=true;status.textContent="Проверяем папку моделей…";
      try {const result=await options.request("/model-library");if(!alive||token!==sequence)return;assets=array(result.assets);render();status.textContent=`${result.summary?.unique_models ?? assets.length} уникальных моделей · ${result.summary?.files ?? assets.length} файлов. ${array(result.warnings).join(" ")}`;}
      catch(error){if(alive&&token===sequence)status.textContent=`Не удалось прочитать библиотеку: ${error.message}`;}
      finally{if(alive&&token===sequence)reload.disabled=false;}
    }
    search.addEventListener("input",render);filter.addEventListener("change",render);load();return {refreshProject:render,reload:load,dispose(){alive=false;sequence++;}};
  }
  function mountStep(host, inspection, options) {
    const {make,svg,button,field}=elements(),components=array(inspection.components),allIds=components.map(c=>c.id),selected=new Set(allIds);
    let alive=true,sequence=0,pending=false,pendingSequence=0,yaw=-Math.PI/4,pitch=Math.PI/6,drag=null,result=null,sectionBox=null,sectionDrag=null,metadataDirty=false,deleteArmed=false,activeSpecId="",baselineRequest=null,baselineSnapshot=null;
    const bounds=inspection.bounds_mm || {min:[0,0,0],max:[1,1,1],size:[1,1,1]},center=bounds.min.map((v,i)=>(v+bounds.max[i])/2),specs=array(options.savedSections).slice();
    const shell=make("div","step-workspace"),header=make("div","step-heading"),grid=make("div","step-grid"),rail=make("div","step-rail"),views=make("div","step-views");
    header.append(make("h3","",components.length===1?"STEP · одна деталь":"STEP · многотельная модель"),make("p","form-hint","Выберите тела и положение плоскости, чтобы построить сечение модели."));
    const summary=make("div","inline-result",`${components.length} тел · габариты ${bounds.size.map(mm).join(" × ")} мм`),warning=make("details","step-warnings");
    warning.append(make("summary","",`Примечания к модели · ${array(inspection.warnings).length}`));array(inspection.warnings).forEach(w=>warning.append(make("p","form-hint",w)));
    const count=make("span","form-hint"),checklist=make("div","step-components"),bodyTools=make("div","step-body-tools");
    function syncChecks(){checklist.querySelectorAll("input").forEach(input=>input.checked=selected.has(Number(input.value)));count.textContent=`Выбрано ${selected.size} из ${components.length}`;}
    function selectBodies(ids){selected.clear();ids.forEach(id=>selected.add(id));syncChecks();invalidate();render3d();}
    bodyTools.append(button("Все",()=>selectBodies(allIds),"mini-button"),button("Снять выбор",()=>selectBodies([]),"mini-button"),count);
    components.forEach(c=>{const label=make("label","step-component"),input=make("input");input.type="checkbox";input.checked=true;input.value=String(c.id);input.addEventListener("change",()=>{input.checked?selected.add(c.id):selected.delete(c.id);syncChecks();invalidate();render3d();});label.append(input,make("span","",`${c.label || `Тело ${c.id}`}${c.valid===false?" · ошибка геометрии":""}`));checklist.append(label);});rail.append(make("h4","","1. Тела для сечения"),bodyTools,checklist);
    const axis=make("select"),offset=make("input"),slider=make("input","step-slider");
    [["x","X · плоскость YZ"],["y","Y · плоскость XZ"],["z","Z · плоскость XY"]].forEach(([value,label])=>{const o=make("option","",label);o.value=value;axis.append(o);});offset.type="number";offset.step="any";slider.type="range";slider.setAttribute("aria-label","Сдвинуть плоскость сечения");
    const status=make("p","inline-result","Выберите тела и плоскость, затем постройте сечение."),sectionNotes=make("details","step-warnings");sectionNotes.hidden=true;status.setAttribute("role","status");const sectionView=svg("svg",{class:"step-section-svg",tabindex:0,role:"img","aria-label":"Предпросмотр сечения, колесо — масштаб, перетягивание — сдвиг"});
    const download=button("Скачать сечение DXF",async()=>{
      if(!result)return;const exported=result;download.disabled=true;
      try{const response=await fetch(`/api/v1/cad-assets/${encodeURIComponent(options.sourceId)}/section.dxf`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(exported.request)});if(!response.ok){const e=await response.json();throw new Error(e.error?.message||"Не удалось экспортировать сечение.");}const blob=await response.blob();if(alive&&result===exported)options.download(blob,`section-${exported.request.axis}-${exported.request.offset_mm}.dxf`);}catch(e){if(alive&&result===exported)status.textContent=e.message;}finally{if(alive)download.disabled=!result?.data.point_count;}
    });download.disabled=true;
    const build=button("Построить сечение",async()=>{
      if(pending)return;let body;try{body=sectionRequest(axis.value,offset.value,[...selected]);}catch(e){status.textContent=e.message;return;}
      const token=++sequence;pendingSequence=token;pending=true;build.disabled=true;download.disabled=true;result=null;sectionView.replaceChildren();sectionNotes.hidden=true;updateSaveButtons();status.textContent="Строим сечение… Сложная резьба может потребовать до 90 секунд. Выбор только нужных тел ускоряет построение.";
      try{const snapshot=await options.request(`/cad-assets/${encodeURIComponent(options.sourceId)}/section-snapshot`,{method:"POST",body});if(!alive||token!==sequence)return;
        if(!sectionSnapshotMatches(snapshot,options.sourceId,body,allIds))throw new Error("Сервер вернул сечение другого исходника или плоскости. Результат не сохранён.");
        result={request:clone(snapshot.request),data:snapshot.result,snapshot:clone(snapshot)};renderSection(result.data);status.textContent=sectionSummary(result.data);download.disabled=!result.data.point_count;updateSaveButtons();
      }catch(e){if(alive&&token===sequence)status.textContent=e.message;}finally{pending=false;if(alive){build.disabled=false;updateSaveButtons();}}
    },"button button-primary");
    function sectionSummary(data){const parts=array(data.components),open=parts.reduce((n,c)=>n+(c.open_wire_count||0),0),closed=parts.reduce((n,c)=>n+(c.closed_wire_count||0),0),notes=[...array(data.warnings),...parts.flatMap(c=>array(c.warnings)),...(open?["Незамкнутые контуры сохранены как есть. Проверьте положение плоскости."]:[])];sectionNotes.replaceChildren(make("summary","",`Примечания к сечению · ${notes.length}`));notes.forEach(text=>sectionNotes.append(make("p","form-hint",text)));sectionNotes.hidden=!notes.length;sectionNotes.open=false;return [`Замкнутых контуров: ${closed}; незамкнутых: ${open}.`,data.point_count?`Приближённые линии, отклонение ${data.deflection_mm} мм.`:"Плоскость не пересекает выбранные тела."].join(" ");}
    function invalidate(){sequence++;result=null;download.disabled=true;build.disabled=pending;sectionBox=null;sectionView.replaceChildren();sectionNotes.hidden=true;deleteArmed=false;status.textContent=pending?"Параметры изменены. Дождитесь завершения расчёта, затем постройте новое сечение.":"Параметры изменены. Постройте сечение заново.";updateSaveButtons();}
    function axisRange(){const i="xyz".indexOf(axis.value);slider.min=String(bounds.min[i]);slider.max=String(bounds.max[i]);slider.step=String(Math.max((bounds.max[i]-bounds.min[i])/1000,.0001));return i;}
    function resetAxis(){const i=axisRange();offset.value=String((bounds.min[i]+bounds.max[i])/2);slider.value=offset.value;invalidate();render3d();}
    axis.addEventListener("change",resetAxis);offset.addEventListener("input",()=>{slider.value=offset.value;invalidate();render3d();});slider.addEventListener("input",()=>{offset.value=slider.value;invalidate();render3d();});
    const presets=make("div","step-plane-presets");[[.25,"¼"],[.5,"По центру"],[.75,"¾"]].forEach(([fraction,label])=>presets.append(button(label,()=>{const i=axisRange();offset.value=String(bounds.min[i]+bounds.size[i]*fraction);slider.value=offset.value;invalidate();render3d();},"mini-button")));
    rail.append(make("h4","","2. Плоскость сечения"),field("Нормаль плоскости",axis),field("Положение сечения, мм",offset),slider,presets,build,download,status,sectionNotes);
    const saved=make("select"),placeholder=make("option","","Новое сечение"),title=make("input"),note=make("textarea"),space=make("select"),zone=make("select");placeholder.value="";title.maxLength=200;title.placeholder="Например, примыкание к полу";note.maxLength=4000;note.rows=3;
    function contextOptions(select,items,empty){const blank=make("option","",empty);blank.value="";select.append(blank);array(items).forEach(item=>{const o=make("option","",item.label);o.value=item.id;select.append(o);});}
    contextOptions(space,options.spaces,"Без привязки к помещению");contextOptions(zone,options.zones,"Без привязки к зоне отделки");[title,note,space,zone].forEach(input=>input.addEventListener("input",()=>{metadataDirty=true;}));
    function addSpecOption(spec){const o=make("option","",`${spec.label || "Без названия"}${spec.geometry_snapshot?" · с геометрией":" · только параметры"}`);o.value=spec.id;saved.append(o);}
    function selectedSpec(){return specs.find(s=>s.id===saved.value);}
    function planeKey(){const raw=String(offset.value),position=raw.trim()&&Number.isFinite(Number(raw))?Number(raw):raw;return JSON.stringify({axis:axis.value,offset:position,ids:[...selected].sort((a,b)=>a-b)});}
    function acceptBaseline(){baselineRequest=planeKey();baselineSnapshot=result?.snapshot?.geometry_sha256 || null;metadataDirty=false;}
    function hasDraft(){return metadataDirty || (pending&&pendingSequence===sequence) || (baselineRequest!==null&&planeKey()!==baselineRequest) || Boolean(result?.snapshot&&result.snapshot.geometry_sha256!==baselineSnapshot);}
    function fillMetadata(spec){
      title.value=spec?.label || "";note.value=spec?.note || "";
      [[space,spec?.target_space_id,"Недоступное помещение"],[zone,spec?.target_zone_id,"Недоступная зона"]].forEach(([select,id,label])=>{if(id&&!Array.from(select.children).some(o=>o.value===id)){const option=make("option","",`${label} · ${id}`);option.value=id;select.append(option);}select.value=id||"";});metadataDirty=false;
    }
    function refreshContexts(contexts){
      [[space,contexts.spaces,"Без привязки к помещению","Недоступное помещение"],[zone,contexts.zones,"Без привязки к зоне отделки","Недоступная зона"]].forEach(([select,items,empty,missing])=>{const id=select.value;select.replaceChildren();contextOptions(select,items,empty);if(id&&!array(items).some(item=>item.id===id)){const option=make("option","",`${missing} · ${id}`);option.value=id;select.append(option);}select.value=id;});
    }
    function loadSpec(spec){
      sequence++;deleteArmed=false;activeSpecId=spec?.id||"";fillMetadata(spec);result=null;download.disabled=true;sectionBox=null;sectionView.replaceChildren();sectionNotes.hidden=true;
      if(!spec){status.textContent="Новое сечение. Выберите плоскость и нажмите «Построить сечение».";acceptBaseline();updateSaveButtons();return;}
      axis.value=spec.axis;axisRange();offset.value=String(spec.offset_mm);slider.value=offset.value;selected.clear();components.filter(c=>!spec.component_ids||spec.component_ids.includes(c.id)).forEach(c=>selected.add(c.id));syncChecks();render3d();
      if(sectionSnapshotMatches(spec.geometry_snapshot,options.sourceId,spec,allIds)){const snapshot=clone(spec.geometry_snapshot);result={request:snapshot.request,data:snapshot.result,snapshot};renderSection(result.data);download.disabled=!result.data.point_count;status.textContent=`Сохранённая геометрия — пересчёт не требуется. ${sectionSummary(result.data)}`;}
      else status.textContent="Сохранены только параметры или снимок устарел. Постройте сечение и обновите его, чтобы включить геометрию в документацию.";acceptBaseline();updateSaveButtons();
    }
    saved.addEventListener("change",()=>{if(hasDraft()){saved.value=activeSpecId;status.textContent="Сначала сохраните правки сечения или нажмите «Сбросить правки сечения».";return;}loadSpec(selectedSpec());});
    function persist(asCopy){try{
      if(!result?.snapshot || !result.data.point_count)throw new Error("Сначала постройте непустое сечение. В документацию сохраняется проверенный снимок геометрии.");
      const body=sectionRequest(axis.value,offset.value,[...selected]);if(!sectionSnapshotMatches(result.snapshot,options.sourceId,body,allIds))throw new Error("Плоскость изменилась. Перестройте сечение.");
      const spec={...clone(result.request),label:title.value.trim() || `Сечение ${body.axis.toUpperCase()} = ${mm(body.offset_mm)} мм`,note:note.value.trim(),target_space_id:space.value||null,target_zone_id:zone.value||null,geometry_snapshot:clone(result.snapshot)};
      const stored=options.saveSection?.(spec,asCopy?null:(saved.value||null));if(!stored)return;
      const index=specs.findIndex(s=>s.id===stored.id);if(index<0)specs.push(stored);else specs[index]=stored;rebuildSaved(stored.id);activeSpecId=stored.id;fillMetadata(stored);acceptBaseline();status.textContent="Сечение с геометрией сохранено в черновике. Нажмите «Сохранить проект», чтобы оно вошло в документацию последней версии.";updateSaveButtons();
    }catch(e){status.textContent=e.message;}}
    const save=button("Сохранить сечение в проект",()=>persist(false),"button button-primary"),copy=button("Сохранить как новое",()=>persist(true)),remove=button("Удалить сечение",()=>{
      const spec=selectedSpec();if(!spec)return;
      if(!deleteArmed){deleteArmed=true;remove.textContent="Подтвердить удаление";status.textContent="Удаляется только это сечение из черновика. Исходный STEP останется; удаление можно отменить общей кнопкой «Отменить».";return;}
      try{if(options.deleteSection?.(spec.id)){const index=specs.findIndex(s=>s.id===spec.id);if(index>=0)specs.splice(index,1);rebuildSaved("");loadSpec(null);status.textContent="Сечение удалено из черновика. Можно отменить правку.";}}catch(e){status.textContent=e.message;}
    });
    function updateSaveButtons(){save.disabled=pending||!result?.snapshot||!result?.data.point_count;copy.disabled=save.disabled||!saved.value;remove.disabled=!saved.value;save.textContent=saved.value?"Обновить выбранное сечение":"Сохранить сечение в проект";if(!deleteArmed)remove.textContent="Удалить сечение";}
    function rebuildSaved(value){saved.replaceChildren(placeholder);specs.forEach(addSpecOption);saved.value=specs.some(s=>s.id===value)?value:"";}
    rebuildSaved("");if(options.saveSection)rail.append(make("h4","","3. Сечение в документации"),field("Сохранённые сечения",saved),field("Название сечения",title),field("Примечание для листа",note),field("Помещение",space),field("Зона отделки",zone),save,copy,button("Сбросить правки сечения",()=>loadSpec(selectedSpec()),"mini-button"),remove,make("p","form-hint","В проекте сохраняются линии сечения, исходник и параметры плоскости."));
    const preview=svg("svg",{class:"step-solid-svg",viewBox:"-100 -100 200 200",tabindex:0,role:"img","aria-label":"Каркас STEP. Потяните для вращения; стрелки также вращают модель."}),projectionBar=make("div","step-projections");
    [["Изометрия",-Math.PI/4,Math.PI/6],["По X",Math.PI/2,0],["По Y",0,0],["По Z",0,Math.PI/2]].forEach(([label,y,p])=>projectionBar.append(button(label,()=>{yaw=y;pitch=p;render3d();},"mini-button")));
    function render3d(){preview.replaceChildren();const paths=components.filter(c=>selected.has(c.id)).flatMap(c=>array(c.preview_paths).map(path=>({id:c.id,points:path.map(p=>projectPoint(p,center,yaw,pitch))})));const span=Math.max(...bounds.size,1)*1.8;preview.setAttribute("viewBox",`${-span/2} ${-span/2} ${span} ${span}`);paths.forEach(path=>preview.append(svg("polyline",{points:path.points.map(p=>p.join(",")).join(" "),fill:"none",stroke:path.id%2?"#52627b":"#7862b2","stroke-width":.8,"vector-effect":"non-scaling-stroke"})));const a="xyz".indexOf(axis.value),uv=[0,1,2].filter(i=>i!==a),pos=Number(offset.value);if(Number.isFinite(pos)){const corners=[[0,0],[1,0],[1,1],[0,1]].map(v=>{const p=[...center];p[a]=pos;p[uv[0]]=v[0]?bounds.max[uv[0]]:bounds.min[uv[0]];p[uv[1]]=v[1]?bounds.max[uv[1]]:bounds.min[uv[1]];return projectPoint(p,center,yaw,pitch);});preview.append(svg("polygon",{points:corners.map(p=>p.join(",")).join(" "),fill:"#9d83f1","fill-opacity":.14,stroke:"#7852d2","stroke-width":1,"vector-effect":"non-scaling-stroke"}));}}
    preview.addEventListener("pointerdown",e=>{if(e.button!==0)return;drag={x:e.clientX,y:e.clientY,yaw,pitch};preview.setPointerCapture(e.pointerId);e.preventDefault();});preview.addEventListener("pointermove",e=>{if(!drag)return;yaw=drag.yaw+(e.clientX-drag.x)*.009;pitch=drag.pitch+(e.clientY-drag.y)*.009;render3d();});["pointerup","pointercancel"].forEach(name=>preview.addEventListener(name,()=>drag=null));preview.addEventListener("keydown",e=>{if(!["ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(e.key))return;e.preventDefault();yaw+=(e.key==="ArrowLeft"?-.15:e.key==="ArrowRight"?.15:0);pitch+=(e.key==="ArrowUp"?-.15:e.key==="ArrowDown"?.15:0);render3d();});
    function fitSection(){const b=result?.data.bounds_mm;if(!b){sectionBox=null;return;}const pad=Math.max(...b.size,1)*.06;sectionBox={x:b.min[0]-pad,y:-b.max[1]-pad,w:Math.max(b.size[0],.01)+2*pad,h:Math.max(b.size[1],.01)+2*pad};updateSectionBox();}
    function updateSectionBox(){if(sectionBox)sectionView.setAttribute("viewBox",`${sectionBox.x} ${sectionBox.y} ${sectionBox.w} ${sectionBox.h}`);}
    function renderSection(data){sectionView.replaceChildren();array(data.components).forEach(c=>array(c.paths).forEach(path=>sectionView.append(svg("polyline",{points:path.map(p=>`${p[0]},${-p[1]}`).join(" "),fill:"none",stroke:c.open_wire_count?"#b06524":"#364c75","stroke-width":1.3,"vector-effect":"non-scaling-stroke"}))));fitSection();}
    sectionView.addEventListener("wheel",e=>{if(!sectionBox)return;e.preventDefault();const matrix=sectionView.getScreenCTM();if(!matrix)return;const f=e.deltaY>0?1.15:1/1.15,p=sectionView.createSVGPoint();p.x=e.clientX;p.y=e.clientY;const q=p.matrixTransform(matrix.inverse());sectionBox={x:q.x-(q.x-sectionBox.x)*f,y:q.y-(q.y-sectionBox.y)*f,w:sectionBox.w*f,h:sectionBox.h*f};updateSectionBox();},{passive:false});
    sectionView.addEventListener("pointerdown",e=>{if(!sectionBox||e.button!==0)return;const m=sectionView.getScreenCTM();if(!m)return;sectionDrag={x:e.clientX,y:e.clientY,box:{...sectionBox},scale:Math.hypot(m.a,m.b)};sectionView.setPointerCapture(e.pointerId);});sectionView.addEventListener("pointermove",e=>{if(!sectionDrag)return;sectionBox={...sectionDrag.box,x:sectionDrag.box.x-(e.clientX-sectionDrag.x)/sectionDrag.scale,y:sectionDrag.box.y-(e.clientY-sectionDrag.y)/sectionDrag.scale};updateSectionBox();});["pointerup","pointercancel"].forEach(name=>sectionView.addEventListener(name,()=>sectionDrag=null));
    const sectionHeading=make("div","step-section-heading");sectionHeading.append(make("h4","","Сечение · координаты в мм"),button("Вписать сечение",fitSection,"mini-button"));views.append(projectionBar,preview,make("p","form-hint","Каркас — приближённый обзор. Потяните мышью для вращения. Фиолетовым показана плоскость сечения."),sectionHeading,sectionView);grid.append(rail,views);shell.append(header,summary,warning,grid);host.replaceChildren(shell);syncChecks();resetAxis();acceptBaseline();
    return {dispose(){alive=false;sequence++;},getSelected(){return [...selected];},hasDraft,refreshContexts,refreshSavedSections(next){const previous=saved.value,prior=selectedSpec();specs.splice(0,specs.length,...array(next));rebuildSaved(previous);if(previous&&!hasDraft()&&JSON.stringify(prior)!==JSON.stringify(selectedSpec()))loadSpec(selectedSpec());updateSaveButtons();}};
  }
  return {regionFromPoints,rasterBox,projectPoint,sectionRequest,sectionSnapshotMatches,filterModels,attachmentConflict,mountLibrary,mountStep};
});
