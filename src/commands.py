"""Bridge command handlers — text slash commands and inline-keyboard callbacks."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.media import clean_inbox, list_inbox
from src.state import is_valid_model
from src.telegram import InlineKeyboard

if TYPE_CHECKING:
    from src.config import Config
    from src.daemon import _TelegramLike
    from src.state import ChatState
    from src.telegram import CallbackQuery, InboundMessage

DEFAULT_MODEL = "gemini-3.7-flash-high"

MODEL_CHOICES: tuple[str, ...] = (
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.1-pro-high",
    "gemini-2.5-pro",
    "claude-sonnet-4-6",
)
MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("code", "Code (auto)"),
    ("plan", "Plan (read-only sandbox)"),
)
_DEFAULT_TOKEN = "_DEFAULT"

WELCOME_TEXT = (
    "👋 Welcome! I'm your Antigravity bridge bot.\n\n"
    "Send me any message and I'll forward it to the agy CLI. "
    "Each chat has its own session that persists across messages.\n\n"
    "Commands: /start · /help · /status · /settings · /model · /mode · /reset"
)

HELP_TEXT = (
    "📖 Antigravity Bridge Help\n\n"
    "Send any text to chat with agy.\n\n"
    "Commands:\n"
    "/status   — read-only system + chat summary\n"
    "/settings — control panel with buttons\n"
    "/model    — pick a model (per-chat override)\n"
    "/mode     — pick a mode (Code or Plan)\n"
    "/reset    — fresh agy session for this chat\n"
    "/info     — same as /status\n"
    "/image on|off — toggle photo processing\n"
    "/files    — list recent uploads\n"
    "/start, /help — these messages\n\n"
    "Per-chat overrides take precedence over config.json."
)


@dataclass(frozen=True)
class BridgeReply:
    """Daemon's structured reply: text, optional inline keyboard, optional toast."""

    text: str
    keyboard: InlineKeyboard | None = None
    toast: str = ""


def _effective_model(cs: "ChatState", cfg: "Config") -> tuple[str, str]:
    if cs.model:
        return cs.model, "chat"
    if cfg.agy.model:
        return cfg.agy.model, "config"
    return DEFAULT_MODEL, "default"


def _effective_mode(cs: "ChatState", cfg: "Config") -> tuple[str, str]:
    if cs.mode:
        return cs.mode, "chat"
    return cfg.agy.mode, "config"


def render_status(cs: "ChatState", cfg: "Config") -> str:
    model, model_src = _effective_model(cs, cfg)
    mode, mode_src = _effective_mode(cs, cfg)
    session = "active (resumes next turn)" if cs.has_session else "fresh (next turn starts new)"
    home = os.path.expanduser("~")
    workdir = cs.chat_dir.replace(home, "~", 1)
    return (
        "🟢 Antigravity Bridge — Status\n"
        f"Model:      {model}  [{model_src}]\n"
        f"Mode:       {mode}  [{mode_src}]\n"
        "\n"
        "This chat:\n"
        f"  Session:  {session}\n"
        f"  Turns:    {cs.turn_count}\n"
        f"  Workdir:  {workdir}"
    )


def _settings_keyboard() -> InlineKeyboard:
    return [
        [
            {"text": "🤖 Change model", "callback_data": "nav:model"},
            {"text": "🛡 Change mode", "callback_data": "nav:mode"},
        ],
        [{"text": "🧹 Reset session", "callback_data": "R"}],
        [{"text": "🔄 Refresh", "callback_data": "nav:settings"}],
    ]


def _model_keyboard(current_per_chat: str) -> InlineKeyboard:
    rows: InlineKeyboard = []
    for m in MODEL_CHOICES:
        marker = "● " if m == current_per_chat else "○ "
        rows.append([{"text": marker + m, "callback_data": f"m:{m}"}])
    default_marker = "● " if not current_per_chat else "○ "
    rows.append([
        {"text": default_marker + "Use config default", "callback_data": f"m:{_DEFAULT_TOKEN}"}
    ])
    rows.append([{"text": "← Back to settings", "callback_data": "nav:settings"}])
    return rows


def _mode_keyboard(current_per_chat: str) -> InlineKeyboard:
    cells: list[dict[str, object]] = []
    for value, label in MODE_CHOICES:
        marker = "● " if value == current_per_chat else "○ "
        cells.append({"text": marker + label, "callback_data": f"M:{value}"})
    default_marker = "● " if not current_per_chat else "○ "
    return [
        cells,
        [{"text": default_marker + "Use config default", "callback_data": f"M:{_DEFAULT_TOKEN}"}],
        [{"text": "← Back to settings", "callback_data": "nav:settings"}],
    ]


