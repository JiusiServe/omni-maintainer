#!/usr/bin/env python3
"""Deploy guard for JiusiServe/omni-reviewbot: one script, two jobs.

Copied into omni-reviewbot as ``deploy/canary_guard.py`` by the Phase 0
``deploy.yml`` pull request and called with the default ``GITHUB_TOKEN``:

  python3 deploy/canary_guard.py record   # canary-record job
  python3 deploy/canary_guard.py guard    # deploy-production's first step

Both modes re-read everything live from GitHub and the dashboards and exit
non-zero to block deployment. Every fact comes from immutable Actions run
and job metadata, commit trees, or the branch tip; issue bodies are only
ever written here, never trusted as evidence. Standard library only.

Environment (set by the workflow): GITHUB_REPOSITORY, GITHUB_SHA,
GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_ACTOR, GITHUB_EVENT_BEFORE
(``${{ github.event.before }}``), GITHUB_HEAD_MESSAGE
(``${{ github.event.head_commit.message }}``), GITHUB_SERVER_URL,
GH_TOKEN, and HUMANS (space-separated allowlisted logins).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = os.environ["GITHUB_REPOSITORY"]
SHA = os.environ["GITHUB_SHA"]
RUN_ID = int(os.environ["GITHUB_RUN_ID"])
RUN_ATTEMPT = int(os.environ.get("GITHUB_RUN_ATTEMPT") or 1)
HUMANS = set((os.environ.get("HUMANS") or "tzhouam").split())
WORKFLOW_FILE = os.path.basename((os.environ.get("GITHUB_WORKFLOW_REF") or "deploy.yml").split("@")[0])
DASHBOARDS = {
    "vllm_omni": "http://review.43.155.186.30.nip.io/code_review/vllm_omni",
    "vllm_gr": "http://review.43.155.186.30.nip.io/code_review/vllm_gr",
}
CANARY_LABEL = "maintainer:canary"
HOLD_LABELS = ("maintainer:canary", "maintainer:incident", "maintainer:rollback")
WORKFLOW_LOGIN = "github-actions[bot]"


def gh(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["gh", *args], text=True, capture_output=True, check=True, input=stdin).stdout


def api(path: str, paginate: bool = False):
    if paginate:
        pages = json.loads(gh("api", "--paginate", "--slurp", path))
        out = []
        for page in pages:
            out.extend(page if isinstance(page, list) else [page])
        return out
    return json.loads(gh("api", path))


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def issues(label: str, state: str = "open"):
    since = ""
    if state == "closed":
        since = "&since=" + (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    return [i for i in api(f"repos/{REPO}/issues?labels={label}&state={state}{since}&per_page=100", paginate=True)
            if "pull_request" not in i]


def record_for_this_attempt():
    """The workflow-authored canary record for (run_id, run_attempt), if any.

    Issue bodies are editable, so authorship alone is not enough: an old
    workflow-created issue could be edited to claim this attempt. The
    record must also have been CREATED (immutable timestamp) after this run
    was created, which only a record written by this run can satisfy.
    """
    run_created = datetime.fromisoformat(api(f"repos/{REPO}/actions/runs/{RUN_ID}")["created_at"].replace("Z", "+00:00"))
    for issue in issues(CANARY_LABEL, "all"):
        if (issue.get("user") or {}).get("login") != WORKFLOW_LOGIN:
            continue
        created = datetime.fromisoformat(str(issue.get("created_at") or "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
        if created < run_created:
            continue
        m = re.search(r"<!--\s*omni-maintainer:canary:v1\s+(\{.*?\})\s*-->", issue.get("body") or "", re.S)
        if not m:
            continue
        rec = json.loads(m.group(1))
        if int(rec.get("deploy_run_id") or 0) == RUN_ID and int(rec.get("run_attempt") or 1) == RUN_ATTEMPT:
            return issue, rec
    return None, None


# ---------------------------------------------------------------- checks

def check_tip() -> None:
    tip = api(f"repos/{REPO}/branches/main")["commit"]["sha"]
    if tip != SHA:
        fail(f"main is at {tip}, this run is for {SHA}: stale run, refusing")
    print(f"main is still at {SHA}")


def check_pusher() -> None:
    """Block a non-human push synchronously, before anything deploys.

    ``GITHUB_ACTOR`` is the server-stamped identity of the push. It must be
    an allowlisted human, and a merge commit must additionally be the
    GitHub-confirmed merge of a pull request into ``main`` by that human.
    Hourly detection would be too late: the commit would already be live.
    """
    actor = os.environ.get("GITHUB_ACTOR") or ""
    if actor not in HUMANS:
        fail(f"push actor {actor!r} is not an allowlisted human; refusing to deploy")
    parents = api(f"repos/{REPO}/commits/{SHA}").get("parents", [])
    if len(parents) >= 2:
        # A merge commit must be GitHub's own record of a pull request merged
        # into main by an allowlisted human. The association endpoint is
        # server-side; the commit message is not consulted at all.
        associated = api(f"repos/{REPO}/commits/{SHA}/pulls")
        confirmed = [p for p in associated
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


def check_cooldown(min_hours: float) -> None:
    """An immutable hold that no issue state can lift: no deployment within
    ``min_hours`` of the previous successful deploy-production job (Actions
    job metadata), except a verified rollback. This bounds what a routine
    that closes or unlabels canary and incident issues could achieve."""
    runs = api(f"repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs?branch=main&event=push&per_page=30").get("workflow_runs", [])
    for run in runs:
        if run.get("head_sha") == SHA or run.get("status") != "completed":
            continue
        jobs = api(f"repos/{REPO}/actions/runs/{run['id']}/jobs?per_page=100").get("jobs", [])
        job = next((j for j in jobs if j.get("name") == "deploy-production" and j.get("conclusion") == "success"), None)
        if job is None:
            continue
        completed = job.get("completed_at") or ""
        done = datetime.fromisoformat(completed.replace("Z", "+00:00")) if completed else None
        if done and datetime.now(timezone.utc) - done < timedelta(hours=min_hours):
            fail(f"previous deployment {run['head_sha'][:8]} completed at {completed}; the {min_hours} h "
                 "cooldown has not elapsed (no issue state can shorten it)")
        break
    print("cooldown elapsed since the previous successful deployment")


def check_deploy_flag() -> None:
    try:
        value = gh("variable", "get", "PRODUCTION_DEPLOY_ENABLED", "-R", REPO).strip()
    except subprocess.CalledProcessError:
        value = ""
    if value != "true":
        fail(f"PRODUCTION_DEPLOY_ENABLED is now {value!r}; refusing to deploy")


def deploy_job_succeeded(run: dict) -> bool:
    jobs = api(f"repos/{REPO}/actions/runs/{run['id']}/jobs?per_page=100").get("jobs", [])
    return any(j.get("name") == "deploy-production" and j.get("conclusion") == "success" for j in jobs)


def rollback_exception() -> bool:
    """Is this push the merge of a maintainer:rollback PR whose tree equals the
    tree that the currently live deployment replaced? Facts: GitHub's PR
    association, run and job metadata, commit trees. No issue is consulted."""
    associated = api(f"repos/{REPO}/commits/{SHA}/pulls")
    rollback_prs = [p for p in associated
                    if p.get("merged_at") and p.get("merge_commit_sha") == SHA
                    and (p.get("base") or {}).get("ref") == "main"
                    and any(l.get("name") == "maintainer:rollback" for l in p.get("labels") or [])]
    if not rollback_prs:
        return False
    pushed_tree = api(f"repos/{REPO}/commits/{SHA}")["commit"]["tree"]["sha"]
    runs = api(f"repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs?branch=main&event=push&per_page=30").get("workflow_runs", [])
    live = next((r for r in runs if r.get("head_sha") != SHA and r.get("status") == "completed"
                 and deploy_job_succeeded(r)), None)
    if live is None:
        print("no previous successful deployment found; rollback exception not applicable")
        return False
    parents = [p["sha"] for p in api(f"repos/{REPO}/commits/{live['head_sha']}").get("parents", [])]
    if not parents:
        return False
    pre_tree = api(f"repos/{REPO}/commits/{parents[0]}")["commit"]["tree"]["sha"]
    if pre_tree == pushed_tree:
        print(f"rollback exception: PR #{rollback_prs[0]['number']} restores the tree that the live deployment "
              f"{live['head_sha'][:8]} (run {live['id']}) replaced")
        return True
    print(f"rollback PR does not restore what the live deployment {live['head_sha'][:8]} replaced")
    return False


def check_holds(exempt_issue_number: int | None) -> None:
    """Open canaries, incidents and rollbacks hold; so does any incident closed
    by a non-human within 30 days (labels never lift a hold). In guard mode
    exactly one issue is exempt: the validated, workflow-authored record of
    this attempt, identified by its issue number (never by a body
    substring, which any labeled issue could forge). The rollback exception
    admits a verified restoration."""
    if rollback_exception():
        return
    blocking = []
    seen = set()
    # Canary records are recognised by authorship and marker, never by label:
    # removing a label from a workflow-authored record does not lift its hold.
    for issue in api(f"repos/{REPO}/issues?creator={WORKFLOW_LOGIN}&state=open&per_page=100", paginate=True):
        if "pull_request" in issue or "omni-maintainer:canary:v1" not in (issue.get("body") or ""):
            continue
        if exempt_issue_number is not None and int(issue["number"]) == exempt_issue_number:
            continue  # this attempt's own record; every earlier attempt still holds
        seen.add(int(issue["number"]))
        blocking.append(f"#{issue['number']} (open canary record) {issue.get('title')}")
    for label in HOLD_LABELS:
        for issue in issues(label):
            if int(issue["number"]) in seen:
                continue
            if exempt_issue_number is not None and label == CANARY_LABEL and int(issue["number"]) == exempt_issue_number:
                continue
            seen.add(int(issue["number"]))
            blocking.append(f"#{issue['number']} ({label}) {issue.get('title')}")
    for issue in issues("maintainer:incident", "closed"):
        closer = ""
        for ev in api(f"repos/{REPO}/issues/{issue['number']}/events?per_page=100", paginate=True):
            if ev.get("event") == "closed":
                closer = (ev.get("actor") or {}).get("login") or ""
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
                with urllib.request.urlopen(base + "/api/status", timeout=90) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
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


# ---------------------------------------------------------------- modes

COOLDOWN_HOURS = float(os.environ.get("DEPLOY_COOLDOWN_HOURS") or 4)


def mode_record() -> None:
    check_tip()
    check_pusher()
    if not rollback_exception():
        check_cooldown(COOLDOWN_HOURS)
    check_holds(exempt_issue_number=None)
    existing, _ = record_for_this_attempt()
    if existing:
        print(f"canary already recorded for this attempt: #{existing['number']}")
        return
    readings = read_dashboards()
    primary = readings["vllm_omni"]
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
        "review_failed": sum(int(((r.get("review_stats") or {}).get("outcomes") or {}).get("failed") or 0) for r in readings.values()),
        "gate_failures": sum(int((r.get("review_stats") or {}).get("gate_failures") or 0) for r in readings.values()),
        "split_failed": sum(int(((r.get("split") or {}).get("attempts") or {}).get("failed") or 0) for r in readings.values()),
        "last_scan_at": (primary.get("metadata") or {}).get("last_scan_at"),
        "started_at": "", "captured_at": now, "attribute_from": "", "max_job_ids": ids,
    }
    message = os.environ.get("GITHUB_HEAD_MESSAGE") or ""
    pr = re.match(r"^Merge pull request #(\d+)", message)
    record = {"repo": REPO, "merge_sha": SHA, "pre_merge_sha": os.environ.get("GITHUB_EVENT_BEFORE") or "",
              "pr_number": int(pr.group(1)) if pr else None, "deploy_run_id": RUN_ID, "run_attempt": RUN_ATTEMPT,
              "opened_at": now, "kind": "rb", "status": "pending", "baseline": baseline,
              "pusher": os.environ.get("GITHUB_ACTOR") or ""}
    body = "\n".join([
        f"Deploy of `{SHA}` (previous `{record['pre_merge_sha']}`), run "
        f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{REPO}/actions/runs/{RUN_ID} attempt {RUN_ATTEMPT}",
        f"PR: #{record['pr_number']}" if record["pr_number"] else "PR: (direct push)",
        "", "The monitor appends one tick comment per hour and closes this issue when the window passes.",
        "", f"<!-- omni-maintainer:canary:v1 {json.dumps(record, sort_keys=True)} -->",
    ])
    out = gh("api", f"repos/{REPO}/issues", "-X", "POST", "--input", "-",
             stdin=json.dumps({"title": f"[canary] deploy {SHA[:8]}", "body": body, "labels": [CANARY_LABEL]}))
    print("canary recorded:", json.loads(out)["html_url"])


def mode_guard() -> None:
    """deploy-production's first step: everything live, seconds before deploying."""
    check_tip()
    check_pusher()
    check_deploy_flag()
    if not rollback_exception():
        check_cooldown(COOLDOWN_HOURS)
    issue, _ = record_for_this_attempt()
    if issue is None:
        fail(f"no canary record for run {RUN_ID} attempt {RUN_ATTEMPT}; refusing to deploy "
             "(after a successful canary-record, use 're-run all jobs', not 're-run failed jobs')")
    check_holds(exempt_issue_number=int(issue["number"]))
    readings = read_dashboards()
    failed, ids = counters(readings)
    pre = {"captured_at": datetime.now(timezone.utc).isoformat(), "counts_failed": failed, "max_job_ids": ids}
    body = "pre-deploy counters captured by the deploy job\n\n<!-- omni-maintainer:predeploy:v1 " + \
        json.dumps(pre, sort_keys=True) + " -->"
    gh("api", f"repos/{REPO}/issues/{issue['number']}/comments", "-X", "POST", "--input", "-",
       stdin=json.dumps({"body": body}))
    print(f"pre-deploy counters posted on canary #{issue['number']}; deploying")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "record":
        mode_record()
    elif mode == "guard":
        mode_guard()
    else:
        fail("usage: canary_guard.py record|guard")
