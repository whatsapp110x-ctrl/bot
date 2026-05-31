"""
All bot slash commands.

/start  /help  /about  /settings  /cancel  /status  /stats
/leech  /ytdl  /audio  /playlist  /torrent
/formats  /info  /setthumb
/igprofile  /igreels  /igstory
/twitter  /short  /pin
"""

import logging
import psutil
from datetime import datetime
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from config import OWNER_IDS, PRIVATE_MODE, AUTHORIZED_IDS
from core.task_manager import task_manager
from database import get_or_create_user, get_user_settings, update_user_settings
from downloaders import YtdlDownloader
from utils.formatters import format_size
from .handlers import is_authorized, run_pipeline, make_progress_cb

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Bot identity
# ─────────────────────────────────────────────────────────────────────────────

DEVELOPER   = "Ashish Tandi"
BOT_NAME    = "Universal Downloader Bot"
BOT_VERSION = "3.0"

# ─────────────────────────────────────────────────────────────────────────────
# Static text
# ─────────────────────────────────────────────────────────────────────────────

WELCOME_TEXT = f"""🚀 **{BOT_NAME}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Download from **2000+ sites** and get it straight in Telegram.
Just paste any link — I handle the rest.

**📥 Supported Sources**
› YouTube · Twitter/X · Instagram · TikTok
› Reddit · Facebook · Vimeo · Twitch
› Snapchat · Pinterest · SoundCloud
› Google Drive · Terabox · Torrents/Magnets
› HLS/M3U8 · Any direct HTTP/HTTPS link

**⚡ Features**
› Best quality — no artificial limits
› Auto thumbnail embed on every video
› Playlists, audio-only, subtitles
› Auto-split files over 2 GB
› Per-user quality & format settings
› Instagram profile/reel/story batch download
› Twitter/X + Spaces support
› Zip / extract archives on the fly

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💻 **Developer:** {DEVELOPER}
"""

HELP_TEXT = f"""📖 **Commands — {BOT_NAME}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**🔗 Quick Download**
Just paste any URL — bot auto-detects the source.

**🎬 Streaming**
`/stream <url>` — get a clickable stream link (no download needed)

**⬇️ Download Commands**
`/leech <url>` — download & send to Telegram
`/ytdl <url>` — force yt-dlp engine
`/audio <url>` — audio only (M4A/MP3)
`/playlist <url>` — full playlist
`/torrent <magnet|url>` — torrent / magnet

**📸 Instagram Commands**
`/igprofile <@user> [n]` — download last N posts (default 5)
`/igreels <@user> [n]` — download last N reels only
`/igstory <@user>` — download active stories (needs session)

**🐦 Twitter/X Commands**
`/twitter <url>` — download tweet video/GIF/photo
`/spaces <url>` — download Twitter Spaces audio

**🎬 Platform Shortcuts**
`/short <url>` — YouTube Shorts
`/pin <url>` — Pinterest video/image
`/tiktok <url>` — TikTok video (no watermark)
`/reel <url>` — Instagram reel (fast path)

**🔍 Info Commands**
`/formats <url>` — list available quality options
`/info <url>` — video title, duration, thumbnail

**⚙️ Settings & Control**
`/settings` — quality, format, audio, captions
`/setthumb` — reply to photo to set custom thumbnail
`/status` — show active task
`/cancel` — stop running task
`/stats` — system CPU / RAM / disk usage
`/about` — bot info & developer

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**🧩 Inline Options** (add after your link)
`[Custom Name.mp4]` — rename file
`{{password}}` — zip with password
`(password)` — extract archive with password

💻 **Developer:** {DEVELOPER}
"""

ABOUT_TEXT = f"""ℹ️ **About {BOT_NAME}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Version:** `{BOT_VERSION}`
**Developer:** {DEVELOPER}

**🛠 Powered by**
› Pyrogram (Telegram MTProto)
› yt-dlp (2000+ site support)
› instaloader (Instagram native client)
› aria2c (fast parallel downloads)
› FFmpeg (thumbnail & video processing)
› SQLAlchemy + aiosqlite (user settings)

**📡 Supported Protocols**
› HTTP/HTTPS, HLS, DASH, M3U8
› Magnet links & .torrent files
› Telegram t.me message links
› Google Drive, Terabox, Snapchat CDN
› Twitter/X guest-token API
› Instagram graph API

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Built with ❤️ by **{DEVELOPER}**
"""

SEND_LINK_TEXT = (
    "**📎 Send Your Link**\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Paste one or more URLs (one per line):\n"
    "```\nhttps://youtube.com/watch?v=...\nhttps://instagram.com/reel/...\n```"
)


# ─────────────────────────────────────────────────────────────────────────────
# Main keyboard builders
# ─────────────────────────────────────────────────────────────────────────────

