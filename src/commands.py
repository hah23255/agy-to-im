"""Bridge command handlers — text slash commands and inline-keyboard callbacks."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import TYPE_CHECKING
import yaml

from src.media import clean_inbox, list_inbox
from src.queue import QUEUE
from src.state import is_valid_model
from src.telegram import InlineKeyboard

if TYPE_CHECKING:
    from src.config import Config
    from src.daemon import _TelegramLike
    from src.state import ChatState
    from src.telegram import CallbackQuery, InboundMessage

DEFAULT_MODEL = "gemini-3.8-flash-low"

# (model_id, display_name, supported_efforts)
BASE_MODELS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("gemini-3.8-flash", "Gemini 3.8 Flash", ("low", "medium", "high")),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)", ()),
    ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)", ()),
    ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)", ()),
)

MODEL_CHOICES: tuple[str, ...] = tuple(m[0] for m in BASE_MODELS) + (
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
)
MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("code", "Code (auto)"),
    ("plan", "Plan (read-only sandbox)"),
)
EFFORT_CHOICES: tuple[str, ...] = ("low", "medium", "high", "max")
_DEFAULT_TOKEN = "_DEFAULT"

WELCOME_TEXT = (
    "👋 Welcome! I'm your Antigravity bridge bot.\n\n"
    "Send me any message and I'll forward it to the agy CLI. "
    "Each chat has its own session that persists across messages.\n\n"
    "Commands: /start · /help · /status · /settings · /model · /mode · /effort · /cd · /pwd · /stop · /reset · /getfile · /mcp · /skills"
)

HELP_TEXT = (
    "📖 Antigravity Bridge Help\n\n"
    "Send any text to chat with agy.\n\n"
    "Commands:\n"
    "/status   — read-only system + chat summary\n"
    "/settings — control panel with buttons\n"
    "/model    — pick a model (per-chat override)\n"
    "/mode     — pick a mode (Code or Plan)\n"
    "/effort   — pick reasoning effort (low/med/high/max)\n"
    "/pwd      — show current working directory\n"
    "/cd <dir> — change working directory for this chat\n"
    "/getfile <file> — download a file from workspace\n"
    "/mcp [name]     — view or inspect MCP servers & tools\n"
    "/skills [name]  — list or inspect registered skills\n"
    "/on       — activate bot (respond to messages)\n"
    "/off      — deactivate bot (ignore non-command messages)\n"
    "/stop     — abort actively running agy turn\n"
    "/reset    — fresh agy session for this chat\n"
    "/info     — same as /status\n"
    "/image on|off — toggle photo processing\n"
    "/files    — list recent uploads\n"
    "/start, /help — these messages\n\n"
    "Per-chat overrides take precedence over config.json."
)

BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "start", "description": "Welcome message and introduction"},
    {"command": "ping", "description": "Quick latency and health check"},
    {"command": "help", "description": "Show available commands and usage"},
    {"command": "status", "description": "System, model, mode, and session summary"},
    {"command": "settings", "description": "Interactive settings panel"},
    {"command": "model", "description": "Select or view model override"},
    {"command": "mode", "description": "Switch between Code and Plan mode"},
    {"command": "effort", "description": "Set reasoning effort (low/med/high/max)"},
    {"command": "pwd", "description": "Show current working directory"},
    {"command": "cd", "description": "Change working directory for this chat"},
    {"command": "on", "description": "Activate bot (respond to messages)"},
    {"command": "off", "description": "Deactivate bot (ignore non-command messages)"},
    {"command": "stop", "description": "Abort active agy turn"},
    {"command": "reset", "description": "Start a fresh session"},
    {"command": "files", "description": "List recent uploaded files"},
    {"command": "image", "description": "Toggle photo processing (on/off)"},
    {"command": "getfile", "description": "Download a file from workspace"},
    {"command": "mcp", "description": "View and manage MCP servers and tools"},
    {"command": "skills", "description": "List and inspect available skills"},
    {"command": "queue", "description": "Turn queue status"},
]


@dataclass(frozen=True)
class BridgeReply:
    """Daemon's structured reply: text, optional inline keyboard, optional toast."""

    text: str
    keyboard: InlineKeyboard | None = None
    toast: str = ""
    document_path: Path | None = None


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
    effort = getattr(cs, "effort", "") or "default"
    session = "active (resumes next turn)" if cs.has_session else "fresh (next turn starts new)"
    home = os.path.expanduser("~")
    workdir = cs.chat_dir.replace(home, "~", 1)
    status_icon = "🟢" if cs.active else "🔴"
    status_label = "Active" if cs.active else "Inactive (mati)"
    return (
        f"{status_icon} Antigravity Bridge — {status_label}\n"
        f"Model:      {model}  [{model_src}]\n"
        f"Mode:       {mode}  [{mode_src}]\n"
        f"Effort:     {effort}\n"
        "\n"
        "This chat:\n"
        f"  Session:  {session}\n"
        f"  Turns:    {cs.turn_count}\n"
        f"  Workdir:  {workdir}"
    )


