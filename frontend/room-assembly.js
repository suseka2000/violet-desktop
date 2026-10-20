(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletRoomAssembly = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const list = value => Array.isArray(value) ? value : [];
  const ACTIVE = new Set(["queued", "running", "waiting", "waiting_for_walls"]);
  const GROUPS = Object.freeze({panels:"Панели", frame:"Каркас / подоснова", nodes:"Узлы и крепёж", base:"Основание"});
  const COLORS = {panels:"#a48b5d", frame:"#45749e", nodes:"#7047b4", base:"#aeb9c0"};
  const DEFAULT_VISIBLE = Object.freeze({panels:true, frame:true, nodes:true, base:false});
  const ONLY_NODES = Object.freeze({panels:false, frame:false, nodes:true, base:false});
  const mm = value => Number(value).toLocaleString("ru-RU", {maximumFractionDigits:1});
  function roleGroup(role) {
    if (["panel", "adhesive", "door_leaf", "hatch_leaf"].includes(role)) return "panels";
    if (["frame", "backing", "subframe"].includes(role)) return "frame";
    if (["substrate", "base", "floor", "ceiling", "wall"].includes(role)) return "base";
    return "nodes";
  }
  function rooms(project, floorId) {
    return list(project?.buildings).flatMap(building => list(building.floors).filter(floor => !floorId || floor.id === floorId).flatMap(floor => list(floor.spaces).map(space => ({id:space.id, floor_id:floor.id, label:[space.number || space.code, space.name || "Помещение"].filter(Boolean).join(" · "), floor_label:floor.name || "Этаж"}))));
  }
  function sameContext(a, b) {return Boolean(a && b && a.project_id === b.project_id && a.source_revision === b.source_revision && a.space_id === b.space_id);}
  function contextKey(value) {return JSON.stringify([value.project_id, value.source_revision, value.space_id]);}
  function checkedResult(value, expected) {
    if (value?.schema_version !== "room-assembly/1.0" || value.space_id !== expected.space_id || value.source_revision !== expected.source_revision || value.project_id && value.project_id !== expected.project_id || typeof value.id !== "string" || !value.id || !Number.isInteger(value.wall_count) || value.wall_count < 1 || !Number.isInteger(value.body_count) || value.body_count < 1) throw Error("Сборка не соответствует выбранной комнате или версии проекта. Постройте её заново.");
    return value;
  }
  function checkedJob(raw, expected, jobId) {
    const value = raw?.job || raw;
    if (!value || typeof value !== "object" || typeof (value.job_id || value.id) !== "string" || !["queued", "waiting", "waiting_for_walls", "running", "ready", "partial", "failed", "stale", "cancelled"].includes(value.status)) throw Error("Сервер вернул неизвестный статус сборки комнаты.");
    const id = value.job_id || value.id;
    if (jobId && id !== jobId || value.project_id !== expected.project_id || value.space_id !== expected.space_id || (value.source_revision ?? value.revision) !== expected.source_revision) throw Error("Ответ относится к другой комнате или версии проекта. Сборка не заменена.");
    return {...value, job_id:id, result:value.result || raw.result};
  }
  function statusMessage(job) {
    const total=job.progress?.total ?? job.total,completed=job.progress?.completed ?? job.completed;
    const suffix = Number.isFinite(total) && total > 0 ? ` · ${completed || 0} из ${total}` : "";
    if (ACTIVE.has(job.status)) return (job.message || (job.status === "running" ? "Собираем отделку комнаты и проверяем соединения…" : "Задача в очереди. Можно закрыть окно — расчёт продолжится в фоне.")) + suffix;
    if (job.status === "ready") return "3D-сборка комнаты готова. Показана сохранённая версия проекта.";
    if (job.status === "partial") return job.message || "Готова часть комнаты. Причины неполной сборки перечислены ниже.";
    if (job.status === "stale") return "Предыдущая сборка устарела. Нужна новая сборка комнаты.";
    return job.message || job.error?.message || (typeof job.error === "string" ? job.error : "Не удалось собрать комнату. Проверьте монтаж всех её стен и сообщения ниже.");
  }
  const finitePoint = point => Array.isArray(point) && point.length === 3 && point.every(Number.isFinite);
  function transformPoint(point, rotation, translation) {return rotation.map((row,axis) => row.reduce((sum,factor,index) => sum+factor*point[index],translation[axis]));}
  function corners(min,max) {return Array.from({length:8},(_,index)=>[0,1,2].map(axis=>(index>>axis)&1?max[axis]:min[axis]));}
  // Legacy callers can inspect paths, but the renderer never expands instances.
  // A repeated screw thus retains one immutable outline, not thousands of copies.
  function lazyPaths(geometry) {
    function* iterate() {for(const instance of geometry.instances())for(const path of instance.definition.paths)yield {group:instance.group,role:instance.role,instance_id:instance.id,floor_id:instance.floor_id,points:path.map(point=>transformPoint(point,instance.rotation,instance.translation_mm))};}
    const methods={
      [Symbol.iterator]:iterate,
      at(index){if(index<0)index+=geometry.path_count;let offset=0;for(const item of iterate())if(offset++===index)return item;},
      forEach(fn){let index=0;for(const item of iterate())fn(item,index++);},
      map(fn){const out=[];let index=0;for(const item of iterate())out.push(fn(item,index++));return out;},
      filter(fn){const out=[];let index=0;for(const item of iterate())if(fn(item,index++))out.push(item);return out;},
      every(fn){let index=0;for(const item of iterate())if(!fn(item,index++))return false;return true;},
      some(fn){let index=0;for(const item of iterate())if(fn(item,index++))return true;return false;},
    };
    return new Proxy([], {get(target,key){if(key==="length")return geometry.path_count;if(key in methods)return methods[key];if(typeof key==="string"&&/^(0|[1-9][0-9]*)$/.test(key))return methods.at(Number(key));return Reflect.get(target,key);}});
  }
  // Only explicitly declared bounds_only definitions become boxes. Automatic
  // display LOD samples original native edges and is always labelled as an overview.
  function wireGeometry(preview, metadata={}) {
    if(preview?.schema!=="wall-assembly-wire-preview/1"||!Array.isArray(preview.definitions)||!Array.isArray(preview.instances))throw Error("Нет поддерживаемого 3D-просмотра комнаты.");
    const definitions=new Map(),instances=[],min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];
    let points=0,stored=0,bounded=0,pathCount=0;
    for(const item of preview.definitions){
      if(!item?.id||definitions.has(item.id))throw Error("Повторяются определения деталей комнаты.");
      let source=item.paths;
      if(item.representation==="bounds_only"){
        if(!finitePoint(item.bounds_mm?.min)||!finitePoint(item.bounds_mm?.max)||item.bounds_mm.min.some((v,a)=>v>item.bounds_mm.max[a]))throw Error("Повреждены габариты 3D-детали.");
        const vertices=corners(item.bounds_mm.min,item.bounds_mm.max);
        source=vertices.flatMap((vertex,index)=>[0,1,2].filter(axis=>!(index&1<<axis)).map(axis=>[vertex,vertices[index|1<<axis]]));
      }
      if(!Array.isArray(source)||!source.length)throw Error("Отсутствуют контуры детали в 3D-сборке.");
      const poolKey=metadata.definitionPool?JSON.stringify([item.representation||"exact_edges",source]):null;
      const shared=poolKey&&metadata.definitionPool.get(poolKey);
      if(shared){definitions.set(item.id,shared);stored+=shared.point_count;continue;}
      const lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];let count=0;
      const paths=source.map(path=>{
        if(!Array.isArray(path)||path.length<2||!path.every(finitePoint))throw Error("Повреждены контуры 3D-детали.");
        count+=path.length;
        return Object.freeze(path.map(point=>{for(let axis=0;axis<3;axis++){lo[axis]=Math.min(lo[axis],point[axis]);hi[axis]=Math.max(hi[axis],point[axis]);}return Object.freeze([...point]);}));
      });
      const definition=Object.freeze({id:item.id,representation:item.representation,paths:Object.freeze(paths),min:Object.freeze(lo),max:Object.freeze(hi),point_count:count});
      if(poolKey)metadata.definitionPool.set(poolKey,definition);
      stored+=count;definitions.set(item.id,definition);
    }
    for(const value of preview.instances){
      const definition=definitions.get(value.definition_id),rotation=value.rotation,translation=value.translation_mm;
      if(!definition||!Array.isArray(rotation)||rotation.length!==3||!rotation.every(finitePoint)||!finitePoint(translation))throw Error("Повреждены координаты 3D-детали комнаты.");
      const lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];
      for(const point of corners(definition.min,definition.max)){
        const transformed=transformPoint(point,rotation,translation);
        if(!transformed.every(Number.isFinite))throw Error("Переполнение координат 3D-детали.");
        for(let axis=0;axis<3;axis++){lo[axis]=Math.min(lo[axis],transformed[axis]);hi[axis]=Math.max(hi[axis],transformed[axis]);min[axis]=Math.min(min[axis],transformed[axis]);max[axis]=Math.max(max[axis],transformed[axis]);}
      }
      instances.push(Object.freeze({id:value.id,role:value.role,group:roleGroup(value.role),floor_id:metadata.floor_id??value.floor_id??null,chunk_id:metadata.id??null,definition,rotation:Object.freeze(rotation.map(row=>Object.freeze([...row]))),translation_mm:Object.freeze([...translation]),min:Object.freeze(lo),max:Object.freeze(hi)}));
      points+=definition.point_count;pathCount+=definition.paths.length;if(definition.representation==="bounds_only")bounded++;
    }
    if(!instances.length)throw Error("В сборке нет геометрии для просмотра.");
    Object.freeze(instances);
    const geometry={min:Object.freeze(min),max:Object.freeze(max),point_count:points,stored_point_count:stored,bounded_count:bounded,path_count:pathCount,instance_count:instances.length,body_count:instances.length,definition_count:definitions.size,instances:()=>instances.values(),definitions:()=>definitions.values()};
    geometry.paths=lazyPaths(geometry);return Object.freeze(geometry);
  }
  // Chunks can be loaded/unloaded per floor. Neither appending nor toggling a
  // floor copies transformed paths; exports remain independent of visibility.
  function createGeometryStore() {
    const chunks=new Map(),definitionPool=new Map();let bounds={min:[0,0,0],max:[0,0,0]},stats={},activeDefinitions=new Set();
    function recalculate(){
      const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];stats={point_count:0,stored_point_count:0,bounded_count:0,path_count:0,instance_count:0,definition_count:0};
      const unique=new Set();
      for(const {geometry} of chunks.values()){for(const key of Object.keys(stats))if(!["stored_point_count","definition_count"].includes(key))stats[key]+=geometry[key];for(const definition of geometry.definitions())unique.add(definition);for(let axis=0;axis<3;axis++){min[axis]=Math.min(min[axis],geometry.min[axis]);max[axis]=Math.max(max[axis],geometry.max[axis]);}}
      stats.definition_count=unique.size;for(const definition of unique)stats.stored_point_count+=definition.point_count;
      for(const [key,definition] of definitionPool)if(!unique.has(definition))definitionPool.delete(key);
      activeDefinitions=unique;
      bounds=chunks.size?{min,max}:{min:[0,0,0],max:[0,0,0]};
    }
    const store={
      appendChunk(chunk){
        if(typeof chunk?.id!=="string"||!chunk.id)throw Error("Не указан идентификатор части модели.");
        let geometry;try{geometry=wireGeometry(chunk.wire_preview,{id:chunk.id,floor_id:chunk.floor_id,definitionPool});}catch(error){recalculate();throw error;}
        const replacing=chunks.has(chunk.id),first=!chunks.size;
        chunks.set(chunk.id,{geometry,floor_id:chunk.floor_id??null,label:chunk.floor_label||chunk.floor_name||"Этаж"});
        if(replacing)recalculate();
        else{
          // Streaming N chunks is linear, not N repeated scans of all earlier chunks.
          for(const key of Object.keys(stats))if(!["stored_point_count","definition_count"].includes(key))stats[key]+=geometry[key];
          for(const definition of geometry.definitions())if(!activeDefinitions.has(definition)){activeDefinitions.add(definition);stats.definition_count++;stats.stored_point_count+=definition.point_count;}
          bounds=first?{min:[...geometry.min],max:[...geometry.max]}:{min:bounds.min.map((v,a)=>Math.min(v,geometry.min[a])),max:bounds.max.map((v,a)=>Math.max(v,geometry.max[a]))};
        }
        return geometry;
      },
      removeChunk(id){const removed=chunks.delete(id);if(removed)recalculate();return removed;},
      clear(){chunks.clear();recalculate();},
      *instances(){for(const {geometry} of chunks.values())yield* geometry.instances();},
      floors(){const floors=new Map();for(const chunk of chunks.values()){if(!floors.has(chunk.floor_id))floors.set(chunk.floor_id,{id:chunk.floor_id,label:chunk.label,instance_count:0});floors.get(chunk.floor_id).instance_count+=chunk.geometry.instance_count;}return [...floors.values()];},
      get min(){return bounds.min;},get max(){return bounds.max;},get chunk_count(){return chunks.size;},get body_count(){return stats.instance_count;},
    };
    recalculate();for(const key of Object.keys(stats))Object.defineProperty(store,key,{get:()=>stats[key],enumerable:true});store.paths=lazyPaths(store);return store;
  }
  function projectPoint(point, center, yaw, pitch) {
    const [x,y,z] = point.map((value,axis) => value - center[axis]);
    return [x*Math.cos(yaw)+y*Math.sin(yaw), -z*Math.cos(pitch)+(-x*Math.sin(yaw)+y*Math.cos(yaw))*Math.sin(pitch)];
  }
  function createViewer(canvas, empty, options = {}) {
    // The bitmap must never size its own CSS viewport. A late/failed stylesheet
    // otherwise makes each DPR resize double the intrinsic canvas dimensions.
    // Positioning it out of normal flow also keeps ResizeObserver stable.
    const viewport=canvas.parentElement;
    if(canvas.style&&viewport?.style){
      Object.assign(canvas.style,{position:"absolute",inset:"0",display:"block",width:"100%",height:"100%"});
      const style=globalThis.getComputedStyle?.(viewport);
      if(!style||style.position==="static"){
        viewport.style.position="relative";
        if(!parseFloat(style?.minHeight))viewport.style.minHeight="280px";
      }
    }
    let geometry = null, visible = {...DEFAULT_VISIBLE}, floors=null, conflicts=new Set(), yaw = -.65, pitch = .55, zoom = 1, pan = [0,0], drag = null, alive = true, queued = false,lastDetail="";
    let baseScale=1,fitPending=true,fittedWidth=0,fittedHeight=0;
    const frame = options.frame || (callback => typeof requestAnimationFrame === "function" ? requestAnimationFrame(callback) : callback());
    function draw() {
      queued = false; if (!alive) return;
      const rect = canvas.getBoundingClientRect(), hasLayout=Number.isFinite(rect.width)&&Number.isFinite(rect.height)&&rect.width>0&&rect.height>0;
      const width=hasLayout?rect.width:1,height=hasLayout?rect.height:1;
      // This is a screen raster budget, not a limit on model parts or edges.
      // Oversized/zoomed hosts cannot ask the browser for an impossible bitmap.
      const dpr=Math.min(2,Math.max(.1,globalThis.devicePixelRatio||1),8192/width,8192/height,Math.sqrt(16777216/(width*height)));
      if (canvas.width !== Math.round(width*dpr) || canvas.height !== Math.round(height*dpr)) {canvas.width = Math.round(width*dpr); canvas.height = Math.round(height*dpr);}
      const ctx = canvas.getContext?.("2d"); if (!ctx) return;
      ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,width,height);
      // A hidden modal has no usable viewport yet. Defer fitting until its
      // first real layout instead of retaining a one-pixel initial scale.
      if (!geometry || !hasLayout) return;
      if(geometry.instance_count===0){empty.hidden=false;return;}
      let min=geometry.min,max=geometry.max;
      if(floors){min=[Infinity,Infinity,Infinity];max=[-Infinity,-Infinity,-Infinity];for(const instance of geometry.instances())if(floors.has(instance.floor_id))for(let axis=0;axis<3;axis++){min[axis]=Math.min(min[axis],instance.min[axis]);max[axis]=Math.max(max[axis],instance.max[axis]);}if(!Number.isFinite(min[0])){empty.hidden=false;empty.textContent="Все этажи скрыты. Включите нужный этаж.";return;}}
      const center = min.map((value,axis) => value/2+max[axis]/2);
      const cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch),ox=width/2+pan[0],oy=height/2+pan[1];
      if(fitPending||width!==fittedWidth||height!==fittedHeight){
        // Orthographic bounds of all eight box corners, with 8% padding on
        // each side. Fit both screen axes, not the longest world dimension.
        const extent=max.map((value,axis)=>value-min[axis]);
        const projectedWidth=Math.max(1,Math.abs(cy)*extent[0]+Math.abs(sy)*extent[1]);
        const projectedHeight=Math.max(1,Math.abs(sy*sp)*extent[0]+Math.abs(cy*sp)*extent[1]+Math.abs(cp)*extent[2]);
        baseScale=Math.min(width*.84/projectedWidth,height*.84/projectedHeight);
        fittedWidth=width;fittedHeight=height;fitPending=false;
      }
      // Orbit never refits: the model must not pulse as its silhouette changes.
      // Resize changes only the fit scale, preserving the user's zoom and pan.
      const scale=baseScale*zoom;
      const u=[cy*scale,sy*scale,0],v=[-sy*sp*scale,cy*sp*scale,-cp*scale];
      const au=u.map(Math.abs),av=v.map(Math.abs);
      const candidates=[];let total=0,cost=0,simplified=false,rendered=0,shown=0;
      for(const instance of geometry.instances()){
        if(!visible[instance.group]||floors&&!floors.has(instance.floor_id))continue;total++;
        const lo=instance.min,hi=instance.max,mx=(lo[0]+hi[0])/2-center[0],my=(lo[1]+hi[1])/2-center[1],mz=(lo[2]+hi[2])/2-center[2];
        const hx=(hi[0]-lo[0])/2,hy=(hi[1]-lo[1])/2,hz=(hi[2]-lo[2])/2;
        const sx=ox+u[0]*mx+u[1]*my+u[2]*mz,sy2=oy+v[0]*mx+v[1]*my+v[2]*mz;
        const rx=au[0]*hx+au[1]*hy+au[2]*hz,ry=av[0]*hx+av[1]*hy+av[2]*hz;
        // Culling uses conservative projected bounds, never substitutes them for edges.
        if(sx+rx<0||sx-rx>width||sy2+ry<0||sy2-ry>height)continue;
        const pixels=Math.max(rx,ry)*2,exact=instance.definition.point_count;
        const allowance=conflicts.has(instance.id)?exact:Math.min(exact,pixels<2?4:pixels<8?24:drag?100:Infinity);
        candidates.push({instance,allowance});cost+=allowance;if(allowance<exact)simplified=true;
      }
      empty.hidden=total>0;if(!total)empty.textContent="Все группы скрыты. Включите нужные детали слева.";
      const budget=Math.max(32,drag?(options.interactionPointBudget||45000):(options.pointBudget||240000));
      const ordinaryBudget=conflicts.size?Math.floor(budget*.8):budget;
      const instanceStride=Math.max(1,Math.ceil(candidates.length*2/ordinaryBudget));
      const factor=Math.min(1,ordinaryBudget/Math.max(cost,1));
      if(factor<1||instanceStride>1)simplified=true;
      const selected=candidates.filter((entry,index)=>index%instanceStride===0||conflicts.has(entry.instance.id));
      // Project each unique definition through its instance matrix directly.
      // No transformed 3D vertex arrays are allocated while orbiting or filtering.
      function traceInstance(instance,limit){
        if(limit<2)return false;
        const r=instance.rotation,t=instance.translation_mm;
        const pu0=u[0]*r[0][0]+u[1]*r[1][0]+u[2]*r[2][0],pu1=u[0]*r[0][1]+u[1]*r[1][1]+u[2]*r[2][1],pu2=u[0]*r[0][2]+u[1]*r[1][2]+u[2]*r[2][2];
        const pv0=v[0]*r[0][0]+v[1]*r[1][0]+v[2]*r[2][0],pv1=v[0]*r[0][1]+v[1]*r[1][1]+v[2]*r[2][1],pv2=v[0]*r[0][2]+v[1]*r[1][2]+v[2]*r[2][2];
        const dx=t[0]-center[0],dy=t[1]-center[1],dz=t[2]-center[2],tx=ox+u[0]*dx+u[1]*dy+u[2]*dz,ty=oy+v[0]*dx+v[1]*dy+v[2]*dz;
        const paths=instance.definition.paths,full=limit>=instance.definition.point_count,pathStride=full?1:Math.max(1,Math.ceil(paths.length*2/limit));
        const pathBudget=full?Infinity:Math.max(2,Math.floor(limit/Math.ceil(paths.length/pathStride)));let used=0;
        for(let index=0;index<paths.length&&used+2<=limit;index+=pathStride){
          const path=paths[index],step=Math.max(1,Math.ceil((path.length-1)/Math.max(1,pathBudget-1)));
          const plot=(point,start)=>{const x=tx+pu0*point[0]+pu1*point[1]+pu2*point[2],y=ty+pv0*point[0]+pv1*point[1]+pv2*point[2];if(start)ctx.moveTo(x,y);else ctx.lineTo(x,y);used++;};
          plot(path[0],true);for(let point=step;point<path.length-1&&used+1<limit;point+=step)plot(path[point],false);if(used<limit)plot(path[path.length-1],false);
        }
        rendered+=used;return used>0;
      }
      for (const group of ["base","panels","frame","nodes"]) {
        if (!visible[group]) continue;
        ctx.beginPath(); ctx.strokeStyle = COLORS[group]; ctx.lineWidth = group === "nodes" ? 1.05 : .85;
        for(const {instance,allowance} of selected)if(instance.group===group){const limit=Math.min(ordinaryBudget-rendered,Math.max(2,Math.floor(allowance*factor)));if(traceInstance(instance,limit))shown++;}
        ctx.stroke();
      }
      if(conflicts.size){const highlighted=selected.filter(item=>conflicts.has(item.instance.id)),allowance=Math.max(2,Math.floor((budget-rendered)/Math.max(1,highlighted.length)));ctx.beginPath();ctx.strokeStyle="#d52c3b";ctx.lineWidth=1.7;for(const {instance} of highlighted)traceInstance(instance,Math.min(budget-rendered,allowance));ctx.stroke();}
      ctx.font = "12px system-ui"; ctx.lineWidth = 2;
      for (const [axis,label,color] of [[0,"X","#b44545"],[1,"Y","#338354"],[2,"Z","#456fc1"]]) {
        const point = [0,0,0]; point[axis] = 32; const end = projectPoint(point,[0,0,0],yaw,pitch);
        ctx.beginPath();ctx.strokeStyle=color;ctx.moveTo(48,height-50);ctx.lineTo(48+end[0],height-50+end[1]);ctx.stroke();ctx.fillStyle=color;ctx.fillText(label,48+end[0]+4,height-50+end[1]);
      }
      const message=simplified?"Обзорный вид: мелкие контуры сокращены. Приблизьте участок. STEP — точная геометрия.":geometry.bounded_count?"Часть деталей показана габаритами. STEP — точная геометрия.":"Контуры исходных деталей · STEP — точная геометрия.";
      ctx.fillStyle="#46546a";ctx.font="11px system-ui";ctx.fillText(message,12,20,Math.max(1,width-24));
      const detail={simplified,shown_instances:shown,total_instances:total,rendered_points:rendered,message},key=JSON.stringify(detail);
      if(key!==lastDetail){lastDetail=key;options.onDetail?.(detail);}
    }
    function schedule() {if (!queued && alive) {queued = true; frame(draw);}}
    function fit() {zoom=1;pan=[0,0];fitPending=true;schedule();}
    canvas.addEventListener("pointerdown", event => {
      if (!geometry || ![0,1,2].includes(event.button)) return;
      event.preventDefault();canvas.focus?.({preventScroll:true});canvas.setPointerCapture?.(event.pointerId);
      drag={id:event.pointerId,x:event.clientX,y:event.clientY,yaw,pitch,pan:[...pan],mode:event.button !== 0 || event.shiftKey ? "pan" : "orbit"};
    });
    canvas.addEventListener("pointermove", event => {
      if (!drag || event.pointerId !== drag.id) return;
      const dx=event.clientX-drag.x,dy=event.clientY-drag.y;
      if (drag.mode === "pan") pan=[drag.pan[0]+dx,drag.pan[1]+dy];
      else {yaw=drag.yaw+dx*.008;pitch=Math.max(-Math.PI/2,Math.min(Math.PI/2,drag.pitch+dy*.008));}
      schedule();
    });
    for (const name of ["pointerup","pointercancel","lostpointercapture"]) canvas.addEventListener(name,()=>{if(drag){drag=null;schedule();}});
    canvas.addEventListener("contextmenu", event => event.preventDefault());
    canvas.addEventListener("wheel", event => {event.preventDefault();zoom=Math.max(.02,Math.min(1000000,zoom*Math.exp(-event.deltaY*.001)));schedule();},{passive:false});
    canvas.addEventListener("dblclick", fit);
    canvas.addEventListener("keydown", event => {
      if (!["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","+","-","=","Home"].includes(event.key)) return;
      event.preventDefault();event.stopPropagation();
      if(event.key === "Home") fit();
      else if(["+","=","-"].includes(event.key)) zoom=Math.max(.02,Math.min(1000000,zoom*(event.key === "-" ? 1/1.15 : 1.15)));
      else if(event.shiftKey) {if(event.key === "ArrowLeft")pan[0]-=20;if(event.key === "ArrowRight")pan[0]+=20;if(event.key === "ArrowUp")pan[1]-=20;if(event.key === "ArrowDown")pan[1]+=20;}
      else {if(event.key === "ArrowLeft")yaw-=.1;if(event.key === "ArrowRight")yaw+=.1;if(event.key === "ArrowUp")pitch=Math.max(-Math.PI/2,pitch-.1);if(event.key === "ArrowDown")pitch=Math.min(Math.PI/2,pitch+.1);}schedule();
    });
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(schedule) : null;observer?.observe(canvas);
    return {
      setGeometry(value,{fit:shouldFit=true}={}) {geometry=value;empty.hidden=Boolean(value);if(shouldFit)fit();else schedule();},
      setVisible(value) {visible={...value};schedule();},
      setFloors(value) {floors=value===null?null:new Set(value);fit();},
      setConflicts(value) {conflicts=new Set(value);schedule();},
      fit, resize:schedule,
      view(mode) {yaw=mode === "front" ? 0 : -.65;pitch=mode === "top" ? Math.PI/2 : mode === "front" ? 0 : .55;fit();},
      dispose() {alive=false;observer?.disconnect();geometry=null;drag=null;},
    };
  }
  function create(host, options) {
    const make=(tag,cls,text)=>{const item=document.createElement(tag);if(cls)item.className=cls;if(text!==undefined)item.textContent=text;return item;};
    const button=(label,action,cls="button button-secondary")=>{const item=make("button",cls,label);item.type="button";item.addEventListener("click",action);return item;};
    const memory = new Map();
    let opened=false,alive=true,sequence=0,timer=null,context=null,job=null,result=null,busy=false,downloading=false,previewBusy=false,store=null,visible={...DEFAULT_VISIBLE};
    const shell=make("section","room-assembly-workspace"), header=make("header","wm-header"), title=make("div"), heading=make("h2","","3D комнаты");
    title.append(heading,make("p","wm-help","Отделка стен комнаты: панели, каркас, монтажные профили и крепёж в координатах проекта."));
    header.append(title,button("Закрыть",()=>close()));
    const status=make("p","ra-status","Выберите комнату и нажмите «Собрать комнату».");status.setAttribute("role","status");status.setAttribute("aria-live","polite");
    const layout=make("div","ra-layout"), settings=make("aside","ra-settings"), drawing=make("div","ra-drawing");
    const selector=make("select");selector.setAttribute("aria-label","Комната для 3D-сборки");
    const label=make("label","wm-field");label.append(make("span","","Комната"),selector);settings.append(label);
    const buildButton=button("Собрать комнату",()=>start(),"button button-primary button-wide"), downloadButton=button("Скачать полный STEP",()=>download(),"button button-secondary button-wide");
    const exportHint=make("p","wm-help");
    const cancelButton=button("Остановить сборку",()=>cancel(),"button button-secondary button-wide");
    settings.append(buildButton,cancelButton,downloadButton,exportHint);
    const groups=make("fieldset","ra-groups"), checks={},groupRows={};groups.append(make("legend","","Показать в модели"));
    for (const [key,name] of Object.entries(GROUPS)) {const row=make("label","ra-check"),input=make("input");input.type="checkbox";input.checked=visible[key];input.setAttribute("aria-label",name);input.addEventListener("change",()=>{visible[key]=input.checked;viewer.setVisible(visible);update();});checks[key]=input;groupRows[key]=row;row.append(input,make("span",`ra-role ra-role-${key}`,name));groups.append(row);}
    settings.append(groups);
    const onlyButton=button("Только узлы",()=>setVisible(ONLY_NODES),"button button-secondary button-wide");onlyButton.setAttribute("aria-pressed","false");
    settings.append(onlyButton,button("Показать отделку целиком",()=>setVisible(DEFAULT_VISIBLE),"button button-secondary button-wide"),make("p","wm-help","«Только узлы»: профили, саморезы, анкеры и дистанционные опоры. Панели, каркас, подоснова и основание скрыты."));
    const facts=make("div","ra-facts"),notes=make("div","ra-notes");settings.append(facts,notes);
    const viewBar=make("div","ra-viewbar");viewBar.append(button("Изометрия",()=>viewer.view("iso")),button("Сверху",()=>viewer.view("top")),button("Спереди",()=>viewer.view("front")),button("Вписать",()=>viewer.fit()));
    const viewport=make("div","ra-viewport"),canvas=make("canvas","ra-canvas"),empty=make("p","ra-empty","Здесь появится 3D-сборка выбранной комнаты.");canvas.tabIndex=0;canvas.setAttribute("role","img");canvas.setAttribute("aria-label","3D-сборка отделки комнаты. Перетягивание — вращение, Shift с перетягиванием — сдвиг, колесо — масштаб.");viewport.append(canvas,empty);
    const previewStatus=make("p","wm-help");previewStatus.setAttribute("role","status");
    drawing.append(viewBar,viewport,previewStatus,make("p","ra-help","Тяните — вращение · Shift + перетягивание или средняя/правая кнопка — сдвиг · колесо — масштаб · двойной щелчок — вписать."));
    layout.append(settings,drawing);shell.append(header,status,layout);host.replaceChildren(shell);
    const viewer=(options.createViewer || createViewer)(canvas,empty);
    const schedule=options.setTimeout || setTimeout,unschedule=options.clearTimeout || clearTimeout;
    function stopPoll() {sequence++;if(timer!==null)unschedule(timer);timer=null;busy=false;downloading=false;previewBusy=false;store?.clear();store=null;previewStatus.textContent="";}
    function source() {return options.getContext();}
    function current(expected=context) {const value=source();return alive&&opened&&value&&sameContext(expected,{...value,space_id:selector.value})&&!value.dirty;}
    function loadId(value) {const key=contextKey(value);try{return memory.get(key)||options.storage?.getItem(`violet.room-assembly:${key}`)||null;}catch(_){return memory.get(key)||null;}}
    function remember(value,id) {const key=contextKey(value);memory.set(key,id);try{options.storage?.setItem(`violet.room-assembly:${key}`,id);}catch(_){/* In-memory recovery still works when storage is unavailable. */}}
    function update() {
      const valid=current();buildButton.disabled=!valid||busy||ACTIVE.has(job?.status);buildButton.textContent=result||job ? "Обновить сборку" : "Собрать комнату";
      cancelButton.disabled=!valid||!ACTIVE.has(job?.status);cancelButton.hidden=!ACTIVE.has(job?.status);
      downloadButton.disabled=!valid||busy||downloading||!result||job?.status!=="ready";
      const partial=result?.coverage?.complete===false;
      downloadButton.textContent=downloading?"Готовим STEP…":partial?"STEP назначенной отделки":"Скачать полный STEP";
      exportHint.textContent=(partial?`В STEP входит только назначенная отделка: ${result.coverage.mounted_wall_count} из ${result.coverage.total_wall_count} сторон. Остальные стороны не собраны. `:"STEP содержит всю отделку, включая скрытые в просмотре панели и каркас. ")+"Виртуальное архитектурное основание в файл не входит. Видимость не меняет экспорт.";
      onlyButton.setAttribute("aria-pressed",String(Object.keys(GROUPS).every(key=>visible[key]===ONLY_NODES[key])));
    }
    function setVisible(value) {visible={...value};for(const key of Object.keys(checks))checks[key].checked=visible[key];viewer.setVisible(visible);update();}
    function showResult(value) {
      result=checkedResult(value,context);facts.replaceChildren();notes.replaceChildren();
      const roles=new Set(result.preview_chunks?["panels","frame","nodes"]:list(result.wire_preview?.instances).map(item=>roleGroup(item.role)));
      for(const key of Object.keys(GROUPS)){groupRows[key].hidden=!roles.has(key);checks[key].disabled=!roles.has(key);}
      facts.append(make("strong","",`${result.coverage?.complete===false?`${result.coverage.mounted_wall_count} из ${result.coverage.total_wall_count} сторон`:result.wall_count+" стен"} · ${result.body_count.toLocaleString("ru-RU")} деталей`));
      if(result.bounds_mm?.min&&result.bounds_mm?.max)facts.append(make("p","wm-help",result.bounds_mm.max.map((v,i)=>mm(v-result.bounds_mm.min[i])).join(" × ")+" мм"));
      const jointProof=result.checks?.joints;
      if(jointProof?.status === "valid") {
        const assembled=new Set(list(jointProof.joints).filter(item=>item.status === "valid").map(item=>item.joint_id));
        const corners=list(result.joints).filter(item=>assembled.has(item.id)&&item.kind !== "straight").length;
        const straight=list(result.joints).filter(item=>assembled.has(item.id)&&item.kind === "straight").length;
        facts.append(make("p","wm-help",`Сопряжения: углов ${corners}, прямых стыков ${straight}.`));
        notes.append(make("p","wm-help","Узлы сопряжений включены в STEP. Посадки профилей, крепежа и анкеров проверены после повторного открытия файла."));
      }
      const issues=[...list(result.warnings),...list(result.checks?.issues),...list(result.checks).filter(value=>value.status!=="valid")];
      for(const issue of issues)notes.append(make("p","ra-warning",typeof issue === "string"?issue:issue.message||issue.label||issue.code||"Проверка требует внимания."));
      const cross=result.checks?.cross_wall,collisions=list(cross?.collisions);
      if(cross?.status === "valid" && cross.complete && !collisions.length)facts.append(make("p","wm-help","Непредусмотренных пересечений: 0."));
      viewer.setConflicts?.(collisions.flatMap(item=>[item.instance_a,item.instance_b]));
      if(collisions.length){notes.append(make("p","ra-warning",`Между разными стенами обнаружены пересечения: ${collisions.length}. Затронутые детали выделены красным. Проверьте сопряжения перед использованием модели.`));const details=make("details");details.append(make("summary","","Пересечения между стенами"));for(const item of collisions.slice(0,50))details.append(make("p","wm-help",`${item.label_a || item.wall_a} ↔ ${item.label_b || item.wall_b}: ${mm(item.volume_mm3)} мм³`));notes.append(details);}
      if(cross && (!cross.complete || cross.status === "incomplete"))notes.append(make("p","ra-warning","Проверка пересечений между стенами выполнена не полностью. Это не подтверждение отсутствия конфликтов."));
      if(cross?.status === "valid" && cross.complete)notes.append(make("p","wm-help",`Пересечения разных стен проверены по точным телам STEP: ${cross.exact_pairs_checked || 0} пар после проверки габаритов.`));
      if(result.preview_chunks){loadPreview({...context},job.job_id,sequence);return;}
      try {
        const geometry=wireGeometry(result.wire_preview);viewer.setGeometry(geometry);viewer.setVisible(visible);
        if(geometry.bounded_count)notes.append(make("p","wm-help",`${geometry.bounded_count} деталей упрощены до габаритов только в просмотре. Точная геометрия сохранена в STEP.`));
      } catch(error) {viewer.setGeometry(null);empty.hidden=false;empty.textContent=error.message;}
    }
    async function loadPreview(expected,id,token) {
      previewBusy=true;previewStatus.textContent="Загружаем проверенные части модели…";
      try {
        const chunks=result.preview_chunks,ids=new Set();
        if(!Array.isArray(chunks)||!chunks.length)throw Error("В сборке нет частей просмотра.");
        for(const chunk of chunks){if(!chunk||typeof chunk.id!=="string"||!/^[A-Za-z0-9_-]{1,128}$/.test(chunk.id)||ids.has(chunk.id))throw Error("Повреждён список частей просмотра.");ids.add(chunk.id);}
        const next=createGeometryStore();store=next;viewer.setGeometry(next);viewer.setVisible(visible);
        for(const [index,chunk] of chunks.entries()){
          if(!current(expected)||sequence!==token)return;
          previewStatus.textContent=`Загружаем модель: ${index+1} из ${chunks.length} частей…`;
          const payload=await options.request(`/room-assembly/${encodeURIComponent(id)}/preview/${encodeURIComponent(chunk.id)}`);
          if(!current(expected)||sequence!==token)return;
          if(payload.id!==chunk.id||payload.floor_id!==chunk.floor_id)throw Error("Часть модели относится к другой комнате.");
          next.appendChunk({...chunk,wire_preview:payload.wire_preview});viewer.setGeometry(next,{fit:false});
        }
        if(next.body_count!==result.body_count)throw Error("Просмотр содержит не все детали сборки.");
        viewer.fit();previewStatus.textContent="Все детали загружены. Повторяющиеся профили и крепёж используют общую геометрию.";
      }catch(error){if(current(expected)&&sequence===token)previewStatus.textContent=`Просмотр загружен не полностью: ${error.message}`;}
      finally{if(sequence===token){previewBusy=false;update();}}
    }
    async function cancel(){
      if(!current()||!ACTIVE.has(job?.status))return false;
      const expected={...context},token=sequence,id=job.job_id;
      try{const value=await options.request(`/room-assembly/${encodeURIComponent(id)}/cancel`,{method:"POST",body:{expected_revision:expected.source_revision}});if(current(expected)&&sequence===token)accept(value,expected,id);return true;}
      catch(error){if(current(expected)&&sequence===token)status.textContent=error.message;return false;}
    }
    function accept(raw,expected,id) {
      job=checkedJob(raw,expected,id);status.textContent=statusMessage(job);shell.setAttribute("data-status",job.status);
      notes.replaceChildren();
      if(job.status === "ready") {
        if(!job.result)throw Error("Готовая сборка не содержит 3D-модель. Нажмите «Обновить сборку».");showResult(job.result);
        const cross=result.checks?.cross_wall,messages=[];
        if(result.coverage?.complete===false)messages.push(`Собрана часть отделки: ${result.coverage.mounted_wall_count} из ${result.coverage.total_wall_count} сторон. Остальным назначьте монтаж.`);
        if(list(cross?.collisions).length)messages.push("Есть пересечения между стенами. Конфликтные детали выделены красным, подробности — слева.");
        else if(cross && (!cross.complete || cross.status === "incomplete"))messages.push("Проверка пересечений между стенами завершена не полностью — подробности слева.");
        if(messages.length){shell.setAttribute("data-status","warning");status.textContent=messages.join(" ");}
      }
      else if(job.status === "partial" && job.result)showResult(job.result);
      for(const item of list(job.issues || job.outcomes).filter(item=>typeof item === "string" || !["ready","valid"].includes(item.status)))notes.append(make("p","ra-warning",typeof item === "string"?item:item.message||item.label||item.reason||"Стена не собрана."));
      remember(expected,job.job_id);update();return job;
    }
    async function poll(expected,id,token) {
      if(!current(expected)||sequence!==token)return;
      try {
        const data=await options.request(`/room-assembly/${encodeURIComponent(id)}`);
        if(!current(expected)||sequence!==token)return;
        const value=accept(data,expected,id);
        if(ACTIVE.has(value.status))timer=schedule(()=>poll(expected,id,token),options.pollMs ?? 2000);
      }catch(error){if(!current(expected)||sequence!==token)return;job=null;busy=false;status.textContent=error.message;update();}
    }
    function choose() {
      stopPoll();job=null;result=null;viewer.setGeometry(null);facts.replaceChildren();notes.replaceChildren();empty.hidden=false;empty.textContent="Здесь появится 3D-сборка выбранной комнаты.";
      for(const key of Object.keys(GROUPS)){groupRows[key].hidden=false;checks[key].disabled=false;}
      const value=source();context=value&&selector.value?{project_id:value.project_id,source_revision:value.source_revision,space_id:selector.value}:null;
      if(!context){status.textContent="На этом этаже ещё нет помещений. Сначала создайте комнату.";update();return;}
      status.textContent=`Сохранённая версия r${context.source_revision}. Сборка использует монтаж всех сторон стен этой комнаты.`;
      const id=loadId(context);if(id){status.textContent="Проверяем предыдущую сборку комнаты…";poll({...context},id,sequence);}update();
    }
    selector.addEventListener("change",choose);
    async function start() {
      if(!current()||busy||ACTIVE.has(job?.status))return false;
      const expected={...context},token=++sequence;busy=true;job=null;result=null;viewer.setGeometry(null);facts.replaceChildren();notes.replaceChildren();empty.hidden=false;empty.textContent="Собираем стены в единую комнату…";status.textContent="Запускаем фоновую сборку комнаты…";update();
      try {
        const data=await options.request("/room-assembly/start",{method:"POST",body:{project_id:expected.project_id,expected_revision:expected.source_revision,space_id:expected.space_id}});
        // A job continues on the server after closing this window. Remember its
        // identity without rendering it into a different room or draft.
        const started=checkedJob(data,expected);if(alive)remember(expected,started.job_id);
        if(!current(expected)||sequence!==token)return false;
        busy=false;const value=accept(data,expected);
        if(ACTIVE.has(value.status))timer=schedule(()=>poll(expected,value.job_id,token),options.pollMs ?? 2000);
        return true;
      }catch(error){if(current(expected)&&sequence===token){status.textContent=error.message;job=null;empty.textContent="Сборка пока не готова. Причина указана над моделью.";}return false;}
      finally{if(sequence===token){busy=false;update();}}
    }
    async function download() {
      if(!current()||busy||downloading||!result||job?.status!=="ready")return false;
      const expected={...context},id=job.job_id,token=sequence,snapshot=result;downloading=true;update();
      try {
        if(options.downloadUrl){options.downloadUrl(`/api/v1/room-assembly/${encodeURIComponent(id)}/export.step`);return true;}
        const blob=await options.requestBlob(`/room-assembly/${encodeURIComponent(id)}/export.step`);
        if(!current(expected)||sequence!==token||result!==snapshot)return false;
        options.download(blob,`${String(snapshot.label||"Комната").replace(/[^a-zA-Zа-яА-ЯёЁ0-9_-]+/g,"_").slice(0,80)}_r${expected.source_revision}_отделка.step`);return true;
      }catch(error){if(current(expected)&&sequence===token)status.textContent=error.message;return false;}
      finally{if(sequence===token){downloading=false;update();}}
    }
    function open({floor_id,space_id}={}) {
      if(!alive||!source())return false;stopPoll();opened=true;selector.replaceChildren();
      const available=rooms(options.getProject(),floor_id);for(const room of available){const option=make("option","",room.label);option.value=room.id;selector.append(option);}
      selector.value=available.some(room=>room.id===space_id)?space_id:available[0]?.id||"";selector.disabled=!available.length;
      heading.textContent="3D комнаты";choose();viewer.resize?.();return true;
    }
    function close() {if(!opened)return true;stopPoll();opened=false;downloading=false;viewer.setGeometry(null);options.onClose?.();return true;}
    function refresh() {if(!opened)return;if(!current()){stopPoll();job=null;result=null;viewer.setGeometry(null);status.textContent="Проект изменился. Закройте это окно, сохраните правки и откройте 3D комнаты заново.";empty.hidden=false;empty.textContent="Предыдущая модель скрыта, чтобы не показывать устаревший монтаж.";}update();}
    return {open,close,start,cancel,download,refresh,setVisible,hasDraft:()=>false,dispose(){close();alive=false;viewer.dispose();host.replaceChildren();}};
  }
  return {ACTIVE,GROUPS,DEFAULT_VISIBLE,ONLY_NODES,roleGroup,rooms,sameContext,checkedResult,checkedJob,statusMessage,wireGeometry,createGeometryStore,projectPoint,createViewer,create};
});
