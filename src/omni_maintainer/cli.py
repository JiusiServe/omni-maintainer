"""``python -m omni_maintainer <command>``: the commands the workflows and routines call.

Mechanical decisions live in the modules; this file wires GitHub reads and
writes around them and prints JSON so a routine's model can act on the
result without re-deriving it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import PolicyError, load_policy, repo_config
from .gate import merge as gate_merge
from .gate.actors import human_pause_active
from .gate.bar import BarInputs, evaluate
from .gate.reads import IncompleteRead, PullSnapshot, load_snapshot
from .gate.verdict import APPROVE, REVISE, format_marker, needs_verdict
from .monitor import canary as canary_mod
from .monitor import rollback as rollback_mod
from .gate.reads import parse_time
from .monitor.dashboard import (Digest, digest, fetch_json, job_time, job_url, new_failed_jobs, status_url,
                                watcher_dead, window_saturated)
from .monitor.fingerprint import classify, excerpt, fingerprint
from .monitor.issues import (UnsafeText, add_labels, close_issue, comment_issue, create_issue,
                             ensure_safe, find_issue_by_marker, fingerprint_marker, remove_labels,
                             render_failure_body)
from .monitor.pushes import INCIDENT_KINDS, classify_commit, new_commits_since
from .routine.ghcli import Gh, GhError, git
from .routine.ledger import parse_cursors, render_ledger, replace_cursors
from .routine.preflight import Preflight, PreflightError, run_preflight
from .routine.release import (ReleaseError, handshake_check, pin_bump_preconditions, prepare_revert,
                              render_pin_bump_body)
from .routine.workqueue import build_queue, stale_decisions

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_PAUSED = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _emit(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def _runner(gh: Gh):
    return lambda path, paginate: gh.api(path, paginate=paginate)


# ---------------------------------------------------------------- preflight

def cmd_preflight(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    try:
        pre = run_preflight(gh, policy, now=_now())
    except PreflightError as exc:
        _emit({"ok": False, "error": str(exc)})
        return EXIT_FAIL
    _emit({"ok": True, **pre.to_json()})
    return EXIT_PAUSED if pre.paused else EXIT_OK


# ---------------------------------------------------------------- gate

def _load_pull(gh: Gh, policy: dict[str, Any], repo: str, number: int) -> PullSnapshot:
    return load_snapshot(_runner(gh), repo, number, max_files=int(policy["bar"]["max_files"]),
                         mergeable_retry=lambda: time.sleep(20))


def _ensure_objects(repo: str, root: str) -> str:
    """A blob-less, checkout-less clone of ``repo`` under ``root`` (idempotent).

    Only git objects are fetched; no working tree is created and nothing
    from the repository is executed. Used to verify revert PRs.
    """
    target = Path(root) / repo.split("/", 1)[1]
    if not (target / ".git").is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        git(["-c", "credential.helper=!gh auth git-credential", "clone", "--quiet", "--filter=blob:none",
             "--no-checkout", f"https://github.com/{repo}.git", str(target)])
    return str(target)


def _revert_facts(gh: Gh, policy: dict[str, Any], snapshot: PullSnapshot, workdir: str | None) -> tuple[bool | None, bool | None]:
    """For a revert PR: (diff empty against pre-merge, main still at the merge)."""
    labels = policy["labels"]
    if labels["rollback"] not in snapshot.labels:
        return None, None
    if workdir:
        # A repository checkout inside ``workdir`` or the directory itself.
        candidate = Path(workdir)
        if not (candidate / ".git").is_dir():
            workdir = _ensure_objects(snapshot.repo, workdir)
    record = None
    canaries = gh.api(f"repos/{snapshot.repo}/issues?labels={labels['canary']}&state=all&per_page=100", paginate=True)
    for issue in canaries or []:
        rec = canary_mod.CanaryRecord.parse(str(issue.get("body") or ""))
        if rec and rec.merge_sha and rec.merge_sha[:8] in (snapshot.title + " " + snapshot.head_ref):
            record = rec
            break
    if record is None or not workdir:
        return False, False
    git(["fetch", "origin", "main", snapshot.head_sha, record.pre_merge_sha], cwd=workdir)
    empty = git(["diff", "--stat", record.pre_merge_sha, snapshot.head_sha], cwd=workdir).strip() == ""
    tip = git(["rev-parse", "origin/main"], cwd=workdir).strip()
    return empty, tip == record.merge_sha


def _bar_inputs(gh: Gh, policy: dict[str, Any], snapshot: PullSnapshot, workdir: str | None) -> tuple[BarInputs, Preflight]:
    pre = run_preflight(gh, policy, now=_now())
    empty, tip_ok = _revert_facts(gh, policy, snapshot, workdir)
    return BarInputs(now=pre.now, merges_today=pre.merges_today, paused=pre.paused,
                     deploy_hold=pre.deploy_hold, open_canary=pre.open_canary,
                     revert_verified_empty=empty, base_tip_is_reverted_merge=tip_ok), pre


def cmd_gate_evaluate(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    repo = repo_config(policy, args.repo)
    try:
        snapshot = _load_pull(gh, policy, args.repo, args.pr)
        inputs, _ = _bar_inputs(gh, policy, snapshot, args.workdir)
    except (IncompleteRead, PreflightError, GhError) as exc:
        # Fail closed: publish a failing check that names the read problem.
        from .gate.bar import BarResult
        result = BarResult(ok=False, failures=[f"gate could not read GitHub completely: {exc}"])
        if args.publish and getattr(args, "head", ""):
            gate_merge.publish_check(gh, repo=args.repo, head_sha=args.head, result=result)
        _emit({"ok": False, "summary": result.summary()})
        return EXIT_FAIL
    # The published check states the PR's own properties; caps and holds
    # apply to autonomous merges and are reported as notes here.
    result = evaluate(snapshot, policy=policy, repo=repo, inputs=inputs, enforce_caps=args.enforce_caps)
    if args.publish:
        gate_merge.publish_check(gh, repo=args.repo, head_sha=snapshot.head_sha, result=result,
                                 details_url=args.details_url or "")
    _emit({"ok": result.ok, "head": snapshot.head_sha, "failures": result.failures, "notes": result.notes,
           "carve_outs": result.carve_out_paths, "requires_go": result.requires_go,
           "revert_fast_path": result.is_revert_fast_path, "summary": result.summary()})
    return EXIT_OK if result.ok else EXIT_FAIL


def cmd_gate_review_queue(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    reviewer = policy["identities"]["reviewer_login"]
    pulls = gh.api(f"repos/{args.repo}/pulls?state=open&per_page=100", paginate=True)
    queue = []
    for pull in pulls or []:
        if pull.get("draft"):
            continue
        number = int(pull["number"])
        reviews = gh.api(f"repos/{args.repo}/pulls/{number}/reviews?per_page=100", paginate=True)
        from .gate.reads import Review, parse_time
        parsed = [Review(str((r.get('user') or {}).get('login') or ''), str((r.get('user') or {}).get('type') or ''),
                         str(r.get('state') or ''), parse_time(r.get('submitted_at')), str(r.get('body') or ''),
                         str(r.get('commit_id') or '').lower()) for r in reviews or []]
        head = str((pull.get("head") or {}).get("sha") or "").lower()
        if needs_verdict(parsed, head_sha=head, reviewer_login=reviewer):
            queue.append({"number": number, "head": head, "title": pull.get("title"),
                          "author": (pull.get("user") or {}).get("login"), "url": pull.get("html_url")})
    _emit({"repo": args.repo, "queue": queue})
    return EXIT_OK


def cmd_gate_post_verdict(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file != "-" else sys.stdin.read()
    try:
        ensure_safe(body)
    except UnsafeText as exc:
        _emit({"ok": False, "error": str(exc)})
        return EXIT_FAIL
    verdict = APPROVE if args.verdict.upper() == "APPROVE" else REVISE
    full = format_marker(args.head, verdict) + "\n\n" + body.strip() + "\n"
    payload = {"commit_id": args.head, "event": "COMMENT", "body": full}
    gh.write(["api", f"repos/{args.repo}/pulls/{args.pr}/reviews", "-X", "POST", "--input", "-"],
             stdin=json.dumps(payload))
    _emit({"ok": True, "verdict": verdict, "head": args.head})
    return EXIT_OK


def cmd_arbiter(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    """Merge Tier A PRs that pass a fresh, complete bar; one at a time."""
    label = policy["labels"]["merge_requested"]
    merged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not policy["phase"].get("merges_enabled"):
        _emit({"ok": True, "merged": [], "skipped": [], "note": "phase.merges_enabled is false"})
        return EXIT_OK
    for slug in policy["repos"]:
        repo = repo_config(policy, slug)
        if not repo.gate_may_merge:
            continue  # Tier B: humans merge
        pulls = gh.api(f"repos/{slug}/pulls?state=open&per_page=100", paginate=True)
        for pull in pulls or []:
            if not any(l.get("name") == label for l in pull.get("labels") or []):
                continue
            number = int(pull["number"])
            try:
                snapshot = _load_pull(gh, policy, slug, number)
                workdir = _ensure_objects(slug, args.workdir) if args.workdir else None
                inputs, _ = _bar_inputs(gh, policy, snapshot, workdir)
            except (IncompleteRead, PreflightError, GhError) as exc:
                skipped.append({"repo": slug, "pr": number, "reason": f"read failed: {exc}"})
                continue
            result = evaluate(snapshot, policy=policy, repo=repo, inputs=inputs, enforce_caps=True)
            if not result.ok:
                skipped.append({"repo": slug, "pr": number, "reason": result.failures})
                continue
            try:
                gate_merge.merge_pull(gh, repo=repo, number=number, head_sha=snapshot.head_sha)
            except GhError as exc:
                skipped.append({"repo": slug, "pr": number, "reason": f"merge failed: {exc}"})
                continue
            merged.append({"repo": slug, "pr": number, "head": snapshot.head_sha})
    _emit({"ok": True, "merged": merged, "skipped": skipped})
    return EXIT_OK


# ---------------------------------------------------------------- monitor

def _fetch_digests(policy: dict[str, Any], now: datetime) -> dict[str, Digest]:
    m = policy["monitor"]
    out: dict[str, Digest] = {}
    for name, base in policy["dashboards"].items():
        fetched = fetch_json(status_url(base), timeout=float(m["http_timeout_seconds"]),
                             attempts=int(m["http_attempts"]), retry_seconds=float(m["http_retry_seconds"]))
        out[name] = digest(name, fetched, now=now)
    return out


def _deploy_run(gh: Gh, repo: str, run_id: int | None) -> tuple[str, str | None]:
    if not run_id:
        return "unknown", None
    try:
        run = gh.api(f"repos/{repo}/actions/runs/{run_id}")
    except GhError:
        return "unknown", None
    status = str(run.get("status") or "unknown")
    conclusion = run.get("conclusion")
    if status == "completed":
        try:
            jobs = gh.api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
            for job in (jobs or {}).get("jobs", []):
                if job.get("name") == "deploy-production":
                    return "completed", job.get("conclusion")
        except GhError:
            pass
    return status, conclusion


def _read_cursors(gh: Gh, policy: dict[str, Any], state_file: str | None) -> tuple[dict[str, Any], str]:
    """Cursors from the ledger issue marker, or from a local file for tests/dry runs."""
    if state_file:
        path = Path(state_file)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return dict(data.get("cursors") or {}), ""
    number = int(policy["ledger"].get("issue") or 0)
    if not number:
        return {}, ""
    issue = gh.api(f"repos/{policy['ledger']['repo']}/issues/{number}")
    body = str(issue.get("body") or "")
    return parse_cursors(body), body


def _write_cursors(gh: Gh, policy: dict[str, Any], state_file: str | None, cursors: dict[str, Any],
                   body: str, now: datetime) -> None:
    if state_file:
        path = Path(state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cursors": cursors, "last_tick_at": now.isoformat()}, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        return
    number = int(policy["ledger"].get("issue") or 0)
    if not number:
        return
    gh.write(["issue", "edit", str(number), "-R", policy["ledger"]["repo"], "--body-file", "-"],
             stdin=replace_cursors(body, {**cursors, "last_tick_at": now.isoformat()}))


def _tick_authors(policy: dict[str, Any]) -> set[str]:
    """Logins whose canary tick comments are believed: the monitor's own."""
    ids = policy["identities"]
    return {login for login in (ids.get("routine_login"), *(ids.get("tick_authors") or ())) if login}


