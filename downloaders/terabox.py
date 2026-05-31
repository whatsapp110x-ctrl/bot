"""
Terabox / 1024tera downloader — uses yt-dlp for reliable full-video downloads.

yt-dlp has a native Terabox extractor that correctly handles HLS streams and
downloads the complete video at the original duration.

Fallback: manual HLS + ffmpeg pipeline if yt-dlp extraction fails.
Set TERABOX_NDUS env var for private/restricted links.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

from config import MAX_DOWNLOAD_SIZE
from utils.file_utils import safe_filename
from .base import BaseDownloader, DownloadError, DownloadCancelled

logger = logging.getLogger(__name__)

# ── Domain patterns ────────────────────────────────────────────────────────────
TERABOX_PATTERN = re.compile(
    r"(terabox\.com|terabox\.app|1024tera(?:box)?\.com|teraboxapp\.com"
    r"|teraboxlink\.com|terafileshare\.com|4funbox\.com|mirrobox\.com"
    r"|nephobox\.com|freeterabox\.com|dubox\.com|boxlinks\.net"
    r"|terasharelink\.com|1024terabox\.com)",
    re.IGNORECASE,
)


def is_terabox_url(url: str) -> bool:
    return bool(TERABOX_PATTERN.search(url))


# ── Constants ──────────────────────────────────────────────────────────────────
BASE_DOMAIN = "dm.1024tera.com"
BASE_URL    = f"https://{BASE_DOMAIN}"

STREAM_QUALITIES = [
    "M3U8_AUTO_1080",
    "M3U8_AUTO_720",
    "M3U8_AUTO_480",
    "M3U8_AUTO_360",
    "M3U8_AUTO_240",
]

FALLBACK_DOMAINS = [
    "https://www.1024terabox.com",
    "https://www.terabox.com",
    "https://teraboxapp.com",
]

CHUNK_SIZE  = 2 * 1024 * 1024
PARALLEL_DL = 4

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


def _ua() -> str:
    return random.choice(USER_AGENTS)


def _logid() -> str:
    return str(random.randint(400_000_000_000_000_000, 999_999_999_999_999_999))


# ══════════════════════════════════════════════════════════════════════════════
# METHOD 1: yt-dlp (primary — handles full HLS stream correctly)
# ══════════════════════════════════════════════════════════════════════════════

def _ytdlp_download(
    url: str,
    dest_dir: Path,
    cancel_event: threading.Event | None,
    progress_cb,
) -> list[Path]:
    """
    Download using yt-dlp's native Terabox extractor.
    yt-dlp correctly fetches the FULL HLS playlist and downloads all segments,
    giving you the complete video at the original duration.
    """
    if not HAS_YTDLP:
        raise DownloadError("yt-dlp not installed")

    output_template = str(dest_dir / "%(title).100s.%(ext)s")
    downloaded_files: list[str] = []

    _loop = asyncio.get_running_loop()

    class _CancelHook:
        def __call__(self, d):
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled()
            if d.get("status") == "finished":
                downloaded_files.append(d["filename"])
            if progress_cb and d.get("status") == "downloading":
                total   = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                current = d.get("downloaded_bytes", 0)
                if total and current:
                    try:
                        if _loop.is_running():
                            asyncio.run_coroutine_threadsafe(progress_cb(current, total), _loop)
                    except Exception:
                        pass

    ydl_opts: dict = {
        "outtmpl":        output_template,
        "format":         "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist":     True,
        "quiet":          False,
        "no_warnings":    False,
        "progress_hooks": [_CancelHook()],
        "socket_timeout": 60,
        "retries":        5,
        "http_headers":   {"User-Agent": _ua()},
        # Let yt-dlp handle HLS completely — no manual chunk logic
        "hls_use_mpegts": True,
        "concurrent_fragment_downloads": 4,
    }

    # Pass ndus cookie if available
    ndus = os.getenv("TERABOX_NDUS")
    if ndus:
        ydl_opts["cookiefile"] = None  # use header cookie instead
        ydl_opts["http_headers"]["Cookie"] = f"ndus={ndus}"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadCancelled:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(f"yt-dlp failed: {e}") from e
    except Exception as e:
        raise DownloadError(f"yt-dlp error: {e}") from e

    # Collect output files
    result_paths: list[Path] = []

    if downloaded_files:
        for f in downloaded_files:
            p = Path(f)
            if p.exists():
                result_paths.append(p)
    else:
        # yt-dlp may rename/merge — scan dest_dir for recently created files
        cutoff = time.time() - 300
        for f in dest_dir.iterdir():
            if f.is_file() and f.stat().st_mtime > cutoff:
                result_paths.append(f)

    if not result_paths:
        raise DownloadError("yt-dlp ran but no output file found")

    return result_paths


# ══════════════════════════════════════════════════════════════════════════════
# METHOD 2: Manual HLS fallback (original approach, kept as backup)
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_short_url(url: str) -> str:
    if "/s/" not in url:
        return url
    try:
        r = requests.get(url, allow_redirects=True, timeout=15, headers={"User-Agent": _ua()})
        if "surl=" in r.url:
            return r.url
    except Exception:
        pass
    return url


def _extract_surl(url: str) -> str | None:
    for pat in [
        r"[?&]surl=([^&/#\s]+)",
        r"/s/([^/?&#\s]+)",
        r"surl=([a-zA-Z0-9_\-]+)",
    ]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _build_session(ndus: str | None = None) -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=PARALLEL_DL + 2,
        pool_maxsize=PARALLEL_DL + 4,
        max_retries=Retry(total=0),
    )
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    s.headers.update({"User-Agent": _ua(), "Accept-Language": "en-US,en;q=0.9"})
    if ndus:
        for domain in [
            ".1024tera.com", ".terabox.com", ".1024terabox.com",
            ".teraboxapp.com", ".terabox.app",
        ]:
            s.cookies.set("ndus", ndus, domain=domain, path="/")
    return s


def _cookie_hdr(session: requests.Session) -> str:
    return "; ".join(f"{c.name}={c.value}" for c in session.cookies)


def _req_hdrs(session: requests.Session, surl: str = "") -> dict:
    h: dict[str, str] = {"User-Agent": _ua()}
    h["Referer"] = f"{BASE_URL}/wap/share/filelist?surl={surl}" if surl else f"{BASE_URL}/"
    ck = _cookie_hdr(session)
    if ck:
        h["Cookie"] = ck
    return h


def _get_js_token(session: requests.Session, surl: str) -> str:
    candidates = [
        f"https://www.1024tera.com/wap/share/filelist?surl={surl}",
        f"{BASE_URL}/wap/share/filelist?surl={surl}&clearCache=1",
        f"https://www.1024terabox.com/wap/share/filelist?surl={surl}",
        f"https://www.terabox.com/wap/share/filelist?surl={surl}",
    ]
    for url in candidates:
        try:
            r    = session.get(url, headers=_req_hdrs(session, surl), timeout=30, allow_redirects=True)
            html = r.text
            m = re.search(r'fn%28%22([A-Fa-f0-9]{20,})%22%29', html)
            if m:
                return m.group(1)
            m2 = re.search(r'eval\(decodeURIComponent\(`([^`]+)`\)\)', html)
            if m2:
                inner = unquote(m2.group(1))
                m3 = re.search(r'fn\("([A-Fa-f0-9]{20,})"\)', inner)
                if m3:
                    return m3.group(1)
            m = re.search(r'"jsToken"\s*:\s*"([^"]{20,})"', html)
            if m:
                return m.group(1)
        except Exception as exc:
            logger.debug("jsToken [%s] failed: %s", url, exc)
        time.sleep(1)
    logger.warning("Could not extract jsToken — proceeding without it")
    return ""


def _get_share_data(session: requests.Session, js_token: str, surl: str) -> dict:
    # ── Priority 1: share/list with bare shorturl (no "1" prefix)
    #    This is the ONLY path that returns a populated dlink when ndus
    #    cookie is present (reverse-engineered from abhinai2244/TeraBox-Dl).
    # ──
    for base in [
        "https://dm.terabox.app",        # abhinai2244 domain
        "https://www.1024tera.com",
        BASE_URL,
        "https://www.1024terabox.com",
        "https://www.terabox.com",
        "https://teraboxapp.com",
    ]:
        try:
            params = {
                "app_id": "250528", "jsToken": js_token,
                "site_referer": "https://www.terabox.app/",
                "shorturl": surl,       # bare shorturl — NO "1" prefix
                "root": "1", "web": "1",
                "channel": "dubox", "clienttype": "0",
            }
            hdrs = {
                "User-Agent": _ua(),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{base}/sharing/link?surl={surl}&clearCache=1",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": base,
            }
            ck = _cookie_hdr(session)
            if ck:
                hdrs["Cookie"] = ck
            r = session.get(f"{base}/share/list", params=params, headers=hdrs, timeout=30)
            data = r.json()
            if data.get("errno") == 0 and data.get("list"):
                # Check if any file has a real dlink
                for f in data.get("list", []):
                    if f.get("dlink"):
                        logger.info("share/list with bare shorturl returned dlink from %s", base)
                        return data
        except Exception as exc:
            logger.debug("share/list [%s] bare: %s", base, exc)

    # ── Priority 2: classic shorturlinfo ("1" prefix, no auth) ──
    primary = {
        "app_id": "250528", "shorturl": f"1{surl}", "root": "1",
        "web": "1", "channel": "dubox", "clienttype": "0",
        "jsToken": js_token, "t": str(int(time.time())), "dp-logid": _logid(),
    }
    secondary = {"surl": surl, "web": "1", "channel": "dubox", "clienttype": "0", "jsToken": js_token}
    combos = [
        ("https://www.1024tera.com",    primary),
        (BASE_URL,                       primary),
        ("https://www.1024terabox.com", primary),
        ("https://www.terabox.com",     primary),
        ("https://www.1024terabox.com", secondary),
        ("https://www.terabox.com",     secondary),
        ("https://teraboxapp.com",      secondary),
    ]
    for base, params in combos:
        try:
            hdrs = {
                "User-Agent": _ua(), "Accept": "application/json, text/plain, */*",
                "Origin": base, "Referer": f"{base}/wap/share/filelist?surl={surl}",
            }
            ck = _cookie_hdr(session)
            if ck:
                hdrs["Cookie"] = ck
            r    = session.get(f"{base}/api/shorturlinfo", params=params, headers=hdrs, timeout=30)
            data = r.json()
            if data.get("errno") == 0 and data.get("list"):
                return data
        except Exception as exc:
            logger.debug("shorturlinfo [%s]: %s", base, exc)

    # ── Priority 3: legacy share/list fallback ──
    for base in FALLBACK_DOMAINS:
        try:
            params2 = {
                "app_id": "250528", "web": "1", "channel": "dubox",
                "clienttype": "0", "jsToken": js_token, "dp-logid": _logid(),
                "page": "1", "num": "20", "order": "time", "desc": "1",
                "shorturl": surl, "root": "1",
            }
            r    = session.get(f"{base}/share/list", params=params2,
                               headers={"User-Agent": _ua(), "Referer": f"{base}/"}, timeout=30)
            data = r.json()
            if data.get("errno") == 0 and data.get("list"):
                return data
        except Exception as exc:
            logger.debug("share/list fallback [%s]: %s", base, exc)

    raise DownloadError(
        "Could not retrieve file list from Terabox API. "
        "The link may be private — set TERABOX_NDUS env var with your ndus cookie."
    )


def _build_stream_url(shareid, uk, sign, timestamp, fs_id, quality, js_token) -> str:
    return f"{BASE_URL}/share/streaming?" + urlencode({
        "uk": uk, "shareid": shareid, "type": quality,
        "fid": fs_id, "sign": sign, "timestamp": timestamp,
        "jsToken": js_token, "esl": "1", "isplayer": "1", "ehps": "1",
        "clienttype": "0", "app_id": "250528", "web": "1",
        "channel": "dubox", "dp-logid": _logid(),
    })


def _pick_working_quality(session, shareid, uk, sign, timestamp, fs_id, js_token):
    for quality in STREAM_QUALITIES:
        url = _build_stream_url(shareid, uk, sign, timestamp, fs_id, quality, js_token)
        try:
            r = session.get(url, headers={"User-Agent": _ua(), "Referer": f"{BASE_URL}/"}, timeout=30)
            if r.status_code == 200 and r.text.strip().startswith("#EXTM3U"):
                return quality, r.text, url
        except Exception as exc:
            logger.debug("quality %s: %s", quality, exc)
    return None, None, None


def _remux_ts_to_mp4(tmp_ts: Path, dest: Path) -> None:
    """Local-only ffmpeg remux: .ts → .mp4.  No network access needed."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(tmp_ts),
        "-c", "copy",
        "-movflags", "+faststart",
        "-bsf:a", "aac_adtstoasc",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().split("\n")[-10:])
        raise DownloadError(f"ffmpeg remux failed (exit {proc.returncode}):\n{tail}")
    if not dest.exists() or dest.stat().st_size < 1000:
        raise DownloadError("ffmpeg remux produced an empty file")


