"""Resolve facing horizontal stock at a stepped finish, without moving a door.

Only adjacent wall faces and their measured L-start sill/head projections are
considered. A matched local clearance is shared by the two finish edges of
the same terminating corner band, never by every wall at the same elevation.
"""
from .wall_framing import _hidden_profile_runs
from .wall_systems import profile_footprint

EPS=1e-6


def _rectangle(wall,run):
    rotation=wall['rotation'];start=wall.get('room_span',{}).get('start_mm',0.)
    translation=[wall['translation_mm'][k]+rotation[k][0]*start for k in range(3)]
    # All three measured hidden L-start/VH profiles share this depth, but this
    # resolver intentionally only handles L-start opening sill/head stock.
    return [[translation[k]+rotation[k][0]*x+rotation[k][1]*y for k in range(2)]
            for x,y in ((run['start_mm'],5.5),(run['end_mm'],5.5),
                        (run['end_mm'],15.9),(run['start_mm'],15.9))]


def _rectangles_overlap(first,second):
    # Exact oriented rectangle broad phase: a rotated wall's enclosing AABB
    # alone must not manufacture an opposing profile and enlarge a clearance.
    for polygon in (first,second):
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):
            normal=[a[1]-b[1],b[0]-a[0]]
            length=(normal[0]**2+normal[1]**2)**.5
            if length<=EPS:return False
            normal=[v/length for v in normal]
            lohi=[(min(sum(p[k]*normal[k] for k in range(2)) for p in points),
                   max(sum(p[k]*normal[k] for k in range(2)) for p in points)) for points in (first,second)]
            if min(v[1] for v in lohi)-max(v[0] for v in lohi)<=EPS:return False
    return True


def transition_profile_overrides(result,previews,systems):
    """Return {wall_index: {opening_id:kind: measured opposing projection}}."""
    walls=result['walls'];runs={};margins={};margins_by_wall={};neighbours={};reasons={}
    for joint in result.get('joints',[]):
        a,b=(side['wall_index'] for side in joint['sides'])
        neighbours.setdefault(a,set()).add(b);neighbours.setdefault(b,set()).add(a)
    for index,preview in previews.items():
        system=systems.get(index,{})
        if system.get('key') not in {'vh2','vh3'}:continue
        runs[index]=[run for run in _hidden_profile_runs(preview,system['key'],system.get('perimeter_families',[]))
                     if run['family']=='lstart' and run['axis']=='horizontal'
                     and run.get('context') in {'opening_sill','opening_head'}]
        openings={item['id']:item for item in preview.get('openings',[])}
        for margin in preview.get('wall',{}).get('finish_transition_clearances',[]):
            opening=openings.get(margin['opening_id'])
            if opening is None:raise ValueError('Для зазора уступа отсутствует вырез отделки')
            key=(index,margin['opening_id']+':'+margin['kind'])
            margins[key]=(margin,opening);reasons[key]={};margins_by_wall.setdefault(index,[]).append(key)
    for key,(margin,opening) in margins.items():
        index=key[0];wall=walls[index];world_z=wall['translation_mm'][2]+margin['structural_z_mm']
        own=[run for run in runs.get(index,[]) if run['context']==margin['kind']
             and abs(run['edge_mm']-margin['finish_z_mm'])<=EPS
             and min(run['end_mm'],opening['x_mm']+opening['width_mm'])-max(run['start_mm'],opening['x_mm'])>EPS]
        opposite='opening_head' if margin['kind']=='opening_sill' else 'opening_sill'
        for other_index in sorted(neighbours.get(index,())):
            for other in runs.get(other_index,[]):
                if (other['context']!=opposite or
                        abs(walls[other_index]['translation_mm'][2]+other['edge_mm']-world_z)>EPS):continue
                if not any(_rectangles_overlap(_rectangle(wall,run),_rectangle(walls[other_index],other)) for run in own):continue
                footprint=profile_footprint('lstart',other['inward_sign'])
                projection=-footprint[0] if opposite=='opening_head' else footprint[1]
                reason={'wall_index':other_index,'profile_id':other['id'],'profile_family':'lstart',
                        'kind':opposite,'projection_mm':projection}
                reasons[key][(other_index,other['id'])]=reason
    # A corner is one complete cell shared by two panel edges. Copy only
    # between finish notches touching the same *end* of that corner band.
    links=[]
    for joint in result.get('joints',[]):
        bands=joint.get('source_segments_z_mm',joint['segments_z_mm'])
        selected=[]
        for side in joint['sides']:
            index=side['wall_index'];wall=walls[index]
            edge=side['panel_end_local_x_mm']-wall.get('room_span',{}).get('start_mm',0.)
            candidates=[]
            for key in margins_by_wall.get(index,[]):
                margin,opening=margins[key]
                if not opening['x_mm']-EPS<=edge<=opening['x_mm']+opening['width_mm']+EPS:continue
                z=wall['translation_mm'][2]+margin['structural_z_mm']
                endpoint=1 if margin['kind']=='opening_sill' else 0
                if any(abs(band[endpoint]-z)<=EPS for band in bands):candidates.append(key)
            selected.append(candidates)
        if len(selected)==2:
            links.extend((a,b) for a in selected[0] for b in selected[1]
                         if margins[a][0]['kind']==margins[b][0]['kind'])
    # Connected components avoid repeated propagation passes on a long chain.
    graph={key:set() for key in margins}
    for a,b in links:graph[a].add(b);graph[b].add(a)
    visited=set();output={}
    for first in graph:
        if first in visited:continue
        pending=[first];component=[];combined={}
        while pending:
            key=pending.pop()
            if key in visited:continue
            visited.add(key);component.append(key);combined.update(reasons[key]);pending.extend(graph[key]-visited)
        if not combined:continue
        evidence=[combined[key] for key in sorted(combined)]
        projection=max(item['projection_mm'] for item in evidence)
        for index,name in component:
            output.setdefault(index,{})[name]={'opposing_projection_mm':projection,'opposing_profiles':evidence}
    return output
