#!/usr/bin/env python3
"""
Unified Telegram Bot — Video + Document Auto-Extractor

Functional principles:
- Only /start is a command. All other messages (including URLs) trigger automatic
  behavior via the plugins/autolink module (video stream interception + merge,
  or view-only document image extraction → PDF).
- Plugins are pluggable: place modules in plugins/ defining setup_*handlers(app)
  and they will be discovered and registered by plugins/__init__.py.
- Robustness & cleanup: temporary files are created under BASE_WORK_DIR and are
  deleted after delivery; extraction tasks time out; Telegram actions and progress
  updates keep the user informed; failures return readable diagnostics.

Setup:
  1. pip install -r requirements.txt
  2. playwright install --with-deps chromium
  3. ensure ffmpeg is installed and available on PATH
  4. set BOT_TOKEN in .env
  5. python bot.py

Author: <Your Name> — built as modular plugin system
"""

from __future__ import annotations

import logging
import sys
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters, MessageHandler

from config import BOT_TOKEN
from plugins import setup_plugins_handlers

# Logging
logging.basicConfig(
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("UnifiedBot")


# ── /start command (ONLY explicit command) ──────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Provide user-friendly startup guidance covering all features + constraints."""
    user = update.effective_user
    await update.message.reply_text(
        f"👋 <b>Hello {user.first_name if user else 'there'}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "I’m your <b>unified helper bot</b> designed to work with links — I’ll decide what to do "
        "automatically so you don’t need extra commands.\n\n"
        "🎥 <b>VIDEO MODE (Google Drive)</b>\n"
        "• Send a <code>drive.google.com/file/d/…</code> link (ideally ending with <code>/view</code>).\n"
        "• I use a headless browser to intercept the real video/audio streams, clean them, "
        "download the best quality, then merge them into a single playable <code>.mp4</code>.\n"
        "• If streams aren’t detected (or require login), I’ll explain clearly.\n\n"
        "📄 <b>DOCUMENT MODE (view-only pages)</b>\n"
        "• Send a <code>docs.google.com/document/…</code>, Slides, Drive viewer link, or similar.\n"
        "• I scroll the page, extract <code>blob:</code> images, de-duplicate them, then "
        "compile a PDF (using <code>img2pdf</code> when available; else Pillow fallback).\n"
        "• If the PDF exceeds Telegram’s bot limit, I’ll automatically send the pages as individual images instead.\n\n"
        "<b>HOW TO SEND LINKS (pick any of these):</b>\n"
        "1. <code>/start</code> — this guide (the only command)\n"
        "2. Paste a URL directly in the chat (auto-detected)\n"
        "3. Reply to a message containing a URL (I’ll use the URL from the replied message)\n\n"
        "<b>Limits & Notes:</b>\n"
        "• Telegram bot upload limit: <b>50 MiB</b> (larger files may be sent as images or via local API setups)\n"
        "• Extraction timeout: <b>~10 minutes</b> overall\n"
        "• The file must be <b>viewable</b> (shared) — I can’t bypass logins/certificates.\n"
        "• Temporary files are stored securely and cleaned up automatically after processing.\n\n"
        "Just send a link and let me handle the rest — no extra commands needed! 🧠",
        parse_mode=ParseMode.HTML,
    )


# ── Error handler ────────────────────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled exceptions and keep tracebacks in logs (no sensitive dumps to users)."""
    logger.error("Unhandled exception occurred:", exc_info=ctx.error)


# ── Main bootstrap ──────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN:
        logger.critical(
            "BOT_TOKEN is not set. Create a .env file or export BOT_TOKEN=… "
            "before launching the bot."
        )
        sys.exit(1)

    logger.info("Building Telegram application…")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .build()
    )

    # Register /start
    app.add_handler(CommandHandler("start", cmd_start))

    # Load all plugins from plugins/ (each plugin defines setup_*handlers(app))
    setup_plugins_handlers(app)

    # Fallback: if no plugin claims the message, keep it safe (and log it)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: None))
    app.add_error_handler(error_handler)

    logger.info(
        "Bot live — only /start is a command. All other messages (including URLs) "
        "trigger automatic detection and processing."
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