def _download_full_ts_single_request(
    seg_url: str,
    ts_size: int,
    dest: Path,
    session: requests.Session,
    ua: str,
    cancel_event,
    progress_cb,
) -> None:
    """
    Download the COMPLETE transcoded TS stream in a single HTTP request.

    Discovery: Terabox's CDN does NOT include `range` or `len` in its URL
    signature check.  By replacing those two parameters with
    `range=0-{ts_size-1}` and `len={ts_size}`, the CDN returns the entire
    TS file (confirmed: 200, correct Content-Length, valid 0x47 sync bytes).

    This means we get the full-length video (all quality tiers that the API
    will serve without auth — typically 360p or 240p) in one streaming
    download instead of the 3-segment preview the M3U8 normally contains.
    """
    from urllib.parse import urlparse, parse_qs, urlencode as _ue

    parsed = urlparse(seg_url)
    qs = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
    qs["range"] = f"0-{ts_size - 1}"
    qs["len"]   = str(ts_size)
    full_url = parsed._replace(query=_ue(qs)).geturl()

    headers = {"User-Agent": ua, "Referer": f"{BASE_URL}/", "Accept": "*/*"}
    tmp_ts  = dest.with_suffix(".tmp.ts")

    try:
        downloaded = 0
        with open(tmp_ts, "wb") as fp:
            with session.get(full_url, headers=headers, timeout=600, stream=True) as r:
                r.raise_for_status()
                for chunk in r.iter_content(CHUNK_SIZE):
                    if cancel_event and cancel_event.is_set():
                        raise DownloadCancelled()
                    fp.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(downloaded, ts_size)
                        except Exception:
                            pass

        if downloaded < ts_size * 0.9:
            raise DownloadError(
                f"Incomplete download: got {downloaded} bytes, expected ~{ts_size}"
            )

        logger.info("Full TS downloaded: %d bytes → remuxing %s", downloaded, dest.name)
        _remux_ts_to_mp4(tmp_ts, dest)

    finally:
        if tmp_ts.exists():
            try:
                tmp_ts.unlink()
            except Exception:
                pass


