from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from omni_maintainer.config import repo_config
from omni_maintainer.gate.bar import BarInputs, carve_out_hits, evaluate, path_matches
from omni_maintainer.gate.reads import CheckRun, FileChange, Review
from omni_maintainer.gate.verdict import format_marker

IMC = "JiusiServe/InferMatrixCopilot"
REVIEWER = "omni-maintainer-gate[bot]"


def _inputs(now, **kw):
    base = dict(now=now, merges_today=0, paused=False, deploy_hold=False, open_canary=False)
    base.update(kw)
    return BarInputs(**base)


def _approved(snapshot, at):
    marker = format_marker(snapshot.head_sha, "APPROVE")
    return replace(snapshot, reviews=snapshot.reviews + (
        Review(REVIEWER, "Bot", "COMMENTED", at, marker + "\n\nlooks fine", snapshot.head_sha),))


def _clean(snapshot):
    """PR #84 as it would look if GitHub reported it mergeable."""
    return replace(snapshot, mergeable=True, mergeable_state="clean")


def _seen(snapshot, at, app="omni-maintainer-gate"):
    """The gate App evaluated this head at ``at`` (its server-stamped check run)."""
    return replace(snapshot, check_runs=snapshot.check_runs + (
        CheckRun("maintainer-gate", "completed", "failure", app, snapshot.head_sha, at),))


def test_real_pr84_fails_on_conflict_and_missing_verdict(pr84, policy, now):
    result = evaluate(pr84, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(now))
    assert not result.ok
    joined = "\n".join(result.failures)
    assert "mergeable=False" in joined
    assert "no reviewer verdict" in joined
    # CI is green on the real head, so no CI failure is reported
    assert "required check 'suite'" not in joined


def test_glob_semantics():
    assert path_matches(".github/workflows/deploy.yml", ".github/workflows/**")
    assert path_matches("deploy/personal-agent/host.sh", "deploy/**")
    assert path_matches("adapters/vllm_omni/manifest.yaml", "adapters/*/manifest.yaml")
    assert not path_matches("adapters/vllm_omni/doc/x.md", "adapters/*/manifest.yaml")
    assert path_matches(".env.local", ".env*")
    assert not path_matches("src/.env", ".env*")
    assert path_matches("scripts/build-release-bundle.sh", "scripts/*release*")


def test_carve_out_detects_dependency_change_and_missing_patch(pr84, policy):
    dep = replace(pr84, files=(FileChange("pyproject.toml", "modified", 1, 1,
                                          '-dependencies = ["infermatrix-copilot==0.2.0"]\n+dependencies = ["infermatrix-copilot==0.3.0"]'),))
    assert any("dependency line changed" in h for h in carve_out_hits(dep, policy))
    cosmetic = replace(pr84, files=(FileChange("pyproject.toml", "modified", 1, 1, '-version = "1"\n+version = "2"'),))
    assert carve_out_hits(cosmetic, policy) == []
    unknown = replace(pr84, files=(FileChange("pyproject.toml", "modified", 900, 900, None),))
    assert any("patch unavailable" in h for h in carve_out_hits(unknown, policy))


