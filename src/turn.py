"""Turn execution — agy print-mode invocation."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from src.agy_runner import AgyResult, run_agy
from src.monitor import TurnMonitor

if TYPE_CHECKING:
    from src.config import Config
    from src.daemon import _TelegramLike
    from src.state import ChatState
    from src.telegram import InboundMessage

LOG = logging.getLogger("antigravity_telegram_bridge")
AGY_TIMEOUT_S = 900.0


class TurnOutput(tuple):
    """2-tuple (text, code) with extra attributes."""
    text: str
    code: int
    has_tools: bool
    response_mid: int | None
    usage: dict[str, int] | None

    def __new__(cls, text: str, code: int, has_tools: bool = False,
                response_mid: int | None = None, usage: dict[str, int] | None = None):
        obj = super().__new__(cls, (text, code))
        obj.has_tools = has_tools
        obj.response_mid = response_mid
        obj.usage = usage
        return obj


async def _compile_memory_layer(chat_dir: str) -> None:
    """Run graphify extract and export in the background to build the OKF memory vault."""
    try:
        import shutil
        from pathlib import Path
        graphify_bin = shutil.which("graphify") or str(Path.home() / ".local" / "bin" / "graphify")
        if not shutil.which("graphify") and not Path(graphify_bin).is_file():
            return
        # Run graphify extract . (AST + semantic codebase index)
        proc1 = await asyncio.create_subprocess_exec(
            graphify_bin, "extract", ".",
            cwd=chat_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc1.wait()
        
        # Run graphify export okf --dir concepts (Google OKF v0.1 bundle)
        proc2 = await asyncio.create_subprocess_exec(
            graphify_bin, "export", "okf", "--dir", "concepts",
            cwd=chat_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc2.wait()
        LOG.info("OKF-Graphify memory layer successfully updated for %s", chat_dir)
    except Exception as exc:
        LOG.warning("Failed to update OKF-Graphify memory layer: %s", exc)

async def execute_agy(
    tg: "_TelegramLike", chat_id: int, msg: "InboundMessage",
    cs: "ChatState", cfg: "Config", agy_path: str,
    placeholder_mid: int | None = None,
) -> TurnOutput:
    """Run one agy turn with typing heartbeat and real-time monitoring."""
    hb_stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat(tg, chat_id, hb_stop))
    turn_start = time.perf_counter()
    monitor = TurnMonitor(tg, chat_id, placeholder_mid, cs.chat_dir) if placeholder_mid else None
    result: AgyResult | None = None
    try:
        from src.style import apply_style_hint
        result = await run_agy(
            prompt=apply_style_hint(msg.text, getattr(cs, "style_hint", True)),
            chat_dir=cs.chat_dir,
            has_session=cs.has_session,
            model=cs.model or cfg.agy.model,
            mode=cs.mode or cfg.agy.mode,
            effort=getattr(cs, "effort", ""),
            agy_path=agy_path,
            timeout=AGY_TIMEOUT_S,
            on_event=monitor.on_stream_event if monitor else None,
        )
        if result.exit_code == 0:
            asyncio.create_task(_compile_memory_layer(cs.chat_dir))
    finally:
        hb_stop.set()
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass
        if monitor and monitor.has_tools() and result is not None:
            success = (result.exit_code == 0)
            err_msg = ""
            if result.exit_code == 124:
                err_msg = "Waktu tunggu habis (timeout)"
            elif result.exit_code in (-9, -15, 137, 143):
                err_msg = "Dibatalkan (/stop)"
            elif result.exit_code != 0:
                err_msg = f"Error (exit {result.exit_code})"
            try:
                await monitor.finish(success=success, error=err_msg)
            except Exception as exc:
                LOG.warning("Failed to finalize monitor: %s", exc)

    elapsed = int((time.perf_counter() - turn_start) * 1000)
    has_tools = monitor.has_tools() if monitor else False
    if result is None:
        return TurnOutput("", 1, has_tools)

    if result.exit_code != 0:
        LOG.error(
            "turn failed chat=%d cwd=%s exit=%d ms=%d stderr=%s",
            chat_id, cs.chat_dir, result.exit_code, elapsed, result.stderr.strip(),
        )
    else:
        LOG.info(
            "turn chat=%d cwd=%s exit=%d ms=%d reply_len=%d tools=%s",
            chat_id, cs.chat_dir, result.exit_code, elapsed, len(result.text or ""), has_tools,
        )
    return TurnOutput(result.text or "", result.exit_code, has_tools, usage=result.usage)


async def _heartbeat(
    tg: "_TelegramLike", chat_id: int, stop_event: asyncio.Event
) -> None:
    """Typing indicator refresh every 4 s."""
    try:
        while not stop_event.is_set():
            try:
                await tg.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        return
