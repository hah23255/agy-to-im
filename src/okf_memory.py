"""OKF Memory Layer — bridges agy-to-im state to the OKF v0.1 knowledge catalog.

The agy CLI reads from its project working directory. By maintaining an OKF
bundle (index.md + concepts/) in the agy workspace, the LLM can reference
persistent knowledge via progressive disclosure — loading only relevant
concept files instead of the entire state.

Wire points:
  - attach_memory(chat_dir, state) → writes OKF bundle to chat_dir/okf_memory/
  - validate_memory(chat_dir)       → checks OKF conformance
  - memory_prompt()                  → returns a prompt fragment directing agy
                                      to read the memory catalog
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.okf_validator import validate_okf_bundle

MEMORY_DIR = "okf_memory"
INDEX_FILE = "index.md"
LOG_FILE = "log.md"
CONCEPTS_DIR = "concepts"


def _okf_timestamp() -> str:
    """Return the OKF timestamp string used in memory files."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def attach_memory(chat_dir: str, state: Any | None = None, bridge_version: str = "") -> Path:
    """Write the current bridge state as an OKF v0.1 bundle in the agy workspace.

    The bundle is placed at {chat_dir}/okf_memory/ and consists of:
      - index.md   — master catalog of all available memory concepts
      - log.md     — change history
      - concepts/  — individual concept files

    Returns the path to the memory root.
    """
    root = Path(chat_dir) / MEMORY_DIR
    concepts_root = root / CONCEPTS_DIR
    concepts_root.mkdir(parents=True, exist_ok=True)

    ts = _okf_timestamp()

    # ── Build concepts from state ──────────────────────────────────────
    concept_files: list[dict[str, str]] = []

    # 1. Session state concept
    if state and hasattr(state, 'chats') and state.chats:
        chat_lines = []
        for chat_id, cs in state.chats.items():
            chat_lines.append(f"| {chat_id} | {cs.turn_count} turns | session={'active' if cs.has_session else 'fresh'} | mode={cs.mode or 'default'} |")
        session_md = f"""---
type: status
title: "Bridge Session State"
description: "Active chats and their agy session status"
tags: [bridge, session, state, runtime]
timestamp: {ts}
confidence: high
---

# Bridge Session State

## Active Chats
| Chat ID | Turns | Session | Mode |
|---|---|---|---|
{chr(10).join(chat_lines)}

*Last updated: {ts}*
"""
        (concepts_root / "bridge_state.md").write_text(session_md, encoding="utf-8")
        concept_files.append({"name": "Bridge Session State", "path": "concepts/bridge_state.md", "tags": "bridge, session"})

    # 2. Bridge configuration concept (redacted)
    cfg_md = f"""---
type: config
title: "Bridge Configuration"
description: "Agy bridge operational parameters (secrets redacted)"
tags: [bridge, config, operational]
timestamp: {ts}
confidence: high
---

# Bridge Configuration

## Identity
- Version: {bridge_version or '0.2.0'}
- Bot: @Gemini_to_im_bot
- Default mode: code
- Default model: (config default)

## Safety
- Media routing: configurable categories (pass/warn/hold/block)
- Memory ceiling: 768 MiB
- Queue depth: 10
- Rate limit: 5 msg / 2s window

*Last updated: {ts}*
"""
    (concepts_root / "bridge_config.md").write_text(cfg_md, encoding="utf-8")
    concept_files.append({"name": "Bridge Configuration", "path": "concepts/bridge_config.md", "tags": "bridge, config"})

    # 3. Health snapshot concept
    try:
        with open("/proc/self/statm") as f:
            rss_pages = int(f.read().split()[1]) if os.path.exists("/proc/self/statm") else 0
        rss_mb = (rss_pages * os.sysconf("SC_PAGE_SIZE")) // 1_048_576 if rss_pages else 0
    except Exception:
        rss_mb = 0

    health_md = f"""---
type: status
title: "System Health Snapshot"
description: "Memory, process, and system health at bundle generation time"
tags: [health, system, memory, status]
timestamp: {ts}
confidence: high
---

# System Health

## Bridge Process
- Resident memory: {rss_mb} MiB
- Memory limit: 768 MiB
- Healthy: {'yes' if rss_mb < 768 else 'NO — OVER LIMIT'}

## Session Summary
- Chats tracked: {len(state.chats) if hasattr(state, 'chats') else 0}

*Snapshot at: {ts}*
"""
    (concepts_root / "system_health.md").write_text(health_md, encoding="utf-8")
    concept_files.append({"name": "System Health Snapshot", "path": "concepts/system_health.md", "tags": "health, system"})

    # ── Write index.md (no frontmatter — OKF v0.1 rule) ────────────────
    rows = []
    for c in concept_files:
        rows.append(f"| [{c['name']}]({c['path']}) | {c['tags']} |")
    index_content = f"""# Agy Bridge Memory Catalog

This is the OKF v0.1 memory layer for the agy-to-im bridge. The agy CLI reads
this catalog to access persistent operational knowledge without consuming
context window on irrelevant state.

## Available Concepts
| Concept | Tags |
|---|---|
{chr(10).join(rows)}

*Generated: {ts}*
"""
    (root / INDEX_FILE).write_text(index_content, encoding="utf-8")

    # ── Write/append log.md ──────────────────────────────────────────
    log_entry = f"## {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n- Memory bundle regenerated ({len(concept_files)} concepts)\n"
    log_path = root / LOG_FILE
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8")
        log_path.write_text(existing + "\n" + log_entry, encoding="utf-8")
    else:
        log_path.write_text(f"# Memory Change Log\n\n{log_entry}", encoding="utf-8")

    return root


def validate_memory(chat_dir: str) -> tuple[bool, list[str]]:
    """Validate that the OKF memory bundle conforms to OKF v0.1."""
    root = Path(chat_dir) / MEMORY_DIR
    if not root.exists():
        return False, ["No OKF memory bundle found"]
    return validate_okf_bundle(str(root))


def memory_prompt() -> str:
    """Return a prompt fragment that tells agy to consult the OKF memory catalog.

    Append this to the user's first-turn prompt to give agy access to
    persistent bridge memory via progressive disclosure.
    """
    return (
        "\n\n[System: A persistent knowledge catalog is available at "
        f"./{MEMORY_DIR}/{INDEX_FILE}. It contains bridge state, "
        "configuration, and system health data. Read the index first, "
        "then navigate to relevant concept files on-demand. "
        "This uses OKF v0.1 progressive disclosure — only load "
        "the files you need.]"
    )


def get_memory_index(chat_dir: str) -> str | None:
    """Read the OKF memory index as plain text. Returns None if no bundle exists."""
    idx = Path(chat_dir) / MEMORY_DIR / INDEX_FILE
    if not idx.exists():
        return None
    return idx.read_text(encoding="utf-8")


# ── Emergency reset ──────────────────────────────────────────────────

def purge_memory(chat_dir: str) -> bool:
    """Remove the OKF memory bundle entirely. Returns True if anything was deleted."""
    root = Path(chat_dir) / MEMORY_DIR
    if not root.exists():
        return False
    shutil.rmtree(root)
    return True
