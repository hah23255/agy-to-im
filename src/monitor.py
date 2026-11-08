"""Real-time turn monitoring and activity tracking for Antigravity Telegram Bridge.

Tracks agent tool executions and file operations from the `stream-json` event stream
and provides throttled in-place status updates in Telegram to avoid message spam.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import html
import logging
from pathlib import Path
import time
from typing import Any

LOG = logging.getLogger("antigravity_telegram_bridge")

_THROTTLE_INTERVAL = 1.5  # Minimum seconds between Telegram editMessageText calls
_MAX_VISIBLE_STEPS = 6    # Maximum steps shown in live status message


def _clean_path(raw_path: str, chat_dir: str = "") -> str:
    """Format file path into a concise, readable string."""
    if not raw_path:
        return ""
    try:
        p = Path(raw_path)
        if chat_dir:
            try:
                rel = p.relative_to(chat_dir)
                return str(rel)
            except (ValueError, RuntimeError):
                pass
        home = str(Path.home())
        if raw_path.startswith(home):
            return "~" + raw_path[len(home):]
        return p.name if len(raw_path) > 35 else raw_path
    except Exception:
        return raw_path[:35]


def format_tool_action(tool_name: str, params: dict[str, Any], chat_dir: str = "") -> tuple[str, str]:
    """Return (icon, human_readable_description) for a tool call."""
    name_lower = tool_name.lower()

    if name_lower in ("write_to_file", "writefile"):
        target = _clean_path(str(params.get("TargetFile", "") or params.get("target_file", "")), chat_dir)
        return "✍️", f"Membuat file <code>{html.escape(target)}</code>" if target else "Menulis file"

    if name_lower in ("replace_file_content", "multi_replace_file_content", "sed_file"):
        target = _clean_path(str(params.get("TargetFile", "") or params.get("path", "")), chat_dir)
        return "📝", f"Mengedit file <code>{html.escape(target)}</code>" if target else "Mengedit file"

    if name_lower in ("view_file", "read_file"):
        target = _clean_path(str(params.get("AbsolutePath", "") or params.get("path", "")), chat_dir)
        return "📖", f"Membaca file <code>{html.escape(target)}</code>" if target else "Membaca file"

    if name_lower in ("run_command", "bash", "execute_command"):
        cmd = str(params.get("CommandLine", "") or params.get("command", "")).strip()
        # Truncate command display
        display_cmd = (cmd[:35] + "...") if len(cmd) > 38 else cmd
        return "⚡", f"Menjalankan <code>{html.escape(display_cmd)}</code>" if display_cmd else "Menjalankan perintah terminal"

    if name_lower in ("call_mcp_tool", "mcp"):
        server = str(params.get("ServerName", "") or params.get("server", ""))
        tool = str(params.get("ToolName", "") or params.get("tool", ""))
        if "octave" in server.lower() or "octave" in tool.lower():
            return "🔢", "Menjalankan perhitungan Octave"
        return "🔌", f"MCP <b>{html.escape(server)}</b>: <code>{html.escape(tool)}</code>"

    if "octave" in name_lower:
        return "🔢", "Menjalankan script Octave"

    if name_lower in ("search_web", "web_search"):
        q = str(params.get("query", "")).strip()
        disp_q = (q[:28] + "...") if len(q) > 30 else q
        return "🌐", f"Mencari web: <i>{html.escape(disp_q)}</i>" if disp_q else "Mencari web"

    if name_lower in ("read_url_content", "open_browser_url"):
        u = str(params.get("Url", "") or params.get("url", "")).strip()
        disp_u = (u[:30] + "...") if len(u) > 33 else u
        return "🌐", f"Membaca web: <code>{html.escape(disp_u)}</code>" if disp_u else "Membaca halaman web"

    if name_lower in ("list_dir", "find_by_name"):
        d = _clean_path(str(params.get("DirectoryPath", "") or params.get("path", "")), chat_dir)
        return "📁", f"Melihat folder <code>{html.escape(d or '.')}</code>"

    if name_lower in ("grep_search", "grep"):
        pattern = str(params.get("query", "") or params.get("pattern", "")).strip()
        disp_p = (pattern[:22] + "...") if len(pattern) > 25 else pattern
        return "🔎", f"Mencari kode: <i>{html.escape(disp_p)}</i>"

    if "subagent" in name_lower:
        return "🤖", "Menjalankan subagent"

    return "🛠️", f"Tool: <code>{html.escape(tool_name)}</code>"


@dataclass
class ActivityStep:
    step_index: int
    tool_name: str
    icon: str
    action_text: str
    status: str = "active"  # "active", "done", "failed"
    duration: float = 0.0


class TurnMonitor:
    """Manages real-time status updates in Telegram during an agy turn."""

    def __init__(
        self,
        tg: Any,
        chat_id: int,
        message_id: int | None,
        chat_dir: str = "",
    ) -> None:
        self.tg = tg
        self.chat_id = chat_id
        self.message_id = message_id
        self.chat_dir = chat_dir
        self.start_time = time.monotonic()
        self.steps: list[ActivityStep] = []
        self._step_by_index: dict[int, ActivityStep] = {}
        self.is_thinking: bool = False
        self._last_edit_time: float = 0.0
        self._pending_update: bool = False
        self._timer_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._finished: bool = False

    def has_tools(self) -> bool:
        """Return True if any tool steps were recorded."""
        return len(self.steps) > 0

    def on_stream_event(self, event: dict[str, Any]) -> None:
        """Process an NDJSON event from agy's stream-json output."""
        event_type = event.get("event")
        if event_type == "step_update":
            step = event.get("step_update", {})
            step_type = step.get("step_type")
            step_index = int(step.get("step_index", 0))
            state = step.get("state", "").upper()

            if step_type == "tool":
                tool_name = step.get("tool_name", "")
                tool_info = step.get("tool_info", {})
                params = tool_info.get("parameters", {}) if isinstance(tool_info, dict) else {}
                duration = float(step.get("duration_seconds", 0.0))

                if state == "ACTIVE":
                    if step_index not in self._step_by_index:
                        icon, action = format_tool_action(tool_name, params, self.chat_dir)
                        act = ActivityStep(
                            step_index=step_index,
                            tool_name=tool_name,
                            icon=icon,
                            action_text=action,
                            status="active",
                        )
                        self.steps.append(act)
                        self._step_by_index[step_index] = act
                        self.is_thinking = False
                        self._schedule_push()
                elif state == "DONE":
                    act = self._step_by_index.get(step_index)
                    if act is not None:
                        act.status = "done"
                        act.duration = duration
                    else:
                        icon, action = format_tool_action(tool_name, params, self.chat_dir)
                        act = ActivityStep(
                            step_index=step_index,
                            tool_name=tool_name,
                            icon=icon,
                            action_text=action,
                            status="done",
                            duration=duration,
                        )
                        self.steps.append(act)
                        self._step_by_index[step_index] = act
                    self.is_thinking = False
                    self._schedule_push()

            elif step_type == "agent_response":
                if state == "ACTIVE" and not self.is_thinking and not any(s.status == "active" for s in self.steps):
                    self.is_thinking = True
                    self._schedule_push()

    def render_live(self) -> str:
        """Render current live monitoring status in HTML."""
        elapsed = int(time.monotonic() - self.start_time)
        lines = [f"⏳ <b>Antigravity sedang bekerja...</b> <i>({elapsed}s)</i>\n"]

        if self.steps:
            lines.append("⚙️ <b>Aktivitas:</b>")
            display_steps = self.steps
            if len(self.steps) > _MAX_VISIBLE_STEPS:
                hidden_count = len(self.steps) - (_MAX_VISIBLE_STEPS - 1)
                lines.append(f"• <i>... (+{hidden_count} aktivitas sebelumnya)</i>")
                display_steps = self.steps[-(_MAX_VISIBLE_STEPS - 1):]

            for s in display_steps:
                if s.status == "active":
                    status_prefix = "⏳ "
                    suffix = " <i>(berjalan...)</i>"
                elif s.status == "done":
                    status_prefix = "✅ "
                    suffix = f" <i>({s.duration:.1f}s)</i>" if s.duration > 0.05 else ""
                else:
                    status_prefix = "❌ "
                    suffix = " <i>(gagal)</i>"
                lines.append(f"• {status_prefix}{s.icon} {s.action_text}{suffix}")

        if self.is_thinking and not any(s.status == "active" for s in self.steps):
            lines.append("• 💭 <i>Merumuskan balasan...</i>")

        lines.append("\n💡 <i>Kirim /stop untuk membatalkan</i>")
        return "\n".join(lines)

    def render_final(self, success: bool = True, error: str = "") -> str:
        """Render permanent summary checklist when turn finishes."""
        elapsed = int(time.monotonic() - self.start_time)
        if success:
            lines = [f"✅ <b>Pekerjaan Selesai</b> <i>({elapsed}s)</i>\n"]
            lines.append("⚙️ <b>Aktivitas yang dilakukan:</b>")
        else:
            header = "🛑 <b>Pekerjaan Dibatalkan</b>" if "batal" in error.lower() or "abort" in error.lower() else "⚠️ <b>Pekerjaan Gagal</b>"
            lines = [f"{header} <i>({elapsed}s)</i>\n"]
            lines.append("⚙️ <b>Aktivitas terakhir:</b>")

        display_steps = self.steps
        if len(self.steps) > 8:
            hidden_count = len(self.steps) - 7
            lines.append(f"• <i>... (+{hidden_count} aktivitas awal)</i>")
            display_steps = self.steps[-7:]

        for s in display_steps:
            mark = "✅ " if s.status == "done" or success else ("❌ " if s.status == "failed" else "• ")
            lines.append(f"• {mark}{s.icon} {s.action_text}")

        if error and not success:
            lines.append(f"\n⚠️ <i>Info: {html.escape(error)}</i>")

        return "\n".join(lines)

    def _schedule_push(self) -> None:
        """Schedule a throttled update to Telegram."""
        if self._finished or self.message_id is None:
            return

        now = time.monotonic()
        elapsed = now - self._last_edit_time
        if elapsed >= _THROTTLE_INTERVAL:
            asyncio.create_task(self._push_update())
        else:
            self._pending_update = True
            if self._timer_task is None or self._timer_task.done():
                delay = _THROTTLE_INTERVAL - elapsed
                self._timer_task = asyncio.create_task(self._delayed_push(delay))

    async def _delayed_push(self, delay: float) -> None:
        """Push a pending update once the throttle delay elapses."""
        try:
            await asyncio.sleep(delay)
            if self._pending_update and not self._finished:
                await self._push_update()
        except asyncio.CancelledError:
            pass

    async def _push_update(self) -> None:
        """Directly push the live rendered text to Telegram."""
        if self._finished or self.message_id is None:
            return

        async with self._lock:
            self._pending_update = False
            self._last_edit_time = time.monotonic()
            text = self.render_live()
            try:
                await self.tg.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text,
                    parse_mode="HTML",
                )
            except Exception as exc:
                desc = str(exc).lower()
                if "not modified" not in desc:
                    LOG.debug("TurnMonitor editMessageText failed chat=%s: %s", self.chat_id, exc)

    async def finish(self, success: bool = True, error: str = "") -> None:
        """Finalize the monitor and update message with clean summary."""
        self._finished = True
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()

        if not self.has_tools() or self.message_id is None:
            return

        text = self.render_final(success=success, error=error)
        try:
            await self.tg.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            if "not modified" not in str(exc).lower():
                LOG.warning("TurnMonitor finish edit failed chat=%s: %s", self.chat_id, exc)
