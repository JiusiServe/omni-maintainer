"""The reviewbot deploy guard is copied verbatim into omni-reviewbot as
``deploy/canary_guard.py`` and runs there on the self-hosted deploy runner
with only the default ``GITHUB_TOKEN``. These tests pin the properties that
make that copy safe to run at all."""

from __future__ import annotations

import ast
import re
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


def _load(monkeypatch, **extra):
    """Import the guard as a module with the environment it reads at import."""
    import importlib.util

    environment = {"GITHUB_REPOSITORY": "JiusiServe/omni-reviewbot", "GITHUB_SHA": "a" * 40,
                   "GITHUB_RUN_ID": "901", "GITHUB_RUN_ATTEMPT": "1", "HUMANS": "tzhouam",
                   "LEDGER_REPO": "JiusiServe/omni-reviewbot", "LEDGER_ISSUE": "32"}
    environment.update(extra)
    for name, value in environment.items():
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


def _record_issue(state: str) -> dict:
    import json

    record = {"deploy_run_id": 901, "run_attempt": 1, "merge_sha": "a" * 40}
    return {
        "number": 70, "state": state, "created_at": "2026-09-03T00:00:00Z",
        "user": {"login": "github-actions[bot]"},
        "body": "<!-- omni-maintainer:canary:v1 " + json.dumps(record, sort_keys=True) + " -->",
    }


def test_guard_refuses_a_canary_record_that_was_closed(monkeypatch) -> None:
    """A deploy that waited in a queue must not proceed after its canary was
    closed: the deployment would then be watched by nobody."""
    guard = _load(monkeypatch)

    def fake_api(path, *, paginate=False):
        if "actions/runs/901" in path:
            return {"created_at": "2026-09-02T00:00:00Z"}
        if "issues?state=all" in path:
            return [_record_issue("closed")]
        raise AssertionError(path)

    monkeypatch.setattr(guard, "api", fake_api)
    with pytest.raises(SystemExit) as exit_info:
        guard.record_for_this_attempt(require_open=True)
    assert exit_info.value.code == 1

    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: (
        {"created_at": "2026-09-02T00:00:00Z"} if "actions/runs/901" in path else [_record_issue("open")]
    ))
    issue, record = guard.record_for_this_attempt(require_open=True)
    assert issue["number"] == 70 and record["run_attempt"] == 1


def test_guard_rechecks_every_live_condition_after_the_dashboard_read(monkeypatch) -> None:
    """Reading two dashboards can take minutes when either is retrying, and the
    deploy command runs as soon as the guard returns."""
    guard = _load(monkeypatch)
    calls = {"preflight": 0, "record": 0}

    def fake_record(require_open=False):
        calls["record"] += 1
        assert require_open is True
        return _record_issue("open"), {}

    monkeypatch.setattr(guard, "record_for_this_attempt", fake_record)
    monkeypatch.setattr(guard, "preflight", lambda exempt_issue_number: calls.__setitem__(
        "preflight", calls["preflight"] + 1))
    monkeypatch.setattr(guard, "read_dashboards", lambda: {
        "vllm_omni": {"counts": {"failed": 1}, "jobs": [{"id": 5}], "history": {"days": []}}
    })
    posted: list = []
    monkeypatch.setattr(guard, "post", lambda path, body: posted.append((path, body)))

    guard.mode_guard()

    assert calls["preflight"] == 2, "live checks run before and after the slow dashboard read"
    assert calls["record"] == 2, "the canary must still be open after the read too"
    assert posted and "omni-maintainer:predeploy:v1" in posted[0][1]["body"]


def test_the_closed_incident_lookup_uses_a_url_safe_timestamp(monkeypatch) -> None:
    """`+00:00` decodes as a space in a query string; GitHub rejects it, and
    every hold check would then fail closed and block deployment."""
    guard = _load(monkeypatch)
    seen: list = []

    def fake_api(path, *, paginate=False):
        seen.append(path)
        return []

    monkeypatch.setattr(guard, "api", fake_api)
    guard.issues("maintainer:incident", "closed")
    assert seen and "since=" in seen[0]
    assert "+" not in seen[0], seen[0]
    assert re.search(r"since=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", seen[0]), seen[0]


