"""Render the pinned "Maintainer ledger" issue body.

The ledger also carries the monitor's cursors in a machine-readable marker
(``<!-- omni-maintainer:cursors:v1 {...} -->``). That is the only cache the
system keeps, and it lives in an issue body, so routines never push a
branch other than ``maintainer/*``. Losing it costs one duplicate read.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .preflight import Preflight

CURSOR_MARKER = "omni-maintainer:cursors:v1"
_CURSOR_RE = re.compile(r"<!--\s*omni-maintainer:cursors:v1\s+(\{.*?\})\s*-->", re.S)


def parse_cursors(body: str) -> dict[str, Any]:
    match = _CURSOR_RE.search(body or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def cursor_marker(cursors: dict[str, Any]) -> str:
    return f"<!-- {CURSOR_MARKER} {json.dumps(cursors, sort_keys=True)} -->"


def replace_cursors(body: str, cursors: dict[str, Any]) -> str:
    """Return ``body`` with the cursor marker replaced or appended."""
    marker = cursor_marker(cursors)
    if _CURSOR_RE.search(body or ""):
        return _CURSOR_RE.sub(lambda _m: marker, body, count=1)
    return (body or "").rstrip() + "\n\n" + marker + "\n"


def render_ledger(pre: Preflight, *, policy: dict[str, Any], waiting: list[dict[str, Any]],
                  recent_actions: list[str], rendered_at: datetime,
                  cursors: dict[str, Any] | None = None) -> str:
    names = policy["labels"]
    caps = policy["caps"]
    lines = [
        "## Maintainer ledger",
        "",
        f"_rendered {rendered_at.replace(microsecond=0).isoformat()}_",
        "",
        f"- paused: **{'yes' if pre.paused else 'no'}** (toggle with label `{names['paused']}` on this issue)",
        f"- merges today: {pre.merges_today}/{caps['merges_per_day']} · PRs opened today: {pre.prs_opened_today}/{caps['prs_per_day']}",
        f"- deploy hold: **{'yes' if pre.deploy_hold else 'no'}** · open canaries: {len(pre.open_canaries)} · open incidents: {len(pre.open_incidents)} · open rollbacks: {len(pre.open_rollbacks)}",
        "",
        "### Waiting for you",
    ]
    if waiting:
        for item in waiting:
            lines.append(f"- {item.get('repo')}#{item.get('number')} — {item.get('title')} ({item.get('why')})")
    else:
        lines.append("- nothing")
    lines += ["", "### Open canaries"]
    for issue in pre.open_canaries or []:
        lines.append(f"- #{issue.get('number')} {issue.get('title')}")
    if not pre.open_canaries:
        lines.append("- none")
    lines += ["", "### Open incidents / rollbacks"]
    for issue in (pre.open_incidents or []) + (pre.open_rollbacks or []):
        lines.append(f"- #{issue.get('number')} {issue.get('title')}")
    if not (pre.open_incidents or pre.open_rollbacks):
        lines.append("- none")
    lines += ["", "### Last actions"]
    for action in recent_actions[-10:]:
        lines.append(f"- {action}")
    if not recent_actions:
        lines.append("- none recorded")
    if pre.notes:
        lines += ["", "### Notes"] + [f"- {n}" for n in pre.notes]
    body = "\n".join(lines) + "\n"
    if cursors is not None:
        body = replace_cursors(body, cursors)
    return body
