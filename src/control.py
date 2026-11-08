"""Plugin-tool entry point: start | stop | status | logs | setup."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SERVICE = "antigravity-telegram-bridge.service"
LOG_FILE = Path.home() / ".antigravity" / "bridge" / "logs" / "bridge.log"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    """Run a systemctl --user command for the bridge service."""
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True, check=False)


def _action_start() -> dict[str, Any]:
    """Handle the bridge start action."""
    res = _systemctl("start", SERVICE)
    ok = res.returncode == 0
    return {"ok": ok,
            "output": (res.stdout + res.stderr).strip() or ("started" if ok else "failed")}


def _action_stop() -> dict[str, Any]:
    """Handle the bridge stop action."""
    res = _systemctl("stop", SERVICE)
    return {"ok": res.returncode == 0,
            "output": (res.stdout + res.stderr).strip() or "stopped"}


def _action_status() -> dict[str, Any]:
    """Handle the bridge status action."""
    res = _systemctl("status", SERVICE, "--no-pager")
    return {"ok": res.returncode in (0, 3),
            "output": (res.stdout + res.stderr).strip()}


def _action_logs(lines: int = 50) -> dict[str, Any]:
    """Handle the bridge logs action."""
    if LOG_FILE.exists():
        text = LOG_FILE.read_text(errors="replace").splitlines()
        return {"ok": True, "output": "\n".join(text[-lines:])}
    res = subprocess.run(
        ["journalctl", "--user", "-u", SERVICE, "-n", str(lines), "--no-pager"],
        capture_output=True, text=True, check=False)
    return {"ok": res.returncode == 0,
            "output": res.stdout.strip() or res.stderr.strip()}


def _cfg_path() -> Path:
    """Resolve the config.json path used by the plugin tool."""
    return Path(__file__).resolve().parent.parent / "config.json"


def _check_config(report: list[str]) -> Any:
    """Verify config.json exists and parses."""
    try:
        from src.config import load_config
        return load_config(_cfg_path())
    except Exception as err:
        report.append(f"✗ config error: {err}")
        return None


def _check_agy(report: list[str]) -> None:
    """Verify the agy binary is resolvable."""
    try:
        k = subprocess.run(["agy", "--version"], capture_output=True, text=True, check=False)
        if k.returncode == 0:
            report.append(f"✓ agy CLI: {k.stdout.strip()}")
        else:
            report.append(f"✗ agy --version failed: {k.stderr.strip()}")
    except FileNotFoundError:
        report.append("✗ agy CLI not on PATH")


def _check_agy_auth(report: list[str]) -> None:
    # agy stores auth under ~/.gemini/antigravity-cli/. A lightweight proxy is
    # the presence of the app data dir plus an installation id.
    """Verify agy has an authenticated session."""
    auth_dir = Path.home() / ".gemini" / "antigravity-cli"
    if auth_dir.exists():
        report.append(f"✓ agy auth dir exists ({auth_dir})")
    else:
        report.append(
            f"✗ agy auth dir missing at {auth_dir} — run `agy` interactively once to log in"
        )


def _check_telegram(report: list[str], bot_token: str) -> None:
    """Verify Telegram credentials are configured."""
    try:
        import httpx
        r = httpx.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=10)
        d = r.json()
        if d.get("ok"):
            report.append(f"✓ telegram token valid (bot @{d['result'].get('username')})")
            from src.commands import BOT_COMMANDS
            try:
                r2 = httpx.post(
                    f"https://api.telegram.org/bot{bot_token}/setMyCommands",
                    json={"commands": BOT_COMMANDS},
                    timeout=10,
                )
                d2 = r2.json()
                if d2.get("ok"):
                    report.append(f"✓ registered {len(BOT_COMMANDS)} bot commands with Telegram")
                else:
                    report.append(f"⚠️ warning: failed to register bot commands: {d2.get('description')}")
            except Exception as cmd_err:
                report.append(f"⚠️ warning: registering bot commands failed: {cmd_err}")
        else:
            report.append(f"✗ telegram token rejected: {d.get('description')}")
    except Exception as err:
        report.append(f"✗ telegram check failed: {err}")


def _action_setup() -> dict[str, Any]:
    """Run the interactive setup checks for the plugin tool."""
    report = [f"config: {_cfg_path()}"]
    cfg = _check_config(report)
    if cfg is None:
        return {"ok": False, "output": "\n".join(report)}
    _check_agy(report)
    _check_agy_auth(report)
    _check_telegram(report, cfg.telegram.bot_token)
    ok = not any(line.startswith("✗") for line in report)
    return {"ok": ok, "output": "\n".join(report)}


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a plugin-tool action payload to its handler."""
    action = payload.get("action")
    handlers = {"start": _action_start, "stop": _action_stop,
                "status": _action_status, "setup": _action_setup}
    if action == "logs":
        return _action_logs(int(payload.get("lines") or 50))
    fn = handlers.get(action)
    if fn:
        return fn()
    return {"ok": False, "output": f"unknown action: {action!r}"}


def main() -> None:  # pragma: no cover
    """CLI entry point for the bridge plugin tool."""
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as err:
        print(json.dumps({"ok": False, "output": f"invalid JSON: {err}"}))
        sys.exit(1)
    print(json.dumps(handle(payload)))


if __name__ == "__main__":
    main()
