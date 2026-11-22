"""Tests for src.agy_catalog — subscription model discovery and quota parsing."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.agy_catalog import (
    EFFORT_LABELS,
    EFFORT_ORDER,
    ModelCatalog,
    ModelEntry,
    ModelFamily,
    QuotaBucket,
    QuotaGroup,
    QuotaSnapshot,
    build_fallback_catalog,
    clear_caches,
    fetch_models_catalog,
    fetch_quota_snapshot,
    format_reset_countdown,
    get_cached_catalog,
    get_cached_quota,
    group_model_families,
    parse_credits_json,
    parse_model_default_json,
    parse_models_output,
    parse_usage_json,
    render_progress_bar,
)

SAMPLE_MODELS_STDOUT = (
    "gemini-4-neon-high\tGemini 4 Neon (High)\n"
    "gemini-4-neon-xhigh\tGemini 4 Neon (XHigh)\n"
    "gemini-4-neon-medium\tGemini 4 Neon (Medium)\n"
    "gemini-4-neon-low\tGemini 4 Neon (Low)\n"
    "gemini-4-argon-medium\tGemini 4 Argon (Medium)\n"
    "gemini-4-argon-high\tGemini 4 Argon (High)\n"
    "gemini-4-argon-low\tGemini 4 Argon (Low)\n"
    "gemini-pro-orchestrator-neon\tOrchestrator (Neon)\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
    "claude-sonnet-5-5-low\tClaude Sonnet 5.5 (Low)\n"
    "claude-sonnet-5-5-medium\tClaude Sonnet 5.5 (Medium)\n"
    "claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)\n"
    "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
)


def test_parse_models_output_parses_and_deduplicates() -> None:
    raw = (
        "\n"
        "gemini-4-neon-low\tGemini 4 Neon (Low)\n"
        "gemini-4-neon-low\tGemini 4 Neon (Low)\n"
        "--bad-flag\tInvalid Flag\n"
        "gpt-oss-120b-medium   GPT-OSS 120B (Medium)\n"
    )
    entries = parse_models_output(raw)
    assert entries == [
        ModelEntry(id="gemini-4-neon-low", label="Gemini 4 Neon (Low)"),
        ModelEntry(id="gpt-oss-120b-medium", label="GPT-OSS 120B (Medium)"),
    ]


def test_group_model_families_groups_variants_and_keeps_fixed_models() -> None:
    entries = parse_models_output(SAMPLE_MODELS_STDOUT)
    families = group_model_families(entries)
    by_id = {f.id: f for f in families}

    assert "gemini-4-neon" in by_id
    neon = by_id["gemini-4-neon"]
    assert neon.label == "Gemini 4 Neon"
    assert neon.efforts == ("low", "medium", "high", "xhigh")
    assert neon.default_effort == "medium"
    assert neon.resolve_slug("") == "gemini-4-neon-medium"
    assert neon.resolve_slug("xhigh") == "gemini-4-neon-xhigh"
    assert neon.resolve_slug("max") == "gemini-4-neon-xhigh"
    assert neon.matches_model("gemini-4-neon")
    assert neon.matches_model("gemini-4-neon-xhigh")
    assert not neon.matches_model("gemini-4-argon-low")

    flash = by_id["gemini-3.8-flash"]
    assert flash.efforts == ("low", "medium", "high")
    assert flash.default_effort == "low"
    assert flash.resolve_slug("") == "gemini-3.8-flash-low"
    assert flash.resolve_slug("max") == "gemini-3.8-flash-high"

    orch = by_id["gemini-pro-orchestrator-neon"]
    assert orch.efforts == ()
    assert orch.resolve_slug("high") == "gemini-pro-orchestrator-neon"

    gpt = by_id["gpt-oss-120b-medium"]
    assert gpt.efforts == ()
    assert gpt.label == "GPT-OSS 120B (Medium)"
    assert gpt.resolve_slug("high") == "gpt-oss-120b-medium"


def test_build_fallback_catalog_has_modern_models() -> None:
    cat = build_fallback_catalog()
    assert cat.source == "fallback"
    assert cat.find_family("gemini-4-neon") is not None
    assert cat.find_family("claude-sonnet-5-5") is not None
    assert cat.find_family("claude-opus-5-5") is not None
    assert cat.find_family("gemini-3.8-flash") is not None
    assert "gemini-4-neon-xhigh" in cat.all_valid_ids()


def test_parse_model_default_json_and_tsv_fallback() -> None:
    payload = json.dumps({
        "status": "SUCCESS",
        "response": "gemini-4-argon-low\tGemini 4 Argon (Low)\n",
        "command": {
            "name": "model",
            "data": {"id": "gemini-4-argon-low", "label": "Gemini 4 Argon (Low)"},
        },
    })
    assert parse_model_default_json(payload) == ("gemini-4-argon-low", "Gemini 4 Argon (Low)")
    assert parse_model_default_json("gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n") == (
        "gemini-3.8-flash-low",
        "Gemini 3.8 Flash (Low)",
    )


def test_parse_usage_json_and_tsv_fallback() -> None:
    payload = json.dumps({
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "description": "Models within this group: Gemini 4 Neon, Gemini 3.8 Flash",
                        "buckets": [
                            {
                                "id": "gemini-weekly",
                                "name": "Weekly Limit Remaining",
                                "window": "weekly",
                                "remaining_fraction": 0.36,
                                "reset_time": "2026-11-28T08:52:02Z",
                            },
                            {
                                "id": "gemini-5h",
                                "name": "Five Hour Limit Remaining",
                                "window": "5h",
                                "remaining_fraction": 1.5,  # clamped to 1.0
                                "reset_time": "2026-11-22T16:22:04Z",
                            },
                        ],
                    }
                ]
            },
        },
    })
    groups = parse_usage_json(payload)
    assert len(groups) == 1
    assert groups[0].name == "Gemini Models"
    assert len(groups[0].buckets) == 2
    assert groups[0].buckets[0].remaining_fraction == pytest.approx(0.36)
    assert groups[0].buckets[1].remaining_fraction == pytest.approx(1.0)

    tsv = (
        "Gemini Models\tWeekly Limit Remaining\t36%\t2026-11-28T08:52:02Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t84%\t2026-11-22T16:22:04Z\n"
    )
    tsv_groups = parse_usage_json(tsv)
    assert len(tsv_groups) == 1
    assert len(tsv_groups[0].buckets) == 2
    assert tsv_groups[0].buckets[0].window == "weekly"
    assert tsv_groups[0].buckets[0].remaining_fraction == pytest.approx(0.36)
    assert tsv_groups[0].buckets[1].window == "5h"


def test_parse_credits_json_and_tsv_fallback() -> None:
    payload = json.dumps({
        "status": "SUCCESS",
        "command": {
            "name": "credits",
            "data": {"remaining_credits": 250, "upgrade_uri": "https://antigravity.google/g1-upgrade"},
        },
    })
    assert parse_credits_json(payload) == (250, "https://antigravity.google/g1-upgrade")
    assert parse_credits_json("Remaining credits\t0\nUpgrade\thttps://antigravity.google/g1-upgrade\n") == (
        0,
        "https://antigravity.google/g1-upgrade",
    )


def test_render_progress_bar_and_reset_countdown() -> None:
    assert render_progress_bar(0.0, width=10) == "░░░░░░░░░░"
    assert render_progress_bar(1.0, width=10) == "██████████"
    assert render_progress_bar(0.5, width=10) == "█████░░░░░"
    assert render_progress_bar(-0.2, width=5) == "░░░░░"
    assert render_progress_bar(1.8, width=5) == "█████"

    now = datetime(2026, 11, 22, 14, 0, 0, tzinfo=timezone.utc)
    assert format_reset_countdown("2026-11-28T08:00:00Z", now=now) == "5d 18h"
    assert format_reset_countdown("2026-11-22T16:13:00Z", now=now) == "2h 13m"
    assert format_reset_countdown("2026-11-22T14:45:00Z", now=now) == "45m"
    assert format_reset_countdown("2026-11-22T14:00:20Z", now=now) == "<1m"
    assert format_reset_countdown("2026-11-22T13:00:00Z", now=now) == "now"
    assert format_reset_countdown("") == ""
    assert format_reset_countdown("not-a-date") == "not-a-date"


async def test_fetch_models_catalog_uses_live_subprocess_and_ttl_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.agy_catalog as cat_mod

    monkeypatch.setattr(cat_mod, "_run_agy_subprocess", cat_mod._default_run_agy_subprocess)
    calls: list[list[str]] = []

    stub = tmp_path / "fake_agy.py"
    stub.write_text(
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['models']:\n"
        "    print('gemini-4-neon-low\\tGemini 4 Neon (Low)')\n"
        "    print('gemini-4-neon-xhigh\\tGemini 4 Neon (XHigh)')\n"
        "elif args[:2] == ['-p', '/model']:\n"
        "    print(json.dumps({'status': 'SUCCESS', 'command': {'name': 'model', 'data': {'id': 'gemini-4-neon-xhigh', 'label': 'Gemini 4 Neon (XHigh)'}}}))\n",
        encoding="utf-8",
    )

    real_runner = cat_mod._default_run_agy_subprocess

    async def via_python(args: list[str], timeout: float) -> tuple[int, str, str]:
        calls.append(args)
        return await real_runner([sys.executable, str(stub), *args[1:]], timeout)

    monkeypatch.setattr(cat_mod, "_run_agy_subprocess", via_python)

    cat1 = await fetch_models_catalog("fake-agy")
    assert cat1.source == "live"
    assert cat1.default_model_id == "gemini-4-neon-xhigh"
    assert cat1.find_family("gemini-4-neon") is not None
    assert get_cached_catalog("fake-agy").source == "live"
    assert len(calls) == 2

    # Second call hits TTL cache without spawning subprocesses
    cat2 = await fetch_models_catalog("fake-agy")
    assert cat2 is cat1
    assert len(calls) == 2

    # Force refresh bypasses TTL cache
    cat3 = await fetch_models_catalog("fake-agy", force_refresh=True)
    assert cat3.source == "live"
    assert len(calls) == 4


async def test_fetch_models_catalog_falls_back_when_cli_fails() -> None:
    clear_caches()
    cat = await fetch_models_catalog("nonexistent-agy")
    assert cat.source == "fallback"
    assert cat.find_family("gemini-4-neon") is not None


async def test_fetch_quota_snapshot_parses_live_and_caches_only_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.agy_catalog as cat_mod

    monkeypatch.setattr(cat_mod, "_run_agy_subprocess", cat_mod._default_run_agy_subprocess)
    stub = tmp_path / "fake_agy_quota.py"
    stub.write_text(
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['-p', '/usage']:\n"
        "    print(json.dumps({'status': 'SUCCESS', 'command': {'name': 'usage', 'data': {'groups': [{'name': 'Gemini Models', 'description': 'Models within this group: Gemini 4 Neon', 'buckets': [{'id': 'g-w', 'name': 'Weekly Limit Remaining', 'window': 'weekly', 'remaining_fraction': 0.5, 'reset_time': '2026-11-28T00:00:00Z'}]}]}}}))\n"
        "elif args[:2] == ['-p', '/credits']:\n"
        "    print(json.dumps({'status': 'SUCCESS', 'command': {'name': 'credits', 'data': {'remaining_credits': 42, 'upgrade_uri': 'https://example.test'}}}))\n",
        encoding="utf-8",
    )
    real_runner = cat_mod._default_run_agy_subprocess
    calls: list[list[str]] = []

    async def via_python(args: list[str], timeout: float) -> tuple[int, str, str]:
        calls.append(args)
        return await real_runner([sys.executable, str(stub), *args[1:]], timeout)

    monkeypatch.setattr(cat_mod, "_run_agy_subprocess", via_python)

    snap1 = await fetch_quota_snapshot("fake-agy")
    assert snap1.error == ""
    assert len(snap1.groups) == 1
    assert snap1.credits_remaining == 42
    assert get_cached_quota("fake-agy") is snap1
    assert len(calls) == 2

    snap2 = await fetch_quota_snapshot("fake-agy")
    assert snap2 is snap1
    assert len(calls) == 2
