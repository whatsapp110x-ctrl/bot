"""
Torrent / magnet-link downloader.

Uses aria2c with full DHT and tracker support.

Supports:
- Magnet links (magnet:?xt=urn:btih:...)
- .torrent files (direct URL to a .torrent)
- Both single-file and multi-file torrents
"""

import asyncio
import logging
import re
import shutil
from pathlib import Path

from config import DOWNLOAD_DIR
from .base import BaseDownloader, DownloadError, DownloadCancelled
from .aria2 import aria2c_available

logger = logging.getLogger(__name__)

MAGNET_RE = re.compile(r"magnet:\?", re.I)
TORRENT_RE = re.compile(r"https?://.*\.torrent(\?.*)?$", re.I)


def is_torrent_url(url: str) -> bool:
    return bool(MAGNET_RE.match(url) or TORRENT_RE.search(url))


class TorrentDownloader(BaseDownloader):
    ENGINE_NAME = "torrent"

    # Stall timeout: stop if no new data for 5 minutes
    STALL_TIMEOUT = 300
    # Global timeout: 12 hours for very large torrents
    GLOBAL_TIMEOUT = 43200

    async def download(self) -> list[Path]:
        if not aria2c_available():
            raise DownloadError(
                "aria2c is required for torrent downloads.\n"
                "It should be installed — try restarting the bot."
            )

        self._log("Starting torrent download: %s", self.url[:80])
        tempdir = self._make_tempdir()

        trackers = ",".join([
            "udp://tracker.opentrackr.org:1337/announce",
            "udp://open.tracker.cl:1337/announce",
            "udp://tracker.openbittorrent.com:6969/announce",
            "udp://opentracker.i2p.rocks:6969/announce",
            "udp://tracker.internetwarriors.net:1337/announce",
            "udp://tracker.torrent.eu.org:451/announce",
        ])

        cmd = [
            "aria2c",
            f"--dir={tempdir}",
            "--seed-time=0",                        # Never seed — just download
            f"--bt-stop-timeout={self.STALL_TIMEOUT}",
            "--max-connection-per-server=8",
            "--min-split-size=1M",
            "--split=8",
            "--file-allocation=none",
            "--allow-overwrite=true",               # =value syntax — avoids "true" as URI bug
            # DHT & peer discovery (all boolean flags use =value syntax)
            "--enable-dht=true",
            "--enable-dht6=false",
            "--bt-enable-lpd=true",
            "--enable-peer-exchange=true",
            "--bt-max-peers=100",
            # Tracker settings
            "--bt-tracker-connect-timeout=20",
            "--bt-tracker-timeout=30",
            "--bt-tracker-interval=30",
            f"--bt-tracker={trackers}",
            "--console-log-level=warn",
        ]
        cmd.append(self.url)

        self._log("Running: aria2c %s …", self.url[:60])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        monitor_task = asyncio.create_task(self._monitor(tempdir, proc))

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.GLOBAL_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.terminate()
            await proc.wait()
            raise DownloadError("Torrent download timed out (12h limit)")
        except asyncio.CancelledError:
            proc.terminate()
            await proc.wait()
            raise DownloadCancelled("Torrent cancelled")
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            out = stdout.decode(errors="replace").strip()
            detail = err or out or "(no output)"
            raise DownloadError(f"aria2c torrent failed (code {proc.returncode}): {detail}")

        files = sorted(
            [f for f in tempdir.rglob("*") if f.is_file() and not f.name.endswith(".aria2")],
            key=lambda f: f.stat().st_size,
            reverse=True,
        )

        if not files:
            raise DownloadError(
                "Torrent produced no files.\n"
                "Possible causes:\n"
                "• Magnet link has no seeders (dead torrent)\n"
                "• UDP port 6881 is blocked by the network\n"
                "• The torrent file is invalid"
            )

        result = []
        for f in files:
            dest = self.dest_dir / f.name
            try:
                f.rename(dest)
            except OSError:
                import shutil as _sh
                _sh.copy2(f, dest)
                f.unlink()
            result.append(dest)

        self._log("Torrent: downloaded %d file(s)", len(result))
        return result

    async def _monitor(self, tempdir: Path, proc: asyncio.subprocess.Process) -> None:
        while proc.returncode is None:
            await asyncio.sleep(5)
            if self._cancel_event.is_set():
                proc.terminate()
                return
            try:
                size = sum(f.stat().st_size for f in tempdir.rglob("*") if f.is_file())
                await self._report_progress(size, max(size, 1))
            except Exception:
                pass
