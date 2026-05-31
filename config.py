"""
Universal Telegram Downloader Bot — Configuration
==================================================
Fill in the USER SETTINGS section below.
All values can also be set via environment variables (useful for hosting services).
"""

import os
import sys
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════
# ✏️  USER SETTINGS — fill in your values here
#     Alternatively, set these as environment variables / in a .env file
# ═══════════════════════════════════════════════════════════════════════

BOT_TOKEN = ""   # set via BOT_TOKEN environment secret
API_ID    = 0    # set via API_ID environment secret
API_HASH  = ""   # set via API_HASH environment secret
OWNER_IDS = [7052170756]   # Your Telegram user IDs  e.g. [123456789]

# Restrict bot to specific users — empty list = open to everyone
AUTHORIZED_IDS: list[int] = []

# Channel/group ID to mirror all downloaded files (None = disabled)
DUMP_CHANNEL: int | None = None

# Instaloader session file path — unlocks private Instagram content
INSTAGRAM_SESSIONID: str | None = None

# Terabox `ndus` cookie — only needed for private Terabox links
TERABOX_NDUS: str | None = None


# Telegram Premium supports 4 GB — set 4*1024**3 if you have Premium
TG_MAX_SIZE: int = 2 * 1024 ** 3   # 2 GB default


# ═══════════════════════════════════════════════════════════════════════
# ⚙️  ADVANCED — only change if needed
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_VIDEO_QUALITY     = "best"    # "best" | "1080" | "720" | "480" | "360"
DEFAULT_AUDIO_FORMAT      = "m4a"     # "m4a" | "mp3" | "opus" | "flac"
MAX_CONCURRENT_TASKS      = 5         # simultaneous downloads
MAX_QUEUE_PER_USER        = 20        # max queued tasks per user
TASK_TIMEOUT_SECONDS      = 21600     # download timeout (6 hours)
MAX_DOWNLOAD_SIZE_GB      = 100       # max file size in GB
RATE_LIMIT_REQUESTS       = 20        # requests per user per window
RATE_LIMIT_WINDOW_SECONDS = 60
ARIA2_CONNECTIONS         = 16        # parallel connections per file
ARIA2_MAX_SPLITS          = 16


# ═══════════════════════════════════════════════════════════════════════
# 🔧  INTERNAL — do not edit below this line
# ═══════════════════════════════════════════════════════════════════════

def _env(name: str, default=None):
    return os.getenv(name, default)

def _int_list(name: str, lst: list) -> list[int]:
    if lst:
        return [int(x) for x in lst]
    return [int(x) for x in _env(name, "").split(",") if x.strip().lstrip("-").isdigit()]

# Environment variables override blank values
BOT_TOKEN  = BOT_TOKEN  or _env("BOT_TOKEN",  "") or ""
API_ID     = int(API_ID or _env("API_ID",  0) or 0)
API_HASH   = API_HASH   or _env("API_HASH",  "") or ""
OWNER_IDS      = _int_list("OWNER_IDS",      OWNER_IDS)
AUTHORIZED_IDS = _int_list("AUTHORIZED_IDS", AUTHORIZED_IDS)
PRIVATE_MODE   = bool(AUTHORIZED_IDS)
DUMP_CHANNEL   = DUMP_CHANNEL or (int(_env("DUMP_CHANNEL", 0) or 0) or None)
INSTAGRAM_SESSIONID = INSTAGRAM_SESSIONID or _env("INSTAGRAM_SESSIONID")
TERABOX_NDUS  = TERABOX_NDUS  or _env("TERABOX_NDUS") or None
TG_MAX_SIZE   = int(TG_MAX_SIZE or _env("TG_MAX_SIZE", str(2 * 1024**3)))

DEFAULT_VIDEO_QUALITY   = DEFAULT_VIDEO_QUALITY   or _env("DEFAULT_VIDEO_QUALITY",   "best")
DEFAULT_AUDIO_FORMAT    = DEFAULT_AUDIO_FORMAT    or _env("DEFAULT_AUDIO_FORMAT",    "m4a")
MAX_CONCURRENT_TASKS    = int(MAX_CONCURRENT_TASKS    or _env("MAX_CONCURRENT_TASKS",    "5"))
MAX_QUEUE_PER_USER      = int(MAX_QUEUE_PER_USER      or _env("MAX_QUEUE_PER_USER",      "20"))
TASK_TIMEOUT_SECONDS    = int(TASK_TIMEOUT_SECONDS    or _env("TASK_TIMEOUT_SECONDS",    "21600"))
RATE_LIMIT_REQUESTS     = int(RATE_LIMIT_REQUESTS     or _env("RATE_LIMIT_REQUESTS",     "20"))
RATE_LIMIT_WINDOW_SECONDS = int(RATE_LIMIT_WINDOW_SECONDS or _env("RATE_LIMIT_WINDOW_SECONDS", "60"))
ARIA2_CONNECTIONS       = int(ARIA2_CONNECTIONS       or _env("ARIA2_CONNECTIONS", "16"))
ARIA2_MAX_SPLITS        = int(ARIA2_MAX_SPLITS        or _env("ARIA2_MAX_SPLITS",  "16"))

_max_gb = float(MAX_DOWNLOAD_SIZE_GB or _env("MAX_DOWNLOAD_SIZE_GB", "100") or "100")
MAX_DOWNLOAD_SIZE: int = int(_max_gb * 1024 ** 3)

# Validate required credentials on startup
if not BOT_TOKEN:
    print("[FATAL] BOT_TOKEN is not set — open config.py or set the BOT_TOKEN environment variable", file=sys.stderr)
    sys.exit(1)
