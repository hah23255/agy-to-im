"""Unit tests for src.queue — TurnQueue submit/get semantics.

The queue is consumed by a single daemon worker task: submit() never blocks
the polling loop, get()/task_done()/join() are worker-side primitives.
"""
from __future__ import annotations

import asyncio

from src.queue import TurnQueue
from src.telegram import InboundMessage


def _m(chat_id: int = 10, text: str = "hi", update_id: int = 1) -> InboundMessage:
    return InboundMessage(update_id=update_id, chat_id=chat_id, user_id=42, text=text)


def test_submit_when_idle_returns_none_and_claims_worker() -> None:
    q = TurnQueue()
    assert q.submit(_m()) is None
    assert q.active is True
    assert q.qsize() == 1


def test_submit_when_busy_returns_position_feedback() -> None:
    q = TurnQueue()
    q.submit(_m(chat_id=1, update_id=1))
    status = q.submit(_m(chat_id=2, update_id=2))
    assert status is not None
    assert "Queued" in status
    assert "#1" in status  # one turn in service, this one is first in line
    status2 = q.submit(_m(chat_id=3, update_id=3))
    assert status2 is not None
    assert "#2" in status2


def test_submit_rejects_when_queue_full() -> None:
    q = TurnQueue(max_depth=2)
    assert q.submit(_m(chat_id=1, update_id=1)) is None
    assert q.submit(_m(chat_id=2, update_id=2)) is not None
    status = q.submit(_m(chat_id=3, update_id=3))
    assert status is not None
    assert "full" in status.lower()
    assert q.qsize() == 2, "rejected message must not be enqueued"


async def test_get_returns_items_in_fifo_order() -> None:
    q = TurnQueue()
    q.submit(_m(chat_id=1, update_id=1))
    q.submit(_m(chat_id=2, update_id=2))
    first = await asyncio.wait_for(q.get(), timeout=1)
    assert first.chat_id == 1
    q.task_done()
    second = await asyncio.wait_for(q.get(), timeout=1)
    assert second.chat_id == 2


def test_complete_clears_active_flag_only_when_queue_empty() -> None:
    q = TurnQueue()
    q.submit(_m(chat_id=1, update_id=1))
    q.submit(_m(chat_id=2, update_id=2))
    q.complete()  # item 2 still pending → still active
    assert q.active is True
    q._q.get_nowait()
    q._q.get_nowait()
    q.complete()
    assert q.active is False


async def test_join_waits_until_all_items_processed() -> None:
    q = TurnQueue()
    q.submit(_m(chat_id=10, update_id=1))
    q.submit(_m(chat_id=11, update_id=2))
    processed: list[int] = []

    async def worker() -> None:
        while True:
            msg = await q.get()
            await asyncio.sleep(0.01)
            processed.append(msg.chat_id)
            q.task_done()
            q.complete()

    w = asyncio.create_task(worker())
    await asyncio.wait_for(q.join(), timeout=2)
    assert processed == [10, 11]
    assert q.active is False
    w.cancel()


def test_status_reports_active_and_depth() -> None:
    q = TurnQueue()
    assert "no" in q.status()[0]
    q.submit(_m())
    lines = q.status()
    assert any("yes" in line for line in lines)
    assert any("1" in line for line in lines)


def test_reset_clears_queue_and_state() -> None:
    q = TurnQueue()
    q.submit(_m(update_id=1))
    q.submit(_m(chat_id=11, update_id=2))
    q.check_ratelimit(42)
    q.reset()
    assert q.qsize() == 0
    assert q.active is False
    # Rate-limit history must be gone too
    ok, _ = q.check_ratelimit(42)
    assert ok


def test_check_ratelimit_sliding_window_per_user() -> None:
    q = TurnQueue(max_per_user=2, cooldown_seconds=2)
    ok1, _ = q.check_ratelimit(1)
    ok2, _ = q.check_ratelimit(1)
    ok3, wait = q.check_ratelimit(1)
    assert ok1 and ok2
    assert not ok3
    assert wait >= 1
    # Another user is unaffected
    ok_other, _ = q.check_ratelimit(2)
    assert ok_other
