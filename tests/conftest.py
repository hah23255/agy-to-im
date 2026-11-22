"""Shared pytest fixtures."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def sample_config_dict() -> dict[str, Any]:
    """A valid config.json structure used across tests."""
    return {
        "telegram": {
            "bot_token": "1234:abcXYZ",
            "allowed_user_ids": [99999999],
            "allowed_chat_ids": [],
        },
        "agy": {
            "chats_root": "",
            "default_workdir": "/tmp",
            "model": "",
            "mode": "code",
        },
    }


@pytest.fixture(autouse=True)
def _isolate_agy_catalog(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep unit tests deterministic and offline by isolating agy_catalog."""
    import src.agy_catalog as agy_catalog

    async def _disabled_runner(args: list[str], timeout: float) -> tuple[int, str, str]:
        return 1, "", "agy subprocess disabled in unit tests"

    agy_catalog.clear_caches()
    monkeypatch.setattr(agy_catalog, "_run_agy_subprocess", _disabled_runner)
    yield
    agy_catalog.clear_caches()


@pytest.fixture
def config_file(tmp_path: Path, sample_config_dict: dict[str, Any]) -> Path:
    """Write sample_config_dict to a temp config.json and return the path."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps(sample_config_dict))
    return p
