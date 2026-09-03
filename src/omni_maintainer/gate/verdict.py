"""Reviewer verdict markers.

The reviewer posts one COMMENT review per head whose body starts with a
marker binding the verdict to the exact head SHA. The bar trusts a marker
only when its author is the configured reviewer identity, which is the
GitHub Actions identity, one no routine can impersonate.
"""

from __future__ import annotations

import re
from datetime import datetime

from .reads import Review

MARKER_PREFIX = "<!-- omni-maintainer:review:v1"
_MARKER = re.compile(
    r"<!--\s*omni-maintainer:review:v1\s+head=(?P<head>[0-9a-f]{40})\s+"
    r"verdict=(?P<verdict>APPROVE|REVISE)\s*-->"
)
APPROVE = "APPROVE"
REVISE = "REVISE"


def format_marker(head_sha: str, verdict: str) -> str:
    head = head_sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("head_sha must be a full 40-hex commit id")
    if verdict not in (APPROVE, REVISE):
        raise ValueError("verdict must be APPROVE or REVISE")
    return f"{MARKER_PREFIX} head={head} verdict={verdict} -->"


def parse_marker(body: str) -> tuple[str, str] | None:
    """Return (head_sha, verdict) from the first marker in ``body``."""
    match = _MARKER.search(body or "")
    if not match:
        return None
    return match.group("head"), match.group("verdict")


def latest_verdict(reviews: tuple[Review, ...] | list[Review], *, head_sha: str,
                   reviewer_login: str) -> str | None:
    """The reviewer's most recent verdict for exactly ``head_sha``.

    Reviews by any other author are ignored regardless of content, so a
    maintainer-routine review that copies the marker verbatim carries no
    weight.
    """
    head = head_sha.strip().lower()
    best: tuple[datetime | None, str] | None = None
    for review in reviews:
        if review.user_login != reviewer_login:
            continue
        parsed = parse_marker(review.body)
        if parsed is None or parsed[0] != head:
            continue
        stamp = review.submitted_at
        if best is None or (stamp is not None and (best[0] is None or stamp >= best[0])):
            best = (stamp, parsed[1])
    return best[1] if best else None


def needs_verdict(reviews: tuple[Review, ...] | list[Review], *, head_sha: str,
                  reviewer_login: str) -> bool:
    return latest_verdict(reviews, head_sha=head_sha, reviewer_login=reviewer_login) is None
