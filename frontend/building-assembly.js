(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports ? require("./room-assembly.js") : root.VioletRoomAssembly);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletBuildingAssembly = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (R) {
  "use strict";
  const list = value => Array.isArray(value) ? value : [];
  const ACTIVE = new Set(["queued", "running", "waiting", "waiting_for_walls", "waiting_for_rooms", "assembling", "cancelling"]);
  const STATUSES = new Set([...ACTIVE, "ready", "partial", "failed", "stale", "cancelled"]);
  function buildings(project) {return list(project?.buildings).map(item => ({id:item.id,label:item.name || "Здание",floors:list(item.floors).map(floor => ({id:floor.id,label:floor.name || "Этаж"}))}));}
  function sameContext(a,b) {return Boolean(a&&b&&a.project_id===b.project_id&&a.source_revision===b.source_revision&&a.building_id===b.building_id);}
  const key = value => JSON.stringify([value.project_id,value.source_revision,value.building_id]);
  function checkedJob(raw, expected, jobId) {
    const job=raw?.job || raw, id=job?.job_id || job?.id;
    if(!job||typeof id!=="string"||!id||!STATUSES.has(job.status))throw Error("Сервер вернул неизвестный статус сборки здания.");
    if(jobId&&id!==jobId || !sameContext({...job,source_revision:job.source_revision ?? job.revision},expected))throw Error("Ответ относится к другому зданию или версии проекта. Модель не заменена.");
    return {...job,job_id:id,result:job.result || raw.result};
  }
  function checkedResult(value, expected) {
    if(value?.schema_version!=="building-assembly/1.0" || !sameContext(value,expected) || !Number.isInteger(value.body_count) || value.body_count<1 || !Number.isInteger(value.wall_count) || value.wall_count<1)throw Error("3D-сборка не соответствует выбранному зданию или версии проекта.");
    return value;
  }
  function checkedChunks(result) {
    if(result.wire_preview)return [{id:"complete",floor_id:null,floor_label:"Здание",wire_preview:result.wire_preview}];
    const chunks=result.preview_chunks || result.preview?.chunks;
    if(!Array.isArray(chunks)||!chunks.length)throw Error("Нет доступных частей 3D-просмотра здания. Полную геометрию можно скачать в STEP.");
    const ids=new Set();
    return chunks.map(item=>{
      if(!item||typeof item.id!=="string"||!/^[a-zA-Z0-9_-]{1,128}$/.test(item.id)||ids.has(item.id)||typeof item.floor_id!=="string"||!item.floor_id)throw Error("Повреждён список частей 3D-модели здания.");
      ids.add(item.id);return {id:item.id,floor_id:item.floor_id,floor_label:item.floor_label || item.floor_name || "Этаж"};
    });
  }
  function statusMessage(job) {
    const total=job.progress?.total ?? job.total, done=job.progress?.completed ?? job.completed;
    const progress=Number.isFinite(total)&&total>0&&!/\d+\s+из\s+\d+/.test(job.message||"")?` · ${Number(done)||0} из ${total}`:"";
    if(ACTIVE.has(job.status))return (job.message || "Собираем отделку здания. Окно можно закрыть — расчёт продолжится в фоне.")+progress;
    if(job.status==="ready")return "3D-сборка здания готова. Показана сохранённая версия проекта.";
    if(job.status==="cancelled")return "Сборка здания остановлена. Сохранённый проект не изменён.";
    if(job.status==="stale")return "Сборка устарела. Сохраните проект и запустите её заново.";
    return job.message || job.error?.message || (typeof job.error==="string"?job.error:"Сборка здания не завершена. Причины указаны ниже.");
  }
  function create(host, options) {
    const make=(tag,cls,text)=>{const element=document.createElement(tag);if(cls)element.className=cls;if(text!==undefined)element.textContent=text;return element;};
    const button=(label,action,cls="button button-secondary")=>{const element=make("button",cls,label);element.type="button";element.addEventListener("click",action);return element;};
    const schedule=options.setTimeout || setTimeout,unschedule=options.clearTimeout || clearTimeout;
    let alive=true,opened=false,sequence=0,timer=null,context=null,job=null,result=null,busy=false,previewBusy=false,downloading=false,cancelling=false,store=null;
    let visible={...R.DEFAULT_VISIBLE},selectedFloors=new Set(),floorsFilterable=true;const memory=new Map(),floorInputs=new Map(),groupInputs={};
    const shell=make("section","building-assembly-workspace room-assembly-workspace"),header=make("header","wm-header"),heading=make("div");
    heading.append(make("h2","","3D здания"),make("p","wm-help","Отделка всех этажей: панели, каркас, узлы и крепёж в общих координатах."));
    header.append(heading,button("Закрыть",()=>close()));
    const status=make("p","ra-status ba-status","Выберите здание и нажмите «Собрать здание».");status.setAttribute("role","status");status.setAttribute("aria-live","polite");
    const layout=make("div","ra-layout ba-layout"),settings=make("aside","ra-settings ba-settings"),drawing=make("div","ra-drawing");
    const selector=make("select"),selectorLabel=make("label","wm-field");selector.setAttribute("aria-label","Здание для 3D-сборки");selectorLabel.append(make("span","","Здание"),selector);settings.append(selectorLabel);
    const buildButton=button("Собрать здание",()=>start(),"button button-primary button-wide"),cancelButton=button("Остановить сборку",()=>cancel(),"button button-secondary button-wide");
    cancelButton.hidden=!options.canCancel;settings.append(buildButton,cancelButton);
    const floorBox=make("fieldset","ba-floors"),floorLegend=make("legend","","Этажи в просмотре"),floorRows=make("div","ba-floor-list");
    floorBox.append(floorLegend,button("Все этажи",()=>setFloors([...floorInputs.keys()]),"button button-secondary ba-all-floors"),floorRows);settings.append(floorBox);
    const groups=make("fieldset","ra-groups");groups.append(make("legend","","Показать в модели"));
    for(const [id,label] of Object.entries(R.GROUPS)) {
      // Building STEP is the finish assembly, not the structural substrate.
      // Do not offer a visibility switch for geometry that is not included.
      if(id==="base")continue;
      const row=make("label","ra-check"),input=make("input");input.type="checkbox";input.checked=visible[id];input.setAttribute("aria-label",label);
      input.addEventListener("change",()=>{visible[id]=input.checked;viewer.setVisible(visible);update();});groupInputs[id]=input;row.append(input,make("span",`ra-role ra-role-${id}`,label));groups.append(row);
    }
    const nodesButton=button("Только узлы",()=>setVisible(R.ONLY_NODES),"button button-secondary button-wide");nodesButton.setAttribute("aria-pressed","false");
    settings.append(groups,nodesButton,button("Вся отделка",()=>setVisible(R.DEFAULT_VISIBLE),"button button-secondary button-wide"));
    const exportBox=make("section","ba-export"),completeButton=button("Скачать STEP здания",()=>download("complete"),"button button-secondary button-wide"),nodesExportButton=button("STEP без панелей",()=>download("nodes"),"button button-secondary button-wide"),exportHint=make("p","wm-help");
    exportBox.append(completeButton,nodesExportButton,exportHint);settings.append(exportBox);
    const facts=make("div","ra-facts"),notes=make("div","ra-notes");settings.append(facts,notes);
    const viewBar=make("div","ra-viewbar");for(const [label,mode] of [["Изометрия","iso"],["Сверху","top"],["Спереди","front"]])viewBar.append(button(label,()=>viewer.view(mode)));viewBar.append(button("Вписать",()=>viewer.fit()));
    const viewport=make("div","ra-viewport"),canvas=make("canvas","ra-canvas"),empty=make("p","ra-empty","Здесь появится 3D-сборка здания.");canvas.tabIndex=0;canvas.setAttribute("role","img");canvas.setAttribute("aria-label","3D отделки здания. Тяните для вращения, Shift для сдвига, колесо для масштаба.");viewport.append(canvas,empty);
    const previewStatus=make("p","ba-preview-status");previewStatus.setAttribute("role","status");
    drawing.append(viewBar,viewport,previewStatus,make("p","ra-help","Тяните — вращение · Shift + перетягивание — сдвиг · колесо — масштаб · двойной щелчок — вписать."));layout.append(settings,drawing);shell.append(header,status,layout);host.replaceChildren(shell);
    const viewer=(options.createViewer || R.createViewer)(canvas,empty,{onDetail:detail=>{if(!previewBusy)previewStatus.textContent=detail?.message || "";}});
    function source(){return options.getContext();}
    function current(expected=context){const value=source();return alive&&opened&&value&&!value.dirty&&sameContext(expected,{...value,building_id:selector.value});}
    function stop(){sequence++;if(timer!==null)unschedule(timer);timer=null;busy=false;previewBusy=false;downloading=false;cancelling=false;}
    function clearGeometry(){viewer.setGeometry(null);store?.clear?.();store=null;previewStatus.textContent="";}
    function remember(expected,id){const identity=key(expected);memory.set(identity,id);if(memory.size>64)memory.delete(memory.keys().next().value);try{options.storage?.setItem(`violet.building-assembly:${identity}`,id);}catch(_){}}
    function knownId(expected){try{return memory.get(key(expected)) || options.storage?.getItem(`violet.building-assembly:${key(expected)}`);}catch(_){return memory.get(key(expected));}}
    function update(){
      const valid=current(),ready=valid&&result&&job?.status==="ready";
      buildButton.disabled=!valid||busy||previewBusy||ACTIVE.has(job?.status);buildButton.textContent=["failed","cancelled"].includes(job?.status)?"Повторить сборку":result||job?"Обновить сборку":"Собрать здание";
      cancelButton.disabled=!valid||!ACTIVE.has(job?.status)||cancelling;cancelButton.textContent=cancelling?"Останавливаем…":"Остановить сборку";
      completeButton.disabled=!ready||downloading;nodesExportButton.disabled=!ready||downloading;
      completeButton.textContent=downloading?"Готовим STEP…":result?.coverage?.complete===false?"STEP назначенной отделки":"Скачать STEP здания";
      exportHint.textContent=(result?.coverage?.complete===false?"В STEP входит только назначенная и собранная отделка. ":"")+"Этажи и видимость фильтруют только просмотр. Полный STEP содержит отделку всех этажей; STEP без панелей — каркас, подоснову, узлы и крепёж. Несущие основания в эти файлы не входят.";
      nodesButton.setAttribute("aria-pressed",String(Object.keys(R.GROUPS).every(id=>visible[id]===R.ONLY_NODES[id])));
    }
    function setVisible(value){visible={...value};for(const [id,input]of Object.entries(groupInputs))input.checked=Boolean(visible[id]);viewer.setVisible(visible);update();}
    function setFloors(values){selectedFloors=new Set(values);for(const [id,input]of floorInputs)input.checked=selectedFloors.has(id);viewer.setFloors?.(floorsFilterable?selectedFloors:null);}
    function populateFloors(){
      floorRows.replaceChildren();floorInputs.clear();const building=buildings(options.getProject()).find(item=>item.id===selector.value);
      for(const floor of building?.floors || []){const row=make("div","ba-floor-row"),label=make("label","ra-check"),input=make("input");input.type="checkbox";input.checked=true;input.setAttribute("aria-label",`Показать этаж «${floor.label}»`);input.addEventListener("change",()=>{if(input.checked)selectedFloors.add(floor.id);else selectedFloors.delete(floor.id);setFloors(selectedFloors);});floorInputs.set(floor.id,input);label.append(input,make("span","",floor.label));const only=button("Только",()=>setFloors([floor.id]),"ba-floor-only");only.setAttribute("aria-label",`Показать только этаж «${floor.label}»`);row.append(label,only);floorRows.append(row);}
      setFloors([...floorInputs.keys()]);floorBox.hidden=!floorInputs.size;
    }
    function showFacts(){
      facts.replaceChildren();notes.replaceChildren();facts.append(make("strong","",`Сторон стен: ${result.wall_count} · деталей: ${result.body_count.toLocaleString("ru-RU")}`));
      const floorCount=result.floor_count || list(result.floors).length;
      if(floorCount)facts.append(make("p","wm-help",`Этажей: ${floorCount}`));
      if(result.coverage?.complete===false)notes.append(make("p","ra-warning","Собрана только назначенная отделка. Стороны без монтажа не включены в модель."));
      const check=result.checks?.cross_room || result.checks?.cross_wall || result.checks;
      if(check?.status==="valid"&&check.complete===true&&list(check.collisions).length===0)facts.append(make("p","wm-help","Непредусмотренных пересечений: 0; проверка завершена."));
      if(!check||check.complete!==true||check.status==="incomplete")notes.append(make("p","ra-warning","Проверка пересечений здания не завершена. Отсутствие конфликтов пока не подтверждено."));
      if(list(check?.collisions).length)notes.append(make("p","ra-warning",`Обнаружены пересечения: ${check.collisions.length}. Проверьте сопряжения перед использованием сборки.`));
      for(const issue of [...list(result.warnings),...list(result.checks?.issues),...list(job?.issues || job?.outcomes).filter(item=>!["ready","valid"].includes(item.status))])notes.append(make("p","ra-warning",typeof issue==="string"?issue:issue.message||issue.reason||issue.label||"Проверка требует внимания."));
    }
    async function loadPreview(expected,id,token){
      previewBusy=true;update();
      try {
        const chunks=checkedChunks(result),next=(options.createGeometryStore || R.createGeometryStore)();store=next;floorsFilterable=!result.wire_preview;
        if(floorsFilterable&&chunks.some(chunk=>!floorInputs.has(chunk.floor_id)))throw Error("В списке частей модели есть этаж другого здания.");
        floorBox.hidden=!floorsFilterable||!floorInputs.size;viewer.setGeometry(next);viewer.setVisible(visible);viewer.setFloors?.(floorsFilterable?selectedFloors:null);
        for(const [index,chunk]of chunks.entries()){
          if(!current(expected)||sequence!==token)return;
          previewStatus.textContent=`Загружаем модель: ${index+1} из ${chunks.length} частей…`;
          const payload=chunk.wire_preview?{wire_preview:chunk.wire_preview}:await options.request(`/building-assembly/${encodeURIComponent(id)}/preview/${encodeURIComponent(chunk.id)}`);
          if(!current(expected)||sequence!==token)return;
          if(payload.project_id&&payload.project_id!==expected.project_id || payload.building_id&&payload.building_id!==expected.building_id || payload.source_revision!=null&&payload.source_revision!==expected.source_revision || payload.floor_id&&payload.floor_id!==chunk.floor_id)throw Error("Часть модели относится к другому зданию, этажу или версии проекта.");
          next.appendChunk({...chunk,wire_preview:payload.wire_preview || payload.preview || payload});
          viewer.setGeometry(next,{fit:false});
        }
        viewer.fit();previewStatus.textContent=next.bounded_count?`${next.bounded_count} деталей упрощены только в просмотре. STEP содержит точную геометрию.`:"";
      }catch(error){if(current(expected)&&sequence===token){previewStatus.textContent=`3D-просмотр загружен не полностью: ${error.message}`;notes.append(make("p","ra-warning","Ошибка просмотра не меняет готовый STEP. Закройте окно и откройте его заново, чтобы повторить загрузку."));}}
      finally{if(sequence===token){previewBusy=false;update();}}
    }
    async function accept(raw,expected,id,token){
      job=checkedJob(raw,expected,id);remember(expected,job.job_id);status.textContent=statusMessage(job);shell.setAttribute("data-status",job.status);notes.replaceChildren();
      if(job.status==="ready"||job.status==="partial"){
        if(!job.result)throw Error("В готовом ответе отсутствует модель здания. Обновите сборку.");
        result=checkedResult(job.result,expected);showFacts();const check=result.checks?.cross_room || result.checks?.cross_wall || result.checks;
        if(!check||check.complete!==true||check.status!=="valid"||list(check.collisions).length){shell.setAttribute("data-status","warning");status.textContent="Сборка требует проверки пересечений; подробности слева.";}
        if(result.coverage?.complete===false){shell.setAttribute("data-status","warning");status.textContent="Готова сборка назначенной отделки. Стороны без монтажа не включены; подробности слева.";}
        await loadPreview(expected,job.job_id,token);
      }else{
        if(job.status==="failed"){
          status.textContent="Здание пока не собрано. Причина указана слева.";empty.hidden=false;empty.textContent="Сборка остановлена: сначала нужно устранить ошибку.";
          notes.append(make("strong","","Почему не получилось"),make("p","ra-warning",statusMessage(job)),make("p","wm-help","Если не готовы стены, проверьте сообщения расчёта проекта и монтаж указанных сторон. После исправления сохраните проект и повторите сборку здания."));
        }
        for(const issue of list(job.issues||job.outcomes))notes.append(make("p","ra-warning",typeof issue==="string"?issue:issue.message||issue.reason||"Сборка требует внимания."));
      }
      update();return job;
    }
    async function poll(expected,id,token){
      if(!current(expected)||sequence!==token)return;
      try{const raw=await options.request(`/building-assembly/${encodeURIComponent(id)}`);if(!current(expected)||sequence!==token)return;const value=await accept(raw,expected,id,token);if(current(expected)&&sequence===token&&ACTIVE.has(value.status))timer=schedule(()=>poll(expected,id,token),options.pollMs??2000);}
      catch(error){if(current(expected)&&sequence===token){busy=false;job=null;status.textContent=error.message;update();}}
    }
    function choose(){
      stop();job=null;result=null;floorsFilterable=true;clearGeometry();facts.replaceChildren();notes.replaceChildren();empty.hidden=false;empty.textContent="Здесь появится 3D-сборка здания.";populateFloors();
      const value=source();context=value&&selector.value?{project_id:value.project_id,source_revision:value.source_revision,building_id:selector.value}:null;
      if(!context){status.textContent="Сначала добавьте здание и этажи в проект.";update();return;}
      status.textContent=`Сохранённая версия r${context.source_revision}. Собирается отделка всех этажей выбранного здания.`;
      const id=knownId(context);if(id){status.textContent="Проверяем предыдущую сборку здания…";poll({...context},id,sequence);}update();
    }
    selector.addEventListener("change",choose);
    async function start(){
      if(!current()||busy||previewBusy||ACTIVE.has(job?.status))return false;
      const expected={...context},token=++sequence;busy=true;job=null;result=null;clearGeometry();facts.replaceChildren();notes.replaceChildren();status.textContent="Запускаем фоновую сборку здания…";empty.hidden=false;empty.textContent="Собираем отделку всех этажей…";update();
      try{const raw=await options.request("/building-assembly/start",{method:"POST",body:{project_id:expected.project_id,building_id:expected.building_id,expected_revision:expected.source_revision}});const started=checkedJob(raw,expected);if(alive)remember(expected,started.job_id);if(!current(expected)||sequence!==token)return false;busy=false;const value=await accept(raw,expected,null,token);if(!current(expected)||sequence!==token)return false;if(ACTIVE.has(value.status))timer=schedule(()=>poll(expected,value.job_id,token),options.pollMs??2000);return true;}
      catch(error){if(current(expected)&&sequence===token){status.textContent=error.message;job=null;}return false;}
      finally{if(sequence===token){busy=false;update();}}
    }
    async function cancel(){
      if(!options.canCancel||!current()||!ACTIVE.has(job?.status)||cancelling)return false;
      const expected={...context},id=job.job_id,token=++sequence;if(timer!==null)unschedule(timer);timer=null;cancelling=true;update();
      try{const raw=await options.request(`/building-assembly/${encodeURIComponent(id)}/cancel`,{method:"POST",body:{expected_revision:expected.source_revision}});if(!current(expected)||sequence!==token)return false;const value=await accept(raw,expected,id,token);if(ACTIVE.has(value.status))timer=schedule(()=>poll(expected,id,token),options.pollMs??2000);return true;}
      catch(error){if(current(expected)&&sequence===token){status.textContent=error.message;timer=schedule(()=>poll(expected,id,token),options.pollMs??2000);}return false;}
      finally{if(sequence===token){cancelling=false;update();}}
    }
    async function download(view="complete"){
      if(!["complete","nodes"].includes(view)||!current()||!result||job?.status!=="ready"||downloading)return false;
      const expected={...context},snapshot=result,id=job.job_id,token=sequence;downloading=true;update();
      try{
        // Check identity before handing a potentially very large STEP directly
        // to the browser's download manager. Never allocate it as a JS Blob.
        const latest=await options.request(`/building-assembly/${encodeURIComponent(id)}`);
        if(!current(expected)||sequence!==token||result!==snapshot)return false;
        if(checkedJob(latest,expected,id).status!=="ready")throw Error("Сборка изменилась или ещё не готова. Обновите 3D здания перед скачиванием.");
        const label=String(snapshot.label||"Здание").replace(/[^a-zA-Zа-яА-ЯёЁ0-9_-]+/g,"_").slice(0,80),filename=`${label}_r${expected.source_revision}_${view==="nodes"?"без_панелей":"отделка"}.step`;
        const url=`/api/v1/building-assembly/${encodeURIComponent(id)}/export.step?view=${view}`;
        if(options.downloadFile)options.downloadFile(url,filename);
        else{const anchor=make("a");anchor.href=url;anchor.download=filename;anchor.click();}
        return true;
      }
      catch(error){if(current(expected)&&sequence===token)status.textContent=error.message;return false;}
      finally{if(sequence===token){downloading=false;update();}}
    }
    function open({building_id}={}){
      if(!alive||!source())return false;stop();opened=true;selector.replaceChildren();const choices=buildings(options.getProject());for(const value of choices){const option=make("option","",value.label);option.value=value.id;selector.append(option);}selector.value=choices.some(value=>value.id===building_id)?building_id:choices[0]?.id||"";selector.disabled=!choices.length;choose();viewer.resize?.();return true;
    }
    function close(){if(!opened)return true;stop();opened=false;clearGeometry();options.onClose?.();return true;}
    function refresh(){if(!opened)return;if(!current()){stop();job=null;result=null;clearGeometry();status.textContent="Проект изменился. Сохраните правки и откройте 3D здания заново.";empty.hidden=false;empty.textContent="Устаревшая модель скрыта.";}update();}
    return {open,close,start,cancel,download,refresh,setVisible,setFloors,hasDraft:()=>false,dispose(){close();alive=false;viewer.dispose();host.replaceChildren();}};
  }
  return {ACTIVE,buildings,sameContext,checkedJob,checkedResult,checkedChunks,statusMessage,create};
});
