"""
SQLAlchemy ORM models.

Improvements over Media-Downloader-Bot:
- SQLite-first (no MySQL dependency) via async SQLAlchemy
- Alembic-ready (declarative_base + mapped_column)
- Credit system is optional (guarded by ENABLE_CREDITS)
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(100))
    username: Mapped[Optional[str]] = mapped_column(String(100))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, onupdate=func.now())

    # Credit system
    free_credits: Mapped[int] = mapped_column(Integer, default=10)
    paid_credits: Mapped[int] = mapped_column(Integer, default=0)
    bandwidth_used: Mapped[int] = mapped_column(BigInteger, default=0)  # bytes, resets daily
    total_bandwidth: Mapped[int] = mapped_column(BigInteger, default=0)  # all-time bytes

    settings: Mapped[Optional["UserSettings"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    downloads: Mapped[list["DownloadHistory"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User telegram_id={self.telegram_id} username={self.username}>"


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), unique=True, nullable=False)

    # Download preferences
    video_quality: Mapped[str] = mapped_column(
        Enum("best", "2160", "1080", "720", "480", "360", "audio"), default="best"
    )
    output_format: Mapped[str] = mapped_column(
        Enum("video", "audio", "document"), default="video"
    )
    audio_format: Mapped[str] = mapped_column(
        Enum("m4a", "mp3", "opus", "flac", "wav"), default="m4a"
    )
    subtitles: Mapped[bool] = mapped_column(Boolean, default=False)

    # Upload preferences
    send_as_document: Mapped[bool] = mapped_column(Boolean, default=False)
    caption_style: Mapped[str] = mapped_column(
        Enum("plain", "bold", "code", "italic"), default="bold"
    )
    filename_prefix: Mapped[str] = mapped_column(String(64), default="")
    filename_suffix: Mapped[str] = mapped_column(String(64), default="")

    # Thumbnail
    custom_thumbnail: Mapped[Optional[str]] = mapped_column(Text)  # file path

    user: Mapped["User"] = relationship(back_populates="settings")


class DownloadHistory(Base):
    __tablename__ = "download_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[Optional[str]] = mapped_column(String(512))
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    engine: Mapped[Optional[str]] = mapped_column(String(32))  # ytdl, gdrive, direct, etc.
    status: Mapped[str] = mapped_column(
        Enum("success", "failed", "cancelled"), default="success"
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    user: Mapped["User"] = relationship(back_populates="downloads")
