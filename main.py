"""
Universal Telegram Downloader Bot — Entry Point
================================================
Run:
    pip install -r requirements.txt
    python main.py
"""

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Use uvloop on Linux/macOS for better performance ─────────────────────────
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from pyrogram import Client

from config import (
    BOT_TOKEN, API_ID, API_HASH, LOG_LEVEL,
    DOWNLOAD_DIR, MAX_CONCURRENT_TASKS,
    STREAM_PORT, STREAM_BASE_URL,
)
from database import init_db, reset_daily_bandwidth
from bot import register_commands, register_handlers
from admin import register_admin

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            "bot.log",
            encoding="utf-8",
            maxBytes=5 * 1024 * 1024,   # 5 MB per file
            backupCount=3,              # keep last 3 rotated files
        ),
    ],
)
for _lib in ("pyrogram", "pyrogram.session", "apscheduler", "hpack"):
    logging.getLogger(_lib).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ── Scheduler ─────────────────────────────────────────────────────────────────
def _build_scheduler():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = AsyncIOScheduler(timezone="UTC")

    sched.add_job(
        reset_daily_bandwidth,
        CronTrigger(hour=0, minute=0),
        id="reset_bw",
        replace_existing=True,
    )

    async def _purge():
        import shutil, time
        cutoff = time.time() - 7200
        purged = 0
        for d in Path("/tmp").iterdir():
            if d.is_dir() and d.name.startswith("tgbot_") and d.stat().st_mtime < cutoff:
                try:
                    shutil.rmtree(d)
                    purged += 1
                except Exception:
                    pass
        if purged:
            logger.info("Purged %d orphaned temp dir(s)", purged)

    sched.add_job(
        _purge,
        CronTrigger(minute="*/30"),
        id="purge_dirs",
        replace_existing=True,
    )

    return sched


# ── Startup cleanup ───────────────────────────────────────────────────────────
def _cleanup_stale_session():
    session_file = Path("universal_tgbot.session")
    if session_file.exists():
        import sqlite3
        try:
            con = sqlite3.connect(str(session_file), timeout=2)
            con.execute("BEGIN EXCLUSIVE")
            con.rollback()
            con.close()
        except sqlite3.OperationalError:
            logger.warning("Stale Pyrogram session lock — removing session file")
            session_file.unlink(missing_ok=True)
            Path("universal_tgbot.session-journal").unlink(missing_ok=True)


def _purge_stale_temp_dirs():
    import shutil, time
    cutoff = time.time() - 3600
    purged = 0
    for scan_dir in {DOWNLOAD_DIR, Path("/tmp")}:
        try:
            for d in scan_dir.iterdir():
                if d.is_dir() and d.name.startswith("tgbot_") and d.stat().st_mtime < cutoff:
                    try:
                        shutil.rmtree(d)
                        purged += 1
                    except Exception:
                        pass
        except Exception:
            pass
    if purged:
        logger.info("Startup: purged %d stale temp dir(s)", purged)


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    logger.info("═══ Universal Telegram Bot starting ═══")

    await init_db()
    logger.info("Database ready.")

    _purge_stale_temp_dirs()
    _cleanup_stale_session()

    app = Client(
        name="universal_tgbot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workers=128,
    )

    register_commands(app)
    register_handlers(app)
    register_admin(app)

    scheduler = _build_scheduler()
    scheduler.start()
    logger.info("Scheduler started.")

    # ── Streaming server ─────────────────────────────────────────────────────
    from stream import start_stream_server
    stream_runner = await start_stream_server(STREAM_PORT)
    if STREAM_BASE_URL:
        logger.info("Stream links will use base URL: %s", STREAM_BASE_URL)
    else:
        logger.warning(
            "STREAM_BASE_URL is not set — /stream links will not work until you set it. "
            "Example: STREAM_BASE_URL=http://<your-vps-ip>:%d", STREAM_PORT
        )

    async with app:
        me = await app.get_me()
        logger.info(
            "Bot running as @%s (id=%d) | concurrency=%d",
            me.username, me.id, MAX_CONCURRENT_TASKS,
        )
        stream_note = (
            f"  Stream  : {STREAM_BASE_URL}"
            if STREAM_BASE_URL
            else f"  Stream  : port {STREAM_PORT} (set STREAM_BASE_URL for links)"
        )
        print(f"""
╔══════════════════════════════════════════════════════╗
║       Universal Downloader Bot  v2.0                 ║
║                                                      ║
║  Bot       : @{me.username:<38} ║
║  Workers   : 128 async handlers                      ║
║  Engines   : yt-dlp · aria2c · snapchat · gdrive     ║
║              torrent · terabox · direct · telegram   ║
╚══════════════════════════════════════════════════════╝
{stream_note}
""")
        logger.info("Press Ctrl+C to stop.")
        await asyncio.Event().wait()

    await stream_runner.cleanup()
    scheduler.shutdown()
    logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)
