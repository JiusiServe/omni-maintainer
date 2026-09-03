"""The daily routine's ordered work queue (plan §8).

Classification is deterministic and label-driven; the routine's model only
decides *how* to fix an item the queue put in front of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from ..gate.reads import parse_time

_RFC_TITLE = re.compile(r"^\s*\[(rfc|feature|proposal)\]", re.I)
_RFC_ONLY = re.compile(r"^\s*\[(rfc|proposal)\]", re.I)
_BUG_TITLE = re.compile(r"^\s*\[(bug|fix)\]", re.I)
_ACCEPTANCE = re.compile(r"acceptance criteria|验收|acceptance:", re.I)

PRIORITY = {
    "incident_followup": 0,
    "filed_code": 1,
    "filed_provider": 1,
    "bug": 2,
    "enhancement": 3,
    "pr_analysis": 4,
    "rfc_proposal": 5,
}


@dataclass(frozen=True)
class WorkItem:
    repo: str
    number: int
    kind: str
    title: str
    url: str
    reason: str

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.kind, 9)


def _labels(issue: dict[str, Any]) -> set[str]:
    return {str(l.get("name")) for l in (issue.get("labels") or []) if l.get("name")}


def classify_issue(issue: dict[str, Any], *, policy: dict[str, Any]) -> tuple[str, str] | None:
    """Return (kind, reason) or None when the issue is not the maintainer's work."""
    labels = _labels(issue)
    names = policy["labels"]
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    if names["proposed"] in labels and names["go"] not in labels:
        return None  # proposal posted; waiting for a human
    if names["incident"] in labels or names["blocked"] in labels or names["rollback"] in labels:
        return "incident_followup", "open incident/blocked/rollback item"
    if names["filed"] in labels:
        if "provider" in labels:
            return "filed_provider", "monitor-filed provider failure"
        if "code" in labels:
            return "filed_code", "monitor-filed reviewbot failure"
        return None  # infra/external: watched, not coded
    if "bug" in labels or _BUG_TITLE.match(title):
        return "bug", "labeled bug"
    if _RFC_TITLE.match(title) or "rfc" in labels or "feature" in labels or "enhancement" in labels:
        if names["go"] in labels:
            return "enhancement", "human go label present"
        # An RFC stays a proposal even with acceptance text; a feature request
        # that states its acceptance criteria is bounded work.
        if _ACCEPTANCE.search(body) and not _RFC_ONLY.match(title):
            return "enhancement", "explicit acceptance criteria"
        return "rfc_proposal", "feature/RFC without a go label"
    return None


def build_queue(issues_by_repo: dict[str, Iterable[dict[str, Any]]], pulls_by_repo: dict[str, Iterable[dict[str, Any]]],
                *, policy: dict[str, Any]) -> list[WorkItem]:
    items: list[WorkItem] = []
    for repo, issues in issues_by_repo.items():
        for issue in issues:
            if "pull_request" in issue:
                continue
            classified = classify_issue(issue, policy=policy)
            if classified is None:
                continue
            kind, reason = classified
            items.append(WorkItem(repo, int(issue["number"]), kind, str(issue.get("title") or ""),
                                  str(issue.get("html_url") or ""), reason))
    for repo, pulls in pulls_by_repo.items():
        for pull in pulls:
            if pull.get("draft"):
                continue
            items.append(WorkItem(repo, int(pull["number"]), "pr_analysis", str(pull.get("title") or ""),
                                  str(pull.get("html_url") or ""), "open pull request"))
    return sorted(items, key=lambda i: (i.priority, i.repo, i.number))


@dataclass(frozen=True)
class StaleDecision:
    number: int
    action: str  # none | label | close
    idle_days: float


def stale_decisions(pulls: Iterable[dict[str, Any]], *, now: datetime, policy: dict[str, Any]) -> list[StaleDecision]:
    """Label after N idle days, close after M; never for go-labeled or assigned PRs."""
    label_after = int(policy["stale"]["label_after_days"])
    close_after = int(policy["stale"]["close_after_days"])
    names = policy["labels"]
    out = []
    for pull in pulls:
        labels = _labels(pull)
        if names["go"] in labels or pull.get("assignees"):
            continue
        updated = parse_time(pull.get("updated_at")) or now
        idle = (now - updated) / timedelta(days=1)
        if idle >= close_after and names["stale"] in labels:
            out.append(StaleDecision(int(pull["number"]), "close", idle))
        elif idle >= label_after and names["stale"] not in labels:
            out.append(StaleDecision(int(pull["number"]), "label", idle))
        else:
            out.append(StaleDecision(int(pull["number"]), "none", idle))
    return out
