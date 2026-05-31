"""
Core message handlers — URL detection, task dispatch, progress updates.
No artificial limits — any URL, any size, any platform.
"""

import asyncio
import logging
import re
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from config import OWNER_IDS, PRIVATE_MODE, AUTHORIZED_IDS
from core.task_manager import task_manager
from database import get_or_create_user, get_user_settings, log_download
from downloaders import (
    resolve_downloader, DownloadError, DownloadCancelled,
    TelegramDownloader, YtdlDownloader
)
from downloaders.terabox import is_terabox_url
from uploaders import TelegramUploader
from utils.rate_limiter import rate_limiter
from utils.formatters import build_progress_message, format_size
from utils.file_utils import cleanup_path, extract_archive, create_zip

logger = logging.getLogger(__name__)

EDIT_THROTTLE = 3.0  # seconds between progress edits

URL_RE = re.compile(
    r"(?:https?://[^\s]+|magnet:\?[^\s]+)",
    re.IGNORECASE,
)

def _needs_quality_keyboard(urls: list[str]) -> bool:
    """
    Return True only if ALL URLs are YouTube/Vimeo links where quality
    selection actually matters.  Everything else auto-downloads.
    """
    from downloaders import YtdlDownloader

    for url in urls:
        # Terabox, direct, magnets, torrents → no quality selector
        try:
            Dl = resolve_downloader(url)
        except Exception:
            return False
        if Dl is not YtdlDownloader:
            return False
        # YtdlDownloader handles 1800+ sites; only show quality selector
        # for YouTube and Vimeo where it actually makes sense.
        if "youtube.com" not in url and "youtu.be" not in url and "vimeo.com" not in url:
            return False
    return True


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def is_authorized(user_id: int) -> bool:
    if user_id in OWNER_IDS:
        return True
    if not PRIVATE_MODE:
        return True
    return user_id in AUTHORIZED_IDS


# ---------------------------------------------------------------------------
# Progress callback factory
# ---------------------------------------------------------------------------

def make_progress_cb(status_msg: Message, filename: str, engine: str):
    last_edit = [0.0]
    start_time = [time.monotonic()]

    async def cb(done: int, total: int) -> None:
        now = time.monotonic()
        if now - last_edit[0] < EDIT_THROTTLE:
            return
        last_edit[0] = now
        elapsed = max(now - start_time[0], 0.1)
        speed = done / elapsed
        text = build_progress_message("📥 Downloading", filename, done, total, speed, elapsed, engine)
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    return cb


