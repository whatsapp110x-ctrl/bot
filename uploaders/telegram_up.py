"""
Telegram uploader — smart type detection, progress, thumbnail,
automatic split for files over the Telegram size limit.
"""

import asyncio
import logging
import time
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Message

from config import TG_MAX_SIZE, THUMBNAIL_DIR, DUMP_CHANNEL
from utils.file_utils import classify_file, split_file, get_video_duration, get_video_dimensions
from utils.formatters import build_progress_message, format_size

logger = logging.getLogger(__name__)

PROGRESS_THROTTLE = 3.0


def _caption(filename: str, size: int, prefix: str, suffix: str, style: str, engine: str) -> str:
    name = f"{prefix}{filename}{suffix}".strip()
    sz = format_size(size)
    tags = {"bold": ("**", "**"), "italic": ("__", "__"), "code": ("`", "`"), "plain": ("", "")}
    o, c = tags.get(style, ("**", "**"))
    parts = [f"{o}{name}{c}", f"`{sz}`"]
    if engine:
        parts.append(f"via `{engine}`")
    return "\n".join(parts)


def _to_jpeg(src: Path, dest: Path) -> Path | None:
    """
    Convert any image (WebP, PNG, etc.) to JPEG <= 320px wide.
    Returns dest on success, None on failure.
    Telegram requires thumbnails to be JPEG, ≤320×320, ≤200 KB.
    """
    try:
        from PIL import Image  # type: ignore
        with Image.open(src) as img:
            img = img.convert("RGB")
            # Downscale so longest side ≤ 320
            img.thumbnail((320, 320), Image.LANCZOS)
            img.save(dest, "JPEG", quality=85, optimize=True)
        if dest.exists() and dest.stat().st_size > 0:
            return dest
    except Exception as exc:
        logger.debug("_to_jpeg failed: %s", exc)
    return None