def _download_hls_segments(
    m3u8_content: str,
    dest: Path,
    session: requests.Session,
    ua: str,
    cancel_event,
    progress_cb,
) -> None:
    """
    Fallback: download each HLS segment individually and concatenate.
    Used when the single-request range trick fails.
    """
    segments = [
        l.strip() for l in m3u8_content.splitlines()
        if l.strip() and not l.startswith("#")
    ]
    if not segments:
        raise DownloadError("No segments found in M3U8 playlist")

    logger.info("HLS segment-by-segment: %d segments", len(segments))
    headers = {"User-Agent": ua, "Referer": f"{BASE_URL}/", "Accept": "*/*"}
    tmp_ts  = dest.with_suffix(".tmp.ts")

    try:
        downloaded = 0
        total_hint = 0

        with open(tmp_ts, "wb") as fp:
            for idx, seg_url in enumerate(segments):
                if cancel_event and cancel_event.is_set():
                    raise DownloadCancelled()

                last_exc: Exception | None = None
                for attempt in range(4):
                    try:
                        r = session.get(seg_url, headers=headers, timeout=120, stream=True)
                        r.raise_for_status()
                        cl = int(r.headers.get("Content-Length", 0))
                        if cl and total_hint == 0:
                            total_hint = cl * len(segments)
                        for chunk in r.iter_content(CHUNK_SIZE):
                            if cancel_event and cancel_event.is_set():
                                raise DownloadCancelled()
                            fp.write(chunk)
                            downloaded += len(chunk)
                        last_exc = None
                        break
                    except DownloadCancelled:
                        raise
                    except Exception as exc:
                        last_exc = exc
                        logger.warning("Segment %d attempt %d: %s", idx + 1, attempt + 1, exc)
                        time.sleep(2 ** attempt)

                if last_exc is not None:
                    raise DownloadError(
                        f"Segment {idx + 1}/{len(segments)} failed: {last_exc}"
                    )

                if progress_cb and total_hint:
                    try:
                        progress_cb(downloaded, total_hint)
                    except Exception:
                        pass

        logger.info("Segments done (%d bytes). Remuxing → %s", downloaded, dest.name)
        _remux_ts_to_mp4(tmp_ts, dest)

    finally:
        if tmp_ts.exists():
            try:
                tmp_ts.unlink()
            except Exception:
                pass


