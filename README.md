# Google Drive Restricted Video Downloader — Telegram Bot

## Prerequisites

| Dependency | Purpose |
|---|---|
| Python 3.11+ | Runtime |
| FFmpeg | Merging audio + video streams |
| Chromium (via Playwright) | Headless browser for stream interception |

## Quick Start

```bash
# 1. Clone & enter the project
cd gdrive-bot

# 2. Create a virtual environment
python -m venv .venv && source .venv/bin/activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright's Chromium browser
playwright install chromium
# On Linux you may also need system deps:
playwright install-deps chromium

# 5. Make sure FFmpeg is installed
# Ubuntu/Debian:  sudo apt install ffmpeg
# macOS:          brew install ffmpeg

# 6. Configure
cp .env.example .env
# Edit .env and set your BOT_TOKEN

# 7. Run
python bot.py
