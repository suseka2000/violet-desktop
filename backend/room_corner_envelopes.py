"""Calibrated material envelopes for planning complete horizontal end cells.

These boxes reserve stock length; they do not replace native corner geometry.
Every final profile, screw and contact is still checked against exact STEP.
"""
from .room_corner_parts import corner_spec,_internal_support_x
from .assembly_recipes_corners import CALIBRATIONS_EXTRA
from .wall_systems import profile_footprint


def corner_material_envelopes(joint):
    spec=corner_spec(joint)
    if spec.get('skipped_reason'):return []
    family=spec['family'];ta,tb=spec['thickness_mm'];thin=spec.get('internal_bond_thickness_mm',0.)
    bond=spec.get('corner_bond_thickness_mm',0.);boxes=[]
    if family=='angle20':
        x,y=(-tb-1-thin,-ta-1-thin) if spec['kind']=='internal_corner' else (-1-bond,-1-bond)
        boxes.append(('profile','profile',[x,y],[x+20,y+20]))
    elif family=='cub':
        calibration=CALIBRATIONS_EXTRA['external-V-Cub'];face=calibration['panel_face_xy_mm']
        lo=calibration['outer_tip_xy_mm']-face;hi=calibration['section_max_xy_mm']-face
        boxes.append(('profile','profile',[lo,lo],[hi,hi]))
    elif family in {'vh2','vh3'}:
        lo,hi=profile_footprint(family)
        boxes.append(('profile','profile',[lo,5.5],[hi,15.9]))
    else:boxes.append(('profile','profile',[-3.,-1.2],[3.,6.1]))
    columns=[]
    if spec['kind']=='internal_corner':
        columns=[(_internal_support_x(tb+thin),-1.,-ta-1-thin,12.)]
    elif family in {'vh2','vh3'}:
        columns=[(x,1.,15.9,10. if family=='vh3' else 12.) for x in ([0.] if family=='vh3' else [-19.,20.])]
    for index,(x,sign,front,depth) in enumerate(columns,1):
        frame_front=front+sign*depth
        boxes += [(f'backing-{index}','backing',[x-14,min(front,frame_front)],[x+14,max(front,frame_front)]),
                  (f'frame-{index}','frame',[x-14,min(frame_front,frame_front+sign*27)],
                   [x+14,max(frame_front,frame_front+sign*27)])]
    a,b=spec['axes_a'],spec['axes_b'];origin=spec['origin_mm'];output=[]
    for number,(low,high) in enumerate(spec['segments_z_mm'],1):
        for identifier,role,lo,hi in boxes:
            points=[[origin[k]+a[k]*x+b[k]*y for k in range(2)] for x in (lo[0],hi[0]) for y in (lo[1],hi[1])]
            output.append({'id':f's{number}:{identifier}','role':role,'bounds_mm':{
                'min':[min(p[k] for p in points) for k in range(2)]+[low],
                'max':[max(p[k] for p in points) for k in range(2)]+[high]}})
    return output


def corner_transition_clearances(result):
    """Reserve only horizontal cells meeting an interior joint height edge."""
    output={}
    for joint in result.get('joints',[]):
        envelopes=None
        for side in joint['sides']:
            index=side['wall_index'];wall=result['walls'][index];plan=side.get('plan',{})
            cfg=side.get('mount_configuration',plan.get('mount_configuration',{}))
            base=wall['translation_mm'][2]
            height=plan.get('wall',{}).get('height_mm')
            if height is None:continue
            transitions=sorted({z-base for interval in joint['segments_z_mm'] for z in interval
                                if base+cfg.get('floor_gap_mm',0.)+1e-6<z<base+height-cfg.get('ceiling_gap_mm',0.)-1e-6})
            if not transitions:continue
            if envelopes is None:envelopes=corner_material_envelopes(joint)
            rotation=wall['rotation'];start=wall.get('room_span',{}).get('start_mm',0.)
            translation=[wall['translation_mm'][k]+rotation[k][0]*start for k in range(3)]
            for item in envelopes:
                bounds=item['bounds_mm'];points=[]
                for x in (bounds['min'][0],bounds['max'][0]):
                    for y in (bounds['min'][1],bounds['max'][1]):
                        for z in (bounds['min'][2],bounds['max'][2]):
                            point=[x,y,z]
                            points.append([sum(rotation[k][axis]*(point[k]-translation[k]) for k in range(3)) for axis in range(3)])
                output.setdefault(index,[]).append({'joint_id':joint['id'],'part_id':item['id'],
                    'transitions_z_mm':transitions,'bounds_mm':{side:[operation(p[k] for p in points) for k in range(3)]
                    for side,operation in (('min',min),('max',max))}})
    return output
