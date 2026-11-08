"""Tests for src.turn — agy turn execution."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from src.agy_runner import AgyResult
from src.config import AgyConfig, Config, TelegramConfig
from src.state import ChatState
from src.turn import execute_agy


@dataclass(frozen=True)
class _FakeMsg:
    chat_id: int = 42
    text: str = "hello"


class _FakeTG:
    def __init__(self) -> None:
        self.actions: list[tuple[int, str]] = []

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append((chat_id, action))


async def test_execute_agy_returns_reply_and_records_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_run_agy(prompt: str, **kwargs: Any) -> AgyResult:
        calls.append({"prompt": prompt, **kwargs})
        await asyncio.sleep(0.01)
        return AgyResult(text="reply text", exit_code=0, stderr="")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    tg = _FakeTG()
    cs = ChatState(chat_dir="/tmp/chat")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, _FakeMsg(text="hello"), cs, cfg, "/usr/bin/agy")

    assert code == 0
    assert text == "reply text"
    assert tg.actions
    assert calls[0]["prompt"].startswith("<TELEGRAM_OUTPUT_STYLE>")
    assert calls[0]["prompt"].endswith("\n\nhello")
    assert calls[0]["chat_dir"] == "/tmp/chat"


async def test_execute_agy_skips_style_hint_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_run_agy(prompt: str, **_: Any) -> AgyResult:
        calls.append(prompt)
        return AgyResult(text="ok", exit_code=0, stderr="")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)
    cs = ChatState(chat_dir="/tmp/chat", style_hint=False)
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    await execute_agy(_FakeTG(), 42, _FakeMsg(text="hello"), cs, cfg, "/usr/bin/agy")
    assert calls == ["hello"]


async def test_execute_agy_returns_timeout_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_agy(**_: Any) -> AgyResult:
        return AgyResult(text="", exit_code=124, stderr="timeout")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    tg = _FakeTG()
    cs = ChatState(chat_dir="/tmp/chat")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, _FakeMsg(), cs, cfg, "/usr/bin/agy")
    assert code == 124
    assert text == ""


async def test_execute_agy_propagates_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from src.agy_runner import AgyResult
    from src.config import AgyConfig, Config, TelegramConfig
    from src.state import ChatState
    from src.turn import execute_agy

    async def fake_run_agy(**kwargs):
        return AgyResult(text="ok", exit_code=0, stderr="",
                         usage={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6})

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    class _TG:
        async def send_chat_action(self, *a, **k):
            pass

    cs = ChatState(chat_dir=str(tmp_path))
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]),
                 agy=AgyConfig(default_workdir="/tmp", model="", mode="code"))
    out = await execute_agy(_TG(), 10, SimpleNamespace(text="hi"), cs, cfg, "agy")
    assert out.usage == {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}
