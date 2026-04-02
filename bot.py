"""
Telegram Bot entry point.

Usage:
    1. pip install -r requirements.txt
    2. playwright install chromium
    3. Set BOT_TOKEN in .env
    4. python bot.py
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, TELEGRAM_FILE_LIMIT_BYTES
from extractor import extract_streams, ExtractionResult
from downloader import download_and_merge, cleanup

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────────

_GDRIVE_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*drive\.google\.com/file/d/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)


def _is_drive_url(text: str) -> bool:
    return bool(_GDRIVE_RE.search(text))


def _sanitize_filename(raw: str) -> str:
    """Produce a filesystem-safe name."""
    name = re.sub(r'[\\/:*?"<>|]', "_", raw).strip()
    return name or "gdrive_video"


# ── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Google Drive Video Downloader Bot*\n\n"
        "Send me a Google Drive video link and I will:\n"
        "1️⃣ Open it in a headless browser\n"
        "2️⃣ Intercept the video & audio streams\n"
        "3️⃣ Download and merge them into a single MP4\n"
        "4️⃣ Send the file back to you\n\n"
        "⚠️ The video must be viewable (shared) — I cannot bypass Google login.\n\n"
        "Just paste a link to get started!",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔧 *Commands*\n"
        "/start — Welcome message\n"
        "/help  — This help text\n\n"
        "📎 *Usage*\n"
        "Paste a Google Drive `/file/d/…` link.\n"
        "The bot will extract, download, and merge the video automatically.\n\n"
        "⏱ Processing usually takes 30 s – 3 min depending on file size.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Main message handler ────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    if not text:
        return

    if not _is_drive_url(text):
        await update.message.reply_text(
            "🔗 Please send a valid Google Drive video link.\n"
            "Example: `https://drive.google.com/file/d/FILE_ID/view`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    drive_url = _GDRIVE_RE.search(text).group(0)  # type: ignore[union-attr]
    # Ensure the URL ends with /view for proper playback page
    if not drive_url.endswith("/view"):
        drive_url = drive_url.rstrip("/") + "/view"

    status_msg = await update.message.reply_text("⏳ Opening link in headless browser…")

    # ── Step 1: Extract streams ──────────────────────────────────────────
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    result: ExtractionResult = await extract_streams(drive_url)

    if result.error and (not result.video or not result.audio):
        await status_msg.edit_text(f"❌ {result.error}")
        return

    video = result.video
    audio = result.audio

    assert video is not None and audio is not None  # guaranteed by check above

    streams_text = (
        f"✅ *Streams detected!*\n"
        f"🎥 Video: `{video.quality}` (itag {video.itag})\n"
        f"🔊 Audio: `{audio.quality}` (itag {audio.itag})\n\n"
        f"⬇️ Downloading and merging…"
    )
    await status_msg.edit_text(streams_text, parse_mode=ParseMode.MARKDOWN)
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)

    # ── Step 2: Download & merge ─────────────────────────────────────────
    output_path: Path | None = None
    try:
        filename_stem = _sanitize_filename(f"gdrive_{video.quality}")
        output_path = await download_and_merge(
            video_url=video.clean_url,
            audio_url=audio.clean_url,
            filename_stem=filename_stem,
        )

        file_size = output_path.stat().st_size

        # ── Step 3: Send to user ─────────────────────────────────────────
        if file_size > TELEGRAM_FILE_LIMIT_BYTES:
            await status_msg.edit_text(
                f"⚠️ Merged file is {file_size / 1024 / 1024:.1f} MiB — "
                f"exceeds Telegram's 50 MiB limit for bots.\n\n"
                f"🎥 Video: `{video.quality}`\n"
                f"🔊 Audio: `{audio.quality}`\n\n"
                f"Try a lower quality or use Telegram's Local Bot API for large files.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await status_msg.edit_text("📤 Uploading to Telegram…")
        await ctx.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        with open(output_path, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                filename=output_path.name,
                caption=(
                    f"🎥 {video.quality} + 🔊 {audio.quality}\n"
                    f"📦 {file_size / 1024 / 1024:.1f} MiB"
                ),
                supports_streaming=True,
                read_timeout=300,
                write_timeout=300,
            )

        await status_msg.edit_text("✅ Done!")

    except Exception as exc:
        logger.exception("Download/merge failed")
        await status_msg.edit_text(f"❌ Download or merge failed:\n`{exc}`", parse_mode=ParseMode.MARKDOWN)

    finally:
        # ── Step 4: Cleanup ──────────────────────────────────────────────
        if output_path:
            cleanup(output_path)


# ── Error handler ────────────────────────────────────────────────────────────

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception:", exc_info=ctx.error)


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set. Create a .env file or export it.")
        sys.exit(1)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot is starting — polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
