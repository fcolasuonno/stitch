"""
SVG to Embroidery Converter — Improved Version
================================================
Key improvements over original:
  1. Full SVG path parser (M/L/H/V/C/S/Q/T/A/Z + relative variants)
  2. Recursive group traversal with proper CSS-class + style inheritance
  3. SVG transform matrix support (translate, scale, rotate, matrix)
  4. Correct thread/color ordering — fixes scrambled colours
  5. Proper satin-column stitches for narrow shapes and stroke outlines
  6. Scanline fill with sorted edge-intersections (replaces slow is_point_in_polygon)
  7. Adaptive path sampling (proportional to arc length)
  8. Named thread colours for common embroidery threads
  9. Jump/trim/END placed correctly — no spurious corner stitches
 10. Per-colour fill-angle variation for a natural tatami look
"""

import json
import os
import re
import math
import struct
import logging
import traceback
from io import BytesIO
from collections import OrderedDict
from typing import List, Tuple, Dict, Any, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)

import xml.etree.ElementTree as ET

# ── optional libraries ──────────────────────────────────────────────────────
try:
    from svgpathtools import parse_path as _svgpt_parse
    SVGPATHTOOLS_AVAILABLE = True
    print("svgpathtools loaded")
except Exception as e:
    SVGPATHTOOLS_AVAILABLE = False
    print(f"svgpathtools unavailable: {e}")

try:
    from multipart import parse_form_data
    MULTIPART_AVAILABLE = True
except ImportError:
    MULTIPART_AVAILABLE = False

try:
    import pyembroidery
    PYEMBROIDERY_AVAILABLE = True
    print("pyembroidery loaded")
except ImportError:
    PYEMBROIDERY_AVAILABLE = False
    print("pyembroidery unavailable")

# ── professional stitch settings ────────────────────────────────────────────
PROFESSIONAL_SETTINGS = {
    'fill_density':         2.0,   # mm between rows
    'fill_stitch_length':   3.0,   # mm (tatami stitch length)
    'satin_stitch_length':  0.4,   # mm between satin needle-fall points
    'running_stitch_length':2.5,   # mm
    'underlay_density':     4.0,   # mm between underlay rows
    'max_stitch_length':    12.0,  # mm (VP3 safety cap)
    'min_stitch_length':    0.3,   # mm
    'satin_width_threshold':8.0,   # mm — shapes narrower than this → satin
    'underlay_angle':       90,    # degrees
    'max_stitches_per_block':8000,
    'target_width_mm':      100.0, # output design width
}

def _get_settings(overrides: Optional[Dict[str, Any]] = None) -> dict:
    """Return PROFESSIONAL_SETTINGS merged with caller overrides."""
    if not overrides:
        return dict(PROFESSIONAL_SETTINGS)
    merged = dict(PROFESSIONAL_SETTINGS)
    merged.update(overrides)
    return merged

# ── named thread colours (hex → (name, brand)) ─────────────────────────────
_THREAD_NAMES: Dict[str, Tuple[str, str]] = {
    '676767': ('Medium Gray',     'Madeira 1713'),
    '3a3939': ('Charcoal',        'Madeira 1234'),
    'dd2e2f': ('Crimson Red',     'Madeira 1308'),
    'ffffff': ('White',           'Madeira 1002'),
    '000000': ('Black',           'Madeira 1000'),
    'ff0000': ('Red',             'Madeira 1147'),
    '0000ff': ('Blue',            'Madeira 1082'),
    '00ff00': ('Green',           'Madeira 1043'),
    'ffff00': ('Yellow',          'Madeira 1070'),
    'ffa500': ('Orange',          'Madeira 1125'),
}

def _get_thread_name(hex_color: str) -> str:
    key = hex_color.lstrip('#').lower()
    if key in _THREAD_NAMES:
        name, brand = _THREAD_NAMES[key]
        return f"{name} ({brand})"
    return f"Thread #{hex_color.upper()}"

def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    h = hex_color.lstrip('#')
    if len(h) == 3:
        h = h[0]*2 + h[1]*2 + h[2]*2
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return r, g, b

_NAMED_COLORS: Dict[str, str] = {
    'black':'#000000','white':'#ffffff','red':'#ff0000','green':'#008000',
    'blue':'#0000ff','yellow':'#ffff00','orange':'#ffa500','purple':'#800080',
    'gray':'#808080','grey':'#808080','none':'none',
}

def _normalize_color(c: Optional[str]) -> str:
    if not c or c.strip() == '' or c.lower() == 'none':
        return 'none'
    c = c.strip()
    if c.startswith('#'):
        h = c.lstrip('#')
        if len(h) == 3:
            h = h[0]*2 + h[1]*2 + h[2]*2
        return '#' + h.lower()
    if c.lower() in _NAMED_COLORS:
        return _NAMED_COLORS[c.lower()]
    if c.startswith('rgb'):
        nums = re.findall(r'\d+', c)
        if len(nums) >= 3:
            return '#{:02x}{:02x}{:02x}'.format(int(nums[0]), int(nums[1]), int(nums[2]))
    return 'none'

