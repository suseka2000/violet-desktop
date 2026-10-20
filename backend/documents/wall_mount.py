"""Draft mounting elevations generated solely from saved configuration + manuals."""
from math import fsum

from .scene import BLUE, GRAY, INK, RED, wrap, width as text_width
from .mount_details import detail_catalog, detail_key, add_detail_sheets
from .label_layout import draw_panel_labels


def _compact_rows(rows):
    """Same evidence once with all view references; no requirements discarded."""
    groups={}
    for row in rows:
        groups.setdefault(tuple(str(cell) for cell in row[1:]),[]).append(row[0])
    result=[]
    for content,marks in groups.items():
        values=sorted({int(mark[1:]) for mark in marks})
        ranges=[];start=end=values[0]
        for value in values[1:]:
            if value==end+1:end=value
            else:ranges.append((start,end));start=end=value
        ranges.append((start,end))
        label=", ".join(f"М{a:03d}"+(f"-М{b:03d}" if a!=b else "") for a,b in ranges)
        result.append([label,*content])
    return result


def _mount_views(preview, mark, title, material, new, width, height, num):
    from . import scale_for
    wall, config = preview["wall"], preview["configuration"]
    numbered = list(enumerate(preview["nodes"], 1))
    # Bounded, paginated leader columns retain EVERY reference. The parent
    # document's 400-sheet limit remains the explicit overall resource bound.
    chunks = [numbered[i:i+24] for i in range(0,len(numbered),24)] or [[]]
    for ci, chunk in enumerate(chunks,1):
        suffix = f" / привязки {ci} из {len(chunks)}" if len(chunks)>1 else ""
        sheet = new(f"Раскладка панелей {mark} / {wall['name']}"+suffix, "wall_mount")
        sheet.references.extend([wall["id"], wall["face_id"]])
        sheet.text(23, 37, f"{mark}. {material}; формат до {num(config['panel_width_mm'])} x {num(config['panel_height_mm'])}, t={num(config['panel_thickness_mm'])} мм; шов {num(config['joint_mm'])} мм.", 3.2, BLUE, True)
        for li,line in enumerate(wrap(title,width-48,3.2)[:2]):
            sheet.text(23,44+li*4.5,line,3.2)
        sheet.text(23,57,"Вид: от конца стены к началу" if wall["side"]=="right" else "Вид: от начала стены к концу",2.8,GRAY)
        sheet.text(23,63,"Ось X - вдоль опорной линии стены; Y - от низа стены.",2.8,GRAY)
        scale=scale_for(wall["length_mm"],wall["height_mm"],width-130,height-178)
        dw,dh=wall["length_mm"]/scale,wall["height_mm"]/scale
        ox,oy=58+(width-130-dw)/2,78+(height-178-dh)/2
        transform=lambda x,y:(ox+x/scale,oy+(wall["height_mm"]-y)/scale)
        from .levels import draw_levels
        for p in preview["panels"]:
            px,py=transform(p["x_mm"],p["y_mm"]+p["height_mm"])
            sheet.rect(px,py,p["width_mm"]/scale,p["height_mm"]/scale,GRAY,"#f3eadc",.12)
        sheet.rect(ox,oy,dw,dh,INK,weight=.3)
        for opening in preview["openings"]:
            px,py=transform(opening["x_mm"],opening["y_mm"]+opening["height_mm"])
            sheet.rect(px,py,opening["width_mm"]/scale,opening["height_mm"]/scale,INK,"#ffffff",.3)
            if opening["width_mm"]/scale>15:
                sheet.text(px+opening["width_mm"]/(2*scale),py+opening["height_mm"]/(2*scale),"Проём",2.5,GRAY,anchor="middle")
        if preview.get("document_whole_wall"):
            from .whole_wall import draw_framing_overlay
            draw_framing_overlay(sheet, preview["document_whole_wall"], transform, scale)
        draw_panel_labels(sheet,preview["panels"],transform,scale,color=GRAY,fill="#f3eadc")
        draw_levels(sheet,preview.get("document_levels",[]),transform,wall["length_mm"],wall.get("base_mm",0),wall["height_mm"])
        ordered=sorted(chunk,key=lambda pair:(pair[1]["x_mm"],pair[1]["y_mm"],pair[0]))
        middle=(len(ordered)+1)//2
        for column,values in enumerate((ordered[:middle],ordered[middle:])):
            values=sorted(values,key=lambda pair:(-pair[1]["y_mm"],pair[1]["x_mm"],pair[0]))
            lx=34 if column==0 else width-29
            for row,(ni,node) in enumerate(values):
                x,y=transform(node["x_mm"],node["y_mm"])
                ly=88+(height-193)*(row/(len(values)-1) if len(values)>1 else .5)
                color=BLUE if node["rule_id"] and node.get("decision_status") != "project_assumption" else RED
                sheet.line(x,y,lx+(3 if column==0 else -3),ly,GRAY,.12)
                sheet.circle(x,y,.65,color,"#ffffff",.18)
                sheet.circle(lx,ly,2.6,color,"#ffffff",.22)
                sheet.text(lx,ly+.9,str(ni),2.3,color,True,"middle")
        sheet.dimension(ox,ox+dw,oy+dh+10,oy+dh,num(wall["length_mm"])+" мм",BLUE)
        ranges=f"Маркеры {chunk[0][0]}-{chunk[-1][0]} из {len(numbered)}." if chunk else "Нет мест примыканий."
        sheet.text(23,74,f"Высота {num(wall['height_mm'])} мм; М 1:{num(scale)}. {ranges}",2.8,BLUE)
        sheet.text(23,height-75,"Маркеры обозначают примыкания. Состав узлов приведён на соответствующих листах.",2.8,GRAY)
        sheet.text(23,height-68,"Размеры фрагментов вокруг проёмов приведены в ведомости геометрии панелей.",2.8,GRAY)
        sheet.text(23,height-61,"Принятые посадки и вопросы по комплекту собраны в примечаниях.",2.8,GRAY)


