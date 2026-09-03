"""All three gate workflows: this repository's own, the template copied into
InferMatrixCopilot, and the sweep that gates omni-reviewbot from here. What is
pinned here are the orderings and failure paths that decide whether a crashed
evaluation can be read as a pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml   # declared in the dev extra; these tests must never silently skip

ROOT = Path(__file__).resolve().parents[1]
GATES = {
    "own": ROOT / ".github" / "workflows" / "maintainer-gate.yml",
    "template": ROOT / "workflows" / "maintainer-gate.yml",
    # reviewbot is private in a Free organization, so its gate cannot live in
    # that repository at all: it runs here, on a schedule, against the target.
    "rb": ROOT / ".github" / "workflows" / "maintainer-gate-rb.yml",
}


@pytest.fixture(params=sorted(GATES), ids=sorted(GATES))
def gate(request) -> tuple[str, dict, str]:
    path = GATES[request.param]
    text = path.read_text()
    return text, yaml.safe_load(text), request.param


def steps(workflow: dict) -> list[dict]:
    jobs = workflow["jobs"]
    assert list(jobs) == ["gate"], "one job; a second could merge"
    return jobs["gate"]["steps"]


def index_of(step_list: list[dict], needle: str) -> int:
    hits = [i for i, s in enumerate(step_list)
            if needle in s.get("name", "") or needle in s.get("uses", "")]
    assert len(hits) == 1, f"{needle}: {hits}"
    return hits[0]


def test_the_pending_check_is_published_before_anything_that_can_fail(gate) -> None:
    """An event that does not move the head (a dismissed review, a removed
    label, the hourly sweep) re-runs this workflow on a head that may already
    carry a successful check. If the run then fails, that success stays the
    newest run and the ruleset reads a crashed evaluation as a pass. Publishing
    a pending run first makes every later failure fail closed."""
    text, workflow, which = gate
    step_list = steps(workflow)
    pending = index_of(step_list, "Invalidate the previous verdict")
    assert index_of(step_list, "Mint the gate App token") < pending
    assert index_of(step_list, "Select pull requests") < pending
    for later in ("actions/setup-python", "Bare objects", "Reviewer queue",
                  "Fetch diffs", "Claude review", "Post verdicts", "Evaluate the bar"):
        assert pending < index_of(step_list, later), \
            f"{later} runs before the pending check is published, so a failure there is a stale pass"
    for i, step in enumerate(step_list[:pending]):
        run = step.get("run", "")
        assert "pip install" not in run and "git clone" not in run and "clone --quiet" not in run, \
            f"step {i} can fail before the pending check exists"


def test_selection_reads_every_head_in_one_request(gate) -> None:
    """Reading heads one pull request at a time aborts the sweep on the first
    transient failure, and every head after it keeps whatever check it already
    had. One listing that returns the heads with the numbers cannot do that."""
    text, workflow, which = gate
    step = steps(workflow)[index_of(steps(workflow), "Select pull requests")]
    select = step["run"]
    assert "selected.txt" in select and "headRefOid" in select
    assert "number,headRefOid" in select, "the heads must come with the listing"
    assert "for pr in $numbers" not in select, "a per-pull-request head lookup is the abort path"
    calls = select.count("gh pr list") + select.count("gh pr view")
    assert calls <= 2, f"{calls} listing calls; at most one can run per invocation"
    if which != "rb":
        assert step["env"].get("EVENT_HEAD") == "${{ github.event.pull_request.head.sha }}", \
            "an event carrying the head needs no request at all"
        assert 'if [ -n "$EVENT_PR" ] && [ -n "$EVENT_HEAD" ]' in select


def test_the_pending_check_covers_every_selected_pull_request(gate) -> None:
    text, workflow, which = gate
    publish = steps(workflow)[index_of(steps(workflow), "Invalidate the previous verdict")]["run"]
    assert "done < selected.txt" in publish
    assert "status=in_progress" in publish
    assert "name=maintainer-gate" in publish
    assert "conclusion" not in publish, "a pending run must not carry a conclusion"


def test_one_failed_invalidation_still_invalidates_the_rest_and_fails(gate) -> None:
    """Exiting on the first failure would leave every later head carrying the
    success it already had, which is the whole failure this guards against."""
    text, workflow, which = gate
    publish = steps(workflow)[index_of(steps(workflow), "Invalidate the previous verdict")]["run"]
    assert "failed=1" in publish and "could not invalidate" in publish
    assert '[ "$failed" = 0 ] || exit 1' in publish, "the job must still fail afterwards"
    assert publish.index("done < selected.txt") < publish.index('[ "$failed" = 0 ]'), \
        "the loop must finish before the job gives up"


def test_only_a_deleted_file_may_be_missing_and_anything_else_fails_closed(gate) -> None:
    """A file the pull request deletes cannot be read at the head. Any other
    unreadable file means the reviewer would judge an incomplete change, so it
    must not be able to approve."""
    text, workflow, which = gate
    fetch = steps(workflow)[index_of(steps(workflow), "Fetch diffs")]["run"]
    assert 'select(.status != "removed")' in fetch
    assert re.search(r"done < \"review-input/\$pr\.readable\"", fetch), \
        "the fetch loop must read the list that excludes deleted files"
    assert "2>/dev/null || true" not in fetch, "a swallowed fetch error approves an incomplete diff"
    assert 'incomplete="$f"' in fetch and "VERDICT: REVISE" in fetch
    forced = fetch[fetch.index('if [ -n "$incomplete" ]'):]
    assert 'forced-verdicts/$pr.md' in forced, \
        "the forced verdict must be written outside the reviewer's writable path"
    assert 'sed -i "/^$pr /d" review-input/pending.txt' in forced, \
        "a pull request whose inputs are incomplete must leave the reviewer's list"


def test_a_forced_verdict_always_beats_the_reviewers_own(gate) -> None:
    text, workflow, which = gate
    post = steps(workflow)[index_of(steps(workflow), "Post verdicts")]["run"]
    assert 'if [ -s "forced-verdicts/$pr.md" ]; then f="forced-verdicts/$pr.md"; fi' in post
    assert r"VERDICT: *\(APPROVE\|REVISE\)" in post, "the verdict line is no longer parsed"


def test_the_reviewer_never_receives_the_gate_token(gate) -> None:
    text, workflow, which = gate
    review = next(s for s in steps(workflow)
                  if s.get("uses", "").startswith("anthropics/claude-code-action"))
    assert review["with"]["github_token"] == "${{ github.token }}"
    assert "steps.app.outputs.token" not in str(review)
    allowed = review["with"]["allowed_tools"]
    assert "Bash" not in allowed and "WebFetch" not in allowed
    for name, scope in re.findall(r"(\w+)\(([^)]*)\)", allowed):
        assert scope.startswith("verdicts/" if name == "Write" else "review-input/"), \
            f"{name} may reach {scope}"


def test_no_gate_can_be_driven_by_a_pull_request_head(gate) -> None:
    """The two in-repository gates use pull_request_target, so the base
    branch's workflow runs and the gate environment stays reachable. The
    reviewbot sweep is triggered only by the clock and by a person, and reads
    another repository entirely."""
    text, workflow, which = gate
    on = workflow[True] if True in workflow else workflow["on"]
    assert "pull_request" not in on, "a pull_request trigger runs the head's own workflow"
    if which == "rb":
        assert set(on) == {"schedule", "workflow_dispatch"}
        assert "pull_request_target" not in on
        assert workflow["env"]["TARGET_REPO"] == "JiusiServe/omni-reviewbot"
    else:
        assert "pull_request_target" in on
    assert workflow["jobs"]["gate"]["environment"] == "gate"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    for step in steps(workflow):
        if step.get("uses", "").startswith("actions/checkout"):
            assert step["with"].get("ref") == "main", "a checkout here must pin the base branch"
            assert step["with"].get("persist-credentials") is False
        for clone in re.findall(r"git (?:-c \S+ )?clone[^\n]*", step.get("run", "")):
            assert "--no-checkout" in clone, clone


def test_every_action_is_pinned_by_commit_sha(gate) -> None:
    text, _, which = gate
    for use in re.findall(r"uses:\s*(\S+)", text):
        assert re.fullmatch(r"[0-9a-f]{40}", use.split("@", 1)[1]), f"{use} is not pinned"


def test_the_workflow_publishes_a_check_but_never_merges(gate) -> None:
    text, workflow, which = gate
    assert workflow["permissions"] == {"contents": "read"}
    assert "pr merge" not in text
    evaluate = steps(workflow)[index_of(steps(workflow), "Evaluate the bar")]["run"]
    assert "--publish" in evaluate
    assert 'if [ "$code" -ge 2 ]; then rc=$code; fi' in evaluate, \
        "a failing bar is a failing check; only a crash may fail the job"


def test_the_reviewbot_gate_writes_its_verdict_on_the_target_repository() -> None:
    """It runs here but must publish on reviewbot, where the pull request is."""
    text = GATES["rb"].read_text()
    workflow = yaml.safe_load(text)
    step_list = steps(workflow)
    publish = step_list[index_of(step_list, "Invalidate the previous verdict")]["run"]
    assert 'repos/${TARGET_REPO}/check-runs' in publish
    evaluate = step_list[index_of(step_list, "Evaluate the bar")]["run"]
    assert "$TARGET_REPO" in evaluate or "${TARGET_REPO}" in evaluate
    assert "$GITHUB_REPOSITORY" not in evaluate, "the verdict belongs on the target, not here"


def test_the_template_carries_a_pin_placeholder_and_the_own_gate_does_not() -> None:
    """The template is copied into another repository, where the evaluator must
    be pinned to an exact commit. Here main is the pin, because main is
    ruleset-protected and human-merge only."""
    template = GATES["template"].read_text()
    own = GATES["own"].read_text()
    assert "<PIN-SHA>" in template
    assert "omni-maintainer@${OMNI_MAINTAINER_SHA}" in template
    assert "<PIN-SHA>" not in own
    assert "pip install --quiet ./evaluator" in own
    assert "ref: main" in own


def test_a_sweep_lists_every_open_pull_request(gate) -> None:
    """gh pr list stops at 30 by default, and the ones past it would keep
    whatever check they already carried."""
    text, workflow, which = gate
    select = steps(workflow)[index_of(steps(workflow), "Select pull requests")]["run"]
    for listing in re.findall(r"gh pr list[^\n]*", select):
        assert "--limit" in listing, listing
        limit = int(re.search(r"--limit (\d+)", listing).group(1))
        assert limit >= 1000, listing


def test_the_verdict_binds_to_the_text_the_reviewer_was_shown(gate) -> None:
    """The reviewer reads the title and description and is told to judge them.
    Both are editable by anyone who can edit the pull request, without moving
    the head, so a verdict keyed to the head alone would go on standing after
    the justification it read was rewritten."""
    text, workflow, which = gate
    step_list = steps(workflow)
    post = step_list[index_of(step_list, "Post verdicts")]["run"]
    assert '--ctx "$ctx"' in post, "the marker must carry the digest of the reviewed text"
    assert "while read -r pr head ctx" in post, "the digest travels with the queue entry"
    fetch = step_list[index_of(step_list, "Fetch diffs")]["run"]
    assert "while read -r pr head ctx" in fetch
    assert "cut -d' ' -f1,2 pending.txt" in fetch, \
        "the reviewer is given the pull request and head, not the digest"
    if which != "rb":
        on = workflow[True] if True in workflow else workflow["on"]
        assert "edited" in on["pull_request_target"]["types"], \
            "an edited title or description must re-run the gate"

