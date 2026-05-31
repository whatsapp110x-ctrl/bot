"""
yt-dlp downloader engine — the heart of the universal bot.

Covers YouTube, Instagram, TikTok, Twitter/X, Facebook, Reddit,
Twitch, Dailymotion, Vimeo, Bilibili, Rumble, Odysee, SoundCloud,
Bandcamp, Mixcloud, Pinterest, Snapchat, LinkedIn, and 1800+ more.

Key improvements vs all source repos:
- Runs fully async (executor, never blocking the event loop)
- Live progress via hook → asyncio queue (thread-safe)
- Playlist support (downloads all videos in a playlist)
- Best format selection without any quality cap by default
- Cookie injection per-platform from file (never hardcoded)
- Auto-retry with exponential backoff
- Fragment download support (HLS/DASH streams, M3U8)
- Age-gated content support via cookies
- Geo-restriction bypass via yt-dlp's built-in methods
- SponsorBlock integration (optional)
- Subtitle download and embedding
"""

import asyncio
import logging
import os
import queue as stdlib_queue
import re
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import yt_dlp

from config import (
    DEFAULT_VIDEO_QUALITY,
    DEFAULT_AUDIO_FORMAT,
    MAX_DOWNLOAD_SIZE,
    INSTAGRAM_COOKIE_FILE,
    TIKTOK_COOKIE_FILE,
    YOUTUBE_COOKIE_FILE,
    FACEBOOK_COOKIE_FILE,
    TWITTER_COOKIE_FILE,
    GENERIC_COOKIE_FILE,
    ALL_COOKIES_FILE,
)
from .base import BaseDownloader, DownloadError, DownloadCancelled, ProgressCallback

logger = logging.getLogger(__name__)

# Node.js path — yt-dlp needs it as JS runtime for YouTube extraction
_NODEJS = shutil.which("node") or shutil.which("nodejs")

# YouTube OAuth2 token file path (set by /ytlogin command)
_YOUTUBE_OAUTH_FILE = Path(__file__).parent.parent / "data" / "yt-oauth2-token.json"

# Platforms yt-dlp handles natively
YTDL_PATTERNS = re.compile(
    # Use (?<![a-z0-9-]) to prevent false domain-suffix matches (e.g. x\.com in blogx.com)
    r"(?<![a-z0-9-])("
    r"youtube\.com|youtu\.be|instagram\.com|tiktok\.com|twitter\.com"
    r"|(?<![a-z])x\.com"          # twitter/X — but NOT blogx.com, bax.com etc.
    r"|reddit\.com|facebook\.com|fb\.watch|dailymotion\.com|twitch\.tv"
    r"|vimeo\.com|bilibili\.com|nicovideo\.jp|rumble\.com|odysee\.com"
    r"|soundcloud\.com|bandcamp\.com|mixcloud\.com|pinterest\.com"
    r"|snapchat\.com|linkedin\.com|vm\.tiktok\.com|m\.youtube\.com"
    r"|clips\.twitch\.tv|open\.spotify\.com|music\.youtube\.com"
    r"|youtu\.be|yt\.be|insta\.gr|bit\.ly"
    r")",
    re.IGNORECASE,
)


def is_ytdl_url(url: str) -> bool:
    return bool(YTDL_PATTERNS.search(url))


# ---------------------------------------------------------------------------
# Facebook URL normalisation
# ---------------------------------------------------------------------------

_FB_PROFILE_RE = re.compile(
    r"facebook\.com/"
    r"(?:profile\.php\?id=\d+/?$"          # /profile.php?id=123
    r"|[A-Za-z0-9._%-]+"                   # /BeingSalmanKhan
    r"(?:/?|/\?[^/]*)?$"                   # optional trailing / or ?params
    r")",
    re.IGNORECASE,
)

_FB_VIDEO_RE = re.compile(
    r"facebook\.com/"
    r"(?:reel/|watch/?\?|videos/|fb\.watch/|story\.php|permalink)",
    re.IGNORECASE,
)


