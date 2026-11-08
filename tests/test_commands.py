"""Unit tests for src.commands — bridge command handlers."""
from __future__ import annotations

from pathlib import Path

from src.commands import handle_callback, handle_text_command
from src.config import AgyConfig, Config, TelegramConfig
from src.state import ChatState
from src.telegram import CallbackQuery


def _cfg(model: str = "", mode: str = "code") -> Config:
    return Config(
        telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]),
        agy=AgyConfig(default_workdir="/tmp", model=model, mode=mode),
    )


def _state(chat_dir: str = "/tmp/chat") -> ChatState:
    return ChatState(chat_dir=chat_dir)


class _FakeMsg:
    def __init__(self, text: str, chat_id: int = 42, user_id: int = 42) -> None:
        self.text = text
        self.chat_id = chat_id
        self.user_id = user_id


async def test_start_command() -> None:
    reply = await handle_text_command(_FakeMsg("/start"), _state(), _cfg())
    assert reply is not None
    assert "Welcome" in reply.text


async def test_help_command() -> None:
    reply = await handle_text_command(_FakeMsg("/help"), _state(), _cfg())
    assert reply is not None
    assert "Commands" in reply.text


async def test_info_shows_default_model() -> None:
    reply = await handle_text_command(_FakeMsg("/info"), _state(), _cfg(model="gemini-2.5-pro"))
    assert reply is not None
    assert "gemini-2.5-pro" in reply.text


async def test_info_no_session() -> None:
    reply = await handle_text_command(_FakeMsg("/info"), _state(), _cfg())
    assert reply is not None
    assert "fresh" in reply.text.lower()


async def test_thinking_not_available() -> None:
    reply = await handle_text_command(_FakeMsg("/thinking on"), _state(), _cfg())
    assert reply is not None
    assert "not available" in reply.text.lower()


async def test_model_picker() -> None:
    reply = await handle_text_command(_FakeMsg("/model"), _state(), _cfg(model="gemini-2.5-pro"))
    assert reply is not None
    assert reply.keyboard is not None


async def test_model_set() -> None:
    cs = _state()
    reply = await handle_text_command(_FakeMsg("/model gemini-2.5-pro"), cs, _cfg())
    assert reply is not None
    assert cs.model == "gemini-2.5-pro"


async def test_reset_clears_session() -> None:
    cs = _state()
    cs.has_session = True
    reply = await handle_text_command(_FakeMsg("/reset"), cs, _cfg())
    assert reply is not None
    assert cs.has_session is False


async def test_unknown_returns_none() -> None:
    reply = await handle_text_command(_FakeMsg("hello world"), _state(), _cfg())
    assert reply is None


async def test_callback_nav_settings() -> None:
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="nav:settings")
    reply = handle_callback(cq, _state(), _cfg())
    assert reply.keyboard is not None


async def test_callback_model_choice() -> None:
    cs = _state()
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="m:gemini-2.5-pro")
    reply = handle_callback(cq, cs, _cfg())
    assert cs.model == "gemini-2.5-pro"
    assert "gemini-2.5-pro" in reply.toast


async def test_callback_reset() -> None:
    cs = _state()
    cs.has_session = True
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="R")
    reply = handle_callback(cq, cs, _cfg())
    assert cs.has_session is False
    assert "reset" in reply.toast.lower()


async def test_callback_mode_choice() -> None:
    cs = _state()
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="M:plan")
    reply = handle_callback(cq, cs, _cfg())
    assert cs.mode == "plan"
    assert "plan" in reply.toast.lower()


async def test_pwd_command() -> None:
    cs = _state()
    reply = await handle_text_command(_FakeMsg("/pwd"), cs, _cfg())
    assert reply is not None
    assert "/tmp/chat" in reply.text


async def test_cd_valid_dir(tmp_path: Path) -> None:
    cs = _state()
    cs.has_session = True
    target = tmp_path / "my_project"
    target.mkdir()
    reply = await handle_text_command(_FakeMsg(f"/cd {target}"), cs, _cfg())
    assert reply is not None
    assert "Switched working directory" in reply.text
    assert cs.chat_dir == str(target.resolve())
    assert cs.has_session is False


async def test_cd_invalid_dir() -> None:
    cs = _state()
    reply = await handle_text_command(_FakeMsg("/cd /nonexistent/dir/xyz"), cs, _cfg())
    assert reply is not None
    assert "not found" in reply.text.lower()


async def test_cd_default_fallback(tmp_path: Path) -> None:
    cs = _state()
    default_dir = tmp_path / "default_dir"
    default_dir.mkdir()
    cfg = Config(
        telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]),
        agy=AgyConfig(default_workdir=str(default_dir)),
    )
    reply = await handle_text_command(_FakeMsg("/cd"), cs, cfg)
    assert reply is not None
    assert "Switched working directory" in reply.text
    assert cs.chat_dir == str(default_dir.resolve())


