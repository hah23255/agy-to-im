# antigravity-telegram-bridge — Design

**Date:** 2026-06-26  
**Status:** Implemented  
**Target CLI:** `agy` v1.0.12+

## Context and motivation

This bridge connects the [Antigravity CLI](https://antigravity.google) (`agy`) to Telegram so an `agy` session can be reached from a phone. `agy` already runs locally; the bridge adds an inbound surface (a Telegram bot) and a per-chat project map so a Telegram conversation maps cleanly onto an `agy` project/working directory.

Design constraints:

1. **Standalone codebase** — no third-party SDKs beyond `httpx`, single Python runtime.
2. **Lives within the Antigravity/Google plugin directory** at `~/.gemini/extensions/`.
3. **Installed as an `agy` plugin** under `~/.gemini/extensions/antigravity-telegram-bridge/` so it shows up in `agy`'s plugin tooling, while the actual daemon runs as a sibling `systemd --user` service.

## Constraint that shaped the design

`agy` plugins are synchronous, short-lived tool wrappers. A Telegram bridge is fundamentally a long-running daemon (poll for updates, spawn `agy` per inbound message, reply). The design therefore splits into two layers: an `agy`-visible plugin façade and a daemon supervised by `systemd`.

## Architecture

### Layer 1 — `agy` plugin façade

Lives at `~/.gemini/extensions/antigravity-telegram-bridge/`. Recognized by `agy` plugin tooling.

`antigravity-extension.json` declares one tool, `bridge`, accepting `action: start | stop | status | logs | setup`. The tool's `command` points at `src/control.py`, which talks to `systemd` to manage the daemon. The tool itself is short-lived; it is **not** the daemon.

### Layer 2 — The daemon

A separate long-running Python process, run as `python -m src.daemon`, supervised by `systemctl --user antigravity-telegram-bridge.service`. The unit is installed by `install.sh`.

**Inbound message loop:**

1. Telegram `getUpdates` long-poll (timeout=30s) returns updates. In webhook mode, callbacks are received on a local HTTP server and drained into the same loop.
2. Filter: ignore non-message/non-callback updates; reject senders whose `from.id` is not in `telegram.allowed_user_ids` (and chats not in `telegram.allowed_chat_ids` when that list is non-empty).
3. Map `chat_id → chat_dir` via persisted state; create the directory if first time.
4. `sendChatAction: typing` to the chat.
5. Build `agy` argv:
   ```
   agy -p "<prompt>" --continue|--new-project [--model <model>]
       --dangerously-skip-permissions [--sandbox]
       --print-timeout 15m
   ```
   and spawn it with `cwd=chat_dir`.
6. Capture plain-text stdout. On exit, send the reply to Telegram (chunked if >4096 chars).
7. Persist `last_update_id` and `chats` map atomically.

**Errors:** non-zero exit from `agy` → reply `⚠️ agy error (exit <code>)` to the chat and log stderr. Network errors talking to Telegram → backoff, never crash the loop.

**Cancellation:** SIGTERM/SIGINT → finish in-flight `agy` run, delete webhook, close health server, then exit.

## File layout

```
~/.gemini/extensions/antigravity-telegram-bridge/   # code + config (the "plugin")
├── antigravity-extension.json                       # agy plugin manifest
├── config.json                                      # user-edited; bot token, allowed users
├── config.example.json                              # template, safe to commit
├── pyproject.toml                                   # deps: httpx, pytest, ...
├── README.md                                        # install + operator guide
├── install.sh                                       # uv venv + uv pip install + systemd unit install
├── systemd/
│   └── antigravity-telegram-bridge.service.template
├── docs/                                            # design, deployment, operations, security
└── src/
    ├── __init__.py
    ├── agy_catalog.py                               # live subscription model catalog + quota discovery
    ├── agy_runner.py                                # build argv + spawn agy, capture plain text
    ├── commands.py                                  # slash commands + inline-keyboard callbacks
    ├── control.py                                   # plugin-tool entry: action=start|stop|status|logs|setup
    ├── daemon.py                                    # main poll/webhook loop + orchestration
    ├── events.py                                    # (reserved) event buffering helpers
    ├── health.py                                    # /health + /metrics HTTP server
    ├── media.py                                     # photo/document download + inbox
    ├── queue.py                                     # FIFO multi-user turn queue
    ├── state.py                                     # ~/.antigravity/bridge/state.json reader/writer
    ├── telegram.py                                  # Telegram HTTP client + pure helpers
    ├── turn.py                                      # per-turn execution with typing heartbeat
    └── webhook.py                                   # webhook receiver + HMAC verification

~/.antigravity/bridge/                              # runtime data (separate from code)
├── chats/<chat_id>/                                # per-chat agy working directory
├── logs/bridge.log                                 # daemon stdout+stderr
└── state.json                                      # {last_update_id, chats: {chat_id: ChatState}}
```

Rationale for the split: `~/.gemini/extensions/antigravity-telegram-bridge/` is code + configuration that should stay clean for `git pull` and re-install. `~/.antigravity/bridge/` is mutable runtime state.

## Plugin manifest

```json
{
  "name": "telegram-bridge",
  "version": "0.1.0",
  "description": "Bridge Telegram chats to Antigravity CLI (agy) sessions",
  "config_file": "config.json",
  "tools": [{
    "name": "bridge",
    "description": "Control the Antigravity telegram bridge daemon (start/stop/status/logs/setup)",
    "command": [".venv/bin/python", "-m", "src.control"],
    "parameters": {
      "type": "object",
      "properties": {
        "action": { "type": "string", "enum": ["start", "stop", "status", "logs", "setup"] },
        "lines":  { "type": "integer", "default": 50 }
      },
      "required": ["action"]
    }
  }]
}
```

`control.py` reads action from stdin and dispatches:

- `start` → `systemctl --user start antigravity-telegram-bridge.service`
- `stop` → `systemctl --user stop antigravity-telegram-bridge.service`
- `status` → `systemctl --user status antigravity-telegram-bridge.service`
- `logs` → tails `~/.antigravity/bridge/logs/bridge.log`
- `setup` → checks config.json validity, validates Telegram bot token via `getMe`, checks `agy --version` and auth dir

## Configuration

`~/.gemini/extensions/antigravity-telegram-bridge/config.json`:

```json
{
  "telegram": {
    "bot_token": "<from BotFather>",
    "allowed_user_ids": [123456789],
    "allowed_chat_ids": []
  },
  "agy": {
    "chats_root": "",
    "default_workdir": "",
    "model": "",
    "mode": "code"
  }
}
```

- `bot_token`: required.
- `allowed_user_ids`: at least one entry required (default-deny).
- `allowed_chat_ids`: optional whitelist; if empty, any chat from an allowed user is accepted.
- `agy.model`: empty string means inherit `agy`'s default.
- `agy.mode`: `code` (auto-approve) or `plan` (read-only sandbox via `--sandbox`).

The config file is read once at daemon start.

## agy session mapping

`agy` resumes sessions by cwd/project. Each Telegram chat gets a dedicated directory under `~/.antigravity/bridge/chats/<chat_id>/`.

- First turn: `agy ... --new-project`
- Subsequent turns: `agy ... --continue`
- `/reset`: clears `has_session`; next turn uses `--new-project` again.

This is Option B from the adoption plan (cwd auto-resolution). It keeps the implementation simple and matches `agy`'s native behavior.

## Dependencies

- Python 3.11+.
- **httpx** for Telegram HTTP API.
- `uv` for venv creation in `install.sh`.

No Node, no npm packages, no third-party LLM SDKs.

## Install flow

`install.sh` performs:

1. Creates `~/.antigravity/bridge/{logs,runtime,chats}/`.
2. Creates `~/.config/systemd/user/` if missing.
3. Builds the Python venv at `.venv/` (skipped if already exists).
4. Installs the project in editable mode: `uv pip install -e .`.
5. Renders `systemd/antigravity-telegram-bridge.service.template` with `$HOME` and venv-python path → `~/.config/systemd/user/antigravity-telegram-bridge.service`.
6. Runs `systemctl --user daemon-reload` and `systemctl --user enable antigravity-telegram-bridge.service`.

The installer does **not** start the service.

## What's in v0.1.0

- Telegram ↔ `agy` text messaging.
- Per-chat project/session isolation.
- Inline-keyboard settings panel.
- Photo and document upload support.
- Multi-user FIFO turn queue.
- Webhook mode with HMAC verification.
- Health and Prometheus metrics endpoints.
- `systemd --user` supervision.
- Plugin tool for `agy`.

## What's deliberately NOT in v0.1.0

- Multi-channel support (Discord, Feishu, QQ) — Telegram only.
- Token-level streaming — `agy` print mode returns the full reply at end of turn.
- Cross-machine sync of `state.json` — single-host only.
- Complex project ID mapping — uses cwd auto-resolution.

## Verification path

End-to-end manual checks after install:

1. `bash install.sh` exits 0; venv exists; systemd unit listed.
2. Edit `config.json`, start the service.
3. `systemctl --user status antigravity-telegram-bridge.service` reports active.
4. From the allowed Telegram account send `/start`. Expect a reply.
5. Send a follow-up; confirm session continuity.
6. From a non-allowed account: no reply; log shows `drop unauth user=...`.
7. `curl http://127.0.0.1:9100/health` returns `{"status":"ok",...}`.
8. Stop with `systemctl --user stop ...`; send another message → no reply.

## Index of source files

| Path | Responsibility |
|---|---|
| `src/config.py` | Parse + validate `config.json` |
| `src/state.py` | Atomic JSON state |
| `src/telegram.py` | Pure helpers + async `TelegramClient` |
| `src/agy_catalog.py` | Live subscription model catalog & quota discovery |
| `src/agy_runner.py` | Build argv and spawn `agy` |
| `src/commands.py` | Slash commands + inline-keyboard callbacks |
| `src/turn.py` | Per-turn execution with typing heartbeat |
| `src/daemon.py` | Main poll/webhook loop |
| `src/control.py` | Plugin-tool dispatcher |
| `src/health.py` | Health + metrics HTTP server |
| `src/media.py` | Photo/document/inbox handling |
| `src/queue.py` | FIFO turn queue |
| `src/webhook.py` | Webhook receiver |
