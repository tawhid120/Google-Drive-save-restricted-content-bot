"""
Playwright-based network interception that mirrors the JS userscript's
`setupNetworkObserver`, XHR-open, and fetch hooks.

Instead of hooking JS prototypes we use Playwright's first-class
`page.on("request", ...)` which fires for **every** network request the
browser makes — including XHR, fetch, media-source, and sub-frame requests.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Request,
    Playwright,
)

from config import (
    PARAMS_TO_REMOVE,
    ITAG_AUDIO,
    ITAG_VIDEO,
    PAGE_LOAD_TIMEOUT_MS,
    STREAM_DETECTION_TIMEOUT_S,
    PLAYBACK_CLICK_DELAY_S,
)

logger = logging.getLogger(__name__)


# ── Data containers ──────────────────────────────────────────────────────────

@dataclass
class StreamInfo:
    """A single detected audio or video stream."""
    itag: str
    stream_type: str          # "audio" | "video"
    quality: str              # human-readable label
    clean_url: str            # videoplayback URL with junk params stripped


@dataclass
class ExtractionResult:
    """Aggregated extraction output."""
    video: StreamInfo | None = None
    audio: StreamInfo | None = None
    all_streams: dict[str, StreamInfo] = field(default_factory=dict)
    error: str | None = None


# ── URL processing (direct port of the JS `handleUrl`) ──────────────────────

def _clean_videoplayback_url(raw_url: str) -> tuple[str | None, str | None, str | None]:
    """
    Parse a raw ``videoplayback`` URL, strip the params from
    ``PARAMS_TO_REMOVE``, and return ``(clean_url, itag, mime)``.

    Returns ``(None, None, None)`` when the URL cannot be used.
    """
    try:
        parsed = urlparse(raw_url)
        params = parse_qs(parsed.query, keep_blank_values=True)

        # Extract itag and mime before cleaning
        itag_values = params.get("itag")
        mime_values = params.get("mime")

        if not itag_values or not mime_values:
            return None, None, None

        itag = itag_values[0]
        mime = mime_values[0]

        # Remove the noisy / range-limiting params — mirrors JS logic exactly
        for param_name in PARAMS_TO_REMOVE:
            params.pop(param_name, None)

        # Rebuild the query string (each value is a list in parse_qs output)
        clean_query = urlencode(
            {k: v[0] for k, v in params.items()},
            doseq=False,
        )
        clean_url = urlunparse(parsed._replace(query=clean_query))
        return clean_url, itag, mime

    except Exception:
        logger.debug("Failed to parse videoplayback URL: %s", raw_url, exc_info=True)
        return None, None, None


def _quality_label(itag: str, stream_type: str) -> str:
    """Return a human-readable quality string for the given itag."""
    if stream_type == "audio":
        return ITAG_AUDIO.get(itag, f"Audio ({itag})")
    return ITAG_VIDEO.get(itag, f"Video ({itag})")


# ── Playwright extraction engine ────────────────────────────────────────────

class _StreamCollector:
    """
    Mutable accumulator attached to a Playwright page via
    ``page.on("request", collector)``.  Mirrors the JS
    ``detectedStreams`` Map and the ``processUrl`` / ``handleUrl`` flow.
    """

    def __init__(self) -> None:
        self.streams: dict[str, StreamInfo] = {}       # keyed by itag
        self._video_found = asyncio.Event()
        self._audio_found = asyncio.Event()

    # -- Playwright request callback (equivalent to XHR/fetch/perf hooks) -----

    def __call__(self, request: Request) -> None:
        """Playwright invokes this for every outgoing request."""
        url = request.url
        if "videoplayback" not in url:
            return
        self._process(url)

    def _process(self, raw_url: str) -> None:
        clean_url, itag, mime = _clean_videoplayback_url(raw_url)
        if not clean_url or not itag or not mime:
            return

        # Already seen this itag — skip
        if itag in self.streams:
            return

        stream_type = "audio" if "audio" in mime else "video"
        quality = _quality_label(itag, stream_type)

        info = StreamInfo(
            itag=itag,
            stream_type=stream_type,
            quality=quality,
            clean_url=clean_url,
        )
        self.streams[itag] = info
        logger.info("Detected %s stream  itag=%s  quality=%s", stream_type, itag, quality)

        if stream_type == "video":
            self._video_found.set()
        else:
            self._audio_found.set()

    # -- Wait helpers ---------------------------------------------------------

    async def wait_for_both(self, timeout: float) -> bool:
        """Return *True* when at least one video **and** one audio stream are found."""
        try:
            await asyncio.wait_for(
                asyncio.gather(self._video_found.wait(), self._audio_found.wait()),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            return False

    # -- Best-stream selectors ------------------------------------------------

    def best_video(self) -> StreamInfo | None:
        """Pick the highest-quality video stream based on itag priority."""
        # Preference order: highest resolution first
        priority = [
            "266", "264", "137", "299", "136", "298",
            "135", "248", "134", "247", "244", "133",
            "243", "242", "160", "22", "18",
        ]
        for itag in priority:
            if itag in self.streams and self.streams[itag].stream_type == "video":
                return self.streams[itag]
        # Fallback: return any video
        for s in self.streams.values():
            if s.stream_type == "video":
                return s
        return None

    def best_audio(self) -> StreamInfo | None:
        priority = ["141", "251", "140", "250", "249", "139"]
        for itag in priority:
            if itag in self.streams and self.streams[itag].stream_type == "audio":
                return self.streams[itag]
        for s in self.streams.values():
            if s.stream_type == "audio":
                return s
        return None


async def _try_start_playback(page: Page) -> None:
    """
    Attempt to click the video player element so the browser starts
    fetching ``videoplayback`` segments.  Not critical — some pages
    auto-play.
    """
    # Possible selectors for the play button or the video surface
    selectors = [
        "video",                                         # raw <video> tag
        '[aria-label="Play"]',                           # English UI
        '[aria-label="Lecture"]',                        # French UI
        '[data-tooltip="Play"]',
        ".ndfHFb-c4YZDc-Wrber",                         # Drive video player
        ".ytp-large-play-button",                        # YouTube-style player
    ]
    for sel in selectors:
        try:
            element = page.locator(sel).first
            if await element.is_visible(timeout=2_000):
                await element.click(timeout=3_000)
                logger.info("Clicked playback element: %s", sel)
                return
        except Exception:
            continue
    logger.info("Could not auto-click a play button — hoping for autoplay")


async def extract_streams(drive_url: str) -> ExtractionResult:
    """
    Open *drive_url* in headless Chromium, intercept network requests,
    and return the best video + audio ``StreamInfo`` objects.

    This is the main public entry point called by the Telegram bot handler.
    """
    pw: Playwright | None = None
    browser: Browser | None = None

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )

        context: BrowserContext = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            # Accept cookies / consent so Drive doesn't block playback
            locale="en-US",
            bypass_csp=True,
        )

        page: Page = await context.new_page()

        # -- Attach the network observer BEFORE navigation --------------------
        collector = _StreamCollector()
        page.on("request", collector)
        # Also listen on responses (some CDN requests only surface there)
        page.on("response", lambda resp: collector(resp.request))

        logger.info("Navigating to %s", drive_url)
        try:
            await page.goto(drive_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as exc:
            logger.warning("page.goto raised (may still work): %s", exc)

        # Wait a moment for the page JS to settle, then try to trigger play
        await asyncio.sleep(PLAYBACK_CLICK_DELAY_S)
        await _try_start_playback(page)

        # -- Wait for streams -------------------------------------------------
        both_ok = await collector.wait_for_both(timeout=STREAM_DETECTION_TIMEOUT_S)

        if not both_ok:
            # Even if we timed out we may have *some* streams; log what we got
            logger.warning(
                "Timeout waiting for both streams. Found %d total (%s)",
                len(collector.streams),
                ", ".join(s.quality for s in collector.streams.values()),
            )

        video = collector.best_video()
        audio = collector.best_audio()

        if not video and not audio:
            return ExtractionResult(error="No streams detected. The video may require authentication or the URL may be invalid.")

        result = ExtractionResult(
            video=video,
            audio=audio,
            all_streams=dict(collector.streams),
        )

        if not video:
            result.error = "Audio stream found but no video stream detected."
        elif not audio:
            result.error = "Video stream found but no audio stream detected."

        return result

    except Exception as exc:
        logger.exception("Extraction failed")
        return ExtractionResult(error=f"Extraction failed: {exc}")

    finally:
        if browser:
            await browser.close()
        if pw:
            await pw.stop()