def build_mount_catalog(project, report, *, assembly_records=None, server_owned=False):
    catalog = detail_catalog(report.get("walls", []))
    if project is not None and project.properties.get("mount_assemblies"):
        from ..mount_assemblies import assembly_usage, detail_identity
        from ..mount_context_symbols import context_symbol
        from .node_construction import has_assembly_support
        from ..wall_mount import validate_wall_mounting, pinned_step_assets
        active, unused = assembly_usage(project, report, saved=assembly_records, server_owned=server_owned)
        mounting = validate_wall_mounting(project)
        assets = pinned_step_assets(mounting.snapshot) if mounting else []
        symbols = {kind: value for kind in ("screw", "frame_ws", "frame_wt", "suspension")
                   if (value := context_symbol(kind, assets)) is not None}
        for entry in catalog.values():
            entry["assembly"] = active.get(detail_identity(entry["preview"],entry["node"]))
            if entry["assembly"]:
                entry["context_symbols"] = {} if has_assembly_support(entry["assembly"]) else symbols
    return catalog


def panel_outline_segments(pieces):
    """Cancel internal rectangle-cut edges within ONE original panel cell."""
    from collections import defaultdict
    edges = defaultdict(lambda: defaultdict(float))
    for piece in pieces:
        x, y, w, h = (float(piece[k]) for k in ("x_mm","y_mm","width_mm","height_mm"))
        for axis, fixed, lo, hi, sign in (("h",y,x,x+w,1),("h",y+h,x,x+w,-1),("v",x,y,y+h,1),("v",x+w,y,y+h,-1)):
            changes = edges[(axis,round(fixed,6))]
            changes[round(lo,6)] += sign
            changes[round(hi,6)] -= sign
    result = []
    for (axis,fixed),changes in edges.items():
        value, previous = 0, None
        for coordinate, change in sorted(changes.items()):
            if previous is not None and value and coordinate-previous > 1e-6:
                result.append(((previous,fixed),(coordinate,fixed)) if axis=="h" else ((fixed,previous),(fixed,coordinate)))
            value += change
            previous = coordinate
    return result


