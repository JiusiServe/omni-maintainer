"""Revert PRs and provider pin bumps (plan §5–§6).

Both produce a pull request and nothing else. On the reviewbot repository a
human merges; on Tier A repositories the gate does. ``prepare_revert``
refuses unless the reverted tree is byte-identical to the recorded
pre-merge commit, which is the property that lets the gate skip the
reviewer for reverts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .ghcli import Gh, GhError, git

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

EXPECTED_SDK_API = "1.0.0"
REQUIRED_CAPABILITIES = (
    "supports_expected_head", "supports_structured_result", "supports_post_false",
    "supports_file_locking", "supports_idempotent_strict_start", "supports_knowledge_curation",
)
REQUIRED_REPOSITORIES = ("vllm-omni", "afd-plugin")


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class RevertResult:
    branch: str
    revert_sha: str
    empty_against_pre_merge: bool
    pr_url: str
    pr_number: int | None = None
    incident_marked: bool = False


def _number_from_url(url: str) -> int | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def prepare_revert(gh: Gh, *, repo: str, workdir: str, merge_sha: str, pre_merge_sha: str,
                   incident_url: str, reason: str, labels: list[str],
                   incident_number: int | None = None) -> RevertResult:
    """Create ``maintainer/revert-<sha8>`` from ``main``, revert, verify, push, open the PR.

    When ``incident_number`` is given, the incident issue receives the
    rollback marker (revert PR number and expected revert sha) and the
    ``rollback:pr-open`` label, which is what lets the deploy workflow's
    canary job admit exactly this revert through its open-canary hold.
    """
    for value in (merge_sha, pre_merge_sha):
        if not _FULL_SHA.match(value):
            raise ReleaseError(f"expected a full 40-hex sha, got {value!r}")
    branch = f"maintainer/revert-{merge_sha[:8]}"
    git(["fetch", "origin", "main", merge_sha, pre_merge_sha], cwd=workdir)
    tip = git(["rev-parse", "origin/main"], cwd=workdir).strip()
    if tip != merge_sha:
        # A later commit landed; reverting one merge would not restore the
        # recorded pre-merge tree. That decision belongs to a human.
        raise ReleaseError(
            f"main is at {tip[:8]}, not the reverted merge {merge_sha[:8]}; rollback needs a human")
    git(["switch", "-c", branch, "origin/main"], cwd=workdir)
    parents = git(["rev-list", "--parents", "-n", "1", merge_sha], cwd=workdir).split()
    revert_args = ["revert", "--no-edit"]
    if len(parents) > 2:
        revert_args += ["-m", "1"]
    try:
        git(revert_args + [merge_sha], cwd=workdir)
    except GhError as exc:
        git(["revert", "--abort"], cwd=workdir)
        raise ReleaseError(f"revert did not apply cleanly: {exc}") from exc
    diff = git(["diff", "--stat", pre_merge_sha, "HEAD"], cwd=workdir).strip()
    empty = diff == ""
    if not empty:
        raise ReleaseError("reverted tree differs from the recorded pre-merge commit; refusing to open the PR")
    revert_sha = git(["rev-parse", "HEAD"], cwd=workdir).strip()
    git(["push", "-u", "origin", branch], cwd=workdir)
    title = f"revert: {merge_sha[:8]} (canary trip)"
    body = "\n".join([
        f"Mechanical revert of `{merge_sha}` after a canary trip.",
        "",
        f"- restores the tree at `{pre_merge_sha}` (verified: `git diff --stat {pre_merge_sha[:8]} HEAD` is empty)",
        f"- reason: {reason}",
        f"- incident: {incident_url}",
        "",
        "Opened by omni-maintainer. On omni-reviewbot a human merges this PR; the gate verifies the empty diff.",
    ])
    result = gh.write(["pr", "create", "-R", repo, "--base", "main", "--head", branch,
                       "--title", title, "--body-file", "-",
                       *sum((["--label", label] for label in labels), [])], stdin=body)
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "(dry-run)"
    pr_number = _number_from_url(url)
    marked = False
    if incident_number is not None:
        from ..monitor.rollback import PR_OPEN, rollback_marker

        marker = rollback_marker(incident=incident_number, revert_pr=pr_number or 0, merge_sha=merge_sha,
                                 pre_merge_sha=pre_merge_sha, expected_revert_sha=revert_sha)
        gh.write(["issue", "comment", str(incident_number), "-R", repo, "--body-file", "-"],
                 stdin=f"revert PR opened: {url}\n\n{marker}")
        gh.write(["issue", "edit", str(incident_number), "-R", repo, "--add-label", PR_OPEN])
        marked = True
    return RevertResult(branch, revert_sha, empty, url, pr_number, marked)


def handshake_check(capabilities: dict[str, Any], *, expected_direct: str, expected_strict: str,
                    expected_knowledge: str, pinned_version: str) -> list[str]:
    """Reproduce the checks ``scripts/build-release-bundle.sh`` performs, as reasons."""
    gaps: list[str] = []
    if str(capabilities.get("sdk_api_version")) != EXPECTED_SDK_API:
        gaps.append(f"sdk_api_version {capabilities.get('sdk_api_version')!r} != {EXPECTED_SDK_API}")
    for key, expected in (("direct_api_version", expected_direct), ("strict_api_version", expected_strict),
                          ("knowledge_api_version", expected_knowledge)):
        if str(capabilities.get(key)) != expected:
            gaps.append(f"{key} {capabilities.get(key)!r} != {expected}")
    for name in REQUIRED_CAPABILITIES:
        if capabilities.get(name) is not True:
            gaps.append(f"{name} is not true")
    if int(capabilities.get("max_strict_workers") or 0) < 1:
        gaps.append("max_strict_workers < 1")
    repos = set(capabilities.get("supported_repositories") or ())
    for name in REQUIRED_REPOSITORIES:
        if name not in repos:
            gaps.append(f"supported_repositories lacks {name}")
    if str(capabilities.get("distribution_version")) != pinned_version:
        gaps.append(f"distribution_version {capabilities.get('distribution_version')!r} != pinned {pinned_version}")
    return gaps


def pin_bump_preconditions(gh: Gh, *, imc_repo: str, candidate_sha: str, ci_check: str,
                           open_provider_incidents: int) -> list[str]:
    gaps: list[str] = []
    if not _FULL_SHA.match(candidate_sha):
        return [f"candidate sha must be 40 hex: {candidate_sha!r}"]
    try:
        compare = gh.api(f"repos/{imc_repo}/compare/main...{candidate_sha}")
    except GhError as exc:
        return [f"cannot compare candidate with main: {exc}"]
    if str(compare.get("status")) not in ("identical", "behind"):
        gaps.append(f"candidate is not on main (compare status {compare.get('status')!r})")
    try:
        runs = gh.api(f"repos/{imc_repo}/commits/{candidate_sha}/check-runs?per_page=100")
    except GhError as exc:
        return gaps + [f"cannot read check runs: {exc}"]
    ok = any(r.get("name") == ci_check and r.get("conclusion") == "success"
             for r in (runs or {}).get("check_runs", []))
    if not ok:
        gaps.append(f"check {ci_check!r} is not successful on the candidate")
    if open_provider_incidents:
        gaps.append(f"{open_provider_incidents} open provider-class incident(s)")
    return gaps


def render_pin_bump_body(*, imc_repo: str, old_sha: str, new_sha: str, commits: list[dict[str, Any]],
                         handshake_gaps: list[str]) -> str:
    lines = [f"Move the provider pin `deploy/provider-release-sha` from `{old_sha[:8]}` to `{new_sha[:8]}`.", ""]
    lines.append(f"Compare: https://github.com/{imc_repo}/compare/{old_sha[:8]}...{new_sha[:8]}")
    lines.append("")
    lines.append("Changes since the current pin:")
    for commit in commits[:40]:
        message = str(((commit.get("commit") or {}).get("message")) or "").splitlines()[0][:100]
        lines.append(f"- `{str(commit.get('sha') or '')[:8]}` {message}")
    if len(commits) > 40:
        lines.append(f"- … {len(commits) - 40} more")
    lines.append("")
    lines.append("Paired-release handshake: " + ("clean" if not handshake_gaps else "; ".join(handshake_gaps)))
    lines.append("")
    lines.append("Opened by omni-maintainer. The pull_request job builds and tests this exact pair; a human merges.")
    return "\n".join(lines)
