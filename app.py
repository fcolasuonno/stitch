from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import base64
import traceback
import os
import io
import json
import re
import threading
import xml.etree.ElementTree as ET
import core.converter as _converter_module
from core.converter import convert_svg_to_vp3_with_pattern, count_stitches_in_vp3, assess_embroidery_quality

try:
    import pyembroidery
    PYEMBROIDERY_AVAILABLE = True
except ImportError:
    PYEMBROIDERY_AVAILABLE = False

try:
    import vtracer
    VTRACER_AVAILABLE = True
except ImportError:
    VTRACER_AVAILABLE = False

try:
    from PIL import Image as PILImage
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RASTER_TYPES = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/bmp": "bmp", "image/gif": "gif", "image/tiff": "tiff", "image/webp": "webp",
}
RASTER_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp"}
_settings_lock = threading.Lock()


def _guess_format(filename: str, content_type: str) -> Optional[str]:
    ext = os.path.splitext(filename.lower())[1]
    if ext in RASTER_EXTENSIONS:
        return ext.lstrip(".")
    return RASTER_TYPES.get(content_type)


# ── colour helpers ────────────────────────────────────────────────────────────
def _parse_svg_color(val: str) -> Optional[str]:
    if not val or val.strip().lower() in ("none", "transparent", "inherit", "currentcolor"):
        return None
    val = val.strip()
    if val.startswith("#"):
        h = val[1:]
        if len(h) == 3:
            h = h[0]*2 + h[1]*2 + h[2]*2
        if len(h) == 6:
            return "#" + h.lower()
    m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", val, re.I)
    if m:
        return "#{:02x}{:02x}{:02x}".format(int(m[1]), int(m[2]), int(m[3]))
    return None


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return "#{:02x}{:02x}{:02x}".format(r, g, b)


def _color_distance_sq(c1, c2) -> float:
    return sum((a - b) ** 2 for a, b in zip(c1[:3], c2[:3]))


# ── raster background removal (flood-fill, topologically correct) ─────────────
# We use a "magic" marker colour that will not appear in any real design.
# After tracing, vtracer turns the marker region into a path of that exact colour,
# which we then auto-exclude from the SVG — leaving enclosed same-colour shapes
# (e.g. white text inside a black circle) completely untouched.
_BG_MARKER = (254, 1, 254)       # ~magenta; almost impossible in real artwork
_BG_MARKER_HEX = "#fe01fe"

