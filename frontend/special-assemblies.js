(function(root,factory){const api=factory(typeof module==='object'&&module.exports?require('./wall-mount.js'):root.VioletWallMount);if(typeof module==='object'&&module.exports)module.exports=api;else root.VioletSpecialAssemblies=api;})(typeof globalThis!=='undefined'?globalThis:this,function(W){
  'use strict';
  const copy=v=>JSON.parse(JSON.stringify(v)), list=v=>Array.isArray(v)?v:[], fingerprint=W.stableKey;
  const defaults={allow_project_assumptions:true,kinds:['concealed_door','hatch'],door:{frame_depth_mm:100,frame_section_mm:35,frame_wall_mm:2,leaf_thickness_mm:40,side_gap_mm:3,bottom_gap_mm:10,hinge_count:3},hatch:{frame_depth_mm:60,frame_section_mm:25,leaf_thickness_mm:20,gap_mm:3},lighting:{location:'above_doors',above_door_offset_mm:20,housing_width_mm:18,housing_depth_mm:12,wall_mm:1.5,diffuser_thickness_mm:1.5}};
  const titles={concealed_door:'Двери со скрытой коробкой',hatch:'Ревизионные люки',lighting:'Линейная подсветка'};
  function configuration(value={}){
    if(!value||typeof value!=='object'||Array.isArray(value))throw Error('Проверьте параметры изделий.');
    const result={...copy(defaults),...copy(value)};
    if(!Array.isArray(result.kinds))throw Error('Выберите виды изделий из списка.');
    result.kinds=[...new Set(result.kinds)];
    if(!result.kinds.length||result.kinds.some(kind=>!titles[kind]))throw Error('Выберите хотя бы один вид изделий.');
    if(result.allow_project_assumptions!==true)throw Error('Эти изделия используют редактируемые проектные параметры.');
    for(const group of ['door','hatch','lighting']){
      if(value[group]!==undefined&&(!value[group]||typeof value[group]!=='object'||Array.isArray(value[group])))throw Error('Проверьте параметры изделия.');
      result[group]={...defaults[group],...value[group]};
      for(const [key,item] of Object.entries(result[group])){
        if(group==='lighting'&&key==='location')continue;
        const bottom=(group==='door'&&key==='bottom_gap_mm')||(group==='lighting'&&key==='above_door_offset_mm');
        if(typeof item!=='number'||!Number.isFinite(item)||(bottom?item<0:item<=0)||item>(bottom?100:1000))throw Error('Проверьте размеры изделий: до 1000 мм; нижний зазор двери и отступ подсветки — от 0 до 100 мм.');
      }
    }
    if(!Number.isInteger(result.door.hinge_count)||result.door.hinge_count<2||result.door.hinge_count>4)throw Error('Число петель — от 2 до 4.');
    if(!['above_doors','wall_top'].includes(result.lighting.location))throw Error('Выберите место подсветки.');
    return result;
  }
  function collection(project){const value=project?.properties?.special_assemblies||{schema_version:'special-assemblies/1.0',items:[]};if(value.schema_version!=='special-assemblies/1.0'||!Array.isArray(value.items)||new Set(value.items.map(x=>x.id)).size!==value.items.length)throw Error('Некорректный список сборок изделий.');return copy(value);}
  function checkedPlan(project,plan){
    if(!plan||!Array.isArray(plan.items)||plan.proposed_installations?.schema_version!=='special-installations/1.0'||!Array.isArray(plan.proposed_installations.items))throw Error('Сервер не вернул план изделий.');
    const items=plan.proposed_installations.items,ids=new Set(items.map(x=>x.id));
    if(ids.size!==items.length||plan.items.some(x=>!ids.has(x.id))||new Set(plan.items.map(x=>x.id)).size!==plan.items.length)throw Error('План содержит повторные или неизвестные изделия.');
    const previous=list(project?.properties?.special_installations?.items);
    if(previous.some(x=>!ids.has(x.id)))throw Error('План теряет существующие изделия. Ничего не изменено.');
    return copy(plan);
  }
  async function prepare(project,plan,request,progress=()=>{},cancelled=()=>false){
    plan=checkedPlan(project,plan);const working=W.requestProject(project),initial=fingerprint(project),outcomes=[];
    working.properties||={};working.properties.special_installations=copy(plan.proposed_installations);working.properties.special_assemblies=collection(project);
    for(const item of plan.items){
      if(cancelled())return {cancelled:true};
      progress({item,outcomes,index:outcomes.length,total:plan.items.length});
      try{
        const data=await request('/special-assemblies/build-one',{method:'POST',body:{project:copy(working),installation_id:item.id}});
        if(cancelled())return {cancelled:true};
        if(data.status==='ready'){
          const record=data.assembly;
          if(!record?.id||record.installation_id!==item.id)throw Error('Получено изделие для другого места.');
          const remaining=working.properties.special_assemblies.items.filter(x=>x.installation_id!==item.id);
          if(remaining.some(x=>x.id===record.id))throw Error('Идентификатор сборки повторяется.');
          working.properties.special_assemblies.items=[...remaining,copy(record)];outcomes.push({item,status:'ready',assembly:copy(record)});
        }else outcomes.push({item,status:'skipped',message:data.message||'Изделие не построено.'});
      }catch(error){if(cancelled())return {cancelled:true};outcomes.push({item,status:'error',message:error.message});}
      progress({outcomes,index:outcomes.length,total:plan.items.length});
    }
    const successful=new Set(outcomes.filter(x=>x.status==='ready').map(x=>x.item.id));
    const previous=list(project?.properties?.special_installations?.items),proposed=working.properties.special_installations;
    const installations={...copy(proposed),items:[...copy(previous.filter(x=>!successful.has(x.id))),...copy(proposed.items.filter(x=>successful.has(x.id)))]};
    return {initial,outcomes,ready:successful.size,installations,assemblies:copy(working.properties.special_assemblies)};
  }
  function applyPrepared(project,result){if(!result?.ready||fingerprint(project)!==result.initial)throw Error('Проект изменился. Пересчитайте изделия перед добавлением.');project.properties||={};project.properties.special_installations=copy(result.installations);project.properties.special_assemblies=copy(result.assemblies);}
  function removeInstallation(project,installationId){
    const installations=project?.properties?.special_installations,records=collection(project);
    if(!Array.isArray(installations?.items)||!installations.items.some(x=>x.id===installationId))throw Error('Установка уже удалена или не найдена.');
    const next=copy(installations);next.items=next.items.filter(x=>x.id!==installationId);records.items=records.items.filter(x=>x.installation_id!==installationId);
    project.properties.special_installations=next;project.properties.special_assemblies=records;
  }
  function create(host,options){
    const make=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el;};
    const button=(text,action,cls='button button-secondary')=>{const el=make('button',cls,text);el.type='button';el.addEventListener('click',action);return el;};
    const svg=(tag,attrs)=>{const el=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs||{}).forEach(([k,v])=>el.setAttribute(k,String(v)));return el;};
    let opened=false,busy=false,applying=false,sequence=0,scope=null,initial='',plan=null,result=null,outcomes=[],selected=null,removing=null;
    const shell=make('section','special-assemblies-workspace'),header=make('header','wm-header'),heading=make('div');
    heading.append(make('h2','','Двери, люки и подсветка'),make('p','wm-help','Настройте изделия для помещения или этажа. Программа соберёт объёмную геометрию, сечения и состав для альбома.'));header.append(heading,button('Закрыть',()=>close()));
    const status=make('p','sa-status');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
    const content=make('div','sa-grid'),settings=make('aside','sa-settings'),main=make('div','sa-main'),fields={},checks={};
    const kinds=make('fieldset','sa-kinds');kinds.append(make('legend','','Что собрать'));
    Object.entries(titles).forEach(([kind,title])=>{const label=make('label','sa-check'),input=make('input');input.type='checkbox';input.checked=defaults.kinds.includes(kind);input.setAttribute('aria-label',title);checks[kind]=input;label.append(input,make('span','',title));kinds.append(label);});settings.append(kinds);
    const note=make('p','wm-help','Начальные размеры — редактируемое проектное решение. Детали из STEP и параметрические части показаны раздельно; корпуса фурнитуры не заменяют модель её внутреннего механизма.');settings.append(note);
    const field=(group,key,label,choices)=>{
      const wrap=make('label','field'),input=make(choices?'select':'input');wrap.append(make('span','',label));input.setAttribute('aria-label',label);
      if(choices)choices.forEach(([value,text])=>{const option=make('option','',text);option.value=value;input.append(option);});
      else{const gap=['bottom_gap_mm','above_door_offset_mm'].includes(key);input.type='number';input.min=gap?'0':key==='hinge_count'?'2':'0.01';input.max=key==='hinge_count'?'4':gap?'100':'1000';input.step=key==='hinge_count'?'1':'any';}
      input.value=String(defaults[group][key]);fields[group+'.'+key]=input;wrap.append(input);return wrap;
    };
    const groups={door:[['frame_depth_mm','Глубина коробки двери, мм'],['frame_section_mm','Ширина стойки двери, мм'],['frame_wall_mm','Стенка коробки, мм'],['leaf_thickness_mm','Толщина полотна двери, мм'],['side_gap_mm','Боковой зазор двери, мм'],['bottom_gap_mm','Зазор под дверью, мм'],['hinge_count','Петель на створку']],hatch:[['frame_depth_mm','Глубина рамы люка, мм'],['frame_section_mm','Ширина рамы люка, мм'],['leaf_thickness_mm','Толщина крышки люка, мм'],['gap_mm','Зазор люка, мм']],lighting:[['housing_width_mm','Ширина корпуса света, мм'],['housing_depth_mm','Глубина корпуса света, мм'],['wall_mm','Стенка корпуса света, мм'],['diffuser_thickness_mm','Толщина рассеивателя, мм']]};
    groups.lighting.unshift(['above_door_offset_mm','От верха проёма до низа корпуса света, мм']);
    for(const [group,entries] of Object.entries(groups)){const box=make('details','sa-parameters');box.append(make('summary','',({door:'Дверь: коробка, полотно, фурнитура',hatch:'Люк: рама и крышка',lighting:'Подсветка: корпус и рассеиватель'})[group]));if(group==='lighting')box.append(field(group,'location','Место подсветки',[['above_doors','Над дверными проёмами'],['wall_top','Верх смонтированных стен']]));entries.forEach(([key,label])=>box.append(field(group,key,label)));settings.append(box);}
    const replaceLabel=make('label','sa-check'),replace=make('input');replace.type='checkbox';replace.setAttribute('aria-label','Заменить существующие настройки изделий в выбранной области');replaceLabel.append(replace,make('span','','Заменить существующие настройки в этой области'));settings.append(replaceLabel);
    const planButton=button('Рассчитать изделия',calculate,'button button-secondary');
    const rail=make('div','sa-items');main.append(rail);content.append(settings,main);
    const footer=make('footer','ma-footer'),buildButton=button('Собрать изделия',build,'button button-primary'),applyButton=button('Добавить изделия в проект',apply,'button button-primary'),stopButton=button('Остановить очередь',stop);footer.append(planButton,stopButton,buildButton,applyButton);
    const discard=make('div','ma-discard');discard.hidden=true;discard.append(make('p','','Результаты ещё не добавлены. Закрыть без добавления?'),button('Закрыть без добавления',()=>close({force:true})),button('Продолжить работу',()=>{discard.hidden=true;}));shell.append(header,status,content,discard,footer);host.replaceChildren(shell);
    const current=()=>options.getProject?.(),stale=()=>fingerprint(current())!==initial;
    const controls=[...Object.values(fields),...Object.values(checks),replace];
    controls.forEach(control=>{const invalidate=()=>{if(busy||result?.ready)return;plan=null;result=null;outcomes=[];selected=null;render();status.textContent='Параметры изменены. Нажмите «Рассчитать изделия».';};control.addEventListener('change',invalidate);control.addEventListener('input',invalidate);});
    function config(){const value=copy(defaults);value.kinds=Object.keys(checks).filter(k=>checks[k].checked);for(const [path,input] of Object.entries(fields)){const [g,k]=path.split('.');value[g][k]=k==='location'?input.value:Number(input.value);}return configuration(value);}
    function update(){const changed=opened&&stale();controls.forEach(c=>{c.disabled=busy||Boolean(result?.ready);});planButton.disabled=!opened||busy||Boolean(result?.ready)||changed;buildButton.disabled=!opened||busy||!plan?.items?.length||Boolean(result)||changed;applyButton.hidden=!result?.ready;applyButton.disabled=busy||!result?.ready||changed;stopButton.hidden=!busy;}
    function notes(parent,title,values){if(!list(values).length)return;const details=make('details','ma-notes');details.append(make('summary','',title));values.forEach(v=>details.append(make('p','wm-help',typeof v==='string'?v:v.message||JSON.stringify(v))));parent.append(details);}
    function picture(snapshot){const data=snapshot?.result||snapshot?.geometry_snapshot?.result;const b=data?.bounds_mm;if(!b)return make('p','wm-help','Сечение ещё не построено.');const pad=Math.max(...b.size,1)*.06;const pic=svg('svg',{class:'ma-preview',role:'img','aria-label':'Точное сечение изделия',viewBox:`${b.min[0]-pad} ${-b.max[1]-pad} ${Math.max(b.size[0],1)+pad*2} ${Math.max(b.size[1],1)+pad*2}`});list(data.components).forEach(c=>list(c.paths).forEach(points=>{if(points.length)pic.append(svg('path',{d:points.map((p,i)=>`${i?'L':'M'}${p[0]} ${-p[1]}`).join(' '),stroke:'#4f46a0',fill:'none','stroke-width':1.2,'vector-effect':'non-scaling-stroke'}));}));return pic;}
    function render(){
      main.replaceChildren();rail.replaceChildren();main.append(rail);
      const records=collection(current()).items,stored=list(current()?.properties?.special_installations?.items);
      const entries=[...list(plan?.items),...stored.filter(x=>!list(plan?.items).some(i=>i.id===x.id))].map(item=>({...item,record:records.find(x=>x.installation_id===item.id)}));
      if(stored.length)main.prepend(make('p','wm-help','Список включает сохранённые изделия всего проекта. Новые изделия рассчитываются только для выбранной области; настройки вне неё не заменяются.'));
      entries.forEach(item=>{const outcome=outcomes.find(x=>x.item.id===item.id),btn=button(item.label||titles[item.kind]||'Изделие',()=>{selected=item.id;render();},'ma-item');btn.setAttribute('aria-pressed',String(selected===item.id));btn.append(make('span','ma-item-status',outcome?.status==='ready'||list(plan?.saved_ids).includes(item.id)?'Сборка готова':outcome?.status==='error'?'Ошибка построения':outcome?.status==='skipped'?'Не построено':item.record?'Сохранено · проверить актуальность':'Ожидает сборки'));rail.append(btn);});
      const outcome=outcomes.find(x=>x.item.id===selected),item=entries.find(x=>x.id===selected),record=outcome?.assembly||item?.record;
      if(item){main.append(make('h3','',item.label||titles[item.kind]));const floor=list(current()?.buildings).flatMap(b=>list(b.floors)).find(f=>f.id===item.floor_id);notes(main,'Где применяется',item.locations||[floor?.name||'Сохранённая установка']);if(item.record&&outcome?.status!=='ready'&&!list(plan?.saved_ids).includes(item.id))main.append(make('p','wm-help','Ниже сохранённая геометрия. Нажмите «Рассчитать изделия», чтобы проверить её актуальность для этой области.'));}
      if(record){const sections=list(record.sections);sections.forEach(section=>{main.append(make('h4','',section.label||section.name||'Сечение'),picture(section.geometry_snapshot||section.snapshot||section));});notes(main,'Состав изделия',list(record.bodies).map(x=>`${x.kind==='step'?'STEP':'По размерам'} · ${x.label||x.instance_id||x.id}`));notes(main,'Принятые размеры и условия',record.assumptions);notes(main,'Исходные модели',list(record.source_models).map(x=>x.filename||x.component_code));}
      else if(outcome?.message)main.append(make('p','ma-error',outcome.message));
      else main.append(make('p','wm-help',plan?.items?.length?'Выберите изделие и нажмите «Собрать изделия».':'Выберите состав и рассчитайте изделия для выбранной области.'));
      if(selected&&list(current()?.properties?.special_installations?.items).some(x=>x.id===selected)&&!busy&&!result?.ready){
        if(removing===selected){main.append(make('p','wm-help','Удалить настройку и сохранённую сборку этого изделия? Сам проём и стена останутся. Правку можно отменить.'),button('Удалить изделие',()=>remove(selected)),button('Оставить изделие',()=>{removing=null;render();}));}
        else main.append(button('Убрать изделие из проекта',()=>{removing=selected;render();}));
      }
      notes(main,'Не включены в сборку',list(plan?.skipped).map(x=>`${x.label||'Изделие'}: ${x.message||x.reason||''}`));notes(main,'Примечания',plan?.warnings);update();
    }
    async function calculate(){
      if(!opened||busy||result?.ready||stale())return false;
      const token=++sequence;busy=true;update();status.textContent='Рассчитываем состав и места изделий…';
      try{
        const value=await options.request('/special-assemblies/plan',{method:'POST',body:{project:W.requestProject(current()),scope:copy(scope),configuration:config(),replace_existing:replace.checked}});
        if(token!==sequence||!opened||stale())return false;
        plan=checkedPlan(current(),value);result=null;outcomes=[];
        selected=plan.items[0]?.id||collection(current()).items.find(x=>list(plan.saved_ids).includes(x.id)||list(plan.saved_ids).includes(x.installation_id))?.installation_id;
        status.textContent=`К сборке: ${plan.items.length}. Сохранено подходящих: ${list(plan.saved_ids).length}.`;return true;
      }catch(error){if(token===sequence)status.textContent=error.message;return false;}
      finally{if(token===sequence){busy=false;render();}}
    }
    async function build(){if(!opened||busy||!plan?.items?.length||result||stale())return false;const token=++sequence;busy=true;update();try{const value=await prepare(current(),plan,options.request,data=>{if(token===sequence){outcomes=data.outcomes;status.textContent=data.item?`Собирается ${data.index+1} из ${data.total}: ${data.item.label}`:`Обработано ${data.index} из ${data.total}`;render();}},()=>token!==sequence||!opened||stale());if(token!==sequence||value.cancelled)return false;result=value;outcomes=value.outcomes;status.textContent=`Готово изделий: ${value.ready}. Добавление в проект — одной отменяемой правкой.`;return true;}catch(error){status.textContent=error.message;return false;}finally{if(token===sequence){busy=false;render();}}}
    function apply(){if(busy||!result?.ready||stale())return false;applying=true;try{const ok=options.mutate('добавление дверей, люков и подсветки',project=>applyPrepared(project,result));if(ok===false)return false;result=null;initial=fingerprint(current());plan=null;status.textContent='Изделия добавлены в черновик. Сохраните проект для выпуска документации.';render();return true;}catch(error){status.textContent=error.message;return false;}finally{applying=false;}}
    function remove(id){if(busy||result?.ready||stale()||removing!==id)return false;applying=true;try{if(options.mutate('удаление специального изделия',project=>removeInstallation(project,id))===false)return false;initial=fingerprint(current());plan=null;outcomes=[];selected=null;removing=null;status.textContent='Изделие убрано из черновика. Проём и стена сохранены.';render();return true;}catch(error){status.textContent=error.message;return false;}finally{applying=false;}}
    function stop(){sequence++;busy=false;result=null;outcomes=[];plan=null;status.textContent='Очередь остановлена. Проект не изменён.';render();}
    function open(value){if(hasDraft())return false;if(!value||!['space','floor'].includes(value.kind)||!value.id)return false;opened=true;scope=copy(value);initial=fingerprint(current());plan=null;result=null;outcomes=[];selected=list(current()?.properties?.special_installations?.items)[0]?.id||null;removing=null;replace.checked=false;discard.hidden=true;status.textContent=value.kind==='space'?'Область: выбранное помещение.':'Область: выбранный этаж.';render();return true;}
    function hasDraft(){return opened&&(busy||Boolean(result?.ready));}
    function close({force=false}={}){if(!force&&hasDraft()){discard.hidden=false;return false;}sequence++;opened=false;busy=false;result=null;plan=null;discard.hidden=true;options.onClose?.();return true;}
    function refresh(){if(opened&&!applying&&stale()){sequence++;busy=false;status.textContent='Проект изменился. Закройте окно и заново рассчитайте изделия.';update();}}
    return {open,close,refresh,hasDraft,calculate,build,apply,stop,dispose:()=>close({force:true})};
  }
  return {defaults,configuration,collection,checkedPlan,prepare,applyPrepared,removeInstallation,create};
});
