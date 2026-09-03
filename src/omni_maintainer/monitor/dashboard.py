"""Read the reviewbot dashboards.

The payload shape is defined by omni-reviewbot ``dashboard.py::status_payload``
and ``ledger.py::dashboard_snapshot/dashboard_history``. This module only
fetches and digests it; every threshold lives in ``canary.py`` and
``cli.py`` so the read layer stays policy-free.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..gate.reads import parse_time

Opener = Callable[[str, float], tuple[int, bytes]]


def _default_opener(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "omni-maintainer/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed http URLs from policy
        return int(response.status), response.read()


@dataclass(frozen=True)
class Fetch:
    ok: bool
    latency_s: float
    payload: dict[str, Any] | None
    error: str = ""
    attempts: int = 1


def fetch_json(url: str, *, timeout: float, attempts: int, retry_seconds: float,
               opener: Opener = _default_opener, sleep: Callable[[float], None] = time.sleep) -> Fetch:
    """GET ``url`` up to ``attempts`` times; a non-JSON body counts as a failure."""
    last_error = ""
    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        try:
            status, body = opener(url, timeout)
            if status != 200:
                last_error = f"HTTP {status}"
            else:
                payload = json.loads(body.decode("utf-8", errors="replace"))
                if not isinstance(payload, dict):
                    last_error = "payload is not a JSON object"
                else:
                    return Fetch(True, time.monotonic() - started, payload, attempts=attempt)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            sleep(retry_seconds)
    return Fetch(False, time.monotonic() - started, None, last_error, attempts=attempts)


def status_url(base: str) -> str:
    return base.rstrip("/") + "/api/status"


def job_url(base: str, job_id: int) -> str:
    return base.rstrip("/") + f"/api/jobs/{int(job_id)}"


@dataclass(frozen=True)
class Digest:
    """The few facts the monitor keys on, normalized."""

    instance: str
    fetched_at: datetime
    latency_s: float
    ok: bool
    error: str
    maintenance: bool
    poll_interval_seconds: int
    last_scan_at: datetime | None
    counts: dict[str, int]
    review_failed: int
    gate_failures: int
    split_failed: int
    jobs: tuple[dict[str, Any], ...]
    history_days: tuple[dict[str, Any], ...]
    max_job_id: int
    post_mode: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "instance": self.instance,
            "fetched_at": self.fetched_at.isoformat(),
            "latency_s": round(self.latency_s, 2),
            "ok": self.ok,
            "error": self.error,
            "maintenance": self.maintenance,
            "poll_interval_seconds": self.poll_interval_seconds,
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "counts": dict(self.counts),
            "review_failed": self.review_failed,
            "gate_failures": self.gate_failures,
            "split_failed": self.split_failed,
            "max_job_id": self.max_job_id,
            "post_mode": self.post_mode,
        }


def digest(instance: str, fetch: Fetch, *, now: datetime | None = None) -> Digest:
    now = now or datetime.now(timezone.utc)
    if not fetch.ok or fetch.payload is None:
        return Digest(instance, now, fetch.latency_s, False, fetch.error or "unreachable",
                      False, 0, None, {}, 0, 0, 0, (), (), 0)
    p = fetch.payload
    counts = {str(k): int(v) for k, v in (p.get("counts") or {}).items() if isinstance(v, int)}
    review_stats = p.get("review_stats") or {}
    outcomes = review_stats.get("outcomes") or {}
    split = p.get("split") or {}
    attempts = split.get("attempts") or {}
    jobs = tuple(j for j in (p.get("jobs") or []) if isinstance(j, dict))
    history = tuple(d for d in ((p.get("history") or {}).get("days") or []) if isinstance(d, dict))
    metadata = p.get("metadata") or {}
    return Digest(
        instance=instance,
        fetched_at=now,
        latency_s=fetch.latency_s,
        ok=True,
        error="",
        maintenance=bool(p.get("maintenance")),
        poll_interval_seconds=int(p.get("poll_interval_seconds") or 120),
        last_scan_at=parse_time(metadata.get("last_scan_at")),
        counts=counts,
        review_failed=int(outcomes.get("failed") or 0),
        gate_failures=int(review_stats.get("gate_failures") or 0),
        split_failed=int(attempts.get("failed") or 0),
        jobs=jobs,
        history_days=history,
        max_job_id=max((int(j.get("id") or 0) for j in jobs), default=0),
        post_mode=str(p.get("post_mode") or ""),
    )


FAILED_STATUSES = frozenset({"failed", "blocked", "timeout"})
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def job_time(job: dict[str, Any], key: str = "updated_at") -> datetime | None:
    """Dashboard timestamps are naive UTC+8 strings (``ledger.py`` shifts
    them by +8 hours); normalize to aware UTC. Aware strings pass through."""
    raw = str(job.get(key) or "")
    stamp = parse_time(raw.replace(" ", "T"))
    if stamp is None:
        return None
    if raw.endswith("Z") or "+" in raw[10:] or raw[10:].count("-") > 0:
        return stamp
    return stamp - timedelta(hours=8)


def new_failed_jobs(jobs: tuple[dict[str, Any], ...] | list[dict[str, Any]], *,
                    after: datetime | None) -> list[dict[str, Any]]:
    """Failed-class jobs whose last update is after the watermark, oldest first.

    A watermark on ``updated_at`` rather than a job-id cursor: a job observed
    as running can fail later under the same id, and the dashboard lists the
    50 most recently *updated* jobs, not the 50 highest ids.
    """
    out = []
    for j in jobs:
        if str(j.get("status") or "") not in FAILED_STATUSES:
            continue
        stamp = job_time(j)
        if after is None or stamp is None or stamp > after:
            out.append(j)
    return sorted(out, key=lambda j: (job_time(j) or _EPOCH, int(j.get("id") or 0)))


def window_saturated(jobs: tuple[dict[str, Any], ...] | list[dict[str, Any]], *, since: datetime | None) -> bool:
    """True when every listed job updated after ``since``: older updates may
    have rolled out of the 50-job window unseen."""
    if since is None or not jobs:
        return False
    stamps = [job_time(j) for j in jobs]
    stamps = [s for s in stamps if s is not None]
    return bool(stamps) and min(stamps) > since


def watcher_dead(last_scan_at: datetime | None, *, poll_interval_seconds: int,
                 now: datetime, multiplier: float) -> bool:
    if last_scan_at is None:
        return True
    return (now - last_scan_at).total_seconds() > multiplier * max(poll_interval_seconds, 1)
