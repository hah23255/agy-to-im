# Security policy

## Reporting a vulnerability

If you find a security issue in `antigravity-telegram-bridge`, please **do not open a public GitHub issue**.

Instead, open a private security advisory at <https://github.com/RosyidAl/Telegrav/security/advisories/new>, or email the maintainer with the subject prefix `[security]`. Include:

- A clear description of the issue.
- Reproduction steps or a proof-of-concept.
- The affected commit SHA.
- The impact (information disclosure, RCE, auth bypass, etc.).

You should expect an initial acknowledgement within five working days.

## Scope

In scope:

- The Python source under `src/`.
- The systemd unit template (`systemd/antigravity-telegram-bridge.service.template`).
- The plugin manifest (`plugin.json`) and its declared `bridge` tool entry point (`src/control.py`).
- The installer (`install.sh`).

Out of scope (report upstream):

- The Kimi CLI itself (<https://github.com/MoonshotAI/kimi-cli>).
- Telegram's API, BotFather, or the Telegram client.
- The `httpx` library.
- The host operating system, systemd, or `uv`.

## Threat model

`antigravity-telegram-bridge` runs as a single-user daemon under `systemctl --user`. It has access to:

- A Telegram bot token (in `config.json`, mode 0600).
- A long-lived OAuth token belonging to the Antigravity CLI (in `~/.antigravity/credentials/`).
- Whatever filesystem and shell access the invoking user has.

Assumed trust boundaries:

| Trusted | Untrusted |
|---|---|
| The host OS and the local user account | Inbound Telegram messages (filtered by `allowed_user_ids`) |
| The `agy` CLI binary on `PATH` | Telegram's network path (mitigated by TLS) |
| `config.json` on disk (mode 0600) | Anything that fails token validation |

### Out-of-band assumptions

- The user keeps `config.json` private and gitignored. The repository's `.gitignore` excludes it; commits are checked in CI.
- The user does not invite the bot into untrusted Telegram groups. The default config sets `allowed_chat_ids: []` (any chat from an allowed user); operators are encouraged to set an explicit chat-id whitelist.
- Backups of `~/.antigravity/` are encrypted at rest by the user.

## Hardening already applied

The systemd unit ships with:

- `NoNewPrivileges=true`
- `PrivateTmp=true`
- `ProtectKernelTunables`, `ProtectKernelModules`, `ProtectControlGroups`
- `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`
- `LockPersonality`, `RestrictNamespaces`, `RestrictRealtime`, `RestrictSUIDSGID`
- `SystemCallArchitectures=native`, `SystemCallFilter=@system-service ~@privileged ~@resources`
- `UMask=0077`

The bridge:

- Defaults to deny — empty `allowed_user_ids` causes the loader to refuse to start.
- Validates that `allowed_user_ids` entries are integers (and rejects bools, which Python would otherwise accept as `int`).
- Validates persisted session ids on load, so a tampered `state.json` cannot inject argv flags into the `agy` subprocess.
- Suppresses `httpx`/`httpcore` INFO-level logging in both daemon and plugin-tool entry points so the bot token (which appears in URL paths) never reaches the log file.
- Bounds every `agy` invocation with a 900-second timeout so a hung subprocess cannot stall the bridge indefinitely.
- Surfaces a friendly user-facing message on timeout (exit code 124) instead of leaking the raw subprocess `stderr`.
- Runs a concurrent heartbeat task that refreshes the Telegram "typing" indicator and posts progress notices at 250s and 600s, so the user knows the bot is still working rather than hung.

## Periodic audits

A pre-publication audit was performed against commit `a12aeeb`. The findings and remediation status are documented in [`docs/security-scan.md`](docs/security-scan.md).

## Versioning

Security fixes will be tagged on `main` and noted in the release notes. There is no LTS branch; users are expected to track `main`.
