"""Telegram glue: pure helpers + async HTTP client with media + webhook support."""
from __future__ import annotations

from dataclasses import dataclass
import html
from pathlib import Path
import re
from typing import Any
import logging

from src.config import TelegramConfig

LOG = logging.getLogger(__name__)

TAG_RE = re.compile(r"<\/?([a-zA-Z0-9]+)(\s+[^>]*)?>")

def html_to_plain(text: str) -> str:
    """Strip HTML tags and unescape entities for raw text fallback."""
    if not text:
        return ""
    no_tags = TAG_RE.sub("", text)
    return html.unescape(no_tags)


def balance_html_chunks(chunks: list[str]) -> list[str]:
    """Ensure HTML tags that open in one chunk are closed before boundary and reopened in next."""
    balanced: list[str] = []
    stack: list[tuple[str, str]] = []  # (tag_name, full_tag_text)
    for chunk in chunks:
        prefix = "".join(f"<{full}>" for _, full in stack)
        text = prefix + chunk
        curr_stack = list(stack)
        for m in TAG_RE.finditer(chunk):
            tag = m.group(1).lower()
            is_closing = m.group(0).startswith("</")
            if is_closing:
                if curr_stack and curr_stack[-1][0] == tag:
                    curr_stack.pop()
                else:
                    for i in reversed(range(len(curr_stack))):
                        if curr_stack[i][0] == tag:
                            curr_stack.pop(i)
                            break
            else:
                full_tag = m.group(0)[1:-1]
                curr_stack.append((tag, full_tag))
        suffix = "".join(f"</{tag}>" for tag, _ in reversed(curr_stack))
        balanced.append(text + suffix)
        stack = curr_stack
    return balanced


