"""
Admin panel — owner-only commands for user management, system control, bot stats.
"""

import asyncio
import logging
import os
import sys
import subprocess
import psutil
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import OWNER_IDS
from database import get_all_users, block_user, unblock_user, reset_daily_bandwidth
from utils.formatters import format_size

logger = logging.getLogger(__name__)


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def register_admin(app: Client) -> None:

    # ------------------------------------------------------------------
    # /adminpanel
    # ------------------------------------------------------------------

    @app.on_message(filters.command("adminpanel") & filters.private)
    async def cmd_admin(client: Client, message: Message):
        if not is_owner(message.from_user.id):
            await message.reply_text("⛔ Owner only.")
            return
        await message.reply_text("🛠 **Admin Panel**", reply_markup=_admin_keyboard())

    @app.on_callback_query(filters.regex(r"^admin:"))
    async def admin_callback(client: Client, callback: CallbackQuery):
        if not is_owner(callback.from_user.id):
            await callback.answer("Not authorized.", show_alert=True)
            return

        action = callback.data[len("admin:"):]
        await callback.answer()

        if action == "ping":
            start = datetime.utcnow()
            msg = await callback.message.reply_text("🏓 Pong!")
            delta = (datetime.utcnow() - start).total_seconds() * 1000
            await msg.edit_text(f"🏓 Pong! `{delta:.0f}ms`")

        elif action == "server_stats":
            cpu  = psutil.cpu_percent(interval=0.5)
            mem  = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            boot   = datetime.fromtimestamp(psutil.boot_time())
            uptime = datetime.utcnow() - boot.replace(tzinfo=None)
            net    = psutil.net_io_counters()

            text = (
                "**🖥 Server Stats**\n\n"
                f"CPU    : `{cpu:.1f}%`\n"
                f"RAM    : `{mem.percent:.1f}%` "
                f"({format_size(mem.used)} / {format_size(mem.total)})\n"
                f"Disk   : `{disk.percent:.1f}%` "
                f"({format_size(disk.used)} / {format_size(disk.total)})\n"
                f"Net ↑  : `{format_size(net.bytes_sent)}`\n"
                f"Net ↓  : `{format_size(net.bytes_recv)}`\n"
                f"Uptime : `{str(uptime).split('.')[0]}`"
            )
            await callback.message.reply_text(text)

        elif action == "users":
            users = await get_all_users()
            lines = [f"**👥 Users ({len(users)} total)**\n"]
            for u in users[:25]:
                name  = u.first_name or "?"
                uname = f"@{u.username}" if u.username else str(u.telegram_id)
                blocked = "🚫" if u.is_blocked else "✅"
                lines.append(f"{blocked} `{u.telegram_id}` — {name} {uname}")
            if len(users) > 25:
                lines.append(f"_...and {len(users) - 25} more_")
            await callback.message.reply_text("\n".join(lines))

        elif action == "dl_stats":
            # Download stats from DB
            try:
                from database import get_download_stats
                stats = await get_download_stats()
                text = (
                    "**📊 Download Stats**\n\n"
                    f"Total    : `{stats.get('total', 0):,}`\n"
                    f"Success  : `{stats.get('success', 0):,}`\n"
                    f"Failed   : `{stats.get('failed', 0):,}`\n"
                    f"Cancelled: `{stats.get('cancelled', 0):,}`\n"
                    f"Data     : `{format_size(stats.get('total_bytes', 0))}`"
                )
            except Exception as exc:
                text = f"❌ Could not fetch stats: `{exc}`"
            await callback.message.reply_text(text)

        elif action == "reset_bandwidth":
            await reset_daily_bandwidth()
            await callback.message.reply_text("✅ Daily bandwidth counters reset.")

        elif action == "update_ytdlp":
            msg = await callback.message.reply_text("⏳ Updating yt-dlp…")
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install", "-U", "yt-dlp",
                    "--break-system-packages",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0:
                    import yt_dlp
                    await msg.edit_text(
                        f"✅ yt-dlp updated to `{yt_dlp.version.__version__}`.\n"
                        "Restart bot to apply."
                    )
                else:
                    await msg.edit_text(f"❌ Update failed:\n`{stderr.decode()[-500:]}`")
            except asyncio.TimeoutError:
                await msg.edit_text("❌ Update timed out.")

        elif action == "update_instaloader":
            msg = await callback.message.reply_text("⏳ Updating instaloader…")
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install", "-U", "instaloader",
                    "--break-system-packages",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0:
                    import instaloader
                    await msg.edit_text(
                        f"✅ instaloader updated to `{instaloader.__version__}`.\n"
                        "Restart bot to apply."
                    )
                else:
                    await msg.edit_text(f"❌ Update failed:\n`{stderr.decode()[-500:]}`")
            except asyncio.TimeoutError:
                await msg.edit_text("❌ Update timed out.")

        elif action == "active_tasks":
            from core.task_manager import task_manager
            tasks = task_manager.all_tasks()
            if not tasks:
                await callback.message.reply_text("✅ No active tasks.")
                return
            lines = [f"**⚙️ Active Tasks ({len(tasks)})**\n"]
            for t in tasks:
                elapsed = (datetime.utcnow() - t.started_at).seconds
                lines.append(f"• User `{t.user_id}` — `{t.status}` — {elapsed}s\n  `{t.url[:60]}`")
            await callback.message.reply_text("\n".join(lines))

        elif action == "cancel_all":
            from core.task_manager import task_manager
            tasks = task_manager.all_tasks()
            cancelled = 0
            for t in tasks:
                if await task_manager.cancel(t.user_id):
                    cancelled += 1
            await callback.message.reply_text(
                f"🚫 Cancelled `{cancelled}` active task(s)."
            )

        elif action == "block_prompt":
            await callback.message.reply_text(
                "Send the Telegram user ID to block:\n`/block <user_id>`"
            )

        elif action == "unblock_prompt":
            await callback.message.reply_text(
                "Send the Telegram user ID to unblock:\n`/unblock <user_id>`"
            )

        elif action == "bot_logs":
            try:
                log_file = "bot.log"
                if os.path.exists(log_file):
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    last = "".join(lines[-40:])
                    await callback.message.reply_text(
                        f"**📋 Last 40 log lines:**\n```\n{last[-3800:]}\n```"
                    )
                else:
                    await callback.message.reply_text("❌ Log file not found.")
            except Exception as exc:
                await callback.message.reply_text(f"❌ Could not read logs: `{exc}`")

        elif action == "clear_logs":
            try:
                open("bot.log", "w").close()
                await callback.message.reply_text("✅ Log file cleared.")
            except Exception as exc:
                await callback.message.reply_text(f"❌ {exc}")

        elif action == "cleanup_tmp":
            import shutil, time
            cutoff = time.time() - 3600
            purged = 0
            for scan in ["/tmp"]:
                try:
                    for d in __import__("pathlib").Path(scan).iterdir():
                        if d.is_dir() and d.name.startswith("tgbot_") and d.stat().st_mtime < cutoff:
                            try:
                                shutil.rmtree(d)
                                purged += 1
                            except Exception:
                                pass
                except Exception:
                    pass
            await callback.message.reply_text(
                f"🧹 Cleaned up `{purged}` stale temp dir(s)."
            )

    # ------------------------------------------------------------------
    # /block and /unblock
    # ------------------------------------------------------------------

    @app.on_message(filters.command("block") & filters.private)
    async def cmd_block(client: Client, message: Message):
        if not is_owner(message.from_user.id):
            return
        if len(message.command) < 2:
            await message.reply_text("Usage: `/block <user_id>`")
            return
        try:
            uid = int(message.command[1])
        except ValueError:
            await message.reply_text("❌ Invalid user ID.")
            return
        ok = await block_user(uid)
        await message.reply_text(f"{'✅ Blocked' if ok else '❌ User not found'} `{uid}`")

    @app.on_message(filters.command("unblock") & filters.private)
    async def cmd_unblock(client: Client, message: Message):
        if not is_owner(message.from_user.id):
            return
        if len(message.command) < 2:
            await message.reply_text("Usage: `/unblock <user_id>`")
            return
        try:
            uid = int(message.command[1])
        except ValueError:
            await message.reply_text("❌ Invalid user ID.")
            return
        ok = await unblock_user(uid)
        await message.reply_text(f"{'✅ Unblocked' if ok else '❌ User not found'} `{uid}`")

    # ------------------------------------------------------------------
    # /finduser
    # ------------------------------------------------------------------

    @app.on_message(filters.command("finduser") & filters.private)
    async def cmd_finduser(client: Client, message: Message):
        if not is_owner(message.from_user.id):
            return
        if len(message.command) < 2:
            await message.reply_text("Usage: `/finduser <user_id or @username>`")
            return
        query = message.command[1].lstrip("@")
        users = await get_all_users()
        found = []
        for u in users:
            if str(u.telegram_id) == query or (u.username and u.username.lower() == query.lower()):
                found.append(u)
        if not found:
            await message.reply_text(f"❌ No user found matching `{query}`")
            return
        lines = []
        for u in found:
            lines.append(
                f"**User Found:**\n"
                f"ID       : `{u.telegram_id}`\n"
                f"Name     : `{u.first_name or 'N/A'}`\n"
                f"Username : `@{u.username or 'N/A'}`\n"
                f"Blocked  : `{'Yes' if u.is_blocked else 'No'}`\n"
            )
        await message.reply_text("\n".join(lines))

    # ------------------------------------------------------------------
    # /broadcast
    # ------------------------------------------------------------------

    @app.on_message(filters.command("broadcast") & filters.private)
    async def cmd_broadcast(client: Client, message: Message):
        if not is_owner(message.from_user.id):
            return
        if len(message.command) < 2:
            await message.reply_text("Usage: `/broadcast <message>`")
            return
        text = " ".join(message.command[1:])
        users = await get_all_users()
        active = [u for u in users if not u.is_blocked]
        sent, failed = 0, 0
        status = await message.reply_text(f"📢 Broadcasting to {len(active)} users…")
        for user in active:
            try:
                await client.send_message(user.telegram_id, text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await status.edit_text(
            f"✅ **Broadcast complete**\n"
            f"Sent: `{sent}` | Failed: `{failed}`"
        )


# ---------------------------------------------------------------------------
# Admin keyboard
# ---------------------------------------------------------------------------

def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏓 Ping",         callback_data="admin:ping"),
            InlineKeyboardButton("🖥 Server Stats",  callback_data="admin:server_stats"),
        ],
        [
            InlineKeyboardButton("👥 Users",         callback_data="admin:users"),
            InlineKeyboardButton("📊 DL Stats",      callback_data="admin:dl_stats"),
        ],
        [
            InlineKeyboardButton("⚙️ Active Tasks",  callback_data="admin:active_tasks"),
            InlineKeyboardButton("🚫 Cancel All",    callback_data="admin:cancel_all"),
        ],
        [
            InlineKeyboardButton("⬆️ Update yt-dlp",       callback_data="admin:update_ytdlp"),
            InlineKeyboardButton("📸 Update instaloader",  callback_data="admin:update_instaloader"),
        ],
        [
            InlineKeyboardButton("🔄 Reset Bandwidth", callback_data="admin:reset_bandwidth"),
            InlineKeyboardButton("🧹 Cleanup /tmp",    callback_data="admin:cleanup_tmp"),
        ],
        [
            InlineKeyboardButton("📋 View Logs",   callback_data="admin:bot_logs"),
            InlineKeyboardButton("🗑 Clear Logs",  callback_data="admin:clear_logs"),
        ],
        [
            InlineKeyboardButton("🚫 Block User",   callback_data="admin:block_prompt"),
            InlineKeyboardButton("✅ Unblock User", callback_data="admin:unblock_prompt"),
        ],
    ])