# ---------------------------------------------------------------------------
# Full download → process → upload pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(
    client: Client,
    user_message: Message,
    status_msg: Message,
    url: str,
    task_options: dict,
) -> None:
    user_id = user_message.from_user.id
    settings = await get_user_settings(user_id)

    # Resolve per-user or per-task options
    quality = task_options.get("quality") or (settings.video_quality if settings else "best")
    output_format = task_options.get("output_format") or (settings.output_format if settings else "video")
    audio_format = task_options.get("audio_format") or (settings.audio_format if settings else "m4a")
    subtitles = task_options.get("subtitles", False) or (settings.subtitles if settings else False)
    send_as_doc = task_options.get("send_as_document", False) or (settings.send_as_document if settings else False)
    playlist = task_options.get("playlist", False)
    sponsorblock = task_options.get("sponsorblock", False)
    upload_type = task_options.get("upload_type", "normal")
    prefix = (settings.filename_prefix if settings else "") or ""
    suffix = (settings.filename_suffix if settings else "") or ""
    caption_style = (settings.caption_style if settings else "bold")
    custom_thumb = Path(settings.custom_thumbnail) if settings and settings.custom_thumbnail else None

    # Pick downloader — _force_cls lets commands bypass auto-routing
    force_cls = task_options.pop("_force_cls", None)
    DownloaderCls = force_cls if force_cls is not None else resolve_downloader(url)

    dl_kwargs: dict = {
        "url": url,
        "custom_filename": task_options.get("custom_filename"),
    }
    if DownloaderCls.__name__ == "TelegramDownloader":
        dl_kwargs["client"] = client
    elif DownloaderCls.__name__ == "YtdlDownloader":
        dl_kwargs.update(
            quality=quality,
            output_format=output_format,
            audio_format=audio_format,
            subtitles=subtitles,
            playlist=playlist,
            sponsorblock=sponsorblock,
        )
    elif DownloaderCls.__name__ == "TwitterDownloader":
        dl_kwargs["audio_only"] = (output_format == "audio")

    downloader = DownloaderCls(**dl_kwargs)

    # Attach progress callback
    downloader.progress_cb = make_progress_cb(status_msg, url[:50], downloader.ENGINE_NAME)

    # Register downloader for cancellation
    task_manager._downloaders[user_id] = downloader

    downloaded_files: list[Path] = []
    final_files: list[Path] = []

    try:
        await status_msg.edit_text(
            f"🔍 **Resolving…**\n"
            f"`{url[:80]}`\n"
            f"Engine: `{downloader.ENGINE_NAME}`"
        )
        downloaded_files = await downloader.download()

        if not downloaded_files:
            await status_msg.edit_text("❌ No files were downloaded.")
            return

        total_bytes = sum(f.stat().st_size for f in downloaded_files if f.is_file())
        await status_msg.edit_text(
            f"✅ **Downloaded** `{len(downloaded_files)}` file(s) — `{format_size(total_bytes)}`\n"
            f"Processing…"
        )

        # --- Post-processing ---
        if upload_type == "zip":
            dest_zip = downloaded_files[0].parent / (downloaded_files[0].stem + "_archive.zip")
            source = downloaded_files[0].parent if len(downloaded_files) > 1 else downloaded_files[0]
            src = source if source.is_dir() else source.parent
            await create_zip(src, dest_zip, password=task_options.get("zip_password"))
            final_files = [dest_zip]

        elif upload_type == "extract":
            ext = downloaded_files[0].suffix.lower()
            if ext in (".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"):
                extract_dir = downloaded_files[0].parent / "extracted"
                extracted = await extract_archive(
                    downloaded_files[0], extract_dir,
                    password=task_options.get("unzip_password")
                )
                final_files = [f for f in extracted if f.is_file()]
            else:
                final_files = downloaded_files

        elif upload_type == "extract+zip":
            ext = downloaded_files[0].suffix.lower()
            if ext in (".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"):
                extract_dir = downloaded_files[0].parent / "extracted"
                await extract_archive(
                    downloaded_files[0], extract_dir,
                    password=task_options.get("unzip_password")
                )
                dest_zip = downloaded_files[0].parent / (downloaded_files[0].stem + "_rezip.zip")
                await create_zip(extract_dir, dest_zip, password=task_options.get("zip_password"))
                final_files = [dest_zip]
            else:
                final_files = downloaded_files

        else:
            final_files = downloaded_files

        # --- Upload ---
        uploader = TelegramUploader(
            client=client,
            dest_message=user_message,
            caption_prefix=prefix,
            caption_suffix=suffix,
            caption_style=caption_style,
            send_as_document=send_as_doc,
            custom_thumbnail=custom_thumb,
        )

        count = len(final_files)
        await status_msg.edit_text(f"📤 **Uploading `{count}` file(s)…**")
        sent = await uploader.upload_many(final_files, engine=downloader.ENGINE_NAME, downloader=downloader)

        total_size = sum(f.stat().st_size for f in final_files if f.is_file())
        await log_download(user_id, url, final_files[0].name if final_files else None,
                           total_size, downloader.ENGINE_NAME, "success")

        try:
            await status_msg.edit_text(
                f"✅ **Done!**  `{len(sent)}` file(s) — `{format_size(total_size)}`"
            )
        except Exception:
            pass

    except DownloadCancelled:
        await status_msg.edit_text("🚫 **Cancelled.**")
        await log_download(user_id, url, None, 0, None, "cancelled")

    except DownloadError as exc:
        err = str(exc)[:400]
        await status_msg.edit_text(f"❌ **Download failed:**\n`{err}`")
        await log_download(user_id, url, None, 0, None, "failed", str(exc))
        logger.error("DownloadError user=%d url=%s err=%s", user_id, url, exc)

    except Exception as exc:
        err = str(exc)[:400]
        await status_msg.edit_text(f"❌ **Unexpected error:**\n`{err}`")
        await log_download(user_id, url, None, 0, None, "failed", str(exc))
        logger.exception("Unhandled error user=%d url=%s", user_id, url)

    finally:
        # Clean up temp directories
        cleaned = set()
        for f in downloaded_files + final_files:
            parent = f.parent
            if parent not in cleaned and parent.name.startswith("tgbot_"):
                await cleanup_path(parent)
                cleaned.add(parent)
        task_manager._downloaders.pop(user_id, None)


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