def _flood_fill_background(img_bytes: bytes, threshold: int = 30) -> bytes:
    """
    BFS flood-fill from all four corners.  Pixels reachable from the border
    that are within *threshold* colour-distance of the corner colour are
    replaced with _BG_MARKER.  Enclosed pixels of the same colour are NOT
    touched because they are not reachable from the edge.
    Returns modified PNG bytes.
    """
    if not PILLOW_AVAILABLE:
        raise RuntimeError("Pillow is not installed. Run: pip install Pillow")

    img = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    pixels = img.load()

    # Determine background colour from the four corners
    corners = [pixels[0,0], pixels[w-1,0], pixels[0,h-1], pixels[w-1,h-1]]
    bg = max(set(corners), key=corners.count)
    thr2 = threshold ** 2

    visited = [[False] * h for _ in range(w)]
    queue = []
    for sx, sy in [(0,0),(w-1,0),(0,h-1),(w-1,h-1)]:
        if _color_distance_sq(pixels[sx,sy], bg) <= thr2:
            visited[sx][sy] = True
            queue.append((sx, sy))

    while queue:
        x, y = queue.pop()
        pixels[x, y] = _BG_MARKER
        for dx, dy in ((-1,0),(1,0),(0,-1),(0,1)):
            nx, ny = x+dx, y+dy
            if 0 <= nx < w and 0 <= ny < h and not visited[nx][ny]:
                if _color_distance_sq(pixels[nx,ny], bg) <= thr2:
                    visited[nx][ny] = True
                    queue.append((nx, ny))

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _detect_svg_bg_color(svg_str: str) -> Optional[str]:
    """
    Return the fill colour of the first full-size background <rect> found in
    the SVG, if any.  Also detects background <path> shapes from vtracer output
    by looking at the first (bottom-most) filled path in the document.
    """
    try:
        root = ET.fromstring(svg_str)
        vb = root.get("viewBox", "")
        vb_parts = re.split(r"[\s,]+", vb.strip())
        vb_w = float(vb_parts[2]) if len(vb_parts) >= 4 else None
        vb_h = float(vb_parts[3]) if len(vb_parts) >= 4 else None
        svg_w = float(root.get("width", vb_w or 0) or vb_w or 0)
        svg_h = float(root.get("height", vb_h or 0) or vb_h or 0)

        def _fill(el):
            f = el.get("fill") or ""
            style = el.get("style", "")
            if not f:
                m = re.search(r"fill\s*:\s*([^;]+)", style)
                if m:
                    f = m.group(1).strip()
            return _parse_svg_color(f)

        def _scan(el):
            for child in el:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag == "rect":
                    try:
                        rw_str = child.get("width", "0")
                        rh_str = child.get("height", "0")
                        rw = svg_w if "%" in rw_str else float(rw_str)
                        rh = svg_h if "%" in rh_str else float(rh_str)
                        rx = float(child.get("x", "0"))
                        ry = float(child.get("y", "0"))
                        if (svg_w and svg_h and
                                rw >= svg_w * 0.9 and rh >= svg_h * 0.9 and
                                rx == 0 and ry == 0):
                            c = _fill(child)
                            if c:
                                return c
                    except ValueError:
                        pass
                # For vtracer output: first path (or path in first group) is the background
                if tag in ("path", "g"):
                    c = _fill(child)
                    if c:
                        return c
                result = _scan(child)
                if result:
                    return result
            return None

        return _scan(root)
    except Exception as e:
        print(f"SVG BG colour detection failed: {e}")
        return None


# ── SVG colour tools ──────────────────────────────────────────────────────────
def _extract_svg_colors(svg_str: str) -> list:
    colors = set()
    for pattern in [
        r'fill\s*=\s*"([^"]+)"',
        r"fill\s*=\s*'([^']+)'",
        r'stroke\s*=\s*"([^"]+)"',
        r"stroke\s*=\s*'([^']+)'",
        r'fill\s*:\s*([^;}"\']+)',
    ]:
        for m in re.finditer(pattern, svg_str, re.I):
            c = _parse_svg_color(m.group(1).strip())
            if c:
                colors.add(c)
    return sorted(colors)


def _remap_svg_colors(svg_str: str, color_map: dict) -> str:
    """
    Remap fill/stroke colours in an SVG.
    color_map: { "#rrggbb": "#rrggbb" | null }
    null → element removed.
    """
    if not color_map:
        return svg_str
    norm_map = {}
    for k, v in color_map.items():
        nk = _parse_svg_color(k)
        if nk:
            norm_map[nk] = _parse_svg_color(v) if v else None

    try:
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
        root = ET.fromstring(svg_str)

        def _dominant_color(el):
            fill = _parse_svg_color(el.get("fill", ""))
            style_fill = None
            style_str = el.get("style", "")
            if style_str:
                m = re.search(r"fill\s*:\s*([^;]+)", style_str)
                if m:
                    style_fill = _parse_svg_color(m.group(1).strip())
            stroke = _parse_svg_color(el.get("stroke", ""))
            return fill or style_fill or stroke, fill, style_fill, stroke, style_str

        def remap_el(parent):
            to_remove = []
            for el in list(parent):
                dominant, fill, style_fill, stroke, style_str = _dominant_color(el)
                if dominant and dominant in norm_map:
                    new_color = norm_map[dominant]
                    if new_color is None:
                        to_remove.append(el)
                        continue
                    if fill:
                        el.set("fill", new_color)
                    if stroke:
                        el.set("stroke", new_color)
                    if style_fill and style_str:
                        el.set("style", re.sub(
                            r"(fill\s*:)\s*[^;]+", r"\g<1>" + new_color, style_str
                        ))
                remap_el(el)
            for el in to_remove:
                parent.remove(el)

        remap_el(root)
        return ET.tostring(root, encoding="unicode", xml_declaration=False)
    except Exception as e:
        print(f"SVG colour remap failed: {e}")
        return svg_str


