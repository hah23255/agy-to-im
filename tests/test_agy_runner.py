"""Tests for src.agy_runner — subprocess spawn and argv construction."""
from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest

from src.agy_runner import AgyResult, run_agy


@pytest.fixture
def fake_agy(tmp_path: Path) -> Path:
    """Create an executable shell stub that echoes a plain-text reply."""
    script = tmp_path / "agy"
    script.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            cat > /dev/null
            echo 'fake reply'
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


async def test_run_agy_returns_stdout_text(fake_agy: Path, tmp_path: Path) -> None:
    result = await run_agy(
        prompt="hello",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(fake_agy),
    )
    assert isinstance(result, AgyResult)
    assert result.exit_code == 0
    assert result.text.strip() == "fake reply"


async def test_run_agy_surfaces_nonzero_exit(tmp_path: Path) -> None:
    script = tmp_path / "agy-fail"
    script.write_text("#!/bin/sh\necho boom 1>&2\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    result = await run_agy(
        prompt="x",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(script),
    )
    assert result.exit_code == 1
    assert "boom" in result.stderr
    assert result.text == ""


async def test_run_agy_builds_argv(tmp_path: Path) -> None:
    captured = tmp_path / "argv.txt"
    script = tmp_path / "agy-record"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            cat > /dev/null
            printf '%s\\n' "$@" > "{captured}"
            echo ok
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    workdir = tmp_path / "work"
    workdir.mkdir()
    await run_agy(
        prompt="hi",
        chat_dir=str(workdir),
        has_session=True,
        model="gemini-2.5-pro",
        mode="plan",
        agy_path=str(script),
    )
    args = captured.read_text().splitlines()
    assert "-p" in args and "hi" in args
    assert "--continue" in args
    assert "--model" in args and "gemini-2.5-pro" in args
    assert "--dangerously-skip-permissions" in args
    assert "--sandbox" in args
    assert "--print-timeout" in args


async def test_run_agy_uses_new_project_for_fresh_session(tmp_path: Path) -> None:
    captured = tmp_path / "argv.txt"
    script = tmp_path / "agy-record"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            cat > /dev/null
            printf '%s\\n' "$@" > "{captured}"
            echo ok
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    workdir = tmp_path / "work"
    workdir.mkdir()
    await run_agy(
        prompt="hi",
        chat_dir=str(workdir),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(script),
    )
    args = captured.read_text().splitlines()
    assert "--new-project" in args
    assert "--continue" not in args


async def test_run_agy_returns_timeout_result(tmp_path: Path) -> None:
    script = tmp_path / "agy-hang"
    script.write_text("#!/bin/sh\ncat > /dev/null\nsleep 5\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    result = await run_agy(
        prompt="x",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(script),
        timeout=0.3,
    )
    assert result.exit_code == 124
    assert "timed out" in result.stderr
    assert result.text == ""


async def test_run_agy_no_timeout_by_default(fake_agy: Path, tmp_path: Path) -> None:
    result = await run_agy(
        prompt="x",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(fake_agy),
    )
    assert result.exit_code == 0
    assert result.text.strip() == "fake reply"


def test_build_args_plan_sandbox() -> None:
    from src.agy_runner import _build_args

    args = _build_args(
        agy_path="/usr/bin/agy",
        prompt="run-security-scan",
        has_session=False,
        model="gemini-2.5-pro",
        mode="plan",
        print_timeout="15m",
        chat_dir="/home/i/chat_workspace",
    )

    assert args[0] == "/usr/bin/agy"
    assert "--mode" in args and "plan" in args
    assert "--sandbox" in args
    assert "-p" in args and "run-security-scan" in args


def test_build_args_effort() -> None:
    from src.agy_runner import _build_args

    args = _build_args(
        agy_path="/usr/bin/agy",
        prompt="hello",
        has_session=False,
        model="",
        mode="code",
        print_timeout="15m",
        chat_dir="/tmp/test",
        effort="high",
    )
    # agy rejects a standalone --effort when no effort-capable model is
    # selected (default model conflicts with it), so nothing may be emitted.
    assert "--effort" not in args
    assert "high" not in args


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("gemini-3.8-flash", "", ["--model", "gemini-3.8-flash-low"]),
        ("gemini-3.8-flash", "medium", ["--model", "gemini-3.8-flash-medium"]),
        ("gemini-3.8-flash", "high", ["--model", "gemini-3.8-flash-high"]),
        ("gemini-3.8-flash", "max", ["--model", "gemini-3.8-flash-high"]),
        ("claude-sonnet-4-6", "", ["--model", "claude-sonnet-4-6"]),
        # agy rejects --effort for Claude ("not supported for model") — the
        # per-chat effort must never leak into the argv for such models.
        ("claude-sonnet-4-6", "high", ["--model", "claude-sonnet-4-6"]),
        ("claude-opus-4-6-thinking", "", ["--model", "claude-opus-4-6-thinking"]),
        ("gpt-oss-120b-medium", "", ["--model", "gpt-oss-120b-medium"]),
        # Suffixed IDs reject a conflicting --effort ("conflicts with --effort=high").
        ("gemini-3.8-flash-low", "high", ["--model", "gemini-3.8-flash-low"]),
        # No model selected: effort alone cannot be applied safely.
        ("", "high", []),
        ("", "", []),
    ],
)
def test_resolve_model_args(model: str, effort: str, expected: list[str]) -> None:
    from src.agy_runner import _resolve_model_args

    assert _resolve_model_args(model, effort) == expected


def test_abort_turn_when_not_running() -> None:
    from src.agy_runner import abort_turn

    assert abort_turn("/nonexistent/chat/dir") is False



async def test_run_agy_cancellation_kills_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling run_agy must kill the child subprocess, not orphan it."""
    import asyncio
    import sys

    import src.agy_runner as ar

    procs: list[object] = []
    real_exec = asyncio.create_subprocess_exec

    async def recording_exec(*args: object, **kwargs: object):
        p = await real_exec(*args, **kwargs)
        procs.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
    monkeypatch.setattr(
        ar, "_build_args",
        lambda **kwargs: [sys.executable, "-c", "import time; time.sleep(30)"],
    )

    task = asyncio.create_task(run_agy(
        prompt="hi", chat_dir=str(tmp_path), has_session=False,
        model="", mode="code", agy_path=sys.executable,
    ))
    for _ in range(100):
        if procs:
            break
        await asyncio.sleep(0.02)
    assert procs, "child process should have been spawned"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.2)
    assert procs[0].returncode is not None, "child must be killed on cancellation"


async def test_run_agy_captures_result_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stream-json result event carries token usage — run_agy must expose it."""
    import sys

    import src.agy_runner as ar

    script = tmp_path / "fake_agy.py"
    script.write_text(
        "import json, sys\n"
        "json.dump({'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'ok', "
        "'usage': {'input_tokens': 10, 'output_tokens': 2, 'thinking_tokens': 0, "
        "'cache_read_tokens': 0, 'total_tokens': 12}}}, sys.stdout)\n"
        "print()\n"
    )
    monkeypatch.setattr(ar, "_build_args", lambda **kwargs: [sys.executable, str(script)])

    res = await run_agy(
        prompt="hi", chat_dir=str(tmp_path), has_session=False,
        model="", mode="code", agy_path=sys.executable,
    )
    assert res.exit_code == 0
    assert res.usage == {
        "input_tokens": 10, "output_tokens": 2, "thinking_tokens": 0,
        "cache_read_tokens": 0, "total_tokens": 12,
    }
