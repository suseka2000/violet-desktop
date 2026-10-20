"""Geometric outer-envelope classification, independent of room ownership.

Closed wall cells are dissolved before looking for the outer ring, so an
unlabelled corridor or the far face of a partition is not mistaken for a facade.
Courtyards, partial/open outlines and conflicting ownership need an explicit
single-face selection; no exposure or weather resistance is inferred.
"""
from functools import lru_cache
import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree


@lru_cache(maxsize=64)
def _envelopes(segments):
    lines = [LineString(((ax, ay), (bx, by))) for _, ax, ay, bx, by in segments if (ax, ay) != (bx, by)]
    cells = list(polygonize(unary_union(lines))) if lines else []
    if not cells:
        return ()
    merged = unary_union(cells)
    polygons = [merged] if merged.geom_type == "Polygon" else list(merged.geoms)
    return tuple(Polygon(p.exterior) for p in polygons if p.geom_type == "Polygon")


@lru_cache(maxsize=64)
def _classified(segments, tolerance):
    """One topology pass per immutable floor, not again for every node."""
    envelopes = _envelopes(segments)
    lines = [LineString(((ax,ay),(bx,by))) for _,ax,ay,bx,by in segments]
    tree=STRtree(lines); result=[]
    for index, (wall_id,ax,ay,bx,by) in enumerate(segments):
        a,b=(ax,ay),(bx,by);line=lines[index];length=line.length
        if any(int(j)!=index and line.intersection(lines[int(j)]).length>tolerance for j in tree.query(line,predicate="intersects")):
            result.append((wall_id,None,None,"overlapping_exterior_wall"));continue
        matching = [p for p in envelopes if line.difference(p.exterior.buffer(tolerance)).length <= tolerance]
        reason = "open_or_internal_outline"
        if length > tolerance and len(matching) == 1:
            polygon = matching[0]
            # Several samples prevent partially internal / overlapping outlines
            # from being assigned a single whole-wall exterior binding.
            normal = (-(b[1]-a[1])/length, (b[0]-a[0])/length)
            offset = max(tolerance*4, min(1., length/100))
            sides = []
            for t in (.1, .5, .9):
                p = line.interpolate(t, normalized=True)
                sides.append(tuple(polygon.contains(Point(p.x+normal[0]*offset*s, p.y+normal[1]*offset*s)) for s in (1, -1)))
            if sides and all(pair == sides[0] for pair in sides) and sides[0] in ((True, False), (False, True)):
                side = "right" if sides[0][0] else "left"
                result.append((wall_id,side,polygon,None));continue
        result.append((wall_id,None,None,reason))
    return tuple(result)


def exterior_floor_context(floor, tolerance):
    segments = tuple((w.id, w.start.x, w.start.y, w.end.x, w.end.y) for w in floor.walls)
    by_id={wall.id:wall for wall in floor.walls}
    if len(by_id)!=len(floor.walls):raise ValueError("Повторяются идентификаторы стен этажа")
    result={};excluded=[]
    for wall_id,side,polygon,reason in _classified(segments,tolerance):
        wall=by_id[wall_id]
        if side:
            face=next(f for f in wall.faces if f.side==side)
            if face.space_id is None:
                result[(wall.id,face.id)]={"wall":wall,"face":face,"floor":floor,"space":None,
                    "inclusion_reason":"exterior_envelope","polygon":polygon,"classification_basis":"closed_wall_envelope"}
                continue
            reason="exterior_space_assignment_conflict"
        for face in wall.faces:
            if face.space_id is None:
                excluded.append({"wall_id":wall.id,"face_id":face.id,"side":face.side,"floor_id":floor.id,"reason":reason})
    return {"targets":list(result.values()),"by_key":result,"excluded":excluded}


def exterior_corner_context(floor, wall, face, point, tolerance):
    context = exterior_floor_context(floor, tolerance)
    selected = context["by_key"].get((wall.id, face.id))
    if not selected:
        return None, []
    vertices = list(selected["polygon"].exterior.coords)[:-1]
    # The envelope may simplify a collinear split vertex. A straight junction
    # is handled by the existing mounted-face continuation classifier instead.
    indices = [i for i,p in enumerate(vertices) if math.hypot(p[0]-point.x,p[1]-point.y) <= tolerance]
    if len(indices) != 1:
        return None, []
    adjacent = [item for item in context["targets"] if item["wall"].id != wall.id and
                any(math.hypot(p.x-point.x,p.y-point.y) <= tolerance for p in (item["wall"].start,item["wall"].end))]
    if len(adjacent) != 1:
        return None, []
    i = indices[0]; a,b,c = vertices[i-1],vertices[i],vertices[(i+1)%len(vertices)]
    u,v = (b[0]-a[0],b[1]-a[1]),(c[0]-b[0],c[1]-b[1])
    area = sum(p[0]*q[1]-q[0]*p[1] for p,q in zip(vertices,vertices[1:]+vertices[:1]))/2
    turn = math.degrees(math.atan2(u[0]*v[1]-u[1]*v[0],u[0]*v[0]+u[1]*v[1]))*(1 if area>0 else -1)
    angular = math.degrees(math.atan2(tolerance,min(math.hypot(*u),math.hypot(*v))))
    corner = {"corner_type":"external_corner" if turn>angular else "internal_corner" if turn < -angular else "straight",
              "interior_angle_deg":round(180+turn,6), "right_angle":abs(abs(turn)-90)<=angular+1e-9,
              "classification_basis":"closed_wall_exterior_winding", "space_id":None,
              "boundary_wall_ids":[wall.id,adjacent[0]["wall"].id]}
    return corner, adjacent
