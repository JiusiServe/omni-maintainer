"""Classify commits that reached a protected-by-policy ``main``.

On the private reviewbot repository nothing prevents a direct push, so the
monitor looks at every new commit on ``main`` and names what it was. A PR
merge by a human is the expected shape; a direct push by an app, a bot, or
an unattributed author is treated as an incident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

_MERGE_MESSAGE = re.compile(r"^Merge pull request #(?P<number>\d+)\b")
_SQUASH_SUFFIX = re.compile(r"\(#(?P<number>\d+)\)\s*$")

PR_MERGE_HUMAN = "pr_merge_human"
PR_MERGE_BOT = "pr_merge_bot"
DIRECT_HUMAN = "direct_push_human"
DIRECT_BOT = "direct_push_bot"
DIRECT_UNATTRIBUTED = "direct_push_unattributed"

INCIDENT_KINDS = frozenset({DIRECT_BOT, DIRECT_UNATTRIBUTED})


@dataclass(frozen=True)
class CommitClass:
    sha: str
    kind: str
    pr_number: int | None
    author_login: str
    author_type: str
    message: str


def classify_commit(commit: dict[str, Any], *, bot_logins: Iterable[str] = ()) -> CommitClass:
    sha = str(commit.get("sha") or "")
    message = str(((commit.get("commit") or {}).get("message")) or "")
    first = message.splitlines()[0] if message else ""
    parents = commit.get("parents") or []
    author = commit.get("author") or {}
    login = str(author.get("login") or "")
    author_type = str(author.get("type") or "")
    bots = set(bot_logins)
    is_bot = author_type == "Bot" or login in bots or login.endswith("[bot]")
    pr_number: int | None = None
    match = _MERGE_MESSAGE.match(first)
    if match and len(parents) >= 2:
        pr_number = int(match.group("number"))
    else:
        squash = _SQUASH_SUFFIX.search(first)
        if squash and len(parents) == 1:
            pr_number = int(squash.group("number"))
    if pr_number is not None:
        kind = PR_MERGE_BOT if is_bot else PR_MERGE_HUMAN
    elif not login:
        kind = DIRECT_UNATTRIBUTED
    elif is_bot:
        kind = DIRECT_BOT
    else:
        kind = DIRECT_HUMAN
    return CommitClass(sha, kind, pr_number, login, author_type, first[:120])


def new_commits_since(commits: list[dict[str, Any]], *, last_seen_sha: str) -> list[dict[str, Any]]:
    """Commits newer than ``last_seen_sha`` in a newest-first list, oldest first.

    When the last seen sha is not in the page, every commit in the page is
    returned; the caller treats that as "history moved further than one page"
    and reports it.
    """
    out: list[dict[str, Any]] = []
    for commit in commits:
        if str(commit.get("sha") or "") == last_seen_sha:
            break
        out.append(commit)
    return list(reversed(out))
