#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
  VIEW-ONLY DOCUMENT EXTRACTOR — Telegram Bot
═══════════════════════════════════════════════════════════════════════════════

  Extracts rendered page images from view-only web documents
  (Google Docs, Google Drive viewer, etc.) and returns a compiled
  PDF or individual images via Telegram.

  Tech Stack
  ──────────
  • python-telegram-bot  v20+   — async Telegram Bot framework
  • Playwright                  — headless Chromium browser automation
  • Pillow                      — image processing & PDF fallback
  • img2pdf  (optional)         — lossless JPEG→PDF (preserves quality)

  Setup
  ─────
  1.  pip install "python-telegram-bot[ext]" playwright Pillow img2pdf
  2.  playwright install --with-deps chromium
  3.  export BOT_TOKEN="123456:ABC-your-token"
  4.  python docextract_bot.py

  Bot Commands
  ────────────
  /start              — welcome message
  /extract  <url>     — extract document → send as PDF
  /images   <url>     — extract document → send as individual images
  /cancel             — cancel the running task
  /help               — detailed usage guide

  You can also just paste a URL directly (no command needed).

═══════════════════════════════════════════════════════════════════════════════
"""

import os
import re
import base64
import asyncio
import hashlib
import logging
import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Optional, Callable, Awaitable

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from playwright.async_api import async_playwright, Page
from PIL import Image

# ── Optional high-quality PDF library ────────────────────────────────────────
try:
    import img2pdf
    HAS_IMG2PDF = True
except ImportError:
    HAS_IMG2PDF = False

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("DocExtractor")

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# ── Browser settings ─────────────────────────────────────────────────────────
VIEWPORT_WIDTH:     int   = 1920
VIEWPORT_HEIGHT:    int   = 1080
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# ── Scroll tuning ────────────────────────────────────────────────────────────
SCROLL_STEP_PX:     int   = 750       # pixels per scroll tick
SCROLL_PAUSE_MS:    int   = 1500      # wait between scrolls (ms)
MAX_SCROLL_TICKS:   int   = 500       # absolute scroll limit
STABLE_THRESHOLD:   int   = 5         # stable heights → "bottom reached"
POST_SCROLL_WAIT_S: float = 4.0       # extra settle time after scrolling

# ── Image filtering ──────────────────────────────────────────────────────────
MIN_IMG_WIDTH:      int   = 100       # ignore icons / buttons
MIN_IMG_HEIGHT:     int   = 100
JPEG_QUALITY:       float = 0.95      # canvas → JPEG quality

# ── Telegram limits ──────────────────────────────────────────────────────────
TG_DOC_LIMIT:       int   = 50 * 1024 * 1024   # 50 MB
OVERALL_TIMEOUT_S:  int   = 600                 # 10 min extraction cap

# ── Temp storage ──────────────────────────────────────────────────────────────
BASE_WORK_DIR: Path = Path(tempfile.gettempdir()) / "docextract_bot"
BASE_WORK_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
#  ACTIVE-TASK REGISTRY   { user_id : asyncio.Task }
# ─────────────────────────────────────────────────────────────────────────────
_active: dict[int, asyncio.Task] = {}

# ─────────────────────────────────────────────────────────────────────────────
#  URL HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def find_first_url(text: str) -> Optional[str]:
    """Return the first HTTP(S) URL in *text*, or None."""
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(".,;:!?)>]") if m else None


# ─────────────────────────────────────────────────────────────────────────────
#  JAVASCRIPT — injected into the page
# ─────────────────────────────────────────────────────────────────────────────

# ① Index all blob <img> elements that meet size thresholds.
#    Stores references in window.__blobImgs for later per-image extraction.
JS_INDEX_BLOBS = """
(minW, minH) => {
    window.__blobImgs = [...document.getElementsByTagName("img")]
        .filter(img => /^blob:/.test(img.src))
        .filter(img => {
            const w = img.naturalWidth  || img.width;
            const h = img.naturalHeight || img.height;
            return w >= minW && h >= minH;
        });
    return window.__blobImgs.length;
}
"""

# ② Extract a single blob image by index → base64 data-URL.
#    Uses <canvas> to read pixel data; catches tainted-canvas errors.
JS_EXTRACT_ONE = """
(idx, quality) => {
    const img = window.__blobImgs[idx];
    if (!img) return null;

    const can = document.createElement("canvas");
    const ctx = can.getContext("2d");

    can.width  = img.naturalWidth  || img.width;
    can.height = img.naturalHeight || img.height;
    ctx.drawImage(img, 0, 0, can.width, can.height);

    try {
        return can.toDataURL("image/jpeg", quality);
    } catch (e) {
        // CORS-tainted canvas — skip silently
        return null;
    }
}
"""

# ③ Free a single slot to release memory early.
JS_FREE_ONE = "(idx) => { window.__blobImgs[idx] = null; }"

# ④ Quick blob count (used during scrolling to show progress).
JS_BLOB_COUNT = """
() => [...document.getElementsByTagName("img")]
      .filter(i => /^blob:/.test(i.src)).length
