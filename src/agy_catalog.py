"""Live Antigravity CLI (`agy`) model catalog and subscription quota discovery."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from src.state import is_valid_model

LOG = logging.getLogger("antigravity_telegram_bridge")

EFFORT_ORDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
EFFORT_LABELS: dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
    "max": "Max",
}

CATALOG_TTL_S: float = 600.0
QUOTA_TTL_S: float = 60.0
_KILL_WAIT_S: float = 5.0

_VARIANT_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<effort>low|medium|high|xhigh|max)$")
_LABEL_EFFORT_RE = re.compile(r"\s*\((low|medium|high|xhigh|max|extra)\)\s*$", re.IGNORECASE)
_PCT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")

_FALLBACK_ENTRIES: tuple[tuple[str, str], ...] = (
    ("gemini-4-neon-low", "Gemini 4 Neon (Low)"),
    ("gemini-4-neon-medium", "Gemini 4 Neon (Medium)"),
    ("gemini-4-neon-high", "Gemini 4 Neon (High)"),
    ("gemini-4-neon-xhigh", "Gemini 4 Neon (XHigh)"),
    ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
    ("gemini-4-argon-medium", "Gemini 4 Argon (Medium)"),
    ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
    ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)"),
    ("gemini-4-hydrogen-medium", "Gemini 4 Hydrogen (Medium)"),
    ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)"),
    ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)"),
    ("gemini-pro-orchestrator-neon", "Orchestrator (Neon)"),
    ("gemini-pro-orchestrator-g1-pro", "Orchestrator (Argon)"),
    ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
    ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
    ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
    ("claude-opus-5-5-low", "Claude Opus 5.5 (Low)"),
    ("claude-opus-5-5-medium", "Claude Opus 5.5 (Medium)"),
    ("claude-opus-5-5-high", "Claude Opus 5.5 (High)"),
    ("claude-sonnet-5-5-low", "Claude Sonnet 5.5 (Low)"),
    ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)"),
    ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)"),
    ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
)


@dataclass(frozen=True)
class ModelEntry:
    """Single model entry as reported by `agy models`."""

    id: str
    label: str


@dataclass(frozen=True)
class ModelFamily:
    """Logical model family grouping effort variants or representing a fixed model."""

    id: str
    label: str
    efforts: tuple[str, ...] = ()
    variants: tuple[tuple[str, str], ...] = ()
    default_effort: str = ""

    def variant_slugs(self) -> frozenset[str]:
        """Return all concrete variant slugs belonging to this family."""
        return frozenset(slug for _, slug in self.variants)

    def matches_model(self, model_id: str) -> bool:
        """True when model_id is this family's base ID or one of its concrete variants."""
        if not model_id:
            return False
        return model_id == self.id or model_id in self.variant_slugs()

    def resolve_slug(self, effort: str = "") -> str:
        """Resolve a family + optional effort level to a concrete agy model slug."""
        if not self.efforts:
            return self.id
        vmap = dict(self.variants)
        if effort and effort in vmap:
            return vmap[effort]
        if effort and effort in EFFORT_ORDER:
            req_idx = EFFORT_ORDER.index(effort)
            candidates = [
                e for e in self.efforts if e in EFFORT_ORDER and EFFORT_ORDER.index(e) <= req_idx
            ]
            chosen = candidates[-1] if candidates else self.efforts[0]
            return vmap.get(chosen, f"{self.id}-{chosen}")
        if self.default_effort and self.default_effort in vmap:
            return vmap[self.default_effort]
        if "medium" in vmap:
            return vmap["medium"]
        first = self.efforts[0]
        return vmap.get(first, f"{self.id}-{first}")