def _manual_hls_fallback(
    session, shareid, uk, sign, timestamp, fs_id, quality, m3u8_content, js_token,
    fname, dest_dir, cancel_event, progress_cb,
) -> Path:
    """
    Download HLS video.  Primary method: single-request range trick for
    the full TS file.  Falls back to segment-by-segment if that fails.
    """
    stem = Path(fname).stem
    dest = dest_dir / f"{stem}.mp4"
    ua   = _ua()

    # Extract first segment URL and ts_size from the M3U8
    seg_lines = [
        l.strip() for l in m3u8_content.splitlines()
        if l.strip() and not l.startswith("#")
    ]

    ts_size: int | None = None
    first_seg: str | None = None
    if seg_lines:
        first_seg = seg_lines[0]
        m = re.search(r"ts_size=(\d+)", first_seg)
        if m:
            ts_size = int(m.group(1))

    if first_seg and ts_size and ts_size > 0:
        logger.info(
            "Terabox range trick: full TS in one request (%s, %.1f MB)",
            quality, ts_size / 1024 / 1024,
        )
        try:
            _download_full_ts_single_request(
                first_seg, ts_size, dest, session, ua, cancel_event, progress_cb
            )
            return dest
        except DownloadCancelled:
            raise
        except Exception as exc:
            logger.warning("Range trick failed (%s), falling back to segment download", exc)

    # Fallback: segment-by-segment
    _download_hls_segments(m3u8_content, dest, session, ua, cancel_event, progress_cb)
    return dest