def _remove_svg_background(svg_str: str) -> str:
    """
    Remove explicit background rects from an SVG file (not vtracer output).
    For vtracer output, use _detect_svg_bg_color + _remap_svg_colors instead.
    """
    try:
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
        root = ET.fromstring(svg_str)
        vb = root.get("viewBox", "")
        vb_parts = re.split(r"[\s,]+", vb.strip())
        vb_w = float(vb_parts[2]) if len(vb_parts) >= 4 else None
        vb_h = float(vb_parts[3]) if len(vb_parts) >= 4 else None
        svg_w = float(root.get("width", vb_w or 0) or vb_w or 0)
        svg_h = float(root.get("height", vb_h or 0) or vb_h or 0)

        def is_bg_rect(el):
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag != "rect":
                return False
            try:
                rw_str = el.get("width", "0")
                rh_str = el.get("height", "0")
                rw = svg_w if "%" in rw_str else float(rw_str)
                rh = svg_h if "%" in rh_str else float(rh_str)
                rx = float(el.get("x", "0"))
                ry = float(el.get("y", "0"))
            except ValueError:
                return False
            if svg_w and svg_h:
                if rw < svg_w * 0.9 or rh < svg_h * 0.9:
                    return False
            return rx == 0 and ry == 0

        def strip_bg(parent):
            to_remove = [c for c in list(parent) if is_bg_rect(c)]
            for el in to_remove:
                parent.remove(el)
            for child in list(parent):
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag not in ("defs", "style"):
                    strip_bg(child)

        strip_bg(root)
        style = root.get("style", "")
        style = re.sub(r"background(-color)?\s*:[^;]+;?", "", style, flags=re.I).strip()
        if style:
            root.set("style", style)
        elif "style" in root.attrib:
            del root.attrib["style"]
        return ET.tostring(root, encoding="unicode", xml_declaration=False)
    except Exception as e:
        print(f"SVG background rect removal failed: {e}")
        return svg_str


def _merge_color_maps(auto: Optional[str], user: Optional[str]) -> Optional[str]:
    """Merge an auto-detected exclusion map with the user-supplied one."""
    combined = {}
    if auto:
        try:
            combined.update(json.loads(auto))
        except Exception:
            pass
    if user:
        try:
            combined.update(json.loads(user))
        except Exception:
            pass
    return json.dumps(combined) if combined else None


# ── thread-safe settings patch ────────────────────────────────────────────────
def _convert_with_settings(svg_content: str, vp3_params: dict):
    overrides = {k: v for k, v in vp3_params.items() if v is not None}
    original = dict(_converter_module.PROFESSIONAL_SETTINGS)
    with _settings_lock:
        _converter_module.PROFESSIONAL_SETTINGS.update(overrides)
        try:
            result = convert_svg_to_vp3_with_pattern(svg_content)
        finally:
            _converter_module.PROFESSIONAL_SETTINGS.clear()
            _converter_module.PROFESSIONAL_SETTINGS.update(original)
    return result


