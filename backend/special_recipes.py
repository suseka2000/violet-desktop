"""Full-size project-designed products, never reverse-engineered vendor goods.

The reusable angle is an unchanged, exact STEP. Frames/leaf/hardware envelopes
are explicitly parameterized solids. Envelope hardware is not a hinge/lock
mechanism and its kinematics, load capacity and fixing require engineering.
"""
from __future__ import annotations

from .assembly_recipes import CALIBRATIONS, IDENTITY

ANGLE = {k: CALIBRATIONS["angle20"][k] for k in ("source_id", "filename", "component_code")}


def _box(identifier, label, role, size, origin, cutouts=()):
    value={"id":identifier,"label":label,"role":role,"size_mm":list(size),"rotation":IDENTITY,"translation_mm":list(origin)}
    if cutouts:value["cutouts"]=list(cutouts)
    return value


def _cut(identifier, size, origin):
    return {"id":identifier,"size_mm":list(size),"origin_mm":list(origin)}


def _angle(identifier, x, y, z, right=False):
    return {"id":identifier,"label":"Реальный STEP: уголок 20x20x1, L=30 мм","source_id":ANGLE["source_id"],"component_ids":[1],
            "rotation":[[0,0,-1],[-1,0,0],[0,1,0]] if right else [[0,0,1],[1,0,0],[0,1,0]],
            "translation_mm":[x,y,z]}


