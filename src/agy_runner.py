"""Spawn the Antigravity CLI (`agy`) and capture its plain-text output.

agy print mode uses Go-style flags:
  agy -p "<prompt>" [--model <id>] [--continue | --new-project]
      [--dangerously-skip-permissions] [--sandbox]
      [--print-timeout <duration>]

Output is plain text/markdown on stdout; there is no stream-json mode.
"""
from __future__ import annotations

import asyncio
import gc
import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

# Safety cap on stdout capture — prevents unbounded memory growth from
# runaway agy output. 1MB is generous (typical replies are 1–5KB).
_STDOUT_CAP_BYTES = 524_288  # 512 KiB

# Reap any defunct child processes left by prior subprocess invocations
# (bwrap sandbox can leave orphaned grand-children).
def _reap_zombies() -> None:
    if not hasattr(os, "WNOHANG"):
        return  # POSIX-only API; Windows has no fork() and thus no zombies
    try:
        while True:
            pid, _status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except (ChildProcessError, OSError):
        pass


@dataclass(frozen=True)
class AgyResult:
    text: str
    exit_code: int
    stderr: str


_ACTIVE_PROC: dict[str, Any] = {}


def abort_turn(chat_dir: str) -> bool:
    """Abort an actively running agy process in chat_dir."""
    proc = _ACTIVE_PROC.get(chat_dir)
    if proc is not None:
        try:
            proc.kill()
            return True
        except Exception:
            pass
    return False


# Base model prefix -> (supported effort suffixes, default effort).
# agy rejects these base IDs without an effort (e.g. "--model claude-sonnet-5-5
# requires --effort (available: low, medium, high)"), so the bridge appends one.
_EFFORT_SUFFIX_MODELS: dict[str, tuple[tuple[str, ...], str]] = {
    "gemini-3.8": (("low", "medium", "high"), "low"),
}
_EFFORT_SUFFIXES: tuple[str, ...] = ("-low", "-medium", "-high", "-max")


def _resolve_model_args(model: str, effort: str) -> list[str]:
    """Map (model, effort) to agy CLI args.

    Base IDs listed in _EFFORT_SUFFIX_MODELS get an effort suffix appended;
    unsupported efforts (e.g. "max") are clamped to the highest supported tier.
    """
    if model and not model.endswith(_EFFORT_SUFFIXES):
        for prefix, (supported, default) in _EFFORT_SUFFIX_MODELS.items():
            if model.startswith(prefix):
                if not effort:
                    chosen = default
                elif effort in supported:
                    chosen = effort
                else:
                    chosen = supported[-1]
                return ["--model", f"{model}-{chosen}"]
    args: list[str] = []
    if model:
        args.extend(["--model", model])
    if effort:
        args.extend(["--effort", effort])
    return args


def _build_args(
    *,
    agy_path: str,
    prompt: str,
    has_session: bool,
    model: str,
    mode: str,
    print_timeout: str,
    chat_dir: str = "",
    effort: str = "",
) -> list[str]:
    import shutil
    resolved_agy = shutil.which(agy_path) or agy_path
    args = [resolved_agy, "-p", prompt]

    if has_session:
        args.append("--continue")
    else:
        args.append("--new-project")

    args.extend(_resolve_model_args(model, effort))

    args.append("--disable-slash-commands")
    args.append("--dangerously-skip-permissions")

    if mode == "plan":
        args.extend(["--mode", "plan", "--sandbox"])
    elif mode == "code":
        args.extend(["--mode", "accept-edits"])

    args.extend(["--print-timeout", print_timeout])
    args.extend(["--output-format", "stream-json"])
    return args