def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 Help",      callback_data="help:main"),
            InlineKeyboardButton("⚙️ Settings",  callback_data="settings:menu"),
        ],
        [
            InlineKeyboardButton("📊 Stats",     callback_data="sys:stats"),
            InlineKeyboardButton("ℹ️ About",     callback_data="about:show"),
        ],
    ])


def _settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("━━━  🎞  Video Quality  ━━━", callback_data="settings:_")],
        [
            InlineKeyboardButton("🏆 Best", callback_data="settings:quality:best"),
            InlineKeyboardButton("4K",      callback_data="settings:quality:2160"),
            InlineKeyboardButton("1080p",   callback_data="settings:quality:1080"),
        ],
        [
            InlineKeyboardButton("720p",    callback_data="settings:quality:720"),
            InlineKeyboardButton("480p",    callback_data="settings:quality:480"),
            InlineKeyboardButton("360p",    callback_data="settings:quality:360"),
        ],
        [InlineKeyboardButton("━━━  📦  Output Format  ━━━", callback_data="settings:_")],
        [
            InlineKeyboardButton("🎬 Video",     callback_data="settings:format:video"),
            InlineKeyboardButton("🎵 Audio",     callback_data="settings:format:audio"),
            InlineKeyboardButton("📄 Document",  callback_data="settings:format:document"),
        ],
        [InlineKeyboardButton("━━━  🎧  Audio Format  ━━━", callback_data="settings:_")],
        [
            InlineKeyboardButton("M4A",  callback_data="settings:audio_fmt:m4a"),
            InlineKeyboardButton("MP3",  callback_data="settings:audio_fmt:mp3"),
            InlineKeyboardButton("FLAC", callback_data="settings:audio_fmt:flac"),
            InlineKeyboardButton("Opus", callback_data="settings:audio_fmt:opus"),
        ],
        [InlineKeyboardButton("━━━  ⚙️  Options  ━━━", callback_data="settings:_")],
        [
            InlineKeyboardButton("📎 Send as Doc: On",  callback_data="settings:doc:yes"),
            InlineKeyboardButton("Off",                 callback_data="settings:doc:no"),
        ],
        [
            InlineKeyboardButton("💬 Caption: Bold", callback_data="settings:style:bold"),
            InlineKeyboardButton("Code",             callback_data="settings:style:code"),
            InlineKeyboardButton("Plain",            callback_data="settings:style:plain"),
        ],
        [
            InlineKeyboardButton("🔤 Subtitles: On",  callback_data="settings:subs:on"),
            InlineKeyboardButton("Off",               callback_data="settings:subs:off"),
        ],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="help:home")],
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_stats_text() -> str:
    cpu    = psutil.cpu_percent(interval=0.3)
    mem    = psutil.virtual_memory()
    disk   = psutil.disk_usage("/")
    boot   = datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.utcnow() - boot.replace(tzinfo=None)

    return (
        "**📊 System Stats**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"CPU    : `{cpu:.1f}%`\n"
        f"RAM    : `{mem.percent:.1f}%` ({format_size(mem.used)} / {format_size(mem.total)})\n"
        f"Disk   : `{disk.percent:.1f}%` ({format_size(disk.used)} / {format_size(disk.total)})\n"
        f"Uptime : `{str(uptime).split('.')[0]}`"
    )