# ── SVG CSS class parser ─────────────────────────────────────────────────────
def _parse_svg_css(svg_content: str) -> Dict[str, Dict[str, str]]:
    """Return {class_name: {prop: value, ...}} from all <style> blocks."""
    styles: Dict[str, Dict[str, str]] = {}
    for m in re.finditer(r'<style[^>]*>(.*?)</style>', svg_content, re.DOTALL | re.IGNORECASE):
        css = m.group(1)
        for rm in re.finditer(r'\.([\w-]+)\s*\{([^}]*)\}', css, re.DOTALL):
            cls, body = rm.group(1), rm.group(2)
            props: Dict[str, str] = {}
            for rule in body.split(';'):
                if ':' in rule:
                    k, v = rule.split(':', 1)
                    props[k.strip()] = v.strip()
            styles[cls] = props
    return styles

# ── SVG transform parser ─────────────────────────────────────────────────────
def _identity() -> List[float]:
    return [1, 0, 0, 1, 0, 0]   # a b c d e f  → [a,b,c,d,e,f]

def _mat_mul(A: List[float], B: List[float]) -> List[float]:
    """Multiply two 2-D affine matrices stored as [a,b,c,d,e,f]."""
    a1,b1,c1,d1,e1,f1 = A
    a2,b2,c2,d2,e2,f2 = B
    return [
        a1*a2 + c1*b2,   b1*a2 + d1*b2,
        a1*c2 + c1*d2,   b1*c2 + d1*d2,
        a1*e2 + c1*f2 + e1, b1*e2 + d1*f2 + f1,
    ]

def _parse_transform(t: str) -> List[float]:
    """Parse an SVG transform string and return the combined affine matrix."""
    m = _identity()
    if not t:
        return m
    for part in re.finditer(r'(\w+)\(([^)]*)\)', t):
        name = part.group(1).lower()
        args = [float(x) for x in re.split(r'[,\s]+', part.group(2).strip()) if x]
        if name == 'translate':
            tx = args[0] if args else 0
            ty = args[1] if len(args) > 1 else 0
            m = _mat_mul(m, [1,0,0,1,tx,ty])
        elif name == 'scale':
            sx = args[0] if args else 1
            sy = args[1] if len(args) > 1 else sx
            m = _mat_mul(m, [sx,0,0,sy,0,0])
        elif name == 'rotate':
            ang = math.radians(args[0])
            c_, s_ = math.cos(ang), math.sin(ang)
            if len(args) == 3:
                cx_, cy_ = args[1], args[2]
                m = _mat_mul(m, [1,0,0,1,cx_,cy_])
                m = _mat_mul(m, [c_,s_,-s_,c_,0,0])
                m = _mat_mul(m, [1,0,0,1,-cx_,-cy_])
            else:
                m = _mat_mul(m, [c_,s_,-s_,c_,0,0])
        elif name == 'skewx':
            m = _mat_mul(m, [1,0,math.tan(math.radians(args[0])),1,0,0])
        elif name == 'skewy':
            m = _mat_mul(m, [1,math.tan(math.radians(args[0])),0,1,0,0])
        elif name == 'matrix' and len(args) == 6:
            m = _mat_mul(m, args)
    return m

def _apply_matrix(coords: List[Tuple[float,float]], mat: List[float]) -> List[Tuple[float,float]]:
    a,b,c,d,e,f = mat
    return [(a*x + c*y + e, b*x + d*y + f) for x,y in coords]

# ── full SVG path parser ─────────────────────────────────────────────────────
_PATH_TOKEN = re.compile(r'([MmZzLlHhVvCcSsQqTtAa])|([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)')

def _tokenize(d: str):
    for m in _PATH_TOKEN.finditer(d):
        if m.group(1):
            yield m.group(1)
        else:
            yield float(m.group(2))

def _bezier_cubic(p0, p1, p2, p3, n=20):
    pts = []
    for i in range(1, n+1):
        t = i/n; mt = 1-t
        px = mt**3*p0[0]+3*mt**2*t*p1[0]+3*mt*t**2*p2[0]+t**3*p3[0]
        py = mt**3*p0[1]+3*mt**2*t*p1[1]+3*mt*t**2*p2[1]+t**3*p3[1]
        pts.append((px,py))
    return pts

def _bezier_quad(p0, p1, p2, n=14):
    pts = []
    for i in range(1, n+1):
        t = i/n; mt = 1-t
        px = mt**2*p0[0]+2*mt*t*p1[0]+t**2*p2[0]
        py = mt**2*p0[1]+2*mt*t*p1[1]+t**2*p2[1]
        pts.append((px,py))
    return pts

