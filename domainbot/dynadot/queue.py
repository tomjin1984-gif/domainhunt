from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar


logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(order=True)
class _QueuedCall:
    priority: int
    sequence: int
    label: str = field(compare=False)
    operation: Callable[[], Awaitable[Any]] | None = field(compare=False)
    future: asyncio.Future[Any] | None = field(compare=False)


class DynadotAPIQueue:
    """Single-worker priority queue for every Dynadot API request."""

    def __init__(self, *, min_interval_seconds: float = 1.0):
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._queue: asyncio.PriorityQueue[_QueuedCall] = asyncio.PriorityQueue()
        self._counter = itertools.count()
        self._worker_task: asyncio.Task[None] | None = None
        self._last_request_started_at: float | None = None
        self._active_requests = 0
        self.max_observed_active_requests = 0
        self._closed = False

    async def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._closed = False
            self._worker_task = asyncio.create_task(self._worker(), name="dynadot-api-queue")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(_QueuedCall(9999, next(self._counter), "stop", None, None))
        if self._worker_task is not None:
            await self._worker_task

    async def call(self, priority: int, label: str, operation: Callable[[], Awaitable[T]]) -> T:
        await self.start()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        await self._queue.put(_QueuedCall(priority, next(self._counter), label, operation, future))
        return await future

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item.operation is None:
                    return
                await self._respect_rate_limit()
                self._active_requests += 1
                self.max_observed_active_requests = max(self.max_observed_active_requests, self._active_requests)
                self._last_request_started_at = time.monotonic()
                try:
                    logger.debug("dynadot queue start label=%s priority=%s", item.label, item.priority)
                    result = await item.operation()
                except Exception as exc:
                    if item.future is not None and not item.future.done():
                        item.future.set_exception(exc)
                else:
                    if item.future is not None and not item.future.done():
                        item.future.set_result(result)
                finally:
                    self._active_requests -= 1
                    logger.debug("dynadot queue done label=%s", item.label)
            finally:
                self._queue.task_done()

    async def _respect_rate_limit(self) -> None:
        if self._last_request_started_at is None or self.min_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_request_started_at
        delay = self.min_interval_seconds - elapsed
        if delay > 0:
            await asyncio.sleep(delay)

