"""
Shared configuration for the unified Telegram bot (video + document support).
All feature modules (plugins/ and base extractors) should import from this file.
"""

import os
import tempfile
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "/tmp/gdrive_bot_downloads")

# ── Video mode configuration ───────────────────────────────────────────────
PARAMS_TO_REMOVE: list[str] = [
    "range", "rn", "rbuf", "cpn", "c", "cver", "srfvp", "ump", "alr",
]
ITAG_AUDIO: dict[str, str] = {
    "139": "48k AAC",  "140": "128k AAC", "141": "256k AAC",
    "249": "50k Opus",  "250": "70k Opus",  "251": "160k Opus",
}
ITAG_VIDEO: dict[str, str] = {
    "18":  "360p MP4",     "22":  "720p MP4",
    "160": "144p MP4",     "133": "240p MP4",    "134": "360p MP4",
    "135": "480p MP4",     "136": "720p MP4",    "137": "1080p MP4",
    "264": "1440p MP4",    "266": "2160p MP4",
    "298": "720p60 MP4",   "299": "1080p60 MP4",
    "242": "240p WebM",    "243": "360p WebM",   "244": "480p WebM",
    "247": "720p WebM",    "248": "1080p WebM",
}
PAGE_LOAD_TIMEOUT_MS: int        = 60_000
STREAM_DETECTION_TIMEOUT_S: int  = 45
PLAYBACK_CLICK_DELAY_S: float    = 4.0
DOWNLOAD_CHUNK_SIZE: int         = 10 * 1024 * 1024  # 10 MiB
DOWNLOAD_TIMEOUT_S: int          = 600
MAX_RETRIES: int                 = 3

# ── Document mode configuration ────────────────────────────────────────────
VIEWPORT_WIDTH:     int   = 1920
VIEWPORT_HEIGHT:    int   = 1080
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
SCROLL_STEP_PX:     int   = 750
SCROLL_PAUSE_MS:    int   = 1500
MAX_SCROLL_TICKS:   int   = 500
STABLE_THRESHOLD:   int   = 5
POST_SCROLL_WAIT_S: float = 4.0
MIN_IMG_WIDTH:      int   = 100
MIN_IMG_HEIGHT:     int   = 100
JPEG_QUALITY:       float = 0.95

# ── Shared limits & temp storage ───────────────────────────────────────────
TELEGRAM_FILE_LIMIT_BYTES: int = 50 * 1024 * 1024  # 50 MiB
OVERALL_TIMEOUT_S: int         = 600  # 10 minutes max for a single extraction task
BASE_WORK_DIR: Path = Path(tempfile.gettempdir()) / "gdrive_unified_bot"
BASE_WORK_DIR.mkdir(parents=True, exist_ok=True)