def draw_mount_view(sheet, preview, mark, box, num, catalog=None):
    """A dimensioned mounting elevation drawn directly in an album cell."""
    from collections import defaultdict
    from . import scale_for
    from .openings import draw_elevation_opening, vertical_dimension, PREFIXES
    x, y, w, h = box
    wall, config = preview["wall"], preview["configuration"]
    material = {"veneer":"Шпон","hpl":"HPL","painted":"Окраска","unspecified":"Материал не выбран"}.get(config["material"],config["material"])
    outside = " / снаружи" if wall.get("installation_zone") == "exterior" else ""
    title = wrap(f"{mark}. {wall['name']}{outside} / {material}", w-8, 3.2, True)
    for index,line in enumerate(title):
        sheet.text(x+3,y+5+index*4.6,line,3.2,INK,True)
    title_h = len(title)*4.6
    legend_lines=[]
    if preview.get("document_whole_wall"):
        from .whole_wall import framing_legend
        legend_lines=wrap(framing_legend(preview["document_whole_wall"]),w-6,2.4)
    legend_h=len(legend_lines)*3.6
    scale = scale_for(wall["length_mm"],wall["height_mm"],w-68,h-57-title_h-legend_h)
    dw, dh = wall["length_mm"]/scale, wall["height_mm"]/scale
    ox, oy = x+24+(w-68-dw)/2, y+title_h+23+legend_h
    transform = lambda a,b: (ox+a/scale,oy+(wall["height_mm"]-b)/scale)
    sheet.text(x+w-3,y+title_h+10,f"М 1:{num(scale)}",2.6,BLUE,anchor="end")
    sheet.text(x+3,y+title_h+10,f"Панель t={num(config['panel_thickness_mm'])}; шов {num(config['joint_mm'])} мм.",2.5,GRAY)
    for index,line in enumerate(legend_lines):
        sheet.text(x+3,y+title_h+14+index*3.6,line,2.4,GRAY)
    sheet.rect(ox,oy,dw,dh,INK,weight=.25)
    groups = defaultdict(list)
    for piece in preview["panels"]:
        groups[piece["panel_id"]].append(piece)
    for pieces in groups.values():
        for a,b in panel_outline_segments(pieces):
            sheet.line(*transform(*a),*transform(*b),BLUE,.16)
    marks = preview.get("document_opening_marks", {})
    for oi, opening in enumerate(preview["openings"],1):
        omark = marks.get(opening["id"],PREFIXES.get(opening.get("kind"),"ПО")+f"-{oi:02d}")
        draw_elevation_opening(sheet,opening,transform,scale,omark)
        if opening["width_mm"]/scale >= 9:
            a,b=transform(opening["x_mm"],opening["y_mm"]),transform(opening["x_mm"]+opening["width_mm"],opening["y_mm"])
            sheet.dimension(a[0],b[0],oy+dh+8,a[1],num(opening["width_mm"]),INK)
        # Height beside the jamb, never over the opening identifier. Tiny
        # openings still retain their exact ordinates in the setting-out table.
        from .openings import draw_opening_height
        draw_opening_height(sheet, opening, transform, scale, num)
    if preview.get("document_specials"):
        from .special_sections import draw_special_locations
        draw_special_locations(sheet,preview["document_specials"],transform,wall,num)
    if preview.get("document_whole_wall"):
        from .whole_wall import draw_framing_overlay
        draw_framing_overlay(sheet, preview["document_whole_wall"], transform, scale)
    draw_panel_labels(sheet,preview["panels"],transform,scale,color=GRAY)
    # Use the common setting-out cells, not a second nominal 600 mm grid or
    # arbitrary rectangles created when subtracting windows from a panel.
    from ..panel_layout import regular_axis
    columns = preview.get("layout", {}).get("columns")
    spans = [(column["start_mm"], column["width_mm"]) for column in columns] if columns is not None else regular_axis(wall["length_mm"], config["panel_width_mm"], config["joint_mm"])
    from .mount_setting_out import draw_panel_chain
    draw_panel_chain(sheet, spans, ox, oy, scale, num, mark=mark)
    sheet.dimension(ox,ox+dw,oy+dh+18,oy+dh,num(wall["length_mm"]),INK)
    vertical_dimension(sheet,ox-8,oy,oy+dh,ox,num(wall["height_mm"]),INK)
    # Row heights are actual panel heights, excluding floor/ceiling gaps.
    for row in preview.get("layout", {}).get("rows", []):
        top = transform(0,row["start_mm"]+row["height_mm"])[1]
        bottom = transform(0,row["start_mm"])[1]
        if bottom-top >= 8:
            vertical_dimension(sheet,ox-18,top,bottom,ox,num(row["height_mm"]),BLUE,2.5)
    from .levels import draw_levels
    level_notes=draw_levels(sheet,preview.get("document_levels",[]),transform,wall["length_mm"],wall.get("base_mm",0),wall["height_mm"])
    if level_notes:
        sheet.paragraph(x+3,y+h-22,"; ".join(level_notes),w-8,2.5,GRAY,leading=3.5)
    # One callout per distinct construction on a view. Repetitions retain all
    # coordinates in wall-mount-junctions.csv and wall-mounting.json.
    used = {}
    for node in preview["nodes"]:
        entry = catalog.get(detail_key(preview,node)) if catalog else None
        if entry:
            used.setdefault(entry["mark"],node)
    occupied=[]
    for primitive in sheet.primitives:
        if primitive["type"]=="text":
            tw=text_width(primitive["value"],primitive["size"],primitive["bold"])
            tx=primitive["x"]-(tw/2 if primitive["anchor"]=="middle" else tw if primitive["anchor"]=="end" else 0)
            occupied.append((tx,primitive["y"]-primitive["size"],tw,primitive["size"]+1))
    def overlap(a,b):
        return max(0,min(a[0]+a[2],b[0]+b[2])-max(a[0],b[0]))*max(0,min(a[1]+a[3],b[1]+b[3])-max(a[1],b[1]))
    for dmark,node in used.items():
        px,py = transform(node["x_mm"],node["y_mm"])
        label_w=text_width(dmark,2.5)+2
        options=[]
        for radius in (7,12,18,25):
            for dx,dy in ((1,-1),(-1,-1),(1,1),(-1,1),(1,0),(-1,0),(0,1),(0,-1)):
                lx,ly=px+dx*radius,py+dy*radius
                rect=(lx-label_w/2,ly-2.8,label_w,4.1)
                if rect[0]<x+3 or rect[0]+rect[2]>x+w-3 or rect[1]<oy+1 or rect[1]+rect[3]>oy+dh-1:
                    continue
                collision=sum(overlap(rect,other) for other in occupied)
                options.append((collision, radius, lx,ly,rect))
        if not options or min(option[0] for option in options)>0:
            # Low parapets and very long walls may have no readable space
            # inside their silhouette. Keep the spatial association with an
            # outside leader, rather than leaving only a list of marks.
            for slot in range(int((h-52)/5)):
                lx,ly=x+w-13,y+29+slot*5
                rect=(lx-label_w/2,ly-2.8,label_w,4.1)
                collision=sum(overlap(rect,other) for other in occupied)
                if not collision:
                    options.append((0,100,lx,ly,rect))
                    break
        if not options or min(option[0] for option in options)>0:
            raise ValueError("Выноски узлов не помещаются в ячейке альбома; нужен увеличенный вид")
        collision,radius,lx,ly,rect=min(options,key=lambda option:option[:2])
        sheet.line(px,py,lx,ly-1,GRAY,.1)
        sheet.circle(px,py,.7,BLUE,"#ffffff",.15)
        sheet.rect(*rect,"none","#ffffff",0)
        sheet.text(lx,ly,dmark,2.5,BLUE,anchor="middle")
        occupied.append(rect)
    label = "Узлы: "+", ".join(used) if used else "Узлы не назначены"
    sheet.paragraph(x+3,y+h-13,label,w-8,2.5,BLUE,leading=3.5)
    sheet.text(x+3,y+h-3,"База X=0: "+("B, направление B > A" if wall["side"]=="right" else "A, направление A > B")+"; Y - от низа стены.",2.5,GRAY)
    sheet.references.extend([wall["id"],wall["face_id"],*[opening["id"] for opening in preview["openings"]]])