@dataclass(frozen=True)
class ModelCatalog:
    """Subscription-aware catalog of available models and the CLI default model."""

    families: tuple[ModelFamily, ...]
    raw_models: tuple[ModelEntry, ...]
    default_model_id: str = ""
    default_model_label: str = ""
    source: str = "fallback"
    fetched_at: float = 0.0

    def find_family(self, model_id: str) -> ModelFamily | None:
        """Find the ModelFamily matching model_id (base or concrete variant)."""
        if not model_id:
            return None
        for fam in self.families:
            if fam.matches_model(model_id):
                return fam
        return None

    def all_valid_ids(self) -> frozenset[str]:
        """Return all accepted IDs (family bases, concrete variants, and raw entries)."""
        ids: set[str] = {entry.id for entry in self.raw_models}
        for fam in self.families:
            ids.add(fam.id)
            ids.update(fam.variant_slugs())
        return frozenset(ids)

    def concrete_slugs(self) -> frozenset[str]:
        """Return all concrete leaf model slugs from the catalog."""
        slugs: set[str] = {entry.id for entry in self.raw_models}
        for fam in self.families:
            if fam.efforts:
                slugs.update(fam.variant_slugs())
            else:
                slugs.add(fam.id)
        return frozenset(slugs)


@dataclass(frozen=True)
class QuotaBucket:
    """Single quota bucket (e.g. 5-hour or weekly remaining fraction)."""

    id: str
    name: str
    window: str
    remaining_fraction: float
    reset_time: str = ""
    description: str = ""


@dataclass(frozen=True)
class QuotaGroup:
    """Logical model pool sharing quota buckets."""

    name: str
    description: str = ""
    buckets: tuple[QuotaBucket, ...] = ()


@dataclass(frozen=True)
class QuotaSnapshot:
    """Snapshot of subscription model quotas and optional AI credits."""

    groups: tuple[QuotaGroup, ...]
    credits_remaining: int | float | None = None
    upgrade_uri: str = ""
    fetched_at: float = 0.0
    error: str = ""


def parse_models_output(stdout: str) -> list[ModelEntry]:
    """Parse `agy models` stdout into deduplicated ModelEntry records."""
    entries: list[ModelEntry] = []
    seen: set[str] = set()
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "\t" in line:
            parts = re.split(r"\t+", line, maxsplit=1)
        else:
            parts = re.split(r"\s{2,}", line, maxsplit=1)
        slug = parts[0].strip()
        label = parts[1].strip() if len(parts) > 1 and parts[1].strip() else slug
        if not is_valid_model(slug) or slug in seen:
            continue
        seen.add(slug)
        entries.append(ModelEntry(id=slug, label=label))
    return entries


def _clean_family_label(label: str) -> str:
    """Strip trailing effort parenthetical from a model display label."""
    cleaned = _LABEL_EFFORT_RE.sub("", label).strip()
    return cleaned or label


def group_model_families(entries: list[ModelEntry]) -> tuple[ModelFamily, ...]:
    """Group concrete effort-suffixed model entries into logical ModelFamily items."""
    order: list[str] = []
    grouped: dict[str, list[tuple[str, ModelEntry]]] = {}

    for entry in entries:
        match = _VARIANT_SUFFIX_RE.match(entry.id)
        key = match.group("base") if match else entry.id
        effort = match.group("effort") if match else ""
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append((effort, entry))

    families: list[ModelFamily] = []
    for key in order:
        items = grouped[key]
        effort_map = {eff: ent for eff, ent in items if eff}
        if len(items) >= 2 and len(effort_map) == len(items):
            ordered_efforts = tuple(e for e in EFFORT_ORDER if e in effort_map)
            variants = tuple((e, effort_map[e].id) for e in ordered_efforts)
            base_label = _clean_family_label(items[0][1].label)
            if key.startswith("gemini-3.8") and "low" in effort_map:
                default_effort = "low"
            elif "medium" in effort_map:
                default_effort = "medium"
            else:
                default_effort = ordered_efforts[0]
            families.append(
                ModelFamily(
                    id=key,
                    label=base_label,
                    efforts=ordered_efforts,
                    variants=variants,
                    default_effort=default_effort,
                )
            )
        else:
            for _, entry in items:
                families.append(ModelFamily(id=entry.id, label=entry.label))
    return tuple(families)


def build_fallback_catalog() -> ModelCatalog:
    """Build the built-in fallback catalog used when live `agy models` is unavailable."""
    entries = [ModelEntry(id=slug, label=label) for slug, label in _FALLBACK_ENTRIES]
    return ModelCatalog(
        families=group_model_families(entries),
        raw_models=tuple(entries),
        source="fallback",
        fetched_at=0.0,
    )


