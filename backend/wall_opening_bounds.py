"""One boundary tolerance for rounded drawings and exact native wall cuts."""
from __future__ import annotations

import math

BOUNDARY_TOLERANCE_MM = 1e-6


def opening_cut_bounds(opening, length, height, *, outside=0.):
    """Return X/Z limits; only already coincident exterior edges may extend.

    Layout rectangles use six decimal places while measured wall lengths may
    carry more. Treating their last fraction of a micrometre as real material
    makes a phantom film at an otherwise open wall end. Interior gaps, even
    0.00001 mm, are not enlarged. ``outside`` only conditions a through-cut;
    the architectural rectangle (outside=0) stays inside the exact wall.
    """
    values=[opening.get(k) for k in ('x_mm','y_mm','width_mm','height_mm')]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v)
           for v in [*values,length,height,outside]):
        raise ValueError('Некорректные границы выреза стены')
    x,z,width,depth=values
    if width<=0 or depth<=0 or length<=0 or height<=0 or outside<0:
        raise ValueError('Вырез стены должен иметь положительную площадь')
    bounds=[x,z,x+width,z+depth]
    for low,high,limit in ((0,2,length),(1,3,height)):
        if abs(bounds[low])<=BOUNDARY_TOLERANCE_MM:bounds[low]=-outside
        if abs(bounds[high]-limit)<=BOUNDARY_TOLERANCE_MM:bounds[high]=limit+outside
    return tuple(bounds)
