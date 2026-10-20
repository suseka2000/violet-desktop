"""Portable actual special-product sections, never replacement sketch geometry.

Only current, validated saved assemblies reach a view. Native geometry kernels
are intentionally not called while exporting a document or its portable ZIP.
"""
from __future__ import annotations

from .scene import BLUE, GRAY, INK, wrap


def special_entries(project, *, derived_assemblies=False):
    # The document's outer workspace boundary checks the complete budget.
    # No special product means no references or assembly payload to validate.
    if not {"special_installations","special_assemblies"}.intersection(project.properties):
        return []
    from ..special_assemblies import special_document_entries
    return [{**entry, "mark":f"СУ{index:03d}"} for index,entry in enumerate(special_document_entries(project,derived_assemblies=derived_assemblies),1)]


def section_entries(entries):
    """One globally stable mark per saved cutting plane; no stale geometry."""
    from ..cad_sections import SectionSnapshot
    result=[]
    for entry in entries:
        if entry["status"]!="ready":
            continue
        for index,section in enumerate(entry["assembly"]["sections"]):
            view={"mark":entry["mark"]+"-"+"АБВГ"[index],"special":entry,
                  "section":section,"snapshot":SectionSnapshot.model_validate(section["geometry_snapshot"])}
            result.append(view)
            # The full door is useful for orientation but hides the box at
            # 1:20. An enlarged *clipped existing cut*, not a new cut or an
            # invented profile, shows its joint in the same album sequence.
            snapshot=view["snapshot"].result
            if entry["kind"] in {"concealed_door","hatch"} and snapshot.axis in {"x","z"}:
                bodies={b["id"]:b for b in entry["assembly"]["bodies"]}
                handing=entry["installation"]["target_geometry"].get("handing")
                ids=(["frame-left","frame-right"] if handing=="double" else
                     ["frame-right" if handing=="right" and entry["kind"]=="concealed_door" else "frame-left"]) if snapshot.axis=="z" else ["frame-head"]
                for part,instance_id in enumerate(ids,1):
                    component=next((c for c in snapshot.components if c.paths and bodies[c.id].get("instance_id")==instance_id),None)
                    if not component:continue
                    points=[p for path in component.paths for p in path]
                    low=[min(p[a] for p in points)-25 for a in (0,1)]
                    high=[max(p[a] for p in points)+25 for a in (0,1)]
                    result.append({**view,"mark":view["mark"]+f".{part}","clip_bounds":(low,high),"fragment_body_id":component.id,
                                   "fragment_label":"Увеличенный фрагмент стойки" if snapshot.axis=="z" else "Увеличенный фрагмент верха коробки"})
    return result


def _closed_loops(paths):
    """Join saved sampled edges only; no synthetic closure across a gap."""
    from shapely.geometry import MultiLineString
    from shapely.ops import linemerge
    if not paths:return []
    merged=linemerge(MultiLineString(paths))
    lines=[merged] if merged.geom_type=="LineString" else list(merged.geoms)
    return [list(line.coords) for line in lines if line.is_ring]


def _graphics(entry):
    from shapely.geometry import Polygon,GeometryCollection,LineString,box
    result=entry["snapshot"].result
    section_bodies={b["id"]:b for b in entry["section"].get("bodies",[])}
    clip=box(*entry["clip_bounds"][0],*entry["clip_bounds"][1]) if entry.get("clip_bounds") else None
    values=[]
    for component in result.components:
        if not component.paths:continue
        loops=section_bodies.get(component.id,{}).get("section_loops",[]) or _closed_loops(component.paths)
        paths=component.paths
        if clip is not None:
            paths=[]
            for path in component.paths:
                cut=LineString(path).intersection(clip)
                pieces=[cut] if cut.geom_type=="LineString" else getattr(cut,"geoms",[])
                paths.extend([list(piece.coords) for piece in pieces if piece.geom_type=="LineString" and not piece.is_empty])
            region=GeometryCollection()
            for loop in loops:
                polygon=Polygon(loop)
                if polygon.is_valid:region=region.symmetric_difference(polygon)
            cut=region.intersection(clip)
            polygons=[cut] if cut.geom_type=="Polygon" else getattr(cut,"geoms",[])
            loops=[list(ring.coords) for polygon in polygons if polygon.geom_type=="Polygon" and not polygon.is_empty
                   for ring in [polygon.exterior,*polygon.interiors]]
        if paths or loops:values.append({"id":component.id,"label":component.label,"paths":paths,"loops":loops})
    low,high=entry.get("clip_bounds",(result.bounds_mm.min,result.bounds_mm.max))
    return values,low,high


