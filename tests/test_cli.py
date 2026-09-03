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


def test_preflight_reports_counters(fake, capsys):
    rc = cli.main(["preflight"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK and out["ok"] and out["merges_today"] == 0 and out["paused"] is False


def test_arbiter_is_inert_until_enabled(fake, capsys):
    rc = cli.main(["gate", "arbiter"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK and out["merged"] == [] and "merges_enabled" in out["note"]
    assert not any(w[:2] == ["pr", "merge"] for w in fake.writes)


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