def markdown_to_telegram_html(text: str) -> str:
    """Convert standard Markdown from LLMs into clean, valid Telegram HTML."""
    if not text:
        return ""

    if text.count("```") % 2 != 0:
        if not text.endswith("\n"):
            text += "\n"
        text += "```"

    # 1. Protect code blocks
    code_blocks: list[str] = []
    def _save_code_block(m: re.Match[str]) -> str:
        """Replace fenced code blocks with placeholders before HTML conversion."""
        lang = m.group(1) or ""
        code = m.group(2)
        idx = len(code_blocks)
        esc_code = html.escape(code.rstrip())
        if lang:
            tag = f'<pre><code class="language-{html.escape(lang)}">{esc_code}</code></pre>'
            if lang.lower() == "mermaid":
                tag = f"📊 <i>Diagram (mermaid)</i>\n{tag}"
        else:
            tag = f"<pre>{esc_code}</pre>"
        code_blocks.append(tag)
        return f"\x00CODEBLOCK_{idx}\x00"

    text = re.sub(r"```([a-zA-Z0-9_-]*)\n?(.*?)```", _save_code_block, text, flags=re.DOTALL)

    # 2. Protect inline code
    inline_codes: list[str] = []
    def _save_inline_code(m: re.Match[str]) -> str:
        """Replace inline code spans with placeholders before HTML conversion."""
        code = m.group(1)
        idx = len(inline_codes)
        tag = f"<code>{html.escape(code)}</code>"
        inline_codes.append(tag)
        return f"\x00INLINECODE_{idx}\x00"

    text = re.sub(r"`([^`]+)`", _save_inline_code, text)

    # 3. Protect existing valid Telegram HTML tags
    tg_tags: list[str] = []
    def _save_tg_tag(m: re.Match[str]) -> str:
        """Protect Telegram entity tags (e.g. <tg-spoiler>) from escaping."""
        idx = len(tg_tags)
        tg_tags.append(m.group(0))
        return f"\x00TGTAG_{idx}\x00"

    text = _HTML_TAG_RE.sub(_save_tg_tag, text)

    # 4. Escape HTML special characters for the rest of the text
    text = html.escape(text, quote=False)

    # 5. Handle horizontal rules (---, ***, ___)
    text = re.sub(r"^(\s*[-*_]\s*){3,}$", "────────────────", text, flags=re.MULTILINE)

    # 6. Handle headers (# Title, ## Title, ### Title)
    def _header_sub(m: re.Match[str]) -> str:
        """Convert markdown headers to bold Telegram-safe HTML."""
        level = len(m.group(1))
        title = m.group(2).strip()
        title = title.replace("**", "").replace("__", "").strip(" *_")
        # Add icon only if it doesn't already start with an emoji
        if not title or ord(title[0]) < 127:
            icon = "🔹 " if level > 2 else "📌 "
        else:
            icon = ""
        return f"{icon}<b>{title}</b>"

    text = re.sub(r"^(#{1,6})\s+(.*)$", _header_sub, text, flags=re.MULTILINE)

    # 6.5. Handle tables (must be done before inline formatting)
    def _render_table(m: re.Match[str]) -> str:
        """Convert a markdown table into a monospaced Telegram-safe block."""
        lines = m.group(0).strip().splitlines()
        if len(lines) < 3: return m.group(0)
        # Parse rows
        rows = []
        for line in lines:
            line = line.strip()
            if not line: continue
            if line.startswith("|"): line = line[1:]
            if line.endswith("|"): line = line[:-1]
            rows.append([cell.strip() for cell in line.split("|")])
        if not rows: return m.group(0)
        
        headers = rows[0]
        # Ignore separator row (index 1)
        data_rows = rows[2:] if len(rows) > 1 and "---" in "".join(rows[1]) else rows[1:]
        
        # Check if narrow
        is_narrow = all(sum(len(c) for c in row) <= 34 for row in rows)
        if is_narrow:
            res = ["<pre>"]
            col_widths = [max(len(row[i]) if i < len(row) else 0 for row in rows) for i in range(len(headers))]
            for r_idx, row in enumerate([headers] + data_rows):
                padded = []
                for i, cell in enumerate(row):
                    w = col_widths[i] if i < len(col_widths) else len(cell)
                    padded.append(cell.ljust(w))
                res.append("   ".join(padded))
                if r_idx == 0:
                    res.append("─" * sum(col_widths) + "─" * (len(col_widths) * 3 - 1))
            res.append("</pre>")
            return "\n".join(res)
        
        # Wide tables become cards
        cards = []
        for row in data_rows:
            card = []
            for i, cell in enumerate(row):
                header = headers[i] if i < len(headers) else ""
                cell = cell.replace("&lt;br&gt;", "; ").replace("&lt;br/&gt;", "; ").replace("<br>", "; ")
                if i == 0:
                    clean_cell = cell.replace("**", "").replace("__", "").strip(" *_")
                    card.append(f"▸ <b>{clean_cell}</b>")
                else:
                    if header:
                        card.append(f"{header}: {cell}")
                    else:
                        card.append(cell)
            cards.append("\n".join(card))
        return "\n\n".join(cards)
        
    text = re.sub(r"(?:(?:^|\n)\|.*\|)+\n?", _render_table, text)

    # 7. Handle alerts (> [!NOTE] ...)
    def _alert_sub(m: re.Match[str]) -> str:
        """Convert GitHub-style alert blocks into Telegram-safe HTML."""
        kind = m.group(1).upper()
        content = m.group(2)
        icons = {"NOTE": "ℹ️ <b>Catatan</b>", "TIP": "💡 <b>Tips</b>", 
                 "IMPORTANT": "❗ <b>Penting</b>", "WARNING": "⚠️ <b>Peringatan</b>", 
                 "CAUTION": "🛑 <b>Hati-hati</b>"}
        label = icons.get(kind, "ℹ️ <b>Info</b>")
        lines = content.splitlines()
        content = "\n".join(re.sub(r"^&gt;\s?", "", l) for l in lines)
        if len(lines) > 6:
            return f"<blockquote expandable>{label}\n{content}</blockquote>"
        return f"<blockquote>{label}\n{content}</blockquote>"
        
    text = re.sub(r"^&gt;\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\n((?:&gt;[^\n]*\n?)+)", _alert_sub, text, flags=re.MULTILINE)

    # 7.5. Handle blockquotes (> Quote, escaped to &gt;)
    def _blockquote_sub(m: re.Match[str]) -> str:
        """Convert markdown blockquotes into Telegram-safe HTML."""
        lines = m.group(0).splitlines()
        content = "\n".join(re.sub(r"^&gt;\s?", "", l) for l in lines)
        if len(lines) > 6 or content.startswith("!"):
            content = content[1:].strip() if content.startswith("!") else content
            return f"<blockquote expandable>{content}</blockquote>"
        return f"<blockquote>{content}</blockquote>"
    text = re.sub(r"(^&gt;[^\n]*\n?)+", _blockquote_sub, text, flags=re.MULTILINE)

    # 8. Convert file:// links
    text = re.sub(r"\[([^\]]+)\]\(file://[^\)]+/([^\)/]+)\)", r"📄 <code>\2</code>", text)
    text = re.sub(r"file://[^\s\)]+/([^\s\)/]+)", r"<code>\1</code>", text)

    # 8.5. Images
    text = re.sub(r"!\[([^\]]*)\]\([^\)]+\)", r"🖼️ <i>\1</i>", text)

    # 9. Standard links: [text](http...)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r'<a href="\2">\1</a>', text)

    # 10. Bold-Italic: ***text***
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    text = re.sub(r"___(.+?)___", r"<b><i>\1</i></b>", text)

    # 11. Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", text)

    # 11.5. Fix overlapping bold-italic (shift trailing * inside bold tag)
    text = text.replace("</b>*", "*</b>").replace("</b>_", "_</b>")

    # 12. Italics: *text* (not bullet points or asterisks) or _text_ at word boundaries
    text = re.sub(r"(?<![\w\*])\*(?!\s|\*)([^\*\n]+?)(?<!\s|\*)\*(?![\w\*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(?!\s|_)([^_\n]+?)(?<!\s|_)_(?!\w)", r"<i>\1</i>", text)

    # 12. Strikethrough and spoiler: ~~text~~ and ||text||
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)

    # 13. Bullet points (* or - at start of line) -> •
    text = re.sub(r"^(\s*)-\s+\[ \]\s+", r"\1☐ ", text, flags=re.MULTILINE)
    text = re.sub(r"^(\s*)-\s+\[[xX]\]\s+", r"\1✅ ", text, flags=re.MULTILINE)
    text = re.sub(r"^(\s\s+)[*-]\s+", r"\1◦ ", text, flags=re.MULTILINE)
    text = re.sub(r"^(\s*)[*-]\s+", r"\1• ", text, flags=re.MULTILINE)
    
    # 13.5. Light Math / LaTeX (convert to unicode wrapped in italic)
    def _math_sub(m: re.Match[str]) -> str:
        """Convert math notation into Telegram-safe HTML."""
        s = m.group(1)
        s = s.replace("\\approx", "≈").replace("\\infty", "∞").replace("\\in", "∈")
        s = s.replace("\\to", "→").replace("\\pi", "π").replace("\\theta", "θ")
        s = s.replace("^2", "²").replace("_1", "₁").replace("_2", "₂")
        s = s.replace("\\sqrt", "√").replace("\\mathbf", "").replace("{", "").replace("}", "")
        s = s.replace("\\sin", "sin").replace("\\cos", "cos").replace("\\tan", "tan")
        return f"<i>{s}</i>"
    text = re.sub(r"\$\$([^\$]+)\$\$", _math_sub, text)
    text = re.sub(r"\$(?!\s)([^\$]+?)(?<!\s)\$", _math_sub, text)

    # 13.8. Raw br to newline
    text = re.sub(r"&lt;br\s*/?&gt;", "\n", text)

    # 14. Restore protected items
    for idx, tag in enumerate(tg_tags):
        text = text.replace(f"\x00TGTAG_{idx}\x00", tag)
    for idx, tag in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE_{idx}\x00", tag)
    for idx, tag in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK_{idx}\x00", tag)

    return text