def _enforce_issues_phase(policy: dict[str, Any], *, what: str) -> str:
    """Shadow until ``phase.issues_live``: every write becomes a dry run.

    Returns a note for the output so a routine can see why nothing posted.
    """
    if policy["phase"].get("issues_live"):
        return ""
    import os
    os.environ["MAINT_DRY_RUN"] = "1"
    return f"phase.issues_live is false: {what} ran as a dry run"


def cmd_monitor_tick(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    now = _now()
    m = policy["monitor"]
    phase_note = _enforce_issues_phase(policy, what="monitor tick --apply") if args.apply else ""
    cursors, ledger_body = _read_cursors(gh, policy, args.state_file)
    digests = _fetch_digests(policy, now)
    decisions: dict[str, Any] = {"at": now.isoformat(), "instances": {}, "failures": [], "canaries": [],
                                 "rollbacks": [], "pushes": [], "signals": [], "pending_acks": [], "notes": []}
    if phase_note:
        decisions["notes"].append(phase_note)
    for name, d in digests.items():
        info = d.to_json()
        info["watcher_dead"] = (not d.ok) or watcher_dead(d.last_scan_at, poll_interval_seconds=d.poll_interval_seconds,
                                                         now=now, multiplier=float(m["watcher_dead_multiplier"]))
        info["slow"] = d.ok and d.latency_s > float(m["slow_seconds"])
        decisions["instances"][name] = info
        if not d.ok:
            decisions["signals"].append({"instance": name, "signal": "endpoint_down", "error": d.error})
        elif info["watcher_dead"]:
            decisions["signals"].append({"instance": name, "signal": "watcher_dead", "last_scan_at": info["last_scan_at"]})
        watermark = parse_time(cursors.get(f"{name}_failed_watermark"))
        if d.ok and window_saturated(d.jobs, since=watermark):
            decisions["signals"].append({"instance": name, "signal": "job_window_saturated",
                                         "note": "every listed job updated after the watermark; older failures may be unseen"})
        newest_seen = watermark
        for job in new_failed_jobs(d.jobs, after=watermark):
            error = str(job.get("error") or "")
            kind = str(job.get("kind") or "")
            fp = fingerprint(name, kind, error)
            stamp = job_time(job)
            decisions["failures"].append({
                "instance": name, "job_id": job.get("id"), "kind": kind, "pr": job.get("pr_number"),
                "head_sha": job.get("head_sha"), "status": job.get("status"), "fingerprint": fp,
                "updated_at": stamp.isoformat() if stamp else None,
                "class": classify(kind, error), "excerpt": excerpt(error, chars=int(m["error_excerpt_chars"])),
                "detail_url": job_url(policy["dashboards"][name], int(job.get("id") or 0)),
                "ack": f"--instance {name} --updated-at {stamp.isoformat() if stamp else ''}",
            })
        # The failure watermark is NOT advanced here. It moves only through
        # `issue upsert --ack-instance/--ack-updated-at` or `monitor ack`,
        # after the corresponding issue exists, so a crash between this tick
        # and the filing cannot skip a failure. With no new failures there is
        # nothing to acknowledge and the watermark may follow the newest job.
        if d.ok and not any(f["instance"] == name for f in decisions["failures"]):
            latest = max((job_time(j) for j in d.jobs if job_time(j)), default=None)
            if latest and (newest_seen is None or latest > newest_seen):
                cursors[f"{name}_failed_watermark"] = latest.isoformat()
        elif d.ok:
            decisions["pending_acks"].append({"instance": name, "failures": len([f for f in decisions["failures"] if f["instance"] == name])})

    # Direct pushes on the reviewbot main branch.
    rb = policy["ledger"]["repo"]
    labels = policy["labels"]
    try:
        commits = gh.api(f"repos/{rb}/commits?sha=main&per_page=50")
    except GhError as exc:
        decisions["signals"].append({"signal": "commits_unreadable", "error": str(exc)})
        commits = []
    last_seen = str(cursors.get("rb_main_sha") or "")
    fresh = new_commits_since(commits or [], last_seen_sha=last_seen) if last_seen else []
    # Server-stamped pushers: the deploy run's canary record carries
    # github.actor for its push. Commit metadata is never used for this.
    pushers: dict[str, str] = {}
    if fresh:
        for issue in gh.api(f"repos/{rb}/issues?labels={labels['canary']}&state=all&per_page=100", paginate=True) or []:
            rec = canary_mod.CanaryRecord.parse(str(issue.get("body") or ""))
            if rec and rec.pusher:
                pushers[rec.merge_sha] = rec.pusher
    def merged_pr_sha(number: int) -> str | None:
        try:
            pull = gh.api(f"repos/{rb}/pulls/{number}")
        except GhError:
            return None
        return str(pull.get("merge_commit_sha") or "") if pull.get("merged") else None

    incident_pushes = 0
    for commit in fresh:
        cls = classify_commit(commit, merged_pr_sha=merged_pr_sha, pushers=pushers,
                              humans=policy["identities"].get("humans") or ())
        is_incident = cls.kind in INCIDENT_KINDS
        incident_pushes += int(is_incident)
        decisions["pushes"].append({"sha": cls.sha, "kind": cls.kind, "pr": cls.pr_number, "pusher": cls.pusher,
                                    "message": cls.message, "incident": is_incident,
                                    "ack": f"--rb-main-sha {cls.sha}" if is_incident else ""})
    # Same rule as job cursors: the main-branch cursor advances past an
    # incident-class push only once its incident issue exists (`monitor ack`).
    if commits and not incident_pushes:
        cursors["rb_main_sha"] = str(commits[0].get("sha") or last_seen)
    elif commits:
        decisions["pending_acks"].append({"rb_main_sha": str(commits[0].get("sha") or "")})

    # Canaries.
    canaries = gh.api(f"repos/{rb}/issues?labels={labels['canary']}&state=open&per_page=100", paginate=True)
    for issue in canaries or []:
        record = canary_mod.CanaryRecord.parse(str(issue.get("body") or ""))
        if record is None:
            decisions["canaries"].append({"issue": issue.get("number"), "error": "no canary record in body"})
            continue
        comments = gh.api(f"repos/{rb}/issues/{issue['number']}/comments?per_page=100", paginate=True)
        # Only the monitor's own identity may contribute ticks: a tick marker
        # pasted by any other commenter would otherwise steer trip/close.
        trusted = _tick_authors(policy)
        ticks = [t for t in (canary_mod.TickView.parse(str(c.get("body") or ""),
                                                        watcher_multiplier=float(m["watcher_dead_multiplier"]))
                             for c in comments or []
                             if str((c.get("user") or {}).get("login") or "") in trusted) if t]
        foreign = sum(1 for c in comments or []
                      if canary_mod.TICK_MARKER in str(c.get("body") or "")
                      and str((c.get("user") or {}).get("login") or "") not in trusted)
        if foreign:
            decisions["signals"].append({"signal": "untrusted_tick_comments", "issue": issue.get("number"),
                                         "count": foreign})
        if not trusted:
            decisions["notes"].append("identities.routine_login is unset: no prior tick is trusted "
                                      "(set it after the probe)")
        status, conclusion = _deploy_run(gh, rb, record.deploy_run_id)
        if record.baseline is not None:
            new_jobs, failed_jobs = canary_mod.jobs_since(record.baseline, digests)
        else:
            new_jobs, failed_jobs = 0, 0
        tick = canary_mod.Tick(at=now, digests=digests, deploy_status=status, deploy_conclusion=conclusion,
                               new_jobs_total=new_jobs, failed_jobs_total=failed_jobs)
        current = canary_mod.TickView.parse(tick.to_marker(), watcher_multiplier=float(m["watcher_dead_multiplier"]))
        decision = canary_mod.evaluate(record, ticks + ([current] if current else []), now=now, policy=policy)
        entry = {"issue": issue.get("number"), "merge_sha": record.merge_sha, "action": decision.action,
                 "reason": decision.reason, "details": decision.details}
        decisions["canaries"].append(entry)
        if args.apply:
            _apply_canary(gh, policy, rb, int(issue["number"]), record, tick, decision, digests, now)

    # Rollbacks: advance every open incident through its state machine so a
    # tripped canary does not stay open forever and block the next deploy.
    _advance_rollbacks(gh, policy, rb, now=now, apply=args.apply, trusted=_tick_authors(policy),
                       decisions=decisions)

    if args.apply or args.state_file:
        _write_cursors(gh, policy, args.state_file, cursors, ledger_body, now)
    _emit(decisions)
    return EXIT_OK


def _apply_canary(gh: Gh, policy: dict[str, Any], repo: str, number: int, record: canary_mod.CanaryRecord,
                  tick: canary_mod.Tick, decision: canary_mod.Decision, digests: dict[str, Digest], now: datetime) -> None:
    labels = policy["labels"]
    comment_issue(gh, repo=repo, number=number,
                  body=f"tick {now.replace(microsecond=0).isoformat()}: **{decision.action}** — {decision.reason}\n\n{tick.to_marker()}")
    if decision.action == canary_mod.START:
        record.baseline = canary_mod.baseline_from(digests, started_at=now)
        record.status = "running"
        _rewrite_canary_body(gh, repo, number, record)
    elif decision.action == canary_mod.CLOSE:
        add_labels(gh, repo=repo, number=number, labels=[f"canary:{decision.details.get('outcome', 'passed')}"])
        close_issue(gh, repo=repo, number=number, comment="canary window closed: " + decision.reason)
    elif decision.action in (canary_mod.TRIP, canary_mod.DEPLOY_FAILED, canary_mod.HOLD):
        title = f"[incident] {decision.details.get('rule', decision.action)} on deploy {record.merge_sha[:8]}"
        body = "\n".join([
            f"**Canary:** #{number} · **deploy:** `{record.merge_sha}` · **previous:** `{record.pre_merge_sha}`",
            f"**Rule:** {decision.details.get('rule', decision.action)} — {decision.reason}",
            "", "Evidence: see the tick comments on the canary issue.",
            "", "Rollback state advances through labels on this issue; evidence is never rewritten.",
        ] + ([
            "", "**Production state is unverified.** A human must confirm which release the host runs "
            "(`omni-reviewbot-host-admin status`) and close this incident, or roll back by hand.",
        ] if decision.action == canary_mod.HOLD else []))
        marker = f"<!-- canary #{number} -->"
        existing = find_issue_by_marker(gh, repo=repo, marker=marker, label=labels["incident"])
        if existing is None:
            ref = create_issue(gh, repo=repo, title=title, body=body + f"\n\n{marker}",
                               labels=[labels["incident"], rollback_mod.PENDING] if decision.action == canary_mod.TRIP
                               else [labels["incident"]])
            comment_issue(gh, repo=repo, number=number, body=f"incident opened: {ref.url}")
        if decision.action in (canary_mod.DEPLOY_FAILED, canary_mod.HOLD):
            # The incident now carries the hold; the canary itself is
            # finished, so it can never linger as an orphan that blocks the
            # next deploy on its own.
            add_labels(gh, repo=repo, number=number, labels=[f"canary:{decision.details.get('rule', decision.action)}"])
            close_issue(gh, repo=repo, number=number, comment="canary closed: " + decision.reason)


_CANARY_REF = re.compile(r"<!--\s*canary #(\d+)\s*-->")


def _trusted_ticks(gh: Gh, repo: str, number: int, trusted: set[str], multiplier: float) -> list[canary_mod.TickView]:
    comments = gh.api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True) or []
    out = []
    for c in comments:
        if str((c.get("user") or {}).get("login") or "") not in trusted:
            continue
        view = canary_mod.TickView.parse(str(c.get("body") or ""), watcher_multiplier=multiplier)
        if view:
            out.append(view)
    return out