def parse_model_default_json(stdout: str) -> tuple[str, str]:
    """Parse `agy -p /model --output-format json` (or TSV) into (model_id, label)."""
    text = stdout.strip()
    if not text:
        return "", ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        entries = parse_models_output(text)
        return (entries[0].id, entries[0].label) if entries else ("", "")

    cmd_data = ((payload.get("command") or {}).get("data")) or {}
    model_id = str(cmd_data.get("id") or "").strip()
    label = str(cmd_data.get("label") or "").strip()
    if model_id and is_valid_model(model_id):
        return model_id, label or model_id

    resp_entries = parse_models_output(str(payload.get("response") or ""))
    if resp_entries:
        return resp_entries[0].id, resp_entries[0].label
    return "", ""


def _clamp_fraction(raw: object) -> float:
    """Clamp a numeric fraction into [0.0, 1.0]."""
    try:
        val = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, val))


def _parse_usage_tsv(tsv_text: str) -> tuple[QuotaGroup, ...]:
    """Fallback parser for tab-separated `/usage` response lines."""
    order: list[str] = []
    buckets_by_group: dict[str, list[QuotaBucket]] = {}
    for raw_line in tsv_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        group_name = parts[0].strip()
        bucket_name = parts[1].strip()
        pct_match = _PCT_RE.search(parts[2])
        if not group_name or not pct_match:
            continue
        fraction = _clamp_fraction(float(pct_match.group(1)) / 100.0)
        reset_time = parts[3].strip() if len(parts) > 3 else ""
        low_name = bucket_name.lower()
        if "5" in low_name or "five" in low_name or "hour" in low_name:
            window = "5h"
        elif "week" in low_name:
            window = "weekly"
        else:
            window = ""
        if group_name not in buckets_by_group:
            order.append(group_name)
            buckets_by_group[group_name] = []
        buckets_by_group[group_name].append(
            QuotaBucket(
                id=f"{group_name.lower().replace(' ', '-')}-{window or len(buckets_by_group[group_name])}",
                name=bucket_name,
                window=window,
                remaining_fraction=fraction,
                reset_time=reset_time,
            )
        )
    return tuple(
        QuotaGroup(name=gname, buckets=tuple(buckets_by_group[gname]))
        for gname in order
    )


def parse_usage_json(stdout: str) -> tuple[QuotaGroup, ...]:
    """Parse `agy -p /usage --output-format json` (or TSV) into QuotaGroup tuples."""
    text = stdout.strip()
    if not text:
        return ()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _parse_usage_tsv(text)

    cmd_data = ((payload.get("command") or {}).get("data")) or {}
    groups_raw = cmd_data.get("groups")
    if isinstance(groups_raw, list) and groups_raw:
        groups: list[QuotaGroup] = []
        for grp in groups_raw:
            if not isinstance(grp, dict):
                continue
            buckets: list[QuotaBucket] = []
            for b in grp.get("buckets") or []:
                if not isinstance(b, dict):
                    continue
                buckets.append(
                    QuotaBucket(
                        id=str(b.get("id") or ""),
                        name=str(b.get("name") or ""),
                        window=str(b.get("window") or ""),
                        remaining_fraction=_clamp_fraction(b.get("remaining_fraction", 0.0)),
                        reset_time=str(b.get("reset_time") or ""),
                        description=str(b.get("description") or ""),
                    )
                )
            groups.append(
                QuotaGroup(
                    name=str(grp.get("name") or ""),
                    description=str(grp.get("description") or ""),
                    buckets=tuple(buckets),
                )
            )
        if groups:
            return tuple(groups)

    return _parse_usage_tsv(str(payload.get("response") or ""))


def _parse_credits_tsv(tsv_text: str) -> tuple[int | float | None, str]:
    """Fallback parser for tab-separated `/credits` response lines."""
    credits_val: int | float | None = None
    upgrade_uri = ""
    for raw_line in tsv_text.splitlines():
        parts = raw_line.strip().split("\t", 1)
        if len(parts) != 2:
            continue
        key, val = parts[0].strip().lower(), parts[1].strip()
        if "credit" in key:
            try:
                num = float(val)
                credits_val = int(num) if num.is_integer() else num
            except ValueError:
                pass
        elif "upgrade" in key:
            upgrade_uri = val
    return credits_val, upgrade_uri


