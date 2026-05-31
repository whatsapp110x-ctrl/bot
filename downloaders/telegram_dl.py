"""
Telegram message/file downloader.

Downloads media from a t.me/channel/messageid link using the Pyrogram client.
Taken from ColabLeechBot's downloader/telegram.py, made async-clean.
"""

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrogram import Client

from .base import BaseDownloader, DownloadError, DownloadCancelled, ProgressCallback

logger = logging.getLogger(__name__)

TG_LINK_PATTERN = re.compile(r"(?:https?://)?t\.me/([^/]+)/(\d+)", re.I)


def is_telegram_url(url: str) -> bool:
    return bool(TG_LINK_PATTERN.search(url))


class TelegramDownloader(BaseDownloader):
    ENGINE_NAME = "telegram"

    def __init__(self, url: str, client: "Client", **kwargs) -> None:
        super().__init__(url, **kwargs)
        self.client = client

    async def download(self) -> list[Path]:
        m = TG_LINK_PATTERN.search(self.url)
        if not m:
            raise DownloadError(f"Not a valid Telegram message link: {self.url}")

        channel, msg_id = m.group(1), int(m.group(2))
        self._log("Fetching message %s/%s", channel, msg_id)

        message = await self.client.get_messages(channel, msg_id)
        if not message or not message.media:
            raise DownloadError("Message has no downloadable media")

        dest = self.dest_dir
        dest.mkdir(parents=True, exist_ok=True)

        file_path = await self.client.download_media(
            message,
            file_name=str(dest / (self.custom_filename or "")),
            progress=self._pyrogram_progress,
        )

        if not file_path:
            raise DownloadError("Pyrogram returned no file path")

        return [Path(file_path)]

    async def _pyrogram_progress(self, current: int, total: int) -> None:
        await self._check_cancelled()
        await self._report_progress(current, total)
