"""Joined presentation footprint of straight, centreline-based walls.

This does not alter architectural axes, room areas or manufacturing geometry.
GEOS unions the real-width strips and bounded endpoint mitres. Opening symbols
remain a separate layer; their voids are removed from the joined footprint.
"""
from __future__ import annotations

import math

from shapely.geometry import GeometryCollection, Polygon
from shapely.ops import unary_union


def _strip(start, end, half):
    dx, dy = end[0]-start[0], end[1]-start[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return None
    nx, ny = -dy/length*half, dx/length*half
    return Polygon([(start[0]+nx,start[1]+ny), (end[0]+nx,end[1]+ny),
                    (end[0]-nx,end[1]-ny), (start[0]-nx,start[1]-ny)])


def wall_footprint(floor, *, openings=True, mitre_limit=4.):
    """Return a valid Polygon/MultiPolygon with no internal overlap strokes.

    Endpoints must coincide within numerical precision; unrelated nearby walls
    are not snapped together. Shallow angles use a bounded bevel instead of
    inventing long spikes. Different thicknesses keep their actual offsets.
    """
    pieces, joints, by_id, wall_pieces = [], {}, {}, {}
    for wall in floor.walls:
        start, end = (float(wall.start.x),float(wall.start.y)), (float(wall.end.x),float(wall.end.y))
        half = float(wall.thickness_mm)/2
        strip = _strip(start,end,half)
        if strip is None:
            continue
        wall_pieces[wall.id]=strip
        length = math.dist(start,end)
        by_id[wall.id] = (start,end,half,length)
        for point, other in ((start,end),(end,start)):
            direction = ((other[0]-point[0])/length,(other[1]-point[1])/length)
            key = tuple(round(v,6) for v in point)
            joints.setdefault(key,[]).append((direction,half,length))
    for point, rays in joints.items():
        if len(rays)<2:
            continue
        rays.sort(key=lambda ray:math.atan2(ray[0][1],ray[0][0]))
        for (a,ha,la),(b,hb,lb) in zip(rays,rays[1:]+rays[:1]):
            cross = a[0]*b[1]-a[1]*b[0]
            if abs(cross)<1e-8:
                continue
            qa=(point[0]-a[1]*ha,point[1]+a[0]*ha)
            qb=(point[0]+b[1]*hb,point[1]-b[0]*hb)
            delta=(qb[0]-qa[0],qb[1]-qa[1])
            along=(delta[0]*b[1]-delta[1]*b[0])/cross
            meet=(qa[0]+along*a[0],qa[1]+along*a[1])
            ring=[point,qa]
            if math.dist(point,meet)<=min(mitre_limit*max(ha,hb),max(ha,hb)+min(la,lb)/2):
                ring.append(meet)
            ring.append(qb)
            wedge=Polygon(ring)
            if wedge.is_valid and wedge.area>1e-8:
                pieces.append(wedge)
    voids=[]
    if openings:
        owned_voids={}
        for opening in floor.openings:
            if opening.wall_id not in by_id:
                continue
            start,end,half,length=by_id[opening.wall_id]
            direction=((end[0]-start[0])/length,(end[1]-start[1])/length)
            lo=max(0.,float(opening.offset_mm));hi=min(length,float(opening.offset_mm+opening.width_mm))
            if hi<=lo:
                continue
            pa=(start[0]+direction[0]*lo,start[1]+direction[1]*lo)
            pb=(start[0]+direction[0]*hi,start[1]+direction[1]*hi)
            void=_strip(pa,pb,half+1e-6)
            voids.append(void)
            owned_voids.setdefault(opening.wall_id,[]).append(void)
        for wall_id,cutters in owned_voids.items():
            wall_pieces[wall_id]=wall_pieces[wall_id].difference(unary_union(cutters))
        if voids:
            # Cut added corner material, but never erase a different wall
            # merely because its footprint crosses this opening's host wall.
            cutters=unary_union(voids)
            pieces=[wedge.difference(cutters) for wedge in pieces]
    pieces.extend(wall_pieces.values())
    result=unary_union(pieces) if pieces else GeometryCollection()
    if not result.is_valid:
        raise ValueError("Не удалось построить объединённый контур стен плана")
    return result


def draw_wall_footprint(sheet, geometry, transform, color, fill):
    polygons=[geometry] if geometry.geom_type=="Polygon" else list(getattr(geometry,"geoms",[]))
    for polygon in sorted(polygons,key=lambda part:part.bounds):
        if polygon.geom_type!="Polygon" or polygon.is_empty:
            continue
        rings=[[transform(x,y) for x,y in polygon.exterior.coords]]
        rings.extend([[transform(x,y) for x,y in ring.coords] for ring in polygon.interiors])
        sheet.region(rings,color,fill,.25)
