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
    """FIFO queue ensuring one agy turn at a time across all chats.

    When a turn is active, subsequent messages are enqueued.
    Each chat may have at most one pending message.
    Owner (first in allowed_user_ids) always skips the queue.
    """
    active: bool = False
    pending: list[tuple[int, "InboundMessage", asyncio.Future[str | None]]] = field(default_factory=list)
    owner_chat_id: int = 0
    # Rate limit state
    _limits: dict[int, RateLimit] = field(default_factory=dict)
    max_per_user: int = 5
    cooldown_seconds: int = 2

    def _pos(self, chat_id: int) -> int:
        for i, (cid, _, _) in enumerate(self.pending):
            if cid == chat_id:
                return i + 1
        return len(self.pending) + 1

    def _already_queued(self, chat_id: int) -> bool:
        return any(cid == chat_id for cid, _, _ in self.pending)

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

    async def submit(self, msg: "InboundMessage") -> str | None:
        """Submit a message for processing. Returns queued status str or None to proceed.

        Returns None when the caller should execute immediately.
        Returns a str when the message was enqueued (status for user).
        """
        cid = msg.chat_id

        # Owner bypass
        if cid == self.owner_chat_id:
            return None

        # Already active — enqueue
        if self.active:
            if self._already_queued(cid):
                return None  # replace previous
            if len(self.pending) >= MAX_QUEUE_DEPTH:
                return "🚫 Queue full. Try again shortly."
            fut: asyncio.Future[str | None] = asyncio.Future()
            self.pending.append((cid, msg, fut))
            return f"⏳ Queued (position #{len(self.pending)}). Processing soon…"

        # Not active — proceed
        self.active = True
        return None

    def complete(self) -> None:
        """Mark current turn as complete."""
        self.active = False

    def next(self) -> tuple["InboundMessage", asyncio.Future[str | None]] | None:
        """Return next queued message or None."""
        if not self.pending:
            return None
        _, msg, fut = self.pending.pop(0)
        self.active = True
        return msg, fut

    def status(self) -> list[str]:
        lines = [f"Active: {'yes' if self.active else 'no'}"]
        if self.pending:
            lines.append(f"Queue ({len(self.pending)}):")
            for i, (cid, msg, _) in enumerate(self.pending):
                preview = msg.text[:40] + ("…" if len(msg.text) > 40 else "")
                lines.append(f"  #{i+1} chat={cid} \"{preview}\"")
        else:
            lines.append("Queue: empty")
        return lines