def test_go_label_requires_human_actor_after_head(pr84, policy, now):
    first_seen = now - timedelta(hours=30)
    snap = _seen(_approved(_clean(pr84), first_seen), first_seen)
    workflow = replace(snap, files=snap.files + (FileChange(".github/workflows/test.yml", "modified", 1, 1, "+x"),))
    inputs = _inputs(now)

    def ev(event, login, typ, at):
        return {"event": event, "label": {"name": "maintainer-go"}, "actor": {"login": login, "type": typ},
                "created_at": at.isoformat()}

    # label present, but applied by a bot: ignored
    bot_labeled = replace(workflow, labels=workflow.labels + ("maintainer-go",),
                          timeline=(ev("labeled", "x[bot]", "Bot", now - timedelta(hours=1)),))
    result = evaluate(bot_labeled, policy=policy, repo=repo_config(policy, IMC), inputs=inputs)
    assert any("carve-out paths need label" in f for f in result.failures)
    # applied by the allowlisted human before the gate first saw this head: lapses
    stale_go = replace(bot_labeled, timeline=(ev("labeled", "tzhouam", "User", first_seen - timedelta(days=1)),))
    result = evaluate(stale_go, policy=policy, repo=repo_config(policy, IMC), inputs=inputs)
    assert any("carve-out paths need label" in f for f in result.failures)
    # applied by the human after the head was first seen: honoured
    fresh_go = replace(bot_labeled, timeline=(ev("labeled", "tzhouam", "User", first_seen + timedelta(minutes=5)),))
    result = evaluate(fresh_go, policy=policy, repo=repo_config(policy, IMC), inputs=inputs)
    assert not any("carve-out" in f for f in result.failures)
    # revoked by the human, then re-added by a bot: not honoured
    revoked = replace(bot_labeled, timeline=(
        ev("labeled", "tzhouam", "User", first_seen + timedelta(minutes=5)),
        ev("unlabeled", "tzhouam", "User", first_seen + timedelta(minutes=10)),
        ev("labeled", "claude[bot]", "Bot", first_seen + timedelta(minutes=15))))
    result = evaluate(revoked, policy=policy, repo=repo_config(policy, IMC), inputs=inputs)
    assert any("carve-out paths need label" in f for f in result.failures)
    # a backdated commit does not move the clock: first-seen comes from the gate's check run
    backdated = replace(fresh_go, last_commit_at=now + timedelta(days=3))
    result = evaluate(backdated, policy=policy, repo=repo_config(policy, IMC), inputs=inputs)
    assert not any("carve-out" in f for f in result.failures)


def test_first_seen_ignores_spoofed_gate_checks(pr84, policy, now):
    # a maintainer-gate check from another app cannot shorten the veto window
    spoofed = _seen(_approved(_clean(pr84), now), now - timedelta(days=2), app="some-other-app")
    result = evaluate(spoofed, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(now))
    assert any("veto window: 24.0 h remaining" in f for f in result.failures)


def test_veto_window_and_caps_and_pause(pr84, policy, now):
    seen = now - timedelta(hours=2)
    snap = _seen(_approved(_clean(pr84), seen), seen)
    repo = repo_config(policy, IMC)
    early = evaluate(snap, policy=policy, repo=repo, inputs=_inputs(now))
    assert any("veto window: 22.0 h remaining" in f for f in early.failures)
    at = seen + timedelta(hours=25)
    late = evaluate(snap, policy=policy, repo=repo, inputs=_inputs(at))
    assert not any("veto window" in f for f in late.failures)
    # a head the gate has never seen starts its window now
    unseen = _approved(_clean(pr84), seen)
    assert any("veto window: 24.0 h" in f for f in evaluate(unseen, policy=policy, repo=repo, inputs=_inputs(at)).failures)
    # caps, pause and holds are failures for the arbiter and notes on the published check
    capped = evaluate(snap, policy=policy, repo=repo, inputs=_inputs(at, merges_today=3), enforce_caps=True)
    assert any("daily merge cap" in f for f in capped.failures)
    advisory = evaluate(snap, policy=policy, repo=repo, inputs=_inputs(at, merges_today=3), enforce_caps=False)
    assert advisory.ok and any("daily merge cap" in n for n in advisory.notes)
    paused = evaluate(snap, policy=policy, repo=repo, inputs=_inputs(at, paused=True), enforce_caps=True)
    assert any("maintainer:paused" in f for f in paused.failures)


def test_passes_when_everything_holds(pr84, policy, now):
    seen = now - timedelta(hours=25)
    snap = _seen(_approved(_clean(pr84), seen), seen)
    result = evaluate(snap, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(now))
    assert result.ok, result.failures
    # once main advances, the same approval no longer describes the actual merge
    behind = replace(snap, mergeable_state="behind")
    result = evaluate(behind, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(now))
    assert any("behind main" in f for f in result.failures)


