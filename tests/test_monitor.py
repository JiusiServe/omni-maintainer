from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from omni_maintainer.monitor import canary, rollback
from omni_maintainer.monitor.dashboard import Fetch, digest, job_time, new_failed_jobs, watcher_dead, window_saturated
from omni_maintainer.monitor.fingerprint import classify, excerpt, fingerprint, normalize
from omni_maintainer.monitor.issues import CREDENTIAL_PATTERNS, UnsafeText, credential_reason, ensure_safe, render_failure_body
from omni_maintainer.monitor.pushes import (DIRECT_HUMAN, DIRECT_UNATTRIBUTED, PR_MERGE_HUMAN, classify_commit,
                                            new_commits_since)
from conftest import load

from dataclasses import replace

# The snapshots were captured at 03:20 UTC; the clock sits a minute later so
# their watcher timestamps are fresh unless a test ages them deliberately.
NOW = datetime(2026, 9, 2, 3, 22, tzinfo=timezone.utc)


def _digests():
    omni = digest("vllm_omni", Fetch(True, 12.6, load("api_status_vllm_omni_20260902.json")), now=NOW)
    gr = digest("vllm_gr", Fetch(True, 0.4, load("api_status_vllm_gr_20260902.json")), now=NOW)
    return {"vllm_omni": omni, "vllm_gr": gr}


def _fresh(digests, at):
    """The same dashboards as seen at ``at`` with a live watcher."""
    return {name: replace(d, fetched_at=at, last_scan_at=at - timedelta(seconds=60) if d.ok else None)
            for name, d in digests.items()}


def test_digest_reads_real_snapshot():
    d = _digests()["vllm_omni"]
    assert d.ok and d.poll_interval_seconds == 120 and d.post_mode == "review"
    assert d.review_failed == 8 and d.gate_failures == 3 and d.split_failed == 5
    assert d.counts["failed"] == 31 and d.max_job_id > 0
    assert not watcher_dead(d.last_scan_at, poll_interval_seconds=120, now=NOW, multiplier=3)
    assert watcher_dead(d.last_scan_at, poll_interval_seconds=120, now=NOW + timedelta(hours=1), multiplier=3)
    gr = _digests()["vllm_gr"]
    failed = new_failed_jobs(gr.jobs, after=None)
    assert sorted(j["id"] for j in failed) == [31, 35, 43]
    stamps = [job_time(j) for j in failed]
    assert stamps == sorted(stamps) and all(s.tzinfo is not None for s in stamps)
    latest = max(job_time(j) for j in gr.jobs)
    assert new_failed_jobs(gr.jobs, after=latest) == []
    # a watermark just before the newest failure keeps only that failure
    newest_failure = max(stamps)
    assert [j["id"] for j in new_failed_jobs(gr.jobs, after=newest_failure - timedelta(seconds=1))] == [
        next(j["id"] for j in failed if job_time(j) == newest_failure)]
    assert not window_saturated(gr.jobs, since=min(job_time(j) for j in gr.jobs))
    assert window_saturated(gr.jobs, since=min(job_time(j) for j in gr.jobs) - timedelta(days=1))


def test_fingerprint_is_stable_across_volatile_tokens():
    a = "git --git-dir /workspace/wzr/omni-reviewbot/state-vllm-gr/git/repository.git worktree remove --force /workspace/x pr-276-abc123def4-9f8e7d6c failed"
    b = "git --git-dir /workspace/wzr/omni-reviewbot/state-vllm-gr/git/repository.git worktree remove --force /workspace/y pr-301-fedcba9876-11223344 failed"
    assert fingerprint("vllm_gr", "assign", a) == fingerprint("vllm_gr", "assign", b)
    assert fingerprint("vllm_gr", "assign", a) != fingerprint("vllm_omni", "assign", a)
    assert "<n>" in normalize(a) and "<path>" in normalize(a)


def test_classification_of_real_failures():
    gr = _digests()["vllm_gr"]
    errors = {j["id"]: j["error"] for j in gr.jobs if j.get("error")}
    assert classify("review", errors[43]) == "code"       # gate rejection
    assert classify("review", errors[35]) == "provider"   # InferMatrix Direct traceback
    assert classify("assign", errors[31]) == "infra"      # git worktree removal
    assert classify("review", "GitHubError: 502 Bad Gateway") == "external"


def test_excerpt_is_bounded_and_fenced():
    text = excerpt("x" * 1000 + "```", chars=400)
    assert text.startswith("```text\n") and text.endswith("\n```") and "…" in text
    assert "``````" not in text