async def _dispatch(
    client: Client,
    message: Message,
    status_msg: Message,
    url: str,
    options: dict,
    force_cls=None,
) -> None:
    """Submit a single-URL task, bypassing quality keyboard."""
    user_id = message.from_user.id

    if task_manager.is_busy(user_id):
        await status_msg.edit_text(
            "⚠️ You have an active task running.\n"
            "Use /cancel to stop it, or wait for it to finish."
        )
        return

    opts = dict(options)
    opts.setdefault("output_format", "video")
    opts.setdefault("quality", "best")
    opts.setdefault("upload_type", "normal")

    if force_cls is not None:
        opts["_force_cls"] = force_cls

    async def coro():
        await run_pipeline(
            client=client,
            user_message=message,
            status_msg=status_msg,
            url=url,
            task_options=opts,
        )

    await task_manager.submit(
        user_id=user_id,
        url=url,
        coro_factory=lambda: coro(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Command registration
# ─────────────────────────────────────────────────────────────────────────────

def register_commands(app: Client) -> None:

    # ── /start ───────────────────────────────────────────────────────────
    @app.on_message(filters.command("start"))
    async def cmd_start(client: Client, message: Message):
        try:
            if not is_authorized(message.from_user.id):
                await message.reply_text(
                    f"⛔ **Access Restricted**\n\n"
                    f"This bot is private.\n"
                    f"Contact developer: **{DEVELOPER}**"
                )
                return
            await get_or_create_user(
                message.from_user.id,
                first_name=message.from_user.first_name,
                username=message.from_user.username,
            )
            await message.reply_text(
                WELCOME_TEXT,
                reply_markup=_main_kb(),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.exception("Error in /start for user %d", message.from_user.id)
            await message.reply_text(f"⚠️ Something went wrong. Please try again.\n`{exc}`")

    # ── /help ────────────────────────────────────────────────────────────
    @app.on_message(filters.command("help"))
    async def cmd_help(client: Client, message: Message):
        await message.reply_text(
            HELP_TEXT,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="help:home")],
            ]),
        )

    # ── /about ───────────────────────────────────────────────────────────
    @app.on_message(filters.command("about"))
    async def cmd_about(client: Client, message: Message):
        await message.reply_text(
            ABOUT_TEXT,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="help:home")],
            ]),
        )

    # ── Callback router for help / home / about ──────────────────────────
    @app.on_callback_query(filters.regex(r"^(help:|about:)"))
    async def nav_cb(client: Client, callback: CallbackQuery):
        await callback.answer()
        action = callback.data

        if action == "help:home":
            await callback.message.edit_text(
                WELCOME_TEXT,
                reply_markup=_main_kb(),
                disable_web_page_preview=True,
            )
        elif action == "help:main":
            await callback.message.edit_text(
                HELP_TEXT,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back to Menu", callback_data="help:home")],
                ]),
                disable_web_page_preview=True,
            )
        elif action == "about:show":
            await callback.message.edit_text(
                ABOUT_TEXT,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back to Menu", callback_data="help:home")],
                ]),
                disable_web_page_preview=True,
            )

    # ── /cancel ──────────────────────────────────────────────────────────
    @app.on_message(filters.command("cancel") & filters.private)
    async def cmd_cancel(client: Client, message: Message):
        user_id = message.from_user.id
        cancelled = await task_manager.cancel(user_id)
        dl = getattr(task_manager, "_downloaders", {}).get(user_id)
        if dl:
            dl.cancel()
        if cancelled:
            await message.reply_text("🚫 **Task cancelled.**")
        else:
            await message.reply_text("ℹ️ No active task to cancel.")

    # ── /status ──────────────────────────────────────────────────────────
    @app.on_message(filters.command("status") & filters.private)
    async def cmd_status(client: Client, message: Message):
        user_id = message.from_user.id
        record = task_manager.get_user_task(user_id)
        if not record:
            await message.reply_text("✅ No active tasks running.")
            return
        elapsed = int((datetime.utcnow() - record.started_at).total_seconds())
        mins, secs = divmod(elapsed, 60)
        await message.reply_text(
            "**⚙️ Active Task**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Status  : `{record.status}`\n"
            f"Elapsed : `{mins}m {secs}s`\n"
            f"URL     : `{record.url[:70]}`"
        )

    # ── /stats ───────────────────────────────────────────────────────────
    @app.on_message(filters.command("stats"))
    async def cmd_stats(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return
        await message.reply_text(_build_stats_text())

    @app.on_callback_query(filters.regex(r"^sys:stats"))
    async def stats_cb(client: Client, callback: CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            _build_stats_text(),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="help:home")],
            ]),
        )

    # ── /leech (alias for plain URL download) ────────────────────────────
    @app.on_message(filters.command("leech") & filters.private)
    async def cmd_leech(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text(SEND_LINK_TEXT)
            return
        url = " ".join(parts)
        status = await message.reply_text(f"⏳ **Starting download…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {"output_format": "video", "quality": "best"})

    # ── /ytdl ────────────────────────────────────────────────────────────
    @app.on_message(filters.command("ytdl") & filters.private)
    async def cmd_ytdl(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text(SEND_LINK_TEXT)
            return
        url = parts[0]
        status = await message.reply_text(f"⏳ **yt-dlp → starting…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {"output_format": "video", "quality": "best"})

    # ── /audio ───────────────────────────────────────────────────────────
    @app.on_message(filters.command("audio") & filters.private)
    async def cmd_audio(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text("Usage: `/audio <url>`")
            return
        url = parts[0]
        status = await message.reply_text(f"🎵 **Audio download…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {"output_format": "audio", "quality": "best"})

    # ── /playlist ────────────────────────────────────────────────────────
    @app.on_message(filters.command("playlist") & filters.private)
    async def cmd_playlist(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text("Usage: `/playlist <url>`")
            return
        url = parts[0]
        status = await message.reply_text(f"📋 **Playlist download…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {"playlist": True, "quality": "best"})

    # ── /torrent ─────────────────────────────────────────────────────────
    @app.on_message(filters.command("torrent") & filters.private)
    async def cmd_torrent(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text(
                "**🧲 Torrent / Magnet Download**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Usage: `/torrent <magnet_link_or_torrent_url>`\n\n"
                "Example:\n`/torrent magnet:?xt=urn:btih:...`"
            )
            return
        url = " ".join(parts)
        from downloaders.torrent import TorrentDownloader
        status = await message.reply_text(f"🧲 **Torrent starting…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {}, force_cls=TorrentDownloader)

    # ── /formats ─────────────────────────────────────────────────────────
    @app.on_message(filters.command("formats") & filters.private)
    async def cmd_formats(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text("Usage: `/formats <url>`")
            return
        url = parts[0]
        msg = await message.reply_text(f"🔍 **Fetching formats…**\n`{url[:80]}`")
        try:
            result = await YtdlDownloader.list_formats(url)
            await msg.edit_text(result[:4000])
        except Exception as exc:
            await msg.edit_text(f"❌ **Error:**\n`{exc}`")

    # ── /info ────────────────────────────────────────────────────────────
    @app.on_message(filters.command("info") & filters.private)
    async def cmd_info(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text("Usage: `/info <url>`")
            return
        url = parts[0]
        msg = await message.reply_text(f"🔍 **Fetching info…**\n`{url[:80]}`")
        try:
            info = await YtdlDownloader.get_info(url)
            title    = info.get("title", "Unknown")
            uploader = info.get("uploader") or info.get("channel", "Unknown")
            duration = info.get("duration") or 0
            views    = info.get("view_count") or 0
            likes    = info.get("like_count") or 0
            desc     = (info.get("description") or "")[:200]
            formats  = len(info.get("formats", []))
            thumb_url = info.get("thumbnail") or ""
            mins, secs = divmod(int(duration), 60)

            text = (
                "**📹 Media Info**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"**Title**    : `{title[:80]}`\n"
                f"**Channel**  : `{uploader[:60]}`\n"
                f"**Duration** : `{mins}m {secs}s`\n"
                f"**Views**    : `{views:,}`\n"
                f"**Likes**    : `{likes:,}`\n"
                f"**Formats**  : `{formats}` available\n"
            )
            if desc:
                text += f"\n_{desc.strip()}_"

            # Try to send with thumbnail photo
            if thumb_url:
                try:
                    await msg.delete()
                    await message.reply_photo(photo=thumb_url, caption=text)
                    return
                except Exception:
                    pass

            await msg.edit_text(text)
        except Exception as exc:
            await msg.edit_text(f"❌ **Error:**\n`{exc}`")

    # ── /setthumb ────────────────────────────────────────────────────────
    @app.on_message(filters.command("setthumb") & filters.reply & filters.private)
    async def cmd_setthumb(client: Client, message: Message):
        user_id = message.from_user.id
        reply = message.reply_to_message
        if not reply or not reply.photo:
            await message.reply_text("❌ Reply to a **photo** to set it as your thumbnail.")
            return
        from config import THUMBNAIL_DIR
        thumb_path = THUMBNAIL_DIR / f"thumb_{user_id}.jpg"
        await client.download_media(reply.photo, file_name=str(thumb_path))
        await update_user_settings(user_id, custom_thumbnail=str(thumb_path))
        await message.reply_text(
            "✅ **Thumbnail saved!**\n"
            "It will be embedded in all future video uploads."
        )

    # ── /settings ────────────────────────────────────────────────────────
    @app.on_message(filters.command("settings") & filters.private)
    async def cmd_settings(client: Client, message: Message):
        await message.reply_text(
            "**⚙️ Settings**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Choose a preference to change:",
            reply_markup=_settings_kb(),
        )

    @app.on_callback_query(filters.regex(r"^settings:"))
    async def settings_cb(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id
        action = callback.data[len("settings:"):]

        if action in ("_", ""):
            await callback.answer()
            return

        if action == "menu":
            await callback.answer()
            await callback.message.edit_text(
                "**⚙️ Settings**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Choose a preference to change:",
                reply_markup=_settings_kb(),
            )
            return

        await callback.answer("✅ Saved")

        if action.startswith("quality:"):
            q = action.split(":", 1)[1]
            await update_user_settings(user_id, video_quality=q)
        elif action.startswith("format:"):
            f = action.split(":", 1)[1]
            await update_user_settings(user_id, output_format=f)
        elif action.startswith("audio_fmt:"):
            af = action.split(":", 1)[1]
            await update_user_settings(user_id, audio_format=af)
        elif action.startswith("doc:"):
            val = action.split(":", 1)[1] == "yes"
            await update_user_settings(user_id, send_as_document=val)
        elif action.startswith("style:"):
            s = action.split(":", 1)[1]
            await update_user_settings(user_id, caption_style=s)
        elif action.startswith("subs:"):
            val = action.split(":", 1)[1] == "on"
            await update_user_settings(user_id, subtitles=val)

        try:
            await callback.message.edit_reply_markup(reply_markup=_settings_kb())
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    # Instagram Profile / Batch Commands (new)
    # ─────────────────────────────────────────────────────────────────────

    @app.on_message(filters.command(["igprofile", "igp"]) & filters.private)
    async def cmd_igprofile(client: Client, message: Message):
        """Download recent posts from an Instagram profile."""
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text(
                "**📸 Instagram Profile Downloader**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Usage: `/igprofile <@username> [count]`\n\n"
                "Downloads the last N posts (photos + videos).\n"
                "Default count: 5 · Max: 20\n\n"
                "Examples:\n"
                "`/igprofile @natgeo 5`\n"
                "`/igprofile cristiano 10`"
            )
            return

        username = parts[0].lstrip("@")
        count = min(int(parts[1]), 20) if len(parts) > 1 and parts[1].isdigit() else 5

        user_id = message.from_user.id
        if task_manager.is_busy(user_id):
            await message.reply_text("⚠️ You have an active task. Use /cancel first.")
            return

        status_msg = await message.reply_text(
            f"📸 **Fetching profile @{username}…**\n"
            f"Downloading last **{count}** post(s)…"
        )

        async def coro():
            import tempfile
            from downloaders.instagram import download_profile_posts
            from uploaders import TelegramUploader
            from downloaders.base import DownloadError

            dest = Path(tempfile.mkdtemp(prefix="tgbot_igp_"))
            try:
                await status_msg.edit_text(
                    f"📸 **@{username}** — downloading {count} post(s)…\n"
                    "This may take a minute."
                )
                files = await download_profile_posts(username, dest, count=count)

                if not files:
                    await status_msg.edit_text(
                        f"❌ No media found for @{username}.\n"
                        "• Profile may be private\n"
                        "• No posts available"
                    )
                    return

                await status_msg.edit_text(
                    f"✅ Got **{len(files)}** file(s) from @{username}\n"
                    f"Uploading…"
                )

                uploader = TelegramUploader(client=client, dest_message=message)
                await uploader.upload_many(files)

                await status_msg.edit_text(
                    f"✅ **Done!** Sent `{len(files)}` file(s) from @{username}"
                )
            except DownloadError as exc:
                await status_msg.edit_text(f"❌ {exc}")
            except Exception as exc:
                await status_msg.edit_text(f"❌ **Error:** `{exc}`")
                logger.exception("igprofile error user=%d", user_id)
            finally:
                from utils.file_utils import cleanup_path
                await cleanup_path(dest)

        await task_manager.submit(
            user_id=user_id,
            url=f"ig://profile/{username}",
            coro_factory=lambda: coro(),
        )

    @app.on_message(filters.command(["igreels", "igr"]) & filters.private)
    async def cmd_igreels(client: Client, message: Message):
        """Download recent reels from an Instagram profile."""
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text(
                "**🎬 Instagram Reels Downloader**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Usage: `/igreels <@username> [count]`\n\n"
                "Downloads the last N reels (videos only).\n"
                "Default count: 5 · Max: 15\n\n"
                "Example: `/igreels @cristiano 5`"
            )
            return

        username = parts[0].lstrip("@")
        count = min(int(parts[1]), 15) if len(parts) > 1 and parts[1].isdigit() else 5

        user_id = message.from_user.id
        if task_manager.is_busy(user_id):
            await message.reply_text("⚠️ You have an active task. Use /cancel first.")
            return

        status_msg = await message.reply_text(
            f"🎬 **Fetching reels from @{username}…**\n"
            f"Last **{count}** reel(s)"
        )

        async def coro():
            import tempfile
            from downloaders.instagram import download_profile_posts
            from uploaders import TelegramUploader
            from downloaders.base import DownloadError

            dest = Path(tempfile.mkdtemp(prefix="tgbot_igr_"))
            try:
                files = await download_profile_posts(
                    username, dest, count=count, only_reels=True
                )
                if not files:
                    await status_msg.edit_text(
                        f"❌ No reels found for @{username}.\n"
                        "• Profile may be private or have no reels"
                    )
                    return

                await status_msg.edit_text(
                    f"✅ Got **{len(files)}** reel(s) — uploading…"
                )
                uploader = TelegramUploader(client=client, dest_message=message)
                await uploader.upload_many(files)
                await status_msg.edit_text(
                    f"✅ **Done!** Sent `{len(files)}` reel(s) from @{username}"
                )
            except DownloadError as exc:
                await status_msg.edit_text(f"❌ {exc}")
            except Exception as exc:
                await status_msg.edit_text(f"❌ **Error:** `{exc}`")
            finally:
                from utils.file_utils import cleanup_path
                await cleanup_path(dest)

        await task_manager.submit(
            user_id=user_id,
            url=f"ig://reels/{username}",
            coro_factory=lambda: coro(),
        )

    @app.on_message(filters.command(["igstory", "igs"]) & filters.private)
    async def cmd_igstory(client: Client, message: Message):
        """Download active stories from an Instagram profile."""
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text(
                "**📖 Instagram Story Downloader**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Usage: `/igstory <@username>`\n\n"
                "Downloads all active stories from a profile.\n"
                "⚠️ Requires bot owner to set INSTAGRAM_SESSIONID.\n\n"
                "Example: `/igstory @natgeo`"
            )
            return

        username = parts[0].lstrip("@")
        user_id = message.from_user.id

        if task_manager.is_busy(user_id):
            await message.reply_text("⚠️ You have an active task. Use /cancel first.")
            return

        status_msg = await message.reply_text(
            f"📖 **Fetching stories from @{username}…**"
        )

        async def coro():
            import tempfile
            from downloaders.instagram import download_profile_stories
            from uploaders import TelegramUploader
            from downloaders.base import DownloadError

            dest = Path(tempfile.mkdtemp(prefix="tgbot_igs_"))
            try:
                files = await download_profile_stories(username, dest)
                if not files:
                    await status_msg.edit_text(
                        f"❌ No active stories found for @{username}.\n"
                        "• Stories may have all expired\n"
                        "• Account may be private"
                    )
                    return

                await status_msg.edit_text(
                    f"✅ Got **{len(files)}** story item(s) — uploading…"
                )
                uploader = TelegramUploader(client=client, dest_message=message)
                await uploader.upload_many(files)
                await status_msg.edit_text(
                    f"✅ **Done!** Sent `{len(files)}` story item(s) from @{username}"
                )
            except DownloadError as exc:
                await status_msg.edit_text(f"❌ {exc}")
            except Exception as exc:
                await status_msg.edit_text(f"❌ **Error:** `{exc}`")
            finally:
                from utils.file_utils import cleanup_path
                await cleanup_path(dest)

        await task_manager.submit(
            user_id=user_id,
            url=f"ig://story/{username}",
            coro_factory=lambda: coro(),
        )

    # ─────────────────────────────────────────────────────────────────────
    # Twitter / X Commands
    # ─────────────────────────────────────────────────────────────────────

    @app.on_message(filters.command(["twitter", "tweet", "xdl"]) & filters.private)
    async def cmd_twitter(client: Client, message: Message):
        """Download Twitter/X tweet media."""
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text(
                "**🐦 Twitter/X Downloader**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Usage: `/twitter <tweet_url>`\n\n"
                "Supports: Videos, GIFs, Photos, Twitter Spaces\n\n"
                "Example:\n`/twitter https://twitter.com/user/status/123456`"
            )
            return
        url = parts[0]
        from downloaders.twitter import TwitterDownloader
        status = await message.reply_text(f"🐦 **Twitter/X download…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {}, force_cls=TwitterDownloader)

    @app.on_message(filters.command(["spaces"]) & filters.private)
    async def cmd_spaces(client: Client, message: Message):
        """Download Twitter Spaces audio."""
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text(
                "**🎙 Twitter Spaces Downloader**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Usage: `/spaces <spaces_url>`\n\n"
                "⚠️ Only **ended/recorded** Spaces can be downloaded.\n"
                "Live Spaces cannot be captured.\n\n"
                "Example:\n`/spaces https://twitter.com/i/spaces/...`"
            )
            return
        url = parts[0]
        from downloaders.twitter import TwitterDownloader
        status = await message.reply_text(f"🎙 **Twitter Spaces…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {"output_format": "audio"}, force_cls=TwitterDownloader)

    # ─────────────────────────────────────────────────────────────────────
    # Platform Shortcuts
    # ─────────────────────────────────────────────────────────────────────

    @app.on_message(filters.command(["short", "shorts", "yt"]) & filters.private)
    async def cmd_short(client: Client, message: Message):
        """Download YouTube Shorts."""
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text("Usage: `/short <youtube_shorts_url>`")
            return
        url = parts[0]
        status = await message.reply_text(f"▶️ **YouTube Shorts…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {"quality": "best", "output_format": "video"})

    @app.on_message(filters.command(["pin", "pinterest"]) & filters.private)
    async def cmd_pin(client: Client, message: Message):
        """Download Pinterest video/image."""
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text("Usage: `/pin <pinterest_url>`")
            return
        url = parts[0]
        status = await message.reply_text(f"📌 **Pinterest download…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {"quality": "best"})

    @app.on_message(filters.command(["tiktok", "tk"]) & filters.private)
    async def cmd_tiktok(client: Client, message: Message):
        """Download TikTok video (attempts no-watermark)."""
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text(
                "**📱 TikTok Downloader**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Usage: `/tiktok <tiktok_url>`\n\n"
                "Tip: Copy the share link from TikTok app.\n"
                "Example: `/tiktok https://vm.tiktok.com/...`"
            )
            return
        url = parts[0]
        status = await message.reply_text(f"📱 **TikTok download…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {"quality": "best"})

    @app.on_message(filters.command(["reel", "ig"]) & filters.private)
    async def cmd_reel(client: Client, message: Message):
        """Download a single Instagram reel/post (fast path)."""
        if not is_authorized(message.from_user.id):
            return
        parts = message.command[1:]
        if not parts:
            await message.reply_text("Usage: `/reel <instagram_url>`")
            return
        url = parts[0]
        status = await message.reply_text(f"📸 **Instagram download…**\n`{url[:80]}`")
        await _dispatch(client, message, status, url, {"quality": "best"})

    # ── /ytlogin — YouTube OAuth2 device-code auth (owner only) ─────────
    @app.on_message(filters.command("ytlogin"))
    async def cmd_ytlogin(client: Client, message: Message):
        if message.from_user.id not in OWNER_IDS:
            await message.reply_text("⛔ Owner only.")
            return

        msg = await message.reply_text(
            "🔐 **Starting YouTube OAuth2 login…**\n\n"
            "This links your Google account so the bot can download age-restricted "
            "and region-locked videos.\n\n"
            "_Please wait — fetching device code…_"
        )

        import asyncio, re, sys
        from pathlib import Path

        oauth_file = Path(__file__).parent.parent / "data" / "yt-oauth2-token.json"
        oauth_file.parent.mkdir(parents=True, exist_ok=True)

        env = dict(__import__("os").environ)
        env["YTDLP_NO_UPDATE"] = "1"

        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--username", "oauth2",
            "--password", "",
            "--skip-download",
            "--no-playlist",
            # NOTE: do NOT add --quiet here — it suppresses the device code output
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except Exception as exc:
            await msg.edit_text(f"❌ Failed to start yt-dlp: {exc}")
            return

        device_url = None
        device_code = None
        auth_complete = False
        full_output: list[str] = []

        async def _read_output():
            nonlocal device_url, device_code, auth_complete
            while True:
                try:
                    line_b = await asyncio.wait_for(proc.stdout.readline(), timeout=300)
                except asyncio.TimeoutError:
                    break
                if not line_b:
                    break
                line = line_b.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                full_output.append(line)

                # Device code flow output (from yt-dlp-youtube-oauth2) looks like:
                #   "[youtube+oauth2] To give yt-dlp access to your account, go to
                #    https://www.google.com/device  and enter code  JVN-JWZ-PCXW"
                # URL and code appear on the SAME line.
                if "google.com/device" in line or "youtube.com/activate" in line:
                    url_m = re.search(r"https?://\S+", line)
                    if url_m:
                        device_url = url_m.group().rstrip(".,)")
                    # Code can be XXX-XXX-XXXX or XXXX-XXXX etc.
                    code_m = re.search(r"\benter code\s+([A-Z0-9][A-Z0-9-]{4,14}[A-Z0-9])\b", line, re.I)
                    if code_m:
                        device_code = code_m.group(1).upper()
                    elif not device_code:
                        # Fallback: any code-like token after the URL
                        code_m2 = re.search(r"\b([A-Z0-9]{2,6}(?:-[A-Z0-9]{2,6}){1,3})\b", line)
                        if code_m2:
                            device_code = code_m2.group(1)

                if device_url and not auth_complete:
                    code_text = f"\n\n🔑 Code: `{device_code}`" if device_code else ""
                    await msg.edit_text(
                        f"🔐 **YouTube Login — Action Required**\n\n"
                        f"1️⃣ Open this URL in your browser:\n{device_url}"
                        f"{code_text}\n\n"
                        f"2️⃣ Sign in with your Google account\n\n"
                        f"_Waiting for you to authorize (5 min timeout)…_"
                    )

                if any(w in line.lower() for w in ("token", "authorized", "logged in", "oauth")):
                    auth_complete = True

        await _read_output()
        await proc.wait()

        if proc.returncode == 0 or auth_complete:
            await msg.edit_text(
                "✅ **YouTube login successful!**\n\n"
                "The bot will now use your account to download age-restricted and "
                "region-locked videos automatically."
            )
            logger.info("YouTube OAuth2 login completed for owner %d", message.from_user.id)
        else:
            output_snippet = "\n".join(full_output[-5:]) if full_output else "(no output)"
            await msg.edit_text(
                f"❌ **YouTube login failed or timed out.**\n\n"
                f"Last output:\n`{output_snippet[:300]}`\n\n"
                f"Try `/ytlogin` again."
            )

    # ── /stream — generate a clickable streaming link ────────────────────
    @app.on_message(filters.command("stream") & filters.private)
    async def cmd_stream(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return

        parts = message.command[1:]
        if not parts:
            await message.reply_text(
                "**🎬 Stream Any URL**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Usage: `/stream <url>`\n\n"
                "Returns a clickable link you can open in any browser or media\n"
                "player (VLC, mpv, etc.) — no downloading required.\n\n"
                "Supports: Terabox · YouTube · Vimeo · Twitter/X\n"
                "          Direct HTTP links · HLS/M3U8 streams\n\n"
                "Example:\n"
                "`/stream https://www.terabox.app/wap/share/filelist?surl=…`"
            )
            return

        url = " ".join(parts)
        msg = await message.reply_text(f"🔍 **Resolving stream…**\n`{url[:80]}`")

        from config import STREAM_BASE_URL
        from stream.server import create_stream_token
        from stream.resolver import resolve_stream_url

        if not STREAM_BASE_URL:
            await msg.edit_text(
                "⚠️ **Stream server not configured.**\n\n"
                "The bot owner must set the `STREAM_BASE_URL` environment variable "
                "to the public address of this server.\n\n"
                "Example for VPS:\n"
                "`STREAM_BASE_URL=http://1.2.3.4:8765`"
            )
            return

        try:
            info = await resolve_stream_url(url)
        except Exception as exc:
            logger.exception("Stream resolve failed for %s", url)
            await msg.edit_text(f"❌ **Could not resolve stream:**\n`{exc}`")
            return

        title    = info.get("title", "stream")[:80]
        ext      = info.get("ext", "mp4").upper()
        is_hls   = info.get("is_hls", False)
        filesize = info.get("filesize")

        token      = create_stream_token(
            url=info["url"],
            filename=f"{title}.{info.get('ext', 'mp4')}",
            mime=info.get("mime", "video/mp4"),
            title=title,
        )
        stream_url = f"{STREAM_BASE_URL}/stream/{token}"

        size_text = ""
        if filesize:
            from utils.formatters import format_size
            size_text = f"\n**Size   :** `{format_size(filesize)}`"

        stream_type = "HLS stream (browser player)" if is_hls else f"{ext} file"

        await msg.edit_text(
            f"🎬 **Stream Ready**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Title  :** `{title}`\n"
            f"**Type   :** `{stream_type}`"
            f"{size_text}\n\n"
            f"**🔗 Stream Link:**\n"
            f"{stream_url}\n\n"
            f"_Open in browser · VLC · mpv · any player_\n"
            f"_Link valid for 24 hours_",
            disable_web_page_preview=True,
        )
        logger.info("Stream token created for user %d url=%s", message.from_user.id, url[:80])

    # ── /ytlogout — remove YouTube OAuth token (owner only) ─────────────
    @app.on_message(filters.command("ytlogout"))
    async def cmd_ytlogout(client: Client, message: Message):
        if message.from_user.id not in OWNER_IDS:
            await message.reply_text("⛔ Owner only.")
            return
        from pathlib import Path
        oauth_file = Path(__file__).parent.parent / "data" / "yt-oauth2-token.json"
        if oauth_file.exists():
            oauth_file.unlink()
            await message.reply_text("✅ YouTube account unlinked.")
        else:
            await message.reply_text("ℹ️ No YouTube account is linked.")

    # ─────────────────────────────────────────────────────────────────────
    # Inline button dispatch for upload type / quality
    # ─────────────────────────────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^ul:"))
    async def handle_upload_type(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id
        choice = callback.data[3:]

        pending = task_manager._pending.get(user_id) if hasattr(task_manager, "_pending") else None
        if not pending:
            await callback.answer("Session expired — send the link again.", show_alert=True)
            return

        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass

        if choice == "cancel":
            task_manager._pending.pop(user_id, None)
            return

        urls: list[str] = pending["urls"]
        options: dict = dict(pending["options"])
        user_message: Message = pending["user_message"]
        task_manager._pending.pop(user_id, None)

        if choice == "audio":
            options["output_format"] = "audio"
            options["upload_type"] = "normal"
        elif choice == "document":
            options["send_as_document"] = True
            options["upload_type"] = "normal"
        elif choice in ("best", "1080", "720", "480"):
            options["quality"] = choice
            options["upload_type"] = "normal"
        else:
            options["upload_type"] = choice

        status_msg = await user_message.reply_text("⏳ **Starting task…**")

        async def coro():
            for url in urls:
                if task_manager.is_cancelled(user_id):
                    break
                await run_pipeline(
                    client=client,
                    user_message=user_message,
                    status_msg=status_msg,
                    url=url,
                    task_options=options,
                )

        await task_manager.submit(
            user_id=user_id,
            url=" | ".join(urls[:3]) + (" ..." if len(urls) > 3 else ""),
            coro_factory=lambda: coro(),
        )
