"use strict";
const test=require("node:test"),assert=require("node:assert/strict");
const V=require("./finish-viewer.js"),P=require("./finish-presentation.js");
const panel={id:"one",face_id:"wall",floor_id:"floor",color:"#aabbcc",footprint_mm:[[0,0],[800,0],[800,16],[0,16]],bottom_mm:3000,height_mm:2500};
const scene={schema_version:"finish-scene/1.0",panels:[panel],schematic_fixings:[{face_id:"wall",floor_id:"floor",points:[[390,-2,3100],[410,-2,3100]]}]};
test("solid panels have two caps, four sides, real thickness and floor elevation",()=>{
  const part=V.prism(panel);assert.equal(part.faces.length,6);assert.equal(part.edges.length,12);
  assert.deepEqual(V.prepare(scene).min,[0,0,3000]);assert.deepEqual(V.prepare(scene).max,[800,16,5500]);
  for(const f of part.faces)assert.ok(Math.abs(Math.hypot(...f.normal)-1)<1e-8);
  assert.equal(V.prism({...panel,footprint_mm:[...panel.footprint_mm].reverse()}).faces.length,6);
  assert.throws(()=>V.prism({...panel,height_mm:NaN}));
});
test("top projection separates plan axes while front projection puts heights up",()=>{
  const top=V.project([100,200,300],[0,0,0],0,Math.PI/2);assert.ok(Math.abs(top[1]+200)<1e-8);assert.equal(top[0],100);
  assert.deepEqual(V.project([100,200,300],[0,0,0],0,0),[100,-300,-200]);
});
test("presentation request excludes STEP caches and supplier snapshots without modifying source",()=>{
  const p={id:"p",properties:{wall_mounting:{walls:[{wall_id:"w"}],snapshot:{large:"manual"}},mount_assemblies:{huge:"STEP"},whole_wall_assemblies:{huge:"STEP"},finish_presentation:{panel_color:"#123456"}}};
  const before=JSON.stringify(p),input=P.projectInput(p);
  assert.equal(input.properties.mount_assemblies,undefined);assert.equal(input.properties.wall_mounting.snapshot,undefined);
  assert.equal(input.properties.finish_presentation.panel_color,"#123456");assert.equal(JSON.stringify(p),before);
});
function harness(){
  const calls=[],events={},frames=[];
  const gl=new Proxy({COMPILE_STATUS:1,LINK_STATUS:2,ARRAY_BUFFER:3,STATIC_DRAW:4,DEPTH_TEST:5,BLEND:6,CULL_FACE:7,TRIANGLES:8,LINES:9,POLYGON_OFFSET_FILL:10}, {get(target,key){if(key in target)return target[key];return (...args)=>{calls.push([key,...args]);if(key.startsWith("create"))return {};if(key==="getShaderParameter"||key==="getProgramParameter")return true;if(key==="getAttribLocation")return 0;};}});
  const canvas={width:0,height:0,getContext:()=>gl,getBoundingClientRect:()=>({width:1000,height:700,left:0,top:0}),addEventListener:(name,fn)=>{events[name]=fn;},removeEventListener(){},setPointerCapture(){},releasePointerCapture(){}};
  const empty={},viewer=V.create(canvas,empty,{frame:fn=>frames.push(fn)});
  const flush=()=>{while(frames.length)frames.shift()();};
  return {viewer,gl,calls,events,flush,empty};
}
test("opaque depth-tested rendering uses two batches and never reuploads on orbit or zoom",()=>{
  const {viewer,gl,calls,events,flush,empty}=harness();viewer.setScene(scene);flush();
  assert.ok(calls.some(c=>c[0]==="enable"&&c[1]===gl.DEPTH_TEST));assert.ok(calls.some(c=>c[0]==="disable"&&c[1]===gl.BLEND));
  assert.ok(calls.some(c=>c[0]==="shaderSource"&&c[2].includes("gl_FragColor=vec4(vColor*tex,1.0)")));
  assert.equal(calls.filter(c=>c[0]==="drawArrays").length,2);
  const uploads=calls.filter(c=>c[0]==="bufferData").length;
  events.pointerdown({button:0,pointerId:1,clientX:50,clientY:50,preventDefault(){}});
  for(let i=1;i<=30;i++){events.pointermove({pointerId:1,clientX:50+i*5,clientY:60});flush();}
  events.pointerup({pointerId:1});events.wheel({deltaY:80,clientX:400,clientY:300,preventDefault(){}});flush();
  assert.equal(calls.filter(c=>c[0]==="bufferData").length,uploads);
  viewer.setLayers(false,true);calls.length=0;flush();assert.equal(calls.filter(c=>c[0]==="drawArrays").length,1);assert.equal(viewer.pick(500,350),null);
  viewer.setFloors([]);flush();assert.equal(viewer.stats().panels,0);assert.equal(empty.hidden,false);viewer.dispose();
});
test("colour and hidden walls update batches; picking selects nearest opaque face",()=>{
  const {viewer,calls,flush}=harness();viewer.setScene(scene);viewer.view("front");flush();
  assert.equal(viewer.pick(500,350).face_id,"wall");
  const before=calls.filter(c=>c[0]==="bufferData").length;viewer.setAppearance({panel_color:"#123456"});flush();assert.equal(calls.filter(c=>c[0]==="bufferData").length,before+6);
  viewer.setHidden(["wall"]);flush();assert.equal(viewer.pick(500,350),null);assert.equal(viewer.stats().panels,0);viewer.dispose();
});
test("right-handed camera has the same plan orientation as the editor at every orbit",()=>{
  const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]],dot=(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0);
  for(const yaw of [-2,-.65,0,1])for(const pitch of [-1,0,.6,Math.PI/2]){const columns=[[1,0,0],[0,1,0],[0,0,1]].map(p=>V.project(p,[0,0,0],yaw,pitch));assert.ok(Math.abs(dot(cross(columns[0],columns[1]),columns[2])+1)<1e-8);}
});
test("substrate occludes panels from the reverse side; no substrate wireframe or colour override",()=>{
  const {viewer,gl,calls,flush}=harness(),base={...panel,id:"base",face_id:undefined,kind:"substrate",material:"concrete",color:"#b0b0b0",footprint_mm:[[0,-160],[800,-160],[800,-10],[0,-10]]};
  viewer.setScene({...scene,structures:[base]});viewer.view("front");flush();assert.equal(viewer.pick(500,350),null);
  assert.equal(calls.filter(c=>c[0]==="drawArrays").length,3);assert.equal(calls.filter(c=>c[0]==="drawArrays"&&c[1]===gl.LINES).length,1);
  viewer.setLayers(true,false,false);flush();assert.equal(viewer.pick(500,350).id,"one");assert.equal(viewer.stats().panels,1);viewer.dispose();
});
test("a trimmed panel retains one selectable identity and per-panel visual material",()=>{
  const split={...panel,meshes:[{...panel,height_mm:1200},{...panel,bottom_mm:4300,height_mm:1200}]};
  const p=V.prepare({...scene,panels:[split]});assert.equal(p.parts.length,2);assert.ok(p.parts.every(p=>p.panel.id==="one"));
  assert.equal(V.surface(panel,{surface:"painted",face_surfaces:{wall:"hpl"},panel_surfaces:{one:"veneer"}}),"veneer");
  assert.equal(V.surface({...panel,material:"veneer"},{surface:"hpl",panel_surfaces:{one:"auto"}}),"veneer");
});
test("opaque upper floor occludes lower panels even with both floors selected, independently of walls",()=>{
  const {viewer,gl,calls,events,flush}=harness();
  const slab={id:"slab",floor_id:"upper",kind:"floor",color:"#cccccc",footprint_mm:[[0,0],[800,0],[800,16],[0,16]],bottom_mm:5600,height_mm:200};
  viewer.setScene({...scene,slabs:[slab]});viewer.setFloors(["floor","upper"]);viewer.setLayers(true,false,false);viewer.view("top");flush();
  assert.equal(viewer.pick(500,350),null);assert.equal(viewer.stats().panels,1);assert.equal(viewer.stats().floors,1);
  assert.equal(calls.filter(c=>c[0]==="drawArrays").length,3);
  assert.ok(calls.some(c=>c[0]==="disable"&&c[1]===gl.BLEND));
  const uploads=calls.filter(c=>c[0]==="bufferData").length;
  events.wheel({deltaY:-10,clientX:500,clientY:350,preventDefault(){}});flush();assert.equal(calls.filter(c=>c[0]==="bufferData").length,uploads);
  viewer.setLayers(true,false,false,true,false);flush();assert.equal(viewer.pick(500,350).id,"one");
  viewer.setLayers(true,false,false);viewer.setFloors(["floor"]);flush();assert.equal(viewer.pick(500,350).id,"one");viewer.dispose();
});
test("profiles and floors have their own colours and batches, not panel texture, count or wireframes",()=>{
  const style={panel_color:"#112233",profile_color:"#ededed",floor_color:"#aabbcc",face_profile_colors:{wall:"#223344"},floor_colors:{upper:"#a0b0c0"}};
  assert.equal(V.partColor({...panel,kind:"profile"},style),"#223344");assert.equal(V.partColor({...panel,kind:"floor"},style),"#aabbcc");
  assert.equal(V.partColor({...panel,kind:"floor",floor_id:"upper"},style),"#a0b0c0");assert.equal(V.partColor(panel,style),"#112233");
  const profile={...panel,id:"trim",kind:"profile",height_mm:3000},h=harness();
  h.viewer.setScene({...scene,profiles:[profile]});h.flush();assert.equal(h.viewer.stats().panels,1);assert.equal(h.viewer.stats().profiles,1);
  assert.equal(h.calls.filter(c=>c[0]==="drawArrays"&&c[1]===h.gl.LINES).length,1);assert.equal(h.calls.filter(c=>c[0]==="drawArrays").length,3);
  h.viewer.setFloors([]);h.flush();assert.equal(h.viewer.stats().profiles,0);h.viewer.dispose();
});
test("thin visible profile centrelines stay depth tested and use one static batch during orbit",()=>{
  const h=harness(),profile={...panel,id:"thin",kind:"profile",front_normal:[0,-1],orientation:"vertical"};
  h.viewer.setScene({...scene,profiles:[profile]});h.flush();
  const lines=h.calls.filter(c=>c[0]==="drawArrays"&&c[1]===h.gl.LINES);
  assert.equal(lines.length,2);assert.ok(lines.some(c=>c[2]>0&&c[3]===2));
  const uploads=h.calls.filter(c=>c[0]==="bufferData").length;
  h.events.pointerdown({button:0,pointerId:1,clientX:300,clientY:300,preventDefault(){}});
  h.events.pointermove({pointerId:1,clientX:500,clientY:350});h.flush();
  assert.equal(h.calls.filter(c=>c[0]==="bufferData").length,uploads);h.viewer.dispose();
});
test("an opaque floor wins picking and depth bias at a coplanar lower wall top",()=>{
  const h=harness(),slab={...panel,id:"slab",floor_id:"upper",kind:"floor",bottom_mm:5300,height_mm:200};
  h.viewer.setScene({...scene,slabs:[slab]});h.viewer.view("top");h.flush();
  assert.equal(h.viewer.pick(500,350),null);assert.ok(h.calls.some(c=>c[0]==="polygonOffset"&&c[1]<0&&c[2]<0));
  h.viewer.setFloors(["floor"]);h.flush();assert.equal(h.viewer.pick(500,350).id,"one");h.viewer.dispose();
});
