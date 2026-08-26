"""Edge-case tests for src/media.py."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from src.config import CategoryConfig, InboxConfig, MediaSafetyConfig, SafetyConfig
from src.media import (
    INBOX_DIR_NAME,
    build_media_prompt,
    clean_inbox,
    classify_file,
    list_inbox,
    save_to_inbox,
    _mime_matches,
)


class _FakeTG:
    def __init__(self, *, get_file_data: bytes = b"", get_file_error: Exception | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self._data = get_file_data
        self._error = get_file_error
        self.get_file_calls: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def get_file(self, file_id: str) -> bytes:
        self.get_file_calls.append(file_id)
        if self._error:
            raise self._error
        return self._data


class _FakeMsg:
    def __init__(
        self,
        text: str = "",
        chat_id: int = 42,
        photo: list[dict] | None = None,
        document: dict | None = None,
    ) -> None:
        self.text = text
        self.chat_id = chat_id
        self.user_id = 99
        self.photo = photo
        self.document = document


class _FakeState:
    def __init__(self, chat_dir: str = "/tmp", photo_enabled: bool = True) -> None:
        self.chat_dir = chat_dir
        self.photo_enabled = photo_enabled


def _fake_cfg(categories: dict[str, CategoryConfig] | None = None) -> Any:
    """Build a minimal Config-like object with safety config for tests."""
    if categories is None:
        categories = {
            "code": CategoryConfig(mime_types=["text/*"], extensions=[".py", ".sh"], routing="pass"),
            "documents": CategoryConfig(mime_types=["application/pdf"], extensions=[".pdf"], routing="pass"),
        }
    media = MediaSafetyConfig(categories=categories, default_routing="block")
    safety = SafetyConfig(media=media)
    # Minimal config-like object
    s = safety
    class Cfg:
        safety = s
    return Cfg()


# ── MIME matching ─────────────────────────────────────────────────


def test_mime_matches_exact() -> None:
    assert _mime_matches("text/plain", "text/plain") is True


def test_mime_matches_wildcard() -> None:
    assert _mime_matches("text/x-python", "text/*") is True


def test_mime_matches_wrong_type() -> None:
    assert _mime_matches("image/png", "text/*") is False


def test_mime_matches_no_wildcard_generic() -> None:
    assert _mime_matches("application/zip", "application/*") is True


# ── classify_file ─────────────────────────────────────────────────


def test_classify_mime_match() -> None:
    cats = {"code": CategoryConfig(mime_types=["text/*"], routing="pass")}
    assert classify_file("text/x-python", "main.py", 5000, cats) == ("code", "pass", "")


def test_classify_extension_fallback() -> None:
    cats = {"code": CategoryConfig(extensions=[".py"], routing="pass")}
    assert classify_file("application/octet-stream", "script.py", 5000, cats) == ("code", "pass", "")


def test_classify_too_large() -> None:
    cats = {"code": CategoryConfig(mime_types=["text/*"], max_size_bytes=1000, routing="pass")}
    assert classify_file("text/plain", "big.txt", 2000, cats)[1] == "block"


def test_classify_unknown_default_block() -> None:
    cats: dict[str, CategoryConfig] = {}
    assert classify_file("application/x-msdownload", "virus.exe", 5000, cats) == ("unknown", "block", "")


def test_classify_unknown_clean_routing() -> None:
    cats: dict[str, CategoryConfig] = {}
    assert classify_file("x/y", "f.bin", 100, cats, default_routing="warn") == ("unknown", "warn", "")


# ── save_to_inbox ─────────────────────────────────────────────────


def test_save_to_inbox_creates_dir(tmp_path: Path) -> None:
    path = save_to_inbox(str(tmp_path), "test.py", b"print(1)")
    assert path.exists()
    assert path.read_bytes() == b"print(1)"
    assert path.parent.name == INBOX_DIR_NAME


def test_save_to_inbox_unique_names(tmp_path: Path) -> None:
    a = save_to_inbox(str(tmp_path), "f.py", b"a")
    time.sleep(0.02)
    b = save_to_inbox(str(tmp_path), "g.py", b"b")
    assert a != b


def test_save_to_inbox_size_cap(tmp_path: Path) -> None:
    wd = str(tmp_path)
    save_to_inbox(wd, "big1.txt", b"x" * 1000, max_total_bytes=1500)
    save_to_inbox(wd, "big2.txt", b"y" * 1000, max_total_bytes=1500)
    # First file should have been purged
    inbox = Path(wd) / INBOX_DIR_NAME
    names = [f.name for f in inbox.iterdir()]
    assert len(names) == 1


# ── clean_inbox ───────────────────────────────────────────────────


def test_clean_inbox_nonexistent_dir() -> None:
    assert clean_inbox("/tmp/nonexistent-inbox-xyz") == 0


def test_clean_inbox_removes_old_files(tmp_path: Path) -> None:
    wd = str(tmp_path)
    path = save_to_inbox(wd, "old.py", b"x")
    os.utime(path, (time.time() - 25 * 3600, time.time() - 25 * 3600))
    removed = clean_inbox(wd, max_age_hours=24)
    assert removed == 1
    assert not path.exists()


# ── build_media_prompt ────────────────────────────────────────────


async def test_prompt_text_only() -> None:
    tg = _FakeTG()
    msg = _FakeMsg(text="hello")
    result = await build_media_prompt(msg, tg, _FakeState(), _fake_cfg())
    assert result == "hello"


async def test_prompt_photo_disabled() -> None:
    tg = _FakeTG()
    msg = _FakeMsg(text="", photo=[{"file_id": "abc", "file_size": 100}])
    state = _FakeState(photo_enabled=False)
    result = await build_media_prompt(msg, tg, state, _fake_cfg())
    assert result is None


async def test_prompt_photo_too_large() -> None:
    tg = _FakeTG()
    msg = _FakeMsg(photo=[{"file_id": "abc", "file_size": 30_000_000}])  # > 20MB default
    result = await build_media_prompt(msg, tg, _FakeState(), _fake_cfg())
    assert result is None
    assert any("too large" in s for _, s in tg.sent)


async def test_prompt_document_unsupported_blocked() -> None:
    tg = _FakeTG()
    msg = _FakeMsg(
        document={
            "file_id": "abc",
            "file_name": "bad.exe",
            "mime_type": "application/x-msdownload",
            "file_size": 100,
        }
    )
    result = await build_media_prompt(msg, tg, _FakeState(), _fake_cfg())
    assert result is None
    assert any("Blocked" in s for _, s in tg.sent)


async def test_prompt_document_passed(tmp_path: Path) -> None:
    tg = _FakeTG(get_file_data=b"contents")
    msg = _FakeMsg(
        text="check this",
        document={
            "file_id": "xyz",
            "file_name": "lib.py",
            "mime_type": "text/x-python",
            "file_size": 100,
        },
    )
    result = await build_media_prompt(msg, tg, _FakeState(str(tmp_path)), _fake_cfg())
    assert result is not None
    assert "check this" in result
    assert "[File:" in result


async def test_prompt_zip_warned(tmp_path: Path) -> None:
    cats = {"archive": CategoryConfig(mime_types=["application/zip"], extensions=[".zip"], routing="warn")}
    tg = _FakeTG(get_file_data=b"zipdata")
    msg = _FakeMsg(
        text="unpack",
        document={
            "file_id": "z1",
            "file_name": "bundle.zip",
            "mime_type": "application/zip",
            "file_size": 1000,
        },
    )
    result = await build_media_prompt(msg, tg, _FakeState(str(tmp_path)), _fake_cfg(cats))
    assert result is not None
    assert "unpack" in result
    assert any("Accepted with caution" in s for _, s in tg.sent)


async def test_prompt_download_error_reported(tmp_path: Path) -> None:
    tg = _FakeTG(get_file_error=RuntimeError("network down"))
    msg = _FakeMsg(
        document={
            "file_id": "xyz",
            "file_name": "lib.py",
            "mime_type": "text/x-python",
            "file_size": 100,
        }
    )
    result = await build_media_prompt(msg, tg, _FakeState(str(tmp_path)), _fake_cfg())
    assert result is None
    assert any("Download failed" in s for _, s in tg.sent)