if not API_ID:
    print("[FATAL] API_ID is not set — open config.py or set the API_ID environment variable", file=sys.stderr)
    sys.exit(1)
if not API_HASH:
    print("[FATAL] API_HASH is not set — open config.py or set the API_HASH environment variable", file=sys.stderr)
    sys.exit(1)

DATABASE_URL: str = _env("BOT_DATABASE_URL", "sqlite+aiosqlite:///./bot_data.db")
LOG_LEVEL:    str = _env("LOG_LEVEL", "INFO")

# ── Streaming server ──────────────────────────────────────────────────────────
# Port the built-in HTTP stream server binds to.
STREAM_PORT: int = int(_env("STREAM_PORT", "8765") or "8765")
# Public base URL used in stream links sent to Telegram users.
# Set this to your VPS address, e.g.:  http://1.2.3.4:8765
# On a domain with reverse proxy:      https://stream.example.com
# Leave empty to disable /stream command output (server still starts).
STREAM_BASE_URL: str = (_env("STREAM_BASE_URL") or "").rstrip("/")

# Auto-detect public address when STREAM_BASE_URL is not set
if not STREAM_BASE_URL:
    # 1. Replit hosted — proxy routes /stream/* to port 8765, no port in URL
    _replit_domain = _env("REPLIT_DOMAINS")
    if _replit_domain:
        STREAM_BASE_URL = f"https://{_replit_domain}"

if not STREAM_BASE_URL:
    # 2. VPS — fetch public IP from a lightweight metadata service
    import urllib.request
    _IP_SERVICES = [
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://ipv4.icanhazip.com",
    ]
    for _svc in _IP_SERVICES:
        try:
            with urllib.request.urlopen(_svc, timeout=3) as _r:
                _ip = _r.read().decode().strip()
            if _ip:
                STREAM_BASE_URL = f"http://{_ip}:{STREAM_PORT}"
                break
        except Exception:
            continue

DOWNLOAD_DIR  = Path("/tmp")
THUMBNAIL_DIR = Path("/tmp")
COOKIES_DIR   = Path("/tmp")

USE_ARIA2    = True
ARIA2_HOST   = "http://localhost"
ARIA2_PORT   = 6800
ARIA2_SECRET: str | None = None

GDRIVE_CREDENTIALS_FILE: str | None = _env("GDRIVE_CREDENTIALS_FILE")
GDRIVE_TOKEN_FILE: str                = _env("GDRIVE_TOKEN_FILE", "gdrive_token.json")
GDRIVE_FOLDER_ID:  str | None        = _env("GDRIVE_FOLDER_ID")

# Cookie file paths (auto-discovered from bot/data/cookies/)
_COOKIE_DIR = Path(__file__).parent / "data" / "cookies"
_COOKIE_DIR.mkdir(parents=True, exist_ok=True)

TERABOX_COOKIE_FILE:   str | None = str(_COOKIE_DIR / "terabox.txt")   if (_COOKIE_DIR / "terabox.txt").exists()   else None
INSTAGRAM_COOKIE_FILE: str | None = str(_COOKIE_DIR / "instagram.txt") if (_COOKIE_DIR / "instagram.txt").exists() else _env("INSTAGRAM_COOKIE_FILE")
TIKTOK_COOKIE_FILE:    str | None = str(_COOKIE_DIR / "tiktok.txt")   if (_COOKIE_DIR / "tiktok.txt").exists()   else _env("TIKTOK_COOKIE_FILE")
YOUTUBE_COOKIE_FILE:   str | None = str(_COOKIE_DIR / "youtube.txt")  if (_COOKIE_DIR / "youtube.txt").exists()  else None
FACEBOOK_COOKIE_FILE:  str | None = str(_COOKIE_DIR / "facebook.txt") if (_COOKIE_DIR / "facebook.txt").exists() else None
TWITTER_COOKIE_FILE:   str | None = str(_COOKIE_DIR / "twitter.txt")  if (_COOKIE_DIR / "twitter.txt").exists()  else None
GENERIC_COOKIE_FILE:   str | None = str(_COOKIE_DIR / "cookies.txt")  if (_COOKIE_DIR / "cookies.txt").exists()  else None
ALL_COOKIES_FILE:      str | None = str(_COOKIE_DIR / "all_cookies.txt") if (_COOKIE_DIR / "all_cookies.txt").exists() else None

# Extract ndus from the Terabox cookie file if present
if TERABOX_COOKIE_FILE and not TERABOX_NDUS:
    try:
        with open(TERABOX_COOKIE_FILE) as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    parts = line.split()
                    if len(parts) >= 7 and parts[5] == "ndus":
                        TERABOX_NDUS = parts[6]
                        break
    except Exception:
        pass

# Extract Instagram sessionid from the Instagram cookie file and build a
# proper instaloader session file (pickled requests.Session) if needed.
if INSTAGRAM_COOKIE_FILE and not INSTAGRAM_SESSIONID:
    try:
        import requests  # type: ignore
        import pickle
        session = requests.Session()
        sessionid_value = None
        with open(INSTAGRAM_COOKIE_FILE) as f:
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 7:
                    domain, path, secure, expires, name, value = parts[0], parts[2], parts[3], parts[4], parts[5], parts[6]
                    try:
                        expires = int(expires) if expires else None
                    except Exception:
                        expires = None
                    session.cookies.set(name, value, domain=domain, path=path)
                    if name == "sessionid":
                        sessionid_value = value
        if sessionid_value:
            _session_path = Path(__file__).parent / "data" / "instagram_session.pkl"
            with open(_session_path, "wb") as fh:
                pickle.dump(session, fh)
            INSTAGRAM_SESSIONID = str(_session_path)
    except Exception:
        pass