def _arc_to_lines(x0, y0, rx, ry, x_rot_deg, large_arc, sweep, x1, y1):
    """Approximate SVG arc with line segments."""
    if rx == 0 or ry == 0 or (x0 == x1 and y0 == y1):
        return [(x1, y1)]
    phi = math.radians(x_rot_deg)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx, dy = (x0-x1)/2, (y0-y1)/2
    x1p =  cos_phi*dx + sin_phi*dy
    y1p = -sin_phi*dx + cos_phi*dy
    rx, ry = abs(rx), abs(ry)
    lam = x1p**2/rx**2 + y1p**2/ry**2
    if lam > 1:
        s = math.sqrt(lam); rx *= s; ry *= s
    num = max(0, rx**2*ry**2 - rx**2*y1p**2 - ry**2*x1p**2)
    den = rx**2*y1p**2 + ry**2*x1p**2
    sq = math.sqrt(num/den) if den else 0
    if large_arc == sweep:
        sq = -sq
    cxp =  sq*rx*y1p/ry
    cyp = -sq*ry*x1p/rx
    cx = cos_phi*cxp - sin_phi*cyp + (x0+x1)/2
    cy = sin_phi*cxp + cos_phi*cyp + (y0+y1)/2
    def ang(ux,uy,vx,vy):
        d = (ux**2+uy**2)*(vx**2+vy**2)
        if d == 0: return 0
        c_ = max(-1,min(1,(ux*vx+uy*vy)/math.sqrt(d)))
        a = math.acos(c_)
        return -a if ux*vy-uy*vx < 0 else a
    theta1 = ang(1,0,(x1p-cxp)/rx,(y1p-cyp)/ry)
    dtheta = ang((x1p-cxp)/rx,(y1p-cyp)/ry,(-x1p-cxp)/rx,(-y1p-cyp)/ry)
    if not sweep and dtheta > 0: dtheta -= 2*math.pi
    if sweep  and dtheta < 0: dtheta += 2*math.pi
    n = max(4, int(abs(dtheta)*rx/2))
    pts = []
    for i in range(1, n+1):
        t = theta1 + dtheta*i/n
        xp = math.cos(t)*rx; yp = math.sin(t)*ry
        pts.append((cos_phi*xp - sin_phi*yp + cx,
                    sin_phi*xp + cos_phi*yp + cy))
    return pts

def parse_svg_path(d: str) -> List[List[Tuple[float,float]]]:
    """
    Parse SVG path data into a list of subpaths.
    Each subpath is a list of (x, y) coordinates.
    """
    if not d:
        return []
    tokens = list(_tokenize(d))
    ti = 0
    def peek():
        while ti < len(tokens) and isinstance(tokens[ti], str):
            return None
        return tokens[ti] if ti < len(tokens) else None
    def gn():
        nonlocal ti
        while ti < len(tokens) and isinstance(tokens[ti], str):
            break
        if ti < len(tokens) and isinstance(tokens[ti], float):
            v = tokens[ti]; ti += 1; return v
        return 0.0

    subpaths: List[List[Tuple[float,float]]] = []
    cur: List[Tuple[float,float]] = []
    x=y=sx=sy=0.0
    last_cmd=''; last_cp=(0.0,0.0)

    while ti < len(tokens):
        tok = tokens[ti]
        if isinstance(tok, str):
            cmd = tok; ti += 1
        elif last_cmd:
            # Implicit repetition of last command
            cmd = 'L' if last_cmd == 'M' else ('l' if last_cmd == 'm' else last_cmd)
        else:
            ti += 1; continue

        last_cmd = cmd
        UC = cmd.upper()
        rel = cmd.islower()

        def rx_(v): return x+v if rel else v
        def ry_(v): return y+v if rel else v

        if UC == 'M':
            if cur: subpaths.append(cur); cur = []
            nx, ny = gn(), gn()
            x,y = (x+nx,y+ny) if rel else (nx,ny)
            sx,sy = x,y
            cur.append((x,y))
            # Subsequent coords are implicit L
            last_cmd = 'l' if rel else 'L'
        elif UC == 'Z':
            cur.append((sx,sy)); subpaths.append(cur); cur=[]
            x,y = sx,sy
        elif UC == 'L':
            nx,ny = gn(),gn()
            x,y = rx_(nx),ry_(ny)
            cur.append((x,y))
        elif UC == 'H':
            v = gn(); x = x+v if rel else v
            cur.append((x,y))
        elif UC == 'V':
            v = gn(); y = y+v if rel else v
            cur.append((x,y))
        elif UC == 'C':
            x1,y1 = rx_(gn()),ry_(gn())
            x2,y2 = rx_(gn()),ry_(gn())
            ex,ey = rx_(gn()),ry_(gn())
            pts = _bezier_cubic((x,y),(x1,y1),(x2,y2),(ex,ey))
            cur.extend(pts)
            last_cp=(x2,y2); x,y=ex,ey
        elif UC == 'S':
            # Smooth cubic: reflect last control point
            if last_cmd.upper() in ('C','S'):
                x1,y1 = 2*x-last_cp[0], 2*y-last_cp[1]
            else:
                x1,y1 = x,y
            x2,y2 = rx_(gn()),ry_(gn())
            ex,ey = rx_(gn()),ry_(gn())
            pts = _bezier_cubic((x,y),(x1,y1),(x2,y2),(ex,ey))
            cur.extend(pts)
            last_cp=(x2,y2); x,y=ex,ey
        elif UC == 'Q':
            x1,y1 = rx_(gn()),ry_(gn())
            ex,ey = rx_(gn()),ry_(gn())
            pts = _bezier_quad((x,y),(x1,y1),(ex,ey))
            cur.extend(pts)
            last_cp=(x1,y1); x,y=ex,ey
        elif UC == 'T':
            if last_cmd.upper() in ('Q','T'):
                x1,y1 = 2*x-last_cp[0], 2*y-last_cp[1]
            else:
                x1,y1 = x,y
            ex,ey = rx_(gn()),ry_(gn())
            pts = _bezier_quad((x,y),(x1,y1),(ex,ey))
            cur.extend(pts)
            last_cp=(x1,y1); x,y=ex,ey
        elif UC == 'A':
            rx_v,ry_v = abs(gn()),abs(gn())
            xr = gn(); la = int(gn()); sw = int(gn())
            ex,ey = rx_(gn()),ry_(gn())
            pts = _arc_to_lines(x,y,rx_v,ry_v,xr,la,sw,ex,ey)
            cur.extend(pts)
            x,y = ex,ey

    if cur:
        subpaths.append(cur)
    return subpaths