# ── embroidery preview ────────────────────────────────────────────────────────
def _generate_preview_svg(pattern) -> str:
    if not PYEMBROIDERY_AVAILABLE or pattern is None:
        return ""
    try:
        stitches = pattern.stitches
        if not stitches:
            return ""
        xs = [s[0] for s in stitches]
        ys = [s[1] for s in stitches]
        if not xs or not ys:
            return ""
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        padding = 50
        width = max_x - min_x + padding * 2
        height = max_y - min_y + padding * 2
        if width <= 0 or height <= 0:
            return ""
        thread_colors = []
        for t in pattern.threadlist:
            c = t.color
            if isinstance(c, int):
                thread_colors.append('#{:06x}'.format(c & 0xFFFFFF))
            elif hasattr(t, 'hex') and t.hex:
                thread_colors.append('#' + t.hex.lstrip('#'))
            else:
                thread_colors.append('#000000')
        if not thread_colors:
            thread_colors = ['#000000']

        import math as _pmath

        def _emit_polyline(pts_list, color, lines_out):
            """
            Emit a polyline with stroke-width scaled to the run length.
            Long runs (fill stitches) → thin + semi-transparent (don't dominate).
            Short runs (outline stitches) → thicker + opaque (show design edges).
            """
            if len(pts_list) < 2:
                return
            # Measure total run length
            total_len = sum(
                _pmath.hypot(
                    float(pts_list[j].split(',')[0]) - float(pts_list[j-1].split(',')[0]),
                    float(pts_list[j].split(',')[1]) - float(pts_list[j-1].split(',')[1])
                )
                for j in range(1, len(pts_list))
            )
            if total_len > 300:        # long fill run: thin + semi-transparent
                sw, opacity = 1.5, 0.55
            elif total_len > 60:       # medium outline: normal
                sw, opacity = 2.5, 0.85
            else:                      # short run: full weight
                sw, opacity = 3.0, 1.0
            lines_out.append(
                f'<polyline points="{" ".join(pts_list)}" fill="none" stroke="{color}"'
                f' stroke-width="{sw}" opacity="{opacity}"'
                f' stroke-linecap="round" stroke-linejoin="round"/>'
            )

        lines = []
        current_points = []
        color_idx = 0
        for s in stitches:
            x, y, cmd = s[0], s[1], s[2]
            sx = x - min_x + padding
            sy = y - min_y + padding
            if cmd == pyembroidery.STITCH:
                current_points.append(f"{sx:.1f},{sy:.1f}")
            elif cmd in (pyembroidery.COLOR_CHANGE, pyembroidery.COLOR_BREAK):
                _emit_polyline(current_points, thread_colors[color_idx % len(thread_colors)], lines)
                current_points = []
                color_idx += 1
            elif cmd in (pyembroidery.JUMP, pyembroidery.TRIM):
                _emit_polyline(current_points, thread_colors[color_idx % len(thread_colors)], lines)
                current_points = [f"{sx:.1f},{sy:.1f}"]
            elif cmd == pyembroidery.END:
                break
            else:
                current_points.append(f"{sx:.1f},{sy:.1f}")
        _emit_polyline(current_points, thread_colors[color_idx % len(thread_colors)], lines)

        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}"'
            f' width="600" height="{600 * height / width:.0f}"'
            f' style="background:#1a1a2e;border-radius:10px">'
            + ''.join(lines) + '</svg>'
        )
        return base64.b64encode(svg.encode('utf-8')).decode('utf-8')
    except Exception as e:
        print(f"Preview generation failed: {e}")
        traceback.print_exc()
    return ""


def _run_vtracer(img_bytes: bytes, img_format: str, params: dict) -> str:
    if not VTRACER_AVAILABLE:
        raise RuntimeError("vtracer is not installed. Run: pip install vtracer")
    clean = {k: v for k, v in params.items() if v is not None}
    return vtracer.convert_raw_image_to_svg(img_bytes, img_format=img_format, **clean)


