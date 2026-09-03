"""CLI smoke tests against a fake ``gh`` that serves the captured fixtures."""

from __future__ import annotations

import json
import re

import pytest

from omni_maintainer import cli
from omni_maintainer.routine.ghcli import GhResult
from conftest import load


class FakeGh:
    """Answers the exact API paths the gate reads; records every write."""

    def __init__(self):
        self.writes: list[list[str]] = []
        self.pull = load("pr84_pull.json")

    def api(self, path, *, method="GET", fields=None, paginate=False, raw_fields=None):
        if re.search(r"/pulls/84$", path):
            return self.pull
        if "/pulls/84/files" in path:
            return load("pr84_files.json")
        if "/pulls/84/reviews" in path:
            return load("pr84_reviews.json")
        if "/issues/84/comments" in path:
            return load("pr84_comments.json")
        if "/pulls/84/commits" in path:
            return load("pr84_commits.json")
        if "/check-runs" in path:
            return [load("pr84_checkruns.json")]
        if "/issues/84/timeline" in path:
            return []
        if path.startswith("search/issues"):
            return {"total_count": 0, "items": []}
        if "/issues?labels=" in path:
            return []
        if re.search(r"/issues/\d+/timeline", path):
            return []
        if re.search(r"/issues/\d+$", path):
            return {"number": 1, "body": "", "labels": []}
        if "/actions/workflows/" in path and "/runs" in path:
            return {"workflow_runs": []}
        if "/compare/main..." in path:
            return {"status": "identical"}
        raise AssertionError(f"unexpected read: {path}")

    def read(self, args, stdin=None):
        return GhResult(True, "", "")

    def write(self, args, stdin=None):
        self.writes.append(list(args))
        return GhResult(True, "", "", dry_run=True)


