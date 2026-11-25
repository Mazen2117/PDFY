---
title: PDFY
emoji: 📄
colorFrom: red
colorTo: gray
sdk: gradio
app_file: app.py
pinned: false
---

# Facebook Post Images to PDF (FastAPI)

A 100% free, open-source Python FastAPI backend that extracts high-resolution images from a public Facebook post using Playwright (headless Chromium) and converts them into a single PDF file (with optional chunking).

## Features

- **Free Playwright Scraper (`scraper.py`)**: Uses headless Chromium with realistic user-agent and viewport settings, dismisses common overlays, scrolls to trigger lazy-loaded images, filters out emojis/avatars/icons/duplicates, and preserves image order.
- **Concurrent Downloader & PDF Builder (`pdf_builder.py`)**: Downloads images concurrently into memory (`BytesIO`) using `httpx.AsyncClient` and `asyncio.gather`, skips broken images without failing valid ones, normalizes WebP/PNG/RGBA/CMYK images to RGB JPEG with Pillow, and builds a single `post_images.pdf` (or chunked PDFs when `split_size` is set) using `img2pdf`.
- **FastAPI Backend (`main.py`)**: Exposes `POST /api/generate-pdf`, mounts `/static`, enables CORS, and runs a background cleanup task deleting output folders older than 30 minutes.

## Setup Instructions

1. **Create and activate a virtual environment**

   Windows (PowerShell):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   macOS / Linux:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install Python dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Install Playwright Chromium browser**

   ```bash
   playwright install chromium
   ```

4. **Run the FastAPI server**

   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```

## API Usage

### `POST /api/generate-pdf`

Request body:
```json
{
  "post_url": "https://www.facebook.com/example/posts/1234567890",
  "split_size": null
}
```

Response:
```json
{
  "session_id": "a1b2c3d4e5f6...",
  "total_images": 5,
  "file_name": "post_images.pdf",
  "download_url": "/static/outputs/a1b2c3d4e5f6.../post_images.pdf",
  "files": null
}
```

## Running Tests

```bash
pytest -v
```
