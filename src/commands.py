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
import time
from typing import TYPE_CHECKING
import yaml

from src.agy_catalog import (
    EFFORT_LABELS,
    EFFORT_ORDER,
    ModelCatalog,
    QuotaSnapshot,
    build_fallback_catalog,
    fetch_models_catalog,
    fetch_quota_snapshot,
    format_reset_countdown,
    get_cached_catalog,
    get_cached_quota,
    render_progress_bar,
)
from src.agy_runner import model_uses_effort
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

_FALLBACK_CATALOG = build_fallback_catalog()

# (model_id, display_name, supported_efforts)
BASE_MODELS: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
    (fam.id, fam.label, fam.efforts) for fam in _FALLBACK_CATALOG.families
)

MODEL_CHOICES: tuple[str, ...] = tuple(sorted(_FALLBACK_CATALOG.all_valid_ids()))
MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("code", "Code (auto)"),
    ("plan", "Plan (read-only sandbox)"),
)
EFFORT_CHOICES: tuple[str, ...] = EFFORT_ORDER
_DEFAULT_TOKEN = "_DEFAULT"

WELCOME_TEXT = (
    "👋 Welcome! I'm your Antigravity bridge bot.\n\n"
    "Send me any message and I'll forward it to the agy CLI. "
    "Each chat has its own session that persists across messages.\n\n"
    "Commands: /start · /help · /status · /settings · /model · /mode · /effort · /usage · /cd · /pwd · /stop · /reset · /getfile · /mcp · /skills"
)

HELP_TEXT = (
    "📖 Antigravity Bridge Help\n\n"
    "Send any text to chat with agy.\n\n"
    "Commands:\n"
    "/status   — read-only system + chat summary\n"
    "/settings — control panel with buttons\n"
    "/model [id|refresh|default] — pick subscription model\n"
    "/mode     — pick a mode (Code or Plan)\n"
    "/effort   — pick reasoning effort (low/med/high/xhigh/max)\n"
    "/usage, /quota — live subscription quota, AI credits & chat tokens\n"
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
    {"command": "model", "description": "Select subscription model or refresh catalog"},
    {"command": "mode", "description": "Switch between Code and Plan mode"},
    {"command": "effort", "description": "Set reasoning effort (low/med/high/xhigh/max)"},
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
    {"command": "usage", "description": "Subscription quota, AI credits & chat usage"},
    {"command": "quota", "description": "Alias for /usage (subscription quota & credits)"},
]


@dataclass(frozen=True)
class BridgeReply:
    """Daemon's structured reply: text, optional inline keyboard, optional toast."""

    text: str
    keyboard: InlineKeyboard | None = None
    toast: str = ""
    document_path: Path | None = None


def _effective_model(
    cs: "ChatState",
    cfg: "Config",
    catalog: ModelCatalog | None = None,
) -> tuple[str, str]:
    """Return the chat's model override, config default, or CLI subscription default."""
    if cs.model:
        return cs.model, "chat"
    if cfg.agy.model:
        return cfg.agy.model, "config"
    cat = catalog or get_cached_catalog()
    if cat.default_model_id:
        return cat.default_model_id, "agy default"
    return DEFAULT_MODEL, "default"


def _effective_mode(cs: "ChatState", cfg: "Config") -> tuple[str, str]:
    """Return the chat's mode override or the config default."""
    if cs.mode:
        return cs.mode, "chat"
    return cfg.agy.mode, "config"


def render_status(
    cs: "ChatState",
    cfg: "Config",
    catalog: ModelCatalog | None = None,
) -> str:
    """Render the /status summary for a chat."""
    model, model_src = _effective_model(cs, cfg, catalog)
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
    """Build the inline keyboard for the /settings panel."""
    power_label = "🔴 Turn Off" if cs.active else "🟢 Turn On"
    return [
        [
            {"text": "🤖 Change model", "callback_data": "nav:model"},
            {"text": "🛡 Change mode", "callback_data": "nav:mode"},
        ],
        [
            {"text": "⚡ Change effort", "callback_data": "nav:effort"},
            {"text": "📊 Usage & Quota", "callback_data": "nav:usage"},
        ],
        [
            {"text": "🔌 MCP Servers", "callback_data": "nav:mcp"},
            {"text": "🧠 Skills", "callback_data": "nav:skills"},
        ],
        [
            {"text": "🧹 Reset session", "callback_data": "R"},
            {"text": power_label, "callback_data": "power:toggle"},
        ],
        [
            {"text": "🔄 Refresh", "callback_data": "nav:settings"},
        ],
    ]


