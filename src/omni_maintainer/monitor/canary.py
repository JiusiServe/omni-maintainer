"""Canary records and tick evaluation.

A canary is a GitHub issue created by the ``canary-record.yml`` workflow on
every push to the reviewbot's ``main``. Its body carries a JSON record; the
monitor appends one tick comment per run and this module decides, from the
record plus all ticks so far, what happens next. The decision is a pure
function so every rule is unit-tested against recorded dashboards.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..gate.reads import parse_time
from .dashboard import Digest, watcher_dead

RECORD_MARKER = "omni-maintainer:canary:v1"
TICK_MARKER = "omni-maintainer:tick:v1"
_RECORD_RE = re.compile(r"<!--\s*omni-maintainer:canary:v1\s+(\{.*?\})\s*-->", re.S)
_TICK_RE = re.compile(r"<!--\s*omni-maintainer:tick:v1\s+(\{.*?\})\s*-->", re.S)

WAIT = "wait"            # deploy run not finished; do nothing
START = "start"          # deploy succeeded; record baseline, window starts now
TRIP = "trip"            # regression; open a rollback
CLOSE = "close"          # window passed clean
HOLD = "hold"            # deploy stuck waiting for approval; needs a human
DEPLOY_FAILED = "deploy_failed"  # server-deploy.sh already rolled back


@dataclass(frozen=True)
class Baseline:
    mean_attention: float
    max_attention: int
    mean_gate_failures: float
    counts_failed: int
    review_failed: int
    gate_failures: int
    split_failed: int
    last_scan_at: str | None
    started_at: str
    # Per-instance highest job id at window start. Jobs above it are "since
    # the deploy"; one shared number would let one busy instance hide the
    # other's failures, and an undeclared field would be lost on reload.
    max_job_ids: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["max_job_ids"] = dict(self.max_job_ids)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Baseline":
        raw_ids = data.get("max_job_ids") or {}
        return cls(
            mean_attention=float(data.get("mean_attention") or 0.0),
            max_attention=int(data.get("max_attention") or 0),
            mean_gate_failures=float(data.get("mean_gate_failures") or 0.0),
            counts_failed=int(data.get("counts_failed") or 0),
            review_failed=int(data.get("review_failed") or 0),
            gate_failures=int(data.get("gate_failures") or 0),
            split_failed=int(data.get("split_failed") or 0),
            last_scan_at=data.get("last_scan_at"),
            started_at=str(data.get("started_at") or ""),
            max_job_ids={str(k): int(v) for k, v in raw_ids.items()} if isinstance(raw_ids, dict) else {},
        )


@dataclass
class CanaryRecord:
    repo: str
    merge_sha: str
    pre_merge_sha: str
    pr_number: int | None
    deploy_run_id: int | None
    opened_at: str
    kind: str = "rb"
    baseline: Baseline | None = None
    status: str = "pending"  # pending | running | closed

    def to_marker(self) -> str:
        payload = {
            "repo": self.repo, "merge_sha": self.merge_sha, "pre_merge_sha": self.pre_merge_sha,
            "pr_number": self.pr_number, "deploy_run_id": self.deploy_run_id,
            "opened_at": self.opened_at, "kind": self.kind, "status": self.status,
            "baseline": self.baseline.to_json() if self.baseline else None,
        }
        return f"<!-- {RECORD_MARKER} {json.dumps(payload, sort_keys=True)} -->"

    @classmethod
    def parse(cls, body: str) -> "CanaryRecord | None":
        match = _RECORD_RE.search(body or "")
        if not match:
            return None
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        baseline = data.get("baseline")
        return cls(
            repo=str(data.get("repo") or ""),
            merge_sha=str(data.get("merge_sha") or ""),
            pre_merge_sha=str(data.get("pre_merge_sha") or ""),
            pr_number=int(data["pr_number"]) if data.get("pr_number") else None,
            deploy_run_id=int(data["deploy_run_id"]) if data.get("deploy_run_id") else None,
            opened_at=str(data.get("opened_at") or ""),
            kind=str(data.get("kind") or "rb"),
            baseline=Baseline.from_json(baseline) if isinstance(baseline, dict) else None,
            status=str(data.get("status") or "pending"),
        )


@dataclass(frozen=True)
class Tick:
    at: datetime
    digests: dict[str, Digest]          # instance -> digest
    deploy_status: str                  # queued|in_progress|waiting|completed|unknown
    deploy_conclusion: str | None       # success|failure|cancelled|None
    new_jobs_total: int                 # jobs across instances with id > baseline max
    failed_jobs_total: int              # failed jobs across instances with id > baseline max

    def to_marker(self) -> str:
        payload = {
            "at": self.at.isoformat(),
            "deploy_status": self.deploy_status,
            "deploy_conclusion": self.deploy_conclusion,
            "new_jobs_total": self.new_jobs_total,
            "failed_jobs_total": self.failed_jobs_total,
            "digests": {name: d.to_json() for name, d in self.digests.items()},
        }
        return f"<!-- {TICK_MARKER} {json.dumps(payload, sort_keys=True)} -->"


@dataclass(frozen=True)
class TickView:
    """A tick as recovered from a comment: only the fields rules need."""

    at: datetime
    deploy_status: str
    deploy_conclusion: str | None
    new_jobs_total: int
    failed_jobs_total: int
    down: dict[str, bool]
    watcher_dead: dict[str, bool]
    maintenance: dict[str, bool]
    review_failed: dict[str, int]
    gate_failures: dict[str, int]
    split_failed: dict[str, int]
    last_scan_at: dict[str, str | None]

    @classmethod
    def parse(cls, body: str, *, watcher_multiplier: float) -> "TickView | None":
        match = _TICK_RE.search(body or "")
        if not match:
            return None
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        at = parse_time(data.get("at"))
        if at is None:
            return None
        digests = data.get("digests") or {}
        view = cls(
            at=at, deploy_status=str(data.get("deploy_status") or "unknown"),
            deploy_conclusion=data.get("deploy_conclusion"),
            new_jobs_total=int(data.get("new_jobs_total") or 0),
            failed_jobs_total=int(data.get("failed_jobs_total") or 0),
            down={}, watcher_dead={}, maintenance={}, review_failed={},
            gate_failures={}, split_failed={}, last_scan_at={},
        )
        for name, d in digests.items():
            ok = bool(d.get("ok"))
            view.down[name] = not ok
            view.maintenance[name] = bool(d.get("maintenance"))
            scan = parse_time(d.get("last_scan_at"))
            view.watcher_dead[name] = (not ok) or watcher_dead(
                scan, poll_interval_seconds=int(d.get("poll_interval_seconds") or 120),
                now=at, multiplier=watcher_multiplier)
            view.review_failed[name] = int(d.get("review_failed") or 0)
            view.gate_failures[name] = int(d.get("gate_failures") or 0)
            view.split_failed[name] = int(d.get("split_failed") or 0)
            view.last_scan_at[name] = d.get("last_scan_at")
        return view


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


def baseline_from(digests: dict[str, Digest], *, started_at: datetime) -> Baseline:
    """Baseline from the primary instance's 14-day history, excluding today."""
    primary = digests.get("vllm_omni") or next(iter(digests.values()))
    days = list(primary.history_days)[:-1] if len(primary.history_days) > 1 else list(primary.history_days)
    attention = [int(d.get("attention") or 0) for d in days]
    gate = [int(d.get("gate_failures") or 0) for d in days]
    return Baseline(
        mean_attention=(sum(attention) / len(attention)) if attention else 0.0,
        max_attention=max(attention) if attention else 0,
        mean_gate_failures=(sum(gate) / len(gate)) if gate else 0.0,
        counts_failed=sum(d.counts.get("failed", 0) for d in digests.values()),
        review_failed=sum(d.review_failed for d in digests.values()),
        gate_failures=sum(d.gate_failures for d in digests.values()),
        split_failed=sum(d.split_failed for d in digests.values()),
        last_scan_at=primary.last_scan_at.isoformat() if primary.last_scan_at else None,
        started_at=started_at.isoformat(),
        max_job_ids={name: d.max_job_id for name, d in digests.items()},
    )