def recipe(installation):
    kind=installation.kind
    cfg=installation.configuration
    t=installation.target_geometry
    boxes=[];placements=[];sections=[];materials=[];notes=[]
    if kind in {"concealed_door","hatch"}:
        c=cfg[kind if kind=="hatch" else "door"]
        W,H=float(t["width_mm"]),float(t["height_mm"])
        D,S=float(c["frame_depth_mm"]),float(c["frame_section_mm"])
        metal=float(c.get("frame_wall_mm",2))
        gap=float(c.get("side_gap_mm",c.get("gap_mm",3)))
        bottom=float(c.get("bottom_gap_mm",gap))
        thickness=float(c["leaf_thickness_mm"])
        margin=10.0
        frame_width=float(t.get("frame_width_mm") or W-2*margin)
        if frame_width>W:raise ValueError("Заданная коробка шире проёма. Уточните монтажный габарит, не наружные наличники.")
        margin=(W-frame_width)/2
        bottom_frame=S if kind=="hatch" else 0
        clear_width=frame_width-2*S
        leaf_width=float(t.get("leaf_width_mm") or clear_width-2*gap)
        if leaf_width>clear_width-2*gap+1e-6:raise ValueError("Полотна не помещаются между стойками коробки с выбранными зазорами")
        side=(clear_width-leaf_width)/2
        leaf_x=margin+S+side
        leaf_bottom=bottom_frame+bottom
        leaf_top=H-margin-S-gap
        leaf_height=leaf_top-leaf_bottom
        if min(clear_width,leaf_width,leaf_height)<100:raise ValueError("Габариты проёма и коробки не оставляют минимум 100 мм для полотна")
        if D<thickness+8:raise ValueError("Глубина коробки должна оставлять не менее 8 мм за полотном")
        if S<=2*metal+10:raise ValueError("Сечение коробки слишком мало для стенок и монтажной полости")
        if D<=2*metal+10:raise ValueError("Глубина коробки слишком мала для стенок и монтажной полости")
        double=kind=="concealed_door" and t.get("handing")=="double"
        right=kind=="concealed_door" and t.get("handing")=="right"
        count=int(c.get("hinge_count",2))
        count=max(2,count)
        hinge_levels=[leaf_bottom+leaf_height*(i+1)/(count+1) for i in range(count)]
        hardware_y=min(10.,thickness/4)
        hardware_depth=min(24.,thickness-hardware_y-2)
        hardware_h=min(60.,leaf_height/(count+1)*.5)
        if hardware_depth<4:raise ValueError("Полотно слишком тонкое для выбранного параметрического габарита фурнитуры")
        jambs=[]
        for side_name,origin in (("left",margin),("right",W-margin-S)):
            cuts=[_cut("hollow",[S-2*metal,D-2*metal,H+1],[metal,metal,-.5])]
            hinged=(side_name==("right" if right else "left")) or double
            if hinged:
                for i,z in enumerate(hinge_levels):
                    cuts.append(_cut(f"hinge-{i}",[metal+1,hardware_depth+2,hardware_h+2],
                                     [S-metal-.5 if side_name=="left" else -.5,hardware_y-1,z-hardware_h/2-1]))
            jambs.append(_box("frame-"+side_name,"Проектная скрытая стойка "+side_name,"frame",[S,D,H-margin-S],[origin,0,0],cuts))
        boxes+=jambs
        boxes.append(_box("frame-head","Проектная верхняя перекладина","frame",[frame_width,D,S],[margin,0,H-margin-S],
                          [_cut("hollow",[frame_width+1,D-2*metal,S-2*metal],[-.5,metal,metal])]))
        if kind=="hatch":
            boxes.append(_box("frame-sill","Проектная нижняя перекладина люка","frame",[clear_width,D,S],[margin+S,0,0],
                              [_cut("hollow",[clear_width+1,D-2*metal,S-2*metal],[-.5,metal,metal])]))
        # Stops touch the back of the closed leaf, without pretending to model
        # a gasket or a proprietary frame extrusion.
        for side_name,x in (("left",margin+S),("right",W-margin-S-8)):
            boxes.append(_box("stop-"+side_name,"Проектный притвор "+side_name,"frame",[8,2,leaf_height-8],[x,thickness,leaf_bottom]))
        boxes.append(_box("stop-head","Проектный верхний притвор","frame",[clear_width,2,8],[margin+S,thickness,leaf_top-8]))
        leaf_parts=2 if double else 1
        unit_width=(leaf_width-gap)/2 if double else leaf_width
        for part in range(leaf_parts):
            px=leaf_x+part*(unit_width+gap)
            hinged_right=right if not double else part==1
            mortise_depth=min(12.,unit_width/5)
            cuts=[]
            for i,z in enumerate(hinge_levels):
                cut_x=unit_width-mortise_depth if hinged_right else 0
                cuts.append(_cut(f"hinge-{i}",[mortise_depth+.5,hardware_depth+2,hardware_h+2],
                                 [cut_x if hinged_right else -.5,hardware_y-1,z-leaf_bottom-hardware_h/2-1]))
                frame_x=W-margin-S if hinged_right else margin+S
                hx=px+unit_width-mortise_depth if hinged_right else px
                boxes.append(_box(f"hinge-leaf-{part}-{i}","Параметрический габарит части петли в полотне","hardware_envelope",[mortise_depth,hardware_depth,hardware_h],[hx,hardware_y,z-hardware_h/2]))
                fx=frame_x if hinged_right else frame_x-metal
                boxes.append(_box(f"hinge-frame-{part}-{i}","Параметрический габарит части петли в коробке","hardware_envelope",[metal,hardware_depth,hardware_h],[fx,hardware_y,z-hardware_h/2]))
                # Coupling occupies only the free reveal; it is expressly a
                # mechanism envelope, not an invented concealed hinge linkage.
                bx=px+unit_width if hinged_right else frame_x
                coupling=frame_x-(px+unit_width) if hinged_right else px-frame_x
                boxes.append(_box(f"hinge-gap-{part}-{i}","Габарит кинематической связи петли (не механизм)","hardware_envelope",[coupling,4,8],[bx,hardware_y+hardware_depth/2-2,z-4]))
            # A mortised latch envelope remains clear of the hinge pockets.
            lock_z=leaf_bottom+leaf_height*.53
            lock_depth=min(20.,unit_width/6)
            lock_x=0 if hinged_right else unit_width-lock_depth
            cuts.append(_cut("lock",[lock_depth+.5,hardware_depth+2,42],[lock_x if not hinged_right else -.5,hardware_y-1,lock_z-leaf_bottom-21]))
            boxes.append(_box(f"lock-{part}","Параметрический габарит замка/защёлки (без механизма)","hardware_envelope",[lock_depth,hardware_depth,40],[px+lock_x,hardware_y,lock_z-20]))
            boxes.append(_box(f"leaf-{part}","Проектное полотно "+str(part+1),"leaf",[unit_width,thickness,leaf_height],[px,0,leaf_bottom],cuts))
        # Small native angle specimens are actual local frame brackets, never
        # stretched to become a continuous jamb or claimed as approved anchors.
        if installation.source_models:
            for i,z in enumerate((H*.25,H*.75)):
                placements.extend([_angle(f"angle-left-{i}",margin,D-20,z),_angle(f"angle-right-{i}",W-margin,D,z,True)])
        sections=[{"id":"hinge","label":"А-А. Горизонтальный разрез на уровне фурнитуры","axis":"z","offset_mm":hinge_levels[0]},
                  {"id":"vertical","label":"Б-Б. Вертикальный разрез через полотно и верх коробки","axis":"x","offset_mm":leaf_x+unit_width*.4},
                  {"id":"elevation","label":"В-В. Продольный разрез через полотно","axis":"y","offset_mm":max(2.,thickness/2)}]
        notes=[
            "Коробка, притвор и полотно - полноценные параметрические твёрдые тела проектного изделия, не восстановленный заводской профиль Виолет.",
            f"Коробка центрирована в проёме: ширина {frame_width:g} мм; стойка {S:g} x {D:g} мм, стенка {metal:g} мм. Полотно {leaf_width:g} x {leaf_height:g} x {thickness:g} мм; боковой зазор {side:g} мм.",
            "Габариты петель и замков показаны отдельными телами в настоящих выборках. Внутренняя кинематика, крепёжные отверстия, прочность и ресурс механизмов не определены.",
            "Положение показано закрытым. Проверка объёмных пересечений не является проверкой траектории открывания, огнестойкости, акустики или несущей способности.",
            "Уголки STEP 20x20x1 длиной 30 мм не растянуты: применены локально как проектные кронштейны. Анкеры, отверстия и основание требуют отдельного подбора." if installation.source_models else "Точный STEP уголка недоступен: кронштейны не добавлены и требуют отдельного подбора.",
            "Уплотнитель, пена, клей и дверная отделка не назначаются как конкретный сертифицированный продукт без его характеристик.",
        ]
        if double:notes.append("Два полотна приняты равными по ширине. Средний зазор равен боковому; это явное проектное допущение, а не подтверждение неравных створок.")
        if t.get("handing") in (None,"unspecified"):notes.append("Петлевая сторона предварительно принята у левого края локального вида; направление открывания неизвестно и не подменяется утверждённым.")
        context=t.get("panel_context")
        if context:
            fragments=[]
            pt=float(context["thickness_mm"])
            for name,x,cw in (("left",-context["left_width_mm"],context["left_width_mm"]),("right",W,context["right_width_mm"])):
                if cw>0:fragments.append(_box("panel-"+name,"Контекст облицовки у проёма: "+name,"panel",[cw,pt,H],[x,0,0]))
            if context["head_height_mm"]>0:fragments.append(_box("panel-head","Контекст облицовки над проёмом","panel",[W,pt,context["head_height_mm"]],[0,0,H]))
            if len(boxes)+len(placements)+len(fragments)<=32:
                boxes+=fragments
                notes.append(f"Облицовка взята из настройки выбранной стороны стены: {context['material']}, {pt:g} мм. Показаны локальные твёрдотельные фрагменты до 150 мм, не полная раскладка; лицевая плоскость предварительно совмещена с полотном. Посадку скрытой коробки и обработку кромок проверить.")
            else:notes.append("Контекст облицовки не включён в STEP из-за лимита 32 тел. Для этой многокомпонентной двери он остаётся на развёртке стены.")
    else:
        c=cfg["lighting"]
        length=float(t["width_mm"])
        width,depth,metal=float(c["housing_width_mm"]),float(c["housing_depth_mm"]),float(c["wall_mm"])
        diffuser=float(c["diffuser_thickness_mm"])
        if width<=2*metal+4 or depth<=2*metal+diffuser+2:raise ValueError("Корпус подсветки не оставляет полость под плату и рассеиватель")
        boxes.append(_box("housing","Проектный U-корпус подсветки","lighting_housing",[length,width,depth],[0,0,0],
                          [_cut("channel",[length+1,width-2*metal,depth-metal+.5],[-.5,metal,metal])]))
        boxes.append(_box("diffuser","Параметрический рассеиватель","diffuser",[length,width-2*metal,diffuser],[0,metal,depth-diffuser]))
        boxes.append(_box("led-envelope","Габарит платы LED (не электрическая модель)","circuit_envelope",[length-2,width-2*metal-2,1],[1,metal+1,metal]))
        if installation.source_models:
            for i,x in enumerate((max(15.,length*.2),min(length-15.,length*.8))):
                # Source x->depth, y->run and z->normal: angle touches bottom
                # of U-channel at z=0 and sits behind it, not inside the LEDs.
                placements.append({"id":f"angle-{i}","label":"STEP уголок крепления корпуса","source_id":ANGLE["source_id"],"component_ids":[1],
                                   "rotation":[[0,1,0],[1,0,0],[0,0,-1]],"translation_mm":[x,0,-20]})
        # Product-local Y=0 is the finished wall face; the actual bracket's
        # farthest Y is 20 mm. Place the entire surface-mounted assembly in
        # front of that plane, never hidden inside a solid cladding panel.
        projection=max(width,20.)
        for body in boxes+placements:body["translation_mm"][1]-=projection
        sections=[{"id":"cross","label":"А-А. Поперечное сечение корпуса и рассеивателя","axis":"x","offset_mm":length/2},
                  {"id":"longitudinal","label":"Б-Б. Продольный разрез канала","axis":"y","offset_mm":width/2-projection}]
        notes=["Корпус, рассеиватель и габарит LED-платы построены параметрически в фактическом масштабе. Это не модель или артикул V-LED и не замена V-Base light.",
               f"Принято сечение корпуса {width:g} x {depth:g} мм со стенкой {metal:g} мм и рассеивателем {diffuser:g} мм; длина {length:g} мм по выбранному месту.",
               "Проверить температуру, мощность, электропитание, сечение проводов, IP, обслуживание и пожарные требования. Светотехнический и электротехнический расчёт не выполнены.",
               "Два локальных кронштейна взяты из точного STEP уголка 20x20x1, L=30 мм. Их количество и положение - проектное размещение, не подтверждённая несущая способность." if installation.source_models else "Точный STEP уголка недоступен: кронштейны не добавлены и требуют отдельного подбора."]
        notes.append(f"Корпус принят накладным перед лицевой плоскостью стены Y=0: вынос {projection:g} мм. Это не скрытый паз или утопленный светильник; облицовка не вырезается произвольно.")
        if cfg["lighting"]["location"]=="above_doors":notes.append(f"Низ корпуса расположен на {cfg['lighting'].get('above_door_offset_mm',20):g} мм выше верха проёма. Кронштейн занимает 20 мм вниз от корпуса и не опускается в габарит коробки.")
    if len(boxes)+len(placements)>32:raise ValueError("В специальной сборке больше 32 тел; уменьшите количество петель")
    for body in boxes:
        role=body["role"]
        material={"frame":"Проектный металл (марка не задана)","leaf":"Проектный материал полотна (не специфицирован)","panel":"Материал облицовки из проекта: "+str(t.get("panel_context",{}).get("material","не задан")),"hardware_envelope":"Габарит фурнитуры; материал не назначен","lighting_housing":"Проектный алюминиевый корпус","diffuser":"Проектный светорассеивающий материал","circuit_envelope":"Габарит LED-платы; изделие не выбрано"}[role]
        materials.append({"instance_id":body["id"],"label":body["label"],"role":role,"material":material,"origin":"project_assumption","quantity":1,"size_mm":body["size_mm"]})
    for body in placements:
        materials.append({"instance_id":body["id"],"label":body["label"],"role":"bracket","material":"Металл: марка не подтверждена STEP","origin":"source_step","source_id":body["source_id"],"quantity":1,"size_mm":[20,30,20]})
    return {"spec":{"schema_version":"assembled-section-request/1.0","placements":placements,"boxes":boxes,
                    "section":{k:sections[0][k] for k in ("axis","offset_mm")},"collision_tolerance_mm3":.001},
            "sections":sections,"materials":materials,"assumptions":notes}
