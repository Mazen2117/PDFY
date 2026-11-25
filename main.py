import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import contextlib
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pdf_builder import PDFBuildError, create_pdf_from_uploaded_bytes, create_pdf_from_urls
from scraper import (
    InvalidURLError,
    NoImagesFoundError,
    PrivateOrLoginWalledError,
    ScraperError,
    extract_post_image_urls,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
OUTPUTS_DIR = STATIC_DIR / "outputs"

OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

CLEANUP_MAX_AGE_SECONDS = 30 * 60  # 30 minutes
CLEANUP_INTERVAL_SECONDS = 5 * 60  # Run every 5 minutes


def cleanup_old_outputs(
    outputs_dir: Path = OUTPUTS_DIR,
    max_age_seconds: int = CLEANUP_MAX_AGE_SECONDS,
) -> int:
    """Delete output session folders older than max_age_seconds. Returns count deleted."""
    if not outputs_dir.exists():
        return 0

    now = time.time()
    deleted = 0
    for item in outputs_dir.iterdir():
        if not item.is_dir():
            continue
        try:
            age = now - item.stat().st_mtime
            if age > max_age_seconds:
                shutil.rmtree(item, ignore_errors=True)
                deleted += 1
        except OSError:
            continue
    return deleted


async def _periodic_cleanup_loop() -> None:
    """Background loop that periodically removes expired output directories."""
    while True:
        try:
            cleanup_old_outputs()
        except Exception:
            pass
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_task = asyncio.create_task(_periodic_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(
    title="Facebook Post Images to PDF API",
    description="Free, open-source FastAPI backend that converts public Facebook post images into a single PDF.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class GeneratePDFRequest(BaseModel):
    post_url: str = Field(..., description="Public Facebook post URL")
    split_size: Optional[int] = Field(
        default=None,
        ge=0,
        description="Optional max images per PDF chunk. Null or 0 produces a single PDF.",
    )
    max_images: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional maximum number of photos to keep in order.",
    )


class PDFFileInfo(BaseModel):
    file_name: str
    download_url: str


class GeneratePDFResponse(BaseModel):
    session_id: str
    total_images: int
    file_name: str
    download_url: str
    files: Optional[list[PDFFileInfo]] = None


@app.get("/")
async def serve_frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/manifest.json")
async def serve_manifest() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "manifest.json",
        media_type="application/manifest+json",
    )


@app.get("/service-worker.js")
async def serve_service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "service-worker.js",
        media_type="application/javascript",
    )


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/generate-pdf", response_model=GeneratePDFResponse)
async def generate_pdf(payload: GeneratePDFRequest) -> GeneratePDFResponse:
    """Scrape public Facebook post images and build a single PDF (or chunked PDFs)."""
    try:
        image_urls = await extract_post_image_urls(
            payload.post_url,
            max_images=payload.max_images,
        )
    except InvalidURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (PrivateOrLoginWalledError, NoImagesFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ScraperError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    session_id = uuid.uuid4().hex
    session_dir = OUTPUTS_DIR / session_id

    try:
        total_images, pdf_paths = await create_pdf_from_urls(
            image_urls=image_urls,
            output_dir=session_dir,
            split_size=payload.split_size,
            max_images=payload.max_images,
        )
    except PDFBuildError as exc:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Unexpected PDF generation error: {exc}") from exc

    primary_pdf = pdf_paths[0]
    primary_url = f"/static/outputs/{session_id}/{primary_pdf.name}"

    files_info = None
    if len(pdf_paths) > 1:
        files_info = [
            PDFFileInfo(
                file_name=p.name,
                download_url=f"/static/outputs/{session_id}/{p.name}",
            )
            for p in pdf_paths
        ]

    return GeneratePDFResponse(
        session_id=session_id,
        total_images=total_images,
        file_name=primary_pdf.name,
        download_url=primary_url,
        files=files_info,
    )


@app.post("/api/images-to-pdf", response_model=GeneratePDFResponse)
async def images_to_pdf(
    files: list[UploadFile] = File(...),
    split_size: Optional[int] = Form(default=None),
) -> GeneratePDFResponse:
    """Directly merge uploaded photos (JPG, PNG, WebP) into a single PDF."""
    if not files:
        raise HTTPException(status_code=400, detail="Please select at least one image file.")

    raw_bytes_list: list[bytes] = []
    for upload in files:
        content = await upload.read()
        if content:
            raw_bytes_list.append(content)

    if not raw_bytes_list:
        raise HTTPException(status_code=400, detail="Uploaded files are empty.")

    session_id = uuid.uuid4().hex
    session_dir = OUTPUTS_DIR / session_id

    try:
        total_images, pdf_paths = await asyncio.to_thread(
            create_pdf_from_uploaded_bytes,
            raw_bytes_list,
            session_dir,
            split_size,
        )
    except PDFBuildError as exc:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Unexpected PDF generation error: {exc}") from exc

    primary_pdf = pdf_paths[0]
    primary_url = f"/static/outputs/{session_id}/{primary_pdf.name}"

    files_info = None
    if len(pdf_paths) > 1:
        files_info = [
            PDFFileInfo(
                file_name=p.name,
                download_url=f"/static/outputs/{session_id}/{p.name}",
            )
            for p in pdf_paths
        ]

    return GeneratePDFResponse(
        session_id=session_id,
        total_images=total_images,
        file_name=primary_pdf.name,
        download_url=primary_url,
        files=files_info,
    )