def test_revise_verdict_and_forged_marker(pr84, policy, now):
    seen = now - timedelta(hours=25)
    snap = _seen(_clean(pr84), seen)
    at = now
    forged = replace(snap, reviews=(Review("someone-else", "User", "COMMENTED", now,
                                           format_marker(snap.head_sha, "APPROVE"), snap.head_sha),))
    result = evaluate(forged, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(at))
    assert any("no reviewer verdict" in f for f in result.failures)
    revised = replace(snap, reviews=(Review(REVIEWER, "Bot", "COMMENTED", now,
                                            format_marker(snap.head_sha, "REVISE"), snap.head_sha),))
    result = evaluate(revised, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(at))
    assert any("REVISE" in f for f in result.failures)


def test_human_changes_requested_and_hold_comment(pr84, policy, now):
    seen = now - timedelta(hours=25)
    snap = _seen(_approved(_clean(pr84), seen), seen)
    at = now
    objected = replace(snap, reviews=snap.reviews + (Review("reviewer", "User", "CHANGES_REQUESTED", now, "no", snap.head_sha),))
    result = evaluate(objected, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(at))
    assert any("requests changes" in f for f in result.failures)
    # the same reviewer later approving supersedes their request
    superseded = replace(objected, reviews=objected.reviews + (
        Review("reviewer", "User", "APPROVED", now + timedelta(minutes=1), "ok", snap.head_sha),))
    result = evaluate(superseded, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(at))
    assert not any("requests changes" in f for f in result.failures)
    from omni_maintainer.gate.reads import Comment
    held = replace(snap, issue_comments=(Comment("tzhouam", "MEMBER", "maintainer: hold until Monday", now),))
    result = evaluate(held, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(at))
    assert any("hold" in f for f in result.failures)
    outsider = replace(snap, issue_comments=(Comment("drive-by", "NONE", "maintainer: hold", now),))
    result = evaluate(outsider, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(at))
    assert not any("hold" in f for f in result.failures)


def test_required_check_states(pr84, policy, now):
    seen = now - timedelta(hours=25)
    snap = _seen(_approved(_clean(pr84), seen), seen)
    at = now
    repo = repo_config(policy, IMC)
    gate_run = CheckRun("maintainer-gate", "completed", "failure", "omni-maintainer-gate", snap.head_sha, seen)
    failing = replace(snap, check_runs=(gate_run, CheckRun("suite", "completed", "failure", "github-actions", snap.head_sha),))
    assert any("'suite' from github-actions is failure" in f
               for f in evaluate(failing, policy=policy, repo=repo, inputs=_inputs(at)).failures)
    stale_head = replace(snap, check_runs=(gate_run, CheckRun("suite", "completed", "success", "github-actions", "0" * 40),))
    assert any("'suite' from github-actions is missing" in f
               for f in evaluate(stale_head, policy=policy, repo=repo, inputs=_inputs(at)).failures)
    # a same-named check from another app is not the required check
    spoofed = replace(snap, check_runs=(gate_run, CheckRun("suite", "completed", "success", "some-other-app", snap.head_sha),))
    assert any("'suite' from github-actions is missing" in f
               for f in evaluate(spoofed, policy=policy, repo=repo, inputs=_inputs(at)).failures)
    fork = replace(stale_head, is_fork=True)
    assert any("fork PR" in f for f in evaluate(fork, policy=policy, repo=repo, inputs=_inputs(at)).failures)


def test_path_conditional_audit_check(pr84, policy, now):
    seen = now - timedelta(hours=25)
    repo = repo_config(policy, IMC)
    base = _seen(_approved(_clean(pr84), seen), seen)
    # PR #84 touches installer files only: audit is not required and its absence is fine
    assert evaluate(base, policy=policy, repo=repo, inputs=_inputs(now)).ok
    touching = replace(base, files=base.files + (FileChange("tools/audit_vllm_omni_release.py", "modified", 1, 1, "+x"),))
    result = evaluate(touching, policy=policy, repo=repo, inputs=_inputs(now))
    assert any("'audit' is required by changed paths" in f and "is missing" in f for f in result.failures)
    cancelled = replace(touching, check_runs=touching.check_runs + (
        CheckRun("audit", "completed", "cancelled", "github-actions", touching.head_sha),))
    assert any("'audit' is required" in f for f in evaluate(cancelled, policy=policy, repo=repo, inputs=_inputs(now)).failures)
    green = replace(touching, check_runs=touching.check_runs + (
        CheckRun("audit", "completed", "success", "github-actions", touching.head_sha),))
    assert evaluate(green, policy=policy, repo=repo, inputs=_inputs(now)).ok


