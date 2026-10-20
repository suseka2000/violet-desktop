(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.VioletDrafting = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Interaction decisions are independent of the DOM and of the canonical
  // project. Callers perform exactly one mutation on `commit` / `close`.
  function point(value, name = "Точка") {
    if (!value || !Number.isFinite(value.x) || !Number.isFinite(value.y)) {
      throw new TypeError(`${name} должна содержать конечные x и y.`);
    }
    return { x: value.x, y: value.y };
  }

  function nonnegative(value, name) {
    if (!Number.isFinite(value) || value < 0) throw new RangeError(`${name} должен быть конечным неотрицательным числом.`);
    return value;
  }

  function shape(kind) {
    if (kind !== "rectangle" && kind !== "line") throw new TypeError("Вид построения должен быть rectangle или line.");
    return kind;
  }

  function distance(first, second) {
    return Math.hypot(first.x - second.x, first.y - second.y);
  }

  function twoPointDown(phase) {
    if (phase === "awaitingFirst") return "begin";
    // A finished preview is never discarded by the following click.
    if (phase === "awaitingSecond" || phase === "ready") return "finish";
    if (phase === "dragging") return "ignore";
    throw new TypeError(`Неизвестный этап построения: ${phase}.`);
  }

  function rectangleHasSize(draft) {
    return Boolean(draft && Number.isFinite(draft.width_mm) && draft.width_mm > 0 && Number.isFinite(draft.depth_mm) && draft.depth_mm > 0);
  }

  function rectangleSizeInput(draft, width, depth) {
    const anchored = draft.phase !== "awaitingFirst";
    // Zero is the initial, free-drawing state. A partially entered size is a
    // preview, never an instruction to create a zero-width room.
    draft.width_mm = width; draft.depth_mm = depth;
    draft.mode = width === 0 && depth === 0 && !anchored ? "two_points" : "dimensions";
    draft.b = { x: draft.a.x + (draft.x_sign || 1) * width, y: draft.a.y + (draft.y_sign || 1) * depth };
    draft.preview = rectangleHasSize(draft);
    if (anchored) draft.phase = "awaitingSecond";
    return draft;
  }

  function repeatRectangle(draft) {
    const dimensions = draft.mode === "dimensions";
    return { ...draft, mode: dimensions ? "dimensions" : "two_points", width_mm: dimensions ? draft.width_mm : 0, depth_mm: dimensions ? draft.depth_mm : 0,
      a: { ...draft.a }, b: dimensions ? { ...draft.b } : { ...draft.a }, phase: "awaitingFirst", preview: false, x_sign: 1, y_sign: 1 };
  }

  function validateTwoPoint(startValue, endValue, kind, epsilonMm = 1e-6) {
    const start = point(startValue), end = point(endValue);
    shape(kind); nonnegative(epsilonMm, "Минимальный размер");
    const width = Math.abs(end.x - start.x), depth = Math.abs(end.y - start.y);
    if (!Number.isFinite(width) || !Number.isFinite(depth)) {
      return { valid: false, reason: "Размер построения выходит за допустимый диапазон." };
    }
    if (kind === "rectangle") {
      if (!(width > epsilonMm && depth > epsilonMm)) {
        return { valid: false, reason: "Укажите противоположный угол: ширина и глубина должны быть больше нуля." };
      }
    } else if (!(Math.hypot(width, depth) > epsilonMm)) {
      return { valid: false, reason: "Укажите вторую точку, отличную от первой." };
    }
    return { valid: true, reason: "" };
  }

  function twoPointUp(options) {
    const { start, end, kind, clientStart, clientEnd, dragThresholdPx = 3, epsilonMm = 1e-6 } = options;
    const from = point(clientStart, "Начальная позиция курсора");
    const to = point(clientEnd, "Конечная позиция курсора");
    nonnegative(dragThresholdPx, "Порог перетаскивания");
    const validity = validateTwoPoint(start, end, kind, epsilonMm);
    // A stationary first click starts the conventional two-click workflow,
    // even when object snapping happened to separate the model coordinates.
    if (distance(from, to) < dragThresholdPx) {
      return { action: "awaitingSecond", phase: "awaitingSecond", reason: "" };
    }
    if (!validity.valid) return { action: "reject", phase: "awaitingSecond", reason: validity.reason };
    return { action: "commit", phase: "ready", reason: "" };
  }

  function polygonPointAction(values, value, options = {}) {
    if (!Array.isArray(values)) throw new TypeError("Вершины контура должны быть массивом.");
    const vertices = values.map((item) => point(item));
    const candidate = point(value);
    const closeRadius = nonnegative(options.closeRadiusMm ?? 0, "Радиус замыкания");
    const duplicateRadius = nonnegative(options.duplicateRadiusMm ?? 1e-6, "Допуск повторной точки");
    const last = vertices.at(-1);
    // Ignore a double click's repeated final vertex. No zero-length edge is
    // ever appended and the input array remains untouched.
    if (last && distance(candidate, last) <= duplicateRadius) {
      return { action: "ignore", point: candidate, reason: "Эта вершина уже добавлена." };
    }
    if (vertices.length >= 3 && distance(candidate, vertices[0]) <= closeRadius) {
      return { action: "close", point: { ...vertices[0] }, reason: "" };
    }
    if (vertices.length > 1 && distance(candidate, vertices[0]) <= duplicateRadius) {
      return { action: "ignore", point: candidate, reason: "Для замкнутого контура нужны минимум три вершины." };
    }
    return { action: "append", point: candidate, reason: "" };
  }

  function constrainEnd(startValue, endValue, options = {}) {
    const start = point(startValue), end = point(endValue);
    const kind = shape(options.kind ?? "line");
    if (!options.orthogonal) return end;
    const dx = end.x - start.x, dy = end.y - start.y;
    if (kind === "rectangle") {
      const side = Math.max(Math.abs(dx), Math.abs(dy));
      return { x: start.x + (dx < 0 ? -side : side), y: start.y + (dy < 0 ? -side : side) };
    }
    return Math.abs(dx) >= Math.abs(dy) ? { x: end.x, y: start.y } : { x: start.x, y: end.y };
  }

  // These transform local SVG coordinates, not client pixels. A caller must
  // still use its screen CTM (including zoom/pan) before svgToModel.
  function modelToSvg(value) {
    const result = point(value);
    return { x: result.x === 0 ? 0 : result.x, y: result.y === 0 ? 0 : -result.y };
  }

  const svgToModel = modelToSvg;

  function gridPoint(value, stepMm) {
    const p = point(value); if (!(stepMm > 0) || !Number.isFinite(stepMm)) throw new RangeError("Шаг сетки должен быть положительным.");
    const round=value=>value<0?-Math.round(-value):Math.round(value);
    return { x: round(p.x / stepMm) * stepMm, y: round(p.y / stepMm) * stepMm };
  }

  // preserveAspectRatio=xMidYMid meet expands the visible world beyond the
  // declared viewBox. Backgrounds must cover this expansion, also during pan.
  function visibleModelBounds(view, widthPx, heightPx) {
    if (!(view.w > 0 && view.h > 0)) throw new RangeError("Размер вида должен быть положительным.");
    const ratio = widthPx > 0 && heightPx > 0 ? widthPx / heightPx : view.w / view.h;
    const w = Math.max(view.w, view.h * ratio), h = Math.max(view.h, view.w / ratio);
    return { x: view.x - (w - view.w) / 2, y: view.y - (h - view.h) / 2, w, h };
  }

  function lineData(value) {
    const start = point(value.start), end = point(value.end);
    const length = distance(start, end); if (!(length > 1e-8)) throw new RangeError("Опорная линия должна иметь длину.");
    return { start, end, length, ux: (end.x-start.x)/length, uy: (end.y-start.y)/length };
  }
  function projectOnLine(line, value, finite = false) {
    const d = lineData(line), p = point(value);
    let along = (p.x-d.start.x)*d.ux + (p.y-d.start.y)*d.uy;
    if (finite) along = Math.max(0, Math.min(d.length, along));
    const projected = { x:d.start.x+along*d.ux, y:d.start.y+along*d.uy };
    return { point:projected, along, distance:distance(p, projected), signedOffset:-(p.x-d.start.x)*d.uy+(p.y-d.start.y)*d.ux };
  }
  function openingAnchor(wall, value, widthMm, stepMm, useGrid) {
    const d = lineData(wall), projected = projectOnLine(wall, value);
    if (!(widthMm > 0) || widthMm > d.length) throw new RangeError("Ширина проёма должна быть положительной и не превышать стену.");
    const rawOffset = projected.along - widthMm / 2;
    if (useGrid && (!(stepMm > 0) || !Number.isFinite(stepMm))) throw new RangeError("Шаг сетки должен быть положительным.");
    const offset = Math.max(0, Math.min(d.length-widthMm, useGrid ? Math.round(rawOffset/stepMm)*stepMm : rawOffset));
    return { point:{x:d.start.x+(offset+widthMm/2)*d.ux,y:d.start.y+(offset+widthMm/2)*d.uy}, offset_mm:offset, snapped:Boolean(useGrid), clamped:Math.abs(offset-rawOffset)>1e-8 };
  }
  function guideLine(mode, value, references = [], branch = "auto") {
    const p = point(value); let origin = p, dx, dy;
    if (mode === "vertical") { dx = 0; dy = 1; }
    else if (mode === "horizontal") { dx = 1; dy = 0; }
    else if (mode === "parallel" || mode === "perpendicular") {
      const d = lineData(references[0]); dx = mode === "parallel" ? d.ux : -d.uy; dy = mode === "parallel" ? d.uy : d.ux;
    } else if (mode === "bisector") {
      const a = lineData(references[0]), b = lineData(references[1]);
      const cross = a.ux*b.uy-a.uy*b.ux;
      if (Math.abs(cross) < 1e-8) {
        const offset=projectOnLine(a,b.start).signedOffset;
        if(Math.abs(offset)<1e-6)throw new RangeError("Опорные линии совпадают. Средняя линия не определена; выберите две разные линии.");
        origin={x:a.start.x-a.uy*offset/2,y:a.start.y+a.ux*offset/2};dx=a.ux;dy=a.uy;
        return {start:origin,end:{x:origin.x+dx*1000,y:origin.y+dy*1000}};
      }
      const qx = b.start.x-a.start.x, qy = b.start.y-a.start.y, t = (qx*b.uy-qy*b.ux)/cross;
      origin = {x:a.start.x+t*a.ux,y:a.start.y+t*a.uy};
      const candidates = [{x:a.ux+b.ux,y:a.uy+b.uy},{x:a.ux-b.ux,y:a.uy-b.uy}].map(v=>{const l=Math.hypot(v.x,v.y);return {x:v.x/l,y:v.y/l};});
      const distances = candidates.map(v=>Math.abs((p.x-origin.x)*v.y-(p.y-origin.y)*v.x));
      const chosen = branch === "1" ? 0 : branch === "2" ? 1 : distances[0] <= distances[1] ? 0 : 1;
      dx = candidates[chosen].x; dy = candidates[chosen].y;
    } else throw new TypeError("Неизвестный режим вспомогательной линии.");
    return { start:{...origin}, end:{x:origin.x+dx*1000,y:origin.y+dy*1000} };
  }
  function hasConstruction(state) {
    return Boolean(state.polygon?.points?.length || [state.rectangle,state.partitionDraft,state.measureDraft].some(d=>d && d.phase!=="awaitingFirst") || (state.guideDraft && (state.guideDraft.references?.length || ["dragging","awaitingSecond","ready"].includes(state.guideDraft.phase))));
  }

  // Presentation geometry only: the room's centreline dimensions and the
  // canonical wall endpoints never move. All pieces have the same winding,
  // so a nonzero SVG fill is their union without seams between overlapping
  // strips. The exposed edges provide actual face corners for object snaps.
  const wallPresentationCache = new WeakMap();
  const GEOMETRY_EPSILON = 1e-7;
  function cross2(a,b){return a.x*b.y-a.y*b.x;}
  function minus(a,b){return{x:a.x-b.x,y:a.y-b.y};}
  function lerp(a,b,t){return{x:a.x+(b.x-a.x)*t,y:a.y+(b.y-a.y)*t};}
  function polygonArea(vertices){let area=0;const origin=vertices[0];for(let i=1;i+1<vertices.length;i++)area+=cross2(minus(vertices[i],origin),minus(vertices[i+1],origin));return area/2;}
  function polygonBounds(vertices){return{minX:Math.min(...vertices.map(p=>p.x)),maxX:Math.max(...vertices.map(p=>p.x)),minY:Math.min(...vertices.map(p=>p.y)),maxY:Math.max(...vertices.map(p=>p.y))};}
  function boundsOverlap(a,b){return a.minX<=b.maxX+GEOMETRY_EPSILON&&a.maxX>=b.minX-GEOMETRY_EPSILON&&a.minY<=b.maxY+GEOMETRY_EPSILON&&a.maxY>=b.minY-GEOMETRY_EPSILON;}
  function containsPolygon(vertices,p){let inside=false;for(let i=0,j=vertices.length-1;i<vertices.length;j=i++){const a=vertices[i],b=vertices[j];if((a.y>p.y)!==(b.y>p.y)&&p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x)inside=!inside;}return inside;}
  function wallStrip(a,b,half){const length=distance(a,b);if(length<GEOMETRY_EPSILON)return null;const nx=-(b.y-a.y)/length*half,ny=(b.x-a.x)/length*half;return[{x:a.x+nx,y:a.y+ny},{x:a.x-nx,y:a.y-ny},{x:b.x-nx,y:b.y-ny},{x:b.x+nx,y:b.y+ny}];}
  function halfPlane(vertices,a,b,inside){
    const result=[],direction=minus(b,a),signed=p=>cross2(direction,minus(p,a));
    for(let i=0;i<vertices.length;i++){const p=vertices[i],q=vertices[(i+1)%vertices.length],sp=signed(p),sq=signed(q),pin=inside?sp>=0:sp<=0,qin=inside?sq>=0:sq<=0;if(pin)result.push(p);if(pin!==qin)result.push(lerp(p,q,sp/(sp-sq)));}
    return result;
  }
  function subtractConvex(vertices,cutter){
    let remainder=vertices;const outside=[];
    for(let i=0;i<cutter.length&&remainder.length>=3;i++){const a=cutter[i],b=cutter[(i+1)%cutter.length],part=halfPlane(remainder,a,b,false);if(part.length>=3&&Math.abs(polygonArea(part))>GEOMETRY_EPSILON)outside.push(part);remainder=halfPlane(remainder,a,b,true);}
    return outside;
  }
  function segmentBreaks(a,b,c,d){
    const u=minus(b,a),v=minus(d,c),delta=minus(c,a),den=cross2(u,v),length2=u.x*u.x+u.y*u.y;
    if(Math.abs(den)>GEOMETRY_EPSILON){const t=cross2(delta,v)/den,s=cross2(delta,u)/den;return t>=-1e-9&&t<=1+1e-9&&s>=-1e-9&&s<=1+1e-9?[Math.max(0,Math.min(1,t))]:[];}
    if(Math.abs(cross2(delta,u))>GEOMETRY_EPSILON*Math.max(1,Math.sqrt(length2)))return[];
    return[c,d].map(p=>((p.x-a.x)*u.x+(p.y-a.y)*u.y)/length2).filter(t=>t>0&&t<1);
  }
  function simplePolygon(vertices){
    for(let i=0;i<vertices.length;i++)for(let j=i+2;j<vertices.length;j++){
      if(i===0&&j===vertices.length-1)continue;
      const a=vertices[i],b=vertices[(i+1)%vertices.length],c=vertices[j],d=vertices[(j+1)%vertices.length];
      if(segmentBreaks(a,b,c,d).some(t=>t>1e-9&&t<1-1e-9)||segmentBreaks(c,d,a,b).some(t=>t>1e-9&&t<1-1e-9))return false;
    }
    return true;
  }
  function exposedWallEdges(pieces){
    const unique=new Map(),key=p=>`${Math.round(p.x*1e6)},${Math.round(p.y*1e6)}`;
    for(const piece of pieces)for(let i=0;i<piece.points.length;i++){
      const a=piece.points[i],b=piece.points[(i+1)%piece.points.length],edgeBox=polygonBounds([a,b]),length=distance(a,b);if(length<GEOMETRY_EPSILON)continue;
      const neighbours=pieces.filter(other=>other!==piece&&boundsOverlap(edgeBox,other.bounds)),cuts=[0,1];
      for(const other of neighbours)for(let j=0;j<other.points.length;j++)cuts.push(...segmentBreaks(a,b,other.points[j],other.points[(j+1)%other.points.length]));
      cuts.sort((x,y)=>x-y);const nx=-(b.y-a.y)/length,ny=(b.x-a.x)/length;
      for(let j=1;j<cuts.length;j++){
        const lo=cuts[j-1],hi=cuts[j];if((hi-lo)*length<GEOMETRY_EPSILON)continue;
        const mid=lerp(a,b,(lo+hi)/2),epsilon=Math.min(.001,(hi-lo)*length/100),left={x:mid.x+nx*epsilon,y:mid.y+ny*epsilon},right={x:mid.x-nx*epsilon,y:mid.y-ny*epsilon};
        const nearby=[piece,...neighbours],isInside=p=>nearby.some(other=>containsPolygon(other.points,p));
        if(isInside(left)===isInside(right))continue;
        const start=lerp(a,b,lo),end=lerp(a,b,hi),id=[key(start),key(end)].sort().join("/");
        const existing=unique.get(id);if(existing)existing.owner_ids=[...new Set([...existing.owner_ids,...piece.owner_ids])].sort();else unique.set(id,{start,end,owner_ids:[...piece.owner_ids]});
      }
    }
    return [...unique.values()];
  }
  function wallPresentation(floor){
    const rawWalls=Array.isArray(floor?.walls)?floor.walls:[],openings=Array.isArray(floor?.openings)?floor.openings:[];
    const signature=JSON.stringify([rawWalls.map(w=>[w.id,w.start?.x,w.start?.y,w.end?.x,w.end?.y,w.thickness_mm]),openings.map(o=>[o.id,o.wall_id,o.offset_mm,o.width_mm])]);
    const cached=floor&&wallPresentationCache.get(floor);if(cached?.signature===signature)return cached.result;
    const walls=rawWalls.filter(w=>Number.isFinite(w.start?.x)&&Number.isFinite(w.start?.y)&&Number.isFinite(w.end?.x)&&Number.isFinite(w.end?.y)&&distance(w.start,w.end)>GEOMETRY_EPSILON);
    const pieces=[],joints=new Map(),cutters=[];
    const add=(points,owner_ids)=>{if(!points||points.length<3)return;const area=polygonArea(points);if(Math.abs(area)<GEOMETRY_EPSILON)return;if(area<0)points=[...points].reverse();pieces.push({points,owner_ids,bounds:polygonBounds(points)});};
    for(const wall of walls){
      const d=lineData(wall),half=Math.max(1,Number.isFinite(wall.thickness_mm)?wall.thickness_mm:1)/2;
      const at=along=>({x:d.start.x+d.ux*along,y:d.start.y+d.uy*along});
      const voids=openings.filter(o=>o.wall_id===wall.id&&Number.isFinite(o.offset_mm)&&Number.isFinite(o.width_mm)&&o.width_mm>0).map(o=>[Math.max(0,o.offset_mm),Math.min(d.length,o.offset_mm+o.width_mm)]).filter(([lo,hi])=>hi>lo).sort((a,b)=>a[0]-b[0]);
      let start=0;for(const[lo,hi]of voids){if(lo>start)add(wallStrip(at(start),at(lo),half),[wall.id]);start=Math.max(start,hi);const points=wallStrip(at(lo),at(hi),half+1e-6);cutters.push({points,bounds:polygonBounds(points)});}if(start<d.length)add(wallStrip(at(start),d.end,half),[wall.id]);
      for(const[p,other]of [[d.start,d.end],[d.end,d.start]]){const key=`${Math.round(p.x*1e6)},${Math.round(p.y*1e6)}`,rays=joints.get(key)||[];rays.push({point:p,ux:(other.x-p.x)/d.length,uy:(other.y-p.y)/d.length,half,length:d.length,id:wall.id});joints.set(key,rays);}
    }
    for(const rays of joints.values()){
      if(rays.length<2)continue;rays.sort((a,b)=>Math.atan2(a.uy,a.ux)-Math.atan2(b.uy,b.ux));
      for(let i=0;i<rays.length;i++){const a=rays[i],b=rays[(i+1)%rays.length],p=a.point,den=a.ux*b.uy-a.uy*b.ux;if(Math.abs(den)<1e-8)continue;
        const qa={x:p.x-a.uy*a.half,y:p.y+a.ux*a.half},qb={x:p.x+b.uy*b.half,y:p.y-b.ux*b.half},delta=minus(qb,qa),along=(delta.x*b.uy-delta.y*b.ux)/den,meet={x:qa.x+along*a.ux,y:qa.y+along*a.uy},ring=[p,qa];
        if(distance(p,meet)<=Math.min(4*Math.max(a.half,b.half),Math.max(a.half,b.half)+Math.min(a.length,b.length)/2))ring.push(meet);ring.push(qb);if(!simplePolygon(ring))continue;if(polygonArea(ring)<0)ring.reverse();
        let remaining=[ring];for(const cutter of cutters)remaining=remaining.flatMap(part=>boundsOverlap(polygonBounds(part),cutter.bounds)?subtractConvex(part,cutter.points):[part]);for(const part of remaining)add(part,[a.id,b.id].sort());
      }
    }
    const edges=exposedWallEdges(pieces),vertices=new Map();
    for(const edge of edges)for(const[p,other]of [[edge.start,edge.end],[edge.end,edge.start]]){const key=`${Math.round(p.x*1e6)},${Math.round(p.y*1e6)}`,entry=vertices.get(key)||{point:p,directions:[],owner_ids:[]};entry.directions.push(minus(other,p));entry.owner_ids.push(...edge.owner_ids);vertices.set(key,entry);}
    const corners=[];for(const entry of vertices.values()){
      const directions=entry.directions;if(directions.length===2&&Math.abs(cross2(directions[0],directions[1]))<=1e-8*Math.hypot(directions[0].x,directions[0].y)*Math.hypot(directions[1].x,directions[1].y))continue;
      corners.push({...entry.point,owner_ids:[...new Set(entry.owner_ids)].sort()});
    }
    const result={pieces:pieces.map(({points,owner_ids})=>({points,owner_ids})),edges,corners};if(floor&&typeof floor==="object")wallPresentationCache.set(floor,{signature,result});return result;
  }
  function characteristicPoints(floor) {
    const points=[];const walls=(Array.isArray(floor?.walls)?floor.walls:[]).filter(w=>Number.isFinite(w.start?.x)&&Number.isFinite(w.start?.y)&&Number.isFinite(w.end?.x)&&Number.isFinite(w.end?.y)&&distance(w.start,w.end)>1e-9);
    for(const wall of walls){const d=lineData(wall);for(const [label,along]of [["начало",0],["середина",d.length/2],["конец",d.length]]){
      points.push({id:`${wall.id}:${label}`,owner_ids:[wall.id],kind:label==="середина"?"midpoint":"wall_axis",x:d.start.x+d.ux*along,y:d.start.y+d.uy*along});
    }}
    for(const corner of wallPresentation(floor).corners){
      const owners=[...corner.owner_ids];let kind="corner";
      for(const opening of floor?.openings||[]){if(!owners.includes(opening.wall_id))continue;const wall=walls.find(w=>w.id===opening.wall_id);if(!wall)continue;const projected=projectOnLine(wall,corner);if(projected.distance<=(wall.thickness_mm||1)/2+1e-6&&[opening.offset_mm,opening.offset_mm+opening.width_mm].some(offset=>Math.abs(projected.along-offset)<1e-6)){owners.push(opening.id);kind="opening_edge";}}
      points.push({...corner,owner_ids:[...new Set(owners)],id:`${corner.owner_ids[0]}:corner:${corner.x}:${corner.y}`,kind});
    }
    for(const opening of floor?.openings||[]){const wall=walls.find(w=>w.id===opening.wall_id);if(!wall)continue;const d=lineData(wall);
      for(const [label,along]of [["крайA",opening.offset_mm],["центр",opening.offset_mm+opening.width_mm/2],["крайB",opening.offset_mm+opening.width_mm]]){
        points.push({id:`${opening.id}:${label}`,owner_ids:[opening.id,wall.id],kind:label==="центр"?"midpoint":"opening_edge",x:d.start.x+d.ux*along,y:d.start.y+d.uy*along});
      }
    }
    return points;
  }
  function axisSnapPoint(value,points,axis,tolerance){const p=point(value);if(!["x","y"].includes(axis))throw new TypeError("Ось должна быть x или y.");let best=null;
    for(const target of points){const delta=Math.abs(target[axis]-p[axis]);if(delta>tolerance)continue;const tie=distance(p,target);if(!best||delta<best.delta-1e-8||(Math.abs(delta-best.delta)<1e-8&&tie<best.tie))best={target,delta,tie};}
    return best?{point:{...p,[axis]:best.target[axis]},target:point(best.target),target_id:best.target.id,target_kind:best.target.kind||"point"}:null;
  }
  function boundsBetween(a,b){return{x:Math.min(a.x,b.x),y:Math.min(a.y,b.y),w:Math.abs(b.x-a.x),h:Math.abs(b.y-a.y)};}
  function geometryInBox(points,box,crossing=false,closed=false){
    if(!points.length)return false;const inside=p=>p.x>=box.x&&p.x<=box.x+box.w&&p.y>=box.y&&p.y<=box.y+box.h;
    if(!crossing)return points.every(inside);if(points.some(inside))return true;
    const edges=points.slice(1).map((p,i)=>[points[i],p]);if(closed)edges.push([points.at(-1),points[0]]);
    for(const [a,b]of edges){let lo=0,hi=1;const delta={x:b.x-a.x,y:b.y-a.y};let possible=true;for(const axis of ["x","y"]){const start=box[axis],end=start+box[axis==="x"?"w":"h"];if(Math.abs(delta[axis])<1e-12){if(a[axis]<start||a[axis]>end)possible=false;}else{const t1=(start-a[axis])/delta[axis],t2=(end-a[axis])/delta[axis];lo=Math.max(lo,Math.min(t1,t2));hi=Math.min(hi,Math.max(t1,t2));}}if(possible&&lo<=hi)return true;}
    if(closed){const p={x:box.x+box.w/2,y:box.y+box.h/2};let contained=false;for(let i=0,j=points.length-1;i<points.length;j=i++){const a=points[i],b=points[j];if((a.y>p.y)!==(b.y>p.y)&&p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x)contained=!contained;}return contained;}
    return false;
  }
  return Object.freeze({ twoPointDown, twoPointUp, rectangleHasSize, rectangleSizeInput, repeatRectangle, validateTwoPoint, polygonPointAction, constrainEnd, modelToSvg, svgToModel, gridPoint, visibleModelBounds, projectOnLine, openingAnchor, guideLine, hasConstruction, wallPresentation,characteristicPoints,axisSnapPoint,boundsBetween,geometryInBox });
});
