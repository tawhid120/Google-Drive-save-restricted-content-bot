"""
plugins/__init__.py — Unified plugin loader and registry

This module helps you add, remove, and extend bot behavior by placing
new modules in plugins/ (without changing bot.py). Plugins can register:

- Telegram command handlers,
- Telegram message handlers (filtered text, URLs, media, etc.),
- Background task management (store and cancel tasks via the shared dict),
- Utilities for URL detection, cleanup, logging, etc.,
- Or custom services like admin-only commands, usage analytics, retries, caching…

The entry point is `load_plugins(app, active_tasks)`: call it once after creating
your `Application` object in bot.py.
"""

import logging
from typing import Callable, Dict, Any
from telegram.ext import Application

# Allow plugins to easily access configuration and shared utilities
from config import *  # noqa
from .common import detect_link_type, find_first_url, LinkType

logger = logging.getLogger("Plugins")

# Typing hint for active tasks registry passed from bot.py
ActiveTasks = Dict[int, Any]  # user_id -> asyncio.Task


def load_plugins(app: Application, active_tasks: ActiveTasks) -> None:
    """
    Dynamically load and register plugins by importing their modules.

    Each plugin module should expose a `register(app, active_tasks)` function
    that sets up the command/message handlers it provides. This keeps logic
    modular and avoids bloating bot.py.

    Note: Plugins that import heavy libraries should place those imports inside
    their handler methods to reduce startup latency (e.g., Playwright, ffmpeg-python).
    """
    logger.info("Loading plugins from plugins/…")

    # Import plugin modules by name (explicit listing makes behavior predictable)
    try:
        from . import video_handlers as video
        video.register(app, active_tasks)
        logger.info("Video plugin registered.")
    except Exception as e:
        logger.error("Failed to register video plugin: %s", e, exc_info=True)

    try:
        from . import doc_handlers as docs
        docs.register(app, active_tasks)
        logger.info("Document plugin registered.")
    except Exception as e:
        logger.error("Failed to register document plugin: %s", e, exc_info=True)

    # Add future plugins here as needed:
    # from . import admin, monitoring, analytics, error_handling, ...
    # admin.register(app, active_tasks)

    logger.info("All plugins loaded.")