# ── recursive SVG element traversal with style inheritance ───────────────────
SVG_NS = '{http://www.w3.org/2000/svg}'
_SHAPE_TAGS = {'path','rect','circle','ellipse','line','polyline','polygon'}

def _elem_style(elem, parent_props: dict, css_classes: dict) -> dict:
    props = dict(parent_props)
    # CSS class
    for cls in elem.get('class','').split():
        if cls in css_classes:
            props.update(css_classes[cls])
    # Inline style attribute
    for rule in elem.get('style','').split(';'):
        if ':' in rule:
            k,v = rule.split(':',1)
            props[k.strip()] = v.strip()
    # Direct attributes override
    for attr in ('fill','stroke','stroke-width','opacity','fill-opacity'):
        v = elem.get(attr)
        if v is not None:
            props[attr] = v
    return props

def _traverse(elem, parent_props: dict, parent_mat: List[float],
               css_classes: dict, depth: int = 0):
    """Yield (elem, effective_style_props, accumulated_matrix) for shape elements."""
    if depth > 30:
        return
    raw_tag = (elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag).lower()
    props = _elem_style(elem, parent_props, css_classes)
    mat = _mat_mul(parent_mat, _parse_transform(elem.get('transform','')))

    if raw_tag in _SHAPE_TAGS:
        yield (elem, props, mat, raw_tag)
    elif raw_tag in ('g','svg','a','symbol'):
        for child in elem:
            yield from _traverse(child, props, mat, css_classes, depth+1)

def extract_svg_elements_v2(svg_content: str):
    """
    Parse SVG and return list of element dicts with properly resolved colours,
    transforms, and shape data.
    """
    try:
        root = ET.fromstring(svg_content)
    except Exception as e:
        print(f"SVG parse error: {e}")
        return [], 100, 100

    css_classes = _parse_svg_css(svg_content)

    # Detect dimensions: viewBox > width/height > default 100
    vb_attr = root.get('viewBox')
    w_attr = root.get('width')
    h_attr = root.get('height')
    
    if vb_attr:
        parts = vb_attr.replace(',',' ').split()
        svg_w = float(parts[2]) if len(parts) >= 4 else 100.0
        svg_h = float(parts[3]) if len(parts) >= 4 else 100.0
    elif w_attr and h_attr:
        svg_w = _safe_float(w_attr, 100.0)
        svg_h = _safe_float(h_attr, 100.0)
    else:
        svg_w, svg_h = 100.0, 100.0

    elements = []
    for elem, props, mat, tag in _traverse(root, {}, _identity(), css_classes):
        fill   = _normalize_color(props.get('fill'))
        stroke = _normalize_color(props.get('stroke'))
        try:
            sw = float(props.get('stroke-width', '1').replace('px',''))
        except Exception:
            sw = 1.0

        elements.append({
            'tag':          tag,
            'fill':         fill,
            'stroke':       stroke,
            'stroke_width': sw,
            'matrix':       mat,
            'd':            elem.get('d',''),
            'x':   _safe_float(elem.get('x','0')),
            'y':   _safe_float(elem.get('y','0')),
            'width':  _safe_float(elem.get('width','0')),
            'height': _safe_float(elem.get('height','0')),
            'cx':  _safe_float(elem.get('cx','0')),
            'cy':  _safe_float(elem.get('cy','0')),
            'r':   _safe_float(elem.get('r','0')),
            'rx':  _safe_float(elem.get('rx','0')),
            'ry':  _safe_float(elem.get('ry','0')),
            'x1':  _safe_float(elem.get('x1','0')),
            'y1':  _safe_float(elem.get('y1','0')),
            'x2':  _safe_float(elem.get('x2','0')),
            'y2':  _safe_float(elem.get('y2','0')),
            'points': elem.get('points',''),
            'svg_width':  svg_w,
            'svg_height': svg_h,
        })
    return elements, svg_w, svg_h

def _safe_float(s, default=0.0):
    try:
        return float(str(s).replace('px',''))
    except Exception:
        return default

# ── coordinate extraction per shape type ────────────────────────────────────
def element_to_subpaths(el: dict) -> List[List[Tuple[float,float]]]:
    tag = el['tag']
    if tag == 'path':
        return parse_svg_path(el['d'])
    elif tag == 'rect':
        x,y,w,h = el['x'],el['y'],el['width'],el['height']
        return [[(x,y),(x+w,y),(x+w,y+h),(x,y+h),(x,y)]]
    elif tag == 'circle':
        cx,cy,r = el['cx'],el['cy'],el['r']
        pts = [(cx+r*math.cos(2*math.pi*i/32), cy+r*math.sin(2*math.pi*i/32)) for i in range(33)]
        return [pts]
    elif tag == 'ellipse':
        cx,cy,rx,ry = el['cx'],el['cy'],el['rx'],el['ry']
        pts = [(cx+rx*math.cos(2*math.pi*i/32), cy+ry*math.sin(2*math.pi*i/32)) for i in range(33)]
        return [pts]
    elif tag in ('polyline','polygon'):
        parts = [p for p in re.split(r'[,\s]+', el['points']) if p]
        pts = []
        for i in range(0, len(parts)-1, 2):
            try: pts.append((float(parts[i]), float(parts[i+1])))
            except ValueError: pass
        if tag == 'polygon' and pts and pts[0] != pts[-1]:
            pts.append(pts[0])
        return [pts] if pts else []
    elif tag == 'line':
        return [[(el['x1'], el['y1']), (el['x2'], el['y2'])]]
    return []

