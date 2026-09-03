from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omni_maintainer.config import PolicyError, load_policy, repo_config
from omni_maintainer.routine.ghcli import Gh, GhError, GhResult, _looks_mutating
from omni_maintainer.routine.release import ReleaseError, handshake_check, prepare_revert
from omni_maintainer.routine.workqueue import build_queue, classify_issue, stale_decisions
from conftest import load

NOW = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)


def test_policy_loads_and_validates(tmp_path):
    policy = load_policy()
    rb = repo_config(policy, "JiusiServe/omni-reviewbot")
    assert rb.tier == "B" and rb.deploys and not rb.gate_may_merge
    imc = repo_config(policy, "JiusiServe/InferMatrixCopilot")
    assert imc.gate_may_merge
    maint = repo_config(policy, "JiusiServe/omni-maintainer")
    assert maint.gate_may_merge and maint.human_only  # the bar demands a human go; the arbiter may then merge
    from omni_maintainer.config import DEFAULT_POLICY_PATH
    assert DEFAULT_POLICY_PATH.name == "policy.json" and DEFAULT_POLICY_PATH.parent.name == "omni_maintainer"
    broken = json.loads(DEFAULT_POLICY_PATH.read_text())
    broken["phase"]["revert_mode"] = "execute"
    path = tmp_path / "p.json"
    path.write_text(json.dumps(broken))
    with pytest.raises(PolicyError):
        load_policy(path)


def test_gh_wrapper_classifies_mutations_and_dry_runs(monkeypatch, capsys):
    assert _looks_mutating(["pr", "merge", "1"])
    assert _looks_mutating(["api", "repos/x/issues", "-X", "POST"])
    assert _looks_mutating(["api", "repos/x/issues", "-F", "title=x"])
    assert not _looks_mutating(["api", "repos/x/issues"])
    assert not _looks_mutating(["pr", "view", "1"])
    gh = Gh()
    with pytest.raises(GhError):
        gh.read(["pr", "merge", "1"])
    monkeypatch.setenv("MAINT_DRY_RUN", "1")
    result = gh.write(["pr", "merge", "1", "-R", "o/r"])
    assert result.dry_run and result.json() is None
    assert "[dry-run] gh pr merge 1 -R o/r" in capsys.readouterr().err


def test_handshake_check_reproduces_bundle_rules():
    good = {"sdk_api_version": "1.0.0", "direct_api_version": "1.0.0", "strict_api_version": "1.0.0",
            "knowledge_api_version": "1.0.0", "supports_expected_head": True, "supports_structured_result": True,
            "supports_post_false": True, "supports_file_locking": True, "supports_idempotent_strict_start": True,
            "supports_knowledge_curation": True, "max_strict_workers": 1,
            "supported_repositories": ["afd-plugin", "vllm-omni"], "distribution_version": "0.2.0"}
    assert handshake_check(good, expected_direct="1.0.0", expected_strict="1.0.0", expected_knowledge="1.0.0",
                           pinned_version="0.2.0") == []
    bad = {**good, "supports_knowledge_curation": False, "distribution_version": "0.3.0",
           "supported_repositories": ["vllm-omni"]}
    gaps = handshake_check(bad, expected_direct="1.0.0", expected_strict="1.0.0", expected_knowledge="1.0.0",
                           pinned_version="0.2.0")
    assert len(gaps) == 3


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def test_prepare_revert_refuses_when_main_advanced(tmp_path, monkeypatch):
    monkeypatch.setenv("MAINT_DRY_RUN", "1")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (work / "a.txt").write_text("base\n")
    _git(work, "add", "."); _git(work, "commit", "-q", "-m", "base"); _git(work, "branch", "-M", "main")
    pre = _git(work, "rev-parse", "HEAD")
    _git(work, "switch", "-q", "-c", "feature")
    (work / "a.txt").write_text("feature\n")
    _git(work, "commit", "-q", "-am", "feature")
    _git(work, "switch", "-q", "main")
    _git(work, "merge", "-q", "--no-ff", "-m", "Merge pull request #1 from x/feature", "feature")
    merge = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", "main")

    class FakeGh:
        def __init__(self):
            self.writes = []

        def write(self, args, stdin=None):
            self.writes.append((list(args), stdin))
            return GhResult(True, "https://github.com/o/r/pull/2\n", "")

    fake = FakeGh()
    result = prepare_revert(fake, repo="o/r", workdir=str(work), merge_sha=merge, pre_merge_sha=pre,
                            incident_url="https://github.com/o/r/issues/7", reason="test", labels=[],
                            incident_number=7)
    assert result.empty_against_pre_merge and result.branch == f"maintainer/revert-{merge[:8]}"
    assert result.pr_number == 2 and result.incident_marked
    from omni_maintainer.monitor.rollback import PR_OPEN, parse_rollback_marker
    marker_comment = next(stdin for args, stdin in fake.writes if args[:2] == ["issue", "comment"])
    marker = parse_rollback_marker(marker_comment)
    assert marker == {"incident": 7, "revert_pr": 2, "merge_sha": merge, "pre_merge_sha": pre,
                      "expected_revert_sha": result.revert_sha}
    assert any(args[:2] == ["issue", "edit"] and PR_OPEN in args for args, _ in fake.writes)

    # main moves on: the revert must be refused
    _git(work, "switch", "-q", "main")
    (work / "b.txt").write_text("later\n")
    _git(work, "add", "."); _git(work, "commit", "-q", "-m", "later"); _git(work, "push", "-q", "origin", "main")
    _git(work, "branch", "-q", "-D", f"maintainer/revert-{merge[:8]}")
    with pytest.raises(ReleaseError, match="needs a human"):
        prepare_revert(FakeGh(), repo="o/r", workdir=str(work), merge_sha=merge, pre_merge_sha=pre,
                       incident_url="i", reason="test", labels=[])