def sanitize_markdown_for_telegram(text: str) -> str:
    """Legacy helper: format markdown text for Telegram MarkdownV1."""
    if not text:
        return ""

    def _header_sub(m: re.Match[str]) -> str:
        """Convert markdown headers to bold Telegram-safe HTML."""
        level = len(m.group(1))
        title = m.group(2).strip()
        icon = "🔹" if level > 2 else "📌"
        return f"{icon} *{title}*"

    lines = []
    in_code_block = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            lines.append(line)
            continue
        if in_code_block:
            lines.append(line)
            continue

        if re.match(r"^(\s*[-*_]\s*){3,}$", line):
            lines.append("────────────────")
            continue

        h_match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h_match:
            lines.append(_header_sub(h_match))
            continue

        line = re.sub(
            r"\[([^\]]+)\]\(file://([^\)]+)\)",
            r"*\1* (`\2`)",
            line,
        )
        line = re.sub(r"file://([^\s\)]+)", r"`\1`", line)
        lines.append(line)

    return "\n".join(lines)



@dataclass(frozen=True)
class InboundMessage:
    update_id: int
    chat_id: int
    user_id: int
    text: str
    photo: list[dict[str, Any]] | None = None
    document: dict[str, Any] | None = None
    media_group_id: str | None = None
    documents: list[dict[str, Any]] | None = None
    photos: list[list[dict[str, Any]]] | None = None