# ── coordinate scaling ───────────────────────────────────────────────────────
def scale_coords(coords: List[Tuple[float,float]],
                 svg_w: float, svg_h: float,
                 target_mm: float = None,
                 settings: Optional[dict] = None) -> List[Tuple[float,float]]:
    if not coords:
        return []
    s = _get_settings(settings)
    target_mm = target_mm or s['target_width_mm']
    scale = target_mm / max(svg_w, svg_h)
    return [(x*scale, y*scale) for x,y in coords]

# ── polygon bounding helpers ─────────────────────────────────────────────────
def _bbox(pts):
    if not pts:
        return 0,0,0,0
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return min(xs),min(ys),max(xs),max(ys)

def _path_length(pts):
    if len(pts) < 2:
        return 0
    return sum(math.hypot(pts[i+1][0]-pts[i][0], pts[i+1][1]-pts[i][1]) for i in range(len(pts)-1))

def _shape_narrow_width(pts):
    """Estimate the 'narrow' dimension of a shape (for satin vs fill decision)."""
    minx,miny,maxx,maxy = _bbox(pts)
    w = maxx - minx; h = maxy - miny
    return min(w, h)

# ── scanline fill ────────────────────────────────────────────────────────────
def generate_scanline_fill(polygon: List[Tuple[float,float]],
                           row_spacing: float = None,
                           stitch_length: float = None,
                           angle_deg: float = 45.0,
                           settings: Optional[dict] = None) -> List[Tuple[float,float]]:
    """
    Tatami scanline fill.  Rotates the polygon by -angle, fills with horizontal
    scan lines, then rotates the output points back.
    """
    s = _get_settings(settings)
    row_spacing   = row_spacing   or s['fill_density']
    stitch_length = stitch_length or s['fill_stitch_length']

    if len(polygon) < 3:
        return []

    # Rotate polygon for angled fill
    ang = math.radians(-angle_deg)
    ca, sa = math.cos(ang), math.sin(ang)
    def rot_in(p):  return ( ca*p[0] - sa*p[1],  sa*p[0] + ca*p[1])
    def rot_out(p): return ( ca*p[0] + sa*p[1], -sa*p[0] + ca*p[1])

    poly_r = [rot_in(p) for p in polygon]
    minx,miny,maxx,maxy = _bbox(poly_r)

    n = len(poly_r)
    stitches = []
    row = 0
    y = miny + row_spacing / 2

    while y <= maxy:
        # Find all x-intersections at this y
        intersections = []
        for i in range(n):
            p1 = poly_r[i]; p2 = poly_r[(i+1)%n]
            y1,y2 = p1[1],p2[1]
            if (y1 <= y < y2) or (y2 <= y < y1):
                t = (y - y1) / (y2 - y1)
                xi = p1[0] + t*(p2[0]-p1[0])
                intersections.append(xi)
        intersections.sort()

        # Fill between pairs of intersections
        for k in range(0, len(intersections)-1, 2):
            x_start = intersections[k] + 0.2
            x_end   = intersections[k+1] - 0.2
            if x_end <= x_start:
                continue
            # Alternate row direction
            if row % 2 == 0:
                xi = x_start
                while xi <= x_end:
                    stitches.append(rot_out((xi, y)))
                    xi += stitch_length
            else:
                xi = x_end
                while xi >= x_start:
                    stitches.append(rot_out((xi, y)))
                    xi -= stitch_length

        row += 1
        y += row_spacing

    return stitches

# ── underlay stitches ────────────────────────────────────────────────────────
def generate_underlay(polygon: List[Tuple[float,float]],
                      angle_deg: float = 0.0,
                      settings: Optional[dict] = None) -> List[Tuple[float,float]]:
    s = _get_settings(settings)
    return generate_scanline_fill(polygon,
                                  row_spacing=s['underlay_density'],
                                  stitch_length=4.0,
                                  angle_deg=angle_deg,
                                  settings=settings)

