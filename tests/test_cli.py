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


def test_recovery_persists_the_label_before_closing_the_incident(monkeypatch, tmp_path, capsys):
    gh = VerifyingGh()
    assert _tick_with(monkeypatch, tmp_path, gh) == cli.EXIT_OK
    order = [tuple(w[:3]) for w in gh.writes]
    label_at = next(i for i, w in enumerate(gh.writes) if "--add-label" in w and "rollback:recovered" in w)
    close_incident_at = next(i for i, w in enumerate(gh.writes) if w[:3] == ["issue", "close", "50"])
    close_canary_at = next(i for i, w in enumerate(gh.writes) if w[:3] == ["issue", "close", "40"])
    assert close_canary_at < label_at < close_incident_at, order
    # if the label write fails, the incident is NOT closed and the next tick retries from verifying
    failing = VerifyingGh(fail_on=lambda a: "--add-label" in a and "rollback:recovered" in a)
    assert _tick_with(monkeypatch, tmp_path, failing) == cli.EXIT_CRASH
    assert not any(w[:3] == ["issue", "close", "50"] for w in failing.writes)


def test_terminal_state_is_reconciled_on_later_ticks(monkeypatch, tmp_path, capsys):
    gh = RecoveredGh()
    rc = _tick_with(monkeypatch, tmp_path, gh)
    assert rc == cli.EXIT_OK
    closes = [w for w in gh.writes if w[:2] == ["issue", "close"]]
    assert {w[2] for w in closes} == {"40", "50"}, "original canary and incident are closed even though the label already says recovered"
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
    """RB main gained one direct push; a canary record claims a human pushed it."""

    OLD = "1" * 40
    NEW = "2" * 40

    def __init__(self, run_actor: str, record_pusher: str = "tzhouam", run_event: str = "push"):
        super().__init__()
        from omni_maintainer.monitor.canary import CanaryRecord
        self.run_actor, self.run_event = run_actor, run_event
        self.record = CanaryRecord(repo="JiusiServe/omni-reviewbot", merge_sha=self.NEW, pre_merge_sha=self.OLD,
                                   pr_number=None, deploy_run_id=901, opened_at="2026-09-03T00:00:00+00:00",
                                   pusher=record_pusher)

    def api(self, path, *, method="GET", fields=None, paginate=False, raw_fields=None):
        if path.startswith("repos/JiusiServe/omni-reviewbot/commits?sha=main"):
            return [{"sha": self.NEW, "parents": [{"sha": self.OLD}], "commit": {"message": "hotfix by hand"}},
                    {"sha": self.OLD, "parents": [{"sha": "0" * 40}], "commit": {"message": "Merge pull request #1 from x/y"}}]
        if "issues?labels=maintainer:canary&state=all" in path:
            return [{"number": 70, "body": self.record.to_marker(), "labels": [{"name": "maintainer:canary"}]}]
        if path.endswith("/actions/runs/901"):
            return {"event": self.run_event, "head_sha": self.NEW, "actor": {"login": self.run_actor}}
        if "issues?labels=maintainer:canary&state=open" in path or "issues?labels=maintainer:incident" in path:
            return []
        return super().api(path, method=method, fields=fields, paginate=paginate, raw_fields=raw_fields)


def _push_tick(monkeypatch, tmp_path, gh):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"cursors": {"rb_main_sha": PushGh.OLD}}))
    monkeypatch.setattr(cli, "Gh", lambda: gh)
    monkeypatch.setattr(cli, "_fetch_digests", lambda policy, now: {})
    monkeypatch.setenv("MAINT_DRY_RUN", "1")
    return cli.main(["monitor", "tick", "--state-file", str(state)])


def test_direct_push_attribution_uses_run_metadata_not_the_record(monkeypatch, tmp_path, capsys):
    # the record says tzhouam pushed, but the immutable run says someone else did: incident
    assert _push_tick(monkeypatch, tmp_path, PushGh(run_actor="someone")) == cli.EXIT_OK
    [push] = json.loads(capsys.readouterr().out)["pushes"]
    assert push["kind"] == "direct_push_unattributed" and push["incident"]
    # record and run agree on an allowlisted human: noted, not an incident
    assert _push_tick(monkeypatch, tmp_path, PushGh(run_actor="tzhouam")) == cli.EXIT_OK
    [push] = json.loads(capsys.readouterr().out)["pushes"]
    assert push["kind"] == "direct_push_human" and not push["incident"] and push["pusher"] == "tzhouam"
    # a run that is not a push event proves nothing
    assert _push_tick(monkeypatch, tmp_path, PushGh(run_actor="tzhouam", run_event="workflow_dispatch")) == cli.EXIT_OK
    [push] = json.loads(capsys.readouterr().out)["pushes"]
    assert push["incident"]


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
