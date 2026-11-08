"""Long-running daemon: poll Telegram, route through agy, reply."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from src.agy_runner import AgyResult, abort_turn
from src.commands import BOT_COMMANDS, BridgeReply, handle_callback, handle_text_command
from src.config import Config, load_config
from src.health import memory_healthy, record_error, record_turn, start_server
from src.media import build_media_prompt, clean_inbox
from src.okf_memory import attach_memory
from src.queue import QUEUE as _QUEUE, TurnQueue
from src.state import ChatState, State, load_state, save_state
from src.telegram import CallbackQuery, InboundMessage, TelegramClient, is_authorized
from src.turn import execute_agy
from src.webhook import drain_webhook_updates, start_webhook_server

LOG = logging.getLogger("antigravity_telegram_bridge")
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.json"
DEFAULT_STATE_PATH = Path.home() / ".antigravity" / "bridge" / "state.json"
DEFAULT_CHATS_ROOT = Path.home() / ".antigravity" / "bridge" / "chats"
DEFAULT_AGY_BIN = "agy"
LONG_POLL_TIMEOUT = 30
AGY_TIMEOUT_S = 900.0
DRAIN_TIMEOUT_S = 30.0
# Bounded wait for the worker to finish cleanup after cancellation, so a
# worker wedged mid-cleanup cannot hang daemon shutdown.
_WORKER_SHUTDOWN_TIMEOUT_S = 10.0


class _TelegramLike(Protocol):
    async def __aenter__(self) -> "_TelegramLike": ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def get_me(self) -> dict[str, Any]:
        """Fetch bot metadata via getMe."""
        ...
    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        """Long-poll Telegram for new updates."""
        ...
    async def send_message(
        self, chat_id: int, text: str, *, keyboard: Any | None = None
    ) -> int | None:
        """Send a text message; returns the first message id."""
        ...
    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, keyboard: Any | None = None
    ) -> None:
        """Edit an existing message's text/keyboard in place."""
        ...
    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a previously sent bot message."""
        ...
    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
        """Acknowledge a callback query with an optional toast."""
        ...
    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Send a chat action such as the typing indicator."""
        ...
    async def set_my_commands(
        self, commands: list[dict[str, str]], *, scope: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Register the bot command menu."""
        ...
    async def send_document(
        self, chat_id: int, file_path: str | Path, *, caption: str = ""
    ) -> int | None:
        """Upload a document to the chat; returns the message id."""
        ...


RunAgyFunc = Callable[..., Awaitable[AgyResult]]


@dataclass(frozen=True)
class DaemonInfo:
    bot_username: str
    started_at: float
    agy_version: str


def _resolve_agy_path() -> str:
    """Resolve the agy binary path from env or default."""
    return os.environ.get("AGY_BIN") or DEFAULT_AGY_BIN


def _resolve_chats_root(cfg: Config) -> Path:
    """Resolve the chats root directory from config."""
    if cfg.agy.chats_root:
        return Path(cfg.agy.chats_root).expanduser()
    return DEFAULT_CHATS_ROOT


def _detect_agy_version(agy_path: str) -> str:
    """Run agy --version and return its output (or 'unknown')."""
    try:
        out = subprocess.run(
            [agy_path, "--version"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception as err:
        LOG.warning("agy --version failed: %s", err)
    return "unknown"


def _ensure_chat_state(state: State, chat_id: int, chats_root: Path) -> ChatState:
    """Return the ChatState for a chat, creating its directory on first use."""
    cs = state.chats.get(chat_id)
    if cs is not None:
        Path(cs.chat_dir).mkdir(parents=True, exist_ok=True)
        return cs
    chat_dir = (chats_root / str(chat_id)).resolve()
    chat_dir.mkdir(parents=True, exist_ok=True)
    cs = ChatState(chat_dir=str(chat_dir))
    state.chats[chat_id] = cs
    return cs


def _record_usage(cs: ChatState, usage: dict[str, Any]) -> None:
    """Accumulate token usage from a stream-json result event into chat state."""
    total = int(usage.get("total_tokens", 0) or 0)
    today = time.strftime("%Y-%m-%d")
    if cs.usage_today_date != today:
        cs.usage_today_date = today
        cs.usage_today_tokens = 0
        cs.usage_today_turns = 0
    cs.usage_last_in = int(usage.get("input_tokens", 0) or 0)
    cs.usage_last_out = int(usage.get("output_tokens", 0) or 0)
    cs.usage_last_total = total
    cs.usage_today_tokens += total
    cs.usage_today_turns += 1
    cs.usage_total_tokens += total
    cs.usage_total_turns += 1


def _format_timeout_reply() -> str:
    """Build the user-facing reply for a timed-out turn."""
    minutes = AGY_TIMEOUT_S / 60
    return (
        f"⏱️ agy turn cut off at {minutes:.0f} min.\n\n"
        "• Send a shorter follow-up\n"
        "• Split into smaller steps\n"
        "• /reset to start fresh"
    )


OUTBOUND_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".zip", ".tar.gz", ".png", ".jpg", ".jpeg", ".svg"}


_SENT_DOCUMENTS: dict[int, set[tuple[str, float, int]]] = {}


def clear_sent_documents(chat_id: int | None = None) -> None:
    """Clear sent documents cache (useful for testing or session reset)."""
    if chat_id is not None:
        _SENT_DOCUMENTS.pop(chat_id, None)
    else:
        _SENT_DOCUMENTS.clear()


async def _detect_and_send_output_files(
    text: str, chat_dir: str, tg: _TelegramLike, chat_id: int,
    initial_files: set[Path] | None = None,
) -> None:
    """Detect and send any newly created or referenced files (e.g. PDF/docs)."""
    cd = Path(chat_dir).resolve()
    candidates: list[Path] = []

    import re
    # 1. Match file:// URLs in text
    for match in re.finditer(r"file://([^\s\)\`\"']+)", text):
        p = Path(match.group(1)).resolve()
        if p.is_file() and p.suffix.lower() in OUTBOUND_EXTS:
            candidates.append(p)

    # 2. Match backticked file names or paths in text
    for match in re.finditer(r"`([^`]+)`", text):
        raw_p = match.group(1).strip()
        p = Path(raw_p)
        if not p.is_absolute():
            p = (cd / p).resolve()
        if p.is_file() and p.suffix.lower() in OUTBOUND_EXTS:
            candidates.append(p)

    # 3. Check newly created files in chat_dir
    if initial_files is not None and cd.is_dir():
        try:
            for item in cd.iterdir():
                if item.is_file() and item not in initial_files:
                    if item.suffix.lower() in OUTBOUND_EXTS and not item.name.startswith("."):
                        candidates.append(item.resolve())
        except Exception:
            pass

    # Deduplicate and send
    seen: set[Path] = set()
    for doc in candidates:
        if doc in seen or not doc.is_file():
            continue
        seen.add(doc)
        # Avoid sending internal files or inbox files
        try:
            rel_parts = doc.relative_to(cd).parts[:-1]
        except Exception:
            rel_parts = doc.parts[:-1]
        if any(part.startswith(".") or part == "okf_memory" for part in rel_parts):
            continue
        try:
            stat = doc.stat()
            sz_kb = stat.st_size // 1024
            if sz_kb > 50 * 1024:  # Telegram 50MB limit
                continue

            file_sig = (str(doc.resolve()), stat.st_mtime, stat.st_size)
            if chat_id in _SENT_DOCUMENTS and file_sig in _SENT_DOCUMENTS[chat_id]:
                LOG.info("skipping already sent document chat=%d file=%s", chat_id, doc.name)
                continue

            await tg.send_document(chat_id, doc, caption=f"📄 {doc.name}")
            _SENT_DOCUMENTS.setdefault(chat_id, set()).add(file_sig)
            LOG.info("sent document chat=%d file=%s (%d KB)", chat_id, doc.name, sz_kb)
        except Exception as exc:
            LOG.warning("failed to send document chat=%d file=%s: %s", chat_id, doc, exc)


def _group_inbound_messages(messages: list[InboundMessage]) -> list[InboundMessage]:
    """Group consecutive messages from the same chat, e.g. media albums or multi-file uploads."""
    if not messages:
        return []

    grouped: list[InboundMessage] = []
    current_batch: list[InboundMessage] = []

    def flush_batch() -> None:
        """Merge the current message batch into a single InboundMessage."""
        nonlocal current_batch
        if not current_batch:
            return
        if len(current_batch) == 1:
            grouped.append(current_batch[0])
        else:
            texts: list[str] = []
            seen_texts: set[str] = set()
            for m in current_batch:
                t = (m.text or "").strip()
                if t and t not in seen_texts:
                    seen_texts.add(t)
                    texts.append(t)
            combined_text = "\n".join(texts)

            docs: list[dict[str, Any]] = []
            for m in current_batch:
                if m.documents:
                    docs.extend(m.documents)
                elif m.document:
                    docs.append(m.document)

            all_photos: list[list[dict[str, Any]]] = []
            for m in current_batch:
                if m.photos:
                    all_photos.extend(m.photos)
                elif m.photo:
                    all_photos.append(m.photo)

            mgid = next((m.media_group_id for m in current_batch if m.media_group_id), None)
            merged = InboundMessage(
                update_id=max(m.update_id for m in current_batch),
                chat_id=current_batch[0].chat_id,
                user_id=current_batch[0].user_id,
                text=combined_text,
                photo=all_photos[-1] if all_photos else None,
                document=docs[0] if docs else None,
                media_group_id=mgid,
                documents=docs if docs else None,
                photos=all_photos if all_photos else None,
            )
            grouped.append(merged)
        current_batch = []

    for msg in messages:
        if msg.text.startswith("/"):
            flush_batch()
            grouped.append(msg)
            continue

        if not current_batch:
            current_batch.append(msg)
            continue

        prev = current_batch[-1]
        same_chat = (msg.chat_id == prev.chat_id and msg.user_id == prev.user_id)
        same_mgid = bool(msg.media_group_id and prev.media_group_id and msg.media_group_id == prev.media_group_id)
        has_media = bool(
            msg.document or msg.documents or msg.photo or msg.photos
            or prev.document or prev.documents or prev.photo or prev.photos
        )

        if same_chat and (same_mgid or has_media):
            current_batch.append(msg)
        else:
            flush_batch()
            current_batch.append(msg)

    flush_batch()
    return grouped


async def _do_turn(
    msg: InboundMessage, tg: _TelegramLike, cs: ChatState,
    cfg: Config, agy_path: str,
) -> None:
    """Execute one agy turn for a message and deliver the reply to Telegram."""
    try:
        await tg.send_chat_action(msg.chat_id, "typing")
    except Exception:
        pass

    # Instant feedback: Send progressive status placeholder within <300ms,
    # BEFORE any media download so the user sees activity immediately.
    placeholder_mid: int | None = None
    try:
        placeholder_mid = await tg.send_message(msg.chat_id, "⏳ Menghubungkan ke agy...")
    except Exception as err:
        LOG.warning("Failed to send status placeholder: %s", err)

    prompt = await build_media_prompt(msg, tg, cs, cfg)
    if prompt is None:
        # All content was blocked (or nothing routable) — remove the stray
        # placeholder instead of leaving it dangling in the chat.
        if placeholder_mid is not None:
            try:
                await tg.delete_message(msg.chat_id, placeholder_mid)
            except Exception as err:
                LOG.warning("deleteMessage failed chat=%s: %s", msg.chat_id, err)
        return

    initial_files = set(Path(cs.chat_dir).iterdir()) if Path(cs.chat_dir).is_dir() else set()
    effective_msg = InboundMessage(
        update_id=msg.update_id,
        chat_id=msg.chat_id,
        user_id=msg.user_id,
        text=prompt,
        photo=msg.photo,
        document=msg.document,
    )
    try:
        turn_res = await execute_agy(tg, msg.chat_id, effective_msg, cs, cfg, agy_path, placeholder_mid=placeholder_mid)
    except TypeError:
        turn_res = await execute_agy(tg, msg.chat_id, effective_msg, cs, cfg, agy_path)
    text, code = turn_res[0], turn_res[1]
    has_tools = getattr(turn_res, "has_tools", False)
    if code == 124:
        record_error()
        reply = _format_timeout_reply()
    elif code != 0:
        record_error()
        reply = f"⚠️ agy error (exit {code})"
    else:
        record_turn()
        if not cs.has_session:
            cs.has_session = True
        cs.turn_count += 1
        reply = text or "(empty reply)"
    # Token accounting: the stream-json result event reports usage when the
    # turn completed successfully.
    usage = getattr(turn_res, "usage", None)
    if usage and usage.get("total_tokens"):
        try:
            _record_usage(cs, usage)
        except Exception as exc:
            LOG.warning("usage accounting failed chat=%d: %s", msg.chat_id, exc)
    # Post-turn maintenance: update OKF memory layer, clean inbox
    try:
        attach_memory(cs.chat_dir, None, bridge_version="0.2.0")
        clean_inbox(cs.chat_dir)
    except Exception:
        pass

    edited = False
    if not has_tools and placeholder_mid is not None and len(reply) <= 4000:
        try:
            await tg.edit_message_text(msg.chat_id, placeholder_mid, reply)
            edited = True
        except Exception as err:
            LOG.warning("editMessageText failed chat=%s: %s, falling back to sendMessage", msg.chat_id, err)

    if not edited:
        try:
            await tg.send_message(msg.chat_id, reply)
        except Exception as err:
            LOG.error("sendMessage failed chat=%s: %s", msg.chat_id, err)

    if code == 0:
        try:
            await _detect_and_send_output_files(reply, cs.chat_dir, tg, msg.chat_id, initial_files)
        except Exception as err:
            LOG.error("sendDocument failed chat=%s: %s", msg.chat_id, err)


async def _process_text(
    msg: InboundMessage, tg: _TelegramLike, state: State,
    state_path: Path, chats_root: Path, cfg: Config,
    agy_path: str, info: DaemonInfo,
) -> None:
    """Fast path: auth, slash commands, queueing. Never runs a turn inline.

    Turn execution lives in _turn_worker so the polling loop keeps fetching
    updates while agy works.
    """
    if not is_authorized(msg, cfg.telegram):
        LOG.info("drop unauth user=%s", msg.user_id)
        return

    cs = _ensure_chat_state(state, msg.chat_id, chats_root)
    save_state(state_path, state)

    reply = await handle_text_command(msg, cs, cfg)
    if reply is not None:
        save_state(state_path, state)
        try:
            await tg.send_message(msg.chat_id, reply.text, keyboard=reply.keyboard)
            if reply.document_path and reply.document_path.is_file():
                await tg.send_document(msg.chat_id, reply.document_path, caption=f"📄 {reply.document_path.name}")
        except Exception as err:
            LOG.error("sendMessage failed chat=%s: %s", msg.chat_id, err)
        return

    # When bot is inactive, silently ignore non-command messages
    if not cs.active:
        LOG.info("drop inactive chat=%s user=%s", msg.chat_id, msg.user_id)
        return

    status = _QUEUE.submit(msg)  # sync — never blocks the polling loop
    if status is not None:
        try:
            await tg.send_message(msg.chat_id, status)
        except Exception as err:
            LOG.error("sendMessage failed chat=%s: %s", msg.chat_id, err)


async def _turn_worker(
    tg: _TelegramLike, state: State, state_path: Path,
    chats_root: Path, cfg: Config, agy_path: str,
) -> None:
    """Single consumer executing one agy turn at a time, FIFO order.

    The polling loop stays free to fetch updates while turns run here.
    """
    while True:
        msg = await _QUEUE.get()
        cs: ChatState | None = None
        try:
            cs = _ensure_chat_state(state, msg.chat_id, chats_root)
            await _do_turn(msg, tg, cs, cfg, agy_path)
            save_state(state_path, state)
        except asyncio.CancelledError:
            # Shutdown mid-turn: kill the agy subprocess so it cannot outlive us.
            if cs is not None:
                abort_turn(cs.chat_dir)
            raise
        except Exception:
            LOG.exception("turn worker failed chat=%d", msg.chat_id)
        finally:
            _QUEUE.task_done()
            _QUEUE.complete()


async def _process_callback(
    cq: CallbackQuery, tg: _TelegramLike, state: State,
    state_path: Path, chats_root: Path, cfg: Config, info: DaemonInfo,
) -> None:
    """Handle an inline-keyboard callback query."""
    if not is_authorized(
        InboundMessage(update_id=cq.update_id, chat_id=cq.chat_id,
                       user_id=cq.user_id, text=""),
        cfg.telegram,
    ):
        LOG.info("drop unauth callback user=%s", cq.user_id)
        await tg.answer_callback_query(cq.callback_query_id, text="Not authorized")
        return

    cs = _ensure_chat_state(state, cq.chat_id, chats_root)
    reply = handle_callback(cq, cs, cfg)
    save_state(state_path, state)

    try:
        await tg.answer_callback_query(cq.callback_query_id, text=reply.toast)
    except Exception:
        pass
    try:
        await tg.edit_message_text(
            cq.chat_id, cq.message_id, reply.text, keyboard=reply.keyboard
        )
    except Exception as err:
        LOG.warning("editMessageText failed (sending fresh): %s", err)
        try:
            await tg.send_message(cq.chat_id, reply.text, keyboard=reply.keyboard)
        except Exception as err2:
            LOG.error("fallback sendMessage failed: %s", err2)


async def _fetch_updates(
    tg: _TelegramLike, offset: int, poll_timeout: int = LONG_POLL_TIMEOUT,
) -> list[dict[str, Any]]:
    """Fetch Telegram updates with a bounded long-poll."""
    try:
        return await asyncio.wait_for(
            tg.get_updates(offset=offset, timeout=poll_timeout),
            timeout=poll_timeout + 5,
        )
    except asyncio.TimeoutError:
        return []
    except Exception as err:
        LOG.warning("getUpdates failed: %s", err)
        await asyncio.sleep(2)
        return []


async def _register_bot_commands(tg: _TelegramLike) -> None:
    """Register the bot's slash-command menu with Telegram."""
    try:
        res = await tg.set_my_commands(BOT_COMMANDS)
        if isinstance(res, dict) and res.get("ok"):
            LOG.info("Registered %d bot commands with Telegram", len(BOT_COMMANDS))
        else:
            LOG.warning("Failed to register bot commands with Telegram: %s", res)
    except Exception as err:
        LOG.warning("Error registering bot commands: %s", err)


async def run(
    *, cfg: Config, state_path: Path, chats_root: Path,
    tg: _TelegramLike, agy_path: str, info: DaemonInfo,
    stop_event: asyncio.Event,
) -> None:
    """Main polling loop: fetch updates, dispatch fast-path handling, feed the turn worker; drains and tears the worker down on stop."""
    _QUEUE.max_per_user = cfg.safety.queue.max_per_user
    _QUEUE.cooldown_seconds = cfg.safety.queue.cooldown_seconds
    _QUEUE.max_depth = cfg.safety.queue.max_depth
    _QUEUE.reset()
    chats_root.mkdir(parents=True, exist_ok=True)
    allowed_dirs = [Path.home()]
    if cfg.agy.default_workdir:
        allowed_dirs.append(Path(cfg.agy.default_workdir).expanduser())
    state = load_state(state_path, chats_root, extra_allowed_dirs=allowed_dirs)
    
    # User requested: Bot should not auto-reply to offline message dumps on startup.
    # It should wake up ONLY when /start is explicitly sent.
    for cs in state.chats.values():
        cs.active = False
        
    tick = 0
    async with tg:
        await _register_bot_commands(tg)
        # Turns run in a dedicated worker so the polling loop below never
        # blocks on agy (which can run for up to AGY_TIMEOUT_S).
        worker = asyncio.create_task(
            _turn_worker(tg, state, state_path, chats_root, cfg, agy_path)
        )
        while not stop_event.is_set():
            tick += 1
            # Health gate: check memory every ~30 loop iterations (~30s at idle,
            # faster when processing bursts). Self-terminate if over limit so
            # systemd Restart=on-failure brings up a clean replacement.
            if tick % 30 == 0 and not memory_healthy():
                LOG.critical("memory limit exceeded — triggering self-restart")
                stop_event.set()
                break

            updates = await _fetch_updates(tg, state.last_update_id + 1)
            wh_updates = drain_webhook_updates()
            updates.extend(wh_updates)

            # If any update in the batch has media/attachments, debounce briefly
            # to collect any other attachments arriving in the same burst/album
            has_media = any(
                (upd.get("message") or {}).get("document")
                or (upd.get("message") or {}).get("photo")
                or (upd.get("message") or {}).get("media_group_id")
                for upd in updates
            )
            if has_media:
                try:
                    await asyncio.sleep(0.3)
                    max_uid = max((int(u.get("update_id", 0)) for u in updates), default=state.last_update_id)
                    more_updates = await _fetch_updates(tg, max_uid + 1, poll_timeout=0)
                    if more_updates:
                        updates.extend(more_updates)
                except Exception:
                    pass

            inbound_msgs: list[InboundMessage] = []
            for upd in updates:
                state.last_update_id = max(state.last_update_id, int(upd.get("update_id", 0)))

                cq = parse_callback_for_daemon(upd)
                if cq is not None:
                    await _process_callback(
                        cq, tg, state, state_path, chats_root, cfg, info,
                    )
                    continue

                msg = parse_update_for_daemon(upd)
                if msg is not None:
                    inbound_msgs.append(msg)

            grouped_msgs = _group_inbound_messages(inbound_msgs)
            for msg in grouped_msgs:
                allowed, wait = _QUEUE.check_ratelimit(msg.user_id)
                if not allowed:
                    await tg.send_message(msg.chat_id, f"⏳ Rate limit. Wait {wait}s.")
                    continue
                await _process_text(
                    msg, tg, state, state_path, chats_root,
                    cfg, agy_path, info,
                )

            if not updates:
                await asyncio.sleep(0)
            save_state(state_path, state)

        # Graceful drain only after a normal stop; any exit path (exception or
        # cancellation) must still tear the worker down via the finally below.
        try:
            await asyncio.wait_for(_QUEUE.join(), timeout=DRAIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            pass
        finally:
            worker.cancel()
            try:
                # shield() so the timeout mechanism itself never waits for the
                # worker to honour cancellation — on timeout we proceed and
                # leave the worker's cleanup running in the background.
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(worker, return_exceptions=True)),
                    timeout=_WORKER_SHUTDOWN_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                LOG.warning(
                    "turn worker did not exit within %ss after cancellation",
                    _WORKER_SHUTDOWN_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                raise


# Aliases to avoid name collisions in the loop above
from src.telegram import parse_callback_query as parse_callback_for_daemon
from src.telegram import parse_update as parse_update_for_daemon


async def _detect_bot_username(cfg: Config) -> str:
    """Fetch the bot's @username via getMe."""
    try:
        async with TelegramClient(cfg.telegram.bot_token) as tg:
            me = await tg.get_me()
            uname = me.get("username")
            return f"@{uname}" if isinstance(uname, str) and uname else "?"
    except Exception as err:
        LOG.warning("getMe failed: %s", err)
    return "?"


def main() -> None:
    """Entry point: load config, start the health server, and run the daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    cfg_path = Path(os.environ.get("AGY_BRIDGE_CONFIG") or DEFAULT_CONFIG_PATH)
    state_path = Path(os.environ.get("AGY_BRIDGE_STATE") or DEFAULT_STATE_PATH)
    cfg = load_config(cfg_path)
    chats_root = _resolve_chats_root(cfg)
    agy_path = _resolve_agy_path()
    stop = asyncio.Event()

    async def _go() -> None:
        """Wire signal handlers and run the daemon under the health server."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        health_srv = await start_server()

        wh_task: asyncio.Task | None = None
        wh_secret = ""
        wh_url = os.environ.get("CTI_WEBHOOK_URL", "")
        wh_port = int(os.environ.get("CTI_WEBHOOK_PORT") or "8080")

        tg = TelegramClient(cfg.telegram.bot_token)
        try:
            bot_username = await _detect_bot_username(cfg)
            info = DaemonInfo(
                bot_username=bot_username,
                started_at=time.time(),
                agy_version=_detect_agy_version(agy_path),
            )
            LOG.info(
                "bridge starting bot=%s agy=%s chats_root=%s mode=%s model=%s",
                info.bot_username, info.agy_version, chats_root,
                cfg.agy.mode, cfg.agy.model or "(default)",
            )

            if wh_url:
                wh_secret = cfg.telegram.bot_token[:20]
                wh_res = await tg.set_webhook(wh_url, wh_secret)
                LOG.info("webhook setup: %s", wh_res.get("description") if wh_res.get("ok") else wh_res)
                wh_task = asyncio.create_task(start_webhook_server(wh_port, tg, wh_secret))

            await run(
                cfg=cfg, state_path=state_path, chats_root=chats_root,
                tg=tg, agy_path=agy_path, info=info, stop_event=stop,
            )
        finally:
            if wh_task is not None:
                wh_task.cancel()
                try:
                    await wh_task
                except Exception:
                    pass
            try:
                await tg.delete_webhook()
            except Exception:
                pass
            health_srv.close()
            await health_srv.wait_closed()

    asyncio.run(_go())


if __name__ == "__main__":
    main()