# ── satin column ────────────────────────────────────────────────────────────
def generate_satin_column(path_pts: List[Tuple[float,float]],
                           width_mm: float = 3.0,
                           settings: Optional[dict] = None) -> List[Tuple[float,float]]:
    """
    Generate proper satin column stitches along a path.
    Each stitch crosses the path perpendicularly by ±width/2.
    """
    if len(path_pts) < 2:
        return []
    s = _get_settings(settings)
    sl = max(s['satin_stitch_length'], 0.3)

    # Re-sample path to equal-spacing points
    total = _path_length(path_pts)
    if total < 0.5:
        return []
    n_pts = max(2, int(total / sl))
    resampled = []
    accumulated = 0.0
    resampled.append(path_pts[0])
    for i in range(len(path_pts)-1):
        seg = math.hypot(path_pts[i+1][0]-path_pts[i][0],
                         path_pts[i+1][1]-path_pts[i][1])
        if seg == 0:
            continue
        for j in range(1, max(2, int(seg/sl))):
            t = j*sl/seg
            if t > 1: break
            px = path_pts[i][0]+t*(path_pts[i+1][0]-path_pts[i][0])
            py = path_pts[i][1]+t*(path_pts[i+1][1]-path_pts[i][1])
            resampled.append((px,py))
    resampled.append(path_pts[-1])

    stitches = []
    half = width_mm / 2.0
    for i, (px, py) in enumerate(resampled):
        # Compute tangent direction
        if i == 0:
            dx = resampled[1][0]-px; dy = resampled[1][1]-py
        elif i == len(resampled)-1:
            dx = px-resampled[-2][0]; dy = py-resampled[-2][1]
        else:
            dx = resampled[i+1][0]-resampled[i-1][0]
            dy = resampled[i+1][1]-resampled[i-1][1]
        length = math.hypot(dx,dy)
        if length == 0:
            continue
        # Perpendicular
        nx = -dy/length; ny = dx/length
        side = half if i%2==0 else -half
        stitches.append((px + nx*side, py + ny*side))

    return stitches

# ── running stitch ───────────────────────────────────────────────────────────
def generate_running_stitches(pts: List[Tuple[float,float]],
                               stitch_length: float = None,
                               settings: Optional[dict] = None) -> List[Tuple[float,float]]:
    s = _get_settings(settings)
    sl = stitch_length or s['running_stitch_length']
    if len(pts) < 2:
        return pts[:]
    out = [pts[0]]
    for i in range(len(pts)-1):
        p1, p2 = pts[i], pts[i+1]
        d = math.hypot(p2[0]-p1[0], p2[1]-p1[1])
        if d < 0.1:
            continue
        n = max(1, int(d/sl))
        for j in range(1, n+1):
            t = j/n
            out.append((p1[0]+t*(p2[0]-p1[0]), p1[1]+t*(p2[1]-p1[1])))
    return out

# ── stitch limit & trim ──────────────────────────────────────────────────────
def _cap_stitch_distance(stitches, max_mm=None, settings=None):
    """Insert JUMP positions where consecutive stitches exceed max distance."""
    s = _get_settings(settings)
    max_mm = max_mm or s['max_stitch_length']
    if not stitches:
        return stitches
    out = [stitches[0]]
    for i in range(1, len(stitches)):
        dx = stitches[i][0]-stitches[i-1][0]
        dy = stitches[i][1]-stitches[i-1][1]
        d = math.hypot(dx, dy)
        if d > max_mm:
            # Interpolate intermediate points
            n = int(d/max_mm)+1
            for k in range(1, n):
                t = k/n
                out.append((stitches[i-1][0]+t*dx, stitches[i-1][1]+t*dy))
        out.append(stitches[i])
    return out

# ── main conversion ──────────────────────────────────────────────────────────
# Per-colour fill angle variation (so different coloured areas look distinct)
_FILL_ANGLES = {
    '#676767': 45.0,
    '#3a3939': 135.0,
    '#dd2e2f': 60.0,
    '#ffffff': 90.0,
    '#000000': 45.0,
}

def _fill_angle_for(color: str) -> float:
    return _FILL_ANGLES.get(color.lower(), 45.0)

