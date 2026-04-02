"""
Auto-link detector and router for the bot.

Responsibilities:
- Intercept all non-command text messages and look for URLs.
- Decide whether the URL is a VIDEO (Google Drive file link) or a DOCUMENT
  (Google Docs/Slides/Drive viewer-style content).
- Dispatch to the correct pipeline (video stream extraction + merge, or view-only
  document rendering → images → PDF) with status updates and proper cleanup.
- Maintain a task registry so a user has only one active extraction at a time.
- Honor Telegram file-size limits (fallback from PDF to images if too large).

This plugin does *not* introduce commands; it runs automatically for every message
and relies on URL parsing + predictable behaviors.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes, MessageHandler, filters

from config import (
    TELEGRAM_FILE_LIMIT_BYTES,
    OVERALL_TIMEOUT_S,
    BASE_WORK_DIR,
)
from plugins.video_extractor import extract_video_streams
from plugins.video_downloader import download_and_merge, cleanup_files
from plugins.doc_extractor import extract_doc_pages, build_pdf
from plugins.utils import find_first_url, sanitize_filename

logger = logging.getLogger(__name__)

# ── Active task registry { user_id: asyncio.Task } ───────────────────────────
_active: dict[int, asyncio.Task] = {}

# ── URL matching patterns ───────────────────────────────────────────────────
_GDRIVE_VIDEO_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*drive\.google\.com/file/d/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

_GDOCS_DOC_RE = re.compile(
    r"https?://docs\.google\.com/"
    r"(?:document|presentation|spreadsheets|forms)/d/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

_GDRIVE_VIEWER_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*drive\.google\.com/"
    r"(?:file/d/[A-Za-z0-9_-]+/(?:preview|view|edit)|viewer)",
    re.IGNORECASE,
)


class LinkType:
    VIDEO = "video"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


def detect_link_type(url: str) -> str:
    """
    Heuristic classification of the provided URL:
      - drive.google.com/file/d/... -> VIDEO
      - docs.google.com/(document|presentation|spreadsheets|forms) -> DOCUMENT
      - drive.google.com/viewer/preview/etc. -> DOCUMENT
      - anything else -> UNKNOWN (defaults to DOCUMENT for safety)
    """
    if _GDRIVE_VIDEO_RE.search(url):
        return LinkType.VIDEO
    if _GDOCS_DOC_RE.search(url):
        return LinkType.DOCUMENT
    if _GDRIVE_VIEWER_RE.search(url):
        return LinkType.DOCUMENT
    return LinkType.UNKNOWN


def _resolve_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """
    Find a URL in the message or its reply (checks text_link entities first,
    then message text/caption, then replied message). This prioritizes explicit
    links from the user and prevents incidental text from triggering.
    """
    text = update.message.text or ""

    # Check for a URL in the command argument context (if a command wrapper ever used)
    parts = text.split(maxsplit=1)
    if len(parts) >= 2:
        url = find_first_url(parts[1])
        if url:
            return url

    # Check replied message
    if update.message.reply_to_message:
        reply = update.message.reply_to_message
        if reply.entities:
            for ent in reply.entities:
                if ent.type == "text_link" and ent.url:
                    return ent.url
        reply_text = reply.text or reply.caption or ""
        url = find_first_url(reply_text)
        if url:
            return url

    # The message body itself
    return find_first_url(text)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log task completion exceptions (especially for fire-and-forget pipelines)."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Unhandled exception in background extraction task")


async def _run_video_pipeline(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str,
) -> None:
    """Handle video URL: intercept streams → download → merge → send MP4 → cleanup."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Normalize Drive video URL to /view when using the viewer path
    if _GDRIVE_VIDEO_RE.search(url) and not url.rstrip("/").endswith("/view"):
        url = url.rstrip("/") + "/view"

    short_url = url[:70] + ("…" if len(url) > 70 else "")

    status = await update.message.reply_text(
        f"🎥 <b>Video mode</b>\n\n"
        f"⏳ Opening link in headless browser…\n"
        f"🔗 <code>{short_url}</code>",
        parse_mode=ParseMode.HTML,
    )

    try:
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)

        # Step 1: Extract best video + audio stream URLs
        result = await extract_video_streams(url)

        if result.error and (not result.video or not result.audio):
            await status.edit_text(
                f"❌ <b>Video stream detection failed</b>\n\n{result.error}\n\n"
                f"💡 <b>Tip:</b> If this is actually a document, I can handle it with: "
                f"<code>/extract {short_url}</code> (or just send the link again).",
                parse_mode=ParseMode.HTML,
            )
            return

        video = result.video
        audio = result.audio
        assert video is not None and audio is not None

        await status.edit_text(
            f"✅ <b>Streams detected!</b>\n"
            f"🎥 Video: <code>{video.quality}</code> (itag {video.itag})\n"
            f"🔊 Audio: <code>{audio.quality}</code> (itag {audio.itag})\n\n"
            f"⬇️ Downloading and merging…",
            parse_mode=ParseMode.HTML,
        )
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)

        # Step 2: Download and merge
        filename_stem = sanitize_filename(f"gdrive_{video.quality}")
        output_path = await download_and_merge(
            video_url=video.clean_url,
            audio_url=audio.clean_url,
            filename_stem=filename_stem,
        )

        file_size = output_path.stat().st_size

        # Step 3: Send to Telegram (fallback if too large)
        if file_size > TELEGRAM_FILE_LIMIT_BYTES:
            await status.edit_text(
                f"⚠️ The merged video is <b>{file_size / 1024 / 1024:.1f} MiB</b> — it exceeds "
                f"Telegram’s {TELEGRAM_FILE_LIMIT_BYTES // (1024*1024)} MiB limit for bots.\n\n"
                f"🎥 {video.quality} • 🔊 {audio.quality}\n\n"
                f"💡 You can try a lower quality / smaller segment or use a local Bot API setup.",
                parse_mode=ParseMode.HTML,
            )
            return

        await status.edit_text("📤 Uploading to Telegram…")
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)

        with open(output_path, "rb") as vf:
            await update.message.reply_video(
                video=vf,
                filename=output_path.name,
                caption=(
                    f"🎥 {video.quality} + 🔊 {audio.quality}\n"
                    f"📦 {file_size / 1024 / 1024:.1f} MiB"
                ),
                supports_streaming=True,
                read_timeout=300,
                write_timeout=300,
            )

        await status.edit_text("✅ <b>Video sent successfully!</b>", parse_mode=ParseMode.HTML)

    except asyncio.CancelledError:
        try:
            await status.edit_text("❌ Video processing was cancelled.")
        except Exception:
            pass

    except Exception as exc:
        logger.exception("Video pipeline failed")
        try:
            await status.edit_text(
                f"❌ <b>Video error:</b>\n<code>{str(exc)[:400]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    finally:
        # Cleanup intermediate files if they're defined (merged output already sent)
        try:
            if "output_path" in dir() and output_path and output_path.exists():
                cleanup_files(output_path)
        except Exception:
            pass
        _active.pop(user_id, None)


async def _run_doc_pipeline(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    url: str,
    as_images: bool = False,
) -> None:
    """Handle document URL: render pages → extract images → PDF (or images) → send → cleanup."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    short_url = url[:70] + ("…" if len(url) > 70 else "")
    mode_label = "📸 Images" if as_images else "📄 PDF"

    status = await update.message.reply_text(
        f"📄 <b>Document mode</b> → {mode_label}\n\n"
        f"🌐 Opening page in headless browser…\n"
        f"🔗 <code>{short_url}</code>",
        parse_mode=ParseMode.HTML,
    )

    async def _progress(text: str) -> None:
        """Lightweight progress updater for long-running extraction stages."""
        try:
            await status.edit_text(
                f"{text}\n\n🔗 <code>{short_url}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass  # ignore message edit errors (rate limits / unchanged state)

    task_dir = BASE_WORK_DIR / f"u{user_id}_{id(asyncio.current_task())}"
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)

        # Step 1: Extract rendered page images (blob images via canvas)
        try:
            pages = await asyncio.wait_for(
                extract_doc_pages(url, task_dir, progress=_progress),
                timeout=OVERALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            await status.edit_text(
                f"⏰ <b>Extraction timed out</b> ({OVERALL_TIMEOUT_S // 60} minutes).\n\n"
                "The document may be too large/complex or it required authenticated access.\n\n"
                f"💡 If this is actually a <b>video</b>, try:\n"
                f"<code>/video {short_url}</code> (or just re-send the link).",
                parse_mode=ParseMode.HTML,
            )
            return

        if not pages:
            await status.edit_text(
                "❌ <b>No document pages found.</b>\n\n"
                "This can happen if the link requires login, is not viewable, or uses a different "
                "rendering method (non-blob images). Ensure the link is shared/viewable and retry.\n\n"
                f"💡 If this is a <b>video</b> instead, send:\n"
                f"<code>/video {short_url}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        total = len(pages)
        await _progress(f"✅ Extracted <b>{total}</b> pages!")

        # Step 2: Deliver as images (direct mode)
        if as_images:
            await _progress(f"📤 Sending {total} images…")
            await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

            for idx, img_path in enumerate(pages, 1):
                try:
                    with open(img_path, "rb") as fh:
                        await ctx.bot.send_document(
                            chat_id=chat_id,
                            document=fh,
                            filename=img_path.name,
                            caption=f"📄 Page {idx}/{total}",
                        )
                except Exception as exc:
                    logger.warning("Page %d failed to send: %s", idx, exc)
                if idx % 8 == 0:  # gentle pause to avoid rate-limits
                    await asyncio.sleep(1.5)

            await status.edit_text(
                f"✅ <b>Done!</b>  Sent <b>{total}</b> page images.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Step 3: Build PDF and send (fallback to images if too large)
        await _progress("📑 Building PDF…")
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

        pdf_path = task_dir / "extracted_document.pdf"
        build_pdf(pages, pdf_path)

        pdf_size = pdf_path.stat().st_size
        size_mb  = pdf_size / 1024 / 1024

        if pdf_size > TELEGRAM_FILE_LIMIT_BYTES:
            await _progress(
                f"⚠️ PDF is <b>{size_mb:.1f} MB</b> — exceeds Telegram’s {TELEGRAM_FILE_LIMIT_BYTES // (1024*1024)} MB limit.\n"
                "Sending individual images instead…"
            )

            for idx, img_path in enumerate(pages, 1):
                try:
                    with open(img_path, "rb") as fh:
                        await ctx.bot.send_document(
                            chat_id=chat_id,
                            document=fh,
                            filename=img_path.name,
                            caption=f"📄 Page {idx}/{total}",
                        )
                except Exception as exc:
                    logger.warning("Page %d failed to send: %s", idx, exc)
                if idx % 8 == 0:
                    await asyncio.sleep(1.5)

            await status.edit_text(
                f"✅ <b>Done!</b>  PDF was too large — sent <b>{total}</b> images instead.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Normal PDF delivery
        with open(pdf_path, "rb") as fh:
            await ctx.bot.send_document(
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

    except asyncio.CancelledError:
        try:
            await status.edit_text("❌ Document extraction cancelled.")
        except Exception:
            pass

    except Exception as exc:
        logger.exception("Document pipeline failed")
        try:
            await status.edit_text(
                f"❌ <b>Document error:</b>\n<code>{str(exc)[:400]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    finally:
        # Cleanup all temp files created in the extraction directory
        shutil.rmtree(task_dir, ignore_errors=True)
        _active.pop(user_id, None)


async def on_plain_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Core automatic handler: triggered by any *non-command text message* containing
    a URL. Decides the intent (video vs document), prevents multiple simultaneous
    tasks per user, and launches the proper pipeline.
    """
    uid = update.effective_user.id

    # Guard against already-running task
    if uid in _active and not _active[uid].done():
        await update.message.reply_text(
            "⚠️ You already have a running extraction/request.\n"
            "Please wait for it to finish or send /cancel (if available) and try again."
        )
        return

    url = _resolve_url(update, ctx)
    if not url:
        return  # No URL found — let other handlers handle it (or do nothing)

    link_type = detect_link_type(url)
    logger.info("User %d → %s  detected_type=%s", uid, url, link_type)

    # Decide pipeline (fall back to document if unknown)
    if link_type == LinkType.VIDEO:
        task = asyncio.create_task(_run_video_pipeline(update, ctx, url))
    else:
        # Default to document mode (PDF) unless explicitly overridden elsewhere
        task = asyncio.create_task(_run_document_pipeline(update, ctx, url, as_images=False))

    task.add_done_callback(_log_task_exception)
    _active[uid] = task


def setup_autolink_handlers(app) -> None:
    """
    Register the automatic URL handler with the bot application.

    All non-command text messages that contain a URL are intercepted and routed
    to the correct pipeline (video or document) with appropriate progress, error reporting,
    delivery, and cleanup. This design keeps the user interface minimal: URLs work
    directly without custom commands while still maintaining extensibility in plugins/.
    """
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r"https?://[^\s<>\"']+"),
            on_plain_url,
        )
    )
