"""Loader and validator for config.json."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when config.json is missing, malformed, or fails validation."""


_MODEL_RE = re.compile(r"^[a-zA-Z0-9._][a-zA-Z0-9._\-]*$")
_VALID_MODES = frozenset({"code", "plan"})


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    allowed_user_ids: list[int]
    allowed_chat_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class AgyConfig:
    chats_root: str = ""
    default_workdir: str = ""
    model: str = ""
    mode: str = "code"  # "code" (auto) | "plan" (read-only sandbox)


@dataclass
class CategoryConfig:
    mime_types: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    max_size_bytes: int = 52_428_800
    routing: str = "block"  # pass | warn | hold | block


@dataclass
class MediaSafetyConfig:
    max_photo_bytes: int = 20_971_520
    max_file_bytes: int = 52_428_800
    categories: dict[str, CategoryConfig] = field(default_factory=dict)
    default_routing: str = "block"


@dataclass
class QueueConfig:
    max_depth: int = 10
    max_per_user: int = 5
    cooldown_seconds: int = 2


@dataclass
class MemoryConfig:
    limit_bytes: int = 805_306_368
    check_interval_loops: int = 30


@dataclass
class InboxConfig:
    max_age_hours: int = 24
    max_total_bytes: int = 524_288_000


@dataclass
class SafetyConfig:
    media: MediaSafetyConfig = field(default_factory=MediaSafetyConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    inbox: InboxConfig = field(default_factory=InboxConfig)


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    agy: AgyConfig
    safety: SafetyConfig = field(default_factory=SafetyConfig)


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config not found at {path}")

    try:
        raw: dict[str, Any] = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc

    tg_raw = raw.get("telegram") or {}
    import os
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    bot_token = ""
    if cred_dir:
        token_file = Path(cred_dir) / "tg_bot_token"
        if token_file.exists():
            bot_token = token_file.read_text().strip()
            
    if not bot_token:
        bot_token = os.environ.get("AGY_TELEGRAM_BOT_TOKEN") or ""
        
    if not bot_token:
        bot_token = tg_raw.get("bot_token") or ""
        
    if not isinstance(bot_token, str) or not bot_token:
        raise ConfigError("telegram.bot_token must be a non-empty string")

    allowed_user_ids = tg_raw.get("allowed_user_ids") or []
    if not isinstance(allowed_user_ids, list) or not allowed_user_ids:
        raise ConfigError(
            "telegram.allowed_user_ids must be a non-empty list (default-deny)"
        )
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in allowed_user_ids):
        raise ConfigError("telegram.allowed_user_ids entries must be integers")

    allowed_chat_ids = tg_raw.get("allowed_chat_ids") or []
    if not isinstance(allowed_chat_ids, list) or not all(
        isinstance(x, int) and not isinstance(x, bool) for x in allowed_chat_ids
    ):
        raise ConfigError("telegram.allowed_chat_ids must be a list of integers")

    a_raw = raw.get("agy") or {}

    model = str(a_raw.get("model") or "")
    if model and not _MODEL_RE.match(model):
        raise ConfigError(
            "agy.model must match ^[a-zA-Z0-9._][a-zA-Z0-9._-]*$ "
            "(no leading dash, no spaces) to be argv-safe"
        )

    mode = a_raw.get("mode")
    if mode is None:
        mode = "code"
    if not isinstance(mode, str) or mode not in _VALID_MODES:
        raise ConfigError(
            f"agy.mode must be one of {sorted(_VALID_MODES)}, got {mode!r}"
        )

    return Config(
        telegram=TelegramConfig(
            bot_token=bot_token,
            allowed_user_ids=list(allowed_user_ids),
            allowed_chat_ids=list(allowed_chat_ids),
        ),
        agy=AgyConfig(
            chats_root=str(a_raw.get("chats_root") or ""),
            default_workdir=str(a_raw.get("default_workdir") or ""),
            model=model,
            mode=mode,
        ),
        safety=_parse_safety(raw.get("safety")),
    )


def _parse_safety(raw: dict | None) -> SafetyConfig:
    if not raw:
        return SafetyConfig()
    cats: dict[str, CategoryConfig] = {}
    for cat_name, cat_raw in (raw.get("media", {}).get("categories", {}) or {}).items():
        cats[cat_name] = CategoryConfig(
            mime_types=list(cat_raw.get("mime_types", [])),
            extensions=list(cat_raw.get("extensions", [])),
            max_size_bytes=int(cat_raw.get("max_size_bytes", 52_428_800)),
            routing=str(cat_raw.get("routing", "block")),
        )
    media = MediaSafetyConfig(
        max_photo_bytes=int(raw.get("media", {}).get("max_photo_bytes", 20_971_520)),
        max_file_bytes=int(raw.get("media", {}).get("max_file_bytes", 52_428_800)),
        categories=cats,
        default_routing=str(raw.get("media", {}).get("default_routing", "block")),
    )
    queue = QueueConfig(
        max_depth=int(raw.get("queue", {}).get("max_depth", 10)),
        max_per_user=int(raw.get("queue", {}).get("max_per_user", 5)),
        cooldown_seconds=int(raw.get("queue", {}).get("cooldown_seconds", 2)),
    )
    memory = MemoryConfig(
        limit_bytes=int(raw.get("memory", {}).get("limit_bytes", 805_306_368)),
        check_interval_loops=int(raw.get("memory", {}).get("check_interval_loops", 30)),
    )
    inbox = InboxConfig(
        max_age_hours=int(raw.get("inbox", {}).get("max_age_hours", 24)),
        max_total_bytes=int(raw.get("inbox", {}).get("max_total_bytes", 524_288_000)),
    )
    return SafetyConfig(media=media, queue=queue, memory=memory, inbox=inbox)