def _find_canary(gh: Gh, repo: str, label: str, *, pr_number: int | None = None,
                 number: int | None = None) -> tuple[dict[str, Any], canary_mod.CanaryRecord] | None:
    if number is not None:
        issue = gh.api(f"repos/{repo}/issues/{number}")
        record = canary_mod.CanaryRecord.parse(str(issue.get("body") or ""))
        return (issue, record) if record else None
    for issue in gh.api(f"repos/{repo}/issues?labels={label}&state=all&per_page=100", paginate=True) or []:
        record = canary_mod.CanaryRecord.parse(str(issue.get("body") or ""))
        if record and pr_number is not None and record.pr_number == pr_number:
            return issue, record
    return None


def _advance_rollbacks(gh: Gh, policy: dict[str, Any], repo: str, *, now: datetime, apply: bool,
                       trusted: set[str], decisions: dict[str, Any]) -> None:
    """Walk every open incident through the rollback state machine (plan §5).

    Facts come from GitHub only: the rollback marker a trusted identity wrote
    on the incident, the revert PR's state, the revert push's own canary
    record (its deploy run) and that canary's trusted tick comments.
    """
    labels = policy["labels"]
    m = policy["monitor"]
    incidents = gh.api(f"repos/{repo}/issues?labels={labels['incident']}&state=open&per_page=100", paginate=True) or []
    for incident in incidents:
        if "pull_request" in incident:
            continue
        number = int(incident["number"])
        names = [str(l.get("name")) for l in incident.get("labels") or []]
        state = rollback_mod.current_state(names)
        if state is None:
            continue
        if state in rollback_mod.TERMINAL:
            # Reconcile: a terminal label whose side effects did not all land
            # (a close or a blocked issue) is retried here, idempotently.
            if apply:
                _before_terminal_label(gh, repo, incident, state, labels)
                _after_terminal_label(gh, repo, incident, state)
            continue
        texts = []
        if str((incident.get("user") or {}).get("login") or "") in trusted:
            texts.append(str(incident.get("body") or ""))
        comments = gh.api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True) or []
        texts += [str(c.get("body") or "") for c in comments
                  if str((c.get("user") or {}).get("login") or "") in trusted]
        marker = next((mk for mk in (rollback_mod.parse_rollback_marker(t) for t in texts) if mk), None)
        facts = rollback_mod.RollbackFacts(now=now)
        if marker and marker.get("revert_pr"):
            revert_pr = int(marker["revert_pr"])
            pull = gh.api(f"repos/{repo}/pulls/{revert_pr}")
            pr_state = "merged" if pull.get("merged") else ("closed" if pull.get("state") == "closed" else "open")
            merged_at = parse_time(pull.get("merged_at"))
            deploy_status, conclusion, healthy, succeeded_at = "", None, 0, None
            if pr_state == "merged":
                found = _find_canary(gh, repo, labels["canary"], pr_number=revert_pr)
                if found:
                    revert_canary, record = found
                    deploy_status, conclusion = _deploy_run(gh, repo, record.deploy_run_id)
                    if record.baseline is not None:
                        succeeded_at = parse_time(record.baseline.started_at)
                        healthy = rollback_mod.healthy_tick_streak(
                            _trusted_ticks(gh, repo, int(revert_canary["number"]), trusted,
                                           float(m["watcher_dead_multiplier"])))
            facts = rollback_mod.RollbackFacts(
                now=now, revert_pr_number=revert_pr, revert_pr_state=pr_state, revert_merged_at=merged_at,
                revert_deploy_status=deploy_status, revert_deploy_conclusion=conclusion,
                healthy_ticks=healthy, revert_deploy_succeeded_at=succeeded_at)
        transition = rollback_mod.next_state(state, facts, policy=policy)
        entry = {"incident": number, "from": state, "to": transition.state, "comment": transition.comment,
                 "revert_pr": facts.revert_pr_number}
        decisions["rollbacks"].append(entry)
        if not apply or transition.state == state:
            continue
        # Retry-safe ordering. Every step is idempotent and the incident stays
        # OPEN until its terminal label is persisted, so a failure anywhere
        # leaves it where the next tick re-enters:
        #   1. side effects that must exist before the state is final
        #      (close the original canary, file the blocked issue)
        #   2. the NEW state label, then the old one removed
        #   3. only then, for recovered: close the incident itself
        if transition.state in rollback_mod.TERMINAL:
            _before_terminal_label(gh, repo, incident, transition.state, labels, comment=transition.comment)
        add_labels(gh, repo=repo, number=number, labels=[transition.state])
        remove_labels(gh, repo=repo, number=number,
                      labels=[s for s in rollback_mod.STATES if s in names and s != transition.state])
        comment_issue(gh, repo=repo, number=number,
                      body=f"rollback: **{state} → {transition.state}** — {transition.comment}")
        if transition.state in rollback_mod.TERMINAL:
            _after_terminal_label(gh, repo, incident, transition.state)