def _normalize_fb_url(url: str) -> str:
    """
    Decode m.facebook.com/?next=<encoded_url> login-redirect wrappers.
    Returns the real destination URL (or the original if not a redirect).

    Raises DownloadError early when the decoded URL is obviously a
    non-video page (profile page, homepage, etc.).
    """
    try:
        parsed = urlparse(url)
        if parsed.hostname and "facebook.com" in parsed.hostname:
            qs = parse_qs(parsed.query)
            next_val = qs.get("next", [""])[0]
            if next_val:
                real_url = unquote(next_val).rstrip("#").strip()
                # If the real URL is a profile/page with no video path → tell user
                if _FB_VIDEO_RE.search(real_url):
                    return real_url  # looks like a video link → proceed
                if _FB_PROFILE_RE.search(real_url):
                    raise DownloadError(
                        "That Facebook link points to a **profile page**, not a video.\n\n"
                        "Please share the direct link to the video or reel instead — for example:\n"
                        "• `https://www.facebook.com/reel/<id>`\n"
                        "• `https://www.facebook.com/watch?v=<id>`\n"
                        "• `https://www.facebook.com/<page>/videos/<id>`"
                    )
                return real_url  # unknown — let yt-dlp try it
    except DownloadError:
        raise
    except Exception:
        pass
    return url


def _get_cookie_file(url: str) -> str | None:
    """Select the right cookie file based on the URL domain."""
    url_l = url.lower()
    if "youtube" in url_l or "youtu.be" in url_l:
        return YOUTUBE_COOKIE_FILE or ALL_COOKIES_FILE or GENERIC_COOKIE_FILE
    if "instagram" in url_l:
        return INSTAGRAM_COOKIE_FILE or ALL_COOKIES_FILE or GENERIC_COOKIE_FILE
    if "tiktok" in url_l:
        return TIKTOK_COOKIE_FILE or ALL_COOKIES_FILE or GENERIC_COOKIE_FILE
    if "facebook" in url_l or "fb.watch" in url_l:
        return FACEBOOK_COOKIE_FILE or ALL_COOKIES_FILE or GENERIC_COOKIE_FILE
    if "twitter" in url_l or "x.com" in url_l:
        return TWITTER_COOKIE_FILE or ALL_COOKIES_FILE or GENERIC_COOKIE_FILE
    return ALL_COOKIES_FILE or GENERIC_COOKIE_FILE


