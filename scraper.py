import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

logger = logging.getLogger("pdfy.scraper")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class ScraperError(Exception):
    """Base exception for scraper errors."""


class InvalidURLError(ScraperError):
    """Raised when the provided URL is invalid or not a Facebook URL."""


class PrivateOrLoginWalledError(ScraperError):
    """Raised when the post is private, deleted, or behind a login wall."""


class NoImagesFoundError(ScraperError):
    """Raised when no extractable high-resolution images are found in the post."""


PROJECT_ROOT = Path(__file__).resolve().parent
COOKIES_PATH = PROJECT_ROOT / "cookies.json"

ALLOWED_FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "fb.com",
    "www.fb.com",
    "fb.watch",
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

VIEWPORT_SETTINGS = {"width": 1366, "height": 900}

IGNORED_URL_PATTERNS = (
    "p32x32",
    "p40x40",
    "p50x50",
    "p64x64",
    "p100x100",
    "s100x100",
    "p160x160",
    "s160x160",
    "emoji.php",
    "static.xx.fbcdn.net",
    "/rsrc.php",
    "data:image/",
    ".svg",
)

# Broad regex matching https://scontent... URLs in unescaped HTML/JSON
SCONTENT_URL_REGEX = re.compile(r'https://scontent[^\s"\'<>]+', re.IGNORECASE)
SIZE_HINT_REGEX = re.compile(r"(?:^|[/_?&.=])([ps])(\d{2,4})x(\d{2,4})(?:[/_&.]|$)", re.IGNORECASE)
PATH_SIZE_SEGMENT_REGEX = re.compile(r"/(?:[ps]\d{2,4}x\d{2,4}|cp0|e15|q\d+)(?=/|$)", re.IGNORECASE)


def validate_facebook_url(post_url: str) -> str:
    """Validate that the URL is a well-formed HTTP(S) Facebook URL."""
    if not post_url or not isinstance(post_url, str):
        raise InvalidURLError("post_url must be a non-empty string.")

    cleaned = post_url.strip()
    parsed = urlparse(cleaned)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InvalidURLError("Invalid URL format. Please provide a valid http(s) Facebook post URL.")

    hostname = (parsed.hostname or "").lower()
    if hostname not in ALLOWED_FACEBOOK_HOSTS and not hostname.endswith(".facebook.com"):
        raise InvalidURLError(
            f"Unsupported domain '{hostname}'. Please provide a valid public Facebook post URL."
        )

    if parsed.path in ("", "/") and not parsed.query:
        raise InvalidURLError("URL points to the Facebook homepage rather than a specific post.")

    return cleaned


def is_candidate_image_url(url: str) -> bool:
    """Filter out obvious tiny thumbnails, emojis, and static non-photo assets."""
    if not url or not url.startswith(("http://", "https://")):
        return False

    lowered = url.lower()
    if any(pattern in lowered for pattern in IGNORED_URL_PATTERNS):
        return False

    path_lower = urlparse(lowered).path
    if path_lower.endswith((".mp4", ".m4a", ".mp3", ".webm", ".css", ".js")):
        return False

    return True


def normalize_photo_key(url: str) -> str:
    """
    Normalize a Facebook scontent URL by taking its URL path before '?' and stripping
    size segments like /p200x200/ or /s100x100/ so resized variants map to one key.
    """
    path = urlparse(url).path
    while True:
        updated = PATH_SIZE_SEGMENT_REGEX.sub("", path)
        if updated == path:
            break
        path = updated
    return path or url.split("?", 1)[0]


def score_url_resolution(url: str) -> int:
    """
    Score a Facebook image URL by resolution so deduplication keeps the highest-resolution
    variant per photo (preferring unresized URLs or the largest sWxH / pWxH dimension).
    """
    lowered = url.lower()
    path = urlparse(lowered).path

    size_matches = SIZE_HINT_REGEX.findall(lowered)
    if size_matches:
        return max(int(w) * int(h) for _, w, h in size_matches)

    if re.search(r"_o\.[a-z0-9]+$", path):
        return 10_000_000

    return 5_000_000


def deduplicate_by_path(urls: list[str]) -> list[str]:
    """
    Deduplicate scontent URLs by their normalized URL path (before '?'), preserving
    first-seen order while keeping the highest-resolution URL for each photo.
    """
    ordered_keys: list[str] = []
    best_by_key: dict[str, tuple[int, str]] = {}

    for url in urls:
        if not is_candidate_image_url(url):
            continue
        key = normalize_photo_key(url)
        score = score_url_resolution(url)

        if key not in best_by_key:
            ordered_keys.append(key)
            best_by_key[key] = (score, url)
        else:
            current_score, _ = best_by_key[key]
            if score > current_score:
                best_by_key[key] = (score, url)

    return [best_by_key[k][1] for k in ordered_keys]


