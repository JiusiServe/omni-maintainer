"""Classify commits that reached a protected-by-policy ``main``.

On the private reviewbot repository nothing prevents a direct push, so the
monitor looks at every new commit on ``main`` and names what it was.

Attribution rules, all server-side facts:

- A commit is a pull-request merge only when GitHub's own record of that
  pull request says it merged **and** its ``merge_commit_sha`` is this
  commit. A merge message and two parents are trivially forged by whoever
  pushes; the PR record is not.
- Commit author and committer metadata are never used to excuse a push.
- A direct push is an incident unless the deploy run's canary record,
  written by GitHub Actions with the server-stamped ``github.actor`` of that
  push, names an allowlisted human as the pusher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

_MERGE_MESSAGE = re.compile(r"^Merge pull request #(?P<number>\d+)\b")

PR_MERGE = "pr_merge"
DIRECT_HUMAN = "direct_push_human"          # pusher proven by a server-stamped canary record
DIRECT_UNATTRIBUTED = "direct_push_unattributed"
FORGED_MERGE = "forged_merge_message"       # claims a PR merge GitHub does not confirm

INCIDENT_KINDS = frozenset({DIRECT_UNATTRIBUTED, FORGED_MERGE})

# pr_number -> merge_commit_sha of that PR when GitHub reports it merged, else None
MergeLookup = Callable[[int], "str | None"]


@dataclass(frozen=True)
class CommitClass:
    sha: str
    kind: str
    pr_number: int | None
    pusher: str          # from the canary record when known, else ""
    message: str


def classify_commit(commit: dict[str, Any], *, merged_pr_sha: MergeLookup,
                    pushers: dict[str, str] | None = None, humans: Iterable[str] = ()) -> CommitClass:
    """``merged_pr_sha(n)`` must answer from GitHub's pull-request record.

    ``pushers`` maps merge_sha → server-stamped pusher login (from canary records).
    """
    sha = str(commit.get("sha") or "")
    message = str(((commit.get("commit") or {}).get("message")) or "")
    first = message.splitlines()[0] if message else ""
    parents = commit.get("parents") or []
    pusher = (pushers or {}).get(sha, "")
    match = _MERGE_MESSAGE.match(first)
    if match and len(parents) >= 2:
        number = int(match.group("number"))
        recorded = merged_pr_sha(number)
        if recorded and recorded.lower() == sha.lower():
            return CommitClass(sha, PR_MERGE, number, pusher, first[:120])
        return CommitClass(sha, FORGED_MERGE, number, pusher, first[:120])
    if pusher and pusher in set(humans):
        return CommitClass(sha, DIRECT_HUMAN, None, pusher, first[:120])
    return CommitClass(sha, DIRECT_UNATTRIBUTED, None, pusher, first[:120])


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