"""


# ─────────────────────────────────────────────────────────────────────────────
#  PLAYWRIGHT — SCROLL THE ENTIRE PAGE
# ─────────────────────────────────────────────────────────────────────────────

async def _scroll_to_bottom(
    page: Page,
    progress: Optional[Callable[[str], Awaitable[None]]] = None,
) -> None:
    """
    Incrementally scroll down the page so that every lazy-loaded
    blob image gets rendered and inserted into the DOM.

    Stops when the scroll height remains unchanged for
    STABLE_THRESHOLD consecutive ticks.
    """
    prev_height  = 0
    stable_count = 0

    for tick in range(1, MAX_SCROLL_TICKS + 1):
        # ── scroll one step ──────────────────────────────────────────────
        await page.evaluate(f"window.scrollBy(0, {SCROLL_STEP_PX})")
        await page.wait_for_timeout(SCROLL_PAUSE_MS)

        # ── check whether new content appeared ───────────────────────────
        cur_height = await page.evaluate(
            "() => Math.max("
            "  document.body.scrollHeight,"
            "  document.documentElement.scrollHeight"
            ")"
        )

        if cur_height == prev_height:
            stable_count += 1
            if stable_count >= STABLE_THRESHOLD:
                log.info(
                    f"Scroll finished — height stable after {tick} ticks"
                )
                break
        else:
            stable_count = 0
            prev_height  = cur_height

        # ── progress update every 10 ticks ───────────────────────────────
        if progress and tick % 10 == 0:
            blobs = await page.evaluate(JS_BLOB_COUNT)
            await progress(
                f"📜 Scrolling… tick {tick}  |  "
                f"🖼 {blobs} images found so far"
            )
    else:
        log.warning(f"Hit MAX_SCROLL_TICKS ({MAX_SCROLL_TICKS}) — proceeding")


# ─────────────────────────────────────────────────────────────────────────────
#  PLAYWRIGHT — FULL EXTRACTION PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

ProgressCB = Optional[Callable[[str], Awaitable[None]]]


async def extract_pages(
    url: str,
    out_dir: Path,
    progress: ProgressCB = None,
) -> list[Path]:
    """
    1. Launch headless Chromium and navigate to *url*.
    2. Scroll the full page to force lazy-loaded blob images into the DOM.
    3. Inject JS to read every blob <img> via <canvas>.toDataURL().
    4. Save de-duplicated JPEGs into *out_dir*.

    Returns a list of saved file paths **in document order**.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-web-security",        # avoid CORS issues
                "--allow-file-access-from-files",
            ],
        )

        ctx = await browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            user_agent=USER_AGENT,
        )
        page = await ctx.new_page()

        # ── 1. NAVIGATE ─────────────────────────────────────────────────
        if progress:
            await progress("🌐 Opening page…")

        try:
            await page.goto(url, wait_until="networkidle", timeout=60_000)
        except Exception:
            # networkidle can time out on heavy pages — fall back to "load"
            log.warning("networkidle timed out — retrying with 'load'")
            await page.goto(url, wait_until="load", timeout=60_000)

        # let initial renders settle
        await page.wait_for_timeout(3_000)

        # ── 2. SCROLL ───────────────────────────────────────────────────
        if progress:
            await progress("📜 Scrolling to load all pages…")

        await _scroll_to_bottom(page, progress)

        # extra settle time after scrolling
        if progress:
            await progress("⏳ Waiting for final rendering…")
        await page.wait_for_timeout(int(POST_SCROLL_WAIT_S * 1000))

        # scroll back to top (some viewers unload top images otherwise)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1_000)

        # ── 3. INDEX BLOB IMAGES ────────────────────────────────────────
        count: int = await page.evaluate(
            JS_INDEX_BLOBS, MIN_IMG_WIDTH, MIN_IMG_HEIGHT,
        )
        log.info(f"Indexed {count} blob images (≥{MIN_IMG_WIDTH}×{MIN_IMG_HEIGHT})")

        if count == 0:
            await browser.close()
            return []

        if progress:
            await progress(f"🔍 Found **{count}** pages — extracting…")

        # ── 4. EXTRACT ONE-BY-ONE (memory-friendly) ─────────────────────
        seen_hashes: set[str] = set()

        for i in range(count):
            data_url: Optional[str] = await page.evaluate(
                JS_EXTRACT_ONE, i, JPEG_QUALITY,
            )

            if not data_url:
                log.debug(f"Image {i} skipped (null / CORS)")
                await page.evaluate(JS_FREE_ONE, i)
                continue

            # strip the "data:image/jpeg;base64," header
            try:
                raw = base64.b64decode(data_url.split(",", 1)[1])
            except Exception:
                log.debug(f"Image {i} — base64 decode failed")
                await page.evaluate(JS_FREE_ONE, i)
                continue

            # de-duplicate by content hash
            digest = hashlib.md5(raw).hexdigest()
            if digest in seen_hashes:
                log.debug(f"Image {i} duplicate — skipping")
                await page.evaluate(JS_FREE_ONE, i)
                continue
            seen_hashes.add(digest)

            # persist to disk immediately
            page_num  = len(saved) + 1
            file_path = out_dir / f"page_{page_num:04d}.jpg"
            file_path.write_bytes(raw)
            saved.append(file_path)

            # free JS reference to reclaim browser memory
            await page.evaluate(JS_FREE_ONE, i)

            # progress every 5 pages or on the last one
            if progress and (page_num % 5 == 0 or i == count - 1):
                await progress(
                    f"📄 Extracted **{page_num}** / {count} pages…"
                )

        await browser.close()

    log.info(f"Extraction complete — {len(saved)} unique pages saved")
    return saved