def _issue_is_open(gh: Gh, repo: str, number: int) -> bool:
    return str(gh.api(f"repos/{repo}/issues/{number}").get("state") or "") == "open"


def _before_terminal_label(gh: Gh, repo: str, incident: dict[str, Any], state: str, labels: dict[str, str],
                           *, comment: str = "") -> None:
    """Terminal side effects that must exist before the state label lands; idempotent."""
    number = int(incident["number"])
    if state == rollback_mod.RECOVERED:
        ref = _CANARY_REF.search(str(incident.get("body") or ""))
        if ref:
            original = int(ref.group(1))
            if _issue_is_open(gh, repo, original):
                add_labels(gh, repo=repo, number=original, labels=["canary:rolled-back"])
                close_issue(gh, repo=repo, number=original, comment=f"rolled back; see incident #{number}")
    elif state == rollback_mod.FAILED:
        marker = f"<!-- omni-maintainer:blocked:rollback #{number} -->"
        if find_issue_by_marker(gh, repo=repo, marker=marker, label=labels["blocked"]) is None:
            create_issue(gh, repo=repo, title=f"[blocked] rollback failed for incident #{number}",
                         body="\n".join([
                             f"The rollback for incident #{number} did not restore production: {comment or state}",
                             "",
                             "A human is needed: add `maintainer:paused` to the ledger issue and set the repository "
                             "variable `PRODUCTION_DEPLOY_ENABLED=false` on omni-reviewbot, then investigate.",
                             "",
                             marker,
                         ]), labels=[labels["blocked"]])