def _model_keyboard(
    current_per_chat: str,
    catalog: ModelCatalog | None = None,
) -> InlineKeyboard:
    """Build the inline keyboard listing subscription-available models."""
    cat = catalog or get_cached_catalog()
    rows: InlineKeyboard = []
    for fam in cat.families:
        cb = f"m:{fam.id}"
        if len(cb.encode("utf-8")) > 64:
            continue
        is_active = fam.matches_model(current_per_chat)
        marker = "● " if is_active else "○ "
        rows.append([{"text": f"{marker}{fam.label}", "callback_data": cb}])
    default_marker = "● " if not current_per_chat else "○ "
    rows.append([
        {"text": default_marker + "Use agy/config default", "callback_data": f"m:{_DEFAULT_TOKEN}"}
    ])
    rows.append([
        {"text": "🔄 Refresh models", "callback_data": "model:refresh"},
        {"text": "← Back to settings", "callback_data": "nav:settings"},
    ])
    return rows


def _model_effort_keyboard(current_effort: str, supported_efforts: tuple[str, ...]) -> InlineKeyboard:
    """Build the inline keyboard for choosing a composable model's effort."""
    cells: list[dict[str, object]] = []
    for val in supported_efforts:
        marker = "● " if val == current_effort else "○ "
        label = EFFORT_LABELS.get(val, val.capitalize())
        cells.append({"text": f"{marker}{label}", "callback_data": f"e:{val}"})

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
    catalog: ModelCatalog | None = None,
) -> BridgeReply:
    """Render the effort sub-picker for a composable model base."""
    cat = catalog or get_cached_catalog()
    family = cat.find_family(model_id)
    if family is not None:
        label = family.label
        efforts = family.efforts or EFFORT_CHOICES
    else:
        label = model_id
        efforts = EFFORT_CHOICES
    cur_effort = getattr(cs, "effort", "") or "default"
    return BridgeReply(
        text=f"⚡ Choose reasoning effort for {label}\n\nModel: {model_id}\nCurrent effort: {cur_effort}",
        keyboard=_model_effort_keyboard(getattr(cs, "effort", ""), efforts),
        toast=toast,
    )


def _mode_keyboard(current_per_chat: str) -> InlineKeyboard:
    """Build the inline keyboard for mode selection."""
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


def _effort_keyboard(
    current_per_chat: str,
    supported_efforts: tuple[str, ...] = EFFORT_CHOICES,
) -> InlineKeyboard:
    """Build the inline keyboard for effort selection."""
    cells: list[dict[str, object]] = []
    for val in supported_efforts:
        marker = "● " if val == current_per_chat else "○ "
        label = EFFORT_LABELS.get(val, val.capitalize())
        cells.append({"text": marker + label, "callback_data": f"e:{val}"})
    default_marker = "● " if not current_per_chat else "○ "
    return [
        cells,
        [{"text": default_marker + "Use model default", "callback_data": f"e:{_DEFAULT_TOKEN}"}],
        [{"text": "← Back to settings", "callback_data": "nav:settings"}],
    ]


def _render_settings(cs: "ChatState", cfg: "Config") -> BridgeReply:
    """Render the settings panel text for the current chat state."""
    return BridgeReply(text=render_status(cs, cfg), keyboard=_settings_keyboard(cs))


def _render_model_picker(
    cs: "ChatState",
    cfg: "Config",
    catalog: ModelCatalog | None = None,
    *,
    toast: str = "",
) -> BridgeReply:
    """Render the subscription-aware model picker reply."""
    cat = catalog or get_cached_catalog()
    cur, src = _effective_model(cs, cfg, cat)
    if cat.source == "live":
        src_line = f"Subscription catalog: {len(cat.families)} models (live)"
    else:
        src_line = "Fallback catalog (live agy models unavailable — tap Refresh)"

    lines = [
        "🤖 Choose a model for this chat",
        "",
        f"Current: {cur}  [{src}]",
        src_line,
    ]
    if cs.model and cat.source == "live" and cs.model not in cat.all_valid_ids():
        lines.append(f"⚠️ '{cs.model}' is not in your active subscription catalog.")

    return BridgeReply(
        text="\n".join(lines),
        keyboard=_model_keyboard(cs.model, cat),
        toast=toast,
    )


def _render_mode_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    """Render the mode picker reply."""
    cur, src = _effective_mode(cs, cfg)
    return BridgeReply(
        text=f"🛡 Choose a mode for this chat\n\nCurrent: {cur}  [{src}]",
        keyboard=_mode_keyboard(cs.mode),
    )


