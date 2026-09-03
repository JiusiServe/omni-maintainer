"""The rollback state machine, as labels on the incident issue.

States advance only through ``next_state``; the monitor applies the returned
label and comment. Evidence in the incident body is never rewritten; each
transition adds a comment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

PENDING = "rollback:pending"
PR_OPEN = "rollback:pr-open"
MERGED = "rollback:merged"
DEPLOYING = "rollback:deploying"
VERIFYING = "rollback:verifying"
NEEDS_HUMAN = "rollback:needs-human"
FAILED = "rollback:failed"
RECOVERED = "rollback:recovered"

STATES = (PENDING, PR_OPEN, MERGED, DEPLOYING, VERIFYING, NEEDS_HUMAN, FAILED, RECOVERED)
TERMINAL = frozenset({NEEDS_HUMAN, FAILED, RECOVERED})

# Written on the incident issue when the revert PR is opened. The deploy
# workflow's canary job reads it to allow exactly that revert through its
# "no open canary" hold: the pushed merge must be of ``revert_pr`` and its
# second parent must be ``expected_revert_sha``.
ROLLBACK_MARKER = "omni-maintainer:rollback:v1"
_ROLLBACK_RE = re.compile(r"<!--\s*omni-maintainer:rollback:v1\s+(\{.*?\})\s*-->", re.S)


def rollback_marker(*, incident: int, revert_pr: int, merge_sha: str, pre_merge_sha: str,
                    expected_revert_sha: str) -> str:
    payload = {"incident": incident, "revert_pr": revert_pr, "merge_sha": merge_sha,
               "pre_merge_sha": pre_merge_sha, "expected_revert_sha": expected_revert_sha}
    return f"<!-- {ROLLBACK_MARKER} {json.dumps(payload, sort_keys=True)} -->"


def parse_rollback_marker(body: str) -> dict[str, Any] | None:
    match = _ROLLBACK_RE.search(body or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class RollbackFacts:
    now: datetime
    revert_pr_number: int | None = None
    revert_pr_state: str = ""          # open|merged|closed|""
    revert_conflict: bool = False
    revert_merged_at: datetime | None = None
    revert_deploy_status: str = ""     # queued|in_progress|waiting|completed|""
    revert_deploy_conclusion: str | None = None
    healthy_ticks: int = 0             # consecutive healthy ticks since revert deploy succeeded
    revert_deploy_succeeded_at: datetime | None = None


@dataclass(frozen=True)
class Transition:
    state: str
    comment: str
    hold: bool = True


def current_state(labels: list[str] | tuple[str, ...]) -> str | None:
    present = [label for label in labels if label in STATES]
    if not present:
        return None
    # the highest-progress state wins if several are present
    return max(present, key=STATES.index)


def next_state(state: str | None, facts: RollbackFacts, *, policy: dict[str, Any]) -> Transition:
    c = policy["canary"]
    if state in TERMINAL:
        return Transition(state, "terminal state; no change", hold=state != RECOVERED)
    if state is None or state == PENDING:
        if facts.revert_conflict:
            return Transition(NEEDS_HUMAN, "revert did not apply cleanly; a human must resolve the conflict")
        if facts.revert_pr_number:
            return Transition(PR_OPEN, f"revert PR #{facts.revert_pr_number} is open and awaits a merge")
        return Transition(PENDING, "trip recorded; preparing the revert PR")
    if state == PR_OPEN:
        if facts.revert_pr_state == "merged":
            return Transition(MERGED, f"revert PR #{facts.revert_pr_number} merged; waiting for its deploy run")
        if facts.revert_pr_state == "closed":
            return Transition(NEEDS_HUMAN, f"revert PR #{facts.revert_pr_number} was closed without merging")
        return Transition(PR_OPEN, f"revert PR #{facts.revert_pr_number} still awaits a merge")
    if state == MERGED:
        if facts.revert_deploy_status in ("queued", "in_progress", "waiting"):
            return Transition(DEPLOYING, f"revert deploy run is {facts.revert_deploy_status}")
        if facts.revert_deploy_status == "completed":
            return _after_deploy(facts, policy)
        return Transition(MERGED, "revert deploy run not found yet")
    if state == DEPLOYING:
        if facts.revert_deploy_status == "completed":
            return _after_deploy(facts, policy)
        return Transition(DEPLOYING, f"revert deploy run is {facts.revert_deploy_status or 'unknown'}")
    if state == VERIFYING:
        if facts.healthy_ticks >= int(c["rollback_verify_ticks"]):
            return Transition(RECOVERED, "both dashboards healthy on consecutive ticks; recovered", hold=False)
        succeeded = facts.revert_deploy_succeeded_at or facts.revert_merged_at
        if succeeded and facts.now - succeeded > timedelta(hours=float(c["rollback_verify_max_hours"])):
            return Transition(FAILED, "production did not verify healthy within the rollback window")
        return Transition(VERIFYING, f"{facts.healthy_ticks} healthy ticks so far")
    return Transition(NEEDS_HUMAN, f"unknown rollback state {state!r}")


def _after_deploy(facts: RollbackFacts, policy: dict[str, Any]) -> Transition:
    if facts.revert_deploy_conclusion == "success":
        return Transition(VERIFYING, "revert deployed; verifying production health")
    return Transition(FAILED, f"revert deploy concluded {facts.revert_deploy_conclusion!r}")
