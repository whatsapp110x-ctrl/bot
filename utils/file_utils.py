"""
File system utilities: MIME detection, splitting, archive handling, cleanup.
"""

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import filetype

logger = logging.getLogger(__name__)

# Video/audio/image extensions recognized for smart Telegram upload type
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".m4v", ".3gp"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}


def detect_mime(path: str | Path) -> str | None:
    """Use libmagic via filetype to detect MIME type."""
    kind = filetype.guess(str(path))
    return kind.mime if kind else None


def classify_file(path: str | Path) -> str:
    """
    Return one of: 'video', 'audio', 'photo', 'document'
    Used to decide how to send a file to Telegram.
    """
    p = Path(path)
    ext = p.suffix.lower()
    mime = detect_mime(p) or ""

    if ext in VIDEO_EXTS or mime.startswith("video/"):
        return "video"
    if ext in AUDIO_EXTS or mime.startswith("audio/"):
        return "audio"
    if ext in IMAGE_EXTS or mime.startswith("image/"):
        return "photo"
    return "document"


def ensure_mp4(path: Path) -> Path:
    """
    If a video file doesn't have a .mp4 extension, rename it.
    yt-dlp sometimes produces .webm or .mkv containers.
    Returns new path.
    """
    if path.suffix.lower() not in (".mp4",):
        new_path = path.with_suffix(".mp4")
        path.rename(new_path)
        return new_path
    return path


def split_file(path: Path, chunk_size: int = 1_900 * 1024 * 1024) -> list[Path]:
    """
    Split a large file into chunks of `chunk_size` bytes.
    Returns list of chunk paths. If the file fits, returns [path].
    """
    if path.stat().st_size <= chunk_size:
        return [path]

    parts: list[Path] = []
    part_index = 0

    with open(path, "rb") as src:
        while True:
            data = src.read(chunk_size)
            if not data:
                break
            part_path = path.with_name(f"{path.stem}.part{part_index:03d}{path.suffix}")
            part_path.write_bytes(data)
            parts.append(part_path)
            part_index += 1

    logger.info("Split %s into %d parts", path.name, len(parts))
    return parts


async def extract_archive(archive_path: Path, dest_dir: Path, password: str | None = None) -> list[Path]:
    """
    Extract a zip/7z/rar/tar.* archive using py7zr (7z/zip) with patool fallback.
    Returns list of extracted file paths.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    ext = archive_path.suffix.lower()

    try:
        if ext in (".7z", ".zip"):
            import py7zr  # type: ignore
            with py7zr.SevenZipFile(str(archive_path), mode="r", password=password) as z:
                z.extractall(path=str(dest_dir))
        else:
            # patool handles rar, tar, bz2, gz, xz, etc.
            cmd = ["patool", "extract", "--outdir", str(dest_dir), str(archive_path)]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"patool failed: {stderr.decode()}")
    except Exception as exc:
        logger.error("Extraction failed for %s: %s", archive_path, exc)
        raise

    return list(dest_dir.rglob("*"))


async def create_zip(source_dir: Path, dest_path: Path, password: str | None = None) -> Path:
    """Create a zip archive from source_dir."""
    import py7zr

    def _zip():
        with py7zr.SevenZipFile(str(dest_path), mode="w", password=password) as z:
            z.writeall(str(source_dir), arcname=source_dir.name)

    await asyncio.get_running_loop().run_in_executor(None, _zip)
    return dest_path


def get_video_duration(path: Path) -> int:
    """Return video duration in seconds using ffprobe, or 0 on failure."""
    try:
        import json
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(path),
            ],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        return int(float(data["format"].get("duration", 0)))
    except Exception:
        return 0


def get_video_dimensions(path: Path) -> tuple[int, int]:
    """Return (width, height) of a video using ffprobe, or (0, 0)."""
    try:
        import json
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-select_streams", "v:0",
                str(path),
            ],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return stream.get("width", 0), stream.get("height", 0)
    except Exception:
        return 0, 0


def safe_filename(name: str, max_len: int = 200) -> str:
    """Strip filesystem-unsafe characters and truncate."""
    unsafe = r'\/:*?"<>|'
    for ch in unsafe:
        name = name.replace(ch, "_")
    name = name[:max_len].strip()
    return name if name else "download"


async def cleanup_path(path: Path) -> None:
    """Remove a file or directory tree safely."""
    try:
        if path.is_dir():
            await asyncio.get_running_loop().run_in_executor(None, shutil.rmtree, path)
        elif path.exists():
            path.unlink()
    except Exception as exc:
        logger.warning("Cleanup failed for %s: %s", path, exc)