def _run_sync(
    args: list[str],
    cwd: str,
    timeout: float | None = None,
) -> AgyResult:
    """Synchronous fallback worker for subprocess invocation."""
    _reap_zombies()
    proc = None
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _ACTIVE_PROC[cwd] = proc
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            stdout_bytes, stderr_bytes = proc.communicate()
        else:
            stdout_bytes, stderr_bytes = b"", b""
        message = f"agy timed out after {timeout}s"
        partial_stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        stderr = f"{partial_stderr}\n{message}".strip() if partial_stderr else message
        gc.collect()
        return AgyResult(text="", exit_code=124, stderr=stderr)
    except Exception as exc:
        gc.collect()
        return AgyResult(text="", exit_code=1, stderr=str(exc))
    finally:
        _ACTIVE_PROC.pop(cwd, None)

    exit_code = proc.returncode if proc else 0
    if exit_code in (-9, -15, 137, 143):
        gc.collect()
        return AgyResult(text="🛑 agy execution was aborted.", exit_code=exit_code, stderr="Aborted")

    stdout = stdout_bytes
    truncated = False
    if len(stdout) > _STDOUT_CAP_BYTES:
        stdout = stdout[:_STDOUT_CAP_BYTES]
        truncated = True

    text = stdout.decode("utf-8", errors="replace")
    if truncated:
        text += "\n\n[output truncated at 512KiB]"

    stderr_out = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    gc.collect()
    return AgyResult(
        text=text,
        exit_code=exit_code,
        stderr=stderr_out,
    )


async def run_agy(
    prompt: str,
    *,
    chat_dir: str,
    has_session: bool,
    model: str,
    mode: str,
    agy_path: str,
    effort: str = "",
    timeout: float | None = None,
    print_timeout: str = "15m",
    on_event: Callable[[dict[str, Any]], Any] | None = None,
) -> AgyResult:
    """Run agy with stream-json output and optional event callback."""
    args = _build_args(
        agy_path=agy_path,
        prompt=prompt,
        has_session=has_session,
        model=model,
        mode=mode,
        effort=effort,
        print_timeout=print_timeout,
        chat_dir=chat_dir,
    )
    _reap_zombies()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=chat_dir,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _ACTIVE_PROC[chat_dir] = proc
    except Exception as exc:
        return AgyResult(text="", exit_code=1, stderr=str(exc))

    plain_text_lines: list[str] = []
    final_response = ""
    result_error = ""

    async def _read_stdout() -> None:
        nonlocal final_response, result_error
        if proc.stdout is None:
            return
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    event_type = data.get("event")
                    if event_type == "result":
                        res = data.get("result", {})
                        final_response = res.get("response", "")
                        result_error = res.get("error", "")
                    if on_event is not None:
                        cb_res = on_event(data)
                        if asyncio.iscoroutine(cb_res):
                            await cb_res
                    continue
                except Exception:
                    pass
            plain_text_lines.append(line)

    async def _read_stderr() -> str:
        if proc.stderr is None:
            return ""
        raw = await proc.stderr.read()
        return raw.decode("utf-8", errors="replace")

    try:
        stderr_task = asyncio.create_task(_read_stderr())
        stdout_task = asyncio.create_task(_read_stdout())
        if timeout:
            await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=timeout)
        else:
            await asyncio.gather(stdout_task, stderr_task)
        stderr_out = stderr_task.result()
    except asyncio.TimeoutError:
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return AgyResult(text="", exit_code=124, stderr=f"agy timed out after {timeout}s")
    except asyncio.CancelledError:
        # Cancellation must not orphan the child: stop it and wait for it to
        # exit BEFORE dropping the registry entry, then re-raise.
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        raise
    except Exception as exc:
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return AgyResult(text="", exit_code=1, stderr=str(exc))
    finally:
        _ACTIVE_PROC.pop(chat_dir, None)

    if proc is not None:
        await proc.wait()
    exit_code = proc.returncode if proc and proc.returncode is not None else 0
    if exit_code in (-9, -15, 137, 143):
        return AgyResult(text="🛑 agy execution was aborted.", exit_code=exit_code, stderr="Aborted")

    text = final_response if final_response else "\n".join(plain_text_lines)
    if len(text) > _STDOUT_CAP_BYTES:
        text = text[:_STDOUT_CAP_BYTES] + "\n\n[output truncated at 512KiB]"

    if result_error and result_error not in stderr_out:
        stderr_out = f"{stderr_out}\n{result_error}".strip() if stderr_out else result_error

    gc.collect()
    return AgyResult(
        text=text,
        exit_code=exit_code,
        stderr=stderr_out,
    )