def draw_special_view(sheet, entry, box, num):
    """Dimensioned native saved section with source-local body numbering."""
    from .assemblies import _hatch
    from .section_dimensions import draw_section_dimensions
    x,y,w,h=box
    special,section=entry["special"],entry["section"]
    result=entry["snapshot"].result
    subtitle=entry.get("fragment_label",section["label"])
    title=wrap(f"{entry['mark']}. {special['label']} / {subtitle}",w-8,3,True)
    for index,line in enumerate(title):
        sheet.text(x+3,y+5+index*4.4,line,3,INK,True)
    top=y+9+len(title)*4.4
    components,low,high=_graphics(entry)
    bw,bh=high[0]-low[0],high[1]-low[1]
    gw,gh=w*.60-24,h-(top-y)-39
    if min(gw,gh)<15:
        raise ValueError("Сечение изделия не помещается")
    required=max(bw/gw,bh/gh,.05)
    scale=next((s for s in (.05,.1,.2,.5,1,2,4,5,10,20,25,50,100) if s>=required),required)
    ox,oy=x+20+(gw-bw/scale)/2,top+4+(gh-bh/scale)/2
    transform=lambda p:(ox+(p[0]-low[0])/scale,oy+(high[1]-p[1])/scale)
    bodies={body["id"]:body for body in special["assembly"]["bodies"]}
    lx,ly,lw=x+w*.63,top+5,w*.37-6
    sheet.text(lx,ly,f"М {num(1/scale)}:1" if scale<1 else f"М 1:{num(scale)}",2.8,BLUE,True)
    ly+=6
    number_positions=[]
    for component in components:
        body=bodies[component["id"]]
        color=BLUE if body.get("role")=="panel" else INK
        _hatch(sheet,[[transform(p) for p in loop] for loop in component["loops"]],"#aab4bf",1 if body.get("role")=="panel" else -1)
        for path in component["paths"]:
            sheet.polygon([transform(p) for p in path],color,weight=.22,closed=False)
    # Labels are the final layer, so another body's hatch cannot cover them.
    for component in components:
        body=bodies[component["id"]]
        color=BLUE if body.get("role")=="panel" else INK
        label=f"{component['id']}. {body.get('label') or component['label']}"
        lines=wrap(label,lw,2.5)
        if ly+len(lines)*3.6>y+h-27:
            raise ValueError("Состав изделия не помещается в ячейке")
        for line in lines:
            sheet.text(lx,ly,line,2.5,color);ly+=3.6
        ly+=1.5
        px,py=transform((component["paths"] or component["loops"])[0][0])
        candidates=[]
        for radius in (5,9,14,20,28):
            for dx,dy in ((-1,-1),(1,-1),(-1,1),(1,1),(0,-1),(0,1),(-1,0),(1,0)):
                cx,cy=px+dx*radius,py+dy*radius
                if not (x+8<=cx<=lx-5 and top+3<=cy<=y+h-31):continue
                if any((cx-a)**2+(cy-b)**2<25 for a,b in number_positions):continue
                candidates.append((radius,cx,cy))
        if not candidates:raise ValueError("Номера тел не помещаются в сечении")
        _,cx,cy=min(candidates)
        number_positions.append((cx,cy))
        sheet.line(px,py,cx,cy,GRAY,.1)
        sheet.circle(cx,cy,2.2,color,"#ffffff",.15)
        sheet.text(cx,cy+.9,str(component["id"]),2.5,color,anchor="middle")
    if entry.get("clip_bounds"):
        # This border is a viewport boundary, never a manufacturing dimension.
        for a,b in [((ox,oy),(ox+bw/scale,oy)),((ox+bw/scale,oy),(ox+bw/scale,oy+bh/scale)),
                    ((ox+bw/scale,oy+bh/scale),(ox,oy+bh/scale)),((ox,oy+bh/scale),(ox,oy))]:sheet.line(*a,*b,GRAY,.1,True)
        sheet.text(x+3,y+h-22,"Фрагмент сохранённого разреза; пунктир - граница обрезки.",2.5,GRAY)
    else:
        draw_section_dimensions(sheet,
            [[transform(p) for p in path] for component in components
             for path in (component["paths"] or component["loops"])], num(bw), num(bh),
            bounds=(x+3, top-7, lx-3, y+h-24))
    sheet.text(x+3,y+h-17,f"Плоскость {result.axis.upper()}={num(result.offset_mm)} мм; проекция {' / '.join(result.projection_axes).upper()}.",2.5,GRAY)
    sheet.paragraph(x+3,y+h-10,"Проектное изделие. Состав, материалы и принятые размеры: см. ведомость "+special["mark"]+".",w-8,2.5,GRAY,leading=3.6)
    sheet.references.extend([special["id"],special["assembly"]["id"],special["wall_id"],special["face_id"],section["id"]])