def _render_settings(cs: "ChatState", cfg: "Config") -> BridgeReply:
    return BridgeReply(text=render_status(cs, cfg), keyboard=_settings_keyboard())


def _render_model_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur, src = _effective_model(cs, cfg)
    return BridgeReply(
        text=f"🤖 Choose a model for this chat\n\nCurrent: {cur}  [{src}]",
        keyboard=_model_keyboard(cs.model),
    )


def _render_mode_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur, src = _effective_mode(cs, cfg)
    return BridgeReply(
        text=f"🛡 Choose a mode for this chat\n\nCurrent: {cur}  [{src}]",
        keyboard=_mode_keyboard(cs.mode),
    )


async def handle_text_command(
    msg: "InboundMessage",
    cs: "ChatState",
    cfg: "Config",
) -> BridgeReply | None:
    """Return a reply for a slash command, else None (forward to agy)."""
    stripped = msg.text.strip()
    if not stripped:
        return None
    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/start":
        return BridgeReply(WELCOME_TEXT)
    if cmd == "/help":
        return BridgeReply(HELP_TEXT)
    if cmd in ("/status", "/info"):
        return BridgeReply(render_status(cs, cfg))
    if cmd == "/settings":
        return _render_settings(cs, cfg)
    if cmd == "/model":
        if args:
            if is_valid_model(args):
                cs.model = args
                return BridgeReply(f"🤖 Model set to {args}")
            return BridgeReply(f"⚠️ Unsupported model: {args}")
        return _render_model_picker(cs, cfg)
    if cmd == "/mode":
        if args:
            if args in {"code", "plan"}:
                cs.mode = args
                return BridgeReply(f"🛡 Mode set to {args}")
            return BridgeReply(f"⚠️ Unsupported mode: {args}")
        return _render_mode_picker(cs, cfg)
    if cmd == "/reset":
        cs.has_session = False
        return BridgeReply("🧹 Session reset. The next message starts fresh.")
    if cmd == "/thinking":
        mode = args.strip().lower()
        if mode in ("on", "true", "1"):
            return BridgeReply("💭 Thinking visibility is not available in agy print mode.")
        if mode in ("off", "false", "0"):
            return BridgeReply("💭 agy print mode already suppresses thinking streams.")
        return BridgeReply("💭 agy print mode does not expose thinking streams.")
    if cmd == "/compact":
        return BridgeReply("🗜️ Context compaction is not supported by agy print mode.")
    if cmd == "/image":
        mode = args.strip().lower()
        if mode in ("on", "true", "1"):
            cs.photo_enabled = True  # type: ignore[attr-defined]
            return BridgeReply("📸 Photo processing: ON")
        if mode in ("off", "false", "0"):
            cs.photo_enabled = False  # type: ignore[attr-defined]
            return BridgeReply("📸 Photo processing: OFF")
        return BridgeReply("📸 Photo processing toggle not available in this build.")
    if cmd == "/files":
        if args == "clean":
            removed = clean_inbox(cs.chat_dir, max_age_hours=0)
            return BridgeReply(f"🧹 Inbox cleaned: {removed} files removed")
        files = list_inbox(cs.chat_dir, limit=20)
        if not files:
            return BridgeReply("📭 Inbox is empty")
        return BridgeReply("📎 Inbox:\n" + "\n".join(f"• {f}" for f in files))
    if cmd == "/queue":
        return BridgeReply("📋 Queue status is available via daemon internals.")
    return None


def handle_callback(
    cq: "CallbackQuery",
    cs: "ChatState",
    cfg: "Config",
) -> BridgeReply:
    """Handle inline-keyboard button taps. Always returns a reply to render."""
    data = cq.data

    if data == "nav:status":
        return BridgeReply(render_status(cs, cfg))
    if data == "nav:settings":
        return _render_settings(cs, cfg)
    if data == "nav:model":
        return _render_model_picker(cs, cfg)
    if data == "nav:mode":
        return _render_mode_picker(cs, cfg)
    if data == "R":
        cs.has_session = False
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast="Session reset")

    if data.startswith("m:"):
        choice = data[2:]
        if choice == _DEFAULT_TOKEN:
            cs.model = ""
            toast = "Using config default"
        elif choice in MODEL_CHOICES:
            cs.model = choice
            toast = f"Model: {choice}"
        else:
            toast = "Unknown choice"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    if data.startswith("M:"):
        choice = data[2:]
        valid_modes = {v for v, _ in MODE_CHOICES}
        if choice == _DEFAULT_TOKEN:
            cs.mode = ""
            toast = "Using config default"
        elif choice in valid_modes:
            cs.mode = choice
            toast = f"Mode: {choice}"
        else:
            toast = "Unknown choice"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    return _render_settings(cs, cfg)