def _settings_keyboard(cs: "ChatState") -> InlineKeyboard:
    power_label = "🔴 Turn Off" if cs.active else "🟢 Turn On"
    return [
        [
            {"text": "🤖 Change model", "callback_data": "nav:model"},
            {"text": "🛡 Change mode", "callback_data": "nav:mode"},
        ],
        [
            {"text": "⚡ Change effort", "callback_data": "nav:effort"},
            {"text": "🧹 Reset session", "callback_data": "R"},
        ],
        [
            {"text": "🔌 MCP Servers", "callback_data": "nav:mcp"},
            {"text": "🧠 Skills", "callback_data": "nav:skills"},
        ],
        [
            {"text": power_label, "callback_data": "power:toggle"},
            {"text": "🔄 Refresh", "callback_data": "nav:settings"},
        ],
    ]


def _model_keyboard(current_per_chat: str) -> InlineKeyboard:
    rows: InlineKeyboard = []
    for model_id, label, _ in BASE_MODELS:
        is_active = (current_per_chat == model_id) or current_per_chat.startswith(f"{model_id}-")
        marker = "● " if is_active else "○ "
        rows.append([{"text": f"{marker}{label}", "callback_data": f"m:{model_id}"}])
    default_marker = "● " if not current_per_chat else "○ "
    rows.append([
        {"text": default_marker + "Use config default", "callback_data": f"m:{_DEFAULT_TOKEN}"}
    ])
    rows.append([{"text": "← Back to settings", "callback_data": "nav:settings"}])
    return rows


def _model_effort_keyboard(current_effort: str, supported_efforts: tuple[str, ...]) -> InlineKeyboard:
    cells: list[dict[str, object]] = []
    for val in supported_efforts:
        marker = "● " if val == current_effort else "○ "
        cells.append({"text": f"{marker}{val.capitalize()}", "callback_data": f"e:{val}"})

    rows: InlineKeyboard = []
    if cells:
        rows.append(cells)
    default_marker = "● " if not current_effort else "○ "
    rows.append([{"text": default_marker + "Use model default", "callback_data": f"e:{_DEFAULT_TOKEN}"}])
    rows.append([
        {"text": "← Back to models", "callback_data": "nav:model"},
        {"text": "⚙️ Settings", "callback_data": "nav:settings"},
    ])
    return rows


def _render_model_effort_picker(
    model_id: str,
    cs: "ChatState",
    cfg: "Config",
    toast: str = "",
) -> BridgeReply:
    label = model_id
    efforts: tuple[str, ...] = EFFORT_CHOICES
    for mid, mlabel, mefforts in BASE_MODELS:
        if mid == model_id:
            label = mlabel
            if mefforts:
                efforts = mefforts
            break
    cur_effort = getattr(cs, "effort", "") or "default"
    return BridgeReply(
        text=f"⚡ Choose reasoning effort for {label}\n\nModel: {model_id}\nCurrent effort: {cur_effort}",
        keyboard=_model_effort_keyboard(getattr(cs, "effort", ""), efforts),
        toast=toast,
    )


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


