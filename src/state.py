"""Atomic JSON-backed bridge state at ~/.antigravity/bridge/state.json.

Per-chat state tracks the chat working directory, whether a session exists,
model/mode overrides, and turn count. agy resumes sessions by cwd/project,
so we do not store opaque session UUIDs.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Defense against state.json tampering: chat_dir paths must look like a tail
# segment we generated ourselves (digits/letters/underscores/dashes, no slashes,
# no '..'). Real chat_ids are integers, so the dir name is the stringified id.
_CHAT_DIR_RE = re.compile(r"^-?[0-9]+$")

# YYYY-MM-DD key for the per-chat daily usage window.
_USAGE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Per-chat overrides have to be argv-safe. The regex rejects any character
# that's not strictly needed for valid model identifiers (e.g. "gemini-3.5-flash").
# Crucially it forbids a leading `-` so a tampered state file can't inject flags.
_MODEL_RE = re.compile(r"^[a-zA-Z0-9._][a-zA-Z0-9._\-]*$")

_ALLOWED_MODES = frozenset({"", "code", "plan"})
_ALLOWED_EFFORTS = frozenset({"", "low", "medium", "high", "max"})


@dataclass
class ChatState:
    chat_dir: str  # absolute path; verified-on-load to be under chats_root
    has_session: bool = False  # True after first successful agy turn
    model: str = ""  # "" → use cfg.agy.model
    mode: str = ""  # "" → use cfg.agy.mode; values: "code" | "plan"
    effort: str = ""  # "" → default; values: "low" | "medium" | "high" | "max"
    photo_enabled: bool = True  # toggle for photo processing
    style_hint: bool = True  # toggle for Telegram specific prompt hints
    turn_count: int = 0  # successful turns served on this chat
    active: bool = True  # False → bot ignores non-command messages
    # Token usage accounting (from agy stream-json result events).
    usage_last_in: int = 0  # input tokens of the last completed turn
    usage_last_out: int = 0  # output tokens of the last completed turn
    usage_last_total: int = 0  # total tokens of the last completed turn
    usage_today_tokens: int = 0  # tokens accumulated today (local time)
    usage_today_turns: int = 0  # turns counted today
    usage_today_date: str = ""  # YYYY-MM-DD window key for the daily counters
    usage_total_tokens: int = 0  # lifetime token total for this chat
    usage_total_turns: int = 0  # lifetime turn count for this chat


@dataclass
class State:
    last_update_id: int = 0
    chats: dict[int, ChatState] = field(default_factory=dict)


def is_valid_model(name: str) -> bool:
    """True when a model id is argv-safe (no leading dash, allowed charset)."""
    return bool(name) and bool(_MODEL_RE.match(name))


def _safe_model(raw: object) -> str:
    """Return the raw model id if argv-safe, else an empty string."""
    if not isinstance(raw, str) or not raw:
        return ""
    return raw if _MODEL_RE.match(raw) else ""


def _safe_mode(raw: object) -> str:
    """Return the mode if allowed, else an empty string."""
    if isinstance(raw, str) and raw in _ALLOWED_MODES:
        return raw
    return ""


def _safe_turn_count(raw: object) -> int:
    """Return the int if a non-negative integer, else 0."""
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def _safe_bool(raw: object) -> bool:
    """Return the bool as-is; anything else defaults to True."""
    if isinstance(raw, bool):
        return raw
    return True


def _safe_effort(raw: object) -> str:
    """Return the effort if allowed, else an empty string."""
    if isinstance(raw, str) and raw in _ALLOWED_EFFORTS:
        return raw
    return ""


def _safe_usage_date(raw: object) -> str:
    """Return a well-formed YYYY-MM-DD string, else an empty string."""
    if isinstance(raw, str) and _USAGE_DATE_RE.match(raw):
        return raw
    return ""


def _safe_chat_state(
    chats_root: Path,
    raw: dict,
    extra_allowed_dirs: list[Path] | None = None,
) -> ChatState | None:
    """Validate and build a ChatState from raw JSON, or None if unsafe."""
    chat_dir = raw.get("chat_dir")
    if not isinstance(chat_dir, str):
        return None
    p = Path(chat_dir).expanduser()
    if not p.is_absolute():
        return None

    is_valid = False
    # Standard chat dir under chats_root
    try:
        p.resolve().relative_to(chats_root.resolve())
        if _CHAT_DIR_RE.match(p.name):
            is_valid = True
    except (ValueError, OSError):
        pass

    # Extra allowed directories (such as default_workdir or projects)
    if not is_valid and extra_allowed_dirs:
        for allowed in extra_allowed_dirs:
            try:
                p_res = p.resolve()
                a_res = allowed.resolve()
                if (p_res == a_res or p_res.is_relative_to(a_res)) and p_res.is_dir():
                    is_valid = True
                    break
            except (ValueError, OSError):
                continue

    if not is_valid:
        return None

    return ChatState(
        chat_dir=str(p.resolve()),
        has_session=bool(raw.get("has_session", False)),
        model=_safe_model(raw.get("model", "")),
        mode=_safe_mode(raw.get("mode", "")),
        effort=_safe_effort(raw.get("effort", "")),
        photo_enabled=_safe_bool(raw.get("photo_enabled", True)),
        style_hint=_safe_bool(raw.get("style_hint", True)),
        turn_count=_safe_turn_count(raw.get("turn_count", 0)),
        active=_safe_bool(raw.get("active", True)),
        usage_last_in=_safe_turn_count(raw.get("usage_last_in", 0)),
        usage_last_out=_safe_turn_count(raw.get("usage_last_out", 0)),
        usage_last_total=_safe_turn_count(raw.get("usage_last_total", 0)),
        usage_today_tokens=_safe_turn_count(raw.get("usage_today_tokens", 0)),
        usage_today_turns=_safe_turn_count(raw.get("usage_today_turns", 0)),
        usage_today_date=_safe_usage_date(raw.get("usage_today_date", "")),
        usage_total_tokens=_safe_turn_count(raw.get("usage_total_tokens", 0)),
        usage_total_turns=_safe_turn_count(raw.get("usage_total_turns", 0)),
    )


def load_state(
    path: Path,
    chats_root: Path,
    extra_allowed_dirs: list[Path] | None = None,
) -> State:
    """Load chat state from disk, dropping malformed entries."""
    if not path.exists():
        return State()
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return State()
    chats_raw = raw.get("chats") or {}
    chats: dict[int, ChatState] = {}
    for k, v in chats_raw.items():
        try:
            chat_id = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        cs = _safe_chat_state(chats_root, v, extra_allowed_dirs=extra_allowed_dirs)
        if cs is not None:
            chats[chat_id] = cs
    return State(
        last_update_id=int(raw.get("last_update_id", 0)),
        chats=chats,
    )


def save_state(path: Path, state: State) -> None:
    """Atomically persist chat state to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "last_update_id": state.last_update_id,
        "chats": {str(k): asdict(v) for k, v in state.chats.items()},
    }
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)
