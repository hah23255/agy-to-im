# Deployment guide

A streamlined install-only guide for getting the Antigravity Telegram bridge running on a fresh host. The longer reference (with troubleshooting, upgrade, uninstall) is [`OPERATIONS.md`](OPERATIONS.md).

---

## Time estimate

About **10 minutes** the first time, including creating the Telegram bot. Subsequent installs on additional hosts are about 3 minutes.

## Prerequisites

| Requirement | Verify |
|---|---|
| Linux with `systemd --user` | `systemctl --user status` returns without error |
| Python ≥ 3.11 | `python3 --version` |
| `uv` ≥ 0.4 | `uv --version` (install: `curl -LsSf https://astral.sh/uv/install.sh \| sh`) |
| `agy` CLI on `PATH` | `agy --version` |
| Authenticated `agy` session | `~/.gemini/antigravity-cli/` exists after running `agy` once |
| Outbound HTTPS to `api.telegram.org` | `curl -sSI https://api.telegram.org/` returns `HTTP/2 200` |

The host does not need an inbound port unless you enable webhook mode. Long-poll mode is outbound only.

## Step 1 — Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`. Follow the prompts to pick a display name and a username ending in `bot`.
3. Copy the token BotFather replies with. Format: `<digits>:<base64-ish>`.
4. Recommended hardening while still in BotFather:
   - `/setprivacy` → choose your bot → `Disable`.
   - `/setjoingroups` → choose your bot → `Disable`.

## Step 2 — Find your Telegram user ID

Message [@userinfobot](https://t.me/userinfobot). It replies with your numeric user ID.

## Step 3 — Clone

```sh
git clone https://github.com/RosyidAl/agy-to-im.git \
  ~/projects/agy-to-im
mkdir -p ~/.gemini/extensions
ln -sfn ~/projects/agy-to-im ~/.gemini/extensions/antigravity-telegram-bridge
cd ~/projects/agy-to-im
```

The canonical development directory is `~/projects/agy-to-im`, symlinked to `~/.gemini/extensions/` where `agy` looks for extensions.

## Step 4 — Configure

```sh
cp config.example.json config.json
chmod 600 config.json
$EDITOR config.json
```

Set the required fields:

```json
{
  "telegram": {
    "bot_token": "PASTE_THE_TOKEN_FROM_BOTFATHER",
    "allowed_user_ids": [PASTE_YOUR_USER_ID_FROM_USERINFOBOT],
    "allowed_chat_ids": [PASTE_YOUR_USER_ID_AGAIN_TO_LOCK_TO_DM]
  },
  "agy": {
    "chats_root": "",
    "default_workdir": "",
    "model": "",
    "mode": "code"
  }
}
```

Pinning `allowed_chat_ids` to your user ID locks the bot to your DM (private chat id equals user id).

## Step 5 — Install

```sh
bash install.sh
```

The installer:

1. Creates `~/.antigravity/bridge/{logs,runtime,chats}/`.
2. Builds a Python venv at `.venv/` (skipped if already present).
3. Installs the project in editable mode via `uv pip install -e .`.
4. Renders the systemd user unit from the template.
5. Reloads the user systemd manager and enables the unit.

It does **not** start the service.

## Step 6 — Pre-flight check

```sh
echo '{"action":"setup"}' | .venv/bin/python -m src.control | python3 -m json.tool
```

Expected output includes `✓` lines for config, `agy` CLI, `agy` auth dir, and Telegram token.

## Step 7 — Start

```sh
systemctl --user start antigravity-telegram-bridge.service
systemctl --user is-active antigravity-telegram-bridge.service
```

Expected: `active`.

## Step 8 — End-to-end smoke test

From your phone, open the bot in Telegram and send `/start` or `hello`. Within ~10 seconds you should see the typing indicator, then an `agy` reply.

Check the log:

```sh
tail -n 5 ~/.antigravity/bridge/logs/bridge.log
```

You should see an INFO line per turn:

```
2026-06-26 ... INFO antigravity_telegram_bridge: turn chat=<id> cwd=... exit=0 ms=... reply_len=...
```

Send a follow-up like `what did I just say?`. The reply should reference the previous message — that's session continuity.

## Step 9 — (Optional) headless boot

```sh
sudo loginctl enable-linger $USER
```

Without lingering, the user systemd manager only runs while you have an active login session.

---

## Automated CI/CD Deployment (GitHub Actions)

This repository includes continuous integration and deployment (`.github/workflows/ci-cd.yml`):
- **On Pull Request & Branch Push**: Runs pytest automated tests across Python 3.11 and 3.12.
- **On Push to `main`**: Automatically deploys the latest version to the Azure VPS via SSH, updates Python dependencies, restarts the background service, and verifies the `/health` endpoint.

Required GitHub Repository Secrets:
- `AZURE_HOST`: VPS IP address or hostname.
- `AZURE_USER`: VPS SSH user.
- `AZURE_SSH_KEY`: Private SSH key authorized on the VPS.
- `AZURE_PORT`: SSH port (default: 22).

---

## Done

The bridge is operational. Day-to-day ops are in [`OPERATIONS.md`](OPERATIONS.md). Architecture rationale is in [`design.md`](design.md). Security notes are in [`SECURITY.md`](../SECURITY.md) and [`security-scan.md`](security-scan.md).

## What to do if something goes wrong

If the service fails to start:

```sh
systemctl --user status antigravity-telegram-bridge.service --no-pager
journalctl --user -u antigravity-telegram-bridge.service -n 50 --no-pager
```

If the bot doesn't reply:

```sh
tail -n 20 ~/.antigravity/bridge/logs/bridge.log
```

Most failures are surfaced by the `setup` self-test in step 6.