def test_live_deployment_pages_past_a_run_of_failed_pushes(monkeypatch) -> None:
    """After a stretch of failed or deploy-less pushes, the live release must
    still be found: the cooldown and the rollback baseline both depend on it."""
    guard = _load(monkeypatch)
    page_one = {"total_count": 101,
                "workflow_runs": [{"id": i, "head_sha": f"{i:040x}", "conclusion": "failure",
                                   "status": "completed"} for i in range(100)]}
    page_two = {"total_count": 101,
                "workflow_runs": [{"id": 500, "head_sha": "b" * 40, "conclusion": "cancelled",
                                   "status": "completed"}]}

    def fake_api(path, *, paginate=False):
        # "&page=", not "page=": per_page=100 contains "page=1"
        if path.endswith("&page=1"):
            return page_one
        if path.endswith("&page=2"):
            return page_two
        return {"total_count": 101, "workflow_runs": []}

    monkeypatch.setattr(guard, "api", fake_api)
    # only run 500 actually deployed; note it is a CANCELLED run whose deploy
    # job succeeded, which still counts: the job's conclusion is authoritative
    monkeypatch.setattr(guard, "deploy_job", lambda run_id, attempt=None: (
        {"conclusion": "success", "completed_at": "2026-09-01T00:00:00Z"} if run_id == 500 else None))
    live = guard.live_deployment()
    assert live is not None and live["run"]["id"] == 500

    # and when the cap is reached, it refuses whether or not it found a
    # candidate: an unscanned older run may have been rerun and deployed later
    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: {
        "total_count": 100000,
        "workflow_runs": [{"id": 1, "head_sha": "c" * 40, "conclusion": "failure", "status": "completed"}] * 100,
    })
    monkeypatch.setattr(guard, "deploy_job", lambda run_id, attempt=None: None)
    with pytest.raises(SystemExit):
        guard.live_deployment()


def test_an_earlier_deployment_of_the_same_commit_still_counts(monkeypatch) -> None:
    """A rerun, or a revert and reapply, deploys the same SHA again. Hiding
    that earlier deployment would skip the cooldown and take the rollback
    baseline from the wrong release."""
    guard = _load(monkeypatch)
    same_sha_run = {"id": 400, "head_sha": "a" * 40, "conclusion": "success", "status": "completed"}
    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: {
        "total_count": 1, "workflow_runs": [same_sha_run]})
    monkeypatch.setattr(guard, "deploy_job", lambda run_id, attempt=None: {
        "conclusion": "success", "completed_at": "2026-09-01T00:00:00Z"})

    live = guard.live_deployment()
    assert live is not None and live["run"]["id"] == 400

    # ...this run's own first attempt is not a previous deployment...
    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: {
        "total_count": 1, "workflow_runs": [{**same_sha_run, "id": guard.RUN_ID}]})
    assert guard.live_deployment() is None


def test_an_earlier_attempt_of_this_run_is_a_previous_deployment(monkeypatch) -> None:
    """A rerun keeps the run id and increments run_attempt, so a successful
    deploy by an earlier attempt is the newest deployment there is."""
    guard = _load(monkeypatch, GITHUB_RUN_ATTEMPT="2")
    asked: list = []

    def fake_api(path, *, paginate=False):
        asked.append(path)
        if path.endswith("actions/runs/901"):
            return {"id": 901, "head_sha": "a" * 40}
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(guard, "api", fake_api)
    monkeypatch.setattr(guard, "deploy_job", lambda run_id, attempt=None: (
        {"conclusion": "success", "completed_at": "2026-09-03T00:00:00Z"} if attempt == 1 else None))

    live = guard.live_deployment()
    assert live is not None and live["attempt"] == 1
    assert any("/runs?" in path for path in asked), "the run scan still runs, to look for a newer one"