def _normalize_playwright_cookies(raw_cookies: Any) -> list[dict[str, Any]]:
    """Normalize cookies.json entries (including browser-extension exports) for Playwright."""
    if not isinstance(raw_cookies, list):
        return []

    same_site_map = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
    }

    normalized: list[dict[str, Any]] = []
    for entry in raw_cookies:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        value = entry.get("value")
        domain = entry.get("domain")
        if not (name and value is not None and domain):
            continue

        cookie: dict[str, Any] = {
            "name": str(name),
            "value": str(value),
            "domain": str(domain),
            "path": str(entry.get("path") or "/"),
        }

        if "secure" in entry:
            cookie["secure"] = bool(entry["secure"])
        if "httpOnly" in entry:
            cookie["httpOnly"] = bool(entry["httpOnly"])

        raw_expires = entry.get("expires", entry.get("expirationDate"))
        if isinstance(raw_expires, (int, float)) and raw_expires > 0:
            cookie["expires"] = float(raw_expires)

        raw_same_site = str(entry.get("sameSite") or "").lower()
        if raw_same_site in same_site_map:
            cookie["sameSite"] = same_site_map[raw_same_site]

        normalized.append(cookie)

    return normalized


def load_cookies_if_present(cookies_path: Path = COOKIES_PATH) -> list[dict[str, Any]]:
    """Load and normalize cookies from cookies.json if it exists in the project root."""
    if not cookies_path.exists():
        return []

    try:
        data = json.loads(cookies_path.read_text(encoding="utf-8"))
        cookies = _normalize_playwright_cookies(data)
        logger.info("Loaded %d cookies from %s", len(cookies), cookies_path.name)
        return cookies
    except Exception as exc:
        logger.warning("Failed to load %s: %s", cookies_path.name, exc)
        return []


def extract_scontent_urls_from_html(raw_html: str) -> list[str]:
    """
    Search raw HTML/JSON script tags for all https://scontent... image URLs
    (unescaping \\/ to / and \\u0026 to &).
    """
    if not raw_html:
        return []

    normalized_html = (
        raw_html.replace(r"\/", "/")
        .replace("\\u0026", "&")
        .replace("&amp;", "&")
    )
    matches = SCONTENT_URL_REGEX.findall(normalized_html)
    return [u.rstrip("\\,;)") for u in matches if is_candidate_image_url(u.rstrip("\\,;)"))]


def _wait_for_share_redirect(page: Page, initial_url: str, max_wait_ms: int = 6000) -> None:
    """
    Wait for networkidle (up to 15s with domcontentloaded fallback), and if Facebook
    stopped at https://www.facebook.com or /share/, wait an extra 4s for client-side redirect.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    if page.url.rstrip("/") == "https://www.facebook.com" or "/share/" in page.url.lower():
        page.wait_for_timeout(4000)

    if "/share/" in initial_url.lower() and "/share/" in page.url.lower():
        deadline = time.monotonic() + (max_wait_ms / 1000.0)
        while time.monotonic() < deadline:
            if "/share/" not in page.url.lower() and page.url.rstrip("/") != "https://www.facebook.com":
                break
            page.wait_for_timeout(300)


def _dismiss_overlays(page: Page) -> None:
    """Press Escape and click close buttons to dismiss [role='dialog'] login/cookie modals."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass

    close_selectors = [
        "[role='dialog'] [aria-label='Close']",
        "[aria-label='Close']",
        "[role='dialog'] [aria-label='close']",
        "[data-testid='cookie-policy-manage-dialog-accept-button']",
        "button[title='Allow all cookies']",
        "button[title='Decline optional cookies']",
        "[aria-label='Allow all cookies']",
        "[aria-label='Decline optional cookies']",
    ]

    for selector in close_selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=700):
                locator.click(timeout=1500)
                page.wait_for_timeout(400)
        except Exception:
            continue


def _detect_block_reason(page: Page) -> str | None:
    """Return a human-readable block/unavailable reason if Facebook blocked the post, else None."""
    current_url = page.url.lower()
    if "/login" in current_url or "checkpoint" in current_url:
        return f"Redirected to login/checkpoint page ({page.url})."

    try:
        body_text = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        return None

    unavailable_markers = (
        "this content isn't available right now",
        "this content is no longer available",
        "you must log in to continue",
        "log in or sign up to view",
        "the link you followed may be broken",
    )
    for marker in unavailable_markers:
        if marker in body_text:
            return f"Page displayed restriction message: '{marker}'."

    return None


