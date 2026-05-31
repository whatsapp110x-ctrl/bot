"""
Snapchat story/spotlight downloader.

Strategy (tried in order):
  1. __NEXT_DATA__ scraper  — reads the snap CDN URLs directly from the page JSON.
                              Works for /spotlight/<token> and t.snapchat.com URLs.
  2. yt-dlp                 — fallback for Spotlight, story.snapchat.com, and other formats.
  3. snapsave.app scraper   — last resort.

NOT supported:
  • snapchat.com/add/<user>  — "Add me" profile pages have no downloadable media.

snapMediaType in __NEXT_DATA__:
  1 = video  → save as .mp4
  0 = image  → save as .jpg
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import requests

from utils.file_utils import safe_filename
from .base import BaseDownloader, DownloadError, DownloadCancelled

logger = logging.getLogger(__name__)

SNAPCHAT_RE = re.compile(
    r"(snapchat\.com|t\.snapchat\.com|story\.snapchat\.com|spotlight\.snapchat\.com)",
    re.IGNORECASE,
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.6099.144 Mobile Safari/537.36"
)
_CDN_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://www.snapchat.com/",
    "Origin": "https://www.snapchat.com",
    "Accept": "*/*",
}


def is_snapchat_url(url: str) -> bool:
    return bool(SNAPCHAT_RE.search(url))


def _is_profile_add_link(url: str) -> bool:
    """
    snapchat.com/add/<user> and snapchat.com/add/<user>/<snapcode> are
    "Add me" profile pages — they contain no downloadable snap media.
    """
    return bool(re.search(r"snapchat\.com/add/[^/?#]+", url, re.I))


# ---------------------------------------------------------------------------
# Strategy 1: __NEXT_DATA__ page scraper (primary)
# ---------------------------------------------------------------------------

def _extract_snap_urls(snap_urls_obj) -> tuple[str, str]:
    """Extract (media_url, thumb_url) from a snapUrls dict."""
    if not isinstance(snap_urls_obj, dict):
        return "", ""
    media_url = snap_urls_obj.get("mediaUrl", "")
    preview = snap_urls_obj.get("mediaPreviewUrl") or {}
    thumb_url = (
        preview.get("value") if isinstance(preview, dict) else str(preview)
    ) or ""
    return media_url, thumb_url


def _scrape_next_data(url: str) -> list[dict]:
    """
    Fetch the Snapchat page, parse __NEXT_DATA__ JSON, and return a list of
    {url, thumb_url, media_type} dicts (one per snap in the story/spotlight).

    Handles:
      • snapchat.com/spotlight/<token>
      • t.snapchat.com/<token>
      • story.snapchat.com/...
    """
    import json

    resp = requests.get(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=20,
        allow_redirects=True,
    )
    resp.raise_for_status()

    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    if not m:
        raise ValueError("__NEXT_DATA__ script tag not found in page")

    data = json.loads(m.group(1))
    page_props = data.get("props", {}).get("pageProps", {})

    # ── Strategy A: story page (snapList) ───────────────────────────────────
    _spotlight = bool(re.search(r"snapchat\.com/spotlight/", url))
    story = page_props.get("story")
    if story:
        snap_list = story.get("snapList", [])
        if snap_list:
            # Spotlight URLs always share a single video — the feed fills snapList
            # with 10+ unrelated videos from other creators; cap to 1.
            items_to_use = snap_list[:1] if _spotlight else snap_list
            results: list[dict] = []
            for snap in items_to_use:
                snap_urls = snap.get("snapUrls", {})
                media_url, thumb_url = _extract_snap_urls(snap_urls)
                if not media_url:
                    continue
                media_type = snap.get("snapMediaType", 1)
                results.append({"url": media_url, "thumb_url": thumb_url, "media_type": media_type})
            if results:
                return results
        if not _spotlight:
            raise ValueError("snapList is empty — story may have expired")

    # ── Strategy B: Spotlight page ───────────────────────────────────────────
    spotlight_keys = [
        ("snapInfo",),
        ("mediaDetails",),
        ("spotlightFeedItems", 0),
        ("initialSnap",),
    ]

    for key_path in spotlight_keys:
        node = page_props
        for k in key_path:
            if isinstance(node, list):
                node = node[k] if k < len(node) else {}
            else:
                node = node.get(k, {}) if isinstance(node, dict) else {}

        if not node:
            continue

        snap_urls = node.get("snapUrls") or node.get("details", {}).get("snapUrls")
        if isinstance(snap_urls, dict):
            media_url, thumb_url = _extract_snap_urls(snap_urls)
            if media_url:
                return [{"url": media_url, "thumb_url": thumb_url, "media_type": 1}]

        snap_list = node.get("snapList", [])
        if snap_list:
            results = []
            _is_spot = bool(re.search(r"snapchat\.com/spotlight/", url))
            for snap in (snap_list[:1] if _is_spot else snap_list):
                snap_urls = snap.get("snapUrls", {})
                media_url, thumb_url = _extract_snap_urls(snap_urls)
                if media_url:
                    results.append({"url": media_url, "thumb_url": thumb_url,
                                    "media_type": snap.get("snapMediaType", 1)})
            if results:
                return results

    # ── Strategy C: deep-search any snapUrls.mediaUrl anywhere in pageProps ─
    _is_spotlight = bool(re.search(r"snapchat\.com/spotlight/", url))

    def _deep_find_media(obj, depth=0) -> list[dict]:
        if depth > 8 or not isinstance(obj, (dict, list)):
            return []
        found = []
        if isinstance(obj, dict):
            mu, tu = _extract_snap_urls(obj)
            if mu and mu.startswith("http"):
                found.append({"url": mu, "thumb_url": tu, "media_type": 1})
            for v in obj.values():
                found.extend(_deep_find_media(v, depth + 1))
        else:
            for item in obj:
                found.extend(_deep_find_media(item, depth + 1))
        return found

    deep_results = _deep_find_media(page_props)
    if deep_results:
        limit = 1 if _is_spotlight else 10
        return deep_results[:limit]

    raise ValueError("No downloadable media found in __NEXT_DATA__ — page may be private or expired")


def _download_snap(item: dict, dest_dir: Path, index: int) -> tuple[Path, Path | None]:
    """Download one snap CDN entry; return (video_path, thumb_path or None)."""
    ext = ".mp4" if item["media_type"] == 1 else ".jpg"
    dest = dest_dir / safe_filename(f"snapchat_{index:03d}{ext}")

    with requests.get(item["url"], headers=_CDN_HEADERS, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=2 * 1024 * 1024):
                f.write(chunk)

    if dest.stat().st_size < 5_000:
        dest.unlink(missing_ok=True)
        raise DownloadError(f"Snap {index} too small after download (CDN may have blocked)")

    thumb_path: Path | None = None
    if item.get("thumb_url"):
        try:
            thumb_dest = dest_dir / safe_filename(f"snapchat_{index:03d}_thumb.jpg")
            with requests.get(item["thumb_url"], headers=_CDN_HEADERS, stream=True, timeout=30) as tr:
                if tr.status_code == 200:
                    with open(thumb_dest, "wb") as tf:
                        for chunk in tr.iter_content(chunk_size=512 * 1024):
                            tf.write(chunk)
                    if thumb_dest.stat().st_size > 1_000:
                        thumb_path = thumb_dest
                    else:
                        thumb_dest.unlink(missing_ok=True)
        except Exception:
            pass

    return dest, thumb_path


# ---------------------------------------------------------------------------
# Strategy 2: yt-dlp (for Spotlight, story.snapchat.com, etc.)
# ---------------------------------------------------------------------------

def _try_ytdlp(url: str, dest_dir: Path) -> list[tuple[Path, Path | None]]:
    import yt_dlp

    IMAGE_EXTS = {".jpg", ".jpeg", ".webp", ".png"}

    opts = {
        "outtmpl": str(dest_dir / "snapchat_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "writethumbnail": True,
        "retries": 3,
        "merge_output_format": "mp4",
        "ignoreerrors": False,
        "http_headers": {"User-Agent": _UA_MOBILE},
        "noplaylist": True,
        "playlistend": 1,
        "check_formats": False,
        "allow_unplayable_formats": True,
        "fixup": "never",
        "prefer_free_formats": False,
    }

    before = set(dest_dir.rglob("*"))
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        raise DownloadError(f"yt-dlp: {exc}") from exc

    after = set(dest_dir.rglob("*"))
    new_files = [f for f in (after - before) if f.is_file() and f.stat().st_size > 5_000]
    if not new_files:
        raise DownloadError("yt-dlp finished but produced no files")

    _KNOWN_MEDIA = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv",
                    ".ts", ".mp3", ".m4a", ".aac", ".opus", ".jpg", ".jpeg",
                    ".webp", ".png"}
    renamed = []
    for f in new_files:
        if f.suffix.lower() not in _KNOWN_MEDIA and f.suffix:
            new_f = f.with_suffix(".mp4")
            f.rename(new_f)
            renamed.append(new_f)
        else:
            renamed.append(f)
    new_files = renamed

    media = [f for f in new_files if f.suffix.lower() not in IMAGE_EXTS]
    thumbs = {f.stem.lower(): f for f in new_files if f.suffix.lower() in IMAGE_EXTS}
    if not media:
        raise DownloadError("yt-dlp produced only thumbnails, no video/audio")

    return [(mf, thumbs.get(mf.stem.lower())) for mf in media]


# ---------------------------------------------------------------------------
# Strategy 3: snapsave.app scraper
# ---------------------------------------------------------------------------

def _scrape_snapsave(url: str) -> list[dict]:
    sess = requests.Session()
    sess.headers.update({"User-Agent": _UA})

    try:
        home = sess.get("https://snapsave.app/", timeout=10)
        token_m = re.search(
            r'name=["\']token["\'][^>]+value=["\']([^"\']+)["\']', home.text
        )
    except Exception:
        token_m = None

    post_data: dict = {"url": url, "lang": "en"}
    if token_m:
        post_data["token"] = token_m.group(1)

    resp = sess.post(
        "https://snapsave.app/action.php",
        data=post_data,
        headers={
            "Referer": "https://snapsave.app/",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=30,
    )
    resp.raise_for_status()

    raw = resp.text.strip()
    if not raw:
        raise ValueError("snapsave returned empty response")

    try:
        body = resp.json()
        html_block = body.get("data") or body.get("html") or ""
    except Exception:
        html_block = raw

    if not html_block:
        raise ValueError("snapsave: no data in response")

    links = re.findall(r'href=["\']?(https://[^"\'>\s]+)["\']?', html_block)
    mp4_links = [l for l in links if ".mp4" in l.lower() or "sc-cdn" in l.lower()]
    if not mp4_links:
        raise ValueError(f"snapsave: no video links found ({len(links)} links total)")

    return [{"url": l, "thumb_url": "", "media_type": 1} for l in mp4_links]


# ---------------------------------------------------------------------------
# Main downloader class
# ---------------------------------------------------------------------------

def _is_next_data_candidate(url: str) -> bool:
    """True for /spotlight/ URLs and t.snapchat.com story links."""
    return (
        bool(re.search(r"snapchat\.com/spotlight/", url))
        or bool(re.search(r"t\.snapchat\.com/", url))
        or bool(re.search(r"story\.snapchat\.com/", url))
    )


class SnapchatDownloader(BaseDownloader):
    ENGINE_NAME = "snapchat"

    async def download(self) -> list[Path]:
        self._log("Snapchat download: %s", self.url)

        _is_add = _is_profile_add_link(self.url)
        tempdir = self._make_tempdir()
        loop = asyncio.get_running_loop()
        errors: list[str] = []
        _add_found_media = False

        # ── Strategy 1: __NEXT_DATA__ scraper ────────────────────────────
        # For /add/ profile links we still attempt a quick scrape (12 s timeout)
        # — a small number of deep-link add URLs embed actual snap media.
        _try_next_data = _is_next_data_candidate(self.url) or _is_add
        if _try_next_data:
            try:
                coro = loop.run_in_executor(None, _scrape_next_data, self.url)
                if _is_add:
                    snaps = await asyncio.wait_for(coro, timeout=12.0)
                else:
                    snaps = await coro
                self._log("Found %d snap(s) in __NEXT_DATA__", len(snaps))
                if snaps and _is_add:
                    _add_found_media = True
                files: list[Path] = []
                for i, item in enumerate(snaps, 1):
                    await self._check_cancelled()
                    try:
                        video_path, thumb_path = await loop.run_in_executor(
                            None, _download_snap, item, tempdir, i
                        )
                        files.append(video_path)
                        if thumb_path:
                            self.thumbnails[video_path.stem.lower()] = thumb_path
                        await self._report_progress(i, len(snaps))
                    except DownloadError as exc:
                        self._log("Snap %d skipped: %s", i, exc)
                        errors.append(f"snap {i}: {exc}")

                if files:
                    self._log("Got %d file(s) via __NEXT_DATA__", len(files))
                    return files

                if snaps:
                    errors.append("__NEXT_DATA__: snaps found but none downloaded successfully")
            except asyncio.TimeoutError:
                self._log("__NEXT_DATA__ timed out (add link)")
            except Exception as exc:
                errors.append(f"__NEXT_DATA__: {exc}")
                self._log("__NEXT_DATA__ scraper failed: %s", exc)

        # Reject plain profile add links — no media found in __NEXT_DATA__
        if _is_add and not _add_found_media:
            raise DownloadError(
                "This is a Snapchat **Add Me** profile link — there is no downloadable video here.\n\n"
                "✅ **Supported URLs:**\n"
                "• Spotlight: `snapchat.com/spotlight/<token>`\n"
                "• Story share: `t.snapchat.com/<token>`\n"
                "• Story page: `story.snapchat.com/...`\n\n"
                "**Tip:** To share a Snap, tap and hold the video → **Share** → **Copy Link**."
            )

        await self._check_cancelled()

        # ── Strategy 2: yt-dlp ────────────────────────────────────────────
        try:
            pairs = await loop.run_in_executor(None, _try_ytdlp, self.url, tempdir)
            files = []
            for video_path, thumb_path in pairs:
                files.append(video_path)
                if thumb_path:
                    self.thumbnails[video_path.stem.lower()] = thumb_path
            if files:
                self._log("yt-dlp got %d file(s)", len(files))
                return files
        except DownloadError as exc:
            errors.append(f"yt-dlp: {exc}")
            self._log("yt-dlp failed: %s", exc)

        await self._check_cancelled()

        # ── Strategy 3: snapsave ──────────────────────────────────────────
        try:
            medias = await loop.run_in_executor(None, _scrape_snapsave, self.url)
            self._log("snapsave found %d item(s)", len(medias))
            files = []
            for i, item in enumerate(medias, 1):
                await self._check_cancelled()
                try:
                    video_path, thumb_path = await loop.run_in_executor(
                        None, _download_snap, item, tempdir, i
                    )
                    files.append(video_path)
                    if thumb_path:
                        self.thumbnails[video_path.stem.lower()] = thumb_path
                    await self._report_progress(i, len(medias))
                except DownloadError as exc:
                    self._log("Item %d skipped: %s", i, exc)

            if files:
                self._log("Got %d file(s) via snapsave", len(files))
                return files
            errors.append("snapsave: items found but none downloaded")
        except Exception as exc:
            errors.append(f"snapsave: {exc}")
            self._log("snapsave failed: %s", exc)

        raise DownloadError(
            "Snapchat download failed — tried 3 methods:\n"
            + "\n".join(f"  • {e}" for e in errors)
            + "\n\nTip: Only **public** Snapchat Spotlight and story share links work. "
            "Private snaps and friend-only stories are inaccessible."
        )