# ── /api/extract-colors ───────────────────────────────────────────────────────
@app.post("/api/extract-colors")
async def extract_colors(
    file: UploadFile = File(None),
    svg_base64: Optional[str] = Form(None),
):
    try:
        if svg_base64:
            svg_str = base64.b64decode(svg_base64).decode("utf-8")
        elif file:
            content = await file.read()
            svg_str = content.decode("utf-8")
        else:
            raise HTTPException(status_code=400, detail="Provide file or svg_base64")
        colors = _extract_svg_colors(svg_str)
        bg_color = _detect_svg_bg_color(svg_str)
        return JSONResponse({"success": True, "colors": colors, "bg_color": bg_color})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /api/convert  (SVG → VP3) ────────────────────────────────────────────────
@app.post("/api/convert")
async def convert_svg(
    file: UploadFile = File(...),
    remove_background:      Optional[str]   = Form(None),
    fill_density:           Optional[float] = Form(None),
    fill_stitch_length:     Optional[float] = Form(None),
    satin_stitch_length:    Optional[float] = Form(None),
    running_stitch_length:  Optional[float] = Form(None),
    underlay_density:       Optional[float] = Form(None),
    max_stitch_length:      Optional[float] = Form(None),
    min_stitch_length:      Optional[float] = Form(None),
    satin_width_threshold:  Optional[float] = Form(None),
    underlay_angle:         Optional[float] = Form(None),
    max_stitches_per_block: Optional[int]   = Form(None),
    target_width_mm:        Optional[float] = Form(None),
    target_height_mm:       Optional[float] = Form(None),
    color_map:              Optional[str]   = Form(None),
):
    if not file.filename.endswith('.svg') and file.content_type != 'image/svg+xml':
        if not file.filename.endswith('.svg'):
            raise HTTPException(status_code=400, detail="Invalid SVG file")
    try:
        content = await file.read()
        try:
            svg_content = content.decode('utf-8')
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Invalid SVG file encoding")

        bg_color = None
        auto_cmap = None

        if remove_background and remove_background.lower() == "true":
            # Detect + remove explicit background rect
            bg_color = _detect_svg_bg_color(svg_content)
            svg_content = _remove_svg_background(svg_content)
            if bg_color:
                auto_cmap = json.dumps({bg_color: None})

        # Merge auto exclusion with user colour map, then apply
        merged = _merge_color_maps(auto_cmap, color_map)
        if merged:
            try:
                svg_content = _remap_svg_colors(svg_content, json.loads(merged))
            except Exception as e:
                print(f"color_map error: {e}")

        effective_width = target_width_mm
        if target_height_mm and not target_width_mm:
            effective_width = target_height_mm

        vp3_params = dict(
            fill_density=fill_density, fill_stitch_length=fill_stitch_length,
            satin_stitch_length=satin_stitch_length, running_stitch_length=running_stitch_length,
            underlay_density=underlay_density, max_stitch_length=max_stitch_length,
            min_stitch_length=min_stitch_length, satin_width_threshold=satin_width_threshold,
            underlay_angle=underlay_angle, max_stitches_per_block=max_stitches_per_block,
            target_width_mm=effective_width,
        )

        vp3_content, pattern = _convert_with_settings(svg_content, vp3_params)
        if not vp3_content:
            raise HTTPException(status_code=500, detail="Conversion failed")

        actual_stitch_count = count_stitches_in_vp3(vp3_content)
        quality_assessment = assess_embroidery_quality(actual_stitch_count, vp3_content)

        return JSONResponse({
            "success": True,
            "vp3_base64": base64.b64encode(vp3_content).decode('utf-8'),
            "preview_base64": _generate_preview_svg(pattern),
            "message": f"Converted with {quality_assessment['level']} quality",
            "quality": quality_assessment['level'],
            "stitchCount": actual_stitch_count,
            "complexity": quality_assessment['complexity'],
            "dimensions": quality_assessment['dimensions'],
            "bg_color": bg_color,
        })
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Conversion failed: {str(e)}")


