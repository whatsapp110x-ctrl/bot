"""
Resolves a platform URL to a direct streamable URL.

Strategy:
  1. If the URL already points to a media file (mp4, mkv, …) — return it as-is.
  2. Otherwise use yt-dlp to extract a single streamable URL.
  3. If yt-dlp fails (unsupported site, network error, …) fall back to the
     original URL and let the proxy try it directly.
"""
from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stream-resolver")

_MIME: dict[str, str] = {
    "mp4":  "video/mp4",
    "mkv":  "video/x-matroska",
    "webm": "video/webm",
    "avi":  "video/x-msvideo",
    "mov":  "video/quicktime",
    "m4v":  "video/mp4",
    "ts":   "video/mp2t",
    "flv":  "video/x-flv",
    "mp3":  "audio/mpeg",
    "m4a":  "audio/mp4",
    "ogg":  "audio/ogg",
    "wav":  "audio/wav",
    "flac": "audio/flac",
    "aac":  "audio/aac",
    "m3u8": "application/x-mpegurl",
}

_DIRECT_RE = re.compile(
    r"^https?://.+\.(mp4|mkv|webm|avi|mov|m4v|ts|flv"
    r"|mp3|m4a|ogg|wav|flac|aac)(\?[^#]*)?$",
    re.IGNORECASE,
)


def _ext_from_url(url: str) -> str:
    path = url.split("?")[0].split("#")[0]
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1].lower()
    return "mp4"


def _resolve_sync(url: str) -> dict:
    """Blocking yt-dlp extraction — always called in a thread."""
    import yt_dlp

    ydl_opts: dict = {
        "quiet":        True,
        "no_warnings":  True,
        "skip_download": True,
        # Prefer a single combined mp4 so the result is one URL
        "format": (
            "best[ext=mp4][protocol=https]"
            "/best[ext=mp4]"
            "/best[protocol=https]"
            "/best"
        ),
        "noplaylist":    True,
        "socket_timeout": 30,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("yt-dlp returned empty info")

    title = info.get("title") or "stream"
    ext   = info.get("ext") or "mp4"

    # Primary: top-level url key
    direct_url: str = info.get("url") or ""

    # For merged/split formats yt-dlp puts each stream in requested_formats;
    # grab the video track's URL for single-stream playback.
    for fmt in info.get("requested_formats") or []:
        u = fmt.get("url") or ""
        if u and not fmt.get("manifest_url"):
            direct_url = u
            ext = fmt.get("ext") or ext
            break

    # Last resort: scan all formats for anything with a direct URL
    if not direct_url:
        for fmt in reversed(info.get("formats") or []):
            u = fmt.get("url") or ""
            if u:
                direct_url = u
                ext = fmt.get("ext") or ext
                break

    if not direct_url:
        raise ValueError("No direct stream URL found by yt-dlp")

    is_hls = (
        ext == "m3u8"
        or ".m3u8" in direct_url
        or (info.get("protocol") or "").startswith("m3u8")
    )
    if is_hls:
        ext = "m3u8"

    return {
        "url":       direct_url,
        "title":     title,
        "ext":       ext,
        "is_hls":    is_hls,
        "filesize":  info.get("filesize") or info.get("filesize_approx"),
        "thumbnail": info.get("thumbnail"),
        "mime":      _MIME.get(ext, "video/mp4"),
    }


async def resolve_stream_url(url: str) -> dict:
    """
    Async entry point.  Returns a dict with keys:
        url, title, ext, is_hls, filesize, thumbnail, mime
    """
    # Already a direct media file — skip yt-dlp
    if _DIRECT_RE.match(url):
        ext = _ext_from_url(url)
        return {
            "url":       url,
            "title":     url.rsplit("/", 1)[-1].split("?")[0] or "stream",
            "ext":       ext,
            "is_hls":    ext == "m3u8",
            "filesize":  None,
            "thumbnail": None,
            "mime":      _MIME.get(ext, "application/octet-stream"),
        }

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, partial(_resolve_sync, url))
    except Exception as exc:
        logger.warning("yt-dlp resolve failed for %s: %s — proxying URL directly", url, exc)
        ext = _ext_from_url(url)
        return {
            "url":       url,
            "title":     "stream",
            "ext":       ext,
            "is_hls":    "m3u8" in url,
            "filesize":  None,
            "thumbnail": None,
            "mime":      _MIME.get(ext, "video/mp4"),
        }