def _effort_keyboard(current_per_chat: str) -> InlineKeyboard:
    cells: list[dict[str, object]] = []
    for val in EFFORT_CHOICES:
        marker = "● " if val == current_per_chat else "○ "
        cells.append({"text": marker + val.capitalize(), "callback_data": f"e:{val}"})
    default_marker = "● " if not current_per_chat else "○ "
    return [
        cells,
        [{"text": default_marker + "Use model default", "callback_data": f"e:{_DEFAULT_TOKEN}"}],
        [{"text": "← Back to settings", "callback_data": "nav:settings"}],
    ]


def _render_settings(cs: "ChatState", cfg: "Config") -> BridgeReply:
    return BridgeReply(text=render_status(cs, cfg), keyboard=_settings_keyboard(cs))


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


def _render_effort_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur = getattr(cs, "effort", "") or "default"
    return BridgeReply(
        text=f"⚡ Choose reasoning effort for this chat\n\nCurrent: {cur}",
        keyboard=_effort_keyboard(getattr(cs, "effort", "")),
    )


def _list_mcp_servers() -> list[dict[str, str]]:
    """List configured MCP servers via agy CLI or disk inspection."""
    agy_bin = shutil.which("agy") or "/usr/bin/agy"
    servers: list[dict[str, str]] = []
    try:
        proc = subprocess.run([agy_bin, "mcp", "list"], capture_output=True, text=True, timeout=5)
        lines = proc.stdout.strip().splitlines()
        if len(lines) > 1:
            for line in lines[1:]:
                parts = re.split(r"\s{2,}", line.strip())
                if len(parts) >= 3:
                    servers.append({
                        "name": parts[0],
                        "type": parts[1],
                        "status": parts[2],
                        "cmd": parts[3] if len(parts) > 3 else "",
                    })
    except Exception:
        pass

    if not servers:
        mcp_root = Path.home() / ".gemini" / "antigravity-cli" / "mcp"
        if mcp_root.is_dir():
            for d in sorted(mcp_root.iterdir()):
                if d.is_dir():
                    servers.append({
                        "name": d.name,
                        "type": "stdio",
                        "status": "enabled",
                        "cmd": "",
                    })
    return servers


def _get_mcp_tools(server_name: str) -> list[dict[str, str]]:
    """Get list of tools and descriptions provided by an MCP server."""
    mcp_dir = Path.home() / ".gemini" / "antigravity-cli" / "mcp" / server_name
    tools: list[dict[str, str]] = []
    if mcp_dir.is_dir():
        for tf in sorted(mcp_dir.glob("*.json")):
            try:
                data = json.loads(tf.read_text(encoding="utf-8"))
                tools.append({
                    "name": data.get("name", tf.stem),
                    "description": data.get("description", ""),
                })
            except Exception:
                tools.append({"name": tf.stem, "description": ""})
    return tools


