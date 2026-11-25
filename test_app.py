import os
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main
from pdf_builder import (
    PDFBuildError,
    build_pdfs_from_jpegs,
    download_and_normalize_images,
    normalize_to_rgb_jpeg,
)
from scraper import (
    InvalidURLError,
    NoImagesFoundError,
    PrivateOrLoginWalledError,
    deduplicate_by_path,
    extract_scontent_urls_from_html,
    is_candidate_image_url,
    load_cookies_if_present,
    normalize_photo_key,
    validate_facebook_url,
)


def _make_image_bytes(fmt: str, mode: str = "RGB", size: tuple[int, int] = (400, 400), color=(255, 0, 0)) -> bytes:
    buf = BytesIO()
    img = Image.new(mode, size, color)
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_validate_facebook_url():
    assert validate_facebook_url("https://www.facebook.com/user/posts/123") == "https://www.facebook.com/user/posts/123"
    assert validate_facebook_url("https://m.facebook.com/story.php?story_fbid=1&id=2").startswith("https://m.facebook.com")

    with pytest.raises(InvalidURLError):
        validate_facebook_url("")
    with pytest.raises(InvalidURLError):
        validate_facebook_url("not-a-url")
    with pytest.raises(InvalidURLError):
        validate_facebook_url("https://example.com/posts/123")
    with pytest.raises(InvalidURLError):
        validate_facebook_url("https://www.facebook.com/")


def test_is_candidate_image_url():
    assert is_candidate_image_url("https://scontent.fcai19-1.fna.fbcdn.net/v/t39.30808-6/photo.jpg")
    assert not is_candidate_image_url("https://static.xx.fbcdn.net/images/emoji.php/v9/t1/1/16/1f600.png")
    assert not is_candidate_image_url("https://scontent.xx.fbcdn.net/v/p50x50/avatar.jpg")
    assert not is_candidate_image_url("data:image/png;base64,AAA")


def test_normalize_to_rgb_jpeg_handles_rgba_png_and_webp():
    png_rgba = _make_image_bytes("PNG", mode="RGBA", color=(0, 128, 255, 128))
    webp_img = _make_image_bytes("WEBP", mode="RGB", color=(10, 200, 50))

    jpeg_from_png = normalize_to_rgb_jpeg(png_rgba)
    jpeg_from_webp = normalize_to_rgb_jpeg(webp_img)

    assert jpeg_from_png is not None
    assert jpeg_from_webp is not None

    with Image.open(BytesIO(jpeg_from_png)) as im:
        assert im.format == "JPEG"
        assert im.mode == "RGB"

    with Image.open(BytesIO(jpeg_from_webp)) as im:
        assert im.format == "JPEG"
        assert im.mode == "RGB"

    assert normalize_to_rgb_jpeg(b"not-an-image") is None


@pytest.mark.asyncio
async def test_download_skips_broken_images_and_preserves_order():
    img1 = _make_image_bytes("PNG", mode="RGBA", color=(255, 0, 0, 200))
    img2 = _make_image_bytes("WEBP", mode="RGB", color=(0, 255, 0))

    def handler(request: httpx.Request) -> httpx.Response:
        if "img1" in str(request.url):
            return httpx.Response(200, content=img1)
        if "broken" in str(request.url):
            return httpx.Response(404, content=b"not found")
        if "img2" in str(request.url):
            return httpx.Response(200, content=img2)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)

    orig_async_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    with patch("pdf_builder.httpx.AsyncClient", side_effect=patched_client):
        results = await download_and_normalize_images(
            [
                "https://scontent.fbcdn.net/img1.png",
                "https://scontent.fbcdn.net/broken.jpg",
                "https://scontent.fbcdn.net/img2.webp",
            ]
        )

    assert len(results) == 2


