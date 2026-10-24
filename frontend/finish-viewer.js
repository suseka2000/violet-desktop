(function(root,factory){const api=factory();if(typeof module==="object"&&module.exports)module.exports=api;else root.VioletFinishViewer=api;})(typeof globalThis!=="undefined"?globalThis:this,function(){
  "use strict";
  const DEFAULT_COLOR="#c4ab86";
  const color=value=>/^#[0-9a-f]{6}$/i.test(value||"")?value.toLowerCase():DEFAULT_COLOR;
  const rgb=value=>[1,3,5].map(i=>parseInt(color(value).slice(i,i+2),16)/255);
  const dot=(a,b)=>a.reduce((sum,value,i)=>sum+value*b[i],0);
  const MATERIAL={veneer:1,hpl:2,painted:3,core:4,concrete:5,solid_masonry:6,hollow_masonry:6,gypsum:7,unknown:8,unspecified:3};
  const surface=(panel,style)=>{const value=style.panel_surfaces?.[panel.id]||style.face_surfaces?.[panel.face_id]||style.surface;return value&&value!=="auto"?value:panel.material||"painted";};
  const kind=part=>part.kind||"panel";
  function partColor(part,style={}){
    if(kind(part)==="substrate")return color(part.color);
    if(kind(part)==="profile")return color(style.face_profile_colors?.[part.face_id]||style.profile_color||part.color||"#d7d9dc");
    if(kind(part)==="floor")return color(style.floor_colors?.[part.floor_id]||style.floor_color||part.color||"#bdb9b0");
    return color(style.panel_colors?.[part.id]||style.face_colors?.[part.face_id]||style.panel_color||part.color);
  }
  function normal(a,b,c){const u=b.map((v,i)=>v-a[i]),v=c.map((w,i)=>w-a[i]),n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]],length=Math.hypot(...n)||1;return n.map(x=>x/length);}
  function prism(panel){
    const polygon=panel.footprint_mm;
    if(!Array.isArray(polygon)||polygon.length<3||!polygon.every(p=>Array.isArray(p)&&p.length===2&&p.every(Number.isFinite))||!Number.isFinite(panel.bottom_mm)||!Number.isFinite(panel.height_mm)||panel.height_mm<=0)throw Error("Повреждена геометрия панели");
    // Normalise winding; all panels are convex clipped prisms with real depth.
    const signed=polygon.reduce((sum,p,i)=>sum+p[0]*polygon[(i+1)%polygon.length][1]-polygon[(i+1)%polygon.length][0]*p[1],0);
    const ring=signed<0?[...polygon].reverse():polygon;
    const bottom=ring.map(p=>[...p,panel.bottom_mm]),top=ring.map(p=>[...p,panel.bottom_mm+panel.height_mm]);
    const caps=panel.cap_triangles_mm,faces=[],edges=[];
    if(caps){for(const triangle of caps){const [a,b,c]=triangle,ccw=(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])>0?triangle:[...triangle].reverse();faces.push({points:[...ccw].reverse().map(p=>[...p,panel.bottom_mm]),normal:[0,0,-1]},{points:ccw.map(p=>[...p,panel.bottom_mm+panel.height_mm]),normal:[0,0,1]});}}
    else faces.push({points:[...bottom].reverse(),normal:[0,0,-1]},{points:top,normal:[0,0,1]});
    for(let i=0;i<ring.length;i++){const j=(i+1)%ring.length,points=[bottom[i],bottom[j],top[j],top[i]];faces.push({points,normal:normal(...points)});edges.push([bottom[i],bottom[j]],[top[i],top[j]],[bottom[i],top[i]]);}
    for(const hole of panel.holes_mm||[]){const signed=hole.reduce((s,p,i)=>s+p[0]*hole[(i+1)%hole.length][1]-hole[(i+1)%hole.length][0]*p[1],0),ring=signed>0?[...hole].reverse():hole;for(let i=0;i<ring.length;i++){const a=ring[i],b=ring[(i+1)%ring.length],points=[[...a,panel.bottom_mm],[...b,panel.bottom_mm],[...b,panel.bottom_mm+panel.height_mm],[...a,panel.bottom_mm+panel.height_mm]];faces.push({points,normal:normal(...points)});}}
    return {panel,faces,edges};
  }
  function prepare(scene){
    if(scene?.schema_version!=="finish-scene/1.0"||!Array.isArray(scene.panels))throw Error("Неизвестный формат модели отделки");
    const parts=[...scene.panels,...(scene.structures||[]),...(scene.profiles||[]),...(scene.slabs||[])].flatMap(p=>(p.meshes||[p]).map(mesh=>{const part=prism({...p,...mesh});part.panel=p;return part;})),min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];
    for(const part of parts)for(const face of part.faces)for(const p of face.points)for(let i=0;i<3;i++){min[i]=Math.min(min[i],p[i]);max[i]=Math.max(max[i],p[i]);}
    if(!parts.length)return {parts,min:[0,0,0],max:[1,1,1],scene};
    return {parts,min,max,scene};
  }
  // Right-handed Z-up camera. At top: +X is right and +Y is up, just like
  // the editor. The old down/depth pair reflected the model, not rotated it.
  function project(point,center,yaw,pitch){const [x,y,z]=point.map((v,i)=>v-center[i]),cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);return [x*cy+y*sy,x*sy*sp-y*cy*sp-z*cp,x*sy*cp-y*cy*cp+z*sp];}
  function inside(point,polygon){let sign=0;for(let i=0;i<polygon.length;i++){const a=polygon[i],b=polygon[(i+1)%polygon.length],cross=(b[0]-a[0])*(point[1]-a[1])-(b[1]-a[1])*(point[0]-a[0]);if(Math.abs(cross)<1e-6)continue;const next=Math.sign(cross);if(sign&&next!==sign)return false;sign=next;}return !!sign;}
  function create(canvas,empty,options={}){
    let geometry=null,visibleParts=[],floors=null,hidden=new Set(),showPanels=true,showFixings=false,showStructure=true,showProfiles=true,showSlabs=true,style={},selected=null;
    let yaw=-.65,pitch=.55,zoom=1,pan=[0,0],drag=null,alive=true,queued=false,baseScale=1,fitPending=true,lastSize="",center=[0,0,0],extent=[1,1,1],buffersDirty=true;
    const frame=options.frame||(fn=>requestAnimationFrame(fn));
    let gl=null,ctx=null,program=null,vertexBuffer=null,lineBuffer=null,fixBuffer=null,baseBuffer=null,profileBuffer=null,slabBuffer=null,vertexCount=0,lineCount=0,fixCount=0,baseCount=0,profileCount=0,profileLineCount=0,slabCount=0,lost=false;
    const vertexSource=`attribute vec3 aPosition;attribute vec3 aNormal;attribute vec3 aColor;attribute vec2 aUV;attribute float aMaterial;
      uniform vec3 uRight,uDown,uDepth;uniform vec2 uScale,uPan;uniform float uRange;
      varying vec3 vColor;varying vec2 vUV;varying float vMaterial;void main(){gl_Position=vec4(dot(aPosition,uRight)*uScale.x+uPan.x,-dot(aPosition,uDown)*uScale.y-uPan.y,-dot(aPosition,uDepth)/uRange,1.0);
      float shade=.65+.35*max(dot(aNormal,normalize(vec3(-.35,-.45,.85))),0.0);vColor=aColor*shade;vUV=aUV;vMaterial=aMaterial;}`;
    const fragmentSource=`precision highp float;varying vec3 vColor;varying vec2 vUV;varying float vMaterial;
      float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
      float noise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);return mix(mix(hash(i),hash(i+vec2(1,0)),f.x),mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),f.x),f.y);}
      void main(){float tex=1.0;
        if(vMaterial>.5&&vMaterial<1.5){float bend=sin(vUV.y*.0017)*9.0+noise(vUV*.0015)*17.0;float grain=sin((vUV.x+bend)*.13)+.45*sin((vUV.x+bend)*.41);tex=.97+grain*.045+(noise(vec2(vUV.x*.08,vUV.y*.001))-.5)*.15;}
        else if(vMaterial<3.5&&vMaterial>.5){tex=.99+(noise(vUV*.35)-.5)*.025;}
        else if(vMaterial>3.5&&vMaterial<4.5){tex=.93+(noise(vUV*.8)-.5)*.18;}
        else if(vMaterial>4.5&&vMaterial<5.5){tex=.97+(noise(vUV*.05)-.5)*.12+(noise(vUV*.5)-.5)*.035;}
        else if(vMaterial>5.5&&vMaterial<6.5){float row=floor(vUV.y/75.0);vec2 cell=mod(vec2(vUV.x+mod(row,2.0)*125.0,vUV.y),vec2(250,75));float mortar=max(1.0-smoothstep(3.0,6.0,cell.x),1.0-smoothstep(3.0,6.0,cell.y));tex=1.0+(noise(vUV*.04)-.5)*.09;gl_FragColor=vec4(mix(vColor*tex,vec3(.67,.65,.60),mortar*.65),1.0);return;}
        else if(vMaterial>9.5){tex=.99+(noise(vUV*.12)-.5)*.045;}
        gl_FragColor=vec4(vColor*tex,1.0);}`;
    function initGL(){
      gl=canvas.getContext?.("webgl",{alpha:false,antialias:true,preserveDrawingBuffer:false});
      if(!gl){ctx=canvas.getContext?.("2d");return;}
      function shader(type,source){const value=gl.createShader(type);gl.shaderSource(value,source);gl.compileShader(value);if(!gl.getShaderParameter(value,gl.COMPILE_STATUS)){gl.deleteShader(value);throw Error("Не удалось подготовить 3D-просмотр");}return value;}
      const vs=shader(gl.VERTEX_SHADER,vertexSource),fs=shader(gl.FRAGMENT_SHADER,fragmentSource);
      program=gl.createProgram();gl.attachShader(program,vs);gl.attachShader(program,fs);gl.linkProgram(program);gl.deleteShader(vs);gl.deleteShader(fs);
      if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error("Не удалось запустить 3D-просмотр");
      vertexBuffer=gl.createBuffer();lineBuffer=gl.createBuffer();fixBuffer=gl.createBuffer();baseBuffer=gl.createBuffer();profileBuffer=gl.createBuffer();slabBuffer=gl.createBuffer();buffersDirty=true;
    }
    try{initGL();}catch(error){empty.textContent=error.message;options.onError?.(error);}
    function refreshParts(){
      const hiddenWalls=new Set(geometry?.scene.views?.filter(v=>hidden.has(v.face_id)).map(v=>v.wall_id));
      visibleParts=geometry?geometry.parts.filter(p=>(!floors||floors.has(p.panel.floor_id))&&!hidden.has(p.panel.face_id)&&!hidden.has(p.panel.wall_id)&&!hiddenWalls.has(p.panel.wall_id)):[];
      const points=visibleParts.flatMap(p=>p.faces.flatMap(f=>f.points));
      if(points.length){const min=[...points[0]],max=[...min];for(const p of points)for(let i=0;i<3;i++){min[i]=Math.min(min[i],p[i]);max[i]=Math.max(max[i],p[i]);}center=min.map((v,i)=>v/2+max[i]/2);extent=max.map((v,i)=>Math.max(.01,v-min[i]));}
      buffersDirty=true;fitPending=true;
    }
    function panelColor(panel){return partColor(panel,style);}
    function isSelected(panel){return selected!=null&&kind(panel)==="panel"&&(panel.id===selected||panel.face_id===selected);}
    function layerVisible(panel){return kind(panel)==="substrate"?showStructure:kind(panel)==="profile"?showProfiles:kind(panel)==="floor"?showSlabs:showPanels;}
    function coordinates(p,n,panel){
      if(kind(panel)==="substrate"||kind(panel)==="floor")return Math.abs(n[2])>.5?[p[0]-center[0],p[1]-center[1]]:[Math.abs(n[0])>Math.abs(n[1])?p[1]-center[1]:p[0]-center[0],p[2]-center[2]];
      const origin=panel.texture_origin_mm||[0,0,0],t=panel.tangent||[1,0],seed=[...(panel.id||"")].reduce((s,c)=>(s*31+c.charCodeAt(0))%997,0);
      return [(p[0]-origin[0])*t[0]+(p[1]-origin[1])*t[1]+seed*7,p[2]-origin[2]];
    }
    function upload(){
      if(!gl||lost||!buffersDirty)return;
      const triangles=[],lines=[],fixings=[],bases=[],profiles=[],profileLines=[],slabs=[];
      const emit=(array,p,n,c,uv=[0,0],material=0)=>array.push(...p.map((v,i)=>v-center[i]),...n,...c,...uv,material);
      for(const part of visibleParts){const panel=part.panel,type=kind(panel),base=type==="substrate",isPanel=type==="panel",c=rgb(panelColor(panel)),batch=base?bases:type==="profile"?profiles:type==="floor"?slabs:triangles;for(const face of part.faces){
        const front=!panel.front_normal||dot(face.normal,[...panel.front_normal,0])>.5;
        const material=base?MATERIAL[panel.material]||8:type==="profile"?9:type==="floor"?10:front?MATERIAL[surface(panel,style)]||3:4;
        const faceColor=!isPanel||front?c:c.map(v=>v*.78);
        for(let i=1;i<face.points.length-1;i++)for(const p of [face.points[0],face.points[i],face.points[i+1]])emit(batch,p,face.normal,faceColor,coordinates(p,face.normal,panel),material);
        // Preserve a subpixel metal face at distant zoom without widening the
        // physical joint. A depth-tested one-pixel centreline stays inside its
        // real face; the buffer is static during all camera movements.
        if(type==="profile"&&panel.front_normal&&dot(face.normal,[...panel.front_normal,0])>.999&&face.points.length===4){
          const midpoint=(a,b)=>a.map((v,i)=>(v+b[i])/2),q=face.points;
          const endpoints=panel.orientation==="horizontal"?[midpoint(q[0],q[3]),midpoint(q[1],q[2])]:[midpoint(q[0],q[1]),midpoint(q[3],q[2])];
          for(const p of endpoints)emit(profileLines,p,face.normal,c);
        }
        }
        // No substrate wireframe: rear walls must not acquire panel seams or
        // triangulation lines. All individual panels remain separate solids.
        if(isPanel){const edgeColor=isSelected(panel)?[.35,.22,.94]:c.map(v=>v*.57);
          for(const edge of part.edges)for(const p of edge)emit(lines,p,[0,0,1],edgeColor);}
      }
      for(const symbol of geometry?.scene.schematic_fixings||[])if((!floors||floors.has(symbol.floor_id))&&!hidden.has(symbol.face_id))for(const p of symbol.points)emit(fixings,p,[0,0,1],[.32,.38,.48]);
      profileCount=profiles.length/12;profileLineCount=profileLines.length/12;
      for(const value of profileLines)profiles.push(value);
      for(const [buffer,values]of [[vertexBuffer,triangles],[lineBuffer,lines],[fixBuffer,fixings],[baseBuffer,bases],[profileBuffer,profiles],[slabBuffer,slabs]]){gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(values),gl.STATIC_DRAW);}
      vertexCount=triangles.length/12;lineCount=lines.length/12;fixCount=fixings.length/12;baseCount=bases.length/12;slabCount=slabs.length/12;buffersDirty=false;
    }
    function transform(width,height){const cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch),right=[cy,sy,0],down=[sy*sp,-cy*sp,-cp],depth=[sy*cp,-cy*cp,sp];
      if(fitPending||lastSize!==`${width}:${height}`){baseScale=Math.min(width*.84/Math.max(1,dot(extent,right.map(Math.abs))),height*.84/Math.max(1,dot(extent,down.map(Math.abs))));lastSize=`${width}:${height}`;fitPending=false;}
      return {right,down,depth,scale:baseScale*zoom,range:Math.max(1,Math.hypot(...extent))};}
    function draw(){
      queued=false;if(!alive||lost)return;const rect=canvas.getBoundingClientRect(),width=rect.width,height=rect.height;if(!(width>0&&height>0))return;
      const dpr=Math.min(2,globalThis.devicePixelRatio||1,8192/width,8192/height);if(canvas.width!==Math.round(width*dpr)||canvas.height!==Math.round(height*dpr)){canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);}
      empty.hidden=!!visibleParts.length;if(!visibleParts.length)empty.textContent=geometry?.parts.length?"Все стены или этажи скрыты. Нажмите «Показать всё».":"Назначьте панелям монтаж, чтобы увидеть готовую отделку.";
      const basis=transform(width,height);
      if(gl&&program){upload();gl.viewport(0,0,canvas.width,canvas.height);gl.clearColor(.957,.966,.978,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.useProgram(program);gl.enable(gl.DEPTH_TEST);gl.disable(gl.BLEND);gl.disable(gl.CULL_FACE);
        for(const [name,value]of [["uRight",basis.right],["uDown",basis.down],["uDepth",basis.depth]])gl.uniform3fv(gl.getUniformLocation(program,name),value);
        gl.uniform2f(gl.getUniformLocation(program,"uScale"),basis.scale*2/width,basis.scale*2/height);gl.uniform2f(gl.getUniformLocation(program,"uPan"),pan[0]*2/width,pan[1]*2/height);gl.uniform1f(gl.getUniformLocation(program,"uRange"),basis.range);
        function render(buffer,count,mode,first=0){gl.bindBuffer(gl.ARRAY_BUFFER,buffer);for(const [name,offset,size]of [["aPosition",0,3],["aNormal",12,3],["aColor",24,3],["aUV",36,2],["aMaterial",44,1]]){const loc=gl.getAttribLocation(program,name);if(loc<0)continue;gl.enableVertexAttribArray(loc);gl.vertexAttribPointer(loc,size,gl.FLOAT,false,48,offset);}gl.drawArrays(mode,first,count);}
        if(showStructure&&baseCount)render(baseBuffer,baseCount,gl.TRIANGLES);
        // Legacy stories may put the upper finished floor exactly at the
        // lower wall top. Opaque floors must cover those coplanar edge lines.
        if(showSlabs&&slabCount){gl.enable(gl.POLYGON_OFFSET_FILL);gl.polygonOffset(-2,-2);render(slabBuffer,slabCount,gl.TRIANGLES);gl.disable(gl.POLYGON_OFFSET_FILL);}
        if(showProfiles&&profileCount){gl.enable(gl.POLYGON_OFFSET_FILL);gl.polygonOffset(1,1);render(profileBuffer,profileCount,gl.TRIANGLES);gl.disable(gl.POLYGON_OFFSET_FILL);if(profileLineCount)render(profileBuffer,profileLineCount,gl.LINES,profileCount);}
        if(showPanels){gl.enable(gl.POLYGON_OFFSET_FILL);gl.polygonOffset(1,1);render(vertexBuffer,vertexCount,gl.TRIANGLES);gl.disable(gl.POLYGON_OFFSET_FILL);render(lineBuffer,lineCount,gl.LINES);}
        if(showFixings)render(fixBuffer,fixCount,gl.LINES);
      }else if(ctx){
        ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle="#f4f6f9";ctx.fillRect(0,0,width,height);
        const p=point=>{const q=project(point,center,yaw,pitch);return [width/2+pan[0]+q[0]*basis.scale,height/2+pan[1]+q[1]*basis.scale,q[2]];};
        const faces=[];for(const part of visibleParts){const base=kind(part.panel)!=="panel";if(!layerVisible(part.panel))continue;for(const face of part.faces){const points=face.points.map(p);faces.push({points,color:panelColor(part.panel),depth:points.reduce((v,p)=>v+p[2],0)/points.length,selected:isSelected(part.panel),base});}}
        faces.sort((a,b)=>a.depth-b.depth);for(const face of faces){ctx.beginPath();face.points.forEach((p,i)=>ctx[i?"lineTo":"moveTo"](p[0],p[1]));ctx.closePath();ctx.globalAlpha=1;ctx.fillStyle=face.color;ctx.fill();if(!face.base){ctx.strokeStyle=face.selected?"#6550d7":"#6b6660";ctx.lineWidth=.6;ctx.stroke();}}
        if(showFixings&&!showPanels)for(const symbol of geometry?.scene.schematic_fixings||[])if((!floors||floors.has(symbol.floor_id))&&!hidden.has(symbol.face_id)){ctx.beginPath();symbol.points.map(p).forEach((point,i)=>ctx[i?"lineTo":"moveTo"](point[0],point[1]));ctx.strokeStyle="#53677e";ctx.stroke();}
      }
      options.onStats?.({...stats(),draw_calls:(showPanels?2:0)+(showFixings?1:0)+(showStructure&&baseCount?1:0)+(showSlabs&&slabCount?1:0)+(showProfiles&&profileCount?1:0)+(showProfiles&&profileLineCount?1:0)});
    }
    function schedule(){if(!queued&&alive){queued=true;frame(draw);}}
    function fit(){zoom=1;pan=[0,0];fitPending=true;schedule();}
    function pick(x,y){const r=canvas.getBoundingClientRect(),basis=transform(r.width,r.height);let best=null,bestDepth=-Infinity;
      if(!showPanels)return null;for(const part of visibleParts){if(!layerVisible(part.panel))continue;for(const face of part.faces){const points=face.points.map(p=>{const q=project(p,center,yaw,pitch);return [r.width/2+pan[0]+q[0]*basis.scale,r.height/2+pan[1]+q[1]*basis.scale,q[2]];});if(!inside([x,y],points))continue;
        // Interpolate depth on the projected triangle, not an average depth
        // (a long side wall must not steal a click from a nearer panel).
        for(let i=1;i<points.length-1;i++){const [a,b,c]=[points[0],points[i],points[i+1]],d=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1]);if(Math.abs(d)<1e-9)continue;const u=((b[1]-c[1])*(x-c[0])+(c[0]-b[0])*(y-c[1]))/d,v=((c[1]-a[1])*(x-c[0])+(a[0]-c[0])*(y-c[1]))/d;if(u<0||v<0||u+v>1)continue;const depth=u*a[2]+v*b[2]+(1-u-v)*c[2];if(depth>bestDepth+1e-6||(Math.abs(depth-bestDepth)<=1e-6&&kind(part.panel)==="floor")){bestDepth=depth;best=part.panel;}}
      }}return best&&kind(best)==="panel"?best:null;}
    function stats(){return {renderer:gl?"WebGL":"Canvas",panels:new Set(visibleParts.filter(p=>kind(p.panel)==="panel").map(p=>p.panel.id)).size,profiles:new Set(visibleParts.filter(p=>kind(p.panel)==="profile").map(p=>p.panel.id)).size,floors:new Set(visibleParts.filter(p=>kind(p.panel)==="floor").map(p=>p.panel.floor_id)).size,triangles:(vertexCount+baseCount+profileCount+slabCount)/3};}
    const listeners=[];function on(type,fn,settings){canvas.addEventListener(type,fn,settings);listeners.push([type,fn,settings]);}
    on("pointerdown",e=>{if(![0,1,2].includes(e.button))return;drag={id:e.pointerId,x:e.clientX,y:e.clientY,lastX:e.clientX,lastY:e.clientY,moved:false,pan:e.button!==0||e.shiftKey};canvas.setPointerCapture?.(e.pointerId);e.preventDefault();});
    on("pointermove",e=>{if(!drag||drag.id!==e.pointerId)return;const dx=e.clientX-drag.lastX,dy=e.clientY-drag.lastY;drag.moved ||= Math.hypot(e.clientX-drag.x,e.clientY-drag.y)>4;drag.lastX=e.clientX;drag.lastY=e.clientY;if(!drag.moved)return;if(drag.pan){pan[0]+=dx;pan[1]+=dy;}else{yaw-=dx*.006;pitch=Math.max(-1.5,Math.min(1.5,pitch+dy*.006));}schedule();});
    function end(e){if(!drag||drag.id!==e.pointerId)return;const click=!drag.moved&&!drag.pan;drag=null;canvas.releasePointerCapture?.(e.pointerId);if(click){const r=canvas.getBoundingClientRect();options.onSelect?.(pick(e.clientX-r.left,e.clientY-r.top));}}
    on("pointerup",end);on("pointercancel",()=>{drag=null;});on("lostpointercapture",()=>{drag=null;});on("contextmenu",e=>e.preventDefault());
    on("wheel",e=>{e.preventDefault();const r=canvas.getBoundingClientRect(),old=zoom;zoom=Math.max(.02,Math.min(100,zoom*Math.exp(-e.deltaY*.001)));const ratio=zoom/old;pan=[e.clientX-r.left-r.width/2-(e.clientX-r.left-r.width/2-pan[0])*ratio,e.clientY-r.top-r.height/2-(e.clientY-r.top-r.height/2-pan[1])*ratio];schedule();},{passive:false});
    on("dblclick",fit);on("webglcontextlost",e=>{e.preventDefault();lost=true;empty.hidden=false;empty.textContent="3D-контекст восстанавливается…";});on("webglcontextrestored",()=>{lost=false;initGL();schedule();});
    const observer=typeof ResizeObserver!=="undefined"?new ResizeObserver(schedule):null;observer?.observe(canvas);
    return {setScene(scene){geometry=scene?prepare(scene):null;refreshParts();fit();},setAppearance(value){style=value||{};buffersDirty=true;schedule();},setSelected(id){selected=id;buffersDirty=true;schedule();},
      setFloors(ids){floors=ids?new Set(ids):null;refreshParts();fit();},setHidden(ids){hidden=new Set(ids);refreshParts();schedule();},setLayers(panels,fixings,structure=true,profiles=true,slabs=true){showPanels=panels;showFixings=fixings;showStructure=structure;showProfiles=profiles;showSlabs=slabs;schedule();},
      view(mode){[yaw,pitch]=mode==="top"?[0,Math.PI/2]:mode==="front"?[0,0]:[-.65,.55];fit();},fit,resize:schedule,pick,
      snapshot(){draw();return new Promise(resolve=>canvas.toBlob(resolve,"image/png"));},
      stats,
      dispose(){alive=false;observer?.disconnect();for(const [type,fn,settings]of listeners)canvas.removeEventListener(type,fn,settings);if(gl&&!lost){for(const buffer of [vertexBuffer,lineBuffer,fixBuffer,baseBuffer,profileBuffer,slabBuffer])gl.deleteBuffer(buffer);gl.deleteProgram(program);}geometry=null;visibleParts=[];}};
  }
  return {DEFAULT_COLOR,color,rgb,prism,prepare,project,inside,surface,partColor,create};
});
