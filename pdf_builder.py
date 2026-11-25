import asyncio
import hashlib
from io import BytesIO
from pathlib import Path
from typing import Optional

import httpx
import img2pdf
from PIL import Image, ImageOps, UnidentifiedImageError


MIN_IMAGE_DIMENSION = 350


class PDFBuildError(Exception):
    """Raised when downloading images or building the PDF fails."""


DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.facebook.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def normalize_to_rgb_jpeg(
    raw_bytes: bytes,
    min_dimension: int = MIN_IMAGE_DIMENSION,
) -> Optional[bytes]:
    """
    Convert any Pillow-supported image (WebP, PNG, RGBA, P, CMYK, etc.)
    into clean RGB JPEG bytes so img2pdf can embed it reliably.
    Discards images smaller than min_dimension (350px) in width or height.
    Returns None if the bytes do not represent a valid/sufficiently large image.
    """
    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            img = ImageOps.exif_transpose(img)

            if img.width < min_dimension or img.height < min_dimension:
                return None

            # Composite alpha channels onto a white background
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[3])
                rgb_img = background
            elif img.mode != "RGB":
                rgb_img = img.convert("RGB")
            else:
                rgb_img = img

            output = BytesIO()
            rgb_img.save(output, format="JPEG", quality=92, optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        return None


async def _fetch_single_image(
    client: httpx.AsyncClient,
    url: str,
    semaphore: asyncio.Semaphore,
) -> Optional[bytes]:
    """Download a single image into memory (BytesIO) and normalize it to RGB JPEG bytes."""
    async with semaphore:
        try:
            response = await client.get(url)
            response.raise_for_status()
            if not response.content:
                return None

            buffer = BytesIO(response.content)
            return normalize_to_rgb_jpeg(buffer.getvalue())
        except (httpx.HTTPError, ValueError):
            return None


async def download_and_normalize_images(
    image_urls: list[str],
    max_concurrency: int = 8,
    timeout_seconds: float = 20.0,
) -> list[bytes]:
    """
    Download images concurrently using httpx.AsyncClient and asyncio.gather into memory,
    preserving order and skipping any broken image if other valid images exist.
    """
    if not image_urls:
        raise PDFBuildError("No image URLs were provided.")

    semaphore = asyncio.Semaphore(max_concurrency)
    timeout = httpx.Timeout(timeout_seconds, connect=10.0)

    async with httpx.AsyncClient(
        headers=DOWNLOAD_HEADERS,
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        tasks = [_fetch_single_image(client, url, semaphore) for url in image_urls]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    valid_images: list[bytes] = []
    seen_hashes: set[str] = set()
    for img_bytes in results:
        if img_bytes is None:
            continue
        digest = hashlib.sha256(img_bytes).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        valid_images.append(img_bytes)

    if not valid_images:
        raise PDFBuildError(
            "Failed to download or decode any valid images from the extracted URLs."
        )

    return valid_images


def build_pdfs_from_jpegs(
    jpeg_images: list[bytes],
    output_dir: Path,
    split_size: Optional[int] = None,
) -> list[Path]:
    """
    Combine normalized RGB JPEG bytes into one single PDF (post_images.pdf) by default,
    or split into chunks if split_size > 0 is explicitly requested.
    """
    if not jpeg_images:
        raise PDFBuildError("No valid images available to build PDF.")

    output_dir.mkdir(parents=True, exist_ok=True)

    # Default: single PDF containing all images
    if split_size is None or split_size <= 0 or split_size >= len(jpeg_images):
        pdf_path = output_dir / "post_images.pdf"
        pdf_bytes = img2pdf.convert(jpeg_images)
        pdf_path.write_bytes(pdf_bytes)
        return [pdf_path]

    # Optional chunking when split_size is explicitly provided and smaller than total count
    generated_files: list[Path] = []
    for idx in range(0, len(jpeg_images), split_size):
        chunk = jpeg_images[idx : idx + split_size]
        part_num = (idx // split_size) + 1
        pdf_path = output_dir / f"post_images_part_{part_num}.pdf"
        pdf_bytes = img2pdf.convert(chunk)
        pdf_path.write_bytes(pdf_bytes)
        generated_files.append(pdf_path)

    return generated_files


async def create_pdf_from_urls(
    image_urls: list[str],
    output_dir: Path,
    split_size: Optional[int] = None,
    max_images: Optional[int] = None,
) -> tuple[int, list[Path]]:
    """
    Download images concurrently, normalize to RGB JPEG, strictly enforce max_images
    after verification/deduplication, and build PDF file(s).
    Returns (total_valid_images, list_of_generated_pdf_paths).
    """
    valid_jpegs = await download_and_normalize_images(image_urls)
    if max_images is not None and max_images > 0:
        valid_jpegs = valid_jpegs[:max_images]
    pdf_paths = build_pdfs_from_jpegs(valid_jpegs, output_dir, split_size=split_size)
    return len(valid_jpegs), pdf_paths


def create_pdf_from_uploaded_bytes(
    raw_files: list[bytes],
    output_dir: Path,
    split_size: Optional[int] = None,
) -> tuple[int, list[Path]]:
    """
    Normalize directly uploaded image bytes (JPG, PNG, WebP, RGBA) in order,
    deduplicate identical hashes, and build PDF file(s).
    """
    if not raw_files:
        raise PDFBuildError("No image files were uploaded.")

    valid_jpegs: list[bytes] = []
    seen_hashes: set[str] = set()
    for raw in raw_files:
        if not raw:
            continue
        jpeg_bytes = normalize_to_rgb_jpeg(raw, min_dimension=1)
        if jpeg_bytes is None:
            continue
        digest = hashlib.sha256(jpeg_bytes).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        valid_jpegs.append(jpeg_bytes)

    if not valid_jpegs:
        raise PDFBuildError("None of the uploaded files could be decoded as valid images.")

    pdf_paths = build_pdfs_from_jpegs(valid_jpegs, output_dir, split_size=split_size)
    return len(valid_jpegs), pdf_paths