def draw_special_locations(sheet, entries, transform, wall, num):
    """Locate the actual installation; do not imply opening kinematics."""
    for index,entry in enumerate(entries):
        target=entry["installation"]["target_geometry"]
        start=float(target.get("offset_mm",0));span=float(target.get("width_mm",0))
        if target.get("view_side")=="right":start=wall["length_mm"]-start-span
        xx=start+span
        zz=float(target.get("installation_bottom_mm",float(target.get("sill_mm",0))+float(target.get("height_mm",0))/2))
        zz=min(wall["height_mm"],max(0,zz))
        px,py=transform(xx,zz)
        # Short local leaders, not sheet-spanning spaghetti. The mark is the
        # product, while suffixes A/B/C identify its saved cutting planes.
        if transform(wall["length_mm"],zz)[0]-px<18:
            px,py=transform(start,zz)
            lx=px-19
        else:lx=px+5
        ly=py-6-index%2*5
        sheet.line(px,py,lx,ly,GRAY,.12)
        label=entry["mark"]
        sheet.rect(lx-1,ly-3.4,14,4.8,"#ffffff","#ffffff",.1)
        sheet.text(lx,ly,label,2.5,BLUE,True)


def draw_special_context(sheet, item, box, project, opening_marks, num):
    """Architectural elevation for a product with no panel mounting assigned."""
    from . import scale_for
    from .levels import draw_levels,view_levels
    from .openings import draw_elevation_opening,vertical_dimension
    mark,preview=item
    wall=preview["wall"]
    x,y,w,h=box
    title=wrap(f"{mark}. {wall['name']} / специальные изделия",w-8,3.2,True)
    for i,line in enumerate(title):sheet.text(x+3,y+5+i*4.6,line,3.2,INK,True)
    th=len(title)*4.6
    scale=scale_for(wall["length_mm"],wall["height_mm"],w-75,h-55-th)
    dw,dh=wall["length_mm"]/scale,wall["height_mm"]/scale
    ox,oy=x+25+(w-75-dw)/2,y+th+20
    transform=lambda a,b:(ox+a/scale,oy+(wall["height_mm"]-b)/scale)
    sheet.text(x+w-3,y+th+10,f"М 1:{num(scale)}",2.6,BLUE,anchor="end")
    sheet.rect(ox,oy,dw,dh,INK,weight=.25)
    for opening in preview["openings"]:
        draw_elevation_opening(sheet,opening,transform,scale,opening_marks[opening["id"]])
    sheet.dimension(ox,ox+dw,oy+dh+9,oy+dh,num(wall["length_mm"]))
    vertical_dimension(sheet,ox-7,oy,oy+dh,ox,num(wall["height_mm"]))
    draw_special_locations(sheet,preview["specials"],transform,wall,num)
    notes=draw_levels(sheet,view_levels(project,wall["floor_id"],wall.get("space_id")),transform,
                      wall["length_mm"],wall.get("base_mm",0),wall["height_mm"])
    sheet.paragraph(x+3,y+h-9,"Архитектурная основа; раскладка панелей не назначена."+(" "+"; ".join(notes) if notes else ""),w-8,2.5,GRAY)
    sheet.references.extend([wall["id"],wall["face_id"]])


def context_view(project, entry, entries):
    """A lightweight view of the selected face, including its mirrored holes."""
    import math
    floor=next(f for b in project.buildings for f in b.floors if f.id==entry["floor_id"])
    wall=next(w for w in floor.walls if w.id==entry["wall_id"])
    face=next(f for f in wall.faces if f.id==entry["face_id"])
    length=math.hypot(wall.end.x-wall.start.x,wall.end.y-wall.start.y)
    openings=[]
    for opening in floor.openings:
        if opening.wall_id!=wall.id:continue
        value=opening.model_dump(mode="json")
        value.update(x_mm=opening.offset_mm if face.side=="left" else length-opening.offset_mm-opening.width_mm,
                     y_mm=opening.sill_mm)
        # Reuse the engineering view-door conversion for right-side views.
        from ..engineering.openings import opening_details
        value.update(opening_details(opening,face.side))
        openings.append(value)
    return {"special_context":True,"wall":{"id":wall.id,"name":wall.name,"face_id":face.id,"floor_id":floor.id,
        "space_id":face.space_id,"side":face.side,"height_mm":wall.height_mm,"length_mm":length,"base_mm":wall.base_mm},
        "openings":openings,"specials":[e for e in entries if e["wall_id"]==wall.id and e["face_id"]==face.id]}