class YtdlDownloader(BaseDownloader):
    ENGINE_NAME = "yt-dlp"

    def __init__(
        self,
        url: str,
        quality: str = DEFAULT_VIDEO_QUALITY,
        output_format: str = "video",      # 'video' | 'audio' | 'document'
        audio_format: str = DEFAULT_AUDIO_FORMAT,
        subtitles: bool = False,
        playlist: bool = False,            # download full playlist
        sponsorblock: bool = False,        # remove sponsor segments
        **kwargs,
    ) -> None:
        super().__init__(url, **kwargs)
        self.quality = quality
        self.output_format = output_format
        self.audio_format = audio_format
        self.subtitles = subtitles
        self.playlist = playlist
        self.sponsorblock = sponsorblock

        # Thread-safe progress queue
        self._progress_queue: stdlib_queue.Queue = stdlib_queue.Queue()

    # ------------------------------------------------------------------
    # Build yt-dlp options — no artificial limits
    # ------------------------------------------------------------------

    def _build_opts(self, tempdir: Path) -> dict:
        name_tmpl = self.custom_filename or "%(title).100s.%(ext)s"
        filename_tmpl = str(tempdir / name_tmpl)

        opts: dict = {
            "outtmpl": filename_tmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": not self.playlist,
            "ignoreerrors": True,          # skip unavailable videos in playlists
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "extractor_retries": 5,
            "progress_hooks": [self._hook],
            "postprocessors": [],
            # No max_filesize — truly unlimited
            "concurrent_fragment_downloads": 8,  # parallel HLS/DASH fragments
            "http_chunk_size": 10 * 1024 * 1024,  # 10 MB chunks
            "prefer_free_formats": False,
            "merge_output_format": "mp4",
            "writethumbnail": True,        # download thumbnail for video files
            "geo_bypass": True,            # bypass geo restrictions
            "age_limit": None,             # no age restriction bypass needed (cookies handle it)
            # Force ffmpeg for HLS/DASH — avoids saving raw .m3u8 as output
            "hls_use_mpegts": True,
        }

        # Format selection
        if self.output_format == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"].append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": self.audio_format,
                "preferredquality": "0",   # best quality
            })

        elif self.quality in ("best", ""):
            # Best video + audio, merged to mp4
            opts["format"] = (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                "/bestvideo+bestaudio"
                "/best"
            )

        else:
            h = self.quality  # e.g. "1080", "720"
            opts["format"] = (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]"
                f"/best"
            )

        # Subtitles
        if self.subtitles:
            opts.update({
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["en", "ar", "fr", "es", "de", "ja", "zh-Hans"],
                "subtitlesformat": "srt/best",
            })
            opts["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})

        # SponsorBlock — skip sponsor segments in YouTube videos
        if self.sponsorblock:
            opts["sponsorblock_mark"] = "all"
            opts["postprocessors"].append({
                "key": "SponsorBlock",
                "categories": ["sponsor", "intro", "outro", "selfpromo"],
            })
            opts["postprocessors"].append({"key": "ModifyChapters"})

        # ── JavaScript runtime (Node.js) ───────────────────────────────────
        # yt-dlp auto-detects Node.js in PATH. If present, it uses it for
        # YouTube nsig decoding. We do NOT pass a fake js_runtimes option
        # (that key does not exist in yt-dlp).

        # ── Cookie injection (from file, NEVER hardcoded) ───────────────────
        cookie_file = _get_cookie_file(self.url)
        if cookie_file:
            opts["cookiefile"] = cookie_file

        # ── YouTube OAuth2 (when token exists from /ytlogin) ───────────────
        url_l = self.url.lower()
        if ("youtube" in url_l or "youtu.be" in url_l) and _YOUTUBE_OAUTH_FILE.exists():
            opts["username"] = "oauth2"
            opts["password"] = ""

        return opts

    # ------------------------------------------------------------------
    # Progress hook (runs in yt-dlp thread)
    # ------------------------------------------------------------------

    def _hook(self, d: dict) -> None:
        if self._cancel_event.is_set():
            raise yt_dlp.utils.DownloadError("Cancelled by user")

        if d.get("status") == "downloading":
            self._progress_queue.put_nowait({
                "done": d.get("downloaded_bytes", 0),
                "total": d.get("total_bytes") or d.get("total_bytes_estimate", 0),
                "speed": d.get("speed") or 0.0,
                "eta": d.get("eta") or 0,
                "filename": Path(d.get("filename", "")).name,
            })

    # ------------------------------------------------------------------
    # Async progress reporter
    # ------------------------------------------------------------------

    async def _progress_loop(self, done_event: asyncio.Event) -> None:
        while not done_event.is_set():
            try:
                p = self._progress_queue.get_nowait()
                await self._report_progress(p.get("done", 0), max(p.get("total", 1), 1))
            except stdlib_queue.Empty:
                pass
            await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # Main download
    # ------------------------------------------------------------------

    async def download(self) -> list[Path]:
        # Pre-process: decode Facebook redirect wrappers and reject profile pages early.
        if "facebook.com" in self.url.lower():
            self.url = _normalize_fb_url(self.url)   # may raise DownloadError immediately

        tempdir = self._make_tempdir()
        opts = self._build_opts(tempdir)
        done_event = asyncio.Event()

        self._log(
            "yt-dlp | quality=%s format=%s playlist=%s url=%s",
            self.quality, self.output_format, self.playlist, self.url
        )

        progress_task = asyncio.create_task(self._progress_loop(done_event))
        loop = asyncio.get_running_loop()

        try:
            await loop.run_in_executor(None, self._run_ytdl, opts)
        except yt_dlp.utils.DownloadError as exc:
            if "Cancelled" in str(exc):
                raise DownloadCancelled("Download cancelled") from exc
            err = str(exc)
            # YouTube bot-detection / age gate — give actionable guidance
            if "youtube" in self.url.lower() or "youtu.be" in self.url.lower():
                if "confirm you're not a bot" in err or "Sign in" in err:
                    oauth_hint = (
                        "\n\n✅ **Fix:** Ask the bot owner to run `/ytlogin` to link a "
                        "YouTube account — one-time setup, fixes all restricted videos."
                    )
                    raise DownloadError(
                        "⚠️ YouTube requires login for this video "
                        "(age-restricted or region-locked)." + oauth_hint
                    ) from exc
                if "age" in err.lower() or "age-restricted" in err.lower():
                    raise DownloadError(
                        "⚠️ This YouTube video is age-restricted.\n"
                        "The bot owner must run `/ytlogin` to authenticate with YouTube."
                    ) from exc
            raise DownloadError(f"yt-dlp: {exc}") from exc
        finally:
            done_event.set()
            await asyncio.gather(progress_task, return_exceptions=True)

        # Collect output files, excluding partial/temp files
        VIDEO_AUDIO_EXTS = {
            ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv",
            ".mp3", ".m4a", ".aac", ".opus", ".flac", ".ogg", ".wav",
        }
        all_files = [
            f for f in tempdir.rglob("*")
            if f.is_file() and f.suffix.lower() not in (".part", ".ytdl", ".aria2")
        ]

        # Reject suspiciously tiny video/audio files (M3U8 playlists, error pages)
        # — the Snapchat CDN, for example, returns 1984-byte playlist files that
        #   yt-dlp saves as .mp4 when the signed segment URLs have expired.
        MIN_MEDIA_BYTES = 50 * 1024  # 50 KB
        valid_files = []
        for f in all_files:
            size = f.stat().st_size
            if f.suffix.lower() in VIDEO_AUDIO_EXTS and size < MIN_MEDIA_BYTES:
                self._log(
                    "Rejecting tiny media file: %s (%d bytes) — "
                    "likely an expired CDN token or playlist file",
                    f.name, size,
                )
                continue
            valid_files.append(f)

        # Separate thumbnails from media files.
        # yt-dlp saves thumbnails as <title>.<ext>.jpg or <title>.jpg alongside
        # the video.  We store them in self.thumbnails so the uploader can embed
        # them instead of sending them as separate messages.
        IMAGE_EXTS = {".jpg", ".jpeg", ".webp", ".png"}
        media_files = [f for f in valid_files if f.suffix.lower() not in IMAGE_EXTS]
        thumb_files  = [f for f in valid_files if f.suffix.lower() in IMAGE_EXTS]

        for thumb in thumb_files:
            # Match by longest common stem prefix
            best_match: Path | None = None
            best_len = 0
            for mf in media_files:
                # yt-dlp names the thumbnail the same as the video but with an
                # image extension, e.g.  "My Video.mp4"  →  "My Video.jpg"
                if mf.stem.lower() == thumb.stem.lower():
                    best_match = mf
                    break
                # Partial prefix match fallback
                common = len(
                    [c for c, d in zip(mf.stem.lower(), thumb.stem.lower()) if c == d]
                )
                if common > best_len:
                    best_len = common
                    best_match = mf
            if best_match:
                self.thumbnails[best_match.stem.lower()] = thumb

        files = sorted(media_files if media_files else valid_files,
                       key=lambda p: p.stat().st_size, reverse=True)

        if not files:
            # Give a platform-specific hint
            url_l = self.url.lower()
            if "youtube" in url_l or "youtu.be" in url_l:
                raise DownloadError(
                    "⚠️ YouTube download produced no files.\n\n"
                    "This video may be:\n"
                    "• Age-restricted — run `/ytlogin` to link a YouTube account\n"
                    "• Region-locked or members-only\n"
                    "• Deleted or private\n\n"
                    "Try `/ytlogin` if the video is publicly visible in a browser."
                )
            if "instagram.com" in url_l:
                raise DownloadError(
                    "Instagram download failed — content requires login.\n"
                    "Use /igstory for stories, or ask the bot owner to set INSTAGRAM_SESSIONID."
                )
            if "tiktok.com" in url_l:
                raise DownloadError(
                    "TikTok download failed — TikTok blocks server IPs.\n"
                    "The video may have been deleted, or try again later."
                )
            if "snapchat.com" in url_l:
                raise DownloadError(
                    "Snapchat download failed — CDN tokens expired before download.\n"
                    "Try again immediately after copying the link."
                )
            raise DownloadError("yt-dlp produced no output files")

        self._log("Downloaded %d file(s)", len(files))
        return files

    def _run_ytdl(self, opts: dict) -> None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([self.url])

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def get_info(url: str) -> dict:
        """Fetch metadata without downloading."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "ignoreerrors": True,
            "geo_bypass": True,
        }
        loop = asyncio.get_running_loop()

        def _fetch():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        return await loop.run_in_executor(None, _fetch) or {}

    @staticmethod
    async def list_formats(url: str) -> str:
        """Return a formatted string of available formats."""
        info = await YtdlDownloader.get_info(url)
        if not info:
            return "Could not fetch formats."
        formats = info.get("formats", [])
        lines = [f"**Available formats for:** `{info.get('title', url)[:60]}`\n"]
        seen = set()
        for f in reversed(formats):  # best quality last = reversed display
            ext = f.get("ext", "?")
            res = f.get("height") or f.get("abr") or "?"
            note = f.get("format_note", "")
            key = (ext, res)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"• `{f['format_id']}` — {ext} {res}p {note}".rstrip())
        return "\n".join(lines[:30])
