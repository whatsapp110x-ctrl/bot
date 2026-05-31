"""
Instagram downloader — robust multi-strategy engine.

Priority chain:
  Posts / Reels / IGTV:
    1. yt-dlp with Instagram headers + app-id (best for public reels, no auth needed)
    2. instaloader (native client, works for public posts)
    3. yt-dlp without extra headers (last-resort)

  Stories:
    1. instaloader with session (if INSTAGRAM_SESSIONID is set)
    2. Instagram embed-page scraper (public stories, no auth)
    3. yt-dlp fallback

  Highlights (/s/<base64_id>):
    1. yt-dlp with Instagram headers
    2. instaloader shortcode fallback
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import tempfile
from pathlib import Path

import requests as _requests

from config import INSTAGRAM_COOKIE_FILE, GENERIC_COOKIE_FILE
from .base import BaseDownloader, DownloadError, DownloadCancelled

logger = logging.getLogger(__name__)

_MEDIA_EXTS = {".mp4", ".jpg", ".jpeg", ".png", ".webp", ".mov", ".m4v", ".mkv", ".webm"}

# Instagram mobile app ID — needed for story/reel API access without login
_IG_APP_ID = "936619743392459"

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def is_instagram_url(url: str) -> bool:
    return "instagram.com" in url.lower() or "instagr.am" in url.lower()


def _is_stories_url(url: str) -> bool:
    return bool(re.search(r"instagram\.com/stories/", url, re.I))


def _is_highlights_url(url: str) -> bool:
    return bool(re.search(r"instagram\.com/s/[A-Za-z0-9]", url, re.I))


def _extract_shortcode(url: str) -> str | None:
    m = re.search(r"instagram\.com/(?:p|reel|tv|stories/[^/]+)/([A-Za-z0-9_\-]+)", url)
    return m.group(1) if m else None


def _extract_story_parts(url: str) -> tuple[str, int] | None:
    m = re.search(r"instagram\.com/stories/([^/?#]+)/(\d+)", url)
    if m:
        return m.group(1), int(m.group(2))
    return None


def _build_ytdlp_opts(dest_dir: Path, cookie_file: str | None = None, extra_headers: dict | None = None) -> dict:
    """Build yt-dlp options optimized for Instagram."""
    opts: dict = {
        "outtmpl": str(dest_dir / "%(title).80s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "retries": 8,
        "fragment_retries": 8,
        "merge_output_format": "mp4",
        "geo_bypass": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10 * 1024 * 1024,
    }

    headers = {
        "User-Agent": _MOBILE_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "x-ig-app-id": _IG_APP_ID,
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
    }
    if extra_headers:
        headers.update(extra_headers)

    opts["http_headers"] = headers

    if cookie_file and os.path.isfile(str(cookie_file)):
        opts["cookiefile"] = str(cookie_file)

    return opts


async def _ytdlp_download(url: str, dest_dir: Path, cookie_file: str | None = None) -> list[Path]:
    """Download via yt-dlp with Instagram-specific headers."""
    import yt_dlp

    opts = _build_ytdlp_opts(dest_dir, cookie_file=cookie_file)
    loop = asyncio.get_running_loop()

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise DownloadError("yt-dlp returned no info for this URL")

        files = [
            f for f in dest_dir.rglob("*")
            if f.is_file()
            and f.suffix.lower() not in (".part", ".ytdl", ".aria2")
            and f.suffix.lower() in _MEDIA_EXTS
            and f.stat().st_size > 20_000
        ]
        return sorted(files, key=lambda f: f.stat().st_size, reverse=True)

    try:
        return await loop.run_in_executor(None, _run)
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"yt-dlp: {exc}") from exc


async def _ytdlp_download_with_retry(url: str, dest_dir: Path) -> list[Path]:
    """Try yt-dlp with Instagram cookies, then without."""
    cookie_file = (
        os.getenv("INSTAGRAM_COOKIE_FILE")
        or INSTAGRAM_COOKIE_FILE
        or GENERIC_COOKIE_FILE
        or None
    )

    last_err: Exception | None = None

    # Attempt 1: with cookies (if available)
    try:
        files = await _ytdlp_download(url, dest_dir, cookie_file=cookie_file)
        if files:
            return files
    except Exception as exc:
        last_err = exc
        logger.debug("yt-dlp attempt 1 failed: %s", exc)

    # Small delay before retry (Instagram rate-limits aggressively)
    await asyncio.sleep(2)

    # Attempt 2: without cookies, different user-agent
    try:
        import yt_dlp
        opts = _build_ytdlp_opts(dest_dir, extra_headers={"User-Agent": _DESKTOP_UA})
        opts.pop("cookiefile", None)
        loop = asyncio.get_running_loop()

        def _run2():
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            return [
                f for f in dest_dir.rglob("*")
                if f.is_file()
                and f.suffix.lower() not in (".part", ".ytdl", ".aria2")
                and f.suffix.lower() in _MEDIA_EXTS
                and f.stat().st_size > 20_000
            ]

        files = await loop.run_in_executor(None, _run2)
        if files:
            return files
    except Exception as exc:
        last_err = exc
        logger.debug("yt-dlp attempt 2 failed: %s", exc)

    raise DownloadError(f"yt-dlp failed: {last_err}")


async def _instaloader_post(url: str, dest_dir: Path) -> list[Path]:
    """Download a public post/reel via instaloader."""
    import instaloader

    shortcode = _extract_shortcode(url)
    if not shortcode:
        raise DownloadError(f"Cannot extract Instagram shortcode from: {url}")

    loop = asyncio.get_running_loop()

    def _run():
        L = instaloader.Instaloader(
            download_videos=True,
            download_video_thumbnails=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            filename_pattern="{shortcode}",
            dirname_pattern=str(dest_dir),
            quiet=True,
        )

        session_file = os.getenv("INSTAGRAM_SESSIONID")
        if session_file and os.path.isfile(session_file):
            try:
                L.load_session_from_file(session_file)
            except Exception:
                pass

        try:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            L.download_post(post, target=dest_dir)
        except instaloader.exceptions.LoginRequiredException:
            raise DownloadError("login_required")
        except instaloader.exceptions.PrivateProfileNotFollowedException:
            raise DownloadError("private_profile")
        except Exception as exc:
            raise DownloadError(f"instaloader: {exc}")

        return [
            f for f in dest_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in _MEDIA_EXTS and f.stat().st_size > 5_000
        ]

    return await loop.run_in_executor(None, _run)


async def _instaloader_story(url: str, dest_dir: Path) -> list[Path]:
    """Download a specific story item via instaloader (requires INSTAGRAM_SESSIONID)."""
    import instaloader

    parts = _extract_story_parts(url)
    if not parts:
        raise DownloadError(f"Cannot parse story URL: {url}")
    username, media_id = parts

    session_file = os.getenv("INSTAGRAM_SESSIONID")
    if not session_file or not os.path.isfile(session_file):
        raise DownloadError("no_session")

    loop = asyncio.get_running_loop()

    def _run():
        L = instaloader.Instaloader(
            download_videos=True,
            download_video_thumbnails=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            filename_pattern="{owner_username}_{mediaid}",
            dirname_pattern=str(dest_dir),
            quiet=True,
        )
        try:
            L.load_session_from_file(session_file)
        except Exception as exc:
            raise DownloadError(f"Could not load session: {exc}")

        try:
            profile = instaloader.Profile.from_username(L.context, username)
            found = False
            for story in L.get_stories(userids=[profile.userid]):
                for item in story.get_items():
                    if item.mediaid == media_id:
                        L.download_storyitem(item, target=dest_dir)
                        found = True
                        break
                if found:
                    break

            if not found:
                raise DownloadError("story_not_found")
        except DownloadError:
            raise
        except instaloader.exceptions.LoginRequiredException:
            raise DownloadError("login_required")
        except Exception as exc:
            raise DownloadError(f"instaloader: {exc}")

        return [
            f for f in dest_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in _MEDIA_EXTS
        ]

    return await loop.run_in_executor(None, _run)


async def _scrape_ig_page_video(url: str, dest_dir: Path) -> list[Path]:
    """
    Extract CDN video/image URLs directly from Instagram's page HTML.

    Instagram embeds media metadata as JSON inside <script> tags with
    forward-slashes escaped as \/ — e.g.:
        "video_versions":[{"type":101,"url":"https:\\/\\/scontent...mp4..."}]

    This works for:
      - Highlight share links  (/s/<base64>)
      - Story pages            (/stories/<user>/<id>/)
      - Post/reel pages        (/p/<shortcode>/)

    Returns an empty list (instead of raising) on 429 so callers can try
    the next strategy.
    """
    loop = asyncio.get_running_loop()

    def _fetch_html() -> str:
        resp = _requests.get(
            url,
            headers={
                "User-Agent": _MOBILE_UA,
                "Accept": "text/html,*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "x-ig-app-id": _IG_APP_ID,
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
            timeout=20,
            allow_redirects=True,
        )
        if resp.status_code == 429:
            raise DownloadError("rate_limited_429")
        if resp.status_code not in (200, 301, 302):
            raise DownloadError(f"http_{resp.status_code}")
        return resp.text

    try:
        html = await loop.run_in_executor(None, _fetch_html)
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"page_fetch: {exc}") from exc

    # ── Extract all video URLs from escaped JSON ───────────────────────
    # The JSON uses \/ for / and \u0026 for &
    media_urls: list[tuple[str, str]] = []   # (url, ext)

    # 1. video_versions → best quality video per media item (first entry is highest res)
    for m in re.finditer(
        r'"video_versions"\s*:\s*\[\s*\{[^}]*"url"\s*:\s*"(https:\\/\\/[^"]+\.mp4[^"]*)"',
        html,
    ):
        clean = _unescape_ig_url(m.group(1))
        if (clean, ".mp4") not in media_urls:
            media_urls.append((clean, ".mp4"))

    # 2. image_versions2 → best quality image (first candidate)
    if not media_urls:
        for m in re.finditer(
            r'"image_versions2"\s*:\s*\{"candidates"\s*:\s*\[\s*\{'
            r'[^}]*"url"\s*:\s*"(https:\\/\\/[^"]+\.jpg[^"]*)"',
            html,
        ):
            clean = _unescape_ig_url(m.group(1))
            if (clean, ".jpg") not in media_urls:
                media_urls.append((clean, ".jpg"))
                break   # only first (highest res) image candidate

    # 3. Brute-force fallback — any CDN mp4/jpg with escaped slashes
    if not media_urls:
        for m in re.finditer(r"https:\\/\\/(?:scontent|video)[^\"\\s]+\.mp4[^\"\\s]*", html):
            clean = _unescape_ig_url(m.group(0))
            media_urls.append((clean, ".mp4"))
            if len(media_urls) >= 3:
                break
    if not media_urls:
        for m in re.finditer(r"https:\\/\\/scontent[^\"\\s]+\.jpg[^\"\\s]*", html):
            clean = _unescape_ig_url(m.group(0))
            media_urls.append((clean, ".jpg"))
            if len(media_urls) >= 2:
                break

    if not media_urls:
        raise DownloadError("No media URLs found in Instagram page HTML")

    # ── Download all extracted URLs ────────────────────────────────────
    def _download_all() -> list[Path]:
        files: list[Path] = []
        sess = _requests.Session()
        sess.headers.update({
            "User-Agent": _MOBILE_UA,
            "Referer": "https://www.instagram.com/",
            "Accept": "*/*",
        })
        for i, (media_url, ext) in enumerate(media_urls[:5]):
            dest = dest_dir / f"ig_page_{i}{ext}"
            try:
                r = sess.get(media_url, stream=True, timeout=120)
                r.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                        fh.write(chunk)
                if dest.stat().st_size > 10_000:
                    files.append(dest)
                else:
                    dest.unlink(missing_ok=True)
            except Exception as exc:
                logger.debug("CDN download failed for %s: %s", media_url[:80], exc)
        return files

    return await loop.run_in_executor(None, _download_all)


def _unescape_ig_url(raw: str) -> str:
    """Unescape Instagram's JSON-encoded CDN URLs."""
    return (
        raw
        .replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u003D", "=")
        .replace("\\u003d", "=")
    )