def test_a_rerun_checks_who_pressed_the_button(monkeypatch) -> None:
    """On a rerun GITHUB_ACTOR is still the original pusher; the person who
    started the rerun is GITHUB_TRIGGERING_ACTOR."""
    guard = _load(monkeypatch)
    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: {"parents": [{"sha": "b" * 40}]})
    monkeypatch.setenv("GITHUB_ACTOR", "tzhouam")

    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "tzhouam")
    guard.check_pusher()

    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "someone-else")
    with pytest.raises(SystemExit) as exit_info:
        guard.check_pusher()
    assert exit_info.value.code == 1


def test_guard_refuses_when_the_canary_vanishes_during_the_dashboard_read(monkeypatch) -> None:
    """The record is re-read last, after the final live checks: a canary that
    is deleted or unlabelled mid-flight is as disqualifying as a closed one."""
    guard = _load(monkeypatch)
    lookups = {"n": 0}

    def fake_record(require_open=False):
        lookups["n"] += 1
        return (_record_issue("open"), {}) if lookups["n"] == 1 else (None, None)

    monkeypatch.setattr(guard, "record_for_this_attempt", fake_record)
    monkeypatch.setattr(guard, "preflight", lambda exempt_issue_number: None)
    monkeypatch.setattr(guard, "read_dashboards", lambda: {
        "vllm_omni": {"counts": {"failed": 0}, "jobs": [], "history": {"days": []}}})
    monkeypatch.setattr(guard, "post", lambda path, body: None)

    with pytest.raises(SystemExit) as exit_info:
        guard.mode_guard()
    assert exit_info.value.code == 1
    assert lookups["n"] == 2


def test_a_historical_run_whose_first_attempt_deployed_still_counts(monkeypatch) -> None:
    """GitHub returns only the latest attempt's jobs unless filter=all is
    asked for, so a run that deployed on attempt 1 and failed early on
    attempt 2 would otherwise read as never deployed."""
    guard = _load(monkeypatch)
    asked: list = []

    def fake_api(path, *, paginate=False):
        asked.append(path)
        return {"jobs": [
            {"name": "build-and-test", "conclusion": "success", "run_attempt": 1},
            {"name": "deploy-production", "conclusion": "success", "run_attempt": 1,
             "completed_at": "2026-09-01T00:00:00Z"},
            {"name": "deploy-production", "conclusion": "failure", "run_attempt": 2,
             "completed_at": "2026-09-02T00:00:00Z"},
        ]}

    monkeypatch.setattr(guard, "api", fake_api)
    job = guard.deploy_job(400)
    assert job["conclusion"] == "success" and job["run_attempt"] == 1
    assert "filter=all" in asked[0], asked[0]


def test_the_live_release_is_the_one_that_finished_last(monkeypatch) -> None:
    """Runs are listed by creation, but rerunning an older run deploys after a
    newer one; the cooldown and the rollback baseline follow the deploy job's
    own completion time."""
    guard = _load(monkeypatch)
    newer_run = {"id": 200, "head_sha": "b" * 40, "status": "completed",
                 "conclusion": "success", "updated_at": "2026-09-01T00:00:00Z"}
    older_run_rerun = {"id": 100, "head_sha": "c" * 40, "status": "completed",
                       "conclusion": "success", "updated_at": "2026-09-02T12:00:00Z"}
    jobs = {200: {"conclusion": "success", "completed_at": "2026-09-01T00:00:00Z"},
            100: {"conclusion": "success", "completed_at": "2026-09-02T12:00:00Z"}}

    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: {
        "total_count": 2, "workflow_runs": [newer_run, older_run_rerun]})
    monkeypatch.setattr(guard, "deploy_job", lambda run_id, attempt=None: jobs[run_id])

    live = guard.live_deployment()
    assert live["run"]["id"] == 100, "the rerun of the older run deployed last"

    # older runs untouched since that deployment need no job lookup at all,
    # which is what keeps a full scan to about one request per page
    stale_run = {"id": 50, "head_sha": "d" * 40, "status": "completed",
                 "conclusion": "success", "updated_at": "2026-08-01T00:00:00Z"}
    asked: list = []
    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: {
        "total_count": 3, "workflow_runs": [newer_run, older_run_rerun, stale_run]})
    monkeypatch.setattr(guard, "deploy_job", lambda run_id, attempt=None: (
        asked.append(run_id) or jobs.get(run_id)))
    assert guard.live_deployment()["run"]["id"] == 100
    assert 50 not in asked, "pruned by updated_at"


