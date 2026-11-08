"""Turn queue — FIFO async queue for multi-user concurrency."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import QueueConfig
    from src.telegram import InboundMessage

LOG = logging.getLogger("antigravity_telegram_bridge")
MAX_QUEUE_DEPTH = 5


class RateLimit:
    __slots__ = ("timestamps",)
    def __init__(self):
        self.timestamps: deque[float] = deque()


@dataclass
class TurnQueue:
    """FIFO queue executed by a single daemon worker — one agy turn at a time.

    submit() never blocks the polling loop: it either hands the message to
    the worker (returns None) or parks it in the FIFO and returns a status
    string for the user. The worker consumes via get()/task_done().
    """
    max_depth: int = MAX_QUEUE_DEPTH
    max_per_user: int = 5
    cooldown_seconds: int = 2
    active: bool = False
    _q: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False, compare=False)
    _limits: dict[int, RateLimit] = field(default_factory=dict, repr=False, compare=False)

    def submit(self, msg: "InboundMessage") -> str | None:
        """Enqueue a message for processing. Returns None when the worker will
        pick it up immediately, or a status string when it was parked.

        Synchronous by design — safe to call from the polling loop while a
        turn is executing, because the loop never waits on the turn itself.
        """
        if self._q.qsize() >= self.max_depth:
            return "🚫 Queue full. Try again shortly."
        busy = self.active or self._q.qsize() > 0
        self._q.put_nowait(msg)
        # A turn is now pending or executing until the worker completes it.
        self.active = True
        if busy:
            return f"⏳ Queued (position #{self._q.qsize() - 1}). Processing soon…"
        return None

    async def get(self) -> "InboundMessage":
        """Worker-side: wait for the next message to execute."""
        return await self._q.get()

    def task_done(self) -> None:
        """Worker-side: mark the item taken via get() as fully processed."""
        self._q.task_done()

    async def join(self) -> None:
        """Wait until every submitted item has been processed (graceful drain)."""
        await self._q.join()

    def qsize(self) -> int:
        return self._q.qsize()

    def complete(self) -> None:
        """Clear the active flag once nothing is pending or executing."""
        if self._q.qsize() == 0:
            self.active = False

    def reset(self) -> None:
        """Drop all queued messages and bookkeeping (used at daemon startup)."""
        self.active = False
        self._limits.clear()
        self._q = asyncio.Queue()

    def check_ratelimit(self, user_id: int) -> tuple[bool, int]:
        """Returns (allowed, wait_seconds). Sliding window per user."""
        now = time.time()
        rl = self._limits.setdefault(user_id, RateLimit())
        window = self.cooldown_seconds * 2
        while rl.timestamps and rl.timestamps[0] < now - window:
            rl.timestamps.popleft()
        if len(rl.timestamps) >= self.max_per_user:
            wait = int(rl.timestamps[0] + window - now) + 1
            return False, max(wait, 0)
        rl.timestamps.append(now)
        return True, 0

    def status(self) -> list[str]:
        lines = [f"Active: {'yes' if self.active else 'no'}"]
        if self._q.qsize():
            lines.append(f"Queue depth: {self._q.qsize()}")
        else:
            lines.append("Queue: empty")
        return lines