async def test_effort_command() -> None:
    cs = _state()
    reply = await handle_text_command(_FakeMsg("/effort"), cs, _cfg())
    assert reply is not None
    assert "effort" in reply.text.lower()

    reply = await handle_text_command(_FakeMsg("/effort high"), cs, _cfg())
    assert reply is not None
    assert "high" in reply.text
    assert cs.effort == "high"

    reply = await handle_text_command(_FakeMsg("/effort invalid"), cs, _cfg())
    assert reply is not None
    assert "unsupported" in reply.text.lower()


async def test_callback_effort_choice() -> None:
    cs = _state()
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="e:medium")
    reply = handle_callback(cq, cs, _cfg())
    assert cs.effort == "medium"
    assert "medium" in reply.toast.lower()

    cq_reset = CallbackQuery(update_id=2, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="e:_DEFAULT")
    reply_reset = handle_callback(cq_reset, cs, _cfg())
    assert cs.effort == ""
    assert "default" in reply_reset.toast.lower()


async def test_stop_command() -> None:
    cs = _state()
    reply = await handle_text_command(_FakeMsg("/stop"), cs, _cfg())
    assert reply is not None
    assert "no active agy process" in reply.text.lower()


async def test_callback_model_triggers_effort_picker() -> None:
    cs = _state()
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="m:gemini-3.8-flash")
    reply = handle_callback(cq, cs, _cfg())
    assert cs.model == "gemini-3.8-flash"
    assert reply.keyboard is not None
    # Verify effort buttons exist in the keyboard
    callback_datas = [btn["callback_data"] for row in reply.keyboard for btn in row]
    assert "e:low" in callback_datas
    assert "e:medium" in callback_datas
    assert "e:high" in callback_datas
    assert "nav:model" in callback_datas


async def test_callback_model_without_effort_finishes() -> None:
    cs = _state()
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="m:gpt-oss-120b-medium")
    reply = handle_callback(cq, cs, _cfg())
    assert cs.model == "gpt-oss-120b-medium"
    assert "GPT-OSS 120B" in reply.toast
    assert reply.keyboard is not None
    callback_datas = [btn["callback_data"] for row in reply.keyboard for btn in row]
    # Direct return to settings
    assert "nav:model" in callback_datas
    assert "R" in callback_datas


async def test_callback_claude_46_direct_selection() -> None:
    cs = _state()
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="m:claude-sonnet-4-6")
    reply = handle_callback(cq, cs, _cfg())
    assert cs.model == "claude-sonnet-4-6"
    assert "Claude Sonnet 4.6" in reply.toast


async def test_model_picker_lists_claude_46() -> None:
    reply = await handle_text_command(_FakeMsg("/model"), _state(), _cfg())
    assert reply is not None and reply.keyboard is not None
    labels = " ".join(btn["text"] for row in reply.keyboard for btn in row)
    assert "Claude Sonnet 4.6" in labels
    assert "Claude Opus 4.6" in labels
    assert "Claude Sonnet 5.5" not in labels


async def test_bot_commands_list_validity() -> None:
    import re
    from src.commands import BOT_COMMANDS

    assert len(BOT_COMMANDS) >= 10
    cmd_pattern = re.compile(r"^[a-z0-9_]{1,32}$")
    for item in BOT_COMMANDS:
        cmd = item["command"]
        desc = item["description"]
        assert cmd_pattern.match(cmd), f"Invalid command name: {cmd}"
        assert 1 <= len(desc) <= 256, f"Description length invalid for {cmd}"


async def test_commands_alias_to_help() -> None:
    reply = await handle_text_command(_FakeMsg("/commands"), _state(), _cfg())
    assert reply is not None
    assert "Commands" in reply.text


async def test_getfile_command(tmp_path: Path) -> None:
    test_file = tmp_path / "report.pdf"
    test_file.write_bytes(b"%PDF content")
    cs = _state(chat_dir=str(tmp_path))
    reply = await handle_text_command(_FakeMsg("/getfile report.pdf"), cs, _cfg())
    assert reply is not None
    assert "Sending" in reply.text
    assert reply.document_path == test_file


async def test_getfile_no_args() -> None:
    reply = await handle_text_command(_FakeMsg("/getfile"), _state(), _cfg())
    assert reply is not None
    assert "Usage" in reply.text


async def test_getfile_not_found(tmp_path: Path) -> None:
    cs = _state(chat_dir=str(tmp_path))
    reply = await handle_text_command(_FakeMsg("/getfile missing.pdf"), cs, _cfg())
    assert reply is not None
    assert "not found" in reply.text


async def test_mcp_command_lists_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.commands._list_mcp_servers", lambda: [{"name": "fake-server", "type": "stdio", "status": "enabled", "cmd": ""}])
    reply = await handle_text_command(_FakeMsg("/mcp"), _state(), _cfg())
    assert reply is not None
    assert "MCP" in reply.text


async def test_mcp_command_inspect_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.commands._list_mcp_servers", lambda: [{"name": "fake-server", "type": "stdio", "status": "enabled", "cmd": ""}])
    reply = await handle_text_command(_FakeMsg("/mcp nonexistent_server_12345"), _state(), _cfg())
    assert reply is not None
    assert "not found" in reply.text


