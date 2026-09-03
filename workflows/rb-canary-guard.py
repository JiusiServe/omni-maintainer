#!/usr/bin/env python3
"""Deploy guard for JiusiServe/omni-reviewbot: one script, two jobs.

Copied into omni-reviewbot as ``deploy/canary_guard.py`` by the Phase 0
``deploy.yml`` pull request and called with the default ``GITHUB_TOKEN``:

  python3 deploy/canary_guard.py record   # canary-record job
  python3 deploy/canary_guard.py guard    # deploy-production's first step

Both modes re-read everything live and exit non-zero to block deployment.
Every fact comes from immutable Actions run and job metadata, commit trees,
the branch tip, or issue events; issue bodies are only ever written here,
never trusted as evidence.

Standard library only, and no ``gh`` CLI: the deploy job runs on a
self-hosted runner that is not guaranteed to have it, and a guard that
cannot run is a guard that blocks every deployment.

Environment (set by the workflow): GITHUB_REPOSITORY, GITHUB_SHA,
GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_ACTOR, GITHUB_EVENT_BEFORE
(``${{ github.event.before }}``), GITHUB_HEAD_MESSAGE
(``${{ github.event.head_commit.message }}``), GITHUB_SERVER_URL,
GITHUB_API_URL, GITHUB_WORKFLOW_REF, GH_TOKEN (or GITHUB_TOKEN), HUMANS
(space-separated allowlisted logins), LEDGER_REPO and LEDGER_ISSUE (the
pinned ledger issue, whose ``maintainer:paused`` label is the live kill
switch), and optionally DEPLOY_COOLDOWN_HOURS and DASHBOARDS.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("GITHUB_REPOSITORY", "")
SHA = os.environ.get("GITHUB_SHA", "")
RUN_ID = int(os.environ.get("GITHUB_RUN_ID") or 0)
RUN_ATTEMPT = int(os.environ.get("GITHUB_RUN_ATTEMPT") or 1)
HUMANS = set((os.environ.get("HUMANS") or "").split())
API = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
WORKFLOW_FILE = os.path.basename((os.environ.get("GITHUB_WORKFLOW_REF") or "deploy.yml").split("@")[0])
COOLDOWN_HOURS = float(os.environ.get("DEPLOY_COOLDOWN_HOURS") or 4)
DASHBOARDS = {
    name: url for name, url in (
        pair.split("=", 1) for pair in (os.environ.get("DASHBOARDS") or (
            "vllm_omni=http://review.43.155.186.30.nip.io/code_review/vllm_omni,"
            "vllm_gr=http://review.43.155.186.30.nip.io/code_review/vllm_gr")).split(",") if pair
    )
}
CANARY_LABEL = "maintainer:canary"
HOLD_LABELS = ("maintainer:canary", "maintainer:incident", "maintainer:rollback")
CANARY_MARKER = "omni-maintainer:canary:v1"
WORKFLOW_LOGIN = "github-actions[bot]"
MAX_COMPARE_FILES = 300


class ApiError(RuntimeError):
    pass


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def _request(method: str, url: str, body: dict | None = None) -> tuple[bytes, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "omni-maintainer-canary-guard")
    if TOKEN:
        request.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed API host
            return response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        raise ApiError(f"{method} {url} -> HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ApiError(f"{method} {url} -> {exc}") from exc


def api(path: str, *, paginate: bool = False):
    """GET one API path; with ``paginate``, follow ``Link: rel="next"``."""
    url = path if path.startswith("http") else f"{API}/{path}"
    body, headers = _request("GET", url)
    first = json.loads(body)
    if not paginate:
        return first
    items = list(first) if isinstance(first, list) else [first]
    while True:
        link = headers.get("Link") or ""
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not match:
            return items
        body, headers = _request("GET", match.group(1))
        page = json.loads(body)
        items.extend(page if isinstance(page, list) else [page])


def post(path: str, body: dict):
    return json.loads(_request("POST", f"{API}/{path}", body)[0])


def parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def issues(label: str, state: str = "open"):
    since = ""
    if state == "closed":
        since = "&since=" + (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    return [i for i in api(f"repos/{REPO}/issues?labels={label}&state={state}{since}&per_page=100", paginate=True)
            if isinstance(i, dict) and "pull_request" not in i]


def canary_record(issue: dict) -> dict | None:
    """The record embedded in a workflow-authored canary issue, or None."""
    if (issue.get("user") or {}).get("login") != WORKFLOW_LOGIN:
        return None
    match = re.search(r"<!--\s*" + CANARY_MARKER + r"\s+(\{.*?\})\s*-->", issue.get("body") or "", re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def record_for_this_attempt():
    """The workflow-authored canary record for (run_id, run_attempt), if any.

    Issue bodies are editable, so authorship alone is not enough: an old
    workflow-created issue could be edited to claim this attempt. The record
    must also have been CREATED (immutable timestamp) at or after this run,
    which only a record written by this run can satisfy.
    """
    run_created = parse_time(api(f"repos/{REPO}/actions/runs/{RUN_ID}").get("created_at"))
    for issue in api(f"repos/{REPO}/issues?state=all&labels={CANARY_LABEL}&per_page=100", paginate=True):
        if not isinstance(issue, dict) or "pull_request" in issue:
            continue
        created = parse_time(issue.get("created_at"))
        if run_created and created and created < run_created:
            continue
        record = canary_record(issue)
        if record and int(record.get("deploy_run_id") or 0) == RUN_ID \
                and int(record.get("run_attempt") or 1) == RUN_ATTEMPT:
            return issue, record
    return None, None


# ---------------------------------------------------------------- checks

def check_tip() -> None:
    tip = api(f"repos/{REPO}/branches/main")["commit"]["sha"]
    if tip != SHA:
        fail(f"main is at {tip}, this run is for {SHA}: stale run, refusing")
    print(f"main is still at {SHA}")


def check_not_paused() -> None:
    """The live kill switch: ``maintainer:paused`` on the ledger issue.

    ``vars.PRODUCTION_DEPLOY_ENABLED`` is read by GitHub when the run is
    QUEUED, so it cannot stop a run already under way, and no workflow
    permission lets a token re-read it. The ledger issue can be read live
    with ``issues: read``, and its label is already the documented pause.
    Provenance is asymmetric: a human ``labeled`` event pauses until a human
    ``unlabeled`` event, and events by anyone else count in neither
    direction, so no automation can pause the system or lift a human's
    pause. A ledger that cannot be read is itself a refusal.
    """
    number = os.environ.get("LEDGER_ISSUE") or ""
    ledger_repo = os.environ.get("LEDGER_REPO") or REPO
    if not number.isdigit():
        fail("LEDGER_ISSUE is not configured; refusing to deploy without a readable kill switch")
    try:
        events = api(f"repos/{ledger_repo}/issues/{int(number)}/events?per_page=100", paginate=True)
    except ApiError as exc:
        fail(f"ledger {ledger_repo}#{number} is unreadable ({exc}); refusing to deploy")
    paused, who = False, ""
    for event in events:
        if (event.get("label") or {}).get("name") != "maintainer:paused":
            continue
        actor = (event.get("actor") or {}).get("login") or ""
        if actor not in HUMANS:
            continue
        if event.get("event") == "labeled":
            paused, who = True, actor
        elif event.get("event") == "unlabeled":
            paused, who = False, ""
    if paused:
        fail(f"the maintainer is paused: {who} labelled {ledger_repo}#{number} maintainer:paused; refusing to deploy")
    print(f"ledger {ledger_repo}#{number} is not paused")


def check_pusher() -> None:
    """Block a non-human push synchronously, before anything deploys.

    ``GITHUB_ACTOR`` is the server-stamped identity of the push. It must be
    an allowlisted human, and a merge commit must additionally be GitHub's
    own record of a pull request merged into ``main`` by an allowlisted
    human. The commit message is never consulted.
    """
    if not HUMANS:
        fail("HUMANS is empty; refusing to deploy without an allowlist to check the pusher against")
    actor = os.environ.get("GITHUB_ACTOR") or ""
    if actor not in HUMANS:
        fail(f"push actor {actor!r} is not an allowlisted human; refusing to deploy")
    parents = api(f"repos/{REPO}/commits/{SHA}").get("parents", [])
    if len(parents) >= 2:
        confirmed = [p for p in api(f"repos/{REPO}/commits/{SHA}/pulls")
                     if p.get("merged_at") and p.get("merge_commit_sha") == SHA
                     and (p.get("base") or {}).get("ref") == "main"]
        if not confirmed:
            fail("merge commit that GitHub does not associate with a pull request merged into main; refusing")
        pull = api(f"repos/{REPO}/pulls/{int(confirmed[0]['number'])}")
        merged_by = (pull.get("merged_by") or {}).get("login") or ""
        if merged_by not in HUMANS:
            fail(f"pull request #{pull.get('number')} was merged by {merged_by!r}, not an allowlisted human; refusing")
        print(f"pull request #{pull.get('number')} merged into main by allowlisted human {merged_by}")
    print(f"push by allowlisted human {actor}")


def deploy_job(run_id: int) -> dict | None:
    """The ``deploy-production`` job of a run, or None when it never ran.

    A run can complete successfully with that job skipped (deployment
    disabled), which is not a deployment.
    """
    for job in api(f"repos/{REPO}/actions/runs/{int(run_id)}/jobs?per_page=100").get("jobs", []):
        if job.get("name") == "deploy-production":
            return job
    return None


def live_deployment() -> dict | None:
    """The newest push run of this workflow on main whose deploy job succeeded."""
    runs = api(f"repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs"
               f"?branch=main&event=push&per_page=30").get("workflow_runs", [])
    for run in runs:
        if run.get("head_sha") == SHA or run.get("status") != "completed":
            continue
        job = deploy_job(int(run["id"]))
        if job and job.get("conclusion") == "success":
            return {"run": run, "job": job}
    return None


def check_cooldown(live: dict | None) -> None:
    """An immutable hold no issue state can lift: no deployment within
    ``COOLDOWN_HOURS`` of the previous successful one. This bounds what a
    routine that closes or unlabels canary and incident issues could do."""
    if live is None:
        print("no previous successful deployment; cooldown not applicable")
        return
    done = parse_time(live["job"].get("completed_at"))
    if done and datetime.now(timezone.utc) - done < timedelta(hours=COOLDOWN_HOURS):
        fail(f"previous deployment {live['run']['head_sha'][:8]} completed at {done.isoformat()}; the "
             f"{COOLDOWN_HOURS} h cooldown has not elapsed (no issue state can shorten it)")
    print("cooldown elapsed since the previous successful deployment")


def rollback_exception(live: dict | None) -> bool:
    """Is this push a verified rollback of the currently live deployment?

    (a) GitHub associates the commit with a merged ``maintainer:rollback``
    pull request into main, and (b) the pushed tree equals the tree that the
    live deployment replaced, that is the tree of its head commit's first
    parent. Run and job metadata and commit trees only; no issue is read.
    """
    associated = api(f"repos/{REPO}/commits/{SHA}/pulls")
    rollback_prs = [p for p in associated
                    if p.get("merged_at") and p.get("merge_commit_sha") == SHA
                    and (p.get("base") or {}).get("ref") == "main"
                    and any(l.get("name") == "maintainer:rollback" for l in p.get("labels") or [])]
    if not rollback_prs:
        return False
    if live is None:
        print("no previous successful deployment; the rollback exception does not apply")
        return False
    pushed_tree = api(f"repos/{REPO}/commits/{SHA}")["commit"]["tree"]["sha"]
    deployed = live["run"]["head_sha"]
    parents = [p["sha"] for p in api(f"repos/{REPO}/commits/{deployed}").get("parents", [])]
    if not parents:
        return False
    pre_tree = api(f"repos/{REPO}/commits/{parents[0]}")["commit"]["tree"]["sha"]
    if pre_tree == pushed_tree:
        print(f"rollback exception: PR #{rollback_prs[0]['number']} restores the tree that the live deployment "
              f"{deployed[:8]} (run {live['run']['id']}) replaced")
        return True
    print(f"rollback PR does not restore what the live deployment {deployed[:8]} replaced")
    return False


def check_holds(exempt_issue_number: int | None) -> None:
    """Open canaries, incidents and rollbacks hold; so does any incident
    closed by a non-human within 30 days. Labels never lift a hold: canary
    records are recognised by authorship and marker, so removing a label
    from one changes nothing. In guard mode exactly one issue is exempt,
    identified by number, and only in its role as this attempt's canary: if
    that same issue also carries an incident or rollback label, it holds.
    """
    blocking, seen = [], set()
    for issue in api(f"repos/{REPO}/issues?state=open&per_page=100", paginate=True):
        if not isinstance(issue, dict) or "pull_request" in issue or canary_record(issue) is None:
            continue
        number = int(issue["number"])
        if exempt_issue_number is not None and number == exempt_issue_number:
            continue  # this attempt's own record; every earlier attempt still holds
        seen.add(number)
        blocking.append(f"#{number} (open canary record) {issue.get('title')}")
    for label in HOLD_LABELS:
        for issue in issues(label):
            number = int(issue["number"])
            if number in seen:
                continue
            if label == CANARY_LABEL and number == exempt_issue_number:
                continue  # exempt as this attempt's canary ONLY; an incident or
                # rollback label on the same issue still holds
            seen.add(number)
            blocking.append(f"#{number} ({label}) {issue.get('title')}")
    for issue in issues("maintainer:incident", "closed"):
        closer = ""
        for event in api(f"repos/{REPO}/issues/{issue['number']}/events?per_page=100", paginate=True):
            if event.get("event") == "closed":
                closer = (event.get("actor") or {}).get("login") or ""
        if closer not in HUMANS:
            blocking.append(f"#{issue['number']} (incident closed by {closer or 'unknown'!r}, not a human)")
    if blocking:
        fail("deployment blocked; resolve or wait for these, then re-run ALL jobs:\n" + "\n".join(blocking))
    print("no open canary, incident or rollback; no non-human close; proceeding")


def read_dashboards() -> dict:
    readings = {}
    for name, base in DASHBOARDS.items():
        last = None
        for _ in range(3):
            try:
                request = urllib.request.Request(base.rstrip("/") + "/api/status",
                                                 headers={"User-Agent": "omni-maintainer-canary-guard"})
                with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310 - fixed http URL
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict) or "jobs" not in payload or "history" not in payload:
                    raise ValueError("malformed status payload")
                readings[name] = payload
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(30)
        if name not in readings:
            fail(f"dashboard {name} unreadable ({last}); refusing to deploy without a pre-deploy reading")
    return readings


def counters(readings: dict) -> tuple[dict, dict]:
    failed = {n: int((r.get("counts") or {}).get("failed") or 0) for n, r in readings.items()}
    ids = {n: max((int(j.get("id") or 0) for j in r.get("jobs") or []), default=0) for n, r in readings.items()}
    return failed, ids


def preflight(exempt_issue_number: int | None) -> None:
    """Every check both modes share, in refusal order."""
    check_tip()
    check_not_paused()
    check_pusher()
    live = live_deployment()
    if not rollback_exception(live):
        check_cooldown(live)
        check_holds(exempt_issue_number)
    else:
        print("verified rollback: cooldown and holds do not apply")


# ---------------------------------------------------------------- modes

def mode_record() -> None:
    preflight(exempt_issue_number=None)
    existing, _ = record_for_this_attempt()
    if existing:
        print(f"canary already recorded for this attempt: #{existing['number']}")
        return
    readings = read_dashboards()
    primary = readings.get("vllm_omni") or next(iter(readings.values()))
    days = (primary.get("history") or {}).get("days") or []
    done = days[:-1] if len(days) > 1 else days
    attention = [int(d.get("attention") or 0) for d in done]
    gate = [int(d.get("gate_failures") or 0) for d in done]
    failed, ids = counters(readings)
    now = datetime.now(timezone.utc).isoformat()
    baseline = {
        "mean_attention": (sum(attention) / len(attention)) if attention else 0.0,
        "max_attention": max(attention) if attention else 0,
        "mean_gate_failures": (sum(gate) / len(gate)) if gate else 0.0,
        "counts_failed": sum(failed.values()),
        "review_failed": sum(int(((r.get("review_stats") or {}).get("outcomes") or {}).get("failed") or 0)
                             for r in readings.values()),
        "gate_failures": sum(int((r.get("review_stats") or {}).get("gate_failures") or 0) for r in readings.values()),
        "split_failed": sum(int(((r.get("split") or {}).get("attempts") or {}).get("failed") or 0)
                            for r in readings.values()),
        "last_scan_at": (primary.get("metadata") or {}).get("last_scan_at"),
        "started_at": "", "captured_at": now, "attribute_from": "", "max_job_ids": ids,
    }
    message = os.environ.get("GITHUB_HEAD_MESSAGE") or ""
    pr = re.match(r"^Merge pull request #(\d+)", message)
    record = {"repo": REPO, "merge_sha": SHA, "pre_merge_sha": os.environ.get("GITHUB_EVENT_BEFORE") or "",
              "pr_number": int(pr.group(1)) if pr else None, "deploy_run_id": RUN_ID, "run_attempt": RUN_ATTEMPT,
              "opened_at": now, "kind": "rb", "status": "pending", "baseline": baseline,
              "pusher": os.environ.get("GITHUB_ACTOR") or ""}
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    body = "\n".join([
        f"Deploy of `{SHA}` (previous `{record['pre_merge_sha']}`), run "
        f"{server}/{REPO}/actions/runs/{RUN_ID} attempt {RUN_ATTEMPT}",
        f"PR: #{record['pr_number']}" if record["pr_number"] else "PR: (direct push)",
        "", "The monitor appends one tick comment per hour and closes this issue when the window passes.",
        "", f"<!-- {CANARY_MARKER} {json.dumps(record, sort_keys=True)} -->",
    ])
    created = post(f"repos/{REPO}/issues",
                   {"title": f"[canary] deploy {SHA[:8]}", "body": body, "labels": [CANARY_LABEL]})
    print("canary recorded:", created.get("html_url"))


def mode_guard() -> None:
    """deploy-production's first step: everything live, seconds before deploying."""
    issue, _ = record_for_this_attempt()
    if issue is None:
        fail(f"no canary record for run {RUN_ID} attempt {RUN_ATTEMPT}; refusing to deploy "
             "(after a successful canary-record, use 're-run all jobs', not 're-run failed jobs')")
    preflight(exempt_issue_number=int(issue["number"]))
    readings = read_dashboards()
    failed, ids = counters(readings)
    pre = {"captured_at": datetime.now(timezone.utc).isoformat(), "counts_failed": failed, "max_job_ids": ids}
    post(f"repos/{REPO}/issues/{int(issue['number'])}/comments",
         {"body": "pre-deploy counters captured by the deploy job\n\n"
                  f"<!-- omni-maintainer:predeploy:v1 {json.dumps(pre, sort_keys=True)} -->"})
    print(f"pre-deploy counters posted on canary #{issue['number']}; deploying")


def main(argv: list[str]) -> None:
    mode = argv[1] if len(argv) > 1 else ""
    if not REPO or not SHA or not RUN_ID:
        fail("GITHUB_REPOSITORY, GITHUB_SHA and GITHUB_RUN_ID must be set")
    try:
        if mode == "record":
            mode_record()
        elif mode == "guard":
            mode_guard()
        else:
            fail("usage: canary_guard.py record|guard")
    except ApiError as exc:
        fail(f"GitHub API read failed, refusing to deploy: {exc}")


if __name__ == "__main__":
    main(sys.argv)
