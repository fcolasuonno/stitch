# Stitch - SVG & Image to VP3 Embroidery Converter

FastAPI application for converting SVG files and raster images into VP3 embroidery files.

## Features

- **Conversion**: Converts SVG paths to VP3 embroidery format.
- **Tracing**: Uses `vtracer` to convert raster images (PNG, JPG) to SVG before conversion.
- **Preview**: Generates an SVG-based preview of the embroidery stitches.
- **Settings**: Adjustable parameters for stitch density, lengths, and underlay.
- **Analysis**: Provides stitch count and estimated physical dimensions.
- **Local**: Runs as a standalone FastAPI server.

## Architecture

- **Backend**: FastAPI (Python).
- **Libraries**: 
    - `pyembroidery` for file serialization.
    - `vtracer` for image-to-vector tracing.
    - `svgpathtools` for path parsing.
- **Frontend**: HTML, CSS, and JavaScript.

## Getting Started

### Prerequisites

- Python 3.9+
- pip

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-repo/stitch.git
   cd stitch
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

### Running

Start the server:
```bash
python app.py
```
Access at `http://localhost:8000`.

## API Endpoints

### `POST /api/convert`
SVG to VP3 conversion.
- **Payload**: `multipart/form-data` with SVG file and optional stitch parameters.
- **Returns**: VP3 data (base64), preview (base64), and metrics.

### `POST /api/trace`
Raster to SVG tracing.
- **Payload**: `multipart/form-data` with image file and optional vtracer parameters.
- **Returns**: SVG data (base64).

### `POST /api/trace-convert`
Image to VP3 pipeline (trace then convert).
- **Payload**: `multipart/form-data` with image file and parameters.
- **Returns**: SVG trace, VP3 data, and preview.

### `GET /api/status`
Status check for backend dependencies (`vtracer`, `pyembroidery`).

## Standards

- **Default Density**: 6.3 SPI (4mm spacing).
- **Stitch Lengths**: Fill (1.5mm), Satin (2.0mm), Running (2.5mm).
- **Thread Mapping**: Maps hex colors to Madeira thread names where possible.
- **Safety**: 12mm maximum stitch length.

## Repository Structure

```
stitch/
├── app.py              # Main application and API
├── core/
│   └── converter.py    # Conversion logic
├── website/
│   ├── index.html      # Frontend
│   └── assets/         # CSS, fonts, and images
├── test_examples/      # Sample files
├── requirements.txt    # Dependencies
├── ROADMAP.md          # Planned updates
└── README.md
```

---

**ur/gd studios**