def add_wall_mount_sheets(report, new, table, width, height, num, project=None, whole_walls=(), *, assembly_records=None, server_owned=False):
    register, locations, requirements, pieces, assumptions, quantities = [], [], [], [], [], []
    catalog = build_mount_catalog(project, report, assembly_records=assembly_records, server_owned=server_owned)
    if project is not None and project.properties.get("mount_assemblies"):
        from ..mount_assemblies import assembly_usage, detail_identity
        active, unused = assembly_usage(project, report, saved=assembly_records, server_owned=server_owned)
        if unused:
            table("Сборки вне текущего монтажа / не включены в разрезы",["Сборка","Причина"],
                  [[item.label,"Параметры, геометрический контекст или исходники изменились, либо соответствующее примыкание удалено. Выполните построение заново."] for item in unused],[3,8],"assembly_stale")
    materials = {"veneer":"Шпон", "hpl":"HPL", "painted":"Окраска", "unspecified":"Не выбран"}
    by_face = {(record.wall_id, record.face_id): record for record in whole_walls}
    for wi, preview in enumerate(report.get("walls", []), 1):
        if project is not None:
            from .levels import mounting_view_levels
            preview={**preview,"document_levels":mounting_view_levels(project,preview)}
        wall, config = preview["wall"], preview["configuration"]
        mark = f"М{wi:03d}"
        chosen = next((c for c in preview["candidates"] if c["rule_id"] == preview["selected_rule_id"]), None)
        title = chosen["title"] if chosen else "Сборка не выбрана"
        summary=preview["summary"]
        quantities.append([mark,wall["name"],num(wall["length_mm"]),num(wall["height_mm"]),
                           str(summary["panel_count"]),str(summary["piece_count"]),num(summary["area_m2"])])
        register.append([mark, wall["name"], "Левая" if wall["side"] == "left" else "Правая",
                         materials[config["material"]] + f" / {num(config['panel_thickness_mm'])} мм", title,
                         "Раскладка по параметрам проекта"])
        _mount_views({**preview, "document_whole_wall": by_face.get((wall["id"], wall["face_id"]))},mark,title,materials[config["material"]],new,width,height,num)
        for ni,node in enumerate(preview["nodes"],1):
            source = node.get("source")
            decision=node.get("decision_status")
            state="Проектное допущение" if decision=="project_assumption" else "Ссылка-кандидат; не крепёж" if node["rule_id"] else "Требуется решение"
            if catalog[detail_key(preview,node)].get("assembly"):
                state="Разрез реальной сборки; посадки проектные"
            locations.append([mark, str(ni), node["label"], f"{num(node['x_mm'])}; {num(node['y_mm'])}",
                              catalog[detail_key(preview,node)]["mark"]+"; "+(f"{source['filename']}, стр. {source['page']}" if source else "Проектная схема, нет прямого источника"),state])
        if preview.get("selection_reason"):
            assumptions.append([mark,"Выбор сборки",str(preview["selection_reason"]),"Алгоритм выбора, не согласование"])
        for assumption in preview.get("assumptions",[]):
            assumptions.append([mark,str(assumption.get("id","Допущение")),assumption.get("message",""),str(assumption.get("required_check","Проверить применимость"))])
        for r in preview["requirements"]:
            s = r["source"]
            requirements.append([mark,r["title"],r["requirement"]+" "+r["scope"],f"{s['filename']}, стр. {s['page']}. SHA256 {s['sha256']}"])
        for issue in preview["issues"]:
            requirements.append([mark,issue["code"],issue["message"],"Проверка генератора; не норматив"])
        if chosen:
            for match in chosen["step_matches"]:
                requirements.append([mark,match["component_code"],match["filename"]+"; только совпадение обозначения, геометрия и взаимозаменяемость не подтверждены.","STEP SHA256 "+match["source_id"]])
        for piece in preview["panels"]:
            pieces.append([mark,piece["panel_id"],str(piece["index"]),f"{num(piece['x_mm'])}; {num(piece['y_mm'])}",
                           f"{num(piece['width_mm'])} x {num(piece['height_mm'])}",num(piece["area_m2"])])
    if register:
        # Round only for display. Summing already formatted row strings loses
        # precision and can disagree with the geometry report and raw CSV.
        walls = report["walls"]
        totals=[fsum(p["wall"]["length_mm"] for p in walls),
                sum(p["summary"]["panel_count"] for p in walls),
                sum(p["summary"]["piece_count"] for p in walls),
                fsum(p["summary"]["area_m2"] for p in walls)]
        quantities.append(["ИТОГО","По выбранным сторонам",num(totals[0]),"",str(totals[1]),str(totals[2]),num(totals[3])])
        table("Сводная ведомость монтажа / геометрические количества",["Вид","Стена","Длина, мм","Высота, мм","Ячеек","Фрагментов","Нетто, м²"],quantities,[1,2.4,1.5,1.5,1.3,1.6,1.5],"wall_mount_quantities")
        table("Ведомость раскладок и систем монтажа",["Марка","Стена","Сторона","Отделка","Сборка","Статус"],register,[1,2,1,2,4,3],"wall_mount_register")
        table("Интерактивный монтаж / места примыканий",["Вид","№","Место","X; Y, мм","Источник / сборка","Статус"],locations,[1,1,2,2,4,3],"wall_mount_nodes")
        table("Интерактивный монтаж / условия и происхождение",["Вид","Условие","Описание","Первоисточник"],_compact_rows(requirements),[1.7,2.4,6,4],"wall_mount_requirements")
        table("Ведомость геометрии панелей",["Вид","Панель","Фрагмент","X; Y, мм","Габарит, мм","Площадь, м²"],pieces,[1,2,1,2,2,1.5],"wall_mount_pieces")
        if assumptions:
            table("Принятые решения и проектные допущения",["Вид","Решение","Обоснование","Что проверить"],_compact_rows(assumptions),[1.7,2,6,4],"wall_mount_assumptions")
        add_detail_sheets(catalog,new,width,height,num)
        from .assemblies import add_assembly_tables
        add_assembly_tables(catalog,table)
