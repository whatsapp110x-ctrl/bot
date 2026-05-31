"""
Async SQLAlchemy engine + session factory + CRUD helpers.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DATABASE_URL
from .models import Base, User, UserSettings, DownloadHistory

logger = logging.getLogger(__name__)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Create all tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

async def get_or_create_user(telegram_id: int, first_name: str | None = None, username: str | None = None) -> User:
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(telegram_id=telegram_id, first_name=first_name, username=username)
            session.add(user)
            await session.flush()

            settings = UserSettings(user_id=user.id)
            session.add(settings)

        else:
            # Update name/username if changed
            if first_name and user.first_name != first_name:
                user.first_name = first_name
            if username and user.username != username:
                user.username = username

        return user


async def get_user(telegram_id: int) -> User | None:
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()


async def get_user_settings(telegram_id: int) -> UserSettings | None:
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(
            select(UserSettings)
            .join(User)
            .where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()


async def update_user_settings(telegram_id: int, **kwargs) -> None:
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(
            select(UserSettings)
            .join(User)
            .where(User.telegram_id == telegram_id)
        )
        settings = result.scalar_one_or_none()
        if settings:
            for key, value in kwargs.items():
                if hasattr(settings, key):
                    setattr(settings, key, value)


async def log_download(
    telegram_id: int,
    url: str,
    file_name: str | None,
    file_size: int,
    engine: str | None,
    status: str,
    error_message: str | None = None,
) -> None:
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            return

        record = DownloadHistory(
            user_id=user.id,
            url=url,
            file_name=file_name,
            file_size=file_size,
            engine=engine,
            status=status,
            error_message=error_message,
        )
        session.add(record)

        if status == "success":
            user.total_bandwidth += file_size
            user.bandwidth_used += file_size


async def block_user(telegram_id: int) -> bool:
    user = await get_user(telegram_id)
    if not user:
        return False
    async with get_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        u = result.scalar_one_or_none()
        if u:
            u.is_blocked = True
            return True
    return False


async def unblock_user(telegram_id: int) -> bool:
    async with get_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        u = result.scalar_one_or_none()
        if u:
            u.is_blocked = False
            return True
    return False


async def get_all_users() -> list[User]:
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()))
        return list(result.scalars().all())


async def reset_daily_bandwidth() -> None:
    """Called by APScheduler at midnight to reset daily bandwidth counters."""
    from sqlalchemy import update

    async with get_session() as session:
        await session.execute(update(User).values(bandwidth_used=0))
    logger.info("Daily bandwidth reset complete")


async def get_download_stats() -> dict:
    """Return aggregate download statistics from DownloadHistory."""
    from sqlalchemy import select, func

    async with get_session() as session:
        total = (await session.execute(
            select(func.count()).select_from(DownloadHistory)
        )).scalar_one()

        success = (await session.execute(
            select(func.count()).select_from(DownloadHistory)
            .where(DownloadHistory.status == "success")
        )).scalar_one()

        failed = (await session.execute(
            select(func.count()).select_from(DownloadHistory)
            .where(DownloadHistory.status == "failed")
        )).scalar_one()

        cancelled = (await session.execute(
            select(func.count()).select_from(DownloadHistory)
            .where(DownloadHistory.status == "cancelled")
        )).scalar_one()

        total_bytes = (await session.execute(
            select(func.coalesce(func.sum(DownloadHistory.file_size), 0))
            .select_from(DownloadHistory)
        )).scalar_one()

    return {
        "total": total or 0,
        "success": success or 0,
        "failed": failed or 0,
        "cancelled": cancelled or 0,
        "total_bytes": int(total_bytes or 0),
    }