def test_revert_fast_path_requires_both_proofs(pr84, policy, now):
    snap = replace(_clean(pr84), labels=("maintainer:rollback",), created_at=now, last_commit_at=now)
    repo = repo_config(policy, IMC)
    ok = evaluate(snap, policy=policy, repo=repo,
                  inputs=_inputs(now, revert_verified_empty=True, base_tip_is_reverted_merge=True))
    assert ok.is_revert_fast_path
    assert not any("veto" in f or "verdict" in f for f in ok.failures)
    advanced = evaluate(snap, policy=policy, repo=repo,
                        inputs=_inputs(now, revert_verified_empty=True, base_tip_is_reverted_merge=False))
    assert any("needs a human" in f for f in advanced.failures)
    unproven = evaluate(snap, policy=policy, repo=repo,
                        inputs=_inputs(now, revert_verified_empty=False, base_tip_is_reverted_merge=True))
    assert any("not verified empty" in f for f in unproven.failures)


def test_human_only_repo_and_never_merge_branches(pr84, policy, now):
    seen = now - timedelta(hours=25)
    maint = replace(_seen(_approved(_clean(pr84), seen), seen), repo="JiusiServe/omni-maintainer")
    result = evaluate(maint, policy=policy, repo=repo_config(policy, "JiusiServe/omni-maintainer"), inputs=_inputs(now))
    assert any("human-merge only" in f for f in result.failures)
    # with a human go after first-seen, the human-only repo becomes mergeable by the arbiter
    go = replace(maint, labels=maint.labels + ("maintainer-go",),
                 check_runs=maint.check_runs + (CheckRun("tests", "completed", "success", "github-actions", maint.head_sha),),
                 timeline=(
        {"event": "labeled", "label": {"name": "maintainer-go"}, "actor": {"login": "tzhouam", "type": "User"},
         "created_at": (seen + timedelta(minutes=1)).isoformat()},))
    result = evaluate(go, policy=policy, repo=repo_config(policy, "JiusiServe/omni-maintainer"), inputs=_inputs(now))
    assert result.ok, result.failures
    assert repo_config(policy, "JiusiServe/omni-maintainer").gate_may_merge
    intake = replace(_seen(_approved(_clean(pr84), seen), seen), head_ref="knowledge/intake-2026-09-03")
    result = evaluate(intake, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(now))
    assert any("never merged" in f for f in result.failures)
    # knowledge and adapter manifests are hard exclusions: a human go does not help
    base_seen = _seen(_approved(_clean(pr84), seen), seen)
    manifest = replace(base_seen,
                       files=(FileChange("adapters/vllm_omni/manifest.yaml", "modified", 1, 1, "+x"),),
                       check_runs=base_seen.check_runs + (
                           CheckRun("audit", "completed", "success", "github-actions", base_seen.head_sha),),
                       labels=("maintainer-go",), timeline=(
                           {"event": "labeled", "label": {"name": "maintainer-go"},
                            "actor": {"login": "tzhouam", "type": "User"},
                            "created_at": (seen + timedelta(minutes=1)).isoformat()},))
    # the arbiter refuses even with a human go...
    result = evaluate(manifest, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(now), enforce_caps=True)
    assert any("human promotion only" in f for f in result.failures)
    # ...while the published check passes so the human can merge through the ruleset
    published = evaluate(manifest, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(now), enforce_caps=False)
    assert published.ok and any("automation excluded" in n for n in published.notes)
    # and without a verified human go the published check fails too
    no_go = replace(manifest, labels=(), timeline=())
    assert any("only a human may merge" in f
               for f in evaluate(no_go, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(now), enforce_caps=False).failures)


def test_size_limits(pr84, policy, now):
    seen = now - timedelta(hours=25)
    big = replace(_seen(_approved(_clean(pr84), seen), seen), additions=700, deletions=0)
    result = evaluate(big, policy=policy, repo=repo_config(policy, IMC), inputs=_inputs(now))
    assert any("700 lines" in f for f in result.failures)