def _after_terminal_label(gh: Gh, repo: str, incident: dict[str, Any], state: str) -> None:
    """The one side effect that must come AFTER the terminal label: closing a
    recovered incident. Closing first would hide it from the open-incident
    scan before its state was persisted."""
    number = int(incident["number"])
    if state == rollback_mod.RECOVERED and _issue_is_open(gh, repo, number):
        close_issue(gh, repo=repo, number=number, comment="production verified healthy after the rollback")


def _rewrite_canary_body(gh: Gh, repo: str, number: int, record: canary_mod.CanaryRecord) -> None:
    issue = gh.api(f"repos/{repo}/issues/{number}")
    body = str(issue.get("body") or "")
    start = body.find("<!-- omni-maintainer:canary:v1")
    head = body[:start] if start >= 0 else body + "\n\n"
    gh.write(["issue", "edit", str(number), "-R", repo, "--body-file", "-"], stdin=head + record.to_marker() + "\n")


def _advance_watermark(cursors: dict[str, Any], instance: str, updated_at: str) -> None:
    key = f"{instance}_failed_watermark"
    new = parse_time(updated_at)
    old = parse_time(cursors.get(key))
    if new is not None and (old is None or new > old):
        cursors[key] = new.isoformat()


def cmd_monitor_ack(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    """Advance a cursor after the corresponding issue exists (idempotent)."""
    now = _now()
    cursors, ledger_body = _read_cursors(gh, policy, args.state_file)
    if args.instance and args.updated_at:
        _advance_watermark(cursors, args.instance, args.updated_at)
    if args.rb_main_sha:
        cursors["rb_main_sha"] = args.rb_main_sha
    _write_cursors(gh, policy, args.state_file, cursors, ledger_body, now)
    _emit({"ok": True, "cursors": cursors})
    return EXIT_OK


def cmd_issue_upsert(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    labels = policy["labels"]
    phase_note = _enforce_issues_phase(policy, what="issue upsert")
    body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file != "-" else sys.stdin.read()
    marker = fingerprint_marker(args.fingerprint)
    if marker not in body:
        body = body.rstrip() + "\n\n" + marker + "\n"
    try:
        ensure_safe(body)
        ensure_safe(args.title)
    except UnsafeText as exc:
        _emit({"ok": False, "error": str(exc)})
        return EXIT_FAIL
    existing = find_issue_by_marker(gh, repo=args.repo, marker=marker, label=labels["filed"])
    if existing is not None:
        comment_issue(gh, repo=args.repo, number=int(existing["number"]),
                      body=f"seen again ({args.count or 1} more): {args.note or ''}\n\n{marker}")
        if existing.get("state") == "closed":
            gh.write(["issue", "reopen", str(existing["number"]), "-R", args.repo])
        result = {"ok": True, "issue": existing["number"], "created": False}
    else:
        ref = create_issue(gh, repo=args.repo, title=args.title, body=body,
                           labels=[labels["filed"], *(args.label or [])])
        result = {"ok": True, "issue": ref.number, "created": True, "url": ref.url}
    if args.ack_instance and args.ack_updated_at:
        # The issue exists (or was printed under dry run); only now may the
        # failure watermark move past this job's last update.
        cursors, ledger_body = _read_cursors(gh, policy, args.state_file)
        _advance_watermark(cursors, args.ack_instance, args.ack_updated_at)
        _write_cursors(gh, policy, args.state_file, cursors, ledger_body, _now())
        result["acked"] = {f"{args.ack_instance}_failed_watermark": cursors.get(f"{args.ack_instance}_failed_watermark")}
    if phase_note:
        result["note"] = phase_note
    _emit(result)
    return EXIT_OK


# ---------------------------------------------------------------- routine helpers

def cmd_work_queue(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    issues_by_repo, pulls_by_repo = {}, {}
    for slug in policy["repos"]:
        issues_by_repo[slug] = [i for i in (gh.api(f"repos/{slug}/issues?state=open&per_page=100", paginate=True) or [])
                                if "pull_request" not in i]
        pulls_by_repo[slug] = gh.api(f"repos/{slug}/pulls?state=open&per_page=100", paginate=True) or []
    queue = build_queue(issues_by_repo, pulls_by_repo, policy=policy)
    _emit([item.__dict__ | {"priority": item.priority} for item in queue])
    return EXIT_OK


def cmd_stale_prs(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    labels = policy["labels"]
    pulls = gh.api(f"repos/{args.repo}/pulls?state=open&per_page=100", paginate=True) or []
    decisions = stale_decisions(pulls, now=_now(), policy=policy)
    out = []
    for d in decisions:
        out.append({"pr": d.number, "action": d.action, "idle_days": round(d.idle_days, 1)})
        if not args.apply or d.action == "none":
            continue
        if d.action == "label":
            gh.write(["pr", "edit", str(d.number), "-R", args.repo, "--add-label", labels["stale"]])
            gh.write(["pr", "comment", str(d.number), "-R", args.repo, "--body",
                      f"This pull request has been idle for {int(d.idle_days)} days. It will be closed after "
                      f"{policy['stale']['close_after_days']} idle days; push a commit or comment to keep it open."])
        elif d.action == "close":
            gh.write(["pr", "close", str(d.number), "-R", args.repo, "--comment",
                      "Closed after a long idle period; reopen anytime by pushing or commenting."])
    _emit(out)
    return EXIT_OK


def cmd_ledger_render(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    try:
        pre = run_preflight(gh, policy, now=_now())
    except PreflightError as exc:
        _emit({"ok": False, "error": str(exc)})
        return EXIT_FAIL
    waiting = []
    rb = policy["ledger"]["repo"]
    for pull in gh.api(f"repos/{rb}/pulls?state=open&per_page=100", paginate=True) or []:
        if any(l.get("name") == policy["labels"]["merge_requested"] for l in pull.get("labels") or []):
            waiting.append({"repo": rb, "number": pull["number"], "title": pull.get("title"), "why": "ready to merge"})
    actions = list(json.loads(Path(args.actions_file).read_text()) if args.actions_file and Path(args.actions_file).exists() else [])
    cursors = {}
    if pre.ledger_issue:
        current = gh.api(f"repos/{rb}/issues/{pre.ledger_issue}")
        cursors = parse_cursors(str(current.get("body") or ""))
    body = render_ledger(pre, policy=policy, waiting=waiting, recent_actions=actions, rendered_at=_now(),
                         cursors=cursors)
    if args.apply and pre.ledger_issue:
        gh.write(["issue", "edit", str(pre.ledger_issue), "-R", rb, "--body-file", "-"], stdin=body)
    print(body)
    return EXIT_OK


def cmd_release_revert(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    labels = policy["labels"]
    if policy["phase"].get("revert_mode") != "propose":
        _emit({"ok": False, "error": "phase.revert_mode is not 'propose'; reporting only"})
        return EXIT_FAIL
    tail = args.incident_url.rstrip("/").rsplit("/", 1)[-1]
    incident_number = int(tail) if tail.isdigit() else None
    try:
        result = prepare_revert(gh, repo=args.repo, workdir=args.workdir, merge_sha=args.merge_sha,
                                pre_merge_sha=args.pre_merge_sha, incident_url=args.incident_url,
                                reason=args.reason, labels=[labels["rollback"], labels["merge_requested"]],
                                incident_number=incident_number)
    except (ReleaseError, GhError) as exc:
        _emit({"ok": False, "error": str(exc), "needs_human": True})
        return EXIT_FAIL
    _emit({"ok": True, **result.__dict__})
    return EXIT_OK


def cmd_release_pin_check(args: argparse.Namespace, policy: dict[str, Any], gh: Gh) -> int:
    imc = next(s for s, c in policy["repos"].items() if c.get("alias") == "imc")
    rb = policy["ledger"]["repo"]
    # Provider failures are filed as `maintainer:filed` + `provider` (in IMC
    # by the monitor); incidents may also carry `provider`. Count both.
    open_provider = 0
    for slug, label in ((imc, policy["labels"]["filed"]), (rb, policy["labels"]["filed"]),
                        (rb, policy["labels"]["incident"])):
        found = gh.api(f"repos/{slug}/issues?labels={label},provider&state=open&per_page=100",
                       paginate=True) or []
        open_provider += len([i for i in found if "pull_request" not in i])
    gaps = pin_bump_preconditions(gh, imc_repo=imc, candidate_sha=args.candidate,
                                  ci_check=repo_config(policy, imc).required_checks[0],
                                  open_provider_incidents=open_provider)
    capabilities = json.loads(Path(args.capabilities).read_text()) if args.capabilities else {}
    if capabilities:
        gaps += handshake_check(capabilities, expected_direct=args.expected_direct, expected_strict=args.expected_strict,
                                expected_knowledge=args.expected_knowledge, pinned_version=args.pinned_version)
    body = ""
    if not gaps and args.current:
        compare = gh.api(f"repos/{imc}/compare/{args.current}...{args.candidate}")
        body = render_pin_bump_body(imc_repo=imc, old_sha=args.current, new_sha=args.candidate,
                                    commits=list(compare.get("commits") or []), handshake_gaps=gaps)
    _emit({"ok": not gaps, "gaps": gaps, "body": body})
    return EXIT_OK if not gaps else EXIT_FAIL


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omni-maintainer")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--policy", help="policy.json path (default: packaged policy or $MAINT_POLICY)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="pause flag and today's counters from GitHub").set_defaults(func=cmd_preflight)

    gate = sub.add_parser("gate", help="gate workflow commands").add_subparsers(dest="gate_command", required=True)
    ev = gate.add_parser("evaluate", help="evaluate the merge bar for one PR")
    ev.add_argument("--repo", required=True)
    ev.add_argument("--pr", type=int, required=True)
    ev.add_argument("--publish", action="store_true", help="create the maintainer-gate check run")
    ev.add_argument("--details-url", default="")
    ev.add_argument("--workdir", help="git checkout used to verify revert PRs")
    ev.add_argument("--head", default="", help="head sha to fail closed on when reads fail")
    ev.add_argument("--enforce-caps", action="store_true",
                    help="treat caps/pause/holds as failures (the arbiter's view) instead of notes")
    ev.set_defaults(func=cmd_gate_evaluate)
    rq = gate.add_parser("review-queue", help="open PRs whose head lacks a reviewer verdict")
    rq.add_argument("--repo", required=True)
    rq.set_defaults(func=cmd_gate_review_queue)
    pv = gate.add_parser("post-verdict", help="post the reviewer's COMMENT review with the marker")
    pv.add_argument("--repo", required=True)
    pv.add_argument("--pr", type=int, required=True)
    pv.add_argument("--head", required=True)
    pv.add_argument("--verdict", required=True, choices=["APPROVE", "REVISE", "approve", "revise"])
    pv.add_argument("--body-file", required=True)
    pv.set_defaults(func=cmd_gate_post_verdict)
    ar = gate.add_parser("arbiter", help="merge Tier A PRs that pass a fresh bar (run under one concurrency group)")
    ar.add_argument("--workdir")
    ar.set_defaults(func=cmd_arbiter)

    mon = sub.add_parser("monitor", help="monitor routine commands").add_subparsers(dest="monitor_command", required=True)
    tick = mon.add_parser("tick", help="read dashboards, classify failures and pushes, evaluate canaries")
    tick.add_argument("--state-file", default=None,
                      help="local cursor file (tests/dry runs); default reads and writes the ledger issue marker")
    tick.add_argument("--apply", action="store_true", help="post canary ticks and transitions, persist cursors")
    tick.set_defaults(func=cmd_monitor_tick)
    ack = mon.add_parser("ack", help="advance a cursor after its issue exists")
    ack.add_argument("--instance")
    ack.add_argument("--updated-at", help="the acknowledged job's updated_at (from monitor tick)")
    ack.add_argument("--rb-main-sha")
    ack.add_argument("--state-file", default=None)
    ack.set_defaults(func=cmd_monitor_ack)

    iss = sub.add_parser("issue", help="issue commands").add_subparsers(dest="issue_command", required=True)
    up = iss.add_parser("upsert", help="one issue per fingerprint")
    up.add_argument("--repo", required=True)
    up.add_argument("--fingerprint", required=True)
    up.add_argument("--title", required=True)
    up.add_argument("--body-file", required=True)
    up.add_argument("--label", action="append")
    up.add_argument("--count", type=int)
    up.add_argument("--note", default="")
    up.add_argument("--ack-instance", help="with --ack-updated-at: advance that failure watermark once the issue exists")
    up.add_argument("--ack-updated-at")
    up.add_argument("--state-file", default=None)
    up.set_defaults(func=cmd_issue_upsert)

    sub.add_parser("work-queue", help="ordered work items for the daily routine").set_defaults(func=cmd_work_queue)
    st = sub.add_parser("stale-prs", help="label/close idle PRs")
    st.add_argument("--repo", required=True)
    st.add_argument("--apply", action="store_true")
    st.set_defaults(func=cmd_stale_prs)
    lg = sub.add_parser("ledger", help="render the ledger issue body")
    lg.add_argument("--apply", action="store_true")
    lg.add_argument("--actions-file")
    lg.set_defaults(func=cmd_ledger_render)

    rel = sub.add_parser("release", help="revert and pin-bump helpers").add_subparsers(dest="release_command", required=True)
    rv = rel.add_parser("revert", help="prepare a revert PR (propose mode only)")
    rv.add_argument("--repo", required=True)
    rv.add_argument("--workdir", required=True)
    rv.add_argument("--merge-sha", required=True)
    rv.add_argument("--pre-merge-sha", required=True)
    rv.add_argument("--incident-url", required=True)
    rv.add_argument("--reason", required=True)
    rv.set_defaults(func=cmd_release_revert)
    pc = rel.add_parser("pin-check", help="preconditions and handshake for a provider pin bump")
    pc.add_argument("--candidate", required=True)
    pc.add_argument("--current", default="")
    pc.add_argument("--capabilities", help="JSON file with the candidate's get_capabilities() output")
    pc.add_argument("--expected-direct", default="1.0.0")
    pc.add_argument("--expected-strict", default="1.0.0")
    pc.add_argument("--expected-knowledge", default="1.0.0")
    pc.add_argument("--pinned-version", default="0.2.0")
    pc.set_defaults(func=cmd_release_pin_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        print(f"policy error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    gh = Gh()
    try:
        return int(args.func(args, policy, gh))
    except GhError as exc:
        _emit({"ok": False, "error": str(exc)})
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