def _get_dlink_from_share_list_retry(
    session: requests.Session,
    surl: str,
    ndus: str | None,
    js_token: str = "",
) -> str:
    """
    Targeted retry of share/list on domains known to return dlinks.

    dm.terabox.app with a valid jsToken reliably returns a populated `dlink`
    for public shares (confirmed by testing).  The dlink is downloadable with
    just the ndus cookie and gives the full-size original file.

    Retries up to 3 times with a short delay to handle intermittent API issues.
    """
    DLINK_DOMAINS = [
        "https://dm.terabox.app",
        "https://www.terabox.app",
        "https://www.terabox.com",
        "https://teraboxapp.com",
        "https://dm.1024tera.com",
        "https://www.1024tera.com",
    ]
    for attempt in range(3):
        if attempt > 0:
            time.sleep(2)
        for base in DLINK_DOMAINS:
            for shorturl in [surl, f"1{surl}"]:
                try:
                    params: dict[str, str] = {
                        "app_id": "250528",
                        "shorturl": shorturl,
                        "root": "1",
                        "web": "1",
                        "channel": "dubox",
                        "clienttype": "0",
                    }
                    if js_token:
                        params["jsToken"] = js_token
                        params["site_referer"] = "https://www.terabox.app/"
                    hdrs: dict[str, str] = {
                        "User-Agent": _ua(),
                        "Accept": "application/json, text/plain, */*",
                        "Referer": f"{base}/",
                        "Origin": base,
                    }
                    if ndus:
                        hdrs["Cookie"] = f"ndus={ndus}"
                    r = session.get(
                        f"{base}/share/list", params=params, headers=hdrs, timeout=20
                    )
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    if data.get("errno") != 0:
                        logger.debug(
                            "dlink retry [%s surl=%s]: errno=%s",
                            base, shorturl, data.get("errno"),
                        )
                        continue
                    for f in data.get("list", []):
                        dl = f.get("dlink", "")
                        if dl:
                            logger.info(
                                "share/list retry (attempt %d) got dlink from %s",
                                attempt + 1, base,
                            )
                            return dl
                except Exception as exc:
                    logger.debug("dlink retry [%s surl=%s]: %s", base, shorturl, exc)
    return ""


def _download_hls_with_ffmpeg(
    stream_url: str,
    dest: Path,
    session: requests.Session,
    ndus: str | None,
    surl: str,
) -> None:
    """
    Download the COMPLETE HLS stream using ffmpeg.

    Passes cookies + headers so ffmpeg can authenticate each segment request.
    This correctly downloads ALL segments (full video), unlike the old range
    trick which only fetched a single TS chunk.
    """
    cookie_hdr = ""
    if ndus:
        cookie_hdr = f"ndus={ndus}"
    else:
        raw = _cookie_hdr(session)
        if raw:
            cookie_hdr = raw

    # Build ffmpeg -headers value (each header ends with \r\n)
    headers_val = (
        f"User-Agent: {_ua()}\r\n"
        f"Referer: {BASE_URL}/wap/share/filelist?surl={surl}\r\n"
    )
    if cookie_hdr:
        headers_val += f"Cookie: {cookie_hdr}\r\n"

    cmd = [
        "ffmpeg", "-y",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
        "-headers", headers_val,
        "-i", stream_url,
        "-c", "copy",
        "-movflags", "+faststart",
        "-bsf:a", "aac_adtstoasc",
        str(dest),
    ]

    logger.info("ffmpeg HLS download started → %s", dest.name)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().split("\n")[-15:])
        raise DownloadError(f"ffmpeg HLS failed (exit {proc.returncode}):\n{tail}")
    if not dest.exists() or dest.stat().st_size < 100_000:
        raise DownloadError("ffmpeg HLS produced an empty or tiny file")

    logger.info("ffmpeg HLS done: %s (%.1f MB)", dest.name, dest.stat().st_size / 1024 / 1024)