def parse_credits_json(stdout: str) -> tuple[int | float | None, str]:
    """Parse `agy -p /credits --output-format json` (or TSV) into (credits, upgrade_uri)."""
    text = stdout.strip()
    if not text:
        return None, ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _parse_credits_tsv(text)

    cmd_data = ((payload.get("command") or {}).get("data")) or {}
    if "remaining_credits" in cmd_data:
        raw_credits = cmd_data.get("remaining_credits")
        credits_val: int | float | None = None
        if isinstance(raw_credits, (int, float)) and not isinstance(raw_credits, bool):
            credits_val = int(raw_credits) if float(raw_credits).is_integer() else float(raw_credits)
        return credits_val, str(cmd_data.get("upgrade_uri") or "")

    return _parse_credits_tsv(str(payload.get("response") or ""))


def render_progress_bar(fraction: float, width: int = 10) -> str:
    """Render a compact unicode progress bar for a [0.0, 1.0] remaining fraction."""
    clamped = max(0.0, min(1.0, fraction))
    filled = round(clamped * width)
    return "█" * filled + "░" * (width - filled)


def format_reset_countdown(reset_time: str, now: datetime | None = None) -> str:
    """Format an ISO-8601 UTC reset timestamp as a compact human countdown."""
    raw = reset_time.strip()
    if not raw:
        return ""
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        target = datetime.fromisoformat(normalized)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
    except ValueError:
        return raw

    ref = now or datetime.now(timezone.utc)
    delta_s = int((target - ref).total_seconds())
    if delta_s <= 0:
        return "now"
    if delta_s < 60:
        return "<1m"
    total_mins = delta_s // 60
    days, rem_mins = divmod(total_mins, 24 * 60)
    hours, mins = divmod(rem_mins, 60)
    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    return f"{mins}m"