def register_handlers(app: Client) -> None:

    @app.on_message(
        filters.text
        & ~filters.command([
            "start", "help", "about", "settings", "cancel", "status", "stats",
            "leech", "mirror", "ytdl", "setthumb", "adminpanel",
            "block", "unblock", "broadcast", "formats", "info",
            "playlist", "torrent", "audio",
        ])
    )
    async def handle_text(client: Client, message: Message):
        user_id = message.from_user.id

        if not is_authorized(user_id):
            await message.reply_text("⛔ You are not authorized to use this bot.")
            return

        if not rate_limiter.is_allowed(user_id):
            wait = rate_limiter.next_available(user_id)
            await message.reply_text(f"⏳ Rate limit — wait `{wait:.0f}s` and try again.")
            return

        text = message.text.strip()
        urls = URL_RE.findall(text)
        if not urls:
            return

        # Parse inline options
        task_options: dict = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                task_options["custom_filename"] = line[1:-1]
            elif line.startswith("{") and line.endswith("}"):
                task_options["zip_password"] = line[1:-1]
            elif line.startswith("(") and line.endswith(")"):
                task_options["unzip_password"] = line[1:-1]

        await get_or_create_user(
            user_id,
            first_name=message.from_user.first_name,
            username=message.from_user.username,
        )

        if task_manager.is_busy(user_id):
            await message.reply_text(
                "⚠️ You have an active task. Use /cancel to stop it, or wait for it to finish."
            )
            return

        # Auto-dispatch anything that is NOT YouTube/Vimeo immediately —
        # no quality / format selector for Terabox, Facebook, TikTok, etc.
        if not _needs_quality_keyboard(urls):
            status_msg = await message.reply_text("⏳ **Starting download…**")
            options = dict(task_options)
            options.setdefault("output_format", "video")
            options.setdefault("quality", "best")
            options.setdefault("upload_type", "normal")

            async def _auto_coro():
                for url in urls:
                    if task_manager.is_cancelled(user_id):
                        break
                    await run_pipeline(
                        client=client,
                        user_message=message,
                        status_msg=status_msg,
                        url=url,
                        task_options=options,
                    )

            await task_manager.submit(
                user_id=user_id,
                url=" | ".join(urls[:3]) + (" ..." if len(urls) > 3 else ""),
                coro_factory=lambda: _auto_coro(),
            )
            return

        # ── yt-dlp / YouTube links: show quality / format selector ──────────
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📄 Normal", callback_data="ul:normal"),
                InlineKeyboardButton("🗜 Zip", callback_data="ul:zip"),
            ],
            [
                InlineKeyboardButton("📂 Extract", callback_data="ul:extract"),
                InlineKeyboardButton("🔄 Extract → Zip", callback_data="ul:extract+zip"),
            ],
            [
                InlineKeyboardButton("🎵 Audio Only", callback_data="ul:audio"),
                InlineKeyboardButton("📎 As Document", callback_data="ul:document"),
            ],
            [
                InlineKeyboardButton("🎞 Best Quality", callback_data="ul:best"),
                InlineKeyboardButton("🎬 1080p", callback_data="ul:1080"),
                InlineKeyboardButton("📺 720p", callback_data="ul:720"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="ul:cancel")],
        ])

        pick_msg = await message.reply_text(
            f"**📥 {len(urls)} URL(s) detected**\n"
            "─" * 24 + "\n"
            "Choose download type:",
            reply_markup=keyboard,
        )

        # Store pending state on the task_manager (thread-safe dict, owned by bot)
        task_manager._pending[user_id] = {
            "urls": urls,
            "options": task_options,
            "pick_msg": pick_msg,
            "user_message": message,
        }

    # ul: callback is handled in commands.py to avoid duplicate registration
