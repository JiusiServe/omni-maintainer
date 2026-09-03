"""The reviewbot deploy guard is copied verbatim into omni-reviewbot as
``deploy/canary_guard.py`` and runs there on the self-hosted deploy runner
with only the default ``GITHUB_TOKEN``. These tests pin the properties that
make that copy safe to run at all."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "workflows" / "rb-canary-guard.py"
FRAGMENT = Path(__file__).resolve().parents[1] / "workflows" / "deploy-canary-job.yml"


def _tree() -> ast.Module:
    return ast.parse(GUARD.read_text(encoding="utf-8"))


def test_guard_parses_and_imports_only_the_standard_library() -> None:
    imported: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), sorted(imported - set(sys.stdlib_module_names))


def test_guard_never_shells_out() -> None:
    """No ``gh`` and no subprocess: the deploy runner is not guaranteed to
    have either, and a guard that cannot run blocks every deployment."""
    source = GUARD.read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert '"gh"' not in source and "'gh'" not in source


def test_guard_offers_exactly_the_two_documented_modes() -> None:
    functions = {n.name for n in ast.walk(_tree()) if isinstance(n, ast.FunctionDef)}
    assert {"mode_record", "mode_guard", "preflight"} <= functions
    source = GUARD.read_text(encoding="utf-8")
    assert "usage: canary_guard.py record|guard" in source


def test_every_refusal_is_shared_by_both_modes() -> None:
    """Both modes run the same preflight, so the deploy job re-verifies what
    canary-record checked: a rerun cannot skip a check by skipping a job."""
    source = GUARD.read_text(encoding="utf-8")
    body = source.split("def preflight(", 1)[1].split("def mode_record", 1)[0]
    for check in ("check_tip()", "check_not_paused()", "check_pusher()", "check_cooldown(", "check_holds("):
        assert check in body, check
    for mode in ("def mode_record", "def mode_guard"):
        assert "preflight(" in source.split(mode, 1)[1].split("\ndef ", 1)[0]


def test_the_live_kill_switch_is_the_ledger_label_not_the_repository_variable() -> None:
    """`vars.PRODUCTION_DEPLOY_ENABLED` is read when a run is queued, so it
    cannot stop a run already under way and no token can re-read it."""
    source = GUARD.read_text(encoding="utf-8")
    assert "maintainer:paused" in source
    assert "LEDGER_ISSUE" in source and "LEDGER_REPO" in source
    guard_body = source.split("def check_not_paused", 1)[1].split("\ndef ", 1)[0]
    assert "labeled" in guard_body and "unlabeled" in guard_body
    assert "HUMANS" in guard_body


def _load(monkeypatch):
    """Import the guard as a module with the environment it reads at import."""
    import importlib.util

    for name, value in {"GITHUB_REPOSITORY": "JiusiServe/omni-reviewbot", "GITHUB_SHA": "a" * 40,
                        "GITHUB_RUN_ID": "901", "GITHUB_RUN_ATTEMPT": "1", "HUMANS": "tzhouam",
                        "LEDGER_REPO": "JiusiServe/omni-reviewbot", "LEDGER_ISSUE": "32"}.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("canary_guard_under_test", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canary_issue(number: int, labels: list[str], run_id: int = 901, attempt: int = 1) -> dict:
    import json as _json
    record = {"deploy_run_id": run_id, "run_attempt": attempt, "merge_sha": "a" * 40}
    return {"number": number, "title": f"[canary] deploy {number}", "user": {"login": "github-actions[bot]"},
            "labels": [{"name": n} for n in labels],
            "body": f"<!-- omni-maintainer:canary:v1 {_json.dumps(record, sort_keys=True)} -->"}


def test_this_attempts_record_is_exempt_as_a_canary_only(monkeypatch, capsys) -> None:
    """A record that also carries an incident or rollback label still holds:
    the exemption exists so a deploy is not blocked by its own canary, not to
    let a labelled incident through."""
    guard = _load(monkeypatch)
    issue = _canary_issue(70, ["maintainer:canary"])

    def fake_api(path, *, paginate=False):
        if "state=open" in path and "labels=" not in path:
            return [issue]
        if "labels=maintainer:canary&state=open" in path:
            return [issue]
        if "labels=maintainer:incident&state=open" in path or "labels=maintainer:rollback&state=open" in path:
            return [issue] if any(l["name"] in ("maintainer:incident", "maintainer:rollback")
                                  for l in issue["labels"]) else []
        if "state=closed" in path:
            return []
        raise AssertionError(f"unexpected read: {path}")

    monkeypatch.setattr(guard, "api", fake_api)
    guard.check_holds(exempt_issue_number=70)          # its own canary: not a hold
    assert "proceeding" in capsys.readouterr().out

    issue["labels"].append({"name": "maintainer:incident"})
    with pytest.raises(SystemExit) as exit_info:
        guard.check_holds(exempt_issue_number=70)
    assert exit_info.value.code == 1
    assert "maintainer:incident" in capsys.readouterr().err


def test_the_fragment_wires_both_jobs_to_the_script() -> None:
    fragment = FRAGMENT.read_text(encoding="utf-8")
    assert "canary_guard.py record" in fragment
    assert "canary_guard.py guard" in fragment
    assert "LEDGER_ISSUE" in fragment
    for permission in ("issues: write", "actions: read", "pull-requests: read"):
        assert permission in fragment, permission