async def _scrape_story_embed(url: str, dest_dir: Path) -> list[Path]:
    """
    Scrape the Instagram embed page for a story item.
    Falls back to og:video / og:image meta tags.
    NOTE: Instagram now returns 404 for most story embed URLs.
    This is kept as a lightweight fallback before the heavier page scraper.
    """
    parts = _extract_story_parts(url)
    if not parts:
        raise DownloadError(f"Cannot parse story URL: {url}")
    username, media_id = parts

    embed_url = f"https://www.instagram.com/stories/{username}/{media_id}/embed/captioned/"
    loop = asyncio.get_running_loop()

    def _fetch_embed() -> str:
        resp = _requests.get(
            embed_url,
            headers={
                "User-Agent": _DESKTOP_UA,
                "Accept": "text/html,*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "x-ig-app-id": _IG_APP_ID,
            },
            timeout=20,
            allow_redirects=True,
        )
        if resp.status_code not in (200,):
            raise DownloadError(f"embed_http_{resp.status_code}")
        return resp.text

    try:
        html = await loop.run_in_executor(None, _fetch_embed)
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"embed_fetch: {exc}") from exc

    media_url: str | None = None
    patterns = [
        r'"video_url"\s*:\s*"(https://[^"]+)"',
        r'"contentUrl"\s*:\s*"(https://[^"]+\.mp4[^"]*)"',
        r'<source[^>]+src="(https://[^"]+)"[^>]+type="video',
        r'property="og:video"\s+content="(https://[^"]+)"',
        r'property="og:image"\s+content="(https://[^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            media_url = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
            break

    if not media_url:
        raise DownloadError("No media URL in embed page")

    ext = ".mp4" if ".mp4" in media_url.lower() else ".jpg"
    dest = dest_dir / f"story_embed{ext}"

    def _dl():
        r = _requests.get(
            media_url,
            headers={"User-Agent": _MOBILE_UA, "Referer": "https://www.instagram.com/"},
            stream=True, timeout=120,
        )
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                fh.write(chunk)
        if dest.stat().st_size < 10_000:
            dest.unlink(missing_ok=True)
            raise DownloadError("Story media too small — likely error page")
        return [dest]

    return await loop.run_in_executor(None, _dl)


# ---------------------------------------------------------------------------
# Profile / batch helpers (new — from insta-dl + InstagramPro-Toolkit)
# ---------------------------------------------------------------------------

async def download_profile_posts(
    username: str,
    dest_dir: Path,
    count: int = 5,
    only_reels: bool = False,
    only_images: bool = False,
) -> list[Path]:
    """
    Download recent posts/reels from an Instagram profile.
    Uses instaloader — works for public profiles without auth.
    Set INSTAGRAM_SESSIONID for private profiles you follow.
    """
    import instaloader

    loop = asyncio.get_running_loop()

    def _run() -> list[Path]:
        L = instaloader.Instaloader(
            download_videos=not only_images,
            download_video_thumbnails=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            filename_pattern="{shortcode}",
            dirname_pattern=str(dest_dir),
            quiet=True,
        )

        session_file = os.getenv("INSTAGRAM_SESSIONID")
        if session_file and os.path.isfile(session_file):
            try:
                L.load_session_from_file(session_file)
            except Exception:
                pass

        try:
            profile = instaloader.Profile.from_username(L.context, username.lstrip("@"))
        except instaloader.exceptions.ProfileNotExistsException:
            raise DownloadError(f"Instagram profile @{username} not found.")
        except instaloader.exceptions.LoginRequiredException:
            raise DownloadError(
                f"@{username} is a private profile.\n"
                "The bot owner must set INSTAGRAM_SESSIONID and follow this account."
            )
        except Exception as exc:
            raise DownloadError(f"Could not load profile @{username}: {exc}")

        downloaded = 0
        errors = 0
        for post in profile.get_posts():
            if downloaded >= count:
                break
            # Filter by type
            is_video = post.is_video
            is_sidecar = post.typename == "GraphSidecar"
            if only_reels and not is_video:
                continue
            if only_images and is_video and not is_sidecar:
                continue
            try:
                L.download_post(post, target=dest_dir)
                downloaded += 1
            except instaloader.exceptions.LoginRequiredException:
                raise DownloadError(
                    f"@{username} is private — INSTAGRAM_SESSIONID required."
                )
            except Exception:
                errors += 1
                if errors > 3:
                    break

        files = [
            f for f in dest_dir.rglob("*")
            if f.is_file()
            and f.suffix.lower() in _MEDIA_EXTS
            and f.stat().st_size > 5_000
        ]
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    return await loop.run_in_executor(None, _run)


async def download_profile_stories(username: str, dest_dir: Path) -> list[Path]:
    """
    Download all current stories from a profile.
    Requires INSTAGRAM_SESSIONID — stories are always private API.
    """
    import instaloader

    session_file = os.getenv("INSTAGRAM_SESSIONID")
    if not session_file or not os.path.isfile(session_file):
        raise DownloadError(
            "⚠️ Stories require authentication.\n"
            "The bot owner must set the INSTAGRAM_SESSIONID secret."
        )

    loop = asyncio.get_running_loop()

    def _run() -> list[Path]:
        L = instaloader.Instaloader(
            download_videos=True,
            download_video_thumbnails=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            filename_pattern="{owner_username}_{mediaid}",
            dirname_pattern=str(dest_dir),
            quiet=True,
        )
        try:
            L.load_session_from_file(session_file)
        except Exception as exc:
            raise DownloadError(f"Session load failed: {exc}")

        try:
            profile = instaloader.Profile.from_username(L.context, username.lstrip("@"))
        except instaloader.exceptions.ProfileNotExistsException:
            raise DownloadError(f"Profile @{username} not found.")
        except Exception as exc:
            raise DownloadError(f"Could not load profile: {exc}")

        try:
            stories = list(L.get_stories(userids=[profile.userid]))
        except instaloader.exceptions.LoginRequiredException:
            raise DownloadError("Session expired — bot owner must refresh INSTAGRAM_SESSIONID.")
        except Exception as exc:
            raise DownloadError(f"Could not fetch stories: {exc}")

        for story in stories:
            for item in story.get_items():
                try:
                    L.download_storyitem(item, target=dest_dir)
                except Exception:
                    pass

        files = [
            f for f in dest_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in _MEDIA_EXTS
        ]
        return files

    return await loop.run_in_executor(None, _run)


class InstagramDownloader(BaseDownloader):
    ENGINE_NAME = "instagram"

    async def download(self) -> list[Path]:
        self._log("Instagram download: %s", self.url)
        tempdir = self._make_tempdir()

        is_story = _is_stories_url(self.url)
        is_highlight = _is_highlights_url(self.url)

        # ── Stories (/stories/<user>/<id>/) ─────────────────────────────────
        if is_story:
            # 1. instaloader with session (most reliable when auth is available)
            try:
                files = await _instaloader_story(self.url, tempdir)
                if files:
                    self._log("instaloader story: %d file(s)", len(files))
                    return files
            except DownloadError as exc:
                err_str = str(exc)
                if err_str == "no_session":
                    self._log("No INSTAGRAM_SESSIONID — skipping instaloader story")
                elif err_str == "story_not_found":
                    raise DownloadError(
                        "⚠️ Story not found.\n"
                        "Stories expire after 24 hours — this one may be gone.\n"
                        "Private stories require the bot owner to set INSTAGRAM_SESSIONID."
                    )
                else:
                    self._log("instaloader story failed: %s", exc)
            except Exception as exc:
                self._log("instaloader story exception: %s", exc)

            # 2. Direct page scraper — extracts video from Instagram's own HTML
            #    (works when Instagram returns 200 for the story page)
            try:
                files = await _scrape_ig_page_video(self.url, tempdir)
                if files:
                    self._log("page scraper story: %d file(s)", len(files))
                    return files
            except DownloadError as exc:
                if "rate_limited_429" in str(exc):
                    self._log("story page 429 — Instagram blocking server IP")
                else:
                    self._log("page scraper story failed: %s", exc)
            except Exception as exc:
                self._log("page scraper story exception: %s", exc)

            # 3. Embed-page scraper (legacy — Instagram now mostly returns 404)
            try:
                files = await _scrape_story_embed(self.url, tempdir)
                if files:
                    self._log("embed scraper story: %d file(s)", len(files))
                    return files
            except Exception as exc:
                self._log("embed story failed: %s", exc)

            # 4. yt-dlp with cookie file (works if INSTAGRAM_COOKIE_FILE is set)
            try:
                files = await _ytdlp_download_with_retry(self.url, tempdir)
                if files:
                    self._log("yt-dlp story: %d file(s)", len(files))
                    return files
            except Exception as exc:
                self._log("yt-dlp story failed: %s", exc)

            raise DownloadError(
                "⚠️ Could not download this Instagram story.\n\n"
                "**Why this happens:**\n"
                "Instagram blocks server IPs from accessing story content without login.\n\n"
                "**Solutions:**\n"
                "① Bot owner sets `INSTAGRAM_SESSIONID` secret\n"
                "  (export your session from browser → provide the cookie file)\n"
                "② Story may have already expired (stories last only 24h)\n"
                "③ Try again — Instagram rate-limits lift after a few minutes"
            )

        # ── Highlights (/s/<base64_id> share links) ──────────────────────────
        if is_highlight:
            # 1. Page scraper — confirmed working for /s/ highlight share URLs
            #    Instagram embeds video_versions JSON (with \/ escaping) in the page HTML
            try:
                files = await _scrape_ig_page_video(self.url, tempdir)
                if files:
                    self._log("page scraper highlight: %d file(s)", len(files))
                    return files
            except DownloadError as exc:
                if "rate_limited_429" in str(exc):
                    self._log("highlight page 429")
                else:
                    self._log("page scraper highlight failed: %s", exc)
            except Exception as exc:
                self._log("page scraper highlight exception: %s", exc)

            # 2. yt-dlp fallback (rarely works for /s/ URLs but worth trying)
            try:
                files = await _ytdlp_download_with_retry(self.url, tempdir)
                if files:
                    self._log("yt-dlp highlight: %d file(s)", len(files))
                    return files
            except Exception as exc:
                self._log("yt-dlp highlight failed: %s", exc)

            raise DownloadError(
                "⚠️ Could not download this Instagram highlight.\n\n"
                "**Possible reasons:**\n"
                "• Highlight is from a private account\n"
                "• Instagram is temporarily rate-limiting this server\n\n"
                "**Solution:** Try again in a few minutes, or ask the bot owner\n"
                "to set `INSTAGRAM_SESSIONID` for authenticated access."
            )

        # ── Posts / Reels / IGTV ─────────────────────────────────────────────
        # 1. yt-dlp first (most reliable for public reels without auth)
        try:
            files = await _ytdlp_download_with_retry(self.url, tempdir)
            if files:
                self._log("yt-dlp reel/post: %d file(s)", len(files))
                return files
        except DownloadError as exc:
            self._log("yt-dlp primary failed: %s", exc)
        except Exception as exc:
            self._log("yt-dlp primary exception: %s", exc)

        # 2. instaloader fallback (good for posts, less reliable for reels)
        try:
            files = await _instaloader_post(self.url, tempdir)
            if files:
                self._log("instaloader post: %d file(s)", len(files))
                return files
        except DownloadError as exc:
            err_str = str(exc)
            if err_str == "login_required":
                raise DownloadError(
                    "⚠️ This Instagram content requires login.\n"
                    "The bot owner must set INSTAGRAM_SESSIONID for private content."
                )
            if err_str == "private_profile":
                raise DownloadError(
                    "⚠️ This is a private Instagram account.\n"
                    "The bot owner must set INSTAGRAM_SESSIONID and follow that account."
                )
            self._log("instaloader failed: %s", exc)
        except Exception as exc:
            self._log("instaloader exception: %s", exc)

        raise DownloadError(
            "⚠️ Could not download this Instagram content.\n\n"
            "Possible reasons:\n"
            "• Instagram is rate-limiting server requests (try again in a few minutes)\n"
            "• The post/reel has been deleted\n"
            "• The account is private\n\n"
            "Tip: Public reels usually work — try again shortly."
        )