@pytest.mark.asyncio
async def test_create_pdf_from_urls_enforces_max_images(tmp_path: Path):
    from pdf_builder import create_pdf_from_urls

    fake_jpegs = [
        normalize_to_rgb_jpeg(_make_image_bytes("PNG", size=(400, 400), color=(i * 60, 50, 50)))
        for i in range(1, 4)
    ]
    clean_jpegs = [j for j in fake_jpegs if j is not None]

    with patch("pdf_builder.download_and_normalize_images", new=AsyncMock(return_value=clean_jpegs)):
        total, paths = await create_pdf_from_urls(
            ["u1", "u2", "u3"],
            output_dir=tmp_path,
            max_images=2,
        )

    assert total == 2
    assert len(paths) == 1
    assert paths[0].name == "post_images.pdf"
    assert paths[0].read_bytes().startswith(b"%PDF")
    # First image was red, second was green
    with Image.open(BytesIO(results[0])) as im1:
        r, g, b = im1.getpixel((10, 10))
        assert r > 200 and g < 80
    with Image.open(BytesIO(results[1])) as im2:
        r, g, b = im2.getpixel((10, 10))
        assert g > 200 and r < 50


def test_build_pdfs_single_and_chunked(tmp_path: Path):
    jpegs = [
        normalize_to_rgb_jpeg(_make_image_bytes("PNG", color=(i * 50, 50, 50)))
        for i in range(1, 4)
    ]
    assert all(j is not None for j in jpegs)
    clean_jpegs = [j for j in jpegs if j is not None]

    # Single PDF default
    single_dir = tmp_path / "single"
    single_paths = build_pdfs_from_jpegs(clean_jpegs, single_dir, split_size=None)
    assert len(single_paths) == 1
    assert single_paths[0].name == "post_images.pdf"
    assert single_paths[0].read_bytes().startswith(b"%PDF")

    # Chunked PDF when split_size=2
    chunk_dir = tmp_path / "chunked"
    chunk_paths = build_pdfs_from_jpegs(clean_jpegs, chunk_dir, split_size=2)
    assert len(chunk_paths) == 2
    assert [p.name for p in chunk_paths] == ["post_images_part_1.pdf", "post_images_part_2.pdf"]