@dataclass(frozen=True)
class CallbackQuery:
    update_id: int
    callback_query_id: str
    chat_id: int
    user_id: int
    message_id: int
    data: str


# Telegram inline keyboards are nested lists of dicts: list[list[Button]].
InlineKeyboard = list[list[dict[str, Any]]]


def parse_update(update: dict[str, Any]) -> InboundMessage | None:
    """Parse a Telegram update into an InboundMessage, or None if not a message."""
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    text = msg.get("text") or msg.get("caption") or ""
    photo = msg.get("photo")
    doc = msg.get("document")
    if not text and photo is None and doc is None:
        return None
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    cid = chat.get("id")
    uid = sender.get("id")
    uid_n = update.get("update_id")
    if not isinstance(cid, int) or not isinstance(uid, int) or not isinstance(uid_n, int):
        return None
    mgid = msg.get("media_group_id")
    return InboundMessage(
        update_id=uid_n,
        chat_id=cid,
        user_id=uid,
        text=text,
        photo=list(photo) if isinstance(photo, list) else None,
        document=doc if isinstance(doc, dict) else None,
        media_group_id=str(mgid) if mgid is not None else None,
        documents=[doc] if isinstance(doc, dict) else None,
        photos=[list(photo)] if isinstance(photo, list) else None,
    )


def parse_callback_query(update: dict[str, Any]) -> CallbackQuery | None:
    """Parse a Telegram update into a CallbackQuery, or None if not applicable."""
    cq = update.get("callback_query")
    if not isinstance(cq, dict):
        return None
    cq_id = cq.get("id")
    sender = cq.get("from") or {}
    user_id = sender.get("id")
    inner_msg = cq.get("message") or {}
    chat = inner_msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = inner_msg.get("message_id")
    data = cq.get("data")
    update_id = update.get("update_id")
    if (
        not isinstance(cq_id, str)
        or not isinstance(user_id, int)
        or not isinstance(chat_id, int)
        or not isinstance(message_id, int)
        or not isinstance(data, str)
        or not isinstance(update_id, int)
    ):
        return None
    return CallbackQuery(
        update_id=update_id,
        callback_query_id=cq_id,
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        data=data,
    )


def is_authorized_user(user_id: int, cfg: TelegramConfig) -> bool:
    """True when the user id is in the configured allowlist."""
    return user_id in cfg.allowed_user_ids


def is_authorized_chat(chat_id: int, cfg: TelegramConfig) -> bool:
    """True when chat filtering is disabled or the chat is allowed."""
    return not cfg.allowed_chat_ids or chat_id in cfg.allowed_chat_ids


def is_authorized(msg: InboundMessage, cfg: TelegramConfig) -> bool:
    """True when both the user and the chat pass the allowlists."""
    if not is_authorized_user(msg.user_id, cfg):
        return False
    return is_authorized_chat(msg.chat_id, cfg)


