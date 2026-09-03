"""The mechanical merge bar.

``evaluate`` is a pure function of a ``PullSnapshot`` plus the policy and a
few facts the caller reads from GitHub (merges today, holds, pause). It
returns every failed rule, never just the first, so the check summary tells
a human exactly what is missing.

Rules are numbered as in the approved plan (§4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any, Callable

from ..config import RepoConfig, carve_out_exempt, carve_out_globs
from .actors import human_label_active
from .reads import PullSnapshot, Review
from .verdict import APPROVE, REVISE, latest_verdict


@dataclass(frozen=True)
class BarInputs:
    """Facts the bar needs beyond the PR itself; all read live by the gate."""

    now: datetime
    merges_today: int
    # Pause as verified from the ledger issue timeline (human labeled, not
    # since human-unlabeled); never from label presence alone.
    paused: bool
    deploy_hold: bool
    open_canary: bool
    # Present only for revert PRs: whether ``git diff <pre_merge_sha> <head>``
    # is empty, verified by the gate itself, and whether the base branch tip
    # is still the reverted merge (no later commits landed).
    revert_verified_empty: bool | None = None
    base_tip_is_reverted_merge: bool | None = None


@dataclass
class BarResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    carve_out_paths: list[str] = field(default_factory=list)
    requires_go: bool = False
    is_revert_fast_path: bool = False

    def summary(self) -> str:
        lines = ["maintainer-gate: " + ("PASS" if self.ok else "FAIL")]
        for item in self.failures:
            lines.append(f"- FAIL {item}")
        for item in self.notes:
            lines.append(f"- note {item}")
        return "\n".join(lines)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def path_matches(path: str, pattern: str) -> bool:
    if "**" in pattern:
        return bool(_glob_to_regex(pattern).match(path))
    if "/" not in pattern:
        # a bare pattern such as ``.env*`` matches at the repository root only
        return fnmatchcase(path, pattern)
    return fnmatchcase(path, pattern)


def carve_out_hits(snapshot: PullSnapshot, policy: dict[str, Any]) -> list[str]:
    globs = carve_out_globs(policy, snapshot.repo)
    exempt = carve_out_exempt(policy, snapshot.repo)
    dep_pattern = re.compile(policy["carve_outs"]["pyproject_dependency_pattern"], re.MULTILINE)
    hits: list[str] = []
    for change in snapshot.files:
        for path in (change.filename, change.previous_filename):
            if not path:
                continue
            if any(path_matches(path, ex) for ex in exempt):
                continue
            if path == "pyproject.toml" or path.endswith("/pyproject.toml"):
                if change.patch is None:
                    hits.append(f"{path} (patch unavailable, treated as a dependency change)")
                elif dep_pattern.search(change.patch):
                    hits.append(f"{path} (dependency line changed)")
                continue
            if any(path_matches(path, glob) for glob in globs):
                hits.append(path)
    return sorted(dict.fromkeys(hits))


GATE_CHECK_NAME = "maintainer-gate"


def _human_changes_requested(snapshot: PullSnapshot) -> bool:
    """Any human CHANGES_REQUESTED review that GitHub still reports as such.

    GitHub keeps a review's state until it is dismissed or superseded by the
    same reviewer, so no timestamp comparison against author-controlled
    commit dates is needed: an un-dismissed request stands.
    """
    latest_by_user: dict[str, Review] = {}
    for review in snapshot.reviews:
        if review.user_type == "Bot" or not review.user_login:
            continue
        if review.state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        previous = latest_by_user.get(review.user_login)
        if previous is None or (review.submitted_at or inputs_epoch()) >= (previous.submitted_at or inputs_epoch()):
            latest_by_user[review.user_login] = review
    return any(r.state == "CHANGES_REQUESTED" for r in latest_by_user.values())


def inputs_epoch() -> datetime:
    return datetime.min.replace(tzinfo=timezone.utc)


def _hold_comment(snapshot: PullSnapshot, policy: dict[str, Any]) -> bool:
    phrase = str(policy["bar"]["hold_phrase"]).casefold()
    trusted = {a.upper() for a in policy["bar"]["collaborator_associations"]}
    return any(
        phrase in comment.body.casefold() and comment.author_association.upper() in trusted
        for comment in snapshot.issue_comments
    )


def _check_state(snapshot: PullSnapshot, name: str, app_slug: str) -> str:
    """success / failure / pending / missing for a named check on the head.

    A check counts only when it was created by the expected integration
    (``app_slug``): a same-named check run from any other app is ignored, so
    a spoofed ``suite`` or ``build-and-test`` cannot satisfy the bar.
    """
    matching = [run for run in snapshot.check_runs
                if run.name == name and run.head_sha == snapshot.head_sha and run.app_slug == app_slug]
    if not matching:
        return "missing"
    if all(run.status == "completed" and run.conclusion == "success" for run in matching):
        return "success"
    if any(run.status != "completed" for run in matching):
        return "pending"
    return "failure"


def evaluate(snapshot: PullSnapshot, *, policy: dict[str, Any], repo: RepoConfig,
             inputs: BarInputs, enforce_caps: bool = True) -> BarResult:
    """Evaluate the bar.

    ``enforce_caps=False`` is what the published check uses: rules 1–6 and 8
    are properties of the pull request, while rule 7 (daily cap, pause,
    deploy holds) governs *autonomous* merges only. A human merging a green
    Tier A PR is not capped; the arbiter always evaluates with
    ``enforce_caps=True`` immediately before it merges.
    """
    result = BarResult(ok=True)
    labels = set(snapshot.labels)
    label_names = policy["labels"]
    reviewer = policy["identities"]["reviewer_login"]

    # Server-stamped clock for "this head": the gate App's earliest check run
    # on it. Never commit metadata, which the pusher controls.
    first_seen = snapshot.head_first_seen(
        gate_app_slug=str(policy["identities"]["gate_app_slug"]), check_name=GATE_CHECK_NAME, now=inputs.now)
    result.notes.append(f"head first seen by the gate at {first_seen.replace(microsecond=0).isoformat()}")

    # Human-only labels count only with human provenance on the timeline,
    # applied after the gate first saw the current head.
    humans = policy["identities"].get("humans") or ()
    has_go = human_label_active(
        snapshot.timeline, label=label_names["go"], humans=humans,
        currently_present=label_names["go"] in labels, after=first_seen)
    if label_names["go"] in labels and not has_go:
        result.notes.append(f"label {label_names['go']} present but not applied by an allowlisted human after the current head; ignored")

    # Revert fast path: only when the gate itself proved the diff empty AND
    # main still sits at the reverted merge (a later commit would make the
    # "exact restoration" claim false).
    is_revert = label_names["rollback"] in labels
    fast_path = bool(is_revert and inputs.revert_verified_empty and inputs.base_tip_is_reverted_merge)
    result.is_revert_fast_path = fast_path
    if is_revert and not inputs.revert_verified_empty:
        result.failures.append(
            "revert PR: diff against the recorded pre-merge commit is not verified empty")
    if is_revert and inputs.revert_verified_empty and not inputs.base_tip_is_reverted_merge:
        result.failures.append(
            "revert PR: main advanced past the reverted merge; needs a human (rollback:needs-human)")

    # 1. shape
    if snapshot.merged:
        result.failures.append("already merged")
    if snapshot.state and snapshot.state != "open":
        result.failures.append(f"pull request is {snapshot.state}, not open")
    if snapshot.draft:
        result.failures.append("draft pull request")
    if snapshot.base_ref != "main":
        result.failures.append(f"base branch is {snapshot.base_ref!r}, not main")
    if snapshot.mergeable is not True:
        result.failures.append(f"GitHub reports mergeable={snapshot.mergeable!r} ({snapshot.mergeable_state})")
    elif snapshot.mergeable_state == "dirty":
        result.failures.append("merge conflicts (mergeable_state=dirty)")

    # never-merge head branches
    for pattern in policy["carve_outs"].get("never_merge_head_branches") or ():
        if fnmatchcase(snapshot.head_ref, pattern):
            result.failures.append(f"head branch {snapshot.head_ref!r} is never merged by automation")

    # 2. required CI on the head, bound to the integration that must produce it
    ci_app = str(policy["identities"].get("ci_app_slug") or "github-actions")
    for name in repo.required_checks:
        state = _check_state(snapshot, name, ci_app)
        if state != "success":
            if snapshot.is_fork and state == "missing":
                result.failures.append(
                    f"required check {name!r} has not run for this fork PR; a human must approve the workflow")
            else:
                result.failures.append(
                    f"required check {name!r} from {ci_app} is {state} on {snapshot.head_sha[:8]}")
    for name in repo.optional_checks:
        state = _check_state(snapshot, name, ci_app)
        if state in ("failure", "pending"):
            result.failures.append(f"check {name!r} is {state} on {snapshot.head_sha[:8]}")

    # 3. reviewer verdict on the exact head
    if not fast_path:
        verdict = latest_verdict(snapshot.reviews, head_sha=snapshot.head_sha, reviewer_login=reviewer)
        if verdict is None:
            result.failures.append(f"no reviewer verdict for head {snapshot.head_sha[:8]}")
        elif verdict == REVISE:
            result.failures.append("reviewer verdict is REVISE for this head")
        elif verdict == APPROVE:
            result.notes.append("reviewer verdict APPROVE on this head")

    # 4. human objections
    if _human_changes_requested(snapshot):
        result.failures.append("a human review requests changes on the current head")
    if label_names["blocked"] in labels:
        result.failures.append(f"label {label_names['blocked']} is present")
    if _hold_comment(snapshot, policy):
        result.failures.append(f"a collaborator comment says {policy['bar']['hold_phrase']!r}")

    # 5. veto window, anchored on server-stamped events only
    if not fast_path:
        anchor = max(snapshot.created_at, first_seen)
        veto = timedelta(hours=float(policy["bar"]["veto_hours"]))
        remaining = anchor + veto - inputs.now
        if remaining > timedelta(0):
            hours = remaining.total_seconds() / 3600
            result.failures.append(
                f"veto window: {hours:.1f} h remaining since the gate first saw this head")

    # 6. carve-outs
    hits = carve_out_hits(snapshot, policy)
    result.carve_out_paths = hits
    if repo.human_only:
        result.requires_go = True
        if not has_go:
            result.failures.append(f"{snapshot.repo} is human-merge only; needs label {label_names['go']}")
    elif hits:
        result.requires_go = True
        if not has_go:
            result.failures.append(
                f"carve-out paths need label {label_names['go']} from a human: " + ", ".join(hits[:8])
                + (" …" if len(hits) > 8 else ""))
        else:
            result.notes.append("carve-out paths present; human go label found")

    # 7. caps and holds: autonomous merges only (see docstring)
    sink = result.failures if enforce_caps else result.notes
    prefix = "" if enforce_caps else "autonomous merges blocked: "
    if inputs.paused:
        sink.append(f"{prefix}ledger is {label_names['paused']}")
    if not fast_path and inputs.merges_today >= int(policy["caps"]["merges_per_day"]):
        sink.append(f"{prefix}daily merge cap reached ({inputs.merges_today}/{policy['caps']['merges_per_day']})")
    if repo.deploys:
        if inputs.deploy_hold:
            sink.append(f"{prefix}a deploy hold is active (open incident or rollback)")
        if inputs.open_canary and not fast_path:
            sink.append(f"{prefix}a canary is still open for the previous deploy")

    # 8. size
    if not has_go:
        max_lines = int(policy["bar"]["max_changed_lines"])
        if snapshot.changed_lines > max_lines:
            result.failures.append(f"diff is {snapshot.changed_lines} lines, above {max_lines}")
        max_commits = int(policy["bar"]["max_commits"])
        if snapshot.commits_count > max_commits:
            result.failures.append(f"{snapshot.commits_count} commits, above {max_commits}")

    result.ok = not result.failures
    return result


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


RevertVerifier = Callable[[PullSnapshot], bool]