def test_credential_patterns_match_reviewbot_source():
    expected = [r"gh[pousr]_[A-Za-z0-9]{16,}", r"github_pat_[A-Za-z0-9_]{20,}", r"AKIA[0-9A-Z]{16}",
                r"xox[baprs]-[A-Za-z0-9-]{10,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"]
    assert [p.pattern for p in CREDENTIAL_PATTERNS] == expected
    assert credential_reason("token ghp_" + "A" * 20) and "ghp_" not in credential_reason("token ghp_" + "A" * 20)
    with pytest.raises(UnsafeText):
        ensure_safe("-----BEGIN RSA PRIVATE KEY-----")
    assert ensure_safe("plain text") == "plain text"
    body = render_failure_body(instance="vllm_gr", kind="review", fp="abc123def456", klass="code", job_ids=[43],
                               job_links=["u"], head_sha="deadbeef" * 5, excerpt="```text\nx\n```", diagnosis="d")
    assert "omni-maintainer:fingerprint:abc123def456" in body


def test_canary_lifecycle_start_trip_close(policy):
    digests = _digests()
    record = canary.CanaryRecord(repo="JiusiServe/omni-reviewbot", merge_sha="a" * 40, pre_merge_sha="b" * 40,
                                 pr_number=1, deploy_run_id=1, opened_at=NOW.isoformat())
    parsed = canary.CanaryRecord.parse("intro\n" + record.to_marker())
    assert parsed and parsed.merge_sha == record.merge_sha

    def view(tick):
        return canary.TickView.parse(tick.to_marker(), watcher_multiplier=3)

    running = view(canary.Tick(NOW, digests, "in_progress", None, 0, 0))
    assert canary.evaluate(record, [running], now=NOW, policy=policy).action == canary.WAIT
    failed = view(canary.Tick(NOW, digests, "completed", "failure", 0, 0))
    assert canary.evaluate(record, [failed], now=NOW, policy=policy).action == canary.DEPLOY_FAILED
    ok = view(canary.Tick(NOW, digests, "completed", "success", 0, 0))
    assert canary.evaluate(record, [ok], now=NOW, policy=policy).action == canary.START

    record.baseline = canary.baseline_from(digests, started_at=NOW)
    assert record.baseline.gate_failures == 3 and record.baseline.review_failed == 8
    assert round(record.baseline.mean_attention, 2) == 3.62
    assert set(record.baseline.max_job_ids) == {"vllm_omni", "vllm_gr"}
    # the baseline survives the record marker round trip, cursors included
    restored = canary.CanaryRecord.parse(record.to_marker())
    assert restored.baseline.max_job_ids == record.baseline.max_job_ids
    # traffic since the deploy is by id; failures since by update time
    assert canary.jobs_since(record.baseline, digests) == (0, 0)
    aged = replace(record.baseline, max_job_ids={k: 0 for k in record.baseline.max_job_ids},
                   started_at=(NOW - timedelta(days=30)).isoformat())
    new_jobs, failed_jobs = canary.jobs_since(aged, digests)
    assert new_jobs == sum(len(d.jobs) for d in digests.values()) and failed_jobs == 3
    h1, h2, h3 = (NOW + timedelta(hours=i) for i in (1, 2, 3))
    t1 = view(canary.Tick(h1, _fresh(digests, h1), "completed", "success", 2, 0))
    assert canary.evaluate(record, [t1], now=h1, policy=policy).action == canary.WAIT
    # failed jobs above the floor trip the canary
    t2 = view(canary.Tick(h2, _fresh(digests, h2), "completed", "success", 4, 3))
    d = canary.evaluate(record, [t1, t2], now=h2, policy=policy)
    assert d.action == canary.TRIP and d.details["rule"] == "failed_jobs"
    # enough clean ticks and traffic close it
    ticks = [view(canary.Tick(h, _fresh(digests, h), "completed", "success", 2 * i, 0))
             for i, h in enumerate((h1, h2, h3), start=1)]
    d = canary.evaluate(record, ticks, now=h3, policy=policy)
    assert d.action == canary.CLOSE
    # not enough traffic keeps it open, the 24 h cap closes it anyway
    quiet = [view(canary.Tick(h, _fresh(digests, h), "completed", "success", 1, 0)) for h in (h1, h2, h3)]
    assert canary.evaluate(record, quiet, now=h3, policy=policy).action == canary.WAIT
    late = NOW + timedelta(hours=25)
    assert canary.evaluate(record, quiet + [view(canary.Tick(late, _fresh(digests, late), "completed", "success", 1, 0))],
                           now=late, policy=policy).action == canary.CLOSE
    # a single new gate failure trips when the 14-day baseline mean is zero
    live = canary.baseline_from(digests, started_at=NOW)
    record.baseline = replace(live, mean_gate_failures=0.0)
    same = view(canary.Tick(h1, _fresh(digests, h1), "completed", "success", 1, 0))
    assert canary.evaluate(record, [same], now=h1, policy=policy).action == canary.WAIT
    record.baseline = replace(live, mean_gate_failures=0.0, gate_failures=live.gate_failures - 1)
    d = canary.evaluate(record, [same], now=h1, policy=policy)
    assert d.action == canary.TRIP and d.details["rule"] == "gate_failures"
    # with a non-zero baseline mean, one more gate failure is tolerated, two trip
    record.baseline = replace(live, gate_failures=live.gate_failures - 1)
    assert canary.evaluate(record, [same], now=h1, policy=policy).action == canary.WAIT
    record.baseline = replace(live, gate_failures=live.gate_failures - 2)
    assert canary.evaluate(record, [same], now=h1, policy=policy).action == canary.TRIP


