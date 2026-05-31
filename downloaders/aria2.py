"""
aria2c downloader — fastest possible HTTP/HTTPS/FTP/torrent downloads.

aria2c splits files into 16 parallel connections by default, achieving
maximum bandwidth utilization. Falls back to DirectDownloader if aria2
is not available.

Requires: aria2 installed (apt install aria2 / brew install aria2)
"""

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse as _urlparse

from config import ARIA2_CONNECTIONS, ARIA2_MAX_SPLITS, DOWNLOAD_DIR, USE_ARIA2
from utils.file_utils import safe_filename
from .base import BaseDownloader, DownloadError, DownloadCancelled, ProgressCallback

logger = logging.getLogger(__name__)


def aria2c_available() -> bool:
    """Check if aria2c binary is on PATH."""
    return shutil.which("aria2c") is not None


class Aria2Downloader(BaseDownloader):
    """
    Downloads via aria2c subprocess for maximum speed.
    Supports HTTP, HTTPS, FTP, and magnet links.
    """

    ENGINE_NAME = "aria2c"

    async def download(self) -> list[Path]:
        if not aria2c_available():
            logger.warning("aria2c not found — falling back to DirectDownloader")
            from .direct import DirectDownloader
            dl = DirectDownloader(
                self.url,
                dest_dir=self.dest_dir,
                progress_cb=self.progress_cb,
                custom_filename=self.custom_filename,
            )
            dl._cancel_event = self._cancel_event
            return await dl.download()

        self._log("Starting aria2c download: %s", self.url)
        tempdir = self._make_tempdir()

        _parsed = _urlparse(self.url)
        _origin = f"{_parsed.scheme}://{_parsed.netloc}"

        cmd = [
            "aria2c",
            "--dir", str(tempdir),
            "--max-connection-per-server", str(ARIA2_CONNECTIONS),
            "--split", str(ARIA2_MAX_SPLITS),
            "--min-split-size=1M",
            "--file-allocation=none",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--retry-wait=3",
            "--max-tries=5",
            "--connect-timeout=30",
            "--timeout=60",
            "--quiet",
            "--console-log-level=error",
            "--summary-interval=2",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            f"--referer={_origin}/",
        ]

        if self.custom_filename:
            cmd += ["--out", safe_filename(self.custom_filename)]

        cmd.append(self.url)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Monitor progress by watching directory size
        monitor_task = asyncio.create_task(
            self._monitor_progress(tempdir, proc)
        )

        try:
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            proc.terminate()
            await proc.wait()
            raise DownloadCancelled("aria2c cancelled")
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            # Code 22 = HTTP server returned error (4xx/5xx) — fall back to aiohttp
            # Code 9  = resource not found / permission denied  — fall back too
            if proc.returncode in (22, 9):
                self._log(
                    "aria2c HTTP error (code %d) — falling back to DirectDownloader",
                    proc.returncode,
                )
                from .direct import DirectDownloader
                dl = DirectDownloader(
                    self.url,
                    dest_dir=self.dest_dir,
                    progress_cb=self.progress_cb,
                    custom_filename=self.custom_filename,
                )
                dl._cancel_event = self._cancel_event
                return await dl.download()
            raise DownloadError(f"aria2c failed (code {proc.returncode}): {err}")

        files = [f for f in tempdir.rglob("*") if f.is_file() and not f.name.endswith(".aria2")]
        if not files:
            raise DownloadError("aria2c produced no output files")

        # Return files directly from tempdir — DO NOT move to dest_dir.
        # Keeping them inside tgbot_* ensures cleanup in handlers.py finds them.
        self._log("aria2c downloaded %d file(s)", len(files))
        return files

    async def _monitor_progress(self, tempdir: Path, proc: asyncio.subprocess.Process) -> None:
        """Poll directory size to report rough progress."""
        while proc.returncode is None:
            await asyncio.sleep(3)
            if self._cancel_event.is_set():
                proc.terminate()
                return
            try:
                total_downloaded = sum(
                    f.stat().st_size
                    for f in tempdir.rglob("*")
                    if f.is_file()
                )
                await self._report_progress(total_downloaded, max(total_downloaded, 1))
            except Exception:
                pass
