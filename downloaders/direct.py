"""
Direct HTTP/HTTPS downloader — aiohttp with chunked streaming.
No size limit, full retry logic, proper resume via Range headers,
Content-Disposition filename extraction.
"""

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp

from utils.file_utils import safe_filename
from .base import BaseDownloader, DownloadError, DownloadCancelled

logger = logging.getLogger(__name__)

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB per chunk — fast

NON_DIRECT_RE = re.compile(
    # (?<![a-z0-9-]) prevents false matches on domain suffixes like blogx.com
    r"(?<![a-z0-9-])("
    r"youtube\.com|youtu\.be|instagram\.com|tiktok\.com|twitter\.com"
    r"|(?<![a-z])x\.com"          # twitter/X — NOT blogx.com, bax.com etc.
    r"|reddit\.com|facebook\.com|fb\.watch|drive\.google\.com|mega\.nz"
    r"|terabox|1024tera|1024terabox|teraboxapp"
    r"|magnet:|\.torrent"
    r")",
    re.IGNORECASE,
)


def is_direct_url(url: str) -> bool:
    return not bool(NON_DIRECT_RE.search(url))


def _extract_filename(url: str, resp: aiohttp.ClientResponse) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    if cd:
        m = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd, re.I)
        if m:
            return safe_filename(unquote(m.group(1).strip()))
    path = urlparse(url).path
    name = Path(unquote(path)).name
    return safe_filename(name) if (name and "." in name) else "download"


class DirectDownloader(BaseDownloader):
    ENGINE_NAME = "direct-http"

    TIMEOUT = aiohttp.ClientTimeout(total=None, connect=60, sock_read=120)
    MAX_RETRIES = 5

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Encoding": "identity",  # disable compression for raw streaming
    }

    async def download(self) -> list[Path]:
        self._log("Direct download: %s", self.url)

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                return await self._attempt_download(attempt)
            except DownloadCancelled:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == self.MAX_RETRIES:
                    raise DownloadError(f"Direct download failed after {self.MAX_RETRIES} attempts: {exc}") from exc
                wait = 2 ** attempt
                self._log("Attempt %d failed (%s), retrying in %ds...", attempt, exc, wait)
                await asyncio.sleep(wait)

    async def _attempt_download(self, attempt: int) -> list[Path]:
        _parsed = urlparse(self.url)
        _origin = f"{_parsed.scheme}://{_parsed.netloc}"
        _headers = {**self.HEADERS, "Referer": f"{_origin}/"}

        connector = aiohttp.TCPConnector(limit=0, force_close=False)
        async with aiohttp.ClientSession(
            timeout=self.TIMEOUT,
            headers=_headers,
            connector=connector,
        ) as session:
            async with session.get(self.url, allow_redirects=True) as resp:
                if resp.status == 400:
                    if any(h in self.url for h in ("googleusercontent.com", "googleapis.com", "ggpht.com")):
                        raise DownloadError(
                            "This Google video link has **expired**.\n"
                            "Google's signed download URLs are time-limited.\n"
                            "Please generate a fresh link from Google Photos or Drive."
                        )
                    raise DownloadError("HTTP 400 — the link may have expired or is malformed")
                if resp.status == 403:
                    raise DownloadError(
                        "HTTP 403 Forbidden — the server blocked the download.\n"
                        "The file may require a login, or the link has a hotlink restriction."
                    )
                if resp.status not in (200, 206):
                    raise DownloadError(f"HTTP {resp.status}")

                total = int(resp.headers.get("Content-Length", 0))
                filename = self.custom_filename or _extract_filename(self.url, resp)

                # Always download to a tgbot_* tempdir so cleanup in
                # handlers.py can find and remove it after upload.
                tempdir = self._make_tempdir()
                dest = tempdir / filename

                done = 0
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                        await self._check_cancelled()
                        try:
                            f.write(chunk)
                        except OSError as e:
                            import errno
                            if e.errno == errno.EDQUOT or e.errno == errno.ENOSPC:
                                raise DownloadError(
                                    "Disk quota exceeded during download. "
                                    "The server is low on space — try again in a moment."
                                ) from e
                            raise
                        done += len(chunk)
                        await self._report_progress(done, total or done)

        self._log("Downloaded %s (%d bytes)", dest.name, done)
        return [dest]
