from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import base64
import traceback
from io import BytesIO
from core.converter import convert_svg_to_vp3_with_pattern, count_stitches_in_vp3, assess_embroidery_quality

try:
    import pyembroidery
    PYEMBROIDERY_AVAILABLE = True
except ImportError:
    PYEMBROIDERY_AVAILABLE = False

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _generate_preview_svg(pattern) -> str:
    """Generate a base64-encoded SVG preview from a pyembroidery EmbPattern.
    Returns an empty string on failure."""
    if not PYEMBROIDERY_AVAILABLE or pattern is None:
        return ""
    try:
        stitches = pattern.stitches
        if not stitches:
            return ""

        # Compute bounding box (stitch coords are in 1/10 mm)
        xs = [s[0] for s in stitches]
        ys = [s[1] for s in stitches]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        padding = 50
        width = max_x - min_x + padding * 2
        height = max_y - min_y + padding * 2
        if width <= 0 or height <= 0:
            return ""

        # Build thread colour lookup
        thread_colors = []
        for t in pattern.threadlist:
            c = t.color
            if isinstance(c, int):
                thread_colors.append('#{:06x}'.format(c & 0xFFFFFF))
            elif hasattr(t, 'hex') and t.hex:
                h = t.hex.lstrip('#')
                thread_colors.append('#' + h)
            else:
                thread_colors.append('#000000')
        if not thread_colors:
            thread_colors = ['#000000']

        # Build polyline segments grouped by colour
        lines = []
        current_points = []
        color_idx = 0

        for s in stitches:
            x, y, cmd = s[0], s[1], s[2]
            # Translate to positive SVG space
            sx = x - min_x + padding
            sy = y - min_y + padding

            if cmd == pyembroidery.STITCH:
                current_points.append(f"{sx:.1f},{sy:.1f}")
            elif cmd == pyembroidery.COLOR_CHANGE or cmd == pyembroidery.COLOR_BREAK:
                if current_points and len(current_points) >= 2:
                    c = thread_colors[color_idx % len(thread_colors)]
                    pts_str = ' '.join(current_points)
                    lines.append(f'<polyline points="{pts_str}" fill="none" stroke="{c}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
                current_points = []
                color_idx += 1
            elif cmd == pyembroidery.JUMP or cmd == pyembroidery.TRIM:
                if current_points and len(current_points) >= 2:
                    c = thread_colors[color_idx % len(thread_colors)]
                    pts_str = ' '.join(current_points)
                    lines.append(f'<polyline points="{pts_str}" fill="none" stroke="{c}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
                current_points = [f"{sx:.1f},{sy:.1f}"]
            elif cmd == pyembroidery.END:
                break
            else:
                current_points.append(f"{sx:.1f},{sy:.1f}")

        # Flush remaining points
        if current_points and len(current_points) >= 2:
            c = thread_colors[color_idx % len(thread_colors)]
            pts_str = ' '.join(current_points)
            lines.append(f'<polyline points="{pts_str}" fill="none" stroke="{c}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')

        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}"'
            f' width="600" height="{600 * height / width:.0f}"'
            f' style="background:#1a1a2e;border-radius:10px">'
            + ''.join(lines)
            + '</svg>'
        )
        return base64.b64encode(svg.encode('utf-8')).decode('utf-8')
    except Exception as e:
        print(f"Preview generation failed: {e}")
        traceback.print_exc()
    return ""

@app.post("/api/convert")
async def convert_svg(file: UploadFile = File(...)):
    if not file.filename.endswith('.svg') and file.content_type != 'image/svg+xml':
        # Accept if either condition is met, sometimes browser doesn't send correct content type
        if not file.filename.endswith('.svg'):
            raise HTTPException(status_code=400, detail="Invalid SVG file")
    
    try:
        content = await file.read()
        try:
            svg_content = content.decode('utf-8')
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Invalid SVG file encoding")
        
        vp3_content, pattern = convert_svg_to_vp3_with_pattern(svg_content)
        
        if not vp3_content:
            raise HTTPException(status_code=500, detail="Conversion failed")
            
        # These functions come from core.converter
        actual_stitch_count = count_stitches_in_vp3(vp3_content)
        quality_assessment = assess_embroidery_quality(actual_stitch_count, vp3_content)
        
        vp3_b64 = base64.b64encode(vp3_content).decode('utf-8')
        preview_b64 = _generate_preview_svg(pattern)
        
        return JSONResponse({
            "success": True,
            "vp3_base64": vp3_b64,
            "preview_base64": preview_b64,
            "message": f"File converted successfully with {quality_assessment['level']} quality",
            "quality": quality_assessment['level'],
            "stitchCount": actual_stitch_count,
            "complexity": quality_assessment['complexity'],
            "dimensions": quality_assessment['dimensions']
        })
    except Exception as e:
        print(f"Error in conversion: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Conversion failed: {str(e)}")

# Mount static files for the frontend
app.mount("/", StaticFiles(directory="website", html=True), name="website")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