def test_api_generate_pdf_success_and_errors(tmp_path: Path):
    client = TestClient(main.app)

    # Error 400 on invalid URL
    with patch("main.extract_post_image_urls", side_effect=InvalidURLError("Invalid URL")):
        resp = client.post("/api/generate-pdf", json={"post_url": "bad-url", "split_size": None})
        assert resp.status_code == 400

    # Error 422 on login wall
    with patch("main.extract_post_image_urls", side_effect=PrivateOrLoginWalledError("Login wall")):
        resp = client.post(
            "/api/generate-pdf",
            json={"post_url": "https://www.facebook.com/private/posts/1", "split_size": None},
        )
        assert resp.status_code == 422

    # Error 422 on no images
    with patch("main.extract_post_image_urls", side_effect=NoImagesFoundError("No images")):
        resp = client.post(
            "/api/generate-pdf",
            json={"post_url": "https://www.facebook.com/public/posts/1", "split_size": None},
        )
        assert resp.status_code == 422

    # Success path
    async def fake_create_pdf(image_urls, output_dir: Path, split_size=None, max_images=None):
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = output_dir / "post_images.pdf"
        pdf_file.write_bytes(b"%PDF-1.4\n%fake\n")
        return 3, [pdf_file]

    with (
        patch("main.OUTPUTS_DIR", tmp_path),
        patch("main.extract_post_image_urls", new=AsyncMock(return_value=["u1", "u2", "u3"])),
        patch("main.create_pdf_from_urls", side_effect=fake_create_pdf),
    ):
        resp = client.post(
            "/api/generate-pdf",
            json={
                "post_url": "https://www.facebook.com/public/posts/1",
                "split_size": None,
                "max_images": 20,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_images"] == 3
        assert data["file_name"] == "post_images.pdf"
        assert data["download_url"] == f"/static/outputs/{data['session_id']}/post_images.pdf"


def test_cleanup_old_outputs(tmp_path: Path):
    old_dir = tmp_path / "old_session"
    fresh_dir = tmp_path / "fresh_session"
    old_dir.mkdir()
    fresh_dir.mkdir()
    (old_dir / "post_images.pdf").write_bytes(b"%PDF")
    (fresh_dir / "post_images.pdf").write_bytes(b"%PDF")

    old_time = time.time() - (31 * 60)
    os.utime(old_dir, (old_time, old_time))

    deleted = main.cleanup_old_outputs(outputs_dir=tmp_path, max_age_seconds=30 * 60)
    assert deleted == 1
    assert not old_dir.exists()
    assert fresh_dir.exists()


def test_frontend_and_pwa_routes():
    client = TestClient(main.app)

    root_resp = client.get("/")
    assert root_resp.status_code == 200
    assert "PDFY" in root_resp.text
    assert "/manifest.json" in root_resp.text

    manifest_resp = client.get("/manifest.json")
    assert manifest_resp.status_code == 200
    manifest_data = manifest_resp.json()
    assert manifest_data["short_name"] == "PDFY"
    assert manifest_data["display"] == "standalone"
    assert "share_target" in manifest_data

    sw_resp = client.get("/service-worker.js")
    assert sw_resp.status_code == 200
    assert "CACHE_NAME" in sw_resp.text

    icon_resp = client.get("/static/icons/icon-192.png")
    assert icon_resp.status_code == 200
    assert icon_resp.headers["content-type"].startswith("image/png")


def test_extract_scontent_urls_from_html_unescapes_and_dedupes():
    sample_html = r'''
    <script type="application/json">
      {
        "photo1": "https:\/\/scontent.fcai19-1.fna.fbcdn.net\/v\/t39.30808-6\/img1.jpg?_nc_cat=1\u0026oh=abc",
        "thumb": "https:\/\/scontent.fcai19-1.fna.fbcdn.net\/v\/p50x50\/avatar.jpg",
        "video": "https:\/\/scontent.fcai19-1.fna.fbcdn.net\/v\/clip.mp4",
        "photo1_dup": "https:\/\/scontent.fcai19-1.fna.fbcdn.net\/v\/t39.30808-6\/img1.jpg?_nc_cat=1\u0026oh=abc",
        "photo2": "https:\/\/scontent.fcai19-2.fna.fbcdn.net\/v\/t39.30808-6\/img2.jpg?oh=xyz"
      }
    </script>
    '''
    urls = deduplicate_by_path(extract_scontent_urls_from_html(sample_html))
    assert urls == [
        "https://scontent.fcai19-1.fna.fbcdn.net/v/t39.30808-6/img1.jpg?_nc_cat=1&oh=abc",
        "https://scontent.fcai19-2.fna.fbcdn.net/v/t39.30808-6/img2.jpg?oh=xyz",
    ]


def test_load_cookies_if_present(tmp_path: Path):
    missing = tmp_path / "cookies.json"
    assert load_cookies_if_present(missing) == []

    cookies_file = tmp_path / "cookies.json"
    cookies_file.write_text(
        '[{"name": "c_user", "value": "123", "domain": ".facebook.com", "sameSite": "no_restriction", "expirationDate": 1800000000}]',
        encoding="utf-8",
    )
    loaded = load_cookies_if_present(cookies_file)
    assert len(loaded) == 1
    assert loaded[0]["sameSite"] == "None"
    assert loaded[0]["expires"] == 1800000000.0


def test_deduplicate_by_path_prefers_highest_resolution_varied_filenames():
    low_res = "https://scontent.fcai19-1.fna.fbcdn.net/v/t39.30808-6/p200x200/custom_photo_abc.jpg?stp=dst-jpg_p200x200_tt6"
    mid_res = "https://scontent.fcai19-1.fna.fbcdn.net/v/t39.30808-6/s600x600/custom_photo_abc.jpg?stp=dst-jpg_s600x600_tt6"
    no_resize = "https://scontent.fcai19-1.fna.fbcdn.net/v/t39.30808-6/custom_photo_abc.jpg?_nc_cat=1"
    other_orig = "https://scontent.fcai19-1.fna.fbcdn.net/v/t39.30808-6/999888777_333_444_o.jpg"

    assert normalize_photo_key(low_res) == "/v/t39.30808-6/custom_photo_abc.jpg"
    assert not is_candidate_image_url("https://scontent.fbcdn.net/v/p160x160/avatar.jpg")
    assert not is_candidate_image_url("https://scontent.fbcdn.net/v/s160x160/avatar.jpg")

    deduped = deduplicate_by_path([low_res, mid_res, other_orig, no_resize])
    assert deduped == [no_resize, other_orig]


def test_normalize_discards_small_images():
    small_img = _make_image_bytes("PNG", size=(200, 200))
    narrow_img = _make_image_bytes("PNG", size=(349, 600))
    valid_img = _make_image_bytes("PNG", size=(350, 350))

    assert normalize_to_rgb_jpeg(small_img) is None
    assert normalize_to_rgb_jpeg(narrow_img) is None
    assert normalize_to_rgb_jpeg(valid_img) is not None


def test_api_images_to_pdf_success_and_invalid(tmp_path: Path):
    client = TestClient(main.app)

    png_bytes = _make_image_bytes("PNG", mode="RGBA", size=(400, 400), color=(255, 0, 0, 200))
    webp_bytes = _make_image_bytes("WEBP", mode="RGB", size=(400, 400), color=(0, 255, 0))

    with patch("main.OUTPUTS_DIR", tmp_path):
        resp = client.post(
            "/api/images-to-pdf",
            files=[
                ("files", ("first.png", png_bytes, "image/png")),
                ("files", ("second.webp", webp_bytes, "image/webp")),
            ],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_images"] == 2
        assert data["file_name"] == "post_images.pdf"
        assert data["download_url"] == f"/static/outputs/{data['session_id']}/post_images.pdf"
        assert (tmp_path / data["session_id"] / "post_images.pdf").read_bytes().startswith(b"%PDF")

        # Invalid image bytes should return 422
        bad_resp = client.post(
            "/api/images-to-pdf",
            files=[("files", ("bad.txt", b"not an image", "text/plain"))],
        )
        assert bad_resp.status_code == 422


@pytest.mark.asyncio
async def test_download_deduplicates_identical_image_hashes():
    img_a = _make_image_bytes("PNG", size=(400, 400), color=(255, 0, 0))
    img_b = _make_image_bytes("PNG", size=(400, 400), color=(0, 0, 255))

    def handler(request: httpx.Request) -> httpx.Response:
        if "a1" in str(request.url) or "a2" in str(request.url):
            return httpx.Response(200, content=img_a)
        return httpx.Response(200, content=img_b)

    transport = httpx.MockTransport(handler)
    orig_async_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    with patch("pdf_builder.httpx.AsyncClient", side_effect=patched_client):
        results = await download_and_normalize_images(
            [
                "https://scontent.fbcdn.net/a1.png",
                "https://scontent.fbcdn.net/b.png",
                "https://scontent.fbcdn.net/a2.png",
            ]
        )

    assert len(results) == 2


@pytest.mark.asyncio
async def test_create_pdf_from_urls_enforces_max_images(tmp_path: Path):
    from pdf_builder import create_pdf_from_urls

    fake_jpegs = [
        normalize_to_rgb_jpeg(_make_image_bytes("PNG", size=(400, 400), color=(i * 60, 50, 50)))
        for i in range(1, 4)
    ]
    clean_jpegs = [j for j in fake_jpegs if j is not None]

    with patch("pdf_builder.download_and_normalize_images", new=AsyncMock(return_value=clean_jpegs)):
        total, paths = await create_pdf_from_urls(
            ["u1", "u2", "u3"],
            output_dir=tmp_path,
            max_images=2,
        )

    assert total == 2
    assert len(paths) == 1
    assert paths[0].name == "post_images.pdf"
    assert paths[0].read_bytes().startswith(b"%PDF")



