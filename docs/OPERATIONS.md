# Operations runbook — antigravity-telegram-bridge

Step-by-step guide for installing, operating, and troubleshooting the Telegram→Antigravity (`agy`) bridge.

---

## Contents

1. [Prerequisites](#prerequisites)
2. [Get a Telegram bot token](#get-a-telegram-bot-token)
3. [Find your Telegram user ID](#find-your-telegram-user-id)
4. [Configure the bridge](#configure-the-bridge)
5. [Install](#install)
6. [Start the bridge](#start-the-bridge)
7. [Verify end-to-end](#verify-end-to-end)
8. [Daily ops](#daily-ops)
9. [Troubleshooting](#troubleshooting)
10. [Upgrading](#upgrading)
11. [Uninstall](#uninstall)
12. [Pushing to GitHub](#pushing-to-github)
13. [Architecture refresher](#architecture-refresher)

---

## Prerequisites

| Requirement | Why | How to verify |
|---|---|---|
| Linux with systemd `--user` | Daemon supervisor | `systemctl --user status` returns without error |
| Python ≥ 3.11 | Runtime | `python3 --version` |
| `uv` ≥ 0.4 | Venv + dep management | `uv --version` |
| `agy` CLI on `PATH` | The backend | `agy --version` |
| Authenticated `agy` session | For the CLI to work | `~/.gemini/antigravity-cli/` exists |
| Outbound HTTPS to `api.telegram.org` | Bot polling | `curl -sSI https://api.telegram.org/` returns 200 |

If `uv` is missing:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

If `agy` is missing, install it from the official Antigravity distribution and run `agy` once to complete OAuth.

---

## Get a Telegram bot token

1. Message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Pick a display name.
4. Pick a username ending in `bot` (must be unique).
5. Copy the token. Keep it secret — anyone with the token can post as the bot.

Recommended hardening:

- `/setprivacy` → your bot → `Disable`.
- `/setjoingroups` → your bot → `Disable`.

---

## Find your Telegram user ID

The bridge is **default-deny**.

**Option A:** message [@userinfobot](https://t.me/userinfobot).

**Option B:** start the bot once, send any message, then read the log:

```sh
tail -f ~/.antigravity/bridge/logs/bridge.log
# "drop unauth user=123456789"
```

Add that ID to `allowed_user_ids`, restart, and you're in.

---

## Configure the bridge

```sh
cd ~/.gemini/extensions/antigravity-telegram-bridge
cp config.example.json config.json
chmod 600 config.json
$EDITOR config.json
```

Fill in:

```json
{
  "telegram": {
    "bot_token": "1234567890:ABCDEFghijklmnopqrstuvwxyz_-1234567890",
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

Field-by-field:

| Field | Required? | Notes |
|---|---|---|
| `telegram.bot_token` | yes | From BotFather. Format `<int>:<base64-ish>`. |
| `telegram.allowed_user_ids` | yes | Non-empty list of int IDs. Empty = refuse to start. |
| `telegram.allowed_chat_ids` | optional | If non-empty, restricts chats even for allowed users. |
| `agy.chats_root` | optional | Per-chat dirs. Default: `~/.antigravity/bridge/chats`. |
| `agy.default_workdir` | optional | Base cwd for `agy`. |
| `agy.model` | optional | Empty = `agy` default. |
| `agy.mode` | optional | `code` (auto) or `plan` (read-only sandbox). |

**Never commit `config.json`.** It is gitignored.

---

## Install

```sh
cd ~/.gemini/extensions/antigravity-telegram-bridge
bash install.sh
```

What `install.sh` does (idempotent):

1. Creates `~/.antigravity/bridge/{logs,runtime,chats}/`.
2. Creates `~/.config/systemd/user/` if missing.
3. Builds `.venv/` (skipped if present).
4. Installs in editable mode: `uv pip install -e .`.
5. Renders the systemd unit template to `~/.config/systemd/user/antigravity-telegram-bridge.service`.
6. `systemctl --user daemon-reload`.
7. Autostart is **opt-in**: by default the unit is left (or made) `disabled`, so the bridge
   does not start on login / WSL boot. Run `bash install.sh --autostart` to
   `systemctl --user enable` it instead.

The installer does **not** start the service.

To toggle autostart later without re-running the installer:

```sh
systemctl --user disable antigravity-telegram-bridge.service   # manual start only
systemctl --user enable  antigravity-telegram-bridge.service   # start on login / WSL boot
```

### Optional: enable lingering for headless boot

```sh
sudo loginctl enable-linger $USER
```

---

## Start the bridge

Three equivalent ways:

```sh
# 1. systemd directly
systemctl --user start antigravity-telegram-bridge.service

# 2. From inside agy
agy -p "use the bridge tool with action=start"

# 3. From any shell via the plugin entry point
echo '{"action":"start"}' | ~/.gemini/extensions/antigravity-telegram-bridge/.venv/bin/python -m src.control
```

Confirm:

```sh
systemctl --user is-active antigravity-telegram-bridge.service
# expect: active
```

Full status:

```sh
systemctl --user status antigravity-telegram-bridge.service --no-pager
```

---

## Verify end-to-end

1. From your phone, open the bot in Telegram.
2. Send `/start` — after a restart every chat wakes up inactive, so this activates the bot.
3. Then send a text message (e.g. `hello`). Within a few seconds expect the typing indicator and a `⏳` placeholder, then an `agy` reply. While a turn is running, further messages are acknowledged immediately with `⏳ Queued (position #N)` and processed in order.
4. Check the log:

```sh
tail -n 20 ~/.antigravity/bridge/logs/bridge.log
```

You should see one INFO line per turn:

```
2026-06-26 ... INFO antigravity_telegram_bridge: turn chat=123456789 cwd=... exit=0 ms=... reply_len=...
```

5. Send a follow-up like `what did I just say?`. The reply should reference the previous message — proves session continuity.
6. From a non-allowed account, send a message. Expect no reply. Log shows:

```
INFO antigravity_telegram_bridge: drop unauth user=999999999
```

7. Health check:

```sh
curl http://127.0.0.1:9100/health
```

If all pass, you're operating cleanly.

---

## Daily ops

### Cheat-sheet

| Action | Command |
|---|---|
| Start | `systemctl --user start antigravity-telegram-bridge.service` |
| Stop | `systemctl --user stop antigravity-telegram-bridge.service` |
| Restart | `systemctl --user restart antigravity-telegram-bridge.service` |
| Status | `systemctl --user status antigravity-telegram-bridge.service` |
| Live log | `tail -f ~/.antigravity/bridge/logs/bridge.log` |
| Recent journal | `journalctl --user -u antigravity-telegram-bridge.service -n 100 --no-pager` |
| Validate config + tools | `echo '{"action":"setup"}' \| .venv/bin/python -m src.control` |
| Health | `curl http://127.0.0.1:9100/health` |
| Metrics | `curl http://127.0.0.1:9100/metrics` |

The health/metrics port defaults to **9100** to avoid conflicting with the
Kimi bridge on 9099. Set `AGY_BRIDGE_HEALTH_PORT` in the systemd unit to
override.

### From inside agy

```sh
agy -p "use the bridge tool with action=status"
agy -p "use the bridge tool with action=logs and lines=200"
agy -p "use the bridge tool with action=setup"
```

The tool returns `{"ok": bool, "output": str}`; `agy` paraphrases it.

### Reload after config edits

```sh
systemctl --user restart antigravity-telegram-bridge.service
```

The daemon re-reads `config.json` only at startup.

### Log rotation

`bridge.log` grows unbounded. Add a logrotate rule at `/etc/logrotate.d/antigravity-telegram-bridge`:

```
/home/YOUR_USER/.antigravity/bridge/logs/bridge.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
```

---

## Troubleshooting

### "Bot not responding" — check the chain

```sh
# 1. Is the daemon running?
systemctl --user is-active antigravity-telegram-bridge.service

# 2. Recent log
journalctl --user -u antigravity-telegram-bridge.service -n 50 --no-pager

# 3. Validate config + tools
cd ~/.gemini/extensions/antigravity-telegram-bridge
echo '{"action":"setup"}' | .venv/bin/python -m src.control
```

### Common failure modes

#### "telegram getUpdates failed: Unauthorized"

The bot token is wrong or revoked. Re-paste from BotFather.

#### "agy CLI not found on PATH"

Either `agy` is not installed or systemd's PATH doesn't include it. The unit exports `PATH=$HOME/.local/bin:...`; if `agy` is elsewhere, edit the rendered unit and `daemon-reload`.

#### "agy error (exit ...)")

`agy` failed. Common causes:

- OAuth expired: run `agy` interactively to refresh.
- Model ID invalid: check `agy models` and update config or picker.
- Prompt too long or unsupported.

Check the full stderr in the log.

#### "agy timed out after 900.0s"

A single turn ran for 15 minutes and was killed. Either the prompt was unusually heavy or `agy` is stuck. Bump `AGY_TIMEOUT_S` in `src/turn.py` if your workload genuinely needs longer turns.

#### Bridge replies with `(empty reply)`

`agy` exited 0 but emitted no text. Check the actual prompt/response by running `agy` manually in the chat directory.

#### Telegram replies truncated

Single message exceeds 4096 chars. The bridge auto-splits at newline boundaries. If one paragraph is itself over 4096 chars, it hard-splits — ugly but never lost.

#### Leaked token in git history

Rotate the token in BotFather, update `config.json`, and scrub history:

```sh
git filter-repo --path config.json --invert-paths
git push --force-with-lease
```

---

## Upgrading

```sh
cd ~/.gemini/extensions/antigravity-telegram-bridge
git pull
bash install.sh
systemctl --user restart antigravity-telegram-bridge.service
```

`install.sh` is idempotent — it refreshes deps and re-renders the systemd unit.

---

## Uninstall

Complete removal:

```sh
systemctl --user disable --now antigravity-telegram-bridge.service
rm -f ~/.config/systemd/user/antigravity-telegram-bridge.service
systemctl --user daemon-reload
rm -rf ~/.gemini/extensions/antigravity-telegram-bridge ~/.antigravity/bridge
```

To keep the code but stop running:

```sh
systemctl --user disable --now antigravity-telegram-bridge.service
```

---

## Pushing to GitHub

The repo lives at `~/.gemini/extensions/antigravity-telegram-bridge/`. To publish:

1. Create the empty repository on GitHub. Do not initialize it with a README or LICENSE.
2. Add the remote and push:

```sh
cd ~/.gemini/extensions/antigravity-telegram-bridge
git remote add origin https://github.com/RosyidAl/Telegrav.git
git push -u origin main
```

3. The first push triggers GitHub Actions (`.github/workflows/test.yml`).

4. Tag a release:

```sh
git tag v0.2.0
git push origin v0.2.0
```

### Pre-push checklist

```sh
git ls-files | xargs grep -l 'bot_token\|api_key\|password' 2>/dev/null
# Should match only example + docs — never config.json with a real token.
git ls-files | grep -E '^config\.json$'
# Should be empty (config.json is gitignored).
```

---

## Architecture refresher

```
                               ┌─────────────────────────────┐
                               │  ~/.gemini/extensions/       │
                               │   antigravity-telegram-bridge/
                               │   antigravity-extension.json │   ← agy sees this plugin
       agy session ───────────▶│   src/control.py "bridge"    │     and can call its tool
                               │     tool (start/stop/...)    │
                               └────────────┬────────────────┘
                                            │ systemctl --user
                                            ▼
                               ┌─────────────────────────────┐
                               │  systemd-supervised daemon   │
                               │  python -m src.daemon        │
                               │                              │
       Telegram phone ───────▶ │  POLLING LOOP (never blocks) │
                               │  • polls getUpdates          │
                               │  • parse_update / callbacks  │
                               │  • is_authorized             │
                               │  • slash commands            │
                               │  • TurnQueue.submit (sync)   │
                               │           │                  │
                               │           ▼                  │
                               │  TURN WORKER (one task)      │── agy -p prompt
                               │  • runs turns FIFO, one at   │           --continue/--new-project
                               │    a time via run_agy        │
                               │  • graceful drain on stop    │
                               │                              │
                               │  state: ~/.antigravity/bridge│
                               │    state.json                │
                               └─────────────────────────────┘
```

The polling loop and turn execution are decoupled: while a turn runs (up to 15 min), the loop keeps fetching updates, so new messages — including `/stop` — are seen immediately and answered with queue feedback instead of waiting behind the running turn.

Code lives in the plugin dir; mutable runtime state lives in `~/.antigravity/bridge/`.

For full architectural detail see [`design.md`](design.md).

---

## Index of source files

| Path | Responsibility |
|---|---|
| `src/config.py` | Parse + validate `config.json` |
| `src/state.py` | Atomic JSON state |
| `src/telegram.py` | Pure helpers + async `TelegramClient` |
| `src/agy_runner.py` | Build argv and spawn `agy` |
| `src/commands.py` | Slash commands + inline-keyboard callbacks |
| `src/turn.py` | Per-turn execution with typing heartbeat |
| `src/daemon.py` | Poll/webhook loop + turn worker |
| `src/control.py` | Plugin-tool dispatcher |
| `src/health.py` | Health + metrics HTTP server |
| `src/media.py` | Photo/document/inbox handling |
| `src/queue.py` | FIFO turn queue (submit/get, one worker) |
| `src/webhook.py` | Webhook receiver + HMAC verification |

Test coverage: **220 tests**, runs in under 30 seconds.
