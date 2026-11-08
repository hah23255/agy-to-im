"""Webhook server — HTTP handler for Telegram webhook delivery."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.daemon import _TelegramLike

LOG = logging.getLogger("antigravity_telegram_bridge")

WEBHOOK_PATH = "/webhook"


def _verify_secret(body: bytes, header: str, token: str) -> bool:
    """Verify the X-Telegram-Bot-Api-Secret-Token header against the shared secret."""
    if not header or not token:
        return False
    expected = hmac.new(
        hashlib.sha256(token.encode()).digest(),
        body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


async def webhook_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    tg: "_TelegramLike", token: str,
) -> None:
    """Handle one incoming webhook request."""
    try:
        raw = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
            if not chunk:
                break
            raw += chunk
            if b"\r\n\r\n" in raw:
                headers_end = raw.index(b"\r\n\r\n") + 4
                body_start = headers_end
                if b"Content-Length:" in raw:
                    cl_pos = raw.index(b"Content-Length:") + 16
                    cl_end = raw.index(b"\r\n", cl_pos)
                    content_length = int(raw[cl_pos:cl_end])
                    while len(raw) - body_start < content_length:
                        more = await asyncio.wait_for(reader.read(4096), timeout=5)
                        if not more:
                            break
                        raw += more
                break

        body = raw[raw.index(b"\r\n\r\n") + 4:]

        secret_header = ""
        for line in raw[:raw.index(b"\r\n\r\n")].decode(errors="replace").split("\r\n"):
            if line.lower().startswith("x-telegram-bot-api-secret-token:"):
                secret_header = line.split(":", 1)[1].strip()

        if not _verify_secret(body, secret_header, token):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        update = json.loads(body)
        _webhook_updates.append(update)

        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
    except Exception as e:
        LOG.debug("webhook handler error: %s", e)
        try:
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
        except Exception:
            pass


_webhook_updates: list[dict] = []


def drain_webhook_updates() -> list[dict]:
    """Return and clear pending webhook updates."""
    updates = list(_webhook_updates)
    _webhook_updates.clear()
    return updates


async def start_webhook_server(
    port: int, tg: "_TelegramLike", token: str,
) -> None:
    """Start a local HTTP server to receive Telegram webhook callbacks."""
    server = await asyncio.start_server(
        lambda r, w: webhook_handler(r, w, tg, token),
        host="0.0.0.0", port=port,
    )
    LOG.info("webhook server listening on port %d", port)
    async with server:
        await server.serve_forever()