def _render_effort_picker(
    cs: "ChatState",
    cfg: "Config",
    catalog: ModelCatalog | None = None,
) -> BridgeReply:
    """Render the effort picker reply, scoped to the active model's capabilities."""
    cat = catalog or get_cached_catalog()
    active_model, _ = _effective_model(cs, cfg, cat)
    family = cat.find_family(active_model)
    cur = getattr(cs, "effort", "") or "default"

    if family is not None and not family.efforts:
        return BridgeReply(
            text=(
                f"⚡ Reasoning effort is fixed for {family.label} ({family.id}).\n\n"
                "This model does not support adjustable effort levels."
            ),
            keyboard=[
                [{"text": "🤖 Change model", "callback_data": "nav:model"}],
                [{"text": "← Back to settings", "callback_data": "nav:settings"}],
            ],
        )

    efforts = family.efforts if (family is not None and family.efforts) else EFFORT_CHOICES
    return BridgeReply(
        text=f"⚡ Choose reasoning effort for this chat\n\nModel: {active_model}\nCurrent: {cur}",
        keyboard=_effort_keyboard(getattr(cs, "effort", ""), efforts),
    )


def _render_usage_reply(
    cs: "ChatState",
    quota: QuotaSnapshot | None = None,
    *,
    toast: str = "",
) -> BridgeReply:
    """Render live subscription quota, AI credits, and per-chat token usage."""
    keyboard: InlineKeyboard = [
        [
            {"text": "🔄 Refresh quota", "callback_data": "usage:refresh"},
            {"text": "🤖 Change model", "callback_data": "nav:model"},
        ],
        [{"text": "← Back to settings", "callback_data": "nav:settings"}],
    ]

    today = time.strftime("%Y-%m-%d")
    tokens_today = cs.usage_today_tokens if cs.usage_today_date == today else 0
    turns_today = cs.usage_today_turns if cs.usage_today_date == today else 0

    if quota is not None and quota.groups:
        lines = ["📊 Subscription Quota & Usage"]
        for grp in quota.groups:
            lines.append("")
            lines.append(f"🔹 {grp.name}")
            desc = grp.description.strip()
            if desc.lower().startswith("models within this group:"):
                lines.append(f"  {desc}")
            for b in grp.buckets:
                pct = round(b.remaining_fraction * 100)
                bar = render_progress_bar(b.remaining_fraction)
                if b.window == "5h":
                    label = "5-Hour"
                elif b.window == "weekly":
                    label = "Weekly"
                else:
                    label = b.name
                countdown = format_reset_countdown(b.reset_time)
                reset_str = f" (reset {countdown})" if countdown else ""
                lines.append(f"• {label}: [{bar}] {pct}%{reset_str}")

        if quota.credits_remaining is not None:
            lines.append("")
            lines.append(f"💳 AI Credits: {quota.credits_remaining}")

        lines.append("")
        lines.append("💬 Token Usage (chat ini)")
        if not cs.usage_total_turns and not cs.usage_last_total:
            lines.append("• Belum ada data token untuk chat ini.")
        else:
            if cs.usage_last_total:
                lines.append(
                    f"• Turn terakhir: {cs.usage_last_in:,} in / {cs.usage_last_out:,} out"
                    f" — {cs.usage_last_total:,} token"
                )
            lines.append(f"• Hari ini: {tokens_today:,} token ({turns_today} turn)")
            lines.append(f"• Total: {cs.usage_total_tokens:,} token ({cs.usage_total_turns} turn)")
        return BridgeReply("\n".join(lines), keyboard=keyboard, toast=toast)

    if not cs.usage_total_turns and not cs.usage_last_total:
        msg = "📊 Belum ada data usage untuk chat ini."
        if quota is not None and quota.error:
            msg += f"\n\n⚠️ Quota langganan tidak tersedia ({quota.error})."
        return BridgeReply(msg, keyboard=keyboard, toast=toast)

    lines = ["📊 Usage agy (chat ini)"]
    if cs.usage_last_total:
        lines.append(
            f"• Turn terakhir: {cs.usage_last_in:,} in / {cs.usage_last_out:,} out"
            f" — {cs.usage_last_total:,} token"
        )
    lines.append(f"• Hari ini: {tokens_today:,} token ({turns_today} turn)")
    lines.append(f"• Total: {cs.usage_total_tokens:,} token ({cs.usage_total_turns} turn)")
    if quota is not None and quota.error:
        lines.append(f"\n⚠️ Quota langganan tidak tersedia ({quota.error}).")
    return BridgeReply("\n".join(lines), keyboard=keyboard, toast=toast)


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
    """Render the /mcp reply from the chat's MCP inventory."""
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
    """Render the /skills reply from the available skills list."""
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
    *,
    agy_path: str = "agy",
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
        arg = args.strip()
        if arg:
            low_arg = arg.lower()
            if low_arg == "refresh":
                catalog = await fetch_models_catalog(agy_path, force_refresh=True)
                return _render_model_picker(cs, cfg, catalog)
            if low_arg in ("default", "reset"):
                cs.model = ""
                return BridgeReply("🤖 Model reset to default.")
            if not is_valid_model(arg):
                return BridgeReply(f"⚠️ Unsupported model: {arg}")
            catalog = await fetch_models_catalog(agy_path)
            if catalog.source == "live" and arg not in catalog.all_valid_ids():
                return BridgeReply(
                    f"⚠️ Model '{arg}' is not available in your active subscription.\n"
                    "Use /model to view available models."
                )
            cs.model = arg
            # Keep effort state coherent: it only applies to models that
            # consume it, so a stale value must not linger on fixed models.
            if not model_uses_effort(arg):
                cs.effort = ""
            else:
                fam = catalog.find_family(arg)
                if fam is not None and fam.efforts and cs.effort and cs.effort not in fam.efforts:
                    cs.effort = ""
            return BridgeReply(f"🤖 Model set to {arg}")
        catalog = await fetch_models_catalog(agy_path)
        return _render_model_picker(cs, cfg, catalog)
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
        catalog = get_cached_catalog(agy_path)
        if args:
            val = args.strip().lower()
            if val in ("reset", "default", "auto", "off"):
                cs.effort = ""
                return BridgeReply("⚡ Reasoning effort reset to default.")
            if val in EFFORT_CHOICES:
                active_model, _ = _effective_model(cs, cfg, catalog)
                fam = catalog.find_family(active_model)
                if fam is not None:
                    if not fam.efforts:
                        return BridgeReply(
                            f"⚠️ Effort is fixed (not adjustable) for {fam.label} ({fam.id})."
                        )
                    if val not in fam.efforts:
                        avail = ", ".join(fam.efforts)
                        return BridgeReply(
                            f"⚠️ {fam.label} does not support '{val}'. Available: {avail}."
                        )
                    if cs.model and cs.model != fam.id and cs.model in fam.variant_slugs():
                        cs.model = fam.id
                cs.effort = val
                return BridgeReply(f"⚡ Reasoning effort set to {val}.")
            return BridgeReply(
                f"⚠️ Unsupported effort: {args}. Choose: low, medium, high, xhigh, max, or default."
            )
        return _render_effort_picker(cs, cfg, catalog)
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
    if cmd in ("/usage", "/quota"):
        quota = await fetch_quota_snapshot(agy_path, force_refresh=True)
        return _render_usage_reply(cs, quota)
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