# ─────────────────────────────────────────────────────────────────────────────
#  PDF CREATION
# ─────────────────────────────────────────────────────────────────────────────

def build_pdf(image_paths: list[Path], pdf_path: Path) -> Path:
    """
    Combine ordered JPEG page images into a single PDF.

    • Uses **img2pdf** if available (zero quality loss — embeds the
      original JPEG streams directly).
    • Falls back to **Pillow** otherwise (minor re-compression).
    """
    if not image_paths:
        raise ValueError("No images to compile into PDF")

    if HAS_IMG2PDF:
        log.info("Creating PDF with img2pdf (lossless)")
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert([str(p) for p in image_paths]))
    else:
        log.info("Creating PDF with Pillow (fallback)")
        frames: list[Image.Image] = []
        for p in image_paths:
            im = Image.open(p)
            if im.mode != "RGB":
                im = im.convert("RGB")
            frames.append(im)

        frames[0].save(
            pdf_path,
            "PDF",
            save_all=True,
            append_images=frames[1:],
            resolution=150.0,
        )
        for im in frames:
            im.close()

    log.info(f"PDF saved — {pdf_path.stat().st_size / 1024 / 1024:.1f} MB")
    return pdf_path


# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM — HANDLER HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """
    Try to find a URL from (in priority order):

    1. Command arguments   — /extract https://…
    2. Replied-to message  — reply to a forwarded link + /extract
    3. Message text itself — just paste a URL
    """
    text = update.message.text or ""

    # ① after the command word
    parts = text.split(maxsplit=1)
    if len(parts) >= 2:
        url = find_first_url(parts[1])
        if url:
            return url

    # ② replied message
    if update.message.reply_to_message:
        reply_text = (
            update.message.reply_to_message.text
            or update.message.reply_to_message.caption
            or ""
        )
        # also check TEXT_LINK entities in the replied message
        reply_msg = update.message.reply_to_message
        if reply_msg.entities:
            for ent in reply_msg.entities:
                if ent.type == "text_link" and ent.url:
                    return ent.url
        url = find_first_url(reply_text)
        if url:
            return url

    # ③ the message itself
    return find_first_url(text)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log unhandled exceptions from fire-and-forget tasks."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Unhandled exception in extraction task")


# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM — CORE EXTRACTION + DELIVERY
# ─────────────────────────────────────────────────────────────────────────────

async def _run_extraction(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    as_images: bool = False,
) -> None:
    """
    End-to-end pipeline:
      navigate → scroll → extract → build PDF → send → cleanup
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    short_url = url[:70] + ("…" if len(url) > 70 else "")

    # ── status message ────────────────────────────────────────────────────
    status = await update.message.reply_text(
        f"🚀 <b>Starting extraction</b>\n\n"
        f"🔗 <code>{short_url}</code>",
        parse_mode=ParseMode.HTML,
    )

    async def _progress(text: str) -> None:
        try:
            await status.edit_text(
                f"{text}\n\n🔗 <code>{short_url}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass                       # message unchanged / rate limited

    # ── work directory ────────────────────────────────────────────────────
    task_dir = BASE_WORK_DIR / f"u{user_id}_{id(asyncio.current_task())}"
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

        # ── extract (with overall timeout) ────────────────────────────────
        try:
            pages = await asyncio.wait_for(
                extract_pages(url, task_dir, _progress),
                timeout=OVERALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            await status.edit_text(
                f"⏰ <b>Extraction timed out</b> "
                f"({OVERALL_TIMEOUT_S // 60} min limit).\n\n"
                "The document may be too large or the page didn't load.",
                parse_mode=ParseMode.HTML,
            )
            return

        # ── nothing found? ────────────────────────────────────────────────
        if not pages:
            await status.edit_text(
                "❌ <b>No document pages found.</b>\n\n"
                "Possible reasons:\n"
                "• The URL doesn't contain blob-rendered pages\n"
                "• The document requires sign-in\n"
                "• The viewer uses a different rendering method\n\n"
                "Make sure the document is <b>publicly viewable</b>.",
                parse_mode=ParseMode.HTML,
            )
            return

        total = len(pages)
        await _progress(f"✅ Extracted <b>{total}</b> pages!")

        # ══════════════════════════════════════════════════════════════════
        #  DELIVERY — as individual images
        # ══════════════════════════════════════════════════════════════════
        if as_images:
            await _progress(f"📤 Sending {total} images…")
            await context.bot.send_chat_action(
                chat_id, ChatAction.UPLOAD_DOCUMENT,
            )

            for idx, img_path in enumerate(pages, 1):
                try:
                    with open(img_path, "rb") as fh:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=fh,
                            filename=img_path.name,
                            caption=f"📄 Page {idx}/{total}",
                        )
                except Exception as exc:
                    log.warning(f"Page {idx} send failed: {exc}")

                # Telegram rate-limit: ~30 msgs/sec (bot), be safe
                if idx % 8 == 0:
                    await asyncio.sleep(1.5)

            await status.edit_text(
                f"✅ <b>Done!</b>  Sent <b>{total}</b> page images.",
                parse_mode=ParseMode.HTML,
            )
            return

        # ══════════════════════════════════════════════════════════════════
        #  DELIVERY — as PDF
        # ══════════════════════════════════════════════════════════════════
        await _progress("📑 Building PDF…")
        await context.bot.send_chat_action(
            chat_id, ChatAction.UPLOAD_DOCUMENT,
        )

        pdf_path = task_dir / "extracted_document.pdf"
        build_pdf(pages, pdf_path)

        pdf_size = pdf_path.stat().st_size
        size_mb  = pdf_size / 1024 / 1024

        # ── too large for Telegram? → fall back to images ─────────────────
        if pdf_size > TG_DOC_LIMIT:
            await _progress(
                f"⚠️ PDF is <b>{size_mb:.1f} MB</b> — "
                f"exceeds Telegram's {TG_DOC_LIMIT // (1024*1024)} MB limit.\n"
                "Falling back to individual images…"
            )

            for idx, img_path in enumerate(pages, 1):
                try:
                    with open(img_path, "rb") as fh:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=fh,
                            filename=img_path.name,
                            caption=f"📄 Page {idx}/{total}",
                        )
                except Exception as exc:
                    log.warning(f"Page {idx} send failed: {exc}")
                if idx % 8 == 0:
                    await asyncio.sleep(1.5)

            await status.edit_text(
                f"✅ <b>Done!</b>  PDF too large — "
                f"sent <b>{total}</b> images instead.",
                parse_mode=ParseMode.HTML,
            )
            return

        # ── send the PDF ──────────────────────────────────────────────────
        with open(pdf_path, "rb") as fh:
            await context.bot.send_document(
                chat_id=chat_id,
                document=fh,
                filename="extracted_document.pdf",
                caption=(
                    f"📄 {total} pages  •  📦 {size_mb:.1f} MB\n"
                    f"🔗 {short_url}"
                ),
            )

        await status.edit_text(
            f"✅ <b>Done!</b>\n"
            f"📄 <b>{total}</b> pages  •  📦 <b>{size_mb:.1f} MB</b>",
            parse_mode=ParseMode.HTML,
        )

    # ── cancellation ──────────────────────────────────────────────────────
    except asyncio.CancelledError:
        try:
            await status.edit_text("❌ Extraction cancelled.")
        except Exception:
            pass

    # ── unexpected errors ─────────────────────────────────────────────────
    except Exception as exc:
        log.error(f"Extraction failed: {exc}", exc_info=True)
        try:
            await status.edit_text(
                f"❌ <b>Error:</b>\n<code>{str(exc)[:400]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # ── cleanup ───────────────────────────────────────────────────────────
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        _active.pop(user_id, None)


# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM — COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📄 <b>View-Only Document Extractor</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "I extract pages from view-only documents and "
        "send them as a PDF.\n\n"
        "<b>Quick start:</b>\n"
        "<code>/extract https://docs.google.com/document/d/.../view</code>\n\n"
        "<b>Commands:</b>\n"
        "• /extract &lt;url&gt;  — get PDF\n"
        "• /images  &lt;url&gt;  — get individual images\n"
        "• /cancel            — stop running task\n"
        "• /help              — detailed guide\n\n"
        "Or just <b>paste a URL</b> — I'll detect it automatically.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 <b>How it works</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Step 1</b> — I open your link in a headless Chromium browser.\n"
        "<b>Step 2</b> — I scroll the entire page to force-load every "
        "lazy-rendered page image.\n"
        "<b>Step 3</b> — I read every <code>blob:</code> image via "
        "<code>&lt;canvas&gt;</code> and export it as JPEG.\n"
        "<b>Step 4</b> — I compile all pages into a PDF and send it.\n\n"
        "<b>Three ways to provide the URL:</b>\n"
        "① <code>/extract https://…</code>\n"
        "② Reply to a message containing a URL → <code>/extract</code>\n"
        "③ Just paste a URL directly\n\n"
        "<b>Supported viewers:</b>\n"
        "• Google Docs (view-only)\n"
        "• Google Drive document viewer\n"
        "• Any page rendering documents as blob images\n\n"
        "<b>Limits:</b>\n"
        f"• Max PDF size: {TG_DOC_LIMIT // (1024*1024)} MB (Telegram)\n"
        f"• Timeout: {OVERALL_TIMEOUT_S // 60} minutes\n"
        "• If PDF exceeds the limit → auto fallback to images\n\n"
        "<b>Tip:</b> Use <code>/images</code> instead of <code>/extract</code> "
        "if you want individual page images.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    task = _active.get(uid)
    if task and not task.done():
        task.cancel()
        _active.pop(uid, None)
        await update.message.reply_text("❌ Extraction cancelled.")
    else:
        await update.message.reply_text("ℹ️ No active task to cancel.")


async def cmd_extract(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """  /extract <url>  or  reply-to-link + /extract  """
    await _dispatch(update, ctx, as_images=False)


async def cmd_images(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """  /images <url>  — deliver as individual images  """
    await _dispatch(update, ctx, as_images=True)


async def on_plain_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect a bare URL (no command needed)."""
    url = find_first_url(update.message.text or "")
    if url:
        await _dispatch(update, ctx, as_images=False, override_url=url)


# ─────────────────────────────────────────────────────────────────────────────

async def _dispatch(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    as_images: bool = False,
    override_url: Optional[str] = None,
) -> None:
    """Validate, de-dup, launch the extraction task."""
    uid = update.effective_user.id

    # already running?
    if uid in _active and not _active[uid].done():
        await update.message.reply_text(
            "⚠️ You already have a running extraction.\n"
            "Send /cancel first, then try again."
        )
        return

    url = override_url or _resolve_url(update, ctx)

    if not url:
        await update.message.reply_text(
            "❌ <b>No URL found.</b>\n\n"
            "Usage:\n"
            "<code>/extract https://docs.google.com/document/d/…/view</code>\n\n"
            "Or reply to a message containing a URL with /extract",
            parse_mode=ParseMode.HTML,
        )
        return

    log.info(f"User {uid} → {url}  (images={as_images})")

    task = asyncio.create_task(
        _run_extraction(update, ctx, url, as_images=as_images)
    )
    task.add_done_callback(_log_task_exception)
    _active[uid] = task


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        print(
            "╔════════════════════════════════════════╗\n"
            "║  ERROR: BOT_TOKEN not set              ║\n"
            "║                                        ║\n"
            "║  export BOT_TOKEN='123456:ABC-...'     ║\n"
            "╚════════════════════════════════════════╝"
        )
        return

    log.info("Building application…")
    app = Application.builder().token(BOT_TOKEN).build()

    # ── register handlers ─────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("extract", cmd_extract))
    app.add_handler(CommandHandler("images",  cmd_images))

    # auto-detect bare URLs (lowest priority)
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex(_URL_RE),
            on_plain_url,
        )
    )

    log.info(
        "Bot is live!  Commands: "
        "/start  /help  /extract  /images  /cancel"
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