async def test_skills_command_lists_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.commands._list_skills", lambda *args, **kwargs: [{"name": "mock-skill", "folder": "mock-skill", "description": "mock", "path": "/tmp"}])
    reply = await handle_text_command(_FakeMsg("/skills"), _state(), _cfg())
    assert reply is not None
    assert "Skills" in reply.text


async def test_skills_command_inspect_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.commands._list_skills", lambda *args, **kwargs: [{"name": "mock-skill", "folder": "mock-skill", "description": "mock", "path": "/tmp"}])
    reply = await handle_text_command(_FakeMsg("/skills nonexistent_skill_12345"), _state(), _cfg())
    assert reply is not None
    assert "not found" in reply.text


async def test_callback_nav_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.commands._list_mcp_servers", lambda: [{"name": "fake-server", "type": "stdio", "status": "enabled", "cmd": ""}])
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="nav:mcp")
    reply = handle_callback(cq, _state(), _cfg())
    assert reply is not None
    assert "MCP" in reply.text


async def test_callback_nav_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.commands._list_skills", lambda *args, **kwargs: [{"name": "mock-skill", "folder": "mock-skill", "description": "mock", "path": "/tmp"}])
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="nav:skills")
    reply = handle_callback(cq, _state(), _cfg())
    assert reply is not None
    assert "Skills" in reply.text



async def test_style_command_toggles_hint() -> None:
    cs = _state()
    assert cs.style_hint is True
    reply = await handle_text_command(_FakeMsg("/style off"), cs, _cfg())
    assert reply is not None and cs.style_hint is False
    reply = await handle_text_command(_FakeMsg("/style on"), cs, _cfg())
    assert reply is not None and cs.style_hint is True
    reply = await handle_text_command(_FakeMsg("/style"), cs, _cfg())
    assert reply is not None and "ON" in reply.text


def test_style_hint_persists_in_state(tmp_path: Path) -> None:
    from src.state import State, load_state, save_state
    root = tmp_path / "chats"
    (root / "42").mkdir(parents=True)
    st = State(chats={42: ChatState(chat_dir=str(root / "42"), style_hint=False)})
    save_state(tmp_path / "s.json", st)
    loaded = load_state(tmp_path / "s.json", root)
    assert loaded.chats[42].style_hint is False


async def test_queue_command_reports_real_status() -> None:
    from src.daemon import _QUEUE
    from src.telegram import InboundMessage

    _QUEUE.reset()
    try:
        reply = await handle_text_command(_FakeMsg("/queue"), _state(), _cfg())
        assert reply is not None
        assert "Active" in reply.text

        _QUEUE.submit(InboundMessage(update_id=1, chat_id=99, user_id=1, text="pending"))
        _QUEUE.submit(InboundMessage(update_id=2, chat_id=98, user_id=1, text="pending2"))
        reply2 = await handle_text_command(_FakeMsg("/queue"), _state(), _cfg())
        assert reply2 is not None
        assert "depth: 2" in reply2.text.lower()
    finally:
        _QUEUE.reset()
async def test_model_selection_clears_incompatible_effort_callback() -> None:
    """Picking a model that ignores effort must reset a stale effort value."""
    cs = _state()
    cs.model = "gemini-3.8-flash"
    cs.effort = "high"
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42,
                       user_id=42, message_id=1, data="m:claude-sonnet-4-6")
    handle_callback(cq, cs, _cfg())
    assert cs.model == "claude-sonnet-4-6"
    assert cs.effort == ""


async def test_model_selection_keeps_effort_for_composable_base() -> None:
    cs = _state()
    cs.effort = "high"
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42,
                       user_id=42, message_id=1, data="m:gemini-3.8-flash")
    handle_callback(cq, cs, _cfg())
    assert cs.model == "gemini-3.8-flash"
    assert cs.effort == "high"  # the launch composes gemini-3.8-flash-high


async def test_text_model_selection_clears_incompatible_effort() -> None:
    cs = _state()
    cs.effort = "high"
    reply = await handle_text_command(_FakeMsg("/model claude-sonnet-4-6"), cs, _cfg())
    assert reply is not None
    assert cs.model == "claude-sonnet-4-6"
    assert cs.effort == ""


async def test_usage_command_renders_counters() -> None:
    import time as _time

    cs = _state()
    cs.usage_last_in, cs.usage_last_out, cs.usage_last_total = 100, 5, 105
    cs.usage_today_tokens = 105
    cs.usage_today_turns = 1
    cs.usage_today_date = _time.strftime("%Y-%m-%d")
    cs.usage_total_tokens, cs.usage_total_turns = 1234, 12
    reply = await handle_text_command(_FakeMsg("/usage"), cs, _cfg())
    assert reply is not None
    assert "105" in reply.text
    assert "1,234" in reply.text  # thousands separator formatting
    assert "Hari ini" in reply.text


async def test_usage_command_empty_state() -> None:
    reply = await handle_text_command(_FakeMsg("/usage"), _state(), _cfg())
    assert reply is not None
    assert "Belum ada" in reply.text
