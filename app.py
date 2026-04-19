from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import base64
import traceback
from core.converter import convert_svg_to_vp3, count_stitches_in_vp3, assess_embroidery_quality

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        
        vp3_content = convert_svg_to_vp3(svg_content)
        
        if not vp3_content:
            raise HTTPException(status_code=500, detail="Conversion failed")
            
        # These functions come from core.converter
        actual_stitch_count = count_stitches_in_vp3(vp3_content)
        quality_assessment = assess_embroidery_quality(actual_stitch_count, vp3_content)
        
        vp3_b64 = base64.b64encode(vp3_content).decode('utf-8')
        
        return JSONResponse({
            "success": True,
            "vp3_base64": vp3_b64,
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