async def handle_callback_async(
    cq: "CallbackQuery",
    cs: "ChatState",
    cfg: "Config",
    *,
    agy_path: str = "agy",
) -> BridgeReply:
    """Handle inline-keyboard callbacks with live async catalog/quota lookups."""
    data = cq.data
    if data == "nav:model":
        catalog = await fetch_models_catalog(agy_path)
        return _render_model_picker(cs, cfg, catalog)
    if data == "model:refresh":
        catalog = await fetch_models_catalog(agy_path, force_refresh=True)
        toast = "Models refreshed" if catalog.source == "live" else "Using fallback catalog"
        return _render_model_picker(cs, cfg, catalog, toast=toast)
    if data == "nav:usage":
        quota = await fetch_quota_snapshot(agy_path, force_refresh=False)
        return _render_usage_reply(cs, quota)
    if data == "usage:refresh":
        quota = await fetch_quota_snapshot(agy_path, force_refresh=True)
        toast = "Quota refreshed" if not quota.error else "Quota unavailable"
        return _render_usage_reply(cs, quota, toast=toast)
    return handle_callback(cq, cs, cfg)


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
    if data == "model:refresh":
        return _render_model_picker(cs, cfg, toast="Cached catalog shown")
    if data == "nav:usage":
        return _render_usage_reply(cs, get_cached_quota())
    if data == "usage:refresh":
        return _render_usage_reply(cs, get_cached_quota(), toast="Cached quota shown")
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
        catalog = get_cached_catalog()
        if choice in catalog.all_valid_ids() or choice in MODEL_CHOICES or is_valid_model(choice):
            cs.model = choice
            if not model_uses_effort(choice):
                cs.effort = ""  # stale effort would be inert on this model
            family = catalog.find_family(choice)
            if family is not None and family.efforts:
                if cs.effort and cs.effort not in family.efforts:
                    cs.effort = ""
                return _render_model_effort_picker(
                    choice, cs, cfg, toast=f"Model: {choice}", catalog=catalog,
                )
            label = family.label if family is not None else choice
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
            catalog = get_cached_catalog()
            active_model, _ = _effective_model(cs, cfg, catalog)
            fam = catalog.find_family(active_model)
            if fam is not None and fam.efforts and choice not in fam.efforts:
                toast = f"Unsupported effort for {fam.label}"
            else:
                if fam is not None and cs.model and cs.model in fam.variant_slugs():
                    cs.model = fam.id
                cs.effort = choice
                toast = f"Effort: {choice}"
        else:
            toast = "Unknown choice"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    return _render_settings(cs, cfg)