def _render_mcp_reply(args: str) -> BridgeReply:
    args = args.strip()
    if args.startswith("enable ") or args.startswith("disable "):
        sub, name = args.split(maxsplit=1)
        agy_bin = shutil.which("agy") or "/usr/bin/agy"
        try:
            res = subprocess.run([agy_bin, "mcp", sub, name.strip()], capture_output=True, text=True, timeout=5)
            msg = res.stdout.strip() or res.stderr.strip() or f"MCP server '{name}' {sub}d."
            return BridgeReply(f"🔌 {msg}")
        except Exception as exc:
            return BridgeReply(f"⚠️ Failed to {sub} MCP server: {exc}")

    servers = _list_mcp_servers()
    if not servers:
        return BridgeReply("🔌 No MCP servers configured.")

    if args and args not in ("list", "ls", "all"):
        target_name = args.lower()
        matched = next((s for s in servers if s["name"].lower() == target_name), None)
        if not matched:
            avail = ", ".join(f"<code>{s['name']}</code>" for s in servers)
            return BridgeReply(f"❌ MCP server '{args}' not found.\nAvailable: {avail}")

        tools = _get_mcp_tools(matched["name"])
        status_icon = "🟢" if matched["status"].lower() == "enabled" else "⚪"
        lines = [
            f"🔌 <b>MCP Server: {matched['name']}</b>",
            f"Status: {status_icon} <code>{matched['status']}</code> ({matched['type']})",
        ]
        if matched["cmd"]:
            lines.append(f"Command: <code>{matched['cmd']}</code>")

        lines.append(f"\n🛠️ <b>Available Tools ({len(tools)}):</b>")
        if not tools:
            lines.append("<i>No tool schemas registered in ~/.gemini/antigravity-cli/mcp/</i>")
        else:
            for t in tools:
                desc = f"\n  <i>{t['description']}</i>" if t["description"] else ""
                lines.append(f"• <code>{t['name']}</code>{desc}")

        return BridgeReply("\n".join(lines))

    lines = ["🔌 <b>Configured MCP Servers:</b>\n"]
    for s in servers:
        status_icon = "🟢" if s["status"].lower() == "enabled" else "⚪"
        tools = _get_mcp_tools(s["name"])
        tool_count = f"{len(tools)} tools" if tools else "active"
        cmd_str = f"\n  <i>Command:</i> <code>{s['cmd']}</code>" if s["cmd"] else ""
        tool_sample = ""
        if tools:
            sample_names = ", ".join(f"<code>{t['name']}</code>" for t in tools[:3])
            if len(tools) > 3:
                sample_names += f", +{len(tools) - 3} more"
            tool_sample = f"\n  <i>Tools:</i> {sample_names}"

        lines.append(f"• <b>{s['name']}</b> {status_icon} <code>{s['status']}</code> ({tool_count}){cmd_str}{tool_sample}\n")

    lines.append("💡 <i>Ketik <code>/mcp &lt;nama&gt;</code> untuk melihat detail tools server tertentu.</i>")
    return BridgeReply("\n".join(lines))


def _list_skills(workspace_dir: str = "") -> list[dict[str, str]]:
    """Discover all skills across builtin, global, and workspace locations."""
    search_dirs = [
        Path.home() / ".gemini" / "antigravity-cli" / "builtin" / "skills",
        Path.home() / ".gemini" / "skills",
        Path.home() / ".gemini" / "antigravity-cli" / "skills",
    ]
    if workspace_dir:
        search_dirs.extend([
            Path(workspace_dir) / ".agents" / "skills",
            Path(workspace_dir) / "skills",
        ])

    seen: set[str] = set()
    skills: list[dict[str, str]] = []
    for sdir in search_dirs:
        if not sdir.is_dir():
            continue
        for sf in sorted(sdir.glob("*/SKILL.md")):
            skill_folder = sf.parent.name
            if skill_folder in seen:
                continue
            seen.add(skill_folder)
            try:
                content = sf.read_text(encoding="utf-8")
                fm = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
                name = skill_folder
                desc = ""
                if fm:
                    meta = yaml.safe_load(fm.group(1)) or {}
                    name = meta.get("name", skill_folder)
                    desc = str(meta.get("description", "")).strip()
                skills.append({"name": name, "folder": skill_folder, "description": desc, "path": str(sf)})
            except Exception:
                skills.append({"name": skill_folder, "folder": skill_folder, "description": "", "path": str(sf)})
    return skills