def add_svg_to_pattern(pattern, svg_content: str, settings: Optional[dict] = None):
    """Convert SVG to pyembroidery pattern with correct colours, satin & fill."""
    elements, svg_w, svg_h = extract_svg_elements_v2(svg_content)
    if not elements:
        print("No elements found in SVG")
        return

    s = _get_settings(settings)
    stitch_blocks = []   # [{'color': str, 'stitches': [...], 'type': str}]

    for i, el in enumerate(elements):
        subpaths = element_to_subpaths(el)
        if not subpaths:
            print(f"Element {i} ({el['tag']}) produced no subpaths")
            continue

        mat  = el['matrix']
        fill = el['fill']
        stroke = el['stroke']
        sw     = el['stroke_width']

        for subpath in subpaths:
            if not subpath:
                continue
            # Apply transform then scale to mm
            transformed = _apply_matrix(subpath, mat)
            scaled       = scale_coords(transformed, svg_w, svg_h, settings=settings)
            if len(scaled) < 2:
                continue

            # ── filled area ──────────────────────────────────────────────
            if fill and fill != 'none' and len(scaled) >= 3:
                closed = list(scaled)
                if closed[0] != closed[-1]:
                    closed.append(closed[0])

                narrow = _shape_narrow_width(closed)
                angle  = _fill_angle_for(fill)

                if narrow < s['satin_width_threshold']:
                    # Satin for narrow shapes (legs, narrow body segments)
                    stitches = generate_satin_column(scaled, width_mm=max(1.0, narrow*0.9), settings=settings)
                else:
                    # Underlay + tatami fill for wide shapes
                    underlay = generate_underlay(closed, angle_deg=angle+90, settings=settings)
                    fill_st  = generate_scanline_fill(closed, angle_deg=angle, settings=settings)
                    stitches = underlay + fill_st

                stitches = _cap_stitch_distance(stitches, settings=settings)
                if stitches:
                    stitch_blocks.append({'color': fill,
                                          'stitches': stitches,
                                          'type': 'fill'})
                else:
                    print(f"Element {i} ({el['tag']}) fill produced 0 stitches (narrow={narrow:.2f}mm)")

            # ── stroked outline ──────────────────────────────────────────
            if stroke and stroke != 'none' and sw > 0:
                sw_mm = sw * s['target_width_mm'] / max(svg_w, svg_h)
                if sw_mm >= s['satin_width_threshold'] * 0.5:
                    # Wide stroke → satin column
                    stitches = generate_satin_column(scaled, width_mm=max(1.0, sw_mm), settings=settings)
                else:
                    # Thin stroke → running stitch
                    stitches = generate_running_stitches(scaled, settings=settings)

                stitches = _cap_stitch_distance(stitches, settings=settings)
                if stitches:
                    stitch_blocks.append({'color': stroke,
                                          'stitches': stitches,
                                          'type': 'stroke'})
                else:
                    print(f"Element {i} ({el['tag']}) stroke produced 0 stitches")

    if not stitch_blocks:
        print("No stitch blocks generated")
        return

    # Group same-colour blocks together (preserving first-occurrence order)
    ordered_colors: List[str] = list(OrderedDict.fromkeys(b['color'] for b in stitch_blocks))
    colour_groups: Dict[str, List] = {c: [] for c in ordered_colors}
    for b in stitch_blocks:
        colour_groups[b['color']].append(b)

    # Add threads in the exact order they will be stitched
    for i, color in enumerate(ordered_colors):
        try:
            r,g,b_ = _hex_to_rgb(color)
            int_color = (r << 16) | (g << 8) | b_
        except Exception:
            int_color = 0
        pattern.add_thread({
            'color': int_color,
            'hex':   color.lstrip('#'),
            'name':  _get_thread_name(color),
        })

    # Emit stitches, adding COLOR_BREAK between colour groups
    first_block = True
    for i, color in enumerate(ordered_colors):
        if not first_block:
            pattern.add_stitch_absolute(pyembroidery.COLOR_BREAK, 0, 0)
        first_block = False

        for block in colour_groups[color]:
            stitches = block['stitches']
            if not stitches:
                continue
            # Jump to start
            pattern.add_stitch_absolute(pyembroidery.JUMP, stitches[0][0]*10, stitches[0][1]*10)
            for j, (sx, sy) in enumerate(stitches):
                if math.isnan(sx) or math.isnan(sy): continue
                if math.isinf(sx) or math.isinf(sy): continue
                pattern.add_stitch_absolute(pyembroidery.STITCH, sx*10, sy*10)
            # Trim at end of each block
            lx, ly = stitches[-1]
            pattern.add_stitch_absolute(pyembroidery.TRIM, lx*10, ly*10)

    # End of pattern
    pattern.add_stitch_absolute(pyembroidery.END, 0, 0)

def convert_svg_to_vp3(svg_content: str, settings: Optional[dict] = None) -> bytes:
    """Convert SVG content to VP3 embroidery format (primary public API)."""
    vp3, _ = convert_svg_to_vp3_with_pattern(svg_content, settings=settings)
    return vp3

def convert_svg_to_vp3_with_pattern(svg_content: str, settings: Optional[dict] = None):
    """Convert SVG to VP3 and also return the pyembroidery pattern object.
    Returns (vp3_bytes, pattern_or_None)."""
    if not PYEMBROIDERY_AVAILABLE:
        return create_simple_vp3_file(svg_content), None

    try:
        pattern = pyembroidery.EmbPattern()
        add_svg_to_pattern(pattern, svg_content, settings=settings)

        buf = BytesIO()
        pyembroidery.write_vp3(pattern, buf)
        result = buf.getvalue()
        if result:
            return result, pattern

        # Fallback: DST
        buf2 = BytesIO()
        pyembroidery.write_dst(pattern, buf2)
        return buf2.getvalue(), pattern

    except Exception as e:
        print(f"convert_svg_to_vp3 error: {e}\n{traceback.format_exc()}")
        return create_simple_vp3_file(svg_content), None

# ── legacy helpers kept for backward compatibility ───────────────────────────
def extract_svg_elements(svg_content: str):
    """Legacy wrapper — returns list compatible with old callers."""
    elements, svg_w, svg_h = extract_svg_elements_v2(svg_content)
    for e in elements:
        e['svg_width']  = svg_w
        e['svg_height'] = svg_h
    return elements

def convert_path_to_coordinates(path_data: str, transform: str = None):
    """Legacy wrapper — returns flat list of (x,y) from first subpath."""
    subs = parse_svg_path(path_data)
    if not subs:
        return []
    pts = subs[0]
    if transform:
        mat = _parse_transform(transform)
        pts = _apply_matrix(pts, mat)
    return pts

def scale_coordinates(coords, svg_width, svg_height, target_width=100):
    return scale_coords(coords, svg_width, svg_height, target_width)

def generate_fill_stitches(coords, angle=None, density=None):
    if len(coords) < 3:
        return []
    closed = list(coords)
    if closed[0] != closed[-1]:
        closed.append(closed[0])
    return generate_scanline_fill(closed, row_spacing=density,
                                  angle_deg=angle or 45.0)

def generate_satin_stitches(coords, width=3.0):
    return generate_satin_column(coords, width_mm=width)

