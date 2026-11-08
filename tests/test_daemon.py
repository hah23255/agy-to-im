"""Tests for src.daemon — orchestration with all I/O mocked."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from src.agy_runner import AgyResult
from src.config import AgyConfig, Config, TelegramConfig
from src.daemon import DaemonInfo, run
from src.state import load_state


pytestmark = pytest.mark.asyncio


@dataclass
class _FakeTelegram:
    updates_to_serve: list[list[dict[str, Any]]]
    sent_messages: list[tuple[int, str]]
    chat_actions: list[tuple[int, str]]
    registered_commands: list[dict[str, str]] | None = None
    events: list[str] = field(default_factory=list)
    get_updates_calls: list[tuple[int, int]] = field(default_factory=list)
    deleted_messages: list[tuple[int, int]] = field(default_factory=list)

    async def __aenter__(self) -> "_FakeTelegram":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        self.get_updates_calls.append((offset, timeout))
        if self.updates_to_serve:
            return self.updates_to_serve.pop(0)
        await asyncio.sleep(0)
        return []

    async def send_message(self, chat_id: int, text: str, *, keyboard: Any | None = None) -> int | None:
        self.events.append("send_message")
        self.sent_messages.append((chat_id, text))
        return len(self.sent_messages) - 1

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, *, keyboard: Any | None = None) -> None:
        if 0 <= message_id < len(self.sent_messages):
            self.sent_messages[message_id] = (chat_id, text)
        else:
            self.sent_messages.append((chat_id, text))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.events.append("delete_message")
        self.deleted_messages.append((chat_id, message_id))

    async def get_file(self, file_id: str) -> bytes:
        self.events.append(f"get_file:{file_id}")
        return b"file-bytes"

    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
        return None

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.chat_actions.append((chat_id, action))

    async def set_my_commands(
        self, commands: list[dict[str, str]], *, scope: Any = None
    ) -> dict[str, Any]:
        self.registered_commands = commands
        return {"ok": True}

    async def send_document(
        self, chat_id: int, file_path: Any, *, caption: str = ""
    ) -> int | None:
        self.sent_messages.append((chat_id, f"doc:{file_path}"))
        return 1


def _msg(text: str, *, update_id: int, user_id: int = 42, chat_id: int = 10) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "T"},
            "text": text,
        },
    }


def _cfg() -> Config:
    return Config(
        telegram=TelegramConfig(
            bot_token="TOKEN",
            allowed_user_ids=[42],
            allowed_chat_ids=[],
        ),
        agy=AgyConfig(default_workdir="/tmp", model="", mode="code"),
    )


async def _fake_execute_agy(
    tg: Any, chat_id: int, msg: Any, cs: Any, cfg: Any, agy_path: str,
) -> tuple[str, int]:
    return f"echo:{msg.text}", 0


async def test_run_replies_to_authorized_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "state.json"
    chats_root = tmp_path / "chats"
    tg = _FakeTelegram(
        updates_to_serve=[[_msg("hello", update_id=100)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    monkeypatch.setattr("src.daemon.execute_agy", _fake_execute_agy)

    await run(
        cfg=_cfg(),
        state_path=state_path,
        chats_root=chats_root,
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )

    assert tg.chat_actions and tg.chat_actions[0] == (10, "typing")
    assert len(tg.sent_messages) == 1
    chat_id, text = tg.sent_messages[0]
    assert chat_id == 10
    assert text == "echo:hello"
    assert tg.registered_commands is not None
    assert len(tg.registered_commands) >= 10

    persisted = load_state(state_path, chats_root)
    assert persisted.last_update_id == 100
    assert 10 in persisted.chats


async def test_run_drops_unauthorized_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tg = _FakeTelegram(
        updates_to_serve=[[_msg("hi", update_id=200, user_id=999)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    monkeypatch.setattr("src.daemon.execute_agy", _fake_execute_agy)

    await run(
        cfg=_cfg(),
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )
    assert tg.sent_messages == []
    assert tg.chat_actions == []


async def test_run_replies_with_error_when_agy_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_agy(*args: Any, **kwargs: Any) -> tuple[str, int]:
        return "", 2

    monkeypatch.setattr("src.daemon.execute_agy", failing_agy)

    tg = _FakeTelegram(
        updates_to_serve=[[_msg("hi", update_id=300)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    await run(
        cfg=_cfg(),
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )
    assert len(tg.sent_messages) == 1
    _, text = tg.sent_messages[0]
    assert "agy error" in text


async def test_run_uses_continue_after_first_successful_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    async def recording_agy(tg: Any, chat_id: int, msg: Any, cs: Any, cfg: Any, agy_path: str) -> tuple[str, int]:
        calls.append(cs.has_session)
        return "ok", 0

    monkeypatch.setattr("src.daemon.execute_agy", recording_agy)

    tg = _FakeTelegram(
        updates_to_serve=[
            [_msg("first", update_id=400)],
            [_msg("second", update_id=401)],
        ],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.4, stop.set)

    await run(
        cfg=_cfg(),
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )
    assert len(calls) == 2
    assert calls[0] is False
    assert calls[1] is True


async def test_run_logs_one_line_per_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    from src.agy_runner import AgyResult

    tg = _FakeTelegram(
        updates_to_serve=[[_msg("ping", update_id=500)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    async def fake_run_agy(**kwargs: Any) -> AgyResult:
        return AgyResult(text=f"echo:{kwargs['prompt']}", exit_code=0, stderr="")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    import src.daemon as daemon_mod

    handler = _Capture()
    handler.setLevel(logging.INFO)
    daemon_mod.LOG.addHandler(handler)
    old_level = daemon_mod.LOG.level
    daemon_mod.LOG.setLevel(logging.INFO)
    print("HANDLERS:", daemon_mod.LOG.handlers, "LEVEL:", daemon_mod.LOG.level)
    try:
        await run(
            cfg=_cfg(),
            state_path=tmp_path / "state.json",
            chats_root=tmp_path / "chats",
            tg=tg,
            agy_path="/usr/bin/true",
            info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
            stop_event=stop,
        )
    finally:
        daemon_mod.LOG.setLevel(old_level)
        daemon_mod.LOG.removeHandler(handler)

    turn_lines = [r for r in captured if r.message.startswith("turn ")]
    assert len(turn_lines) == 1
    msg = turn_lines[0].message
    assert "chat=10" in msg
    assert "exit=0" in msg
    assert "ms=" in msg
    assert "reply_len=" in msg


async def test_detect_and_send_output_files(tmp_path: Path) -> None:
    from src.daemon import _detect_and_send_output_files, clear_sent_documents

    clear_sent_documents()
    tg = _FakeTelegram(updates_to_serve=[], sent_messages=[], chat_actions=[])
    pdf_file = tmp_path / "generated.pdf"
    pdf_file.write_bytes(b"%PDF content")

    text = "I created the file `generated.pdf` for you."
    await _detect_and_send_output_files(text, str(tmp_path), tg, chat_id=10)

    assert len(tg.sent_messages) == 1
    assert "generated.pdf" in tg.sent_messages[0][1]


async def test_detect_and_send_output_files_suppresses_duplicates(tmp_path: Path) -> None:
    from src.daemon import _detect_and_send_output_files, clear_sent_documents

    clear_sent_documents()
    tg = _FakeTelegram(updates_to_serve=[], sent_messages=[], chat_actions=[])
    pdf_file = tmp_path / "report.pdf"
    pdf_file.write_bytes(b"%PDF initial")

    text = "Here is `report.pdf`"
    # First turn sends the file
    await _detect_and_send_output_files(text, str(tmp_path), tg, chat_id=10)
    assert len(tg.sent_messages) == 1

    # Second turn with unmodified file should NOT send again
    await _detect_and_send_output_files(text, str(tmp_path), tg, chat_id=10)
    assert len(tg.sent_messages) == 1  # Still 1, duplicate suppressed!

    # Modifying the file should allow resending the new version
    import time
    time.sleep(0.01)
    pdf_file.write_bytes(b"%PDF revised content")
    await _detect_and_send_output_files(text, str(tmp_path), tg, chat_id=10)
    assert len(tg.sent_messages) == 2  # Sent because file was modified!


async def test_group_inbound_messages_media_group() -> None:
    from src.daemon import _group_inbound_messages
    from src.telegram import InboundMessage

    msg1 = InboundMessage(
        update_id=1,
        chat_id=10,
        user_id=42,
        text="",
        document={"file_id": "doc1", "file_name": "target.docx"},
        documents=[{"file_id": "doc1", "file_name": "target.docx"}],
        media_group_id="group_1",
    )
    msg2 = InboundMessage(
        update_id=2,
        chat_id=10,
        user_id=42,
        text="Lengkapi file ini",
        document={"file_id": "doc2", "file_name": "source.docx"},
        documents=[{"file_id": "doc2", "file_name": "source.docx"}],
        media_group_id="group_1",
    )

    grouped = _group_inbound_messages([msg1, msg2])
    assert len(grouped) == 1
    merged = grouped[0]
    assert merged.update_id == 2
    assert merged.text == "Lengkapi file ini"
    assert len(merged.documents) == 2
    assert merged.documents[0]["file_name"] == "target.docx"
    assert merged.documents[1]["file_name"] == "source.docx"


async def test_group_inbound_messages_multi_file_burst() -> None:
    from src.daemon import _group_inbound_messages
    from src.telegram import InboundMessage

    msg1 = InboundMessage(
        update_id=1,
        chat_id=10,
        user_id=42,
        text="",
        document={"file_id": "doc1", "file_name": "file1.docx"},
        documents=[{"file_id": "doc1", "file_name": "file1.docx"}],
    )
    msg2 = InboundMessage(
        update_id=2,
        chat_id=10,
        user_id=42,
        text="Ini instruksinya",
        document={"file_id": "doc2", "file_name": "file2.docx"},
        documents=[{"file_id": "doc2", "file_name": "file2.docx"}],
    )

    grouped = _group_inbound_messages([msg1, msg2])
    assert len(grouped) == 1
    assert len(grouped[0].documents) == 2
    assert grouped[0].text == "Ini instruksinya"


# ── Concurrency: polling loop must not block on turn execution ────────────


def _media_msg(text: str, *, update_id: int, user_id: int = 42, chat_id: int = 10) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "T"},
            "text": text,
            "document": {
                "file_id": "doc1",
                "file_name": "input.pdf",
                "mime_type": "application/pdf",
                "file_size": 1024,
            },
        },
    }


def _pass_cfg() -> Config:
    base = _cfg()
    from src.config import MediaSafetyConfig, SafetyConfig

    return Config(
        telegram=base.telegram,
        agy=base.agy,
        safety=SafetyConfig(media=MediaSafetyConfig(default_routing="pass")),
    )


async def test_run_polls_telegram_while_turn_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_release = asyncio.Event()
    in_flight = 0
    max_concurrent = 0
    polls_during_turn = 0

    async def blocking_agy(*args: Any, **kwargs: Any) -> tuple[str, int]:
        nonlocal in_flight, max_concurrent
        in_flight += 1
        max_concurrent = max(max_concurrent, in_flight)
        try:
            await turn_release.wait()
        finally:
            in_flight -= 1
        return f"echo:{args[2].text}", 0

    monkeypatch.setattr("src.daemon.execute_agy", blocking_agy)

    tg = _FakeTelegram(
        updates_to_serve=[
            [_msg("first", update_id=100, chat_id=10)],
            [_msg("second", update_id=101, chat_id=11)],
        ],
        sent_messages=[],
        chat_actions=[],
    )

    orig_get_updates = tg.get_updates

    async def counting_get_updates(offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        nonlocal polls_during_turn
        if in_flight > 0:
            polls_during_turn += 1
        return await orig_get_updates(offset, timeout)

    tg.get_updates = counting_get_updates  # type: ignore[method-assign]

    stop = asyncio.Event()
    run_task = asyncio.create_task(run(
        cfg=_cfg(),
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    ))
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while in_flight == 0 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert in_flight == 1, "first turn should start"

        await asyncio.sleep(0.3)  # let the loop poll again while the turn blocks
        assert polls_during_turn >= 1, "daemon must keep polling while a turn is in flight"
    finally:
        turn_release.set()
        stop.set()
        await asyncio.wait_for(run_task, timeout=15.0)

    texts = [t for _, t in tg.sent_messages]
    assert "echo:first" in texts
    assert "echo:second" in texts
    assert max_concurrent == 1, "turns must execute serially"


async def test_run_sends_queued_feedback_while_turn_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_release = asyncio.Event()
    in_flight = 0

    async def blocking_agy(*args: Any, **kwargs: Any) -> tuple[str, int]:
        nonlocal in_flight
        in_flight += 1
        try:
            await turn_release.wait()
        finally:
            in_flight -= 1
        return f"echo:{args[2].text}", 0

    monkeypatch.setattr("src.daemon.execute_agy", blocking_agy)

    tg = _FakeTelegram(
        updates_to_serve=[
            [_msg("first", update_id=100, chat_id=10)],
            [_msg("second", update_id=101, chat_id=11)],
        ],
        sent_messages=[],
        chat_actions=[],
    )

    stop = asyncio.Event()
    run_task = asyncio.create_task(run(
        cfg=_cfg(),
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    ))
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while in_flight == 0 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert in_flight == 1, "first turn should start"

        await asyncio.sleep(0.3)
        feedback = [t for cid, t in tg.sent_messages if cid == 11 and "Queued" in t]
        assert feedback, "chat 11 must receive queue feedback while chat 10's turn runs"
    finally:
        turn_release.set()
        stop.set()
        await asyncio.wait_for(run_task, timeout=15.0)

    texts = [t for _, t in tg.sent_messages]
    assert texts.index("echo:first") < texts.index("echo:second")


# ── Media debounce: follow-up fetch must actually happen ──────────────────


async def test_fetch_updates_accepts_short_poll_timeout() -> None:
    from src.daemon import _fetch_updates

    tg = _FakeTelegram(
        updates_to_serve=[[_msg("m", update_id=9)]],
        sent_messages=[],
        chat_actions=[],
    )
    updates = await _fetch_updates(tg, 5, poll_timeout=0)
    assert updates and updates[0]["update_id"] == 9
    assert tg.get_updates_calls[-1] == (5, 0)


async def test_media_update_triggers_immediate_followup_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fast_agy(*args: Any, **kwargs: Any) -> tuple[str, int]:
        return "done", 0

    monkeypatch.setattr("src.daemon.execute_agy", fast_agy)

    tg = _FakeTelegram(
        updates_to_serve=[[_media_msg("", update_id=600)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(1.0, stop.set)

    await run(
        cfg=_cfg(),
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )
    assert any(t == 0 for _, t in tg.get_updates_calls), (
        "media burst must trigger a zero-timeout follow-up fetch"
    )


# ── Placeholder ordering: feedback before media download ──────────────────


async def test_placeholder_sent_before_media_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fast_agy(*args: Any, **kwargs: Any) -> tuple[str, int]:
        return "done:processed", 0

    monkeypatch.setattr("src.daemon.execute_agy", fast_agy)

    tg = _FakeTelegram(
        updates_to_serve=[[_media_msg("process this", update_id=700)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(1.0, stop.set)

    await run(
        cfg=_pass_cfg(),
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )
    assert "get_file:doc1" in tg.events, "media should be downloaded"
    assert tg.events.index("send_message") < tg.events.index("get_file:doc1"), (
        "placeholder must be sent before the media download starts"
    )


async def test_blocked_media_deletes_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fast_agy(*args: Any, **kwargs: Any) -> tuple[str, int]:
        return "done", 0

    monkeypatch.setattr("src.daemon.execute_agy", fast_agy)

    tg = _FakeTelegram(
        updates_to_serve=[[_media_msg("", update_id=800)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(1.0, stop.set)

    await run(
        cfg=_cfg(),  # default_routing="block" → all content blocked
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )
    assert any("Blocked" in t for _, t in tg.sent_messages)
    assert tg.deleted_messages, "placeholder must be cleaned up when all content is blocked"


# ── Review fixes: shared singleton + worker error path ────────────────────


def test_queue_singleton_is_shared_between_queue_and_daemon() -> None:
    import src.daemon as daemon_mod
    import src.queue as queue_mod

    assert queue_mod.QUEUE is daemon_mod._QUEUE, (
        "/queue must observe the same TurnQueue the worker consumes, "
        "even when the daemon runs as __main__"
    )


async def test_turn_worker_releases_queue_when_chat_state_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.daemon as daemon_mod
    from src.queue import TurnQueue
    from src.state import State
    from src.telegram import InboundMessage

    q = TurnQueue()
    monkeypatch.setattr(daemon_mod, "_QUEUE", q)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("chat dir unavailable")

    monkeypatch.setattr(daemon_mod, "_ensure_chat_state", _boom)

    q.submit(InboundMessage(update_id=1, chat_id=10, user_id=42, text="hi"))
    worker = asyncio.create_task(daemon_mod._turn_worker(
        tg=None, state=State(), state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats", cfg=_cfg(), agy_path="/usr/bin/true",
    ))
    try:
        await asyncio.wait_for(q.join(), timeout=3)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert q.active is False


# ── /usage: token accounting per chat ─────────────────────────────────────


def test_record_usage_resets_daily_window() -> None:
    from src.daemon import _record_usage
    from src.state import ChatState

    cs = ChatState(chat_dir="/tmp/x", usage_today_date="2000-01-01",
                   usage_today_tokens=999, usage_today_turns=9,
                   usage_total_tokens=999, usage_total_turns=9)
    _record_usage(cs, {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
    assert cs.usage_today_tokens == 12  # stale day reset
    assert cs.usage_today_turns == 1
    assert cs.usage_total_tokens == 999 + 12
    assert cs.usage_total_turns == 10
    assert cs.usage_last_in == 10
    assert cs.usage_last_out == 2
    assert cs.usage_last_total == 12


async def test_run_records_usage_in_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.turn import TurnOutput

    async def fake_agy(*args: Any, **kwargs: Any) -> TurnOutput:
        return TurnOutput("echo:hi", 0, usage={
            "input_tokens": 100, "output_tokens": 5, "total_tokens": 105,
        })

    monkeypatch.setattr("src.daemon.execute_agy", fake_agy)

    state_path = tmp_path / "state.json"
    chats_root = tmp_path / "chats"
    tg = _FakeTelegram(
        updates_to_serve=[[_msg("hi", update_id=100)]],
        sent_messages=[], chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.4, stop.set)

    await run(
        cfg=_cfg(), state_path=state_path, chats_root=chats_root, tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )
    persisted = load_state(state_path, chats_root)
    cs = persisted.chats[10]
    assert cs.usage_total_tokens == 105
    assert cs.usage_total_turns == 1
    assert cs.usage_last_in == 100
    assert cs.usage_today_tokens == 105


# ── Review round 2: bounded shutdown + audible accounting failures ────────


async def test_run_shutdown_bounds_stuck_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown must not hang forever if the worker ignores cancellation."""
    import logging

    import src.daemon as daemon_mod
    from src.state import State

    loop = asyncio.get_running_loop()
    ignore_until = loop.time() + 1.0  # stuck in cleanup for ~1s, then gives up

    async def stuck_worker(*args: Any, **kwargs: Any) -> None:
        while loop.time() < ignore_until:
            try:
                await asyncio.sleep(max(0.0, ignore_until - loop.time()))
            except asyncio.CancelledError:
                continue  # ignores cancellation during the stuck window

    monkeypatch.setattr(daemon_mod, "_turn_worker", stuck_worker)
    monkeypatch.setattr(daemon_mod, "_WORKER_SHUTDOWN_TIMEOUT_S", 0.3)

    tg = _FakeTelegram(updates_to_serve=[], sent_messages=[], chat_actions=[])
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    daemon_mod.LOG.addHandler(handler)
    daemon_mod.LOG.setLevel(logging.INFO)
    try:
        await asyncio.wait_for(run(
            cfg=_cfg(), state_path=tmp_path / "state.json",
            chats_root=tmp_path / "chats", tg=tg, agy_path="/usr/bin/true",
            info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
            stop_event=stop,
        ), timeout=5)
    finally:
        daemon_mod.LOG.removeHandler(handler)

    assert any("did not exit" in r.message for r in captured), (
        "shutdown must warn when the worker exceeds the cancellation wait bound"
    )


async def test_usage_accounting_failure_logs_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    import src.daemon as daemon_mod
    from src.turn import TurnOutput

    async def fake_agy(*args: Any, **kwargs: Any) -> TurnOutput:
        return TurnOutput("echo:hi", 0, usage={
            "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
        })

    monkeypatch.setattr("src.daemon.execute_agy", fake_agy)

    def boom(cs: Any, usage: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr("src.daemon._record_usage", boom)

    tg = _FakeTelegram(
        updates_to_serve=[[_msg("hi", update_id=110)]],
        sent_messages=[], chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.4, stop.set)

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    daemon_mod.LOG.addHandler(handler)
    daemon_mod.LOG.setLevel(logging.INFO)
    try:
        await run(
            cfg=_cfg(), state_path=tmp_path / "state.json",
            chats_root=tmp_path / "chats", tg=tg, agy_path="/usr/bin/true",
            info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
            stop_event=stop,
        )
    finally:
        daemon_mod.LOG.removeHandler(handler)

    assert any("usage accounting failed" in r.message and "disk full" in r.message
               for r in captured), "accounting failures must not be silent"
