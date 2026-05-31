"""
Human-readable formatting helpers (size, time, progress bar).
Consolidated from ColabLeechBot's helper.py and Media-Downloader-Bot's helper.py.
"""

import math
import time


def format_size(num_bytes: int | float, suffix: str = "B") -> str:
    """Return a human-readable file size string."""
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:6.1f} {unit}{suffix}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} Yi{suffix}"


def format_speed(bytes_per_sec: float) -> str:
    return f"{format_size(bytes_per_sec)}/s"


def format_eta(seconds: float) -> str:
    """Convert seconds to human-readable ETA string."""
    if seconds <= 0:
        return "0s"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def progress_bar(percentage: float, width: int = 10) -> str:
    """
    Return a Unicode block progress bar.
    E.g. percentage=45.0 → '████░░░░░░'
    """
    filled = int(percentage / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    return bar


def moon_bar(percentage: float, width: int = 10) -> str:
    """
    Moon-phase progress bar inspired by Media-Downloader-Bot.
    Progress fills left to right: 🌑 → 🌒 → 🌓 → 🌔 → 🌕
    """
    progress = max(0.0, min(100.0, percentage)) / 100
    filled = int(progress * width)
    remainder = (progress * width) - filled

    partial = ""
    if filled < width and remainder > 0:
        if remainder >= 0.67:
            partial = "🌔"
        elif remainder >= 0.34:
            partial = "🌓"
        else:
            partial = "🌒"

    full_count = filled
    empty_count = width - filled - (1 if partial else 0)
    return "🌕" * full_count + partial + "🌑" * empty_count


def build_progress_message(
    action: str,
    filename: str,
    done_bytes: int,
    total_bytes: int,
    speed: float,
    elapsed: float,
    engine: str = "",
) -> str:
    """Build the standard progress status message sent to Telegram."""
    percentage = (done_bytes / total_bytes * 100) if total_bytes > 0 else 0
    remaining = total_bytes - done_bytes
    eta = (remaining / speed) if speed > 0 else 0

    bar = moon_bar(percentage)

    lines = [
        f"**{action}**",
        f"`{filename[:60]}`",
        "",
        f"{bar} `{percentage:.1f}%`",
        f"**Done:** `{format_size(done_bytes)}`  /  `{format_size(total_bytes)}`",
        f"**Speed:** `{format_speed(speed)}`",
        f"**ETA:** `{format_eta(eta)}`",
    ]
    if engine:
        lines.append(f"**Engine:** `{engine}`")

    return "\n".join(lines)
