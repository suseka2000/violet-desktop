(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports ? require("./model-editor.js") : root.VioletModel);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletCadMapping = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (M) {
  "use strict";
  const kinds = {room:"Помещение", wall:"Стена", door:"Дверь", window:"Окно", passage:"Проход", opening:"Проём", ignore:"Не учитывать", note:"Примечание"};
  function distance(p, a, b) {
    const dx=b.x-a.x, dy=b.y-a.y, length=dx*dx+dy*dy;
    const t=length ? Math.max(0,Math.min(1,((p.x-a.x)*dx+(p.y-a.y)*dy)/length)) : 0;
    return Math.hypot(p.x-a.x-t*dx,p.y-a.y-t*dy);
  }
  function hitEntity(entities, point, tolerance, layer="") {
    let best=null, minimum=tolerance;
    for (const entity of entities) {
      if (layer && entity.layer !== layer) continue;
      for (const segment of entity.segments || []) {
        const d=distance(point,segment.start,segment.end);
        if (d < minimum || (!best && d === minimum)) { minimum=d; best=entity; }
      }
    }
    return best;
  }
  function snapPoint(entities, marks, point, tolerance, layer="") {
    const segments=entities.filter(e=>e.exact_geometry && (!layer || e.layer===layer)).flatMap(e=>e.segments || []);
    for(const mark of marks) if(["wall","room"].includes(mark.kind) && mark.exact_geometry !== false) {
      mark.points.slice(1).forEach((b,i)=>segments.push({start:mark.points[i],end:b}));
      if(mark.kind === "room" || mark.closed) segments.push({start:mark.points.at(-1),end:mark.points[0]});
    }
    let endpoint=null,endpointDistance=tolerance,projection=null,projectionDistance=tolerance;
    for(const s of segments) {
      for(const p of [s.start,s.end]) {const d=Math.hypot(p.x-point.x,p.y-point.y);if(d<=endpointDistance){endpoint={...p};endpointDistance=d;}}
      const dx=s.end.x-s.start.x,dy=s.end.y-s.start.y,den=dx*dx+dy*dy;if(!den) continue;
      const t=Math.max(0,Math.min(1,((point.x-s.start.x)*dx+(point.y-s.start.y)*dy)/den)),p={x:s.start.x+t*dx,y:s.start.y+t*dy},d=Math.hypot(point.x-p.x,point.y-p.y);
      if(d<=projectionDistance){projection=p;projectionDistance=d;}
    }
    return {point:endpoint || projection || {...point},snapped:Boolean(endpoint || projection)};
  }
  function markEntity(entity, kind, options={}) {
    if (!kinds[kind]) throw new Error("Неизвестный тип объекта.");
    let points=(entity.points || []).map(p=>({...p}));
    const exact=entity.exact_geometry === true;
    if (!points.length) points=(entity.segments || []).flatMap(s=>[{...s.start},{...s.end}]).slice(0,2000);
    if (!points.length) throw new Error("У объекта нет доступной геометрии. Обведите его вручную.");
    if (points.length > 2000) throw new Error("Слишком сложный объект. Разметьте нужный участок вручную.");
    if(kind === "room" && points.length<3) throw new Error("Помещению нужен замкнутый контур минимум из трёх точек. Обведите границу вручную или выберите замкнутую полилинию.");
    return {id:M.id(),kind,label:options.label || `${kinds[kind]} · ${entity.block_name || entity.layer || entity.handle}`,
      points,note:exact ? "" : "Условная/кривая геометрия: перед созданием обведите точную ось стены или две границы проёма.",
      cad_handles:[entity.handle], exact_geometry:exact && (kind !== "room" || entity.room_supported === true),
      closed:entity.closed === true, height_mm:options.height_mm ?? 2100,sill_mm:options.sill_mm ?? 0,model_ids:[]};
  }
  /** Join explicitly selected straight edges, never infer hidden wall semantics. */
  function markConnectedEntities(entities, kind, options={}) {
    if(!["room","wall"].includes(kind)) throw new Error("Соединение линий доступно для контура помещения или стены. Для проёма укажите две границы вручную.");
    if(!Array.isArray(entities) || !entities.length || entities.some(e=>e.exact_geometry !== true)) throw new Error("Выберите прямые линии или точные полилинии. Кривые и условные блоки обведите вручную.");
    const tolerance=options.tolerance ?? 1e-6;
    if(!Number.isFinite(tolerance) || tolerance<=0 || tolerance>1) throw new Error("Недопустимый допуск соединения линий.");
    const segments=entities.flatMap(e=>e.segments || []);
    if(!segments.length || segments.length>2000 || entities.length>1000) throw new Error("Выберите от 1 до 2000 отрезков, не более 1000 объектов.");
    const vertices=[], edges=[], seen=new Set();
    const vertex=p=>{
      if(!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) throw new Error("Некорректные координаты линии.");
      const existing=vertices.findIndex(v=>Math.hypot(v.point.x-p.x,v.point.y-p.y)<=tolerance);
      if(existing>=0) return existing;
      vertices.push({point:{...p},edges:[]});return vertices.length-1;
    };
    for(const segment of segments) {
      const a=vertex(segment.start),b=vertex(segment.end); if(a===b) continue;
      const key=[Math.min(a,b),Math.max(a,b)].join(":");if(seen.has(key)) continue;seen.add(key);
      vertices[a].edges.push(edges.length);vertices[b].edges.push(edges.length);edges.push([a,b]);
    }
    const active=vertices.map((v,i)=>i).filter(i=>vertices[i].edges.length),ends=active.filter(i=>vertices[i].edges.length===1);
    if(!edges.length || active.some(i=>vertices[i].edges.length>2) || ![0,2].includes(ends.length)) throw new Error("Линии ветвятся или не образуют одну цепочку. Уберите лишние линии либо обведите контур вручную.");
    const closed=ends.length===0;
    if(kind === "room" && !closed) throw new Error("Контур помещения не замкнут. Выберите недостающие линии либо обведите границу вручную.");
    let current=ends[0] ?? active[0]; const initial=current, visited=new Set(), points=[vertices[current].point];
    while(visited.size<edges.length) {
      const edge=vertices[current].edges.find(i=>!visited.has(i)); if(edge===undefined) break;
      visited.add(edge);current=edges[edge][0]===current ? edges[edge][1] : edges[edge][0];
      if(current !== initial) points.push(vertices[current].point);
    }
    if(visited.size !== edges.length || (closed && current !== initial)) throw new Error("Выбраны отдельные несвязанные контуры. Размечайте каждый контур отдельно.");
    if(kind === "room" && points.length<3) throw new Error("Для помещения нужен контур минимум из трёх вершин.");
    const handles=[...new Set(entities.map(e=>e.handle).filter(Boolean))];
    return {id:M.id(),kind,label:options.label || (kind === "room" ? "Помещение по выбранным линиям" : "Стена по выбранным линиям"),
      points,note:`Соединено выбранных объектов: ${entities.length}; допуск ${tolerance} в единицах исходника.`,cad_handles:handles,
      exact_geometry:true,closed,height_mm:options.height_mm ?? 2100,sill_mm:options.sill_mm ?? 0,model_ids:[]};
  }
  function toModel(point, source) {
    if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error("Координаты должны быть конечными числами.");
    if(source.requires_calibration && !source.calibration) throw new Error("Для этого листа сначала подтвердите масштаб калибровкой A–B. Единицы файла не задают масштаб всех его видов.");
    if (source.coordinate_space === "dxf_world_mm" && !source.calibration) return {...point};
    if (!source.calibration) throw new Error("Сначала задайте единицы CAD-файла или откалибруйте исходник по известному расстоянию.");
    const c=source.calibration, dx=c.source_b.x-c.source_a.x, dy=c.source_b.y-c.source_a.y, den=dx*dx+dy*dy;
    if (![c.source_a.x,c.source_a.y,c.source_b.x,c.source_b.y,c.origin_mm.x,c.origin_mm.y,c.distance_mm,den].every(Number.isFinite) || !(den > 1e-12) || !(c.distance_mm > 0)) throw new Error("Некорректная калибровка.");
    const px=point.x-c.source_a.x, py=point.y-c.source_a.y, factor=c.distance_mm/den;
    const result={x:c.origin_mm.x+(px*dx+py*dy)*factor,y:c.origin_mm.y+(-px*dy+py*dx)*factor*(c.y_axis_down ? -1 : 1)};
    if(!Number.isFinite(result.x) || !Number.isFinite(result.y)) throw new Error("Координаты калибровки выходят за допустимые пределы.");
    return result;
  }
  function cleanPoints(points) {
    const clean=points.filter((p,i)=>!i || Math.hypot(p.x-points[i-1].x,p.y-points[i-1].y)>1e-6);
    if (clean.length > 2 && Math.hypot(clean[0].x-clean.at(-1).x,clean[0].y-clean.at(-1).y)<1e-6) clean.pop();
    return clean;
  }
  function openingHost(floor, a, b, tolerance) {
    if (!(Math.hypot(b.x-a.x,b.y-a.y)>tolerance)) throw new Error("Границы проёма должны различаться.");
    const matches=(floor.walls || []).filter(w=>distance(a,w.start,w.end)<=tolerance && distance(b,w.start,w.end)<=tolerance);
    if (matches.length !== 1) throw new Error(matches.length ? "Несколько стен подходят для проёма. Уточните его границы." : "Обе точки проёма должны лежать на одной стене. Сначала разметьте стену, затем две границы проёма на её оси.");
    return matches[0];
  }
  /** Build on a clone. Neither annotations nor geometry mutate after any failed item. */
  function buildMarkup(project, floorId, source, options={}) {
    for(const key of ["wall_height_mm","wall_thickness_mm"]) if(options[key] !== undefined && (!Number.isFinite(options[key]) || options[key]<=0)) throw new Error("Высота и толщина стен должны быть положительными конечными числами.");
    const work=M.clone(project), floor=M.findFloor(work,floorId)?.floor;
    if (!floor) throw new Error("Выберите этаж для создания объектов.");
    const attachment=floor.properties?.source_markup?.sources?.find(s=>s.source_id===source.source_id && s.page===source.page);
    if (!attachment) throw new Error("Разметка исходника не приложена к этажу.");
    const pending=attachment.marks.filter(m=>!m.model_ids?.length && !["ignore","note"].includes(m.kind));
    if (!pending.length) throw new Error("Нет новой разметки для создания. Назначьте тип объектам или обведите их вручную.");
    if(pending.reduce((sum,mark)=>sum+mark.points.length,0)>2000) throw new Error("За один раз можно создать не более 2000 исходных вершин. Разделите импорт на меньшие участки.");
    if (pending.some(m=>m.exact_geometry === false)) throw new Error("Есть условные блоки или кривые. Они классифицированы, но для создания модели нужна точная ручная обводка. Пометьте условный блок «Не учитывать», затем обведите нужную геометрию.");
    const tolerance=Math.max(1e-6,Number(work.settings?.geometry_tolerance_mm || .1));
    const markKey=(attached,mark)=>JSON.stringify([attached.source_id,attached.page,mark.id]);
    const wallInterval=wall=>({root:wall.properties?.split_origin_wall_id || wall.id,
      from:Number(wall.properties?.split_origin_offset_mm || 0),
      to:Number(wall.properties?.split_origin_offset_mm || 0)+M.wallLength(wall)});
    const previousWallIds=new Set(floor.walls.map(wall=>wall.id)),builtWallLinks=new Map();
    // Built is historical state, not a geometric coincidence with the source.
    // Snapshot current linked intervals before any splits: edited/moved walls
    // keep their links, and deleted targets never silently become pending again.
    for(const attached of floor.properties.source_markup.sources) for(const mark of attached.marks) {
      if(mark.kind !== "wall" || !mark.model_ids?.length) continue;
      builtWallLinks.set(markKey(attached,mark),{ids:[...mark.model_ids],intervals:mark.model_ids
        .map(id=>floor.walls.find(wall=>wall.id===id)).filter(Boolean).map(wallInterval)});
    }
    const order={room:0,wall:1,door:2,window:2,passage:2,opening:2};
    for (const mark of [...pending].sort((a,b)=>order[a.kind]-order[b.kind])) {
      const points=cleanPoints(mark.points.map(p=>toModel(p,attachment))); let ids=[];
      try {
        if (mark.kind === "room") {
          const room=M.createPolygonRoom(work,floorId,{code:String(floor.spaces.length+1).padStart(3,"0"),name:mark.label,height_mm:options.wall_height_mm || 3000,thickness_mm:options.wall_thickness_mm || 150},points);
          ids=[room.id];
        } else if (mark.kind === "wall") {
          if (points.length<2) throw new Error("Стена требует минимум две точки.");
          const segments=points.slice(1).map((b,i)=>[points[i],b]); if (mark.closed && points.length>2) segments.push([points.at(-1),points[0]]);
          for (const [a,b] of segments) {
            const current=M.findFloor(work,floorId).floor;
            const existing=current.walls.find(w=>(distance(a,w.start,w.end)<=tolerance && distance(b,w.start,w.end)<=tolerance && Math.abs(M.wallLength(w)-Math.hypot(b.x-a.x,b.y-a.y))<=tolerance));
            const wall=existing || M.createPartition(work,floorId,a,b,{name:mark.label,height_mm:options.wall_height_mm || 3000,thickness_mm:options.wall_thickness_mm || 150,snap_tolerance_mm:tolerance}); ids.push(wall.id);
          }
        } else {
          if (points.length!==2) throw new Error("Для проёма нужны ровно две точки на оси стены — его левая и правая границы. Блок двери с дугой нужно обвести вручную.");
          const current=M.findFloor(work,floorId).floor, wall=openingHost(current,points[0],points[1],tolerance);
          const width=Math.hypot(points[1].x-points[0].x,points[1].y-points[0].y), center={x:(points[0].x+points[1].x)/2,y:(points[0].y+points[1].y)/2};
          const opening=M.createOpeningAt(work,floorId,wall.id,center,{kind:mark.kind === "opening" ? "passage" : mark.kind,name:mark.label,width_mm:width,height_mm:mark.height_mm ?? 2100,sill_mm:mark.sill_mm ?? 0}); ids=[opening.id];
        }
      } catch (error) { throw new Error(`${mark.label}: ${error.message}`); }
      // Model operations may replace the floor atomically, so resolve annotations again.
      const currentSource=M.findFloor(work,floorId).floor.properties.source_markup.sources.find(s=>s.source_id===source.source_id && s.page===source.page);
      currentSource.marks.find(m=>m.id===mark.id).model_ids=ids;
    }
    const split=M.splitWallsAtIntersections(work,floorId);
    const finalFloor=M.findFloor(work,floorId).floor;
    // Existing marks follow only new descendants of their linked intervals.
    // New marks use source extent to avoid claiming a sibling of a reused part.
    for(const attached of finalFloor.properties.source_markup.sources) for(const mark of attached.marks) {
      if(mark.kind !== "wall" || !mark.model_ids?.length) continue;
      const previous=builtWallLinks.get(markKey(attached,mark));
      if(previous) {
        const descendants=finalFloor.walls.filter(wall=>{
          if(previousWallIds.has(wall.id)) return false;
          const interval=wallInterval(wall);
          return previous.intervals.some(before=>before.root===interval.root
            && interval.from>=before.from-tolerance && interval.to<=before.to+tolerance);
        }).map(wall=>wall.id);
        mark.model_ids=[...new Set([...previous.ids,...descendants])];
        continue;
      }
      const origins=new Set(mark.model_ids.map(id=>finalFloor.walls.find(w=>w.id===id)?.properties?.split_origin_wall_id || id));
      const points=cleanPoints(mark.points.map(point=>toModel(point,attached)));
      const segments=points.slice(1).map((end,index)=>({start:points[index],end}));
      if(mark.closed && points.length>2) segments.push({start:points.at(-1),end:points[0]});
      const descendants=finalFloor.walls.filter(w=>origins.has(w.properties?.split_origin_wall_id || w.id)
        && segments.some(segment=>distance(w.start,segment.start,segment.end)<=tolerance
          && distance(w.end,segment.start,segment.end)<=tolerance)).map(w=>w.id);
      mark.model_ids=[...new Set([...mark.model_ids,...descendants])];
    }
    const pendingIds=new Set(pending.map(mark=>mark.id));
    const finalAttachment=finalFloor.properties.source_markup.sources.find(attached=>attached.source_id===source.source_id && attached.page===source.page);
    const modelIds=[...new Set(finalAttachment.marks.filter(mark=>pendingIds.has(mark.id)).flatMap(mark=>mark.model_ids || []))];
    Object.keys(project).forEach(key=>delete project[key]); Object.assign(project,work);
    return {created_count:modelIds.length,model_ids:modelIds,split};
  }
  return {kinds,hitEntity,snapPoint,markEntity,markConnectedEntities,toModel,openingHost,buildMarkup};
});
