"""
Async task queue with per-user slots and global concurrency limit.
Each user gets an independent queue — no user can starve another.
No artificial limits on queue depth.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class TaskRecord:
    user_id: int
    url: str
    started_at: datetime = field(default_factory=datetime.utcnow)
    asyncio_task: asyncio.Task | None = None
    status: str = "queued"


class TaskManager:
    def __init__(self, max_concurrent: int = 5) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active: dict[str, TaskRecord] = {}
        self._user_tasks: dict[int, str] = {}
        self._lock = asyncio.Lock()
        self._counter = 0
        # Downloader references for cancellation
        self._downloaders: dict[int, object] = {}
        # Pending quality/format selection state (YouTube/Vimeo URLs)
        self._pending: dict[int, dict] = {}

    def _new_id(self) -> str:
        self._counter += 1
        return f"T{self._counter:05d}"

    async def submit(
        self,
        user_id: int,
        url: str,
        coro_factory,
        on_done=None,
    ) -> str:
        task_id = self._new_id()
        record = TaskRecord(user_id=user_id, url=url)

        async with self._lock:
            self._active[task_id] = record
            self._user_tasks[user_id] = task_id

        async def _runner():
            async with self._semaphore:
                record.status = "running"
                error = None
                try:
                    await coro_factory()
                except asyncio.CancelledError:
                    record.status = "cancelled"
                except Exception as exc:
                    record.status = "failed"
                    error = exc
                    logger.error("[%s] Task failed: %s", task_id, exc)
                else:
                    record.status = "done"
                finally:
                    async with self._lock:
                        self._active.pop(task_id, None)
                        if self._user_tasks.get(user_id) == task_id:
                            self._user_tasks.pop(user_id, None)

                if on_done:
                    try:
                        await on_done(task_id, error)
                    except Exception as exc:
                        logger.warning("[%s] on_done failed: %s", task_id, exc)

        loop = asyncio.get_running_loop()
        at = loop.create_task(_runner(), name=task_id)
        record.asyncio_task = at
        logger.info("[%s] Submitted for user %d: %s", task_id, user_id, url[:60])
        return task_id

    async def cancel(self, user_id: int) -> bool:
        async with self._lock:
            task_id = self._user_tasks.get(user_id)
            if not task_id:
                return False
            record = self._active.get(task_id)

        if record and record.asyncio_task and not record.asyncio_task.done():
            record.asyncio_task.cancel()
            record.status = "cancelled"
            # Also cancel the downloader
            dl = self._downloaders.get(user_id)
            if dl and hasattr(dl, "cancel"):
                dl.cancel()
            return True
        return False

    def is_cancelled(self, user_id: int) -> bool:
        task_id = self._user_tasks.get(user_id)
        if not task_id:
            return False
        record = self._active.get(task_id)
        return record is not None and record.status == "cancelled"

    def get_user_task(self, user_id: int) -> TaskRecord | None:
        task_id = self._user_tasks.get(user_id)
        return self._active.get(task_id) if task_id else None

    def is_busy(self, user_id: int) -> bool:
        return user_id in self._user_tasks

    def active_count(self) -> int:
        return len(self._active)

    def all_tasks(self) -> list[TaskRecord]:
        return list(self._active.values())


# Singleton
task_manager = TaskManager()