def optimize_stitch_order(stitch_blocks):
    """Keep first-occurrence colour order; sort blocks within each colour by proximity."""
    if not stitch_blocks:
        return []
    seen_colors = []
    color_groups = {}
    for b in stitch_blocks:
        c = b.get('color','#000000')
        if c not in color_groups:
            color_groups[c] = []
            seen_colors.append(c)
        color_groups[c].append(b)
    result = []
    for c in seen_colors:
        blocks = color_groups[c]
        ordered = []
        remaining = list(blocks)
        cur_pos = (0.0, 0.0)
        while remaining:
            nearest = min(remaining, key=lambda b: math.hypot(
                b['stitches'][0][0]-cur_pos[0], b['stitches'][0][1]-cur_pos[1])
                if b['stitches'] else float('inf'))
            ordered.append(nearest)
            remaining.remove(nearest)
            if nearest['stitches']:
                cur_pos = nearest['stitches'][-1]
        result.extend(ordered)
    return result

# ── simple VP3 fallback (no pyembroidery) ───────────────────────────────────
def create_simple_vp3_file(svg_content: str, settings: Optional[dict] = None) -> bytes:
    """Create a minimal VP3 without pyembroidery (fallback path)."""
    try:
        elements, svg_w, svg_h = extract_svg_elements_v2(svg_content)
        all_stitches = []
        for el in elements:
            if el['fill'] == 'none':
                continue
            for sp in element_to_subpaths(el):
                scaled = scale_coords(_apply_matrix(sp, el['matrix']), svg_w, svg_h, settings=settings)
                all_stitches.extend(generate_scanline_fill(scaled, settings=settings))
        if not all_stitches:
            return create_default_vp3_file()
        return create_vp3_file_with_stitches(all_stitches, ['#000000'])
    except Exception as e:
        print(f"create_simple_vp3_file error: {e}")
        return create_default_vp3_file()

def create_default_vp3_file() -> bytes:
    stitches = [(0,0),(100,0),(100,100),(0,100),(0,0)]
    return create_vp3_file_with_stitches(stitches, ['#000000'])

def create_vp3_file_with_stitches(stitches, colors) -> bytes:
    vp3 = bytearray()
    vp3.extend(b'#VP3')
    vp3.extend(b'\x00\x00\x00\x00\x01\x00\x00\x00')
    vp3.extend(b'\x64\x00\x64\x00\x00\x00\x00\x00')
    for i,c in enumerate(colors):
        vp3.extend(f'Thread {i+1}\x00'.encode('ascii'))
    vp3.extend(b'\x00\x00')
    for i,(x,y) in enumerate(stitches):
        cmd = b'\x00\x01' if i==0 else b'\x00\x02'
        vp3.extend(cmd)
        vp3.extend(struct.pack('<H', max(0, min(0xFFFF, int(x*10)))))
        vp3.extend(struct.pack('<H', max(0, min(0xFFFF, int(y*10)))))
    vp3.extend(b'\x00\x00')
    return bytes(vp3)

# ── count / assess helpers ───────────────────────────────────────────────────
def count_stitches_in_vp3(vp3_content: bytes) -> int:
    if not vp3_content or len(vp3_content) < 8:
        return 0
    if PYEMBROIDERY_AVAILABLE:
        try:
            pat = pyembroidery.EmbPattern()
            pyembroidery.read(pat, 'x.vp3', {'data': vp3_content})
            return len([s for s in pat.stitches if s[2] == pyembroidery.STITCH])
        except Exception:
            pass
    # Rough estimate: every 6 bytes could be a stitch record
    return max(0, (len(vp3_content)-20)//6)

def assess_embroidery_quality(stitch_count: int, vp3_content: bytes) -> dict:
    dims = extract_vp3_dimensions(vp3_content)
    if stitch_count < 20:   lvl,cpx = 'basic','very_simple'
    elif stitch_count < 300: lvl,cpx = 'basic','simple'
    elif stitch_count < 1000:lvl,cpx = 'good','moderate'
    elif stitch_count < 5000:lvl,cpx = 'high','complex'
    else:                    lvl,cpx = 'professional','highly_complex'
    area = max(1, dims['width']*dims['height'])
    return {'level':lvl,'complexity':cpx,'dimensions':dims,'stitch_density':stitch_count/area}

def extract_vp3_dimensions(vp3_content: bytes) -> dict:
    try:
        if len(vp3_content) >= 20:
            w = struct.unpack('<H', vp3_content[16:18])[0]*0.1
            h = struct.unpack('<H', vp3_content[18:20])[0]*0.1
            return {'width':w,'height':h}
    except Exception:
        pass
    return {'width':0,'height':0}

def get_cors_headers() -> dict:
    return {
        'Access-Control-Allow-Origin':  '*',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        'Content-Type': 'application/json',
    }

def parse_multipart_data(body: str) -> Optional[str]:
    if MULTIPART_AVAILABLE:
        try:
            files, _ = parse_form_data(body)
            for _, f in files.items():
                if hasattr(f,'file') and getattr(f,'content_type','') == 'image/svg+xml':
                    return f.file.read().decode('utf-8')
        except Exception:
            pass
    if 'image/svg+xml' in body and '<svg' in body:
        s = body.find('<svg')
        e = body.rfind('</svg>')
        if s != -1 and e != -1:
            return body[s:e+6]
    return None