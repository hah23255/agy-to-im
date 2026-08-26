"""Media handling — photo download, file upload, inbox management."""
from __future__ import annotations

import fnmatch
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import InboxConfig, CategoryConfig
    from src.telegram import TelegramClient

MAX_PHOTO_SIZE = 10 * 1024 * 1024
MAX_FILE_SIZE = 20 * 1024 * 1024

INBOX_DIR_NAME = ".bridge-inbox"


# ── Legacy helpers (kept for backward compat during migration) ────────────

async def download_photo(tg: "TelegramClient", file_id: str) -> bytes:
    return await tg.get_file(file_id)


async def download_document(tg: "TelegramClient", file_id: str) -> bytes:
    return await tg.get_file(file_id)


def save_to_inbox(workdir: str, filename: str, data: bytes, max_total_bytes: int = 524_288_000) -> Path:
    """Save file to inbox, enforcing total size cap by purging oldest files first."""
    inbox = Path(workdir) / INBOX_DIR_NAME
    inbox.mkdir(parents=True, exist_ok=True)
    # Enforce total size cap
    files = sorted(inbox.glob("*"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while total + len(data) > max_total_bytes and files:
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink()
    ts = int(time.time())
    dest = inbox / f"{ts}_{filename}"
    dest.write_bytes(data)
    return dest


def clean_inbox(workdir: str, max_age_hours: int = 24) -> int:
    inbox = Path(workdir) / INBOX_DIR_NAME
    if not inbox.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for f in inbox.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed


def list_inbox(workdir: str, limit: int = 5) -> list[str]:
    inbox = Path(workdir) / INBOX_DIR_NAME
    if not inbox.exists():
        return []
    files = sorted(inbox.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
    result: list[str] = []
    for f in files[:limit]:
        size = f.stat().st_size
        sz = f"{size}B" if size < 1024 else f"{size//1024}K"
        result.append(f"{f.name} ({sz})")
    return result


# ── Phase 1: File Classification ──────────────────────────────────────────

def _mime_matches(actual: str, pattern: str) -> bool:
    """Match MIME types with glob-style wildcards.
    'text/plain' matches 'text/*'.
    'application/vnd.*' matches any vendor-specific MIME.
    """
    if pattern == actual:
        return True
    if pattern.endswith("/*"):
        prefix = pattern[:-1]
        return actual.startswith(prefix)
    if "*" in pattern:
        return fnmatch.fnmatch(actual, pattern)
    return False


def _classify_by_mime(mime: str, categories: dict[str, "CategoryConfig"]) -> tuple[str, "CategoryConfig"] | None:
    """Return (category_name, CategoryConfig) if mime matches a category, else None."""
    for cat_name, cat_cfg in categories.items():
        for pattern in cat_cfg.mime_types:
            if _mime_matches(mime, pattern):
                return cat_name, cat_cfg
    return None


def _classify_by_extension(filename: str, categories: dict[str, "CategoryConfig"]) -> tuple[str, "CategoryConfig"] | None:
    """Return (category_name, CategoryConfig) if extension matches, else None. Longest-match-first."""
    ext = Path(filename).suffix.lower()
    if not ext:
        return None
    for cat_name, cat_cfg in categories.items():
        if ext in cat_cfg.extensions:
            return cat_name, cat_cfg
    return None


def classify_file(
    mime: str,
    filename: str,
    file_size: int,
    categories: dict[str, "CategoryConfig"],
    default_routing: str = "block",
) -> tuple[str, str, str]:
    """Classify a file and return (category, routing, reason).
    
    Priority: MIME match → extension fallback → default routing.
    Size check applied after category match.
    """
    result = _classify_by_mime(mime, categories)
    if result is None:
        result = _classify_by_extension(filename, categories)
    if result is not None:
        cat_name, cat_cfg = result
        if file_size > cat_cfg.max_size_bytes:
            return cat_name, "block", f"exceeds {cat_cfg.max_size_bytes // 1_048_576}MB limit"
        return cat_name, cat_cfg.routing, ""
    return "unknown", default_routing, ""


# ── Phase 2: Routing Actions ──────────────────────────────────────────────

async def _safe_download(tg: "TelegramClient", file_id: str, chat_id: int, fname: str) -> bytes | None:
    """Download file with user-facing error on failure. Returns None if failed."""
    try:
        return await tg.get_file(file_id)
    except Exception as exc:
        await tg.send_message(chat_id, f"⚠️ Download failed for {fname}: {exc}")
        return None


async def _route_block(tg: "TelegramClient", chat_id: int, fname: str, category: str, reason: str = "") -> None:
    """Notify user that file was blocked."""
    msg = f"⛔ Blocked: {fname}"
    if category != "unknown":
        msg += f" ({category})"
    if reason:
        msg += f" — {reason}"
    await tg.send_message(chat_id, msg)


async def _route_save_and_prompt(
    routing: str,
    tg: "TelegramClient",
    chat_id: int,
    file_id: str,
    fname: str,
    category: str,
    workdir: str,
    inbox_cfg: "InboxConfig",
) -> str | None:
    """Execute routing action. Returns prompt fragment or None.
    
    pass  → save + include in prompt (no notification)
    warn  → save + include + warning notification  
    hold  → save + notify + text-only prompt (returns None)
    """
    data = await _safe_download(tg, file_id, chat_id, fname)
    if data is None:
        return None
    path = save_to_inbox(workdir, fname, data, inbox_cfg.max_total_bytes)
    size_kb = len(data) // 1024
    if routing == "pass":
        return f"[File: {path} — {size_kb}KB]"
    elif routing == "warn":
        await tg.send_message(chat_id, f"⚠️ Accepted with caution: {fname} ({category})")
        return f"[File ({category}): {path} — {size_kb}KB]"
    elif routing == "hold":
        await tg.send_message(chat_id, f"📎 {fname} ({size_kb}KB) saved to inbox. Not processed this turn.")
        return None
    return None


# ── Phase 3: Unified Prompt Builder ───────────────────────────────────────

async def build_media_prompt(
    msg, tg, state, cfg,
) -> str | None:
    """Build prompt from text + routed media. Returns None if all content blocked."""
    parts = [msg.text] if msg.text else []
    wd = state.chat_dir
    inbox_cfg = cfg.safety.inbox

    if msg.photo and state.photo_enabled:
        largest = max(msg.photo, key=lambda p: p.get("file_size", 0))
        fsize = largest.get("file_size", 0)
        if fsize > cfg.safety.media.max_photo_bytes:
            await tg.send_message(msg.chat_id, "📸 Photo too large")
        else:
            data = await _safe_download(tg, largest["file_id"], msg.chat_id, "photo.jpg")
            if data:
                path = save_to_inbox(wd, f"photo_{largest['file_id'][:12]}.jpg", data, inbox_cfg.max_total_bytes)
                parts.append(f"[Photo: {path} — {len(data)//1024}KB]")

    if msg.document:
        doc = msg.document
        fname = doc.get("file_name", "unknown")
        mime = doc.get("mime_type", "")
        fsize = doc.get("file_size", 0)

        category, routing, reason = classify_file(
            mime, fname, fsize,
            cfg.safety.media.categories,
            cfg.safety.media.default_routing,
        )

        if routing == "block":
            await _route_block(tg, msg.chat_id, fname, category, reason)
        elif routing in ("pass", "warn", "hold"):
            prompt = await _route_save_and_prompt(
                routing, tg, msg.chat_id, doc["file_id"],
                fname, category, wd, inbox_cfg,
            )
            if prompt:
                parts.append(prompt)

    return " ".join(parts) if parts else None
