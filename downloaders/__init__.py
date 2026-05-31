"""
Download engine registry — auto-routes URLs to the best engine.
"""

import re

from .base import BaseDownloader, DownloadError, DownloadCancelled, ProgressCallback
from .ytdl import YtdlDownloader, is_ytdl_url
from .direct import DirectDownloader, is_direct_url
from .gdrive import GDriveDownloader, is_gdrive_url
from .terabox import TeraboxDownloader, is_terabox_url
from .telegram_dl import TelegramDownloader, is_telegram_url
from .torrent import TorrentDownloader, is_torrent_url
from .aria2 import Aria2Downloader, aria2c_available
from .instagram import InstagramDownloader, is_instagram_url
from .snapchat import SnapchatDownloader, is_snapchat_url
from .twitter import TwitterDownloader, is_twitter_url


def resolve_downloader(url: str) -> type[BaseDownloader]:
    """
    Return the most appropriate downloader class for a URL.

    Priority order:
      1. Telegram message links
      2. Google Drive
      3. Terabox (all mirror domains)
      4. Torrent / magnet
      5. Snapchat   — dedicated downloader (yt-dlp CDN tokens expire too fast)
      6. Instagram  — yt-dlp primary + instaloader fallback
      7. Twitter/X  — dedicated downloader with guest-token fallback
      8. yt-dlp     — 2000+ sites including TikTok, YouTube, Reddit, Facebook, etc.
      9. aria2c / direct HTTP
    """
    if is_telegram_url(url):
        return TelegramDownloader
    if is_gdrive_url(url):
        return GDriveDownloader
    if is_terabox_url(url):
        return TeraboxDownloader
    if is_torrent_url(url):
        return TorrentDownloader
    if is_snapchat_url(url):
        return SnapchatDownloader
    if is_instagram_url(url):
        return InstagramDownloader
    if is_twitter_url(url):
        return TwitterDownloader
    if is_ytdl_url(url):
        return YtdlDownloader
    # For direct HTTP links, use aria2c if available (faster), else aiohttp
    if is_direct_url(url):
        return Aria2Downloader if aria2c_available() else DirectDownloader
    # Ultimate fallback: try yt-dlp on anything
    return YtdlDownloader


__all__ = [
    "BaseDownloader", "DownloadError", "DownloadCancelled", "ProgressCallback",
    "YtdlDownloader", "DirectDownloader", "GDriveDownloader",
    "TeraboxDownloader", "TelegramDownloader", "TorrentDownloader",
    "Aria2Downloader", "InstagramDownloader", "SnapchatDownloader",
    "TwitterDownloader", "resolve_downloader",
]
