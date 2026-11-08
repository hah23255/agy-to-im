"""HTTP health/metrics endpoint — zero-dependency asyncio server."""
from __future__ import annotations

import asyncio
import gc
import json
import os
import time

HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = int(os.environ.get("AGY_BRIDGE_HEALTH_PORT") or "9099")
# Self-restart if resident memory exceeds this threshold (bytes).
# 768 MiB — healthy state is 20–100MB.
MEMORY_LIMIT_BYTES = int(os.environ.get("AGY_BRIDGE_MEMORY_LIMIT") or str(805_306_368))  # 768 MiB
_started = time.time()
_turns_total = 0
_errors_total = 0
_turn_latencies: list[float] = []
_RSS_PATH = "/proc/self/statm"


def record_turn() -> None:
    """Increment the successful-turn counter."""
    global _turns_total
    _turns_total += 1


def record_error() -> None:
    """Increment the error counter."""
    global _errors_total
    _errors_total += 1


def record_latency(ms: float) -> None:
    """Record a turn latency sample."""
    _turn_latencies.append(ms)
    # Rotate at 100 entries — retain recent history without unbounded growth
    if len(_turn_latencies) > 100:
        _turn_latencies[:50] = []


def _read_rss_bytes() -> int:
    """Read resident memory from /proc/self/statm (page count * page size)."""
    try:
        with open(_RSS_PATH) as f:
            parts = f.read().split()
            if len(parts) >= 2:
                return int(parts[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        pass
    return 0


def memory_healthy() -> bool:
    """True if resident memory is below the self-restart threshold."""
    rss = _read_rss_bytes()
    return rss < MEMORY_LIMIT_BYTES


def _health_body() -> bytes:
    """Build the /health JSON payload."""
    rss = _read_rss_bytes()
    return json.dumps({
        "status": "ok",
        "uptime_seconds": time.time() - _started,
        "turns_total": _turns_total,
        "errors_total": _errors_total,
        "memory_rss_bytes": rss,
        "memory_limit_bytes": MEMORY_LIMIT_BYTES,
        "memory_healthy": memory_healthy(),
    }).encode()


def _metrics_body() -> bytes:
    """Build the Prometheus /metrics payload."""
    lines = [
        "# HELP bridge_turns_total Total turns processed",
        "# TYPE bridge_turns_total counter",
        f"bridge_turns_total {_turns_total}",
        "# HELP bridge_errors_total Total turn errors",
        "# TYPE bridge_errors_total counter",
        f"bridge_errors_total {_errors_total}",
        "# HELP bridge_uptime_seconds Daemon uptime",
        "# TYPE bridge_uptime_seconds gauge",
        f"bridge_uptime_seconds {time.time() - _started:.1f}",
        "# HELP bridge_active 1 if daemon is running",
        "# TYPE bridge_active gauge",
        "bridge_active 1",
    ]
    if _turn_latencies:
        avg = sum(_turn_latencies) / len(_turn_latencies)
        lines.extend([
            "# HELP bridge_turn_latency_ms_avg Average turn latency",
            "# TYPE bridge_turn_latency_ms_avg gauge",
            f"bridge_turn_latency_ms_avg {avg:.0f}",
        ])
    return "\n".join(lines).encode() + b"\n"


async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Serve /health and /metrics requests."""
    try:
        req = (await asyncio.wait_for(reader.readuntil(b"\r\n"), 5)).decode().strip()
    except (asyncio.TimeoutError, ValueError):
        writer.close()
        return

    path = req.split(" ")[1] if " " in req else "/"

    # Drain headers
    try:
        while True:
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), 5)
            if line in (b"\r\n", b""):
                break
    except (asyncio.TimeoutError, ValueError):
        pass

    if path == "/metrics":
        body, ct = _metrics_body(), "text/plain; version=0.0.4"
    else:
        body, ct = _health_body(), "application/json"

    resp = (f"HTTP/1.1 200 OK\r\nContent-Type: {ct}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
    writer.write(resp + body)
    await writer.drain()
    writer.close()


async def start_server(host: str = HEALTH_HOST, port: int = HEALTH_PORT) -> asyncio.AbstractServer:
    """Start the health/metrics HTTP server."""
    return await asyncio.start_server(_handler, host, port)
