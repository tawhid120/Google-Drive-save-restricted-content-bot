"""
Configuration constants translated from the reference JavaScript userscript.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "/tmp/gdrive_bot_downloads")

# ── URL cleaning: parameters stripped from the raw videoplayback URL ─────────
# Mirrors the JS constant `PARAMS_TO_REMOVE`
PARAMS_TO_REMOVE: list[str] = [
    "range",
    "rn",
    "rbuf",
    "cpn",
    "c",
    "cver",
    "srfvp",
    "ump",
    "alr",
]

# ── ITAG quality maps (kept for logging / user feedback) ─────────────────────
ITAG_AUDIO: dict[str, str] = {
    "139": "48k AAC",
    "140": "128k AAC",
    "141": "256k AAC",
    "249": "50k Opus",
    "250": "70k Opus",
    "251": "160k Opus",
}

ITAG_VIDEO: dict[str, str] = {
    "18": "360p MP4",
    "22": "720p MP4",
    "160": "144p MP4",
    "133": "240p MP4",
    "134": "360p MP4",
    "135": "480p MP4",
    "136": "720p MP4",
    "137": "1080p MP4",
    "264": "1440p MP4",
    "266": "2160p MP4",
    "298": "720p60 MP4",
    "299": "1080p60 MP4",
    "242": "240p WebM",
    "243": "360p WebM",
    "244": "480p WebM",
    "247": "720p WebM",
    "248": "1080p WebM",
}

# ── Playwright / download tunables ───────────────────────────────────────────
PAGE_LOAD_TIMEOUT_MS: int = 60_000          # max wait for the page
STREAM_DETECTION_TIMEOUT_S: int = 45        # seconds to wait for both streams
PLAYBACK_CLICK_DELAY_S: float = 4.0         # pause before clicking the player
DOWNLOAD_CHUNK_SIZE: int = 10 * 1024 * 1024 # 10 MiB per HTTP range chunk
DOWNLOAD_TIMEOUT_S: int = 600               # per-stream download timeout
MAX_RETRIES: int = 3                        # download retry attempts

# ── Telegram file-size limit (bot API allows up to 50 MiB without local API) ─
TELEGRAM_FILE_LIMIT_BYTES: int = 50 * 1024 * 1024