def test_canary_down_and_watcher_rules(policy):
    digests = _digests()
    record = canary.CanaryRecord(repo="r", merge_sha="a" * 40, pre_merge_sha="b" * 40, pr_number=None,
                                 deploy_run_id=None, opened_at=NOW.isoformat(),
                                 baseline=canary.baseline_from(digests, started_at=NOW))
    down = {**digests, "vllm_gr": digest("vllm_gr", Fetch(False, 90, None, "timeout"), now=NOW)}

    def view(tick):
        return canary.TickView.parse(tick.to_marker(), watcher_multiplier=3)

    h1, h2 = NOW + timedelta(hours=1), NOW + timedelta(hours=2)
    t1 = view(canary.Tick(h1, _fresh(down, h1), "completed", "success", 1, 0))
    assert canary.evaluate(record, [t1], now=h1, policy=policy).action == canary.WAIT
    t2 = view(canary.Tick(h2, _fresh(down, h2), "completed", "success", 2, 0))
    d = canary.evaluate(record, [t1, t2], now=h2, policy=policy)
    assert d.action == canary.TRIP and d.details["rule"] == "endpoint_down"
    # watcher dead after the grace period: the snapshot's last_scan_at is hours old by then
    t3 = view(canary.Tick(NOW + timedelta(hours=5), digests, "completed", "success", 3, 0))
    d = canary.evaluate(record, [t3], now=NOW + timedelta(hours=5), policy=policy)
    assert d.action == canary.TRIP and d.details["rule"] == "watcher_dead"
    # but not inside the grace period right after the deploy
    t4 = view(canary.Tick(NOW + timedelta(minutes=10), digests, "completed", "success", 1, 0))
    assert canary.evaluate(record, [t4], now=NOW + timedelta(minutes=10), policy=policy).action == canary.WAIT


def test_rollback_state_machine(policy):
    t0 = NOW
    f = rollback.RollbackFacts
    assert rollback.next_state(None, f(t0), policy=policy).state == rollback.PENDING
    assert rollback.next_state(rollback.PENDING, f(t0, revert_conflict=True), policy=policy).state == rollback.NEEDS_HUMAN
    assert rollback.next_state(rollback.PENDING, f(t0, revert_pr_number=9), policy=policy).state == rollback.PR_OPEN
    assert rollback.next_state(rollback.PR_OPEN, f(t0, 9, "open"), policy=policy).state == rollback.PR_OPEN
    assert rollback.next_state(rollback.PR_OPEN, f(t0, 9, "closed"), policy=policy).state == rollback.NEEDS_HUMAN
    assert rollback.next_state(rollback.PR_OPEN, f(t0, 9, "merged"), policy=policy).state == rollback.MERGED
    assert rollback.next_state(rollback.MERGED, f(t0, 9, "merged", revert_deploy_status="in_progress"),
                               policy=policy).state == rollback.DEPLOYING
    assert rollback.next_state(rollback.DEPLOYING, f(t0, 9, "merged", revert_deploy_status="completed",
                                                     revert_deploy_conclusion="failure"), policy=policy).state == rollback.FAILED
    verifying = rollback.next_state(rollback.DEPLOYING, f(t0, 9, "merged", revert_deploy_status="completed",
                                                          revert_deploy_conclusion="success"), policy=policy)
    assert verifying.state == rollback.VERIFYING
    assert rollback.next_state(rollback.VERIFYING, f(t0, healthy_ticks=1, revert_deploy_succeeded_at=t0),
                               policy=policy).state == rollback.VERIFYING
    done = rollback.next_state(rollback.VERIFYING, f(t0, healthy_ticks=2, revert_deploy_succeeded_at=t0), policy=policy)
    assert done.state == rollback.RECOVERED and done.hold is False
    late = rollback.next_state(rollback.VERIFYING, f(t0 + timedelta(hours=4), healthy_ticks=0,
                                                     revert_deploy_succeeded_at=t0), policy=policy)
    assert late.state == rollback.FAILED
    assert rollback.current_state(["x", rollback.PENDING, rollback.MERGED]) == rollback.MERGED


def test_push_classification_on_real_commits():
    commits = load("imc_main_commits.json")
    kinds = {c["sha"][:7]: classify_commit(c).kind for c in commits}
    assert kinds["c4f96aa"] == PR_MERGE_HUMAN
    assert kinds["cbe4d68"] == DIRECT_UNATTRIBUTED   # human push from an unlinked email
    assert kinds["229ded5"] == DIRECT_HUMAN
    assert classify_commit(commits[1]).pr_number == 115
    fresh = new_commits_since(commits, last_seen_sha=commits[2]["sha"])
    assert [c["sha"][:7] for c in fresh] == ["c4f96aa", "cbe4d68"]
    assert new_commits_since(commits, last_seen_sha=commits[0]["sha"]) == []
