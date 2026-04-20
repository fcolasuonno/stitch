from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import base64
import traceback
import os
import threading
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

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── globals ──────────────────────────────────────────────────────────────────
RASTER_TYPES = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/bmp": "bmp", "image/gif": "gif", "image/tiff": "tiff", "image/webp": "webp",
}
RASTER_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp"}

# Lock so concurrent requests don't stomp each other's PROFESSIONAL_SETTINGS patch
_settings_lock = threading.Lock()


def _guess_format(filename: str, content_type: str) -> Optional[str]:
    ext = os.path.splitext(filename.lower())[1]
    if ext in RASTER_EXTENSIONS:
        return ext.lstrip(".")
    return RASTER_TYPES.get(content_type)


# ── thread-safe settings patch ───────────────────────────────────────────────
def _convert_with_settings(svg_content: str, vp3_params: dict):
    """
    Temporarily override PROFESSIONAL_SETTINGS for this conversion, then restore.
    Uses a lock so concurrent requests are serialised around the global mutation.
    """
    # Filter out None values
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


# ── embroidery preview ───────────────────────────────────────────────────────
def _generate_preview_svg(pattern) -> str:
    if not PYEMBROIDERY_AVAILABLE or pattern is None:
        print("Preview skipped: pyembroidery unavailable or pattern is None")
        return ""
    try:
        stitches = pattern.stitches
        if not stitches:
            print("Preview skipped: No stitches in pattern")
            return ""
        
        # Filter for actual coordinates, excluding commands that don't have them
        # (Though in pyembroidery most stitches have coordinates)
        xs = [s[0] for s in stitches]
        ys = [s[1] for s in stitches]
        
        if not xs or not ys:
            print("Preview skipped: No coordinates found")
            return ""
            
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        
        padding = 50
        width = max_x - min_x + padding * 2
        height = max_y - min_y + padding * 2
        
        if width <= 0 or height <= 0:
            print(f"Preview skipped: Invalid dimensions {width}x{height}")
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
                if len(current_points) >= 2:
                    c = thread_colors[color_idx % len(thread_colors)]
                    lines.append(f'<polyline points="{" ".join(current_points)}" fill="none" stroke="{c}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
                current_points = []
                color_idx += 1
            elif cmd in (pyembroidery.JUMP, pyembroidery.TRIM):
                if len(current_points) >= 2:
                    c = thread_colors[color_idx % len(thread_colors)]
                    lines.append(f'<polyline points="{" ".join(current_points)}" fill="none" stroke="{c}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
                current_points = [f"{sx:.1f},{sy:.1f}"]
            elif cmd == pyembroidery.END:
                break
            else:
                # Other commands treat as movement
                current_points.append(f"{sx:.1f},{sy:.1f}")
                
        if len(current_points) >= 2:
            c = thread_colors[color_idx % len(thread_colors)]
            lines.append(f'<polyline points="{" ".join(current_points)}" fill="none" stroke="{c}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
            
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


# ── shared Form params for VP3 settings ─────────────────────────────────────
# (declared as a helper so we don't repeat 11 lines in every endpoint)
def _vp3_params(
    fill_density:          Optional[float] = Form(None),
    fill_stitch_length:    Optional[float] = Form(None),
    satin_stitch_length:   Optional[float] = Form(None),
    running_stitch_length: Optional[float] = Form(None),
    underlay_density:      Optional[float] = Form(None),
    max_stitch_length:     Optional[float] = Form(None),
    min_stitch_length:     Optional[float] = Form(None),
    satin_width_threshold: Optional[float] = Form(None),
    underlay_angle:        Optional[float] = Form(None),
    max_stitches_per_block:Optional[int]   = Form(None),
    target_width_mm:       Optional[float] = Form(None),
):
    return dict(
        fill_density=fill_density,
        fill_stitch_length=fill_stitch_length,
        satin_stitch_length=satin_stitch_length,
        running_stitch_length=running_stitch_length,
        underlay_density=underlay_density,
        max_stitch_length=max_stitch_length,
        min_stitch_length=min_stitch_length,
        satin_width_threshold=satin_width_threshold,
        underlay_angle=underlay_angle,
        max_stitches_per_block=max_stitches_per_block,
        target_width_mm=target_width_mm,
    )


# ── /api/convert  (SVG → VP3) ────────────────────────────────────────────────
@app.post("/api/convert")
async def convert_svg(
    file: UploadFile = File(...),
    # VP3 stitch parameters
    fill_density:          Optional[float] = Form(None),
    fill_stitch_length:    Optional[float] = Form(None),
    satin_stitch_length:   Optional[float] = Form(None),
    running_stitch_length: Optional[float] = Form(None),
    underlay_density:      Optional[float] = Form(None),
    max_stitch_length:     Optional[float] = Form(None),
    min_stitch_length:     Optional[float] = Form(None),
    satin_width_threshold: Optional[float] = Form(None),
    underlay_angle:        Optional[float] = Form(None),
    max_stitches_per_block:Optional[int]   = Form(None),
    target_width_mm:       Optional[float] = Form(None),
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

        vp3_params = dict(
            fill_density=fill_density, fill_stitch_length=fill_stitch_length,
            satin_stitch_length=satin_stitch_length, running_stitch_length=running_stitch_length,
            underlay_density=underlay_density, max_stitch_length=max_stitch_length,
            min_stitch_length=min_stitch_length, satin_width_threshold=satin_width_threshold,
            underlay_angle=underlay_angle, max_stitches_per_block=max_stitches_per_block,
            target_width_mm=target_width_mm,
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
    # vtracer parameters
    colormode:        Optional[str]   = Form(None),
    hierarchical:     Optional[str]   = Form(None),
    mode:             Optional[str]   = Form(None),
    filter_speckle:   Optional[int]   = Form(None),
    color_precision:  Optional[int]   = Form(None),
    layer_difference: Optional[int]   = Form(None),
    corner_threshold: Optional[int]   = Form(None),
    length_threshold: Optional[float] = Form(None),
    max_iterations:   Optional[int]   = Form(None),
    splice_threshold: Optional[int]   = Form(None),
    path_precision:   Optional[int]   = Form(None),
):
    if not VTRACER_AVAILABLE:
        raise HTTPException(status_code=503, detail="vtracer is not installed on the server")
    img_format = _guess_format(file.filename, file.content_type)
    if not img_format:
        raise HTTPException(status_code=400, detail="Unsupported image format.")
    try:
        img_bytes = await file.read()
        vt_params = dict(
            colormode=colormode, hierarchical=hierarchical, mode=mode,
            filter_speckle=filter_speckle, color_precision=color_precision,
            layer_difference=layer_difference, corner_threshold=corner_threshold,
            length_threshold=length_threshold, max_iterations=max_iterations,
            splice_threshold=splice_threshold, path_precision=path_precision,
        )
        svg_str = _run_vtracer(img_bytes, img_format, vt_params)
        return JSONResponse({
            "success": True,
            "svg_base64": base64.b64encode(svg_str.encode('utf-8')).decode('utf-8'),
            "message": "Image traced successfully",
        })
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Tracing failed: {str(e)}")


# ── /api/trace-convert  (raster → SVG → VP3) ────────────────────────────────
@app.post("/api/trace-convert")
async def trace_and_convert(
    file: UploadFile = File(...),
    # vtracer parameters
    colormode:        Optional[str]   = Form(None),
    hierarchical:     Optional[str]   = Form(None),
    mode:             Optional[str]   = Form(None),
    filter_speckle:   Optional[int]   = Form(None),
    color_precision:  Optional[int]   = Form(None),
    layer_difference: Optional[int]   = Form(None),
    corner_threshold: Optional[int]   = Form(None),
    length_threshold: Optional[float] = Form(None),
    max_iterations:   Optional[int]   = Form(None),
    splice_threshold: Optional[int]   = Form(None),
    path_precision:   Optional[int]   = Form(None),
    # VP3 stitch parameters
    fill_density:          Optional[float] = Form(None),
    fill_stitch_length:    Optional[float] = Form(None),
    satin_stitch_length:   Optional[float] = Form(None),
    running_stitch_length: Optional[float] = Form(None),
    underlay_density:      Optional[float] = Form(None),
    max_stitch_length:     Optional[float] = Form(None),
    min_stitch_length:     Optional[float] = Form(None),
    satin_width_threshold: Optional[float] = Form(None),
    underlay_angle:        Optional[float] = Form(None),
    max_stitches_per_block:Optional[int]   = Form(None),
    target_width_mm:       Optional[float] = Form(None),
):
    if not VTRACER_AVAILABLE:
        raise HTTPException(status_code=503, detail="vtracer is not installed on the server")
    img_format = _guess_format(file.filename, file.content_type)
    if not img_format:
        raise HTTPException(status_code=400, detail="Unsupported image format.")
    try:
        img_bytes = await file.read()
        vt_params = dict(
            colormode=colormode, hierarchical=hierarchical, mode=mode,
            filter_speckle=filter_speckle, color_precision=color_precision,
            layer_difference=layer_difference, corner_threshold=corner_threshold,
            length_threshold=length_threshold, max_iterations=max_iterations,
            splice_threshold=splice_threshold, path_precision=path_precision,
        )
        vp3_params = dict(
            fill_density=fill_density, fill_stitch_length=fill_stitch_length,
            satin_stitch_length=satin_stitch_length, running_stitch_length=running_stitch_length,
            underlay_density=underlay_density, max_stitch_length=max_stitch_length,
            min_stitch_length=min_stitch_length, satin_width_threshold=satin_width_threshold,
            underlay_angle=underlay_angle, max_stitches_per_block=max_stitches_per_block,
            target_width_mm=target_width_mm,
        )

        # Step 1: trace
        svg_str = _run_vtracer(img_bytes, img_format, vt_params)
        svg_b64 = base64.b64encode(svg_str.encode('utf-8')).decode('utf-8')

        # Step 2: convert
        vp3_content, pattern = _convert_with_settings(svg_str, vp3_params)
        if not vp3_content:
            raise HTTPException(status_code=500, detail="VP3 conversion failed after tracing")

        actual_stitch_count = count_stitches_in_vp3(vp3_content)
        quality_assessment = assess_embroidery_quality(actual_stitch_count, vp3_content)

        return JSONResponse({
            "success": True,
            "svg_base64": svg_b64,
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


# ── /api/status ──────────────────────────────────────────────────────────────
@app.get("/api/status")
async def status():
    return JSONResponse({
        "vtracer": VTRACER_AVAILABLE,
        "pyembroidery": PYEMBROIDERY_AVAILABLE,
    })


# ── static frontend ──────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="website", html=True), name="website")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)