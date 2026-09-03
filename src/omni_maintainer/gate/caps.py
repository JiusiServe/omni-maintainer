"""Daily caps, reconstructed from GitHub history rather than a cache.

A crash between an action and a state write must not let the next run act
again, so the counters are recomputed from merged-PR history and from PRs
authored by the routine identity on every evaluation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from .reads import parse_time


def utc_day_start(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def merges_today(merged_pulls: Iterable[dict[str, Any]], *, now: datetime,
                 merger_logins: Iterable[str]) -> int:
    """Count PRs merged since 00:00 UTC by any of ``merger_logins``."""
    start = utc_day_start(now)
    logins = {login for login in merger_logins}
    count = 0
    for pull in merged_pulls:
        merged_at = parse_time(pull.get("merged_at"))
        if merged_at is None or merged_at < start:
            continue
        merged_by = (pull.get("merged_by") or {}).get("login") or ""
        if merged_by in logins:
            count += 1
    return count


def prs_opened_today(pulls: Iterable[dict[str, Any]], *, now: datetime, author_login: str) -> int:
    start = utc_day_start(now)
    count = 0
    for pull in pulls:
        created = parse_time(pull.get("created_at"))
        if created is None or created < start:
            continue
        if ((pull.get("user") or {}).get("login") or "") == author_login:
            count += 1
    return count


def search_query_merged_today(repo: str, now: datetime) -> str:
    day = utc_day_start(now).date().isoformat()
    return f"repo:{repo} is:pr is:merged merged:>={day}"


def search_query_opened_today(repo: str, now: datetime, author_login: str) -> str:
    day = utc_day_start(now).date().isoformat()
    return f"repo:{repo} is:pr author:{author_login} created:>={day}"