def jobs_since(baseline: Baseline, digests: dict[str, Digest]) -> tuple[int, int]:
    """(new jobs, failed jobs) across instances since the window start.

    New jobs are counted by id above the per-instance baseline (traffic);
    failures by ``updated_at`` after the window start, so a job that was
    already running at deploy time and failed afterwards is attributed to
    the deploy.
    """
    from .dashboard import new_failed_jobs

    started = parse_time(baseline.started_at)
    new_total = 0
    failed_total = 0
    for name, d in digests.items():
        cursor = int(baseline.max_job_ids.get(name, 0))
        new_total += sum(1 for j in d.jobs if int(j.get("id") or 0) > cursor)
        failed_total += len(new_failed_jobs(d.jobs, after=started))
    return new_total, failed_total


def evaluate(record: CanaryRecord, ticks: list[TickView], *, now: datetime,
             policy: dict[str, Any]) -> Decision:
    """Decide the canary's next step from its record and all ticks so far.

    ``ticks`` must be in chronological order and include the current tick
    last. Rules follow the approved plan §5.
    """
    if not ticks:
        return Decision(WAIT, "no tick recorded yet")
    current = ticks[-1]
    c = policy["canary"]

    # Before the window: only the deploy run matters.
    if record.baseline is None:
        if current.deploy_status == "completed":
            if current.deploy_conclusion == "success":
                return Decision(START, "deploy run succeeded; window starts")
            return Decision(DEPLOY_FAILED,
                            f"deploy run concluded {current.deploy_conclusion!r}; the host already rolled back")
        opened = parse_time(record.opened_at)
        if current.deploy_status == "waiting" and opened is not None and \
                now - opened > timedelta(hours=float(c["deploy_waiting_hold_hours"])):
            return Decision(HOLD, "deploy run has been waiting for approval beyond the hold limit")
        return Decision(WAIT, f"deploy run is {current.deploy_status}")

    started = parse_time(record.baseline.started_at) or now
    window = [t for t in ticks if t.at >= started]
    if not window:
        return Decision(WAIT, "window has not received a tick yet")
    hours_open = max((now - started).total_seconds() / 3600.0, 0.0)
    base = record.baseline

    # Trip rules.
    for name in current.down:
        if len(window) >= 2 and all(t.down.get(name) for t in window[-int(c["down_ticks_to_trip"]):]) \
                and len(window) >= int(c["down_ticks_to_trip"]):
            return Decision(TRIP, f"{name}: dashboard unreachable on {c['down_ticks_to_trip']} consecutive ticks",
                            {"rule": "endpoint_down", "instance": name})
    grace = timedelta(minutes=float(c["start_grace_minutes"]))
    if now - started > grace:
        for name, dead in current.watcher_dead.items():
            if dead and not current.down.get(name):
                return Decision(TRIP, f"{name}: watcher has not scanned within the allowed interval",
                                {"rule": "watcher_dead", "instance": name})
    threshold = max(int(c["failed_jobs_floor"]),
                    math.ceil(float(c["failed_jobs_multiplier"]) * base.mean_attention * hours_open / 24.0))
    if current.failed_jobs_total > threshold:
        return Decision(TRIP, f"{current.failed_jobs_total} failed jobs since deploy, above {threshold}",
                        {"rule": "failed_jobs", "threshold": threshold})
    gate_delta = sum(current.gate_failures.values()) - base.gate_failures
    if gate_delta >= int(c["gate_failures_delta"]) or (gate_delta >= 1 and base.mean_gate_failures == 0):
        return Decision(TRIP, f"gate failures rose by {gate_delta} since deploy",
                        {"rule": "gate_failures", "delta": gate_delta})
    review_delta = sum(current.review_failed.values()) - base.review_failed
    if review_delta >= int(c["outcome_failed_delta"]):
        return Decision(TRIP, f"failed review outcomes rose by {review_delta} since deploy",
                        {"rule": "review_failed", "delta": review_delta})
    split_delta = sum(current.split_failed.values()) - base.split_failed
    if split_delta >= int(c["split_failed_delta"]):
        return Decision(TRIP, f"failed split attempts rose by {split_delta} since deploy",
                        {"rule": "split_failed", "delta": split_delta})
    regressions = 0
    for name in current.last_scan_at:
        previous: datetime | None = None
        for t in window:
            stamp = parse_time(t.last_scan_at.get(name))
            if previous is not None and stamp is not None and stamp < previous:
                regressions += 1
            if stamp is not None:
                previous = stamp
    if regressions >= int(c["scan_regressions_to_trip"]):
        return Decision(TRIP, f"watcher scan time regressed {regressions} times (restart loop)",
                        {"rule": "scan_regression", "count": regressions})

    # Close rules.
    if hours_open >= float(c["max_hours"]):
        return Decision(CLOSE, f"window hit the {c['max_hours']} h cap", {"outcome": "passed"})
    if len(window) >= int(c["min_ticks"]) and current.new_jobs_total >= int(c["min_new_jobs"]):
        return Decision(CLOSE, "window passed with enough traffic", {"outcome": "passed"})
    return Decision(WAIT, f"{len(window)} ticks, {current.new_jobs_total} new jobs; window continues")