def test_work_queue_ordering_and_rfc_gate():
    policy = load_policy()
    issues = {"JiusiServe/InferMatrixCopilot": [
        {"number": 1, "title": "[RFC] big thing", "labels": [], "body": "", "html_url": "u"},
        {"number": 2, "title": "crash on x", "labels": [{"name": "bug"}], "body": "", "html_url": "u"},
        {"number": 3, "title": "[monitor] review: gate", "labels": [{"name": "maintainer:filed"}, {"name": "code"}],
         "body": "", "html_url": "u"},
        {"number": 4, "title": "[Feature] small", "labels": [{"name": "enhancement"}],
         "body": "Acceptance criteria: returns 200", "html_url": "u"},
        {"number": 5, "title": "[Feature] proposed", "labels": [{"name": "maintainer:proposed"}], "body": "", "html_url": "u"},
        {"number": 6, "title": "[monitor] infra", "labels": [{"name": "maintainer:filed"}, {"name": "infra"}], "body": "", "html_url": "u"},
    ]}
    pulls = {"JiusiServe/InferMatrixCopilot": [{"number": 84, "title": "pr", "draft": False, "html_url": "u"},
                                               {"number": 42, "title": "draft", "draft": True, "html_url": "u"}]}
    queue = build_queue(issues, pulls, policy=policy)
    assert [(i.number, i.kind) for i in queue] == [(3, "filed_code"), (2, "bug"), (4, "enhancement"),
                                                    (84, "pr_analysis"), (1, "rfc_proposal")]
    assert classify_issue(issues["JiusiServe/InferMatrixCopilot"][4], policy=policy) is None


def test_ledger_cursor_marker_roundtrip():
    from omni_maintainer.routine.ledger import cursor_marker, parse_cursors, replace_cursors
    body = "## Maintainer ledger\n\n- paused: no\n"
    updated = replace_cursors(body, {"vllm_omni_last_job_id": 1082, "rb_main_sha": "a" * 40})
    assert parse_cursors(updated) == {"vllm_omni_last_job_id": 1082, "rb_main_sha": "a" * 40}
    again = replace_cursors(updated, {"vllm_omni_last_job_id": 1090})
    assert parse_cursors(again) == {"vllm_omni_last_job_id": 1090}
    assert again.count("omni-maintainer:cursors:v1") == 1
    assert parse_cursors("no marker") == {} and cursor_marker({}).startswith("<!-- omni-maintainer:cursors:v1")


def test_stale_decisions_respect_go_and_assignees():
    policy = load_policy()
    old = (NOW - timedelta(days=31)).isoformat()
    mid = (NOW - timedelta(days=15)).isoformat()
    pulls = [
        {"number": 1, "updated_at": old, "labels": [{"name": "maintainer:stale"}], "assignees": []},
        {"number": 2, "updated_at": mid, "labels": [], "assignees": []},
        {"number": 3, "updated_at": old, "labels": [{"name": "maintainer-go"}], "assignees": []},
        {"number": 4, "updated_at": old, "labels": [], "assignees": [{"login": "x"}]},
        {"number": 5, "updated_at": NOW.isoformat(), "labels": [], "assignees": []},
    ]
    decisions = {d.number: d.action for d in stale_decisions(pulls, now=NOW, policy=policy)}
    assert decisions == {1: "close", 2: "label", 5: "none"}
