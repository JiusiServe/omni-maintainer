from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from omni_maintainer.gate.actors import human_label_active, human_pause_active
from omni_maintainer.gate.caps import merges_today, prs_opened_today
from omni_maintainer.gate.reads import IncompleteRead, build_snapshot, load_snapshot
from omni_maintainer.gate.verdict import (context_digest, format_marker,
                                          latest_verdict, parse_marker)
from omni_maintainer.gate.reads import Review
from conftest import load


def _ev(event, label, login, typ, at):
    return {"event": event, "label": {"name": label}, "actor": {"login": login, "type": typ}, "created_at": at.isoformat()}


def test_pause_is_human_asymmetric():
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    humans = ["tzhouam"]
    events = [_ev("labeled", "maintainer:paused", "tzhouam", "User", t0)]
    assert human_pause_active(events, label="maintainer:paused", humans=humans)
    # a bot removing it changes nothing
    events.append(_ev("unlabeled", "maintainer:paused", "claude[bot]", "Bot", t0 + timedelta(hours=1)))
    assert human_pause_active(events, label="maintainer:paused", humans=humans)
    # a human removing it lifts the pause
    events.append(_ev("unlabeled", "maintainer:paused", "tzhouam", "User", t0 + timedelta(hours=2)))
    assert not human_pause_active(events, label="maintainer:paused", humans=humans)
    # a bot adding it does not pause
    events.append(_ev("labeled", "maintainer:paused", "claude[bot]", "Bot", t0 + timedelta(hours=3)))
    assert not human_pause_active(events, label="maintainer:paused", humans=humans)
    # a non-allowlisted human cannot pause either
    events.append(_ev("labeled", "maintainer:paused", "stranger", "User", t0 + timedelta(hours=4)))
    assert not human_pause_active(events, label="maintainer:paused", humans=humans)


def test_go_label_latest_event_wins():
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    events = [_ev("labeled", "maintainer-go", "tzhouam", "User", t0),
              _ev("unlabeled", "maintainer-go", "tzhouam", "User", t0 + timedelta(hours=1)),
              _ev("labeled", "maintainer-go", "claude[bot]", "Bot", t0 + timedelta(hours=2))]
    assert not human_label_active(events, label="maintainer-go", humans=["tzhouam"], currently_present=True)
    assert not human_label_active(events[:1], label="maintainer-go", humans=["tzhouam"], currently_present=False)
    assert human_label_active(events[:1], label="maintainer-go", humans=["tzhouam"], currently_present=True,
                              after=t0 - timedelta(minutes=1))
    assert not human_label_active(events[:1], label="maintainer-go", humans=["tzhouam"], currently_present=True,
                                  after=t0 + timedelta(minutes=1))


def test_marker_roundtrip_and_reviewer_identity():
    head = "a" * 40
    ctx = context_digest("Fix the thing", "because it was broken")
    marker = format_marker(head, "APPROVE", ctx)
    assert parse_marker("intro\n" + marker + "\nbody") == (head, ctx, "APPROVE")
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    reviews = [Review("omni-maintainer-gate[bot]", "Bot", "COMMENTED", now, marker, head),
               Review("omni-maintainer-gate[bot]", "Bot", "COMMENTED", now + timedelta(minutes=1),
                      format_marker(head, "REVISE", ctx), head),
               Review("impostor", "User", "COMMENTED", now + timedelta(minutes=2), marker, head)]
    assert latest_verdict(reviews, head_sha=head, ctx=ctx, reviewer_login="omni-maintainer-gate[bot]") == "REVISE"
    assert latest_verdict(reviews[:1], head_sha="b" * 40, ctx=ctx, reviewer_login="omni-maintainer-gate[bot]") is None
    # A different description is a different review, whatever the head says.
    other = context_digest("Fix the thing", "because it was broken, actually")
    assert latest_verdict(reviews, head_sha=head, ctx=other,
                          reviewer_login="omni-maintainer-gate[bot]") is None
    # A v1 marker carried no digest and is not a verdict any more.
    v1 = f"<!-- omni-maintainer:review:v1 head={head} verdict=APPROVE -->"
    assert parse_marker(v1) is None
    assert latest_verdict([Review("omni-maintainer-gate[bot]", "Bot", "COMMENTED", now, v1, head)],
                          head_sha=head, ctx=ctx,
                          reviewer_login="omni-maintainer-gate[bot]") is None
    with pytest.raises(ValueError):
        format_marker("short", "APPROVE", ctx)


def test_build_snapshot_refuses_incomplete_collections(policy):
    pull = load("pr84_pull.json")
    args = dict(files=load("pr84_files.json"), reviews=[], comments=[], commits=load("pr84_commits.json"),
                check_runs=load("pr84_checkruns.json"), max_files=3000)
    snap = build_snapshot("JiusiServe/InferMatrixCopilot", pull=pull, **args)
    assert snap.number == 84 and len(snap.files) == pull["changed_files"]
    with pytest.raises(IncompleteRead):
        build_snapshot("JiusiServe/InferMatrixCopilot", pull=pull, **{**args, "files": args["files"][:1]})
    with pytest.raises(IncompleteRead):
        build_snapshot("JiusiServe/InferMatrixCopilot", pull=pull, **{**args, "max_files": 2})
    with pytest.raises(IncompleteRead):
        build_snapshot("JiusiServe/InferMatrixCopilot", pull=pull, **{**args, "commits": []})
    with pytest.raises(IncompleteRead):
        build_snapshot("JiusiServe/InferMatrixCopilot", pull=pull,
                       **{**args, "check_runs": {"total_count": 5, "check_runs": []}})


def test_load_snapshot_refuses_unknown_mergeability():
    pull = {**load("pr84_pull.json"), "mergeable": None}
    calls = []

    def runner(path, paginate):
        calls.append(path)
        if path.endswith("/pulls/84"):
            return pull
        return []

    with pytest.raises(IncompleteRead):
        load_snapshot(runner, "JiusiServe/InferMatrixCopilot", 84, max_files=3000, mergeable_retry=lambda: None)
    assert sum(p.endswith("/pulls/84") for p in calls) == 3


def test_caps_count_from_history_across_utc_boundary():
    merged = load("pr115_merged_pull.json")  # merged 2026-08-31T04:02:57Z by tzhouam
    same_day = datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)
    next_day = datetime(2026, 9, 1, 0, 1, tzinfo=timezone.utc)
    assert merges_today([merged], now=same_day, merger_logins=["tzhouam"]) == 1
    assert merges_today([merged], now=same_day, merger_logins=["omni-maintainer-gate[bot]"]) == 0
    assert merges_today([merged], now=next_day, merger_logins=["tzhouam"]) == 0
    pr = load("pr84_pull.json")
    at = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    assert prs_opened_today([pr], now=at, author_login=pr["user"]["login"]) == 1
    assert prs_opened_today([pr], now=at + timedelta(days=1), author_login=pr["user"]["login"]) == 0
