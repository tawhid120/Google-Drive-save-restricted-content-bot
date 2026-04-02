"""
Downloads separate audio/video streams and merges them with FFmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path

import httpx
import ffmpeg  # ffmpeg-python

from config import (
    DOWNLOAD_DIR,
    DOWNLOAD_CHUNK_SIZE,
    DOWNLOAD_TIMEOUT_S,
    MAX_RETRIES,
)

logger = logging.getLogger(__name__)


def _ensure_download_dir() -> Path:
    path = Path(DOWNLOAD_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def download_stream(url: str, dest: Path, label: str = "") -> Path:
    """
    Download a single stream URL to *dest* using chunked transfer.

    Retries up to ``MAX_RETRIES`` times on transient errors.
    """
    logger.info("Downloading %s → %s", label, dest.name)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://drive.google.com/",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_S, connect=30.0),
            ) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0

                    with open(dest, "wb") as fp:
                        async for chunk in resp.aiter_bytes(chunk_size=DOWNLOAD_CHUNK_SIZE):
                            fp.write(chunk)
                            downloaded += len(chunk)
                            if total:
                                pct = downloaded * 100 // total
                                if pct % 20 == 0:
                                    logger.info(
                                        "%s download progress: %d%%  (%d / %d bytes)",
                                        label, pct, downloaded, total,
                                    )

            logger.info("%s download complete: %s (%d bytes)", label, dest.name, dest.stat().st_size)
            return dest

        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            logger.warning(
                "%s download attempt %d/%d failed: %s",
                label, attempt, MAX_RETRIES, exc,
            )
            if attempt == MAX_RETRIES:
                raise
            await asyncio.sleep(2 ** attempt)   # exponential back-off

    raise RuntimeError(f"{label} download failed after {MAX_RETRIES} attempts")


def merge_streams(video_path: Path, audio_path: Path, output_path: Path) -> Path:
    """
    Merge separate video and audio files into a single MP4 using ffmpeg-python.

    Uses ``-c copy`` (stream copy) so no re-encoding happens — it's fast.
    """
    logger.info("Merging %s + %s → %s", video_path.name, audio_path.name, output_path.name)

    video_input = ffmpeg.input(str(video_path))
    audio_input = ffmpeg.input(str(audio_path))

    (
        ffmpeg
        .output(
            video_input,
            audio_input,
            str(output_path),
            vcodec="copy",
            acodec="copy",
            # Ensure the output is a proper MP4 with moov atom at the start
            movflags="+faststart",
        )
        .overwrite_output()
        .run(quiet=True)
    )

    logger.info("Merge complete: %s (%d bytes)", output_path.name, output_path.stat().st_size)
    return output_path


def cleanup(*paths: Path) -> None:
    """Silently delete temporary files."""
    for p in paths:
        try:
            if p.exists():
                p.unlink()
                logger.debug("Deleted temp file: %s", p)
        except OSError:
            logger.debug("Could not delete: %s", p, exc_info=True)


async def download_and_merge(
    video_url: str,
    audio_url: str,
    filename_stem: str = "output",
) -> Path:
    """
    High-level helper: download both streams in parallel, merge, and return
    the path to the final MP4.  Caller is responsible for sending it and
    then calling ``cleanup()``.
    """
    work_dir = _ensure_download_dir()
    uid = uuid.uuid4().hex[:8]

    video_path = work_dir / f"{uid}_video.mp4"
    audio_path = work_dir / f"{uid}_audio.m4a"
    output_path = work_dir / f"{filename_stem}_{uid}.mp4"

    try:
        # Download both streams concurrently
        await asyncio.gather(
            download_stream(video_url, video_path, label="Video"),
            download_stream(audio_url, audio_path, label="Audio"),
        )

        # Merge (CPU-bound but very fast with -c copy)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, merge_streams, video_path, audio_path, output_path)

        return output_path

    finally:
        # Always clean up the intermediate files; keep the merged output
        cleanup(video_path, audio_path)