def _render_skills_reply(args: str, cs: ChatState) -> BridgeReply:
    skills = _list_skills(cs.chat_dir)
    if not skills:
        return BridgeReply("🧠 No skills discovered.")

    args = args.strip().lower()
    if args and args not in ("list", "ls", "all"):
        normalized_arg = args.replace("_", "-")
        matched = next(
            (s for s in skills if s["name"].lower() == args or s["folder"].lower() == args
             or s["name"].lower() == normalized_arg or s["folder"].lower() == normalized_arg),
            None,
        )
        if not matched:
            avail = ", ".join(f"<code>{s['name']}</code>" for s in skills)
            return BridgeReply(f"❌ Skill '{args}' not found.\nAvailable skills: {avail}")

        try:
            content = Path(matched["path"]).read_text(encoding="utf-8")
            body = re.sub(r"^---\s*\n.*?\n---\s*", "", content, flags=re.DOTALL).strip()
            if len(body) > 2000:
                body = body[:2000] + "\n\n<i>[...konten dipotong...]</i>"
        except Exception:
            body = matched["description"] or "No detailed guide available."

        lines = [
            f"🧠 <b>Skill: {matched['name']}</b>",
            f"<i>{matched['description']}</i>\n",
            "────────────────",
            body,
            "────────────────",
            f"📂 <code>{matched['path']}</code>",
        ]
        return BridgeReply("\n".join(lines))

    lines = ["🧠 <b>Registered Skills:</b>\n"]
    for s in skills:
        desc = f"\n  <i>{s['description'][:120]}...</i>" if len(s["description"]) > 120 else (f"\n  <i>{s['description']}</i>" if s["description"] else "")
        lines.append(f"• <b>{s['name']}</b>{desc}\n")

    lines.append("💡 <i>Ketik <code>/skills &lt;nama_skill&gt;</code> untuk membaca panduan detail skill.</i>")
    return BridgeReply("\n".join(lines))



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
        cs.active = True
        return BridgeReply(WELCOME_TEXT)
    if cmd == "/ping":
        return BridgeReply("🏓 Pong! Antigravity Bridge aktif dan siap menerima perintah.")
    if cmd in ("/help", "/commands"):
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
    if cmd == "/pwd":
        home = os.path.expanduser("~")
        pretty = cs.chat_dir.replace(home, "~", 1)
        return BridgeReply(f"📂 Current working directory:\n{pretty}\n\nFull path:\n{cs.chat_dir}")
    if cmd == "/cd":
        if not args:
            target_str = cfg.agy.default_workdir or os.path.expanduser("~")
            target = Path(target_str).expanduser().resolve()
        else:
            target = Path(args.strip()).expanduser().resolve()

        if not target.is_dir():
            return BridgeReply(f"❌ Directory not found: {args}")

        cs.chat_dir = str(target)
        cs.has_session = False
        home = os.path.expanduser("~")
        pretty = str(target).replace(home, "~", 1)
        return BridgeReply(
            f"✅ Switched working directory to:\n{pretty}\n\n(A new session has been started for this workspace.)"
        )
    if cmd == "/effort":
        if args:
            val = args.strip().lower()
            if val in ("reset", "default", "auto", "off"):
                cs.effort = ""
                return BridgeReply("⚡ Reasoning effort reset to default.")
            if val in EFFORT_CHOICES:
                cs.effort = val
                return BridgeReply(f"⚡ Reasoning effort set to {val}.")
            return BridgeReply(f"⚠️ Unsupported effort: {args}. Choose: low, medium, high, max, or default.")
        return _render_effort_picker(cs, cfg)
    if cmd in ("/stop", "/abort"):
        from src.agy_runner import abort_turn
        if abort_turn(cs.chat_dir):
            return BridgeReply("🛑 Active turn aborted.")
        return BridgeReply("ℹ️ No active agy process is currently running for this chat.")
    if cmd == "/on":
        cs.active = True
        return BridgeReply("🟢 Bot diaktifkan. Saya akan merespon pesan Anda.")
    if cmd == "/off":
        cs.active = False
        return BridgeReply("🔴 Bot dimatikan. Saya akan mengabaikan pesan non-command.\n\nKetik /on untuk mengaktifkan kembali.")
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
    if cmd == "/style":
        mode = args.strip().lower()
        if mode in ("on", "true", "1"):
            setattr(cs, "style_hint", True)
            return BridgeReply("🎨 Telegram style hint: ON (pesan akan diformat khusus Telegram)")
        if mode in ("off", "false", "0"):
            setattr(cs, "style_hint", False)
            return BridgeReply("🎨 Telegram style hint: OFF (pesan diteruskan mentah ke agy)")
        curr = "ON" if getattr(cs, "style_hint", True) else "OFF"
        return BridgeReply(f"🎨 Telegram style hint is currently {curr}. Use /style on|off to change.")
    if cmd == "/files":
        if args == "clean":
            removed = clean_inbox(cs.chat_dir, max_age_hours=0)
            return BridgeReply(f"🧹 Inbox cleaned: {removed} files removed")
        files = list_inbox(cs.chat_dir, limit=20)
        if not files:
            return BridgeReply("📭 Inbox is empty")
        return BridgeReply("📎 Inbox:\n" + "\n".join(f"• {f}" for f in files))
    if cmd == "/queue":
        lines = QUEUE.status()
        return BridgeReply("📋 Turn queue\n" + "\n".join(f"• {line}" for line in lines))
    if cmd in ("/getfile", "/sendfile", "/download"):
        if not args:
            return BridgeReply("⚠️ Usage: /getfile <filename>")
        fname = args.strip().strip("'\"`")
        target = Path(fname)
        if not target.is_absolute():
            target = (Path(cs.chat_dir) / target).resolve()
        else:
            target = target.resolve()

        if not target.is_file():
            inbox_target = Path(cs.chat_dir) / ".bridge-inbox" / fname
            if inbox_target.is_file():
                target = inbox_target
            else:
                return BridgeReply(f"❌ File not found: {fname}")

        allowed_dirs = [Path.home().resolve()]
        if cfg.agy.default_workdir:
            allowed_dirs.append(Path(cfg.agy.default_workdir).expanduser().resolve())
        is_safe = any(target.is_relative_to(d) for d in allowed_dirs)
        if not is_safe:
            return BridgeReply("⛔ Access denied: file is outside allowed directories.")

        return BridgeReply(
            text=f"📤 Sending: {target.name} ({target.stat().st_size // 1024} KB)",
            document_path=target,
        )
    if cmd in ("/mcp", "/tool", "/tools"):
        return _render_mcp_reply(args)
    if cmd in ("/skill", "/skills"):
        return _render_skills_reply(args, cs)
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
    if data == "nav:effort":
        return _render_effort_picker(cs, cfg)
    if data == "nav:mcp":
        return _render_mcp_reply("")
    if data == "nav:skills":
        return _render_skills_reply("", cs)
    if data == "R":
        cs.has_session = False
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast="Session reset")

    if data == "power:toggle":
        cs.active = not cs.active
        toast = "🟢 Bot aktif" if cs.active else "🔴 Bot mati"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    if data.startswith("m:"):
        choice = data[2:]
        if choice == _DEFAULT_TOKEN:
            cs.model = ""
            toast = "Using config default"
            rep = _render_settings(cs, cfg)
            return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)
        elif choice in MODEL_CHOICES or is_valid_model(choice):
            cs.model = choice
            model_info = next((m for m in BASE_MODELS if m[0] == choice), None)
            if model_info and model_info[2]:
                return _render_model_effort_picker(choice, cs, cfg, toast=f"Model: {choice}")
            label = model_info[1] if model_info else choice
            toast = f"Model: {label}"
            rep = _render_settings(cs, cfg)
            return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)
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

    if data.startswith("e:"):
        choice = data[2:]
        if choice == _DEFAULT_TOKEN:
            cs.effort = ""
            toast = "Using default effort"
        elif choice in EFFORT_CHOICES:
            cs.effort = choice
            toast = f"Effort: {choice}"
        else:
            toast = "Unknown choice"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    return _render_settings(cs, cfg)

