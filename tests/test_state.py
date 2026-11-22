"""Tests for src.state — atomic JSON state file."""
from __future__ import annotations

from pathlib import Path

from src.state import ChatState, State, load_state, save_state


def test_load_missing_file_returns_zero_state(tmp_path: Path) -> None:
    s = load_state(tmp_path / "state.json", tmp_path / "chats")
    assert s.last_update_id == 0
    assert s.chats == {}


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    chat_dir = str(tmp_path / "chats" / "123")
    cs = ChatState(chat_dir=chat_dir, has_session=True, model="gemini-2.5-pro", mode="plan", turn_count=5)
    original = State(last_update_id=42, chats={123: cs})
    save_state(p, original)

    loaded = load_state(p, tmp_path / "chats")
    assert loaded.last_update_id == 42
    assert loaded.chats[123].chat_dir == chat_dir
    assert loaded.chats[123].has_session is True
    assert loaded.chats[123].model == "gemini-2.5-pro"
    assert loaded.chats[123].mode == "plan"
    assert loaded.chats[123].turn_count == 5


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "dirs" / "state.json"
    save_state(p, State(last_update_id=1, chats={}))
    assert p.exists()


def test_save_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    save_state(p, State(last_update_id=1, chats={}))
    siblings = list(p.parent.iterdir())
    assert siblings == [p], f"unexpected files: {siblings}"


def test_corrupt_state_file_returns_zero_state(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("{ not json")
    s = load_state(p, tmp_path / "chats")
    assert s.last_update_id == 0
    assert s.chats == {}


def test_chat_keys_round_trip_as_int(tmp_path: Path) -> None:
    """JSON object keys are strings; loader must coerce back to int."""
    p = tmp_path / "state.json"
    chat_dir = str(tmp_path / "chats" / "123")
    save_state(p, State(last_update_id=5, chats={123: ChatState(chat_dir=chat_dir)}))
    loaded = load_state(p, tmp_path / "chats")
    assert all(isinstance(k, int) for k in loaded.chats)


def test_load_state_drops_malformed_chat_dirs(tmp_path: Path) -> None:
    """A tampered state.json with chat_dir outside chats_root should be dropped."""
    p = tmp_path / "state.json"
    p.write_text(
        '{"last_update_id": 5, "chats": {'
        '"123": {"chat_dir": "/tmp/evil", "has_session": false},'
        '"456": {"chat_dir": "not-absolute", "has_session": false},'
        '"789": {"chat_dir": "' + str(tmp_path / "chats" / "789") + '", "has_session": true}'
        '}}'
    )
    state = load_state(p, tmp_path / "chats")
    assert state.last_update_id == 5
    assert state.chats.keys() == {789}


def test_load_state_drops_invalid_model_or_mode(tmp_path: Path) -> None:
    chat_dir = str(tmp_path / "chats" / "123")
    (tmp_path / "chats" / "123").mkdir(parents=True)
    p = tmp_path / "state.json"
    p.write_text(
        '{"last_update_id": 1, "chats": {'
        '"123": {"chat_dir": "' + chat_dir + '", "model": "--evil", "mode": "bad"}'
        '}}'
    )
    state = load_state(p, tmp_path / "chats")
    assert state.chats[123].model == ""
    assert state.chats[123].mode == ""


def test_load_state_keeps_photo_enabled(tmp_path: Path) -> None:
    chat_dir = str(tmp_path / "chats" / "123")
    (tmp_path / "chats" / "123").mkdir(parents=True)
    p = tmp_path / "state.json"
    p.write_text(
        '{"last_update_id": 1, "chats": {'
        '"123": {"chat_dir": "' + chat_dir + '", "photo_enabled": false}'
        '}}'
    )
    state = load_state(p, tmp_path / "chats")
    assert state.chats[123].photo_enabled is False


def test_save_and_load_xhigh_effort(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    chat_dir = str(tmp_path / "chats" / "123")
    cs = ChatState(chat_dir=chat_dir, model="gemini-4-neon", effort="xhigh")
    save_state(p, State(last_update_id=1, chats={123: cs}))
    loaded = load_state(p, tmp_path / "chats")
    assert loaded.chats[123].effort == "xhigh"