@pytest.fixture
def fake(monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(cli, "Gh", lambda: gh)
    monkeypatch.setenv("MAINT_DRY_RUN", "1")
    return gh


def test_gate_evaluate_reports_every_failed_rule(fake, capsys):
    rc = cli.main(["gate", "evaluate", "--repo", "JiusiServe/InferMatrixCopilot", "--pr", "84", "--publish"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_FAIL and out["ok"] is False
    assert any("mergeable=False" in f for f in out["failures"])
    assert any("no reviewer verdict" in f for f in out["failures"])
    # the check run was published (dry-run) with a failure conclusion
    publish = [w for w in fake.writes if w[:2] == ["api", "repos/JiusiServe/InferMatrixCopilot/check-runs"]]
    assert publish and "-X" in publish[0]


def test_pending_check_is_published_before_any_read(fake, capsys):
    head = fake.pull["head"]["sha"]
    rc = cli.main(["gate", "evaluate", "--repo", "JiusiServe/InferMatrixCopilot", "--pr", "84", "--publish",
                   "--head", head])
    assert rc == cli.EXIT_FAIL
    capsys.readouterr()
    checks = [w for w in fake.writes if w[:2] == ["api", "repos/JiusiServe/InferMatrixCopilot/check-runs"]]
    assert len(checks) == 2, "one in_progress run first, then the completed one"
    # if the head moved since the caller read it, the actual head gets its own pending run as well
    fake.writes.clear()
    rc = cli.main(["gate", "evaluate", "--repo", "JiusiServe/InferMatrixCopilot", "--pr", "84", "--publish",
                   "--head", "0" * 40])
    capsys.readouterr()
    checks = [w for w in fake.writes if w[:2] == ["api", "repos/JiusiServe/InferMatrixCopilot/check-runs"]]
    assert len(checks) == 3, "pending on the stale head, pending on the real head, then the completed one"
    # and if that second pending publish fails, the job crashes instead of failing the stale head
    from omni_maintainer.routine.ghcli import GhError
    calls = {"n": 0}

    def flaky_write(args, stdin=None):
        if args[:2] == ["api", "repos/JiusiServe/InferMatrixCopilot/check-runs"]:
            calls["n"] += 1
            if calls["n"] == 2:
                raise GhError("boom on the real head")
        return GhResult(True, "", "", dry_run=True)

    fake.write = flaky_write
    rc = cli.main(["gate", "evaluate", "--repo", "JiusiServe/InferMatrixCopilot", "--pr", "84", "--publish",
                   "--head", "0" * 40])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_CRASH and out["head"] == head and calls["n"] == 2


def test_publish_failure_crashes_the_job_instead_of_looking_like_a_failed_bar(fake, capsys):
    from omni_maintainer.routine.ghcli import GhError

    def failing_write(args, stdin=None):
        if args[:2] == ["api", "repos/JiusiServe/InferMatrixCopilot/check-runs"]:
            raise GhError("boom")
        return GhResult(True, "", "", dry_run=True)

    fake.write = failing_write
    rc = cli.main(["gate", "evaluate", "--repo", "JiusiServe/InferMatrixCopilot", "--pr", "84", "--publish"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_CRASH and "publish_error" in out
    assert cli.EXIT_CRASH >= 2 and cli.EXIT_FAIL == 1


def test_published_check_stays_pr_local_when_global_state_is_unreadable(fake, capsys, monkeypatch):
    from omni_maintainer.routine.preflight import PreflightError
    monkeypatch.setattr(cli, "run_preflight", lambda gh, policy, now: (_ for _ in ()).throw(PreflightError("ledger down")))
    rc = cli.main(["gate", "evaluate", "--repo", "JiusiServe/InferMatrixCopilot", "--pr", "84"])
    out = json.loads(capsys.readouterr().out)
    # the PR's own failures are still reported and the global gap is a note, not a crash
    assert rc == cli.EXIT_FAIL and any("unreadable" in n for n in out["notes"])
    # the arbiter's view fails closed instead
    rc = cli.main(["gate", "evaluate", "--repo", "JiusiServe/InferMatrixCopilot", "--pr", "84", "--enforce-caps"])
    assert rc == cli.EXIT_CRASH


def test_preflight_reports_counters(fake, capsys):
    rc = cli.main(["preflight"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK and out["ok"] and out["merges_today"] == 0 and out["paused"] is False


def test_arbiter_is_inert_until_enabled(fake, capsys):
    rc = cli.main(["gate", "arbiter"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK and out["merged"] == [] and "merges_enabled" in out["note"]
    assert not any(w[:2] == ["pr", "merge"] for w in fake.writes)


class RollbackGh(FakeGh):
    """An open incident in rollback:pr-open whose revert PR has merged and deployed."""

    MERGE = "a" * 40
    PRE = "b" * 40
    REVERT_HEAD = "c" * 40

    def __init__(self):
        super().__init__()
        from omni_maintainer.monitor.rollback import PR_OPEN, rollback_marker
        from omni_maintainer.monitor.canary import Baseline, CanaryRecord
        self.marker = rollback_marker(incident=50, revert_pr=9, merge_sha=self.MERGE, pre_merge_sha=self.PRE,
                                      expected_revert_sha=self.REVERT_HEAD)
        self.incident = {"number": 50, "title": "[incident] trip", "user": {"login": "maint-bot"},
                         "body": "trip evidence\n\n<!-- canary #40 -->",
                         "labels": [{"name": "maintainer:incident"}, {"name": PR_OPEN}]}
        revert_record = CanaryRecord(repo="JiusiServe/omni-reviewbot", merge_sha="d" * 40, pre_merge_sha=self.MERGE,
                                     pr_number=9, deploy_run_id=777, opened_at="2026-09-03T00:00:00+00:00",
                                     baseline=Baseline(0, 0, 0.0, 0, 0, 0, 0, None, "2026-09-03T00:10:00+00:00"))
        self.revert_canary = {"number": 41, "title": "[canary] deploy dddddddd", "body": revert_record.to_marker(),
                              "labels": [{"name": "maintainer:canary"}]}

    def api(self, path, *, method="GET", fields=None, paginate=False, raw_fields=None):
        if "issues?labels=maintainer:incident&state=open" in path:
            return [self.incident]
        if "/issues/50/comments" in path:
            return [{"user": {"login": "maint-bot"}, "body": "revert PR opened\n\n" + self.marker}]
        if path.endswith("/pulls/9"):
            return {"merged": True, "state": "closed", "merged_at": "2026-09-03T00:05:00Z"}
        if "issues?labels=maintainer:canary&state=all" in path:
            return [self.revert_canary]
        if "/actions/runs/777/jobs" in path:
            return {"jobs": [{"name": "deploy-production", "conclusion": "success"}]}
        if "/actions/runs/777" in path:
            return {"status": "completed", "conclusion": "success"}
        if "/issues/41/comments" in path:
            return []
        if "issues?labels=maintainer:canary&state=open" in path:
            return []
        if path.startswith("repos/JiusiServe/omni-reviewbot/commits"):
            return []
        return super().api(path, method=method, fields=fields, paginate=paginate, raw_fields=raw_fields)


def _tick_with(monkeypatch, tmp_path, gh):
    monkeypatch.setattr(cli, "Gh", lambda: gh)
    monkeypatch.setattr(cli, "_fetch_digests", lambda policy, now: {})
    monkeypatch.setenv("MAINT_DRY_RUN", "1")
    import omni_maintainer.config as config
    policy = config.load_policy()
    policy["identities"]["routine_login"] = "maint-bot"
    monkeypatch.setattr(cli, "load_policy", lambda path=None: policy)
    return cli.main(["monitor", "tick", "--apply", "--state-file", str(tmp_path / "s.json")])


def test_monitor_tick_advances_rollback_after_revert_deploys(monkeypatch, tmp_path, capsys):
    gh = RollbackGh()
    rc = _tick_with(monkeypatch, tmp_path, gh)
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK
    [entry] = out["rollbacks"]
    # pr-open → merged → (next tick) deploying/verifying: one step per tick
    assert entry["from"] == "rollback:pr-open" and entry["to"] == "rollback:merged" and entry["revert_pr"] == 9
    edits = [w for w in gh.writes if w[:2] == ["issue", "edit"] and "50" in w]
    add = next(i for i, w in enumerate(gh.writes) if "--add-label" in w and "rollback:merged" in w)
    remove = next(i for i, w in enumerate(gh.writes) if "--remove-label" in w and "rollback:pr-open" in w)
    assert add < remove, "the new state label lands before the old one is removed"


class FailingGh(RollbackGh):
    """Raises on the first write matching ``fail_on``; records everything else."""

    def __init__(self, fail_on):
        super().__init__()
        self.fail_on = fail_on

    def write(self, args, stdin=None):
        if self.fail_on(list(args)):
            from omni_maintainer.routine.ghcli import GhError
            raise GhError("simulated GitHub failure")
        return super().write(args, stdin)


def test_rollback_transition_failure_leaves_old_state_in_place(monkeypatch, tmp_path, capsys):
    gh = FailingGh(lambda a: "--add-label" in a and "rollback:merged" in a)
    rc = _tick_with(monkeypatch, tmp_path, gh)
    assert rc == cli.EXIT_CRASH
    # nothing was removed, so the next tick sees rollback:pr-open again and retries
    assert not any("--remove-label" in w for w in gh.writes)


class RecoveredGh(RollbackGh):
    """Incident labeled rollback:recovered but still open: the close never landed."""

    def __init__(self, fail_close=False):
        super().__init__()
        from omni_maintainer.monitor.rollback import RECOVERED
        self.incident["labels"] = [{"name": "maintainer:incident"}, {"name": RECOVERED}]
        self.incident["state"] = "open"
        self.fail_close = fail_close

    def api(self, path, *, method="GET", fields=None, paginate=False, raw_fields=None):
        if path.endswith("/issues/40") or path.endswith("/issues/50"):
            return {"number": int(path[-2:]), "state": "open"}
        return super().api(path, method=method, fields=fields, paginate=paginate, raw_fields=raw_fields)

    def write(self, args, stdin=None):
        if self.fail_close and args[:2] == ["issue", "close"]:
            from omni_maintainer.routine.ghcli import GhError
            raise GhError("simulated close failure")
        return super().write(args, stdin)


class VerifyingGh(RollbackGh):
    """Incident in rollback:verifying with enough healthy ticks to recover."""

    def __init__(self, fail_on=None):
        super().__init__()
        from omni_maintainer.monitor.rollback import VERIFYING
        self.incident["labels"] = [{"name": "maintainer:incident"}, {"name": VERIFYING}]
        self.incident["state"] = "open"
        self.fail_on = fail_on

    def api(self, path, *, method="GET", fields=None, paginate=False, raw_fields=None):
        if "/issues/41/comments" in path:
            from omni_maintainer.monitor.canary import Tick
            from datetime import datetime, timezone
            healthy = {}  # no instances at all reads as healthy: nothing down, nothing dead, no failures
            ticks = [Tick(datetime(2026, 9, 3, h, tzinfo=timezone.utc), healthy, "completed", "success", 1, 0)
                     for h in (1, 2)]
            return [{"user": {"login": "maint-bot"}, "body": t.to_marker()} for t in ticks]
        if path.endswith("/issues/40") or path.endswith("/issues/50"):
            return {"number": int(path[-2:]), "state": "open"}
        return super().api(path, method=method, fields=fields, paginate=paginate, raw_fields=raw_fields)

    def write(self, args, stdin=None):
        if self.fail_on and self.fail_on(list(args)):
            from omni_maintainer.routine.ghcli import GhError
            raise GhError("simulated failure")
        return super().write(args, stdin)


def test_recovery_labels_but_never_closes_the_incident(monkeypatch, tmp_path, capsys):
    gh = VerifyingGh()
    assert _tick_with(monkeypatch, tmp_path, gh) == cli.EXIT_OK
    order = [tuple(w[:3]) for w in gh.writes]
    label_at = next(i for i, w in enumerate(gh.writes) if "--add-label" in w and "rollback:recovered" in w)
    close_canary_at = next(i for i, w in enumerate(gh.writes) if w[:3] == ["issue", "close", "40"])
    assert close_canary_at < label_at, order
    # automation never closes an incident: only a human may (and a bot close is reopened)
    assert not any(w[:3] == ["issue", "close", "50"] for w in gh.writes)
    assert any(w[:3] == ["issue", "comment", "50"] and "only a human may close" in (w[-1] if isinstance(w[-1], str) else "")
               or (w[:3] == ["issue", "comment", "50"]) for w in gh.writes)
    # if the label write fails, nothing terminal is recorded and the next tick retries from verifying
    failing = VerifyingGh(fail_on=lambda a: "--add-label" in a and "rollback:recovered" in a)
    assert _tick_with(monkeypatch, tmp_path, failing) == cli.EXIT_CRASH
    assert not any("rollback:recovered" in w and "--add-label" in w for w in failing.writes[:-1])


def test_terminal_state_is_reconciled_on_later_ticks(monkeypatch, tmp_path, capsys):
    gh = RecoveredGh()
    rc = _tick_with(monkeypatch, tmp_path, gh)
    assert rc == cli.EXIT_OK
    closes = [w for w in gh.writes if w[:2] == ["issue", "close"]]
    assert {w[2] for w in closes} == {"40"}, "the original canary is closed even though the label already says recovered; the incident is left for a human"
    # and a failing close is retried next tick because nothing else changes
    gh2 = RecoveredGh(fail_close=True)
    assert _tick_with(monkeypatch, tmp_path, gh2) == cli.EXIT_CRASH


def test_failed_rollback_files_one_blocked_issue(monkeypatch, tmp_path, capsys):
    from omni_maintainer.monitor.rollback import FAILED

    class FailedGh(RollbackGh):
        def __init__(self):
            super().__init__()
            self.incident["labels"] = [{"name": "maintainer:incident"}, {"name": FAILED}]
            self.blocked = []

        def api(self, path, *, method="GET", fields=None, paginate=False, raw_fields=None):
            if "issues?labels=maintainer:blocked" in path:
                return self.blocked
            return super().api(path, method=method, fields=fields, paginate=paginate, raw_fields=raw_fields)

    gh = FailedGh()
    assert _tick_with(monkeypatch, tmp_path, gh) == cli.EXIT_OK
    creates = [w for w in gh.writes if w[:2] == ["issue", "create"]]
    assert len(creates) == 1 and "[blocked] rollback failed for incident #50" in creates[0]
    # once the blocked issue exists (found by its marker), nothing is created again
    gh.blocked = [{"number": 60, "body": "<!-- omni-maintainer:blocked:rollback #50 -->"}]
    gh.writes.clear()
    assert _tick_with(monkeypatch, tmp_path, gh) == cli.EXIT_OK
    assert not any(w[:2] == ["issue", "create"] for w in gh.writes)


class PushGh(FakeGh):
    """RB main received pushes; the deploy workflow's push runs are the immutable record."""

    OLD = "1" * 40
    NEW = "2" * 40

    def __init__(self, run_actor: str, compare_status: str = "identical", merged_by: str = "tzhouam"):
        super().__init__()
        from datetime import datetime, timezone
        self.run_actor, self.compare_status, self.merged_by = run_actor, compare_status, merged_by
        now = datetime.now(timezone.utc).isoformat()
        self.runs = [{"id": 901, "head_sha": self.NEW, "event": "push", "created_at": now,
                      "actor": {"login": run_actor}},
                     {"id": 900, "head_sha": self.OLD, "event": "push", "created_at": now,
                      "actor": {"login": "whoever"}}]
        self.closed_incidents = []

    def api(self, path, *, method="GET", fields=None, paginate=False, raw_fields=None):
        if "/actions/workflows/deploy.yml/runs" in path:
            return {"workflow_runs": self.runs}
        if path.endswith(f"/commits/{self.NEW}"):
            return {"sha": self.NEW, "parents": [{"sha": self.OLD}], "commit": {"message": "hotfix by hand"}}
        if path.endswith(f"/commits/{self.OLD}"):
            return {"sha": self.OLD, "parents": [{"sha": "0" * 40}, {"sha": "9" * 40}],
                    "commit": {"message": "Merge pull request #1 from x/y"}}
        if path.endswith("/pulls/1"):
            return {"merged": True, "base": {"ref": "main"}, "merge_commit_sha": self.OLD,
                    "merged_by": {"login": self.merged_by}}
        if "/compare/main..." in path:
            return {"status": self.compare_status}
        if "issues?labels=maintainer:incident&state=closed" in path:
            return self.closed_incidents
        if "issues?labels=maintainer:canary&state=open" in path or "issues?labels=maintainer:incident&state=open" in path:
            return []
        if re.search(r"/issues/\d+/events", path):
            return [{"event": "closed", "actor": {"login": "claude[bot]"}}]
        return super().api(path, method=method, fields=fields, paginate=paginate, raw_fields=raw_fields)


def _push_tick(monkeypatch, tmp_path, gh):
    state = tmp_path / "s.json"
    # a tampered cursor pointing past the newest push must not hide it
    state.write_text(json.dumps({"cursors": {"rb_main_sha": PushGh.NEW}}))
    monkeypatch.setattr(cli, "Gh", lambda: gh)
    monkeypatch.setattr(cli, "_fetch_digests", lambda policy, now: {})
    monkeypatch.setenv("MAINT_DRY_RUN", "1")
    return cli.main(["monitor", "tick", "--apply", "--state-file", str(state)])


def test_pushes_come_from_immutable_run_metadata_not_cursors(monkeypatch, tmp_path, capsys):
    # a direct push by an unknown actor is an incident, whatever any ledger cursor says
    assert _push_tick(monkeypatch, tmp_path, PushGh(run_actor="someone")) == cli.EXIT_OK
    by_sha = {p["sha"]: p for p in json.loads(capsys.readouterr().out)["pushes"]}
    assert by_sha[PushGh.NEW]["kind"] == "direct_push_unattributed" and by_sha[PushGh.NEW]["incident"]
    assert by_sha[PushGh.OLD]["kind"] == "pr_merge" and not by_sha[PushGh.OLD]["incident"]
    # the same push by an allowlisted human (server-stamped actor): noted, not an incident
    assert _push_tick(monkeypatch, tmp_path, PushGh(run_actor="tzhouam")) == cli.EXIT_OK
    by_sha = {p["sha"]: p for p in json.loads(capsys.readouterr().out)["pushes"]}
    assert by_sha[PushGh.NEW]["kind"] == "direct_push_human" and not by_sha[PushGh.NEW]["incident"]
    # a PR merge performed by an app rather than a human is an incident on Tier B
    assert _push_tick(monkeypatch, tmp_path, PushGh(run_actor="tzhouam", merged_by="some-app[bot]")) == cli.EXIT_OK
    by_sha = {p["sha"]: p for p in json.loads(capsys.readouterr().out)["pushes"]}
    assert by_sha[PushGh.OLD]["kind"] == "pr_merge_unattributed" and by_sha[PushGh.OLD]["incident"]
    # a pushed commit no longer reachable from main means history was rewritten
    assert _push_tick(monkeypatch, tmp_path, PushGh(run_actor="tzhouam", compare_status="diverged")) == cli.EXIT_OK
    assert all(p["kind"] == "history_rewritten" and p["incident"] for p in json.loads(capsys.readouterr().out)["pushes"])


def test_incidents_closed_by_non_humans_are_reopened(monkeypatch, tmp_path, capsys):
    gh = PushGh(run_actor="tzhouam")
    gh.closed_incidents = [{"number": 88, "title": "[incident] x", "labels": [{"name": "maintainer:incident"}]}]
    assert _push_tick(monkeypatch, tmp_path, gh) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["reopened"] == [{"incident": 88, "closed_by": "claude[bot]"}]
    assert any(w[:3] == ["issue", "reopen", "88"] for w in gh.writes)


def test_issue_upsert_is_a_dry_run_until_issues_live(fake, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MAINT_DRY_RUN", raising=False)
    body = tmp_path / "b.md"
    body.write_text("diagnosis")
    state = tmp_path / "cursors.json"
    rc = cli.main(["issue", "upsert", "--repo", "JiusiServe/omni-reviewbot", "--fingerprint", "abcdef012345",
                   "--title", "[monitor] test", "--body-file", str(body), "--ack-instance", "vllm_gr",
                   "--ack-updated-at", "2026-09-02T03:00:00+00:00", "--state-file", str(state)])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK and "issues_live is false" in out["note"]
    # the write was printed, not executed, and the watermark still advanced (the dry run is the "issue")
    assert out["acked"] == {"vllm_gr_failed_watermark": "2026-09-02T03:00:00+00:00"}
    assert json.loads(state.read_text())["cursors"]["vllm_gr_failed_watermark"] == "2026-09-02T03:00:00+00:00"


def test_post_verdict_refuses_credentials(fake, tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text("looks fine, token ghp_" + "A" * 24)
    rc = cli.main(["gate", "post-verdict", "--repo", "JiusiServe/InferMatrixCopilot", "--pr", "84",
                   "--head", "a" * 40, "--verdict", "APPROVE", "--body-file", str(body)])
    assert rc == cli.EXIT_FAIL and not fake.writes
    body.write_text("looks fine")
    rc = cli.main(["gate", "post-verdict", "--repo", "JiusiServe/InferMatrixCopilot", "--pr", "84",
                   "--head", "a" * 40, "--verdict", "approve", "--body-file", str(body)])
    assert rc == cli.EXIT_OK and fake.writes[-1][:2] == ["api", "repos/JiusiServe/InferMatrixCopilot/pulls/84/reviews"]