async def _default_run_agy_subprocess(args: list[str], timeout: float) -> tuple[int, str, str]:
    """Run an agy CLI subcommand asynchronously with bounded timeout and kill cleanup."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode if proc.returncode is not None else 0,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=_KILL_WAIT_S)
            except Exception as exc:
                LOG.warning("failed cleaning up timed-out agy subprocess %s: %s", args[:2], exc)
        return 124, "", f"timed out after {timeout}s"
    except asyncio.CancelledError:
        if proc is not None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=_KILL_WAIT_S)
            except Exception as exc:
                LOG.warning("failed cleaning up cancelled agy subprocess %s: %s", args[:2], exc)
        raise
    except OSError as exc:
        return 1, "", str(exc)


SubprocessRunner = Callable[[list[str], float], Awaitable[tuple[int, str, str]]]
_run_agy_subprocess: SubprocessRunner = _default_run_agy_subprocess

_CATALOG_CACHE: dict[str, ModelCatalog] = {}
_QUOTA_CACHE: dict[str, QuotaSnapshot] = {}
_CATALOG_LOCKS: dict[str, asyncio.Lock] = {}
_QUOTA_LOCKS: dict[str, asyncio.Lock] = {}


def _get_lock(pool: dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
    """Return or create an asyncio.Lock for the given cache key in the current loop."""
    lock = pool.get(key)
    if lock is None:
        lock = asyncio.Lock()
        pool[key] = lock
    return lock


def clear_caches() -> None:
    """Clear cached live model catalogs, quota snapshots, and locks."""
    _CATALOG_CACHE.clear()
    _QUOTA_CACHE.clear()
    _CATALOG_LOCKS.clear()
    _QUOTA_LOCKS.clear()


def get_cached_catalog(agy_path: str = "agy") -> ModelCatalog:
    """Return the cached live ModelCatalog if available, else the built-in fallback."""
    return _CATALOG_CACHE.get(agy_path) or build_fallback_catalog()


def get_cached_quota(agy_path: str = "agy") -> QuotaSnapshot | None:
    """Return the most recently cached live QuotaSnapshot, or None."""
    return _QUOTA_CACHE.get(agy_path)


async def fetch_models_catalog(
    agy_path: str = "agy",
    *,
    force_refresh: bool = False,
    timeout: float = 20.0,
) -> ModelCatalog:
    """Fetch the subscription-filtered model catalog and CLI default model."""
    now = time.time()
    cached = _CATALOG_CACHE.get(agy_path)
    if not force_refresh and cached is not None and (now - cached.fetched_at) < CATALOG_TTL_S:
        return cached

    async with _get_lock(_CATALOG_LOCKS, agy_path):
        now = time.time()
        cached = _CATALOG_CACHE.get(agy_path)
        if not force_refresh and cached is not None and (now - cached.fetched_at) < CATALOG_TTL_S:
            return cached

        agy_bin = shutil.which(agy_path) or agy_path
        models_res, default_res = await asyncio.gather(
            _run_agy_subprocess([agy_bin, "models"], timeout),
            _run_agy_subprocess(
                [agy_bin, "-p", "/model", "--output-format", "json", "--print-timeout", "15s"],
                timeout,
            ),
            return_exceptions=True,
        )

        entries: list[ModelEntry] = []
        if not isinstance(models_res, BaseException):
            rc, stdout, stderr = models_res
            if rc == 0:
                entries = parse_models_output(stdout)
            else:
                LOG.warning("agy models failed (rc=%s): %s", rc, stderr.strip())
        else:
            LOG.warning("agy models raised: %s", models_res)

        if not entries:
            return cached if cached is not None else build_fallback_catalog()

        default_id, default_label = "", ""
        if not isinstance(default_res, BaseException):
            d_rc, d_stdout, _ = default_res
            if d_rc == 0:
                default_id, default_label = parse_model_default_json(d_stdout)

        catalog = ModelCatalog(
            families=group_model_families(entries),
            raw_models=tuple(entries),
            default_model_id=default_id,
            default_model_label=default_label,
            source="live",
            fetched_at=time.time(),
        )
        _CATALOG_CACHE[agy_path] = catalog
        return catalog


async def fetch_quota_snapshot(
    agy_path: str = "agy",
    *,
    force_refresh: bool = False,
    timeout: float = 25.0,
) -> QuotaSnapshot:
    """Fetch live subscription quota buckets and AI credits via `agy -p /usage` and `/credits`."""
    now = time.time()
    cached = _QUOTA_CACHE.get(agy_path)
    if not force_refresh and cached is not None and (now - cached.fetched_at) < QUOTA_TTL_S:
        return cached

    async with _get_lock(_QUOTA_LOCKS, agy_path):
        now = time.time()
        cached = _QUOTA_CACHE.get(agy_path)
        if not force_refresh and cached is not None and (now - cached.fetched_at) < QUOTA_TTL_S:
            return cached

        agy_bin = shutil.which(agy_path) or agy_path
        usage_res, credits_res = await asyncio.gather(
            _run_agy_subprocess(
                [agy_bin, "-p", "/usage", "--output-format", "json", "--print-timeout", "20s"],
                timeout,
            ),
            _run_agy_subprocess(
                [agy_bin, "-p", "/credits", "--output-format", "json", "--print-timeout", "15s"],
                timeout,
            ),
            return_exceptions=True,
        )

        groups: tuple[QuotaGroup, ...] = ()
        err_msg = ""
        if isinstance(usage_res, BaseException):
            err_msg = str(usage_res)
            LOG.warning("agy /usage raised: %s", usage_res)
        else:
            u_rc, u_stdout, u_stderr = usage_res
            if u_rc == 0:
                groups = parse_usage_json(u_stdout)
                if not groups:
                    err_msg = "empty quota response"
            else:
                err_msg = u_stderr.strip() or f"exit {u_rc}"
                LOG.warning("agy /usage failed (rc=%s): %s", u_rc, err_msg)

        credits_val: int | float | None = None
        upgrade_uri = ""
        if not isinstance(credits_res, BaseException):
            c_rc, c_stdout, _ = credits_res
            if c_rc == 0:
                credits_val, upgrade_uri = parse_credits_json(c_stdout)

        if not groups:
            return QuotaSnapshot(
                groups=(),
                credits_remaining=credits_val,
                upgrade_uri=upgrade_uri,
                fetched_at=time.time(),
                error=err_msg or "quota unavailable",
            )

        snapshot = QuotaSnapshot(
            groups=groups,
            credits_remaining=credits_val,
            upgrade_uri=upgrade_uri,
            fetched_at=time.time(),
            error="",
        )
        _QUOTA_CACHE[agy_path] = snapshot
        return snapshot
