"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),A=require("./agent-panel.js");
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.attributes={};this.events={};this.value="";this.checked=false;this.disabled=false;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(k,v){this.attributes[k]=v;}
  addEventListener(k,fn){this.events[k]=fn;}
  focus(){}
  fire(k){return this.events[k]?.({target:this,preventDefault(){},stopPropagation(){}});}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(configured=true){
  const doc={createElement:tag=>new Element(tag)},host=new Element("aside");host.ownerDocument=doc;
  let project={id:"p",revision:1,metadata:{name:"До"},properties:{},buildings:[]};
  const calls=[],pending=[],applied=[],navigated=[];
  const settings={configured,model:"gpt-6.1-sol",budget_usd:2,reasoning:"medium",models:[{id:"gpt-6.1-sol",label:"Sol"}],key_source:"session"};
  const request=(url,args)=>{calls.push({url,args});if(url==="/agent/settings")return Promise.resolve(settings);if(url==="/agent/help")return Promise.resolve({destinations:[{target:"mount",label:"Открыть монтаж",instructions:"Выберите стену → Монтаж → Стыки"}]});return new Promise((resolve,reject)=>pending.push({url,resolve,reject}));};
  const api=A.create(host,{document:doc,getProject:()=>project,sourceProject:()=>project,request,getContext:()=>({wall_id:"wall-1"}),navigate:link=>navigated.push(link),canStart:()=>true,confirm:()=>true,apply:(data)=>{applied.push(data);project=data;api.refresh();return true;}});
  const nodes=(root=host)=>[root,...root.children.flatMap(nodes)],button=text=>nodes().find(n=>n.tagName==="button"&&n.textContent===text);
  const consent=()=>nodes().filter(n=>n.type==="checkbox")[0];
  async function start(){api.open();await tick();consent().checked=true;consent().fire("change");const input=nodes().find(n=>n.tagName==="textarea");input.value="Переименуй";nodes().find(n=>n.tagName==="form").fire("submit");await tick();}
  const finished={id:"r1",status:"completed",text:"Готово <script>bad()</script>",links:[{target:"mount",entity_id:"wall-1",label:"Открыть монтаж"}],previews:[],proposal_ready:true,cost_usd:.01,budget_usd:2,events:[],message:"Готово"};
  const candidate={project:{...structuredClone(project),metadata:{name:"После"}},summary:["Название"],diff:[{path:"/metadata/name",before:"До",after:"После"}],diff_total:1};
  async function propose(){await start();pending.shift().resolve({id:"r1",status:"running",events:[],budget_usd:2});await tick();pending.shift().resolve(finished);await tick();pending.shift().resolve(candidate);await tick();}
  return {api,host,nodes,button,consent,settings,calls,pending,applied,navigated,start,propose,candidate,getProject:()=>project,replace:value=>{project=value;api.refresh();}};
}
test("safe navigation, signatures and explicit source attachments",()=>{
  assert.equal(A.canApply({id:"p",n:1},{n:1,id:"p"}),true);
  assert.equal(A.canApply({id:"p",n:1},{id:"p",n:2}),false);
  assert.equal(A.safeNavigation({target:"mount",entity_id:"wall-1"}),true);
  for(const link of [{target:"https://evil"},{target:"mount",entity_id:"<script>"},{target:"shell"}])assert.equal(A.safeNavigation(link),false);
  const source={source_id:"a".repeat(64),filename:"plan.dxf"};
  assert.deepEqual(A.sourceFiles({properties:{cad_assets:[source]},buildings:[{floors:[{properties:{source_import:source}}]}]}),[source]);
});
test("opening and navigation help do not require a key or issue paid requests",async()=>{
  const h=setup(false);h.api.open();await tick();assert.equal(h.button("Отправить").disabled,true);
  await h.button("Где профили?").fire("click");h.button("Открыть монтаж").fire("click");
  assert.deepEqual(h.navigated,[{target:"mount",entity_id:"wall-1",label:"Открыть монтаж",instructions:"Выберите стену → Монтаж → Стыки"}]);
  assert.equal(h.calls.some(c=>c.url==="/agent/runs"),false);assert.equal(h.host.hidden,true);h.api.dispose();
});
test("consent is required even when an API key is configured",async()=>{
  const h=setup();h.api.open();await tick();assert.equal(h.button("Отправить").disabled,true);
  h.consent().checked=true;h.consent().fire("change");assert.equal(h.button("Отправить").disabled,false);h.api.dispose();
});
test("agent proposes without changing the project, then applies once with explicit acceptance",async()=>{
  const h=setup();await h.propose();assert.equal(h.getProject().metadata.name,"До");assert.equal(h.applied.length,0);
  assert.equal(h.nodes().some(n=>n.tagName==="script"),false);assert.ok(h.nodes().some(n=>n.textContent?.includes("<script>bad()")));
  const accepting=h.button("Применить правки").fire("click");await tick();const validation=h.pending.shift();assert.equal(validation.url,"/agent/runs/r1/proposal");
  validation.resolve(h.candidate);await accepting;assert.equal(h.getProject().metadata.name,"После");assert.equal(h.applied.length,1);
  assert.equal(h.calls.some(c=>c.url.includes("/projects/")),false);h.api.dispose();
});
test("manual edits after start block replacement and remain intact",async()=>{
  const h=setup();await h.propose();h.getProject().metadata.name="Ручная правка";h.api.refresh();assert.equal(h.button("Применить правки").disabled,true);
  await h.button("Применить правки").fire("click");assert.equal(h.applied.length,0);assert.equal(h.pending.length,0);h.api.dispose();
});
test("manual edit during final validation is not overwritten",async()=>{
  const h=setup();await h.propose();const accepting=h.button("Применить правки").fire("click");await tick();h.getProject().metadata.name="Новее";h.pending.shift().resolve(h.candidate);await accepting;
  assert.equal(h.applied.length,0);assert.equal(h.getProject().metadata.name,"Новее");h.api.dispose();
});
test("switching projects cancels late tasks and does not carry consent",async()=>{
  const h=setup();await h.start();assert.equal(h.button("Остановить").disabled,true);
  h.replace({id:"other",metadata:{name:"Другой"},properties:{},buildings:[]});h.pending.shift().resolve({id:"late",status:"running"});await tick();
  assert.ok(h.calls.some(c=>c.url==="/agent/runs/late/cancel"));assert.equal(h.consent().checked,false);assert.equal(h.applied.length,0);h.api.dispose();
});
test("rejecting a proposal enables a follow-up without changing the model",async()=>{
  const h=setup();await h.propose();h.button("Отклонить").fire("click");assert.equal(h.button("Отправить").disabled,false);assert.equal(h.getProject().metadata.name,"До");h.api.dispose();
});
