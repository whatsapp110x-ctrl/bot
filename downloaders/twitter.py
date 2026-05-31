"""
Twitter / X downloader — dedicated engine.

Handles:
  - Tweets with embedded video/GIF/photo
  - Twitter Spaces (live & recorded audio)
  - Thread media (with /thread flag)

Strategy:
  1. yt-dlp with Twitter-optimised headers + cookie file
  2. yt-dlp without cookies (public content)
  3. Direct CDN extraction via Twitter's API v2 guest token (no key needed)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import requests as _requests

from config import GENERIC_COOKIE_FILE, TWITTER_COOKIE_FILE
from .base import BaseDownloader, DownloadError, DownloadCancelled

logger = logging.getLogger(__name__)

_MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".m4v", ".mov", ".mp3", ".m4a", ".aac", ".jpg", ".jpeg", ".png", ".gif"}

_TWITTER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

TWITTER_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com|t\.co)/",
    re.IGNORECASE,
)

SPACES_RE = re.compile(
    r"(?:twitter|x)\.com/i/spaces/([A-Za-z0-9]+)",
    re.IGNORECASE,
)


def is_twitter_url(url: str) -> bool:
    return bool(TWITTER_RE.search(url))


def _is_spaces_url(url: str) -> bool:
    return bool(SPACES_RE.search(url))


def _extract_tweet_id(url: str) -> str | None:
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else None


async def _ytdlp_twitter(url: str, dest_dir: Path, audio_only: bool = False) -> list[Path]:
    """yt-dlp with Twitter-specific headers."""
    import yt_dlp

    cookie_file = TWITTER_COOKIE_FILE or GENERIC_COOKIE_FILE or None

    if _is_spaces_url(url):
        # Twitter Spaces — audio stream
        fmt = "bestaudio/best"
        pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}]
    elif audio_only:
        fmt = "bestaudio/best"
        pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
    else:
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        pp = []

    opts: dict = {
        "outtmpl": str(dest_dir / "%(title).80s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "retries": 8,
        "fragment_retries": 8,
        "merge_output_format": "mp4",
        "format": fmt,
        "postprocessors": pp,
        "noplaylist": True,
        "geo_bypass": True,
        "http_headers": {
            "User-Agent": _TWITTER_UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://twitter.com/",
            "Origin": "https://twitter.com",
        },
    }

    if cookie_file and os.path.isfile(str(cookie_file)):
        opts["cookiefile"] = str(cookie_file)

    loop = asyncio.get_running_loop()

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise DownloadError("yt-dlp returned no info for this Twitter URL")
        return [
            f for f in dest_dir.rglob("*")
            if f.is_file()
            and f.suffix.lower() not in (".part", ".ytdl", ".aria2")
            and f.stat().st_size > 5_000
        ]

    try:
        return await loop.run_in_executor(None, _run)
    except Exception as exc:
        raise DownloadError(f"yt-dlp Twitter: {exc}") from exc


async def _guest_token_download(url: str, dest_dir: Path) -> list[Path]:
    """
    Extract video from a tweet using Twitter's guest-token API.
    No API key required — works like a browser.
    """
    tweet_id = _extract_tweet_id(url)
    if not tweet_id:
        raise DownloadError("Could not extract tweet ID")

    loop = asyncio.get_running_loop()

    def _fetch() -> list[str]:
        session = _requests.Session()
        session.headers.update({
            "User-Agent": _TWITTER_UA,
            "Accept": "*/*",
            "Authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
        })

        # Get guest token
        r = session.post("https://api.twitter.com/1.1/guest/activate.json", timeout=10)
        r.raise_for_status()
        guest_token = r.json().get("guest_token", "")
        if not guest_token:
            raise DownloadError("Could not get Twitter guest token")

        session.headers["x-guest-token"] = guest_token

        # Fetch tweet
        params = {
            "variables": json.dumps({
                "tweetId": tweet_id,
                "withCommunity": False,
                "includePromotedContent": False,
                "withVoice": False,
            }),
            "features": json.dumps({
                "creator_subscriptions_tweet_preview_api_enabled": True,
                "tweetypie_unmention_optimization_enabled": True,
                "responsive_web_edit_tweet_api_enabled": True,
                "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
                "view_counts_everywhere_api_enabled": True,
                "longform_notetweets_consumption_enabled": True,
                "responsive_web_twitter_article_tweet_consumption_enabled": False,
                "tweet_awards_web_tipping_enabled": False,
                "longform_notetweets_rich_text_read_enabled": True,
                "longform_notetweets_inline_media_enabled": True,
                "rweb_video_timestamps_enabled": True,
                "responsive_web_graphql_exclude_directive_enabled": True,
                "verified_phone_label_enabled": False,
                "freedom_of_speech_not_reach_fetch_enabled": True,
                "standardized_nudges_misinfo": True,
                "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
                "responsive_web_graphql_timeline_navigation_enabled": True,
                "interactive_text_enabled": True,
                "responsive_web_text_conversations_enabled": False,
                "responsive_web_enhance_cards_enabled": False,
            }),
        }

        r2 = session.get(
            "https://twitter.com/i/api/graphql/5GOHgZe-8IwrWljed3dBnQ/TweetResultByRestId",
            params=params,
            timeout=15,
        )
        r2.raise_for_status()
        data = r2.json()

        # Navigate the JSON to find video URLs
        media_urls: list[str] = []
        try:
            tweet_result = (
                data["data"]["tweetResult"]["result"]
                .get("tweet", data["data"]["tweetResult"]["result"])
            )
            media_items = (
                tweet_result["legacy"]["extended_entities"]["media"]
            )
            for item in media_items:
                if item.get("type") == "video":
                    variants = item["video_info"]["variants"]
                    # Best bitrate
                    best = max(
                        [v for v in variants if v.get("content_type") == "video/mp4"],
                        key=lambda v: v.get("bitrate", 0),
                        default=None,
                    )
                    if best:
                        media_urls.append(best["url"])
                elif item.get("type") == "photo":
                    orig = item["media_url_https"] + "?name=orig"
                    media_urls.append(orig)
                elif item.get("type") == "animated_gif":
                    variants = item["video_info"]["variants"]
                    if variants:
                        media_urls.append(variants[0]["url"])
        except (KeyError, TypeError):
            pass

        return media_urls

    media_urls = await loop.run_in_executor(None, _fetch)
    if not media_urls:
        raise DownloadError("No media found in tweet via guest-token API")

    # Download each media URL
    def _dl_all() -> list[Path]:
        files: list[Path] = []
        sess = _requests.Session()
        sess.headers["User-Agent"] = _TWITTER_UA
        for i, media_url in enumerate(media_urls):
            ext = ".mp4" if ".mp4" in media_url.lower() else ".jpg"
            dest = dest_dir / f"tweet_{tweet_id}_{i}{ext}"
            r = sess.get(media_url, stream=True, timeout=120)
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                    fh.write(chunk)
            if dest.stat().st_size > 5_000:
                files.append(dest)
        return files

    return await loop.run_in_executor(None, _dl_all)


class TwitterDownloader(BaseDownloader):
    ENGINE_NAME = "twitter"

    def __init__(self, url: str, audio_only: bool = False, **kwargs) -> None:
        super().__init__(url, **kwargs)
        self.audio_only = audio_only

    async def download(self) -> list[Path]:
        self._log("Twitter/X download: %s", self.url)
        tempdir = self._make_tempdir()

        is_spaces = _is_spaces_url(self.url)

        if is_spaces:
            self._log("Twitter Spaces detected — extracting audio")
            try:
                files = await _ytdlp_twitter(self.url, tempdir, audio_only=True)
                if files:
                    return files
            except DownloadError as exc:
                raise DownloadError(
                    f"⚠️ Twitter Spaces download failed.\n\n"
                    f"• Live spaces cannot be recorded — only ended/recorded spaces work\n"
                    f"• Error: {exc}"
                )

        # 1. yt-dlp with Twitter headers (primary — most reliable)
        try:
            files = await _ytdlp_twitter(self.url, tempdir, audio_only=self.audio_only)
            if files:
                self._log("yt-dlp Twitter: %d file(s)", len(files))
                return files
        except DownloadError as exc:
            self._log("yt-dlp Twitter primary failed: %s", exc)

        # 2. Guest-token API (fallback — works for most public tweets)
        try:
            files = await _guest_token_download(self.url, tempdir)
            if files:
                self._log("guest-token API: %d file(s)", len(files))
                return files
        except DownloadError as exc:
            self._log("guest-token fallback failed: %s", exc)
        except Exception as exc:
            self._log("guest-token exception: %s", exc)

        raise DownloadError(
            "⚠️ Could not download this Twitter/X content.\n\n"
            "Possible reasons:\n"
            "• Tweet is from a private/protected account\n"
            "• Tweet has been deleted\n"
            "• Twitter is blocking server requests\n\n"
            "Tip: Public tweets with video usually work — try again shortly."
        )