def chunk_message(text: str, max_len: int = 4096) -> list[str]:
    """Split text into chunks within Telegram's message length limit."""
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, max_len)
        if cut == -1:
            cut = max_len
        chunks.append(remaining[:cut])
        if cut < len(remaining) and remaining[cut:cut + 1] == "\n":
            remaining = remaining[cut + 1:]
        else:
            remaining = remaining[cut:]
    return chunks


import httpx

_HTML_TAG_RE = re.compile(
    r"<\/?(b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler|a)(\s+[^>]*)?>",
    re.IGNORECASE,
)


def detect_parse_mode(text: str) -> str:
    """Detect whether text is primarily HTML or Markdown."""
    if _HTML_TAG_RE.search(text):
        return "HTML"
    return "Markdown"


class TelegramClient:
    """Thin async wrapper around the Telegram bot HTTP API."""

    def __init__(self, bot_token: str, *, base_url: str = "https://api.telegram.org") -> None:
        self._base = f"{base_url}/bot{bot_token}"
        self._bot_token = bot_token
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TelegramClient":
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(35.0))
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._client is not None
        await self._client.aclose()
        self._client = None

    async def get_me(self) -> dict[str, Any]:
        """Fetch bot metadata via getMe."""
        assert self._client is not None
        r = await self._client.get(f"{self._base}/getMe")
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram getMe failed: {data.get('description')}")
        return dict(data.get("result") or {})

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        """Long-poll Telegram for new updates."""
        assert self._client is not None
        r = await self._client.get(f"{self._base}/getUpdates", params={
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": '["message","callback_query"]',
        })
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram getUpdates failed: {data.get('description')}")
        return list(data.get("result") or [])

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
        parse_mode: str | None = "HTML",
    ) -> int | None:
        """Send text (auto-chunked) and return the first message id."""
        assert self._client is not None
        first_message_id: int | None = None

        effective_mode = parse_mode
        if effective_mode == "HTML":
            text = markdown_to_telegram_html(text)
        elif effective_mode == "Markdown":
            text = sanitize_markdown_for_telegram(text)

        chunks = chunk_message(text)
        if effective_mode == "HTML":
            chunks = balance_html_chunks(chunks)

        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if effective_mode:
                payload["parse_mode"] = effective_mode
            if i == 0 and keyboard is not None:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            r = await self._client.post(f"{self._base}/sendMessage", json=payload)
            data = r.json()
            if not data.get("ok"):
                desc = str(data.get("description", "")).lower()
                if payload.get("parse_mode") and ("parse" in desc or "entity" in desc or "tag" in desc):
                    LOG.warning(f"Telegram parse error, falling back to plain text: {desc}\nOffending chunk: {chunk}")
                    payload.pop("parse_mode", None)
                    payload["text"] = html_to_plain(chunk)
                    r = await self._client.post(f"{self._base}/sendMessage", json=payload)
                    data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"sendMessage failed: {data.get('description')}")
            if i == 0:
                result = data.get("result") or {}
                mid = result.get("message_id")
                if isinstance(mid, int):
                    first_message_id = mid
        return first_message_id

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
        parse_mode: str | None = "HTML",
    ) -> None:
        """Edit an existing message's text/keyboard in place."""
        assert self._client is not None
        effective_mode = parse_mode
        if effective_mode == "HTML":
            text = markdown_to_telegram_html(text)
        elif effective_mode == "Markdown":
            text = sanitize_markdown_for_telegram(text)

        chunks = chunk_message(text)
        if effective_mode == "HTML":
            chunks = balance_html_chunks(chunks)
        
        if not chunks:
            return

        # First chunk gets edited
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": chunks[0],
        }
        if effective_mode:
            payload["parse_mode"] = effective_mode
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
            
        r = await self._client.post(f"{self._base}/editMessageText", json=payload)
        data = r.json()
        if not data.get("ok"):
            desc = str(data.get("description", "")).lower()
            if "not modified" in desc:
                pass
            elif payload.get("parse_mode") and ("parse" in desc or "entity" in desc or "tag" in desc):
                LOG.warning(f"Telegram edit parse error, falling back to plain text: {desc}\nOffending chunk: {chunks[0]}")
                payload.pop("parse_mode", None)
                payload["text"] = html_to_plain(chunks[0])
                r = await self._client.post(f"{self._base}/editMessageText", json=payload)
                data = r.json()
                if not data.get("ok") and "not modified" not in str(data.get("description", "")).lower():
                    raise RuntimeError(f"editMessageText failed: {data.get('description')}")
            else:
                raise RuntimeError(f"editMessageText failed: {data.get('description')}")

        # Send subsequent chunks as new messages
        for chunk in chunks[1:]:
            send_payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if effective_mode:
                send_payload["parse_mode"] = effective_mode
            r = await self._client.post(f"{self._base}/sendMessage", json=send_payload)
            data = r.json()
            if not data.get("ok"):
                desc = str(data.get("description", "")).lower()
                if send_payload.get("parse_mode") and ("parse" in desc or "entity" in desc or "tag" in desc):
                    LOG.warning(f"Telegram parse error, falling back to plain text: {desc}\nOffending chunk: {chunk}")
                    send_payload.pop("parse_mode", None)
                    send_payload["text"] = html_to_plain(chunk)
                    r = await self._client.post(f"{self._base}/sendMessage", json=send_payload)
                    data = r.json()
                if not data.get("ok"):
                    raise RuntimeError(f"sendMessage failed: {data.get('description')}")

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
    ) -> None:
        """Acknowledge a callback query with an optional toast."""
        assert self._client is not None
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        r = await self._client.post(f"{self._base}/answerCallbackQuery", json=payload)
        data = r.json()
        if not data.get("ok"):
            return

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Send a chat action such as the typing indicator."""
        assert self._client is not None
        r = await self._client.post(
            f"{self._base}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
        )
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"sendChatAction failed: {data.get('description')}")

    async def get_file(self, file_id: str) -> bytes:
        """Download a file by file_id and return its bytes."""
        assert self._client is not None
        r = await self._client.get(f"{self._base}/getFile", params={"file_id": file_id})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"getFile failed: {data.get('description')}")
        file_path = data["result"]["file_path"]
        r2 = await self._client.get(
            f"https://api.telegram.org/file/bot{self._bot_token}/{file_path}")
        return r2.content

    async def set_my_commands(
        self,
        commands: list[dict[str, str]],
        *,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register the bot command menu (optionally scoped)."""
        assert self._client is not None
        payload: dict[str, Any] = {"commands": commands}
        if scope is not None:
            payload["scope"] = scope
        r = await self._client.post(f"{self._base}/setMyCommands", json=payload)
        return r.json()

    async def set_webhook(self, url: str, secret_token: str = "") -> dict[str, Any]:
        """Register a webhook endpoint for incoming updates."""
        assert self._client is not None
        params: dict[str, Any] = {"url": url}
        if secret_token:
            params["secret_token"] = secret_token
        r = await self._client.get(f"{self._base}/setWebhook", params=params)
        return r.json()

    async def send_document(
        self,
        chat_id: int,
        file_path: str | Path,
        *,
        caption: str = "",
    ) -> int | None:
        """Upload a document to the chat; returns the message id."""
        assert self._client is not None
        p = Path(file_path)
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption

        with p.open("rb") as f:
            files = {"document": (p.name, f.read())}

        r = await self._client.post(
            f"{self._base}/sendDocument",
            data=data,
            files=files,
        )
        res = r.json()
        if not res.get("ok"):
            raise RuntimeError(f"sendDocument failed: {res.get('description')}")
        result = res.get("result") or {}
        mid = result.get("message_id")
        return mid if isinstance(mid, int) else None

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a message sent by the bot (Telegram deleteMessage API)."""
        assert self._client is not None
        r = await self._client.post(
            f"{self._base}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
        )
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"deleteMessage failed: {data.get('description')}")

    async def delete_webhook(self) -> dict[str, Any]:
        """Remove the webhook registration."""
        assert self._client is not None
        r = await self._client.get(f"{self._base}/deleteWebhook")
        return r.json()