def add_special_tables(entries, table, num):
    if not entries:return
    table("Специальные изделия / места и состояние",["Изделие","Наименование","Геометрия / разрезы"],
          [[e["mark"],e["label"],"Сохранённые реальные сечения: "+", ".join(s["label"] for s in e["assembly"]["sections"]) if e["status"]=="ready" else e["message"]] for e in entries],
          [1.2,3.5,7.3],"special_products")
    rows,notes=[],{}
    generic_warnings={
        "Составная геометрия не является утверждённым монтажным решением. Проверьте посадки, материалы и крепёж по методичке.",
        "Кривые сечения аппроксимированы: 0,02 мм — цель дискретизации, а не производственный допуск.",
        "Геометрическое сечение — не утверждённый монтажный узел. Штриховки, материалы и размеры требуют проверки.",
        "Кривые представлены приближёнными отрезками; 0,02 мм — цель дискретизации, а не сертифицированный допуск.",
    }
    common_marks=[];deflections=set()
    for entry in entries:
        if entry["status"]!="ready":continue
        record=entry["assembly"]
        by_instance={body["instance_id"]:body["id"] for body in record["bodies"]}
        grouped={}
        for material in record["materials"]:
            key=(material["label"],material["material"],tuple(material.get("size_mm",[])),material["origin"])
            value=grouped.setdefault(key,{"ids":[],"quantity":0})
            value["ids"].append(str(by_instance[material["instance_id"]]));value["quantity"]+=material.get("quantity",1)
        for key,value in grouped.items():
            rows.append([entry["mark"],", ".join(value["ids"]),key[0],key[1]," × ".join(num(v) for v in key[2]),str(value["quantity"]),
                         "Исходный STEP" if key[3]=="source_step" else "Облицовка по проекту" if key[3]=="project_context" else "Проектное тело"])
        for assumption in record["assumptions"]:notes.setdefault(assumption,[]).append(entry["mark"])
        for section in record["sections"]:
            result=section["geometry_snapshot"]["result"]
            deflections.add(result["deflection_mm"])
            if entry["mark"] not in common_marks:common_marks.append(entry["mark"])
            def add_warning(warning,prefix):
                if warning in generic_warnings or warning.endswith("Плоскость не пересекает это тело; на сечении оно не показано."):return
                notes.setdefault(prefix+warning,[]).append(entry["mark"])
            for warning in result.get("warnings",[]):add_warning(warning,section["label"]+": ")
            for body in result["components"]:
                for warning in body.get("warnings",[]):add_warning(warning,section["label"]+f" / тело {body['id']}: ")
    if common_marks:
        notes["Сечения показаны по сохранённой геометрии. Непересекаемые плоскостью тела остаются в ведомости, но не изображаются на данном разрезе. Цель дискретизации кривых: "+", ".join(num(v) for v in sorted(deflections))+" мм, не производственный допуск. Посадки, материалы и крепление проверяются по приведённым условиям."]=common_marks
    table("Состав специальных изделий / геометрические образцы",["Изделие","Поз.","Элемент","Материал","Габарит, мм","Кол.","Происхождение"],rows,[1.2,.7,2.7,2,2,.6,1.8],"special_components")
    table("Специальные изделия / принятые решения",["Изделия","Условие"],[[", ".join(marks),text] for text,marks in notes.items()],[2,10],"special_assumptions")


def package_special(project, files, json_bytes, *, derived_assemblies=False):
    from ..special_assemblies import SpecialAssembly
    from .cad_sections import section_dxf_bytes
    entries=special_entries(project,derived_assemblies=derived_assemblies)
    index=[]
    for entry in entries:
        item={k:entry[k] for k in ("id","mark","label","kind","wall_id","face_id","opening_id","status","message")}
        if entry["status"]=="ready":
            record=SpecialAssembly.model_validate(entry["assembly"])
            prefix="special-assemblies/"+entry["mark"]
            files[prefix+".step"]=record.assembled_step.content()
            files[prefix+".json"]=json_bytes(entry["assembly"])
            item.update(step_path=prefix+".step",snapshot_path=prefix+".json",sections=[])
            for i,section in enumerate(record.sections):
                section_prefix=prefix+"-"+"АБВГ"[i]
                files[section_prefix+".dxf"]=section_dxf_bytes(section.geometry_snapshot)
                files[section_prefix+".json"]=json_bytes(section.model_dump(mode="json",by_alias=True))
                item["sections"].append({"id":section.id,"mark":entry["mark"]+"-"+"АБВГ"[i],"dxf_path":section_prefix+".dxf","snapshot_path":section_prefix+".json"})
        index.append(item)
    if index:files["special-assemblies/index.json"]=json_bytes(index)
    return index
