"""
Google Drive downloader.

Handles both file and folder links. Uses gdown for simple cases,
falls back to the Drive API for larger files or private content.
"""

import asyncio
import logging
from pathlib import Path

from .base import BaseDownloader, DownloadError, DownloadCancelled
from config import GDRIVE_CREDENTIALS_FILE, GDRIVE_TOKEN_FILE

logger = logging.getLogger(__name__)

GDRIVE_PATTERN = "drive.google.com"


def is_gdrive_url(url: str) -> bool:
    return GDRIVE_PATTERN in url


def _extract_file_id(url: str) -> str | None:
    """Extract Google Drive file/folder ID from various URL formats."""
    import re
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"/folders/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
        r"/open\?id=([a-zA-Z0-9_-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


class GDriveDownloader(BaseDownloader):
    ENGINE_NAME = "gdrive"

    async def download(self) -> list[Path]:
        self._log("Starting Google Drive download: %s", self.url)
        tempdir = self._make_tempdir()

        loop = asyncio.get_running_loop()
        try:
            files = await loop.run_in_executor(None, self._sync_download, tempdir)
        except Exception as exc:
            raise DownloadError(f"Google Drive download failed: {exc}") from exc

        return files

    def _sync_download(self, dest: Path) -> list[Path]:
        try:
            import gdown  # type: ignore
        except ImportError:
            raise DownloadError("gdown is not installed. Add it to requirements.txt")

        file_id = _extract_file_id(self.url)
        if not file_id:
            raise DownloadError(f"Could not extract file ID from: {self.url}")

        if "folders" in self.url:
            output = str(dest)
            gdown.download_folder(id=file_id, output=output, quiet=True, use_cookies=False)
            files = [f for f in dest.rglob("*") if f.is_file()]
            if not files:
                raise DownloadError("Google Drive folder download returned no files")
            return files
        else:
            output = str(dest / (self.custom_filename or f"{file_id}"))
            # Build a direct download URL — works across all gdown versions
            direct_url = f"https://drive.google.com/uc?id={file_id}&export=download"
            result = None
            # Try URL-based download first (no fuzzy kwarg needed)
            try:
                result = gdown.download(url=direct_url, output=output, quiet=False)
            except TypeError:
                # Older gdown API: positional url argument
                result = gdown.download(direct_url, output, quiet=False)
            except Exception as exc:
                logger.warning("gdown URL download failed: %s — trying id=", exc)

            # Fallback: id-based download
            if not result:
                try:
                    result = gdown.download(id=file_id, output=output, quiet=False)
                except Exception as exc:
                    raise DownloadError(f"gdown failed: {exc}")

            if result:
                p = Path(result)
                if p.exists() and p.stat().st_size > 0:
                    return [p]
            raise DownloadError("gdown returned no file path — file may be private or deleted")
