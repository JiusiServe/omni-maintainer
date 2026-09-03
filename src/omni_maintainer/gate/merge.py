"""Gate-side merge and check-run publication.

Only Tier A repositories are ever merged here, and only after ``bar.evaluate``
passed on a snapshot read seconds earlier; ``--match-head-commit`` makes the
merge fail if the head moved in between.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..config import RepoConfig
from ..routine.ghcli import Gh, GhError
from .bar import BarResult

CHECK_NAME = "maintainer-gate"


def publish_pending(gh: Gh, *, repo: str, head_sha: str, details_url: str = "") -> Any:
    """Create an ``in_progress`` ``maintainer-gate`` check run BEFORE evaluating.

    Rulesets consider the newest check run of a name on a commit, so an
    evaluator that crashes after this call leaves a pending run in place
    of any earlier success: a stale success can never stand while a new
    objection or hold goes unrecorded.
    """
    payload = {"name": CHECK_NAME, "head_sha": head_sha, "status": "in_progress",
               "output": {"title": "maintainer-gate evaluating", "summary": "evaluation in progress"}}
    if details_url:
        payload["details_url"] = details_url
    return gh.write(["api", f"repos/{repo}/check-runs", "-X", "POST", "--input", "-"],
                    stdin=json.dumps(payload)).json()


def publish_check(gh: Gh, *, repo: str, head_sha: str, result: BarResult,
                  details_url: str = "") -> Any:
    """Create the ``maintainer-gate`` check run for ``head_sha``.

    Check runs are attributed to the integration that creates them. Run from
    the gate workflow this is the GitHub Actions app, which is what the
    ruleset's required check is bound to.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conclusion = "success" if result.ok else "failure"
    payload = {
        "name": CHECK_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": now,
        "output": {
            "title": "maintainer-gate " + ("PASS" if result.ok else "FAIL"),
            "summary": result.summary() + f"\n\nevaluated_at: {now}",
        },
    }
    if details_url:
        payload["details_url"] = details_url
    return gh.write(["api", f"repos/{repo}/check-runs", "-X", "POST", "--input", "-"],
                    stdin=json.dumps(payload)).json()


def merge_pull(gh: Gh, *, repo: RepoConfig, number: int, head_sha: str) -> str:
    """Merge with a merge commit, refusing if the head moved."""
    if not repo.gate_may_merge:
        raise GhError(f"{repo.slug} is not a Tier A repository; the gate never merges here")
    gh.write(["pr", "merge", str(number), "-R", repo.slug, "--merge",
              "--match-head-commit", head_sha])
    return head_sha
