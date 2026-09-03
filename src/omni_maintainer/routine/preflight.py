"""What every routine establishes before acting.

Nothing here trusts a cache: the pause flag is the ledger issue's labels,
today's counters come from PR history, and holds come from open issues.
A routine that cannot complete preflight must stop and post nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import TIER_A, repo_config
from ..gate.actors import human_pause_active
from ..gate.caps import merges_today, prs_opened_today, search_query_merged_today, search_query_opened_today
from .ghcli import Gh, GhError


class PreflightError(RuntimeError):
    """Preflight could not be completed; the routine must not proceed."""


@dataclass
class Preflight:
    now: datetime
    paused: bool
    ledger_issue: int
    merges_today: int
    prs_opened_today: int
    open_canaries: list[dict[str, Any]] = field(default_factory=list)
    open_incidents: list[dict[str, Any]] = field(default_factory=list)
    open_rollbacks: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def deploy_hold(self) -> bool:
        return bool(self.open_incidents or self.open_rollbacks)

    @property
    def open_canary(self) -> bool:
        return bool(self.open_canaries)

    def to_json(self) -> dict[str, Any]:
        return {
            "now": self.now.isoformat(),
            "paused": self.paused,
            "ledger_issue": self.ledger_issue,
            "merges_today": self.merges_today,
            "prs_opened_today": self.prs_opened_today,
            "open_canaries": [i.get("number") for i in self.open_canaries],
            "open_incidents": [i.get("number") for i in self.open_incidents],
            "open_rollbacks": [i.get("number") for i in self.open_rollbacks],
            "deploy_hold": self.deploy_hold,
            "notes": list(self.notes),
        }


def _search_pulls(gh: Gh, query: str) -> list[dict[str, Any]]:
    data = gh.api(f"search/issues?q={query}&per_page=100", paginate=False)
    items = (data or {}).get("items") if isinstance(data, dict) else None
    return [i for i in (items or []) if isinstance(i, dict)]


def _pull_details(gh: Gh, repo: str, numbers: list[int]) -> list[dict[str, Any]]:
    out = []
    for number in numbers:
        out.append(gh.api(f"repos/{repo}/pulls/{number}"))
    return out


def _open_issues(gh: Gh, repo: str, label: str) -> list[dict[str, Any]]:
    data = gh.api(f"repos/{repo}/issues?labels={label}&state=open&per_page=100", paginate=True)
    return [i for i in (data or []) if isinstance(i, dict) and "pull_request" not in i]


def run_preflight(gh: Gh, policy: dict[str, Any], *, now: datetime) -> Preflight:
    labels = policy["labels"]
    ledger_repo = policy["ledger"]["repo"]
    ledger_number = int(policy["ledger"].get("issue") or 0)
    paused = False
    notes: list[str] = []
    if ledger_number:
        # Pause is a human control: decided from who labeled/unlabeled on the
        # timeline, never from the label's mere presence (a routine can flip
        # labels; it cannot forge a human actor).
        try:
            timeline = gh.api(f"repos/{ledger_repo}/issues/{ledger_number}/timeline?per_page=100",
                              paginate=True)
        except GhError as exc:
            raise PreflightError(f"cannot read the ledger issue timeline: {exc}") from exc
        paused = human_pause_active(timeline or [], label=labels["paused"],
                                    humans=policy["identities"].get("humans") or ())
    else:
        notes.append("ledger issue not configured; pause flag unavailable")

    merges = 0
    for slug in policy["repos"]:
        if repo_config(policy, slug).tier != TIER_A:
            continue
        try:
            found = _search_pulls(gh, search_query_merged_today(slug, now))
            details = _pull_details(gh, slug, [int(i["number"]) for i in found])
        except GhError as exc:
            raise PreflightError(f"cannot count merges for {slug}: {exc}") from exc
        merges += merges_today(details, now=now, merger_logins=policy["identities"]["gate_merger_logins"])

    opened = 0
    routine_login = policy["identities"].get("routine_login") or ""
    if routine_login:
        for slug in policy["repos"]:
            try:
                found = _search_pulls(gh, search_query_opened_today(slug, now, routine_login))
            except GhError as exc:
                raise PreflightError(f"cannot count opened PRs for {slug}: {exc}") from exc
            opened += prs_opened_today(found, now=now, author_login=routine_login)
    else:
        notes.append("routine_login not configured; PR cap counts 0 (set it after the probe)")

    rb = policy["ledger"]["repo"]
    try:
        canaries = _open_issues(gh, rb, labels["canary"])
        incidents = _open_issues(gh, rb, labels["incident"])
        rollbacks = _open_issues(gh, rb, labels["rollback"])
    except GhError as exc:
        raise PreflightError(f"cannot read open canary/incident issues: {exc}") from exc

    return Preflight(now=now, paused=paused, ledger_issue=ledger_number,
                     merges_today=merges, prs_opened_today=opened,
                     open_canaries=canaries, open_incidents=incidents,
                     open_rollbacks=rollbacks, notes=notes)