# ── /api/trace  (raster → SVG) ───────────────────────────────────────────────
@app.post("/api/trace")
async def trace_image(
    file: UploadFile = File(...),
    remove_background:  Optional[str] = Form(None),
    bg_threshold:       Optional[int] = Form(30),
    colormode:          Optional[str]   = Form(None),
    hierarchical:       Optional[str]   = Form(None),
    mode:               Optional[str]   = Form(None),
    filter_speckle:     Optional[int]   = Form(None),
    color_precision:    Optional[int]   = Form(None),
    layer_difference:   Optional[int]   = Form(None),
    corner_threshold:   Optional[int]   = Form(None),
    length_threshold:   Optional[float] = Form(None),
    max_iterations:     Optional[int]   = Form(None),
    splice_threshold:   Optional[int]   = Form(None),
    path_precision:     Optional[int]   = Form(None),
):
    if not VTRACER_AVAILABLE:
        raise HTTPException(status_code=503, detail="vtracer is not installed on the server")
    img_format = _guess_format(file.filename, file.content_type)
    if not img_format:
        raise HTTPException(status_code=400, detail="Unsupported image format.")
    try:
        img_bytes = await file.read()

        # Flood-fill the image from its edges, replacing background pixels with
        # a "magic" marker colour (#fe01fe).  Enclosed same-colour pixels are
        # NOT reachable from the border, so they are preserved.
        # After tracing, we auto-exclude just that marker colour from the SVG.
        bg_color = None
        auto_cmap = None
        if remove_background and remove_background.lower() == "true":
            img_bytes = _flood_fill_background(img_bytes, threshold=bg_threshold or 30)
            img_format = "png"
            bg_color = _BG_MARKER_HEX
            auto_cmap = json.dumps({_BG_MARKER_HEX: None})

        vt_params = dict(
            colormode=colormode, hierarchical=hierarchical, mode=mode,
            filter_speckle=filter_speckle, color_precision=color_precision,
            layer_difference=layer_difference, corner_threshold=corner_threshold,
            length_threshold=length_threshold, max_iterations=max_iterations,
            splice_threshold=splice_threshold, path_precision=path_precision,
        )
        svg_str = _run_vtracer(img_bytes, img_format, vt_params)

        # Remove the marker colour paths from the traced SVG
        if auto_cmap:
            svg_str = _remap_svg_colors(svg_str, json.loads(auto_cmap))

        colors = _extract_svg_colors(svg_str)
        return JSONResponse({
            "success": True,
            "svg_base64": base64.b64encode(svg_str.encode('utf-8')).decode('utf-8'),
            "colors": colors,
            "bg_color": bg_color,
            "message": "Image traced successfully",
        })
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Tracing failed: {str(e)}")