async def _make_thumb(video_path: Path, sibling_thumb: Path | None = None) -> Path | None:
    """
    Return a JPEG thumbnail for video_path.

    Priority:
    1. sibling_thumb  — image saved by the downloader (converted to JPEG if needed)
    2. THUMBNAIL_DIR cache — previously extracted/converted frame
    3. ffmpeg frame extraction — extract at 1/4 duration
    """
    cached = THUMBNAIL_DIR / f"{video_path.stem}_thumb.jpg"

    # 1. Use the thumbnail the downloader already saved next to the video
    if sibling_thumb and sibling_thumb.exists() and sibling_thumb.stat().st_size > 1024:
        # If it's already a small JPEG, use it directly
        if sibling_thumb.suffix.lower() in (".jpg", ".jpeg"):
            # Still resize if too large
            converted = _to_jpeg(sibling_thumb, cached)
            return converted or sibling_thumb
        # Convert WebP / PNG → JPEG
        converted = _to_jpeg(sibling_thumb, cached)
        if converted:
            return converted

    # 2. Check persistent cache
    if cached.exists() and cached.stat().st_size > 1024:
        return cached

    # 3. Extract from video via ffmpeg
    try:
        dur = get_video_duration(video_path)
        seek = min(5, max(1, dur // 4)) if dur > 0 else 1
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", str(seek), "-i", str(video_path),
            "-frames:v", "1", "-q:v", "2", "-vf", "scale=320:-2",
            str(cached),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        return cached if cached.exists() and cached.stat().st_size > 1024 else None
    except Exception:
        return None


class TelegramUploader:
    def __init__(
        self,
        client: Client,
        dest_message: Message,
        caption_prefix: str = "",
        caption_suffix: str = "",
        caption_style: str = "bold",
        send_as_document: bool = False,
        custom_thumbnail: Path | None = None,
        dump_channel: int | None = DUMP_CHANNEL,
    ) -> None:
        self.client = client
        self.dest_message = dest_message
        self.caption_prefix = caption_prefix
        self.caption_suffix = caption_suffix
        self.caption_style = caption_style
        self.send_as_document = send_as_document
        self.custom_thumbnail = custom_thumbnail
        self.dump_channel = dump_channel
        self._last_edit = 0.0
        self._status_msg: Message | None = None

    async def upload_many(
        self,
        files: list[Path],
        engine: str = "",
        downloader=None,
    ) -> list[Message]:
        # Build a sibling-thumbnail map from the file list so thumbnails are
        # embedded with their video rather than uploaded as separate files.
        IMAGE_EXTS = {".jpg", ".jpeg", ".webp", ".png"}
        VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv", ".ts", ".3gp"}
        AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".flac", ".ogg", ".wav"}

        # Collect all image files that look like thumbnails
        thumb_files = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
        media_files = [f for f in files if f.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS]

        # Also pull any thumbnails the downloader stored in its .thumbnails dict
        dl_thumbs: dict[str, Path] = getattr(downloader, "thumbnails", {}) if downloader else {}

        # Build stem → thumb mapping (strip common _thumb suffix for matching)
        def _thumb_stem(p: Path) -> str:
            s = p.stem.lower()
            for suffix in ("_thumb", "_cover", "_thumbnail"):
                if s.endswith(suffix):
                    s = s[: -len(suffix)]
            return s

        sibling_map: dict[str, Path] = {}
        for t in thumb_files:
            sibling_map[_thumb_stem(t)] = t
        for stem, path in dl_thumbs.items():
            sibling_map[stem.lower()] = path

        # Decide which files to skip (thumbnails that will be embedded)
        embedded_thumbs: set[Path] = set()
        video_thumb_pairs: dict[Path, Path | None] = {}
        for mf in media_files:
            matched = sibling_map.get(mf.stem.lower())
            if matched:
                embedded_thumbs.add(matched)
            video_thumb_pairs[mf] = matched

        sent = []
        for f in files:
            if f in embedded_thumbs:
                # This image is a thumbnail — skip it as a standalone upload
                continue
            sibling = video_thumb_pairs.get(f)
            sent.extend(await self.upload_file(f, engine, sibling_thumb=sibling))
        return sent

    async def upload_file(
        self,
        file_path: Path,
        engine: str = "",
        sibling_thumb: Path | None = None,
    ) -> list[Message]:
        size = file_path.stat().st_size

        # Auto-split if too large for Telegram
        if size > TG_MAX_SIZE:
            parts = split_file(file_path, chunk_size=TG_MAX_SIZE - 50 * 1024 * 1024)
            logger.info("Split %s into %d parts", file_path.name, len(parts))
            all_sent = []
            for i, part in enumerate(parts, 1):
                cap = _caption(
                    part.name, part.stat().st_size,
                    self.caption_prefix, self.caption_suffix,
                    self.caption_style, engine
                ) + f"\n`Part {i}/{len(parts)}`"
                all_sent.extend(await self._send(part, cap, engine))
            return all_sent

        cap = _caption(
            file_path.name, size,
            self.caption_prefix, self.caption_suffix,
            self.caption_style, engine
        )
        return await self._send(file_path, cap, engine, sibling_thumb=sibling_thumb)

    async def _send(self, file_path: Path, caption: str, engine: str, sibling_thumb: Path | None = None) -> list[Message]:
        file_type = "document" if self.send_as_document else classify_file(file_path)
        size = file_path.stat().st_size
        start = time.monotonic()
        self._last_edit = start
        sent_messages = []

        async def progress(current: int, total: int) -> None:
            now = time.monotonic()
            if now - self._last_edit < PROGRESS_THROTTLE:
                return
            self._last_edit = now
            elapsed = max(now - start, 0.1)
            speed = current / elapsed
            text = build_progress_message("📤 Uploading", file_path.name, current, total, speed, elapsed, engine)
            if self._status_msg:
                try:
                    await self._status_msg.edit_text(text)
                except Exception:
                    pass

        self._status_msg = await self.dest_message.reply_text(
            f"📤 Uploading `{file_path.name}` ({format_size(size)})..."
        )

        try:
            msg = await self._send_typed(file_path, caption, file_type, progress, sibling_thumb=sibling_thumb)
            if msg:
                sent_messages.append(msg)
                if self.dump_channel:
                    try:
                        await msg.copy(self.dump_channel)
                    except Exception as exc:
                        logger.warning("Mirror to dump channel failed: %s", exc)
        finally:
            if self._status_msg:
                try:
                    await self._status_msg.delete()
                except Exception:
                    pass

        return sent_messages

    async def _send_typed(
        self,
        file_path: Path,
        caption: str,
        file_type: str,
        progress_cb,
        sibling_thumb: Path | None = None,
    ) -> Message | None:
        thumb = None
        kwargs = dict(caption=caption, progress=progress_cb)

        for attempt in range(4):
            try:
                if file_type == "video":
                    thumb = self.custom_thumbnail or await _make_thumb(file_path, sibling_thumb=sibling_thumb)
                    w, h = get_video_dimensions(file_path)
                    dur = get_video_duration(file_path)
                    return await self.dest_message.reply_video(
                        video=str(file_path),
                        supports_streaming=True,
                        width=w, height=h, duration=dur,
                        thumb=str(thumb) if thumb else None,
                        **kwargs,
                    )
                elif file_type == "audio":
                    return await self.dest_message.reply_audio(
                        audio=str(file_path),
                        thumb=str(self.custom_thumbnail) if self.custom_thumbnail else None,
                        **kwargs,
                    )
                elif file_type == "photo":
                    return await self.dest_message.reply_photo(
                        photo=str(file_path),
                        caption=caption,
                        progress=progress_cb,
                    )
                else:
                    return await self.dest_message.reply_document(
                        document=str(file_path),
                        thumb=str(self.custom_thumbnail) if self.custom_thumbnail else None,
                        **kwargs,
                    )

            except FloodWait as e:
                logger.warning("FloodWait — sleeping %ds", e.value)
                await asyncio.sleep(e.value + 1)

            except RPCError as exc:
                if attempt < 3:
                    logger.warning("Upload RPC error (attempt %d): %s — retrying as document", attempt + 1, exc)
                    file_type = "document"
                    await asyncio.sleep(2)
                else:
                    raise

        return None