def test_a_scan_that_never_ends_refuses_even_with_a_candidate(monkeypatch) -> None:
    guard = _load(monkeypatch)
    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: {
        "total_count": 10 ** 9,
        "workflow_runs": [{"id": 7, "head_sha": "e" * 40, "status": "completed",
                           "updated_at": "2026-09-09T00:00:00Z"}] * 100,
    })
    monkeypatch.setattr(guard, "deploy_job", lambda run_id, attempt=None: {
        "conclusion": "success", "completed_at": "2026-09-08T00:00:00Z"})

    with pytest.raises(SystemExit) as exit_info:
        guard.live_deployment()
    assert exit_info.value.code == 1


def test_a_truncated_run_list_is_a_refusal_not_an_empty_list(monkeypatch) -> None:
    """The runs API stops returning results past 1000 matches; an empty page
    while total_count is larger means the list was cut off."""
    guard = _load(monkeypatch)
    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: {
        "total_count": 5000, "workflow_runs": []})

    with pytest.raises(SystemExit) as exit_info:
        guard.live_deployment()
    assert exit_info.value.code == 1


def test_a_service_that_never_deployed_is_not_a_refusal(monkeypatch) -> None:
    guard = _load(monkeypatch)
    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: {
        "total_count": 0, "workflow_runs": []})

    assert guard.live_deployment() is None


def test_an_old_run_rerun_recently_is_still_the_live_release(monkeypatch) -> None:
    """GitHub allows re-running a run for 30 days after it was created, so a
    run created outside the first scanned window can still have deployed most
    recently; the scan reaches back that far before the candidate finished."""
    guard = _load(monkeypatch)
    recent = {"id": 300, "head_sha": "b" * 40, "status": "completed",
              "updated_at": "2026-09-01T00:00:00Z"}
    old_but_rerun = {"id": 100, "head_sha": "c" * 40, "status": "completed",
                     "updated_at": "2026-09-02T00:00:00Z"}
    jobs = {300: {"conclusion": "success", "completed_at": "2026-09-01T00:00:00Z"},
            100: {"conclusion": "success", "completed_at": "2026-09-02T00:00:00Z"}}
    windows: list = []

    def fake_api(path, *, paginate=False):
        since = path.split("created=%3E%3D", 1)[1].split("&", 1)[0]
        windows.append(since)
        # the old run was created long ago, so only the deep rescan sees it
        runs = [recent] if len(windows) == 1 else [recent, old_but_rerun]
        return {"total_count": len(runs), "workflow_runs": runs}

    monkeypatch.setattr(guard, "api", fake_api)
    monkeypatch.setattr(guard, "deploy_job", lambda run_id, attempt=None: jobs[run_id])

    live = guard.live_deployment()
    assert live["run"]["id"] == 100, "the re-run of the old run deployed last"
    assert len(windows) == 2, "one widening scan, then one deep rescan"
    assert windows[1] < windows[0], "the rescan reaches further back"


def test_a_run_still_in_progress_can_hold_an_earlier_deployment(monkeypatch) -> None:
    """Its newest attempt may be queued or running while an earlier attempt
    already deployed; deploy_job reads every attempt."""
    guard = _load(monkeypatch)
    in_progress = {"id": 800, "head_sha": "f" * 40, "status": "in_progress",
                   "conclusion": None, "updated_at": "2026-09-05T00:00:00Z"}
    monkeypatch.setattr(guard, "api", lambda path, *, paginate=False: {
        "total_count": 1, "workflow_runs": [in_progress]})
    monkeypatch.setattr(guard, "deploy_job", lambda run_id, attempt=None: {
        "conclusion": "success", "completed_at": "2026-09-04T00:00:00Z", "run_attempt": 1})

    live = guard.live_deployment()
    assert live is not None and live["run"]["id"] == 800