# ── /api/trace-convert  (raster → SVG → VP3) ─────────────────────────────────
@app.post("/api/trace-convert")
async def trace_and_convert(
    file: UploadFile = File(...),
    remove_background:      Optional[str]   = Form(None),
    bg_threshold:           Optional[int]   = Form(30),
    colormode:              Optional[str]   = Form(None),
    hierarchical:           Optional[str]   = Form(None),
    mode:                   Optional[str]   = Form(None),
    filter_speckle:         Optional[int]   = Form(None),
    color_precision:        Optional[int]   = Form(None),
    layer_difference:       Optional[int]   = Form(None),
    corner_threshold:       Optional[int]   = Form(None),
    length_threshold:       Optional[float] = Form(None),
    max_iterations:         Optional[int]   = Form(None),
    splice_threshold:       Optional[int]   = Form(None),
    path_precision:         Optional[int]   = Form(None),
    fill_density:           Optional[float] = Form(None),
    fill_stitch_length:     Optional[float] = Form(None),
    satin_stitch_length:    Optional[float] = Form(None),
    running_stitch_length:  Optional[float] = Form(None),
    underlay_density:       Optional[float] = Form(None),
    max_stitch_length:      Optional[float] = Form(None),
    min_stitch_length:      Optional[float] = Form(None),
    satin_width_threshold:  Optional[float] = Form(None),
    underlay_angle:         Optional[float] = Form(None),
    max_stitches_per_block: Optional[int]   = Form(None),
    target_width_mm:        Optional[float] = Form(None),
    target_height_mm:       Optional[float] = Form(None),
    color_map:              Optional[str]   = Form(None),
):
    if not VTRACER_AVAILABLE:
        raise HTTPException(status_code=503, detail="vtracer is not installed on the server")
    img_format = _guess_format(file.filename, file.content_type)
    if not img_format:
        raise HTTPException(status_code=400, detail="Unsupported image format.")
    try:
        img_bytes = await file.read()

        # Flood-fill from edges, replacing background pixels with the magic marker.
        # Topologically correct: enclosed same-colour shapes are not reachable
        # from the border and are therefore preserved.
        bg_color = None
        auto_cmap = None
        if remove_background and remove_background.lower() == "true":
            img_bytes = _flood_fill_background(img_bytes, threshold=bg_threshold or 30)
            img_format = "png"
            bg_color = _BG_MARKER_HEX
            auto_cmap = json.dumps({_BG_MARKER_HEX: None})

        vt_params = dict(
            colormode=colormode, hierarchical=hierarchical, mode=mode,
            filter_speckle=filter_speckle, color_precision=color_precision,
            layer_difference=layer_difference, corner_threshold=corner_threshold,
            length_threshold=length_threshold, max_iterations=max_iterations,
            splice_threshold=splice_threshold, path_precision=path_precision,
        )

        effective_width = target_width_mm
        if target_height_mm and not target_width_mm:
            effective_width = target_height_mm

        vp3_params = dict(
            fill_density=fill_density, fill_stitch_length=fill_stitch_length,
            satin_stitch_length=satin_stitch_length, running_stitch_length=running_stitch_length,
            underlay_density=underlay_density, max_stitch_length=max_stitch_length,
            min_stitch_length=min_stitch_length, satin_width_threshold=satin_width_threshold,
            underlay_angle=underlay_angle, max_stitches_per_block=max_stitches_per_block,
            target_width_mm=effective_width,
        )

        svg_str = _run_vtracer(img_bytes, img_format, vt_params)

        # Merge auto BG exclusion with any user colour remaps, then apply
        merged = _merge_color_maps(auto_cmap, color_map)
        if merged:
            try:
                svg_str = _remap_svg_colors(svg_str, json.loads(merged))
            except Exception as e:
                print(f"color_map error: {e}")

        svg_b64 = base64.b64encode(svg_str.encode('utf-8')).decode('utf-8')
        colors = _extract_svg_colors(svg_str)

        vp3_content, pattern = _convert_with_settings(svg_str, vp3_params)
        if not vp3_content:
            raise HTTPException(status_code=500, detail="VP3 conversion failed after tracing")

        actual_stitch_count = count_stitches_in_vp3(vp3_content)
        quality_assessment = assess_embroidery_quality(actual_stitch_count, vp3_content)

        return JSONResponse({
            "success": True,
            "svg_base64": svg_b64,
            "colors": colors,
            "bg_color": bg_color,
            "vp3_base64": base64.b64encode(vp3_content).decode('utf-8'),
            "preview_base64": _generate_preview_svg(pattern),
            "message": f"Traced and converted with {quality_assessment['level']} quality",
            "quality": quality_assessment['level'],
            "stitchCount": actual_stitch_count,
            "complexity": quality_assessment['complexity'],
            "dimensions": quality_assessment['dimensions'],
        })
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Trace-convert failed: {str(e)}")


# ── /api/status ───────────────────────────────────────────────────────────────
@app.get("/api/status")
async def status():
    return JSONResponse({
        "vtracer": VTRACER_AVAILABLE,
        "pyembroidery": PYEMBROIDERY_AVAILABLE,
        "pillow": PILLOW_AVAILABLE,
    })


# ── static frontend ───────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="website", html=True), name="website")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)