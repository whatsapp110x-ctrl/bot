"""
Abstract base class for all download engines.

Architecture taken from Media-Downloader-Bot's BaseDownloader ABC,
significantly cleaned up and made framework-agnostic.
"""

import asyncio
import logging
import shutil
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Awaitable

from config import DOWNLOAD_DIR

logger = logging.getLogger(__name__)

# Minimum free space required before starting any download (100 MB)
_MIN_FREE_BYTES = 100 * 1024 * 1024


def _purge_stale_tempdirs(base: Path, max_age_seconds: int = 1800) -> int:
    """Remove tgbot_* temp dirs older than max_age_seconds. Returns count purged."""
    purged = 0
    cutoff = time.time() - max_age_seconds
    for scan in {base, Path("/tmp")}:
        try:
            for d in scan.iterdir():
                if d.is_dir() and d.name.startswith("tgbot_"):
                    try:
                        if d.stat().st_mtime < cutoff:
                            shutil.rmtree(d, ignore_errors=True)
                            purged += 1
                    except Exception:
                        pass
        except Exception:
            pass
    return purged


def _check_disk_space(path: Path) -> None:
    """Raise DownloadError if free disk space on path's filesystem is too low.
    Tries to purge stale temp dirs first to free up space."""
    try:
        usage = shutil.disk_usage(path)
        if usage.free < _MIN_FREE_BYTES:
            # Try emergency cleanup before giving up
            purged = _purge_stale_tempdirs(path, max_age_seconds=300)
            if purged:
                logger.warning("Emergency cleanup: purged %d stale temp dir(s)", purged)
                usage = shutil.disk_usage(path)
            if usage.free < _MIN_FREE_BYTES:
                free_mb = usage.free // (1024 * 1024)
                raise Exception(
                    f"Not enough disk space to download — only {free_mb} MB free "
                    f"(need at least {_MIN_FREE_BYTES // (1024*1024)} MB). "
                    "Send /cancel to free up space or try again later."
                )
    except OSError:
        pass  # can't check — allow download to proceed

# Type alias for a progress callback: (done_bytes, total_bytes) -> None
ProgressCallback = Callable[[int, int], Awaitable[None]]


class DownloadError(Exception):
    """Raised when a download fails in a handled way."""


class DownloadCancelled(Exception):
    """Raised when the user cancels an in-progress download."""


class BaseDownloader(ABC):
    """
    Every downloader subclass must implement `download()`.

    Features provided by the base:
    - A per-task temp directory that auto-cleans on failure
    - A cancellation token (asyncio.Event)
    - A standard progress-callback interface
    - Logging with task ID for correlation
    """

    ENGINE_NAME: str = "generic"

    def __init__(
        self,
        url: str,
        dest_dir: Path | None = None,
        progress_cb: ProgressCallback | None = None,
        custom_filename: str | None = None,
    ) -> None:
        self.url = url
        self.dest_dir = dest_dir or DOWNLOAD_DIR
        self.progress_cb = progress_cb
        self.custom_filename = custom_filename
        self.task_id = uuid.uuid4().hex[:8]
        self._cancel_event = asyncio.Event()
        self._tempdir: tempfile.TemporaryDirectory | None = None
        # thumbnail map: video_stem_lowercase → thumbnail Path
        # Populated by downloaders that save thumbnails alongside video files.
        # The uploader uses this to embed the thumbnail instead of sending it separately.
        self.thumbnails: dict[str, Path] = {}

        _check_disk_space(Path("/tmp"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @abstractmethod
    async def download(self) -> list[Path]:
        """
        Execute the download.

        Returns:
            List of Path objects pointing to the downloaded file(s).
            Multiple files are returned for playlists or multi-part content.

        Raises:
            DownloadError: on unrecoverable failure
            DownloadCancelled: when cancelled by the user
        """

    def cancel(self) -> None:
        """Signal this download to stop as soon as possible."""
        self._cancel_event.set()
        logger.info("[%s] Cancellation requested", self.task_id)

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # ------------------------------------------------------------------
    # Helpers for subclasses
    # ------------------------------------------------------------------

    def _log(self, msg: str, *args) -> None:
        logger.info(f"[{self.task_id}][{self.ENGINE_NAME}] {msg}", *args)

    def _err(self, msg: str, *args) -> None:
        logger.error(f"[{self.task_id}][{self.ENGINE_NAME}] {msg}", *args)

    async def _report_progress(self, done: int, total: int) -> None:
        if self.progress_cb:
            try:
                await self.progress_cb(done, total)
            except Exception:
                pass  # Never let a progress callback kill a download

    async def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise DownloadCancelled("Task was cancelled")

    def _make_tempdir(self) -> Path:
        """Create an isolated temp directory in the system /tmp for this task."""
        td = tempfile.mkdtemp(prefix=f"tgbot_{self.task_id}_")
        return Path(td)