def _probe_dlink_full_access(
    session: requests.Session,
    dlink: str,
    size: int,
    ndus: str | None,
) -> bool:
    """
    Quick 2-second probe: can we fetch the LAST 100 bytes of the file?

    Free Terabox accounts: CDN silently truncates GET responses to ~6 MB, but
    the HEAD returns the full Content-Length.  A range request for the final
    bytes reveals the truth:
      - 206 Partial Content with matching byte range → premium/full access ✓
      - 403, 200 (whole file), redirect, or error → free account, use HLS ✗

    This prevents wasting 3-5 minutes downloading a truncated file.
    """
    if size <= 0:
        return True  # unknown size, assume ok and let direct download try
    probe_start = max(0, size - 100)
    hdrs = {
        "User-Agent": _ua(),
        "Range": f"bytes={probe_start}-{size - 1}",
    }
    if ndus:
        hdrs["Cookie"] = f"ndus={ndus}"
    try:
        r = session.get(dlink, headers=hdrs, timeout=10, allow_redirects=True, stream=True)
        r.close()
        if r.status_code == 206:
            cr = r.headers.get("Content-Range", "")
            # Validate: Content-Range should end at size-1
            if f"/{size}" in cr or f"/{size - 1}" in cr or cr.endswith(str(size)):
                logger.info("dlink probe OK (206, range %s) → premium access", cr)
                return True
            # 206 but wrong range → partial/CDN issue, still try
            logger.info("dlink probe 206 but Content-Range=%s — assuming ok", cr)
            return True
        elif r.status_code == 200:
            # Server returned full file for range request — CDN ignores Range header
            # This sometimes means the file is small enough to always serve fully
            if size < 20 * 1024 * 1024:  # < 20 MB, probably ok
                return True
            logger.info("dlink probe got 200 (no range support) for %d MB — assuming truncation", size // 1024 // 1024)
            return False
        else:
            logger.info("dlink probe status=%d — free account, using HLS", r.status_code)
            return False
    except Exception as exc:
        logger.debug("dlink probe error: %s — assuming ok, will detect during download", exc)
        return True  # if probe fails, attempt download and check after


def _terabox_manual_sync(url, dest_dir, ndus, cancel_event, progress_cb) -> list[Path]:
    url   = _resolve_short_url(url)
    surl  = _extract_surl(url)
    if not surl:
        raise DownloadError(f"Cannot extract surl from URL: {url}")

    session   = _build_session(ndus)
    js_token  = _get_js_token(session, surl)
    data      = _get_share_data(session, js_token, surl)

    shareid   = str(data.get("shareid", ""))
    uk        = str(data.get("uk", ""))
    sign      = str(data.get("sign", ""))
    timestamp = str(data.get("timestamp", ""))
    file_list = data.get("list", [])

    if not file_list:
        raise DownloadError("Terabox returned an empty file list")

    results: list[Path] = []

    for item in file_list:
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled()

        fname = safe_filename(item.get("server_filename", "terabox_file"))
        size  = int(item.get("size", 0))
        dlink = item.get("dlink", "")
        fs_id = str(item.get("fs_id", ""))

        if size and size > MAX_DOWNLOAD_SIZE:
            raise DownloadError(f"{fname}: {size / 1024**3:.1f} GB exceeds size limit")

        dest = dest_dir / fname

        # ── Priority 1: share/list dlink retry (dm.terabox.app reliably returns dlinks) ──
        if not dlink:
            dlink = _get_dlink_from_share_list_retry(session, surl, ndus, js_token)
            if dlink:
                logger.info("share/list retry returned dlink for %s", fname)

        # ── Priority 2: Authenticated /api/download → original-quality dlink ──
        if ndus and fs_id and not dlink:
            try:
                logger.info(
                    "Authenticated download: resolving dlink via /api/download for %s (fs_id=%s)",
                    fname, fs_id,
                )
                dlink = _get_dlink_via_api(session, ndus, fs_id)
            except DownloadError as exc:
                logger.warning("Authenticated dlink failed for %s: %s — falling back to HLS", fname, exc)
                dlink = ""

        # ── Priority 3: Full HLS via ffmpeg — PRIMARY path (complete video, all segments) ──
        #
        # Why HLS first?  Terabox's CDN silently truncates direct downloads to
        # ~6 MB for free accounts.  The HLS stream is NOT subject to this limit
        # and delivers every segment (full 19-min video at 1080p).  For premium
        # accounts the direct dlink is better quality, but we cannot reliably
        # detect premium vs free upfront (range probes return 206 for both).
        # HLS is used as the reliable primary path; dlink is kept as a last
        # resort only when HLS is completely unavailable.
        downloaded = False
        if shareid and uk and sign and timestamp and fs_id:
            logger.info("HLS full-stream via ffmpeg (primary): %s", fname)
            try:
                quality, m3u8_content, stream_url = _pick_working_quality(
                    session, shareid, uk, sign, timestamp, fs_id, js_token
                )
                if quality and m3u8_content and stream_url:
                    stem = Path(fname).stem
                    hls_dest = dest_dir / f"{stem}.mp4"
                    _download_hls_with_ffmpeg(stream_url, hls_dest, session, ndus, surl)
                    dest = hls_dest
                    downloaded = True
                    results.append(dest)
                else:
                    logger.warning("HLS not available for %s — will try dlink", fname)
            except Exception as exc:
                logger.warning("HLS download failed (%s) — will try dlink", exc)
                if dest.exists():
                    dest.unlink(missing_ok=True)

        # ── Priority 4: Direct dlink — fallback when HLS unavailable or failed ──
        if not downloaded and dlink:
            logger.info("Direct dlink download (HLS unavailable): %s", dlink[:70])
            try:
                _direct_download(session, dlink, dest, size, progress_cb, cancel_event, ndus=ndus)
                actual = dest.stat().st_size if dest.exists() else 0
                if size and actual < size * 0.9:
                    logger.warning(
                        "dlink truncated: got %d B / expected %d B (free-account CDN limit)",
                        actual, size,
                    )
                # Accept whatever we got (may be partial, but better than nothing)
                if dest.exists() and dest.stat().st_size > 0:
                    results.append(dest)
                    downloaded = True
            except Exception as exc:
                logger.warning("Direct dlink also failed: %s", exc)
                if dest.exists():
                    dest.unlink(missing_ok=True)

        if not downloaded:
            logger.warning("All download methods exhausted for %s — skipping", fname)

    if not results:
        raise DownloadError("No files could be downloaded from this Terabox share")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Authenticated download — RC4 signature + /api/download → real dlink
# (reverse-engineered from seiya-npm/terabox-api)
# ══════════════════════════════════════════════════════════════════════════════

def _sign_download(s1: str, s2: str) -> str:
    """
    RC4-like stream cipher used by Terabox to sign download requests.
    Translated 1-to-1 from seiya-npm/terabox-api signDownload(s1, s2).
      s1 = sign3  (secret key)
      s2 = sign1  (data to sign)
    Returns Base64-encoded signature (signb).
    """
    import base64
    p = list(range(256))
    a = [ord(s1[i % len(s1)]) for i in range(256)]

    j = 0
    for i in range(256):
        j = (j + p[i] + a[i]) % 256
        p[i], p[j] = p[j], p[i]

    result = []
    i = j = 0
    for q in range(len(s2)):
        i = (i + 1) % 256
        j = (j + p[i]) % 256
        p[i], p[j] = p[j], p[i]
        k = p[(p[i] + p[j]) % 256]
        result.append(ord(s2[q]) ^ k)

    return base64.b64encode(bytes(result)).decode()


def _get_home_info_sign(session: requests.Session, ndus: str) -> tuple[str, str, str]:
    """
    GET /api/home/info with authenticated cookie.
    Returns (sign1, sign3, timestamp) needed to compute the download signature.
    """
    for base in [
        "https://www.1024tera.com",
        BASE_URL,
        "https://www.terabox.com",
        "https://www.1024terabox.com",
    ]:
        try:
            hdrs = {
                "User-Agent": _ua(),
                "Cookie": f"ndus={ndus}",
                "Referer": f"{base}/",
            }
            r = session.get(f"{base}/api/home/info", headers=hdrs, timeout=20)
            if r.status_code != 200:
                continue
            d = r.json()
            if d.get("errno") == 0 and "data" in d:
                data  = d["data"]
                sign1 = str(data.get("sign1", ""))
                sign3 = str(data.get("sign3", ""))
                ts    = str(data.get("timestamp", ""))
                if sign1 and sign3 and ts:
                    logger.info("home/info OK from %s (ts=%s)", base, ts)
                    return sign1, sign3, ts
            else:
                logger.debug("home/info errno=%s from %s", d.get("errno"), base)
        except Exception as exc:
            logger.debug("home/info [%s]: %s", base, exc)

    raise DownloadError(
        "Could not get signing data from /api/home/info — "
        "check that your TERABOX_NDUS cookie is valid and not expired."
    )


def _get_dlink_via_api(
    session: requests.Session,
    ndus: str,
    fs_id: str,
) -> str:
    """
    Full authenticated dlink resolution (seiya-npm flow):
      1. GET /api/home/info       → sign1, sign3, timestamp
      2. signb = _sign_download(sign3, sign1)   ← RC4-based
      3. POST /api/download       → dlink (original-quality direct URL)

    The returned dlink is a CDN URL that must be fetched with the ndus cookie.
    """
    sign1, sign3, timestamp = _get_home_info_sign(session, ndus)
    signb = _sign_download(sign3, sign1)
    logger.info("signb computed (len=%d) for fs_id=%s", len(signb), fs_id)

    for base in [
        "https://www.1024tera.com",
        BASE_URL,
        "https://www.terabox.com",
        "https://www.1024terabox.com",
    ]:
        try:
            hdrs = {
                "User-Agent": _ua(),
                "Cookie": f"ndus={ndus}",
                "Referer": f"{base}/",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            payload = {
                "app_id": "250528",
                "web": "1",
                "channel": "dubox",
                "clienttype": "0",
                "fidlist": f"[{fs_id}]",
                "type": "dlink",
                "vip": "2",
                "sign": signb,
                "timestamp": timestamp,
                "need_speed": "1",
            }
            r = session.post(
                f"{base}/api/download",
                data=payload,
                headers=hdrs,
                timeout=30,
            )
            if r.status_code != 200:
                logger.debug("api/download [%s] HTTP %s", base, r.status_code)
                continue
            d = r.json()
            if d.get("errno") == 0:
                dlinks = d.get("dlink", [])
                if dlinks and isinstance(dlinks, list):
                    dl = dlinks[0].get("dlink", "")
                    if dl:
                        logger.info(
                            "Authenticated dlink acquired from %s for fs_id=%s", base, fs_id
                        )
                        return dl
            else:
                logger.debug(
                    "api/download [%s] errno=%s: %s",
                    base, d.get("errno"), d.get("errmsg", ""),
                )
        except Exception as exc:
            logger.debug("api/download [%s]: %s", base, exc)

    raise DownloadError(
        "Could not get original-quality dlink from Terabox /api/download. "
        "Make sure your TERABOX_NDUS cookie belongs to a logged-in account "
        "that can access this share."
    )


def _direct_download(
    session, url, dest, total_hint, progress_cb, cancel_event,
    ndus: str | None = None,
) -> None:
    try:
        head_hdrs = {"User-Agent": _ua()}
        if ndus:
            head_hdrs["Cookie"] = f"ndus={ndus}"
        r = session.head(url, timeout=15, allow_redirects=True, headers=head_hdrs)
        total = int(r.headers.get("Content-Length", 0))
    except Exception:
        total = 0

    if total == 0:
        total = total_hint

    parsed   = urlparse(url)
    get_hdrs = {
        "User-Agent": _ua(),
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
    }
    if ndus:
        get_hdrs["Cookie"] = f"ndus={ndus}"

    done = 0
    with session.get(url, headers=get_hdrs, stream=True, timeout=300) as r:
        r.raise_for_status()
        if total == 0:
            total = int(r.headers.get("Content-Length", 0))
        with open(dest, "wb") as f:
            for chunk in r.iter_content(CHUNK_SIZE):
                if cancel_event and cancel_event.is_set():
                    raise DownloadCancelled()
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total or done)


# ══════════════════════════════════════════════════════════════════════════════
# Main sync entry point
# ══════════════════════════════════════════════════════════════════════════════

def _terabox_download_sync(url, dest_dir, ndus, cancel_event, progress_cb) -> list[Path]:
    # ── When NDUS cookie is set: use authenticated API → original quality ──
    # yt-dlp with a Terabox cookie still only fetches HLS/transcoded streams.
    # The seiya-npm /api/download flow is the only path to the original file.
    if ndus:
        logger.info("Terabox: TERABOX_NDUS set — using authenticated /api/download for original quality")
        try:
            return _terabox_manual_sync(url, dest_dir, ndus, cancel_event, progress_cb)
        except DownloadCancelled:
            raise
        except Exception as e:
            logger.warning("Authenticated manual sync failed (%s)", e)
            raise

    # ── No cookie: try yt-dlp (gets HLS/transcoded quality) ──
    if HAS_YTDLP:
        try:
            logger.info("Terabox: trying yt-dlp method (no auth cookie)")
            results = _ytdlp_download(url, dest_dir, cancel_event, progress_cb)
            logger.info("yt-dlp download succeeded: %s", [str(p) for p in results])
            return results
        except DownloadCancelled:
            raise
        except Exception as e:
            logger.warning("yt-dlp method failed (%s), falling back to manual HLS", e)
    else:
        logger.warning("yt-dlp not available — install it: pip install yt-dlp")

    # ── Final fallback: manual API + HLS ──
    logger.info("Terabox: using manual HLS fallback (no auth)")
    return _terabox_manual_sync(url, dest_dir, ndus, cancel_event, progress_cb)


# ══════════════════════════════════════════════════════════════════════════════
# Downloader class
# ══════════════════════════════════════════════════════════════════════════════

class TeraboxDownloader(BaseDownloader):
    ENGINE_NAME = "terabox"

    async def download(self) -> list[Path]:
        self._log("Starting Terabox download: %s", self.url)

        # Get ndus from env var OR from the cookie file
        ndus = os.getenv("TERABOX_NDUS") or None
        if not ndus:
            from config import TERABOX_NDUS
            ndus = TERABOX_NDUS or None
        cancel_event = threading.Event()

        async def _watch() -> None:
            while not cancel_event.is_set():
                try:
                    await self._check_cancelled()
                except DownloadCancelled:
                    cancel_event.set()
                    return
                await asyncio.sleep(0.5)

        watcher = asyncio.ensure_future(_watch())

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        def _sync_progress(done: int, total: int) -> None:
            if self.progress_cb and loop:
                try:
                    asyncio.run_coroutine_threadsafe(self.progress_cb(done, total), loop)
                except Exception:
                    pass

        try:
            results = await asyncio.to_thread(
                _terabox_download_sync,
                self.url,
                self.dest_dir,
                ndus,
                cancel_event,
                _sync_progress if self.progress_cb else None,
            )
        except DownloadCancelled:
            raise
        finally:
            cancel_event.set()
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, DownloadCancelled):
                pass

        return results