def _collect_dom_scontent_images(page: Page) -> list[str]:
    """
    Gently scroll 2-3 times so post images render, then extract all scontent <img> src URLs.
    """
    collected: list[str] = []

    def harvest() -> None:
        urls: list[str] = page.evaluate(
            """() => {
                return Array.from(document.querySelectorAll('img'))
                    .map(img => img.currentSrc || img.src || '')
                    .filter(src => src.toLowerCase().includes('scontent'));
            }"""
        )
        for u in urls:
            if is_candidate_image_url(u):
                collected.append(u)

    harvest()
    for _ in range(3):
        page.evaluate("() => window.scrollBy(0, 800)")
        page.wait_for_timeout(800)
        _dismiss_overlays(page)
        harvest()

    return collected


def _extract_post_image_urls_sync(
    validated_url: str,
    timeout_ms: int = 30000,
    max_images: int | None = None,
) -> list[str]:
    """
    Synchronous Playwright extraction executed in a worker thread via asyncio.to_thread.
    Retries up to 2 times with a fresh context if Facebook redirects to the bare homepage.
    """
    cookies = load_cookies_if_present()
    max_attempts = 3  # 1 initial attempt + up to 2 automatic retries

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        try:
            for attempt in range(1, max_attempts + 1):
                context = browser.new_context(
                    user_agent=DEFAULT_USER_AGENT,
                    viewport=VIEWPORT_SETTINGS,
                    locale="en-US",
                )

                if cookies:
                    try:
                        context.add_cookies(cookies)
                    except Exception as exc:
                        logger.warning("Could not apply cookies.json to Playwright context: %s", exc)

                page = context.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )

                try:
                    try:
                        page.goto(validated_url, wait_until="networkidle", timeout=15000)
                    except PlaywrightTimeoutError:
                        if page.url == "about:blank":
                            page.goto(validated_url, wait_until="domcontentloaded", timeout=timeout_ms)

                    _wait_for_share_redirect(page, validated_url, max_wait_ms=6000)
                    _dismiss_overlays(page)

                    final_url = page.url

                    # If Facebook randomly bounced to the bare homepage, retry with a fresh context
                    if final_url.rstrip("/") == "https://www.facebook.com" and attempt < max_attempts:
                        logger.warning(
                            "Attempt %d/%d landed on bare Facebook homepage (%s). Retrying in 2s...",
                            attempt,
                            max_attempts,
                            final_url,
                        )
                        time.sleep(2)
                        continue

                    dom_urls = _collect_dom_scontent_images(page)
                    raw_html = page.content()
                    json_urls = extract_scontent_urls_from_html(raw_html)

                    all_candidates = [*dom_urls, *json_urls]
                    deduped_urls = deduplicate_by_path(all_candidates)

                    if max_images is not None and max_images > 0:
                        # Keep a 2x buffer before download so <350px filtering in pdf_builder
                        # still leaves enough valid photos, then pdf_builder strictly slices [:max_images]
                        deduped_urls = deduped_urls[: max_images * 2]

                    logger.info(
                        "Final URL: %s | Candidates before dedupe: %d (dom=%d, json=%d) | After dedupe/limit: %d",
                        final_url,
                        len(all_candidates),
                        len(dom_urls),
                        len(json_urls),
                        len(deduped_urls),
                    )

                    if not deduped_urls:
                        block_reason = _detect_block_reason(page)
                        if block_reason:
                            logger.error(
                                "0 images extracted. Final URL: %s | Reason: %s",
                                final_url,
                                block_reason,
                            )
                            raise PrivateOrLoginWalledError(
                                "This post is private, unavailable, or blocked by a Facebook login wall."
                            )

                        logger.error(
                            "0 images extracted. Final URL: %s | Reason: No candidate scontent URLs found in DOM (%d) or HTML/JSON (%d).",
                            final_url,
                            len(dom_urls),
                            len(json_urls),
                        )
                        raise NoImagesFoundError(
                            "No extractable high-resolution images were found in this Facebook post."
                        )

                    return deduped_urls

                except ScraperError:
                    raise
                except PlaywrightTimeoutError as exc:
                    logger.error("Timeout scraping %s (final URL: %s): %s", validated_url, page.url, exc)
                    raise ScraperError(
                        "Timed out while loading the Facebook post. Please check the URL and try again."
                    ) from exc
                except Exception as exc:
                    logger.error("Unexpected error scraping %s (final URL: %s): %s", validated_url, page.url, exc)
                    raise ScraperError(f"Failed to scrape Facebook post: {exc}") from exc
                finally:
                    context.close()
        finally:
            browser.close()

    raise NoImagesFoundError("No extractable high-resolution images were found in this Facebook post.")


async def extract_post_image_urls(
    post_url: str,
    timeout_ms: int = 30000,
    max_images: int | None = None,
) -> list[str]:
    """
    Validate the Facebook post URL and run sync_playwright() in a worker thread
    via asyncio.to_thread to avoid Windows event-loop subprocess issues.
    """
    validated_url = validate_facebook_url(post_url)
    return await asyncio.to_thread(_extract_post_image_urls_sync, validated_url, timeout_ms, max_images)
