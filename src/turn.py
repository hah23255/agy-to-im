"""Turn execution — agy print-mode invocation."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from src.agy_runner import run_agy

if TYPE_CHECKING:
    from src.config import Config
    from src.daemon import _TelegramLike
    from src.state import ChatState
    from src.telegram import InboundMessage

LOG = logging.getLogger("antigravity_telegram_bridge")
AGY_TIMEOUT_S = 900.0


async def _compile_memory_layer(chat_dir: str) -> None:
    """Run graphify extract and export in the background to build the OKF memory vault."""
    try:
        import shutil
        graphify_bin = shutil.which("graphify") or "/home/i/.local/bin/graphify"
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
) -> tuple[str, int]:
    """Run one agy turn with typing heartbeat."""
    hb_stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat(tg, chat_id, hb_stop))
    turn_start = time.perf_counter()
    try:
        result = await run_agy(
            prompt=msg.text,
            chat_dir=cs.chat_dir,
            has_session=cs.has_session,
            model=cs.model or cfg.agy.model,
            mode=cs.mode or cfg.agy.mode,
            agy_path=agy_path,
            timeout=AGY_TIMEOUT_S,
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
    elapsed = int((time.perf_counter() - turn_start) * 1000)
    if result.exit_code != 0:
        LOG.error(
            "turn failed chat=%d cwd=%s exit=%d ms=%d stderr=%s",
            chat_id, cs.chat_dir, result.exit_code, elapsed, result.stderr.strip(),
        )
    else:
        LOG.info(
            "turn chat=%d cwd=%s exit=%d ms=%d reply_len=%d",
            chat_id, cs.chat_dir, result.exit_code, elapsed, len(result.text or ""),
        )
    return result.text or "", result.exit_code


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
