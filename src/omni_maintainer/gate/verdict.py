"""Reviewer verdict markers.

The reviewer posts one COMMENT review whose body starts with a marker binding
the verdict to exactly what was reviewed: the head SHA, and a digest of the
pull request's title and description. The bar trusts a marker only when its
author is the configured reviewer identity, one no routine can impersonate.

The description is part of the binding because the reviewer reads it and is
told to judge it. It is also editable at any time, by anyone who can edit the
pull request, without moving the head, so a verdict keyed to the head alone
would go on standing after the justification it read was rewritten.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from .reads import Review

MARKER_PREFIX = "<!-- omni-maintainer:review:v2"
_MARKER = re.compile(
    r"<!--\s*omni-maintainer:review:v2\s+head=(?P<head>[0-9a-f]{40})\s+"
    r"ctx=(?P<ctx>[0-9a-f]{12})\s+"
    r"verdict=(?P<verdict>APPROVE|REVISE)\s*-->"
)
APPROVE = "APPROVE"
REVISE = "REVISE"


def context_digest(title: str, body: str) -> str:
    """Digest of the prose the reviewer was shown alongside the diff.

    A v1 marker carried no digest, so it can never equal one of these and is
    simply not a verdict any more: a re-review costs one gate run, where
    honouring the old marker would let an edited description keep an approval
    it was never given.
    """
    payload = (title or "") + "\x00" + (body or "")
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def format_marker(head_sha: str, verdict: str, ctx: str) -> str:
    head = head_sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("head_sha must be a full 40-hex commit id")
    if verdict not in (APPROVE, REVISE):
        raise ValueError("verdict must be APPROVE or REVISE")
    digest = (ctx or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{12}", digest):
        raise ValueError("ctx must be a 12-hex context digest")
    return f"{MARKER_PREFIX} head={head} ctx={digest} verdict={verdict} -->"


def parse_marker(body: str) -> tuple[str, str, str] | None:
    """Return (head_sha, ctx, verdict) from the first marker in ``body``."""
    match = _MARKER.search(body or "")
    if not match:
        return None
    return match.group("head"), match.group("ctx"), match.group("verdict")


def latest_verdict(reviews: tuple[Review, ...] | list[Review], *, head_sha: str,
                   ctx: str, reviewer_login: str) -> str | None:
    """The reviewer's most recent verdict for exactly this head and context.

    Reviews by any other author are ignored regardless of content, so a
    maintainer-routine review that copies the marker verbatim carries no
    weight. A marker whose digest is not ``ctx`` describes a description the
    pull request no longer has, and is not a verdict on this one.
    """
    head = head_sha.strip().lower()
    digest = (ctx or "").strip().lower()
    best: tuple[datetime | None, str] | None = None
    for review in reviews:
        if review.user_login != reviewer_login:
            continue
        parsed = parse_marker(review.body)
        if parsed is None or parsed[0] != head or parsed[1] != digest:
            continue
        stamp = review.submitted_at
        if best is None or (stamp is not None and (best[0] is None or stamp >= best[0])):
            best = (stamp, parsed[2])
    return best[1] if best else None


def needs_verdict(reviews: tuple[Review, ...] | list[Review], *, head_sha: str,
                  ctx: str, reviewer_login: str) -> bool:
    return latest_verdict(reviews, head_sha=head_sha, ctx=ctx,
                          reviewer_login=reviewer_login) is None
