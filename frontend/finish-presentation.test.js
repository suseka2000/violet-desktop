"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),P=require("./finish-presentation.js");
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.style={};this.attributes={};this.events={};this.value="";this.checked=false;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(k,v){this.attributes[k]=v;}
  addEventListener(k,fn){this.events[k]=fn;}
  fire(k){return this.events[k]?.({target:this});}
}
const tick=async()=>{await Promise.resolve();await Promise.resolve();};
function setup(){
  const doc={createElement:tag=>new Element(tag)},host=new Element("div");host.ownerDocument=doc;
  let project={id:"p",revision:4,properties:{},buildings:[{id:"b",name:"Здание",floors:[{id:"f",name:"Этаж",spaces:[]}]}]};
  const result={schema_version:"finish-scene/1.0",project_id:"p",source_revision:4,appearance:{panel_color:"#abcdef",face_colors:{},align_floors:true},floors:[{id:"f",name:"Этаж",elevation_mm:0}],views:[{face_id:"face",name:"Стена",mark:"П001"}],summary:{piece_count:1,area_m2:2},warnings:[]};
  const pending=[],calls=[],mutations=[],draws=[],colours=[],viewer={setScene:x=>draws.push(x),setAppearance:x=>colours.push(x),setSelected(){},setHidden(){},setLayers(){},setFloors(){},resize(){},fit(){},dispose(){}};
  const api=P.create(host,{document:doc,getProject:()=>project,request:(url,args)=>{calls.push({url,args});return new Promise(resolve=>pending.push(resolve));},createViewer:(_c,_e,options)=>{viewer.options=options;return viewer;},mutate:(name,fn)=>{mutations.push(name);fn(project);api.refresh();},onClose(){}});
  function nodes(root=host){return [root,...root.children.flatMap(nodes)];}
  return {api,project,result,pending,calls,mutations,draws,colours,viewer,nodes,replace:p=>{project=p;}};
}
test("presentation opens without saving or enqueuing, and commits colour without a new geometry request",async()=>{
  const h=setup();h.api.open({kind:"building",id:"b"});assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,"/finish-presentation/preview");assert.equal(h.mutations.length,0);
  h.pending.shift()(h.result);await tick();
  h.nodes().find(e=>e.attributes["aria-label"]==="Белый").fire("click");
  assert.equal(h.project.properties.finish_presentation.panel_color,"#edece5");assert.equal(h.calls.length,1);assert.equal(h.mutations.length,1);
  assert.equal(h.colours.at(-1).panel_color,"#edece5");
  h.viewer.options.onSelect({id:"face:panel-1",face_id:"face",label:"П001 / 1",width_mm:600,height_mm:3000,thickness_mm:12});
  h.nodes().find(e=>e.attributes["aria-label"]==="Зелёный").fire("click");
  assert.equal(h.project.properties.finish_presentation.panel_colors["face:panel-1"],"#65776a");assert.equal(h.project.properties.finish_presentation.face_colors.face,undefined);
  const target=h.nodes().find(e=>e.attributes["aria-label"]==="Изменить внешний вид");target.value="wall";target.fire("change");
  h.nodes().find(e=>e.attributes["aria-label"]==="Зелёный").fire("click");
  assert.equal(h.project.properties.finish_presentation.face_colors.face,"#65776a");assert.equal(h.project.properties.finish_presentation.panel_color,"#edece5");assert.equal(h.calls.length,1);
  assert.equal(h.project.properties.finish_presentation.panel_colors["face:panel-1"],undefined);
});
test("texture is scoped to the chosen panel without rebuilding or changing engineering material",async()=>{
  const h=setup();h.api.open({kind:"building",id:"b"});h.pending.shift()(h.result);await tick();
  h.viewer.options.onSelect({id:"face:panel-1",face_id:"face",label:"П001 / 1",width_mm:600,height_mm:3000,thickness_mm:12});
  const surface=h.nodes().find(e=>e.attributes["aria-label"]==="Фактура панелей");surface.value="veneer";surface.fire("change");
  assert.equal(h.project.properties.finish_presentation.panel_surfaces["face:panel-1"],"veneer");assert.equal(h.calls.length,1);assert.equal(h.project.properties.wall_mounting,undefined);
});
test("late presentation responses after close or a project switch are ignored",async()=>{
  for(const mode of ["close","switch"]){const h=setup();h.api.open({kind:"building",id:"b"});if(mode==="close")h.api.close();else h.replace({...h.project,id:"another"});h.pending.shift()(h.result);await tick();assert.equal(h.draws.filter(Boolean).length,0);}
});
test("latest scope wins when older requests return out of order",async()=>{
  const h=setup();h.api.open({kind:"building",id:"b"});h.api.open({kind:"floor",id:"f"});
  h.pending[1](h.result);await tick();h.pending[0]({...h.result,summary:{piece_count:999,area_m2:999}});await tick();assert.equal(h.draws.filter(Boolean).length,1);assert.equal(h.draws.filter(Boolean)[0].summary.piece_count,1);
});
test("a stale saved revision cannot populate the view",async()=>{
  const h=setup();h.api.open({kind:"floor",id:"f"});h.pending.shift()({...h.result,source_revision:3});await tick();assert.equal(h.draws.filter(Boolean).length,0);assert.ok(h.nodes().some(e=>e.textContent==="Модель относится к другой версии проекта"));
});
test("profile and floor colours are independent, scoped, saved, and do not request new geometry",async()=>{
  const h=setup();h.api.open({kind:"building",id:"b"});h.pending.shift()(h.result);await tick();
  const find=label=>h.nodes().find(e=>e.attributes["aria-label"]===label),change=(label,value)=>{const e=find(label);e.value=value;e.fire("change");};
  change("Цвет межпанельных профилей","#112233");change("Цвет пола","#667788");
  h.viewer.options.onSelect({id:"face:panel-1",face_id:"face",label:"П001 / 1",width_mm:600,height_mm:3000,thickness_mm:12});
  change("Где изменить цвет профилей","wall");change("Цвет межпанельных профилей","#aabbcc");
  change("Где изменить цвет пола","f");change("Цвет пола","#998877");
  let style=h.project.properties.finish_presentation;
  assert.equal(style.panel_color,"#abcdef");assert.equal(style.profile_color,"#112233");assert.equal(style.face_profile_colors.face,"#aabbcc");assert.equal(style.floor_color,"#667788");assert.equal(style.floor_colors.f,"#998877");
  assert.equal(h.calls.length,1);assert.equal(h.mutations.length,4);
  change("Где изменить цвет профилей","all");change("Цвет межпанельных профилей","#ffffff");
  style=h.project.properties.finish_presentation;assert.deepEqual(style.face_profile_colors,{});assert.equal(style.floor_colors.f,"#998877");
  assert.ok(h.nodes().some(e=>e.textContent==="Полы (непрозрачные)"));
});
