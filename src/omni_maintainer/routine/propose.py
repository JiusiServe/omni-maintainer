"""``maintainer propose``: the publication channel of InferMatrixCopilot's
meta-improvement engine (its design §9.2).

The engine holds no GitHub token. It writes *action files* into an outbox
directory (``improve-outbox/1``): open a proposal issue, update its labels and
add a trailing comment on a state change, close it. This routine picks the
actions up with the routine's own token, answers each with an *ack file*, and
observes every proposal issue (human comments, the ``maintainer: hold`` phrase
from a collaborator, pull requests that reference the issue and whether they
merged) into an *inbox file* the engine reads back. Shadow until
``phase.issues_live``: every write is a dry run and the ack says so.

Layout (the engine owns the directory; this routine writes ``acks/`` and
``inbox/`` only)::

    <outbox>/actions/<action id>.json   {"id", "action": "open|update|close", "proposal", "workflow", "repo",
                                         "state", "title", "body", "labels", "comment", "marker", "issued_at"}
    <outbox>/acks/<action id>.json      {"action": <kind>, "action_id", "proposal", "workflow", "ok", "issue",
                                         "url", "state", "dry_run", "error", "at"}
    <outbox>/inbox/<proposal id>.json   {"proposal", "workflow", "issue", "url", "state", "observed_at",
                                         "human_comments": [{"at", "author", "hold"}],
                                         "references": [{"url", "number", "merged", "at"}]}
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import dry_run
from ..monitor.issues import (UnsafeText, add_labels, close_issue, comment_issue, create_issue, ensure_safe,
                              remove_labels)
from .ghcli import Gh, GhError

PROTOCOL = "improve-outbox/1"
PROPOSAL_LABEL = "improve:proposal"
MARKER_PREFIX = "<!-- improve:proposal:v1 "
_MARKER_RE = re.compile(r"<!--\s*improve:proposal:v1\s+(\{.*?\})\s*-->", re.S)
ACTIONS = ("open", "update", "close")
# a proposal id is a file name in the engine's inbox: one path segment, no dot-prefix
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value)) and ".." not in value


def effective_dry_run(policy: dict[str, Any]) -> bool:
    """A write is a dry run when MAINT_DRY_RUN is set OR the issues phase is
    not live; the ack must say what actually happened."""
    return dry_run() or not bool(policy["phase"].get("issues_live"))


class ProposeError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def parse_marker(body: str) -> dict[str, Any] | None:
    """The proposal marker's payload (``proposal``, ``workflow``, ``tier``, ``state``)."""
    match = _MARKER_RE.search(body or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not valid_id(payload.get("proposal")):
        return None            # an id that is not a plain name never becomes a path
    return payload


def marker_id(marker: str) -> str:
    payload = parse_marker(marker)
    return str(payload["proposal"]) if payload else ""


def pending_actions(outbox: Path) -> list[dict[str, Any]]:
    """Action files without an ack, oldest first; malformed files are skipped
    with an error ack so the engine sees them."""
    actions_dir, acks_dir = outbox / "actions", outbox / "acks"
    if not actions_dir.is_dir():
        return []
    acked = {p.stem for p in acks_dir.glob("*.json")} if acks_dir.is_dir() else set()
    out = []
    for path in sorted(actions_dir.glob("*.json")):
        if path.name.startswith(".") or path.stem in acked:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _atomic_write(acks_dir / f"{path.stem}.json", {"action": "", "action_id": path.stem, "ok": False,
                                                           "error": f"unreadable action file: {exc}", "at": _now_iso()})
            continue
        if not isinstance(data, dict) or data.get("protocol") != PROTOCOL or data.get("action") not in ACTIONS \
                or str(data.get("id") or "") != path.stem:
            _atomic_write(acks_dir / f"{path.stem}.json", {"action": str((data or {}).get("action") or "") if isinstance(data, dict) else "",
                                                           "action_id": path.stem, "ok": False,
                                                           "error": "malformed action (protocol/action/id)", "at": _now_iso()})
            continue
        out.append(data)
    return out


def find_proposal_issue(gh: Gh, *, repo: str, proposal: str) -> dict[str, Any] | None:
    """The issue carrying this proposal's marker (labelled issues only). A
    failed listing raises: an open must never create a duplicate because
    the scan for the existing issue happened to fail."""
    issues = gh.api(f"repos/{repo}/issues?labels={PROPOSAL_LABEL}&state=all&per_page=100", paginate=True)
    for issue in issues or []:
        if not isinstance(issue, dict) or "pull_request" in issue:
            continue
        payload = parse_marker(str(issue.get("body") or ""))
        if payload and str(payload.get("proposal")) == proposal:
            return issue
    return None


def _labels_of(issue: dict[str, Any]) -> set[str]:
    return {str(l.get("name")) for l in (issue.get("labels") or []) if isinstance(l, dict) and l.get("name")}


_LABEL_COLOURS = {"improve:proposal": "5319e7", "improve:tier1": "c2e0c6", "improve:tier2": "0e8a16"}


def ensure_labels(gh: Gh, *, repo: str, labels: list[str]) -> list[str]:
    """Create the `improve:*` labels an action needs that the repository
    lacks (Phase 0 provisions only the maintainer labels; `gh` refuses an
    unknown label). Returns the labels created. A failed listing raises so
    the action fails in its ack rather than half-applying."""
    wanted = [l for l in labels if l.startswith("improve:")]
    if not wanted:
        return []
    existing = gh.api(f"repos/{repo}/labels?per_page=100", paginate=True) or []
    have = {str(l.get("name")) for l in existing if isinstance(l, dict)}
    created = []
    for name in wanted:
        if name in have:
            continue
        colour = _LABEL_COLOURS.get(name, "bfd4f2" if name.startswith("improve:tier") else "ededed")
        gh.write(["label", "create", name, "-R", repo, "--color", colour,
                  "--description", "meta-improvement engine proposal (see the outbox protocol)"])
        created.append(name)
    return created


def apply_action(gh: Gh, policy: dict[str, Any], action: dict[str, Any], *, repo_override: str = "") -> dict[str, Any]:
    """Execute one action against GitHub and return its ack (never raises
    for a GitHub failure: the ack carries the error)."""
    kind = str(action["action"])
    repo = repo_override or str(action.get("repo") or "")
    ack: dict[str, Any] = {"action": kind, "action_id": str(action["id"]), "proposal": str(action.get("proposal") or ""),
                           "workflow": str(action.get("workflow") or ""), "state": str(action.get("state") or ""),
                           "ok": False, "at": _now_iso(), "dry_run": effective_dry_run(policy)}
    if not repo or "/" not in repo:
        ack["error"] = "action names no repository"
        return ack
    if not valid_id(action.get("proposal")):
        ack["error"] = "proposal id is not a plain name"
        return ack
    try:
        labels = [str(l) for l in (action.get("labels") or []) if str(l).startswith("improve:")]
        if PROPOSAL_LABEL not in labels:
            labels.insert(0, PROPOSAL_LABEL)
        proposal = str(action.get("proposal") or "")
        existing = find_proposal_issue(gh, repo=repo, proposal=proposal) if proposal else None
        if kind == "open":
            title, body = str(action.get("title") or ""), str(action.get("body") or "")
            payload = parse_marker(body)
            # the body must carry THE marker (parsed, not matched as text):
            # it is how every later action and observation finds the issue
            if not title or payload is None or str(payload.get("proposal")) != proposal:
                ack["error"] = "open needs a title and a body carrying this proposal's marker"
                return ack
            ensure_safe(title)
            ensure_safe(body)
            if existing is not None:
                # the same proposal was opened before (a lost ack): reuse, never duplicate
                ack.update(ok=True, issue=int(existing["number"]), url=str(existing.get("html_url") or ""),
                           created=False)
                return ack
            ack["labels_created"] = ensure_labels(gh, repo=repo, labels=labels)
            ref = create_issue(gh, repo=repo, title=title, body=body,
                               labels=[*labels, policy["labels"]["proposed"]])
            ack.update(ok=True, issue=ref.number, url=ref.url, created=True)
            return ack
        if existing is None:
            ack["error"] = f"no issue carries proposal {proposal} in {repo}"
            return ack
        number = int(existing["number"])
        comment = str(action.get("comment") or "")
        if comment:
            ensure_safe(comment)
        if kind == "update":
            current = {l for l in _labels_of(existing) if l.startswith("improve:")}
            wanted = set(labels)
            ack["labels_created"] = ensure_labels(gh, repo=repo, labels=sorted(wanted - current))
            remove_labels(gh, repo=repo, number=number, labels=sorted(current - wanted))
            add_labels(gh, repo=repo, number=number, labels=sorted(wanted - current))
            if comment:
                comment_issue(gh, repo=repo, number=number, body=comment)
            ack.update(ok=True, issue=number, url=str(existing.get("html_url") or ""))
            return ack
        # close: labels to the terminal state, the closing comment, then close
        current = {l for l in _labels_of(existing) if l.startswith("improve:")}
        wanted = set(labels)
        ack["labels_created"] = ensure_labels(gh, repo=repo, labels=sorted(wanted - current))
        remove_labels(gh, repo=repo, number=number, labels=sorted(current - wanted))
        add_labels(gh, repo=repo, number=number, labels=sorted(wanted - current))
        if str(existing.get("state") or "") != "closed":
            close_issue(gh, repo=repo, number=number, comment=comment)
        ack.update(ok=True, issue=number, url=str(existing.get("html_url") or ""))
        return ack
    except UnsafeText as exc:
        ack["error"] = f"refused: {exc}"
        return ack
    except GhError as exc:
        ack["error"] = str(exc)[:500]
        return ack


def _parse_time(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def observe(gh: Gh, policy: dict[str, Any], *, repo: str, issue: dict[str, Any]) -> dict[str, Any] | None:
    """One proposal issue's observation for the engine: human comments (a
    collaborator's, never the routine's or a bot's), the hold phrase, and
    referencing pull requests with their merge state."""
    payload = parse_marker(str(issue.get("body") or ""))
    if not payload:
        return None
    number = int(issue["number"])
    phrase = str(policy["bar"]["hold_phrase"]).casefold()
    trusted = {a.upper() for a in policy["bar"]["collaborator_associations"]}
    own = {login for login in (policy["identities"].get("routine_login"), policy["identities"].get("reviewer_login")) if login}
    comments = gh.api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True) or []
    human_comments = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        user = c.get("user") or {}
        login, kind = str(user.get("login") or ""), str(user.get("type") or "")
        if kind.lower() == "bot" or login in own or login.endswith("[bot]"):
            continue
        association = str(c.get("author_association") or "").upper()
        if association not in trusted:
            continue
        body = str(c.get("body") or "").casefold()
        human_comments.append({"at": _parse_time(c.get("created_at")), "author": login, "hold": phrase in body})
    timeline = gh.api(f"repos/{repo}/issues/{number}/timeline?per_page=100", paginate=True) or []
    references = []
    for event in timeline:
        if not isinstance(event, dict) or event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        pr = source.get("pull_request")
        if not isinstance(pr, dict):
            continue
        references.append({"url": str(source.get("html_url") or ""), "number": source.get("number"),
                           "merged": bool(pr.get("merged_at")), "at": _parse_time(event.get("created_at"))})
    return {"protocol": PROTOCOL, "proposal": str(payload["proposal"]), "workflow": str(payload.get("workflow") or ""),
            "issue": number, "url": str(issue.get("html_url") or ""), "state": str(issue.get("state") or ""),
            "observed_at": datetime.now(timezone.utc).timestamp(), "human_comments": human_comments,
            "references": references}


def run(gh: Gh, policy: dict[str, Any], *, outbox: Path, repo: str = "") -> dict[str, Any]:
    """Apply every pending action (acks written), then observe every proposal
    issue (inbox written) — in `repo` when given, else in every repository
    the policy governs, so observations keep flowing after the actions that
    opened the issues were acknowledged long ago."""
    outbox = Path(outbox).resolve()
    if not outbox.is_dir():
        raise ProposeError(f"outbox directory {outbox} does not exist")
    report: dict[str, Any] = {"applied": [], "failed": [], "observed": 0, "repos": []}
    repos: set[str] = {repo} if repo else set(policy["repos"])
    for action in pending_actions(outbox):
        ack = apply_action(gh, policy, action, repo_override=repo)
        _atomic_write(outbox / "acks" / f"{action['id']}.json", ack)
        (report["applied"] if ack["ok"] else report["failed"]).append(
            {"id": action["id"], "action": ack["action"], "proposal": ack["proposal"], "issue": ack.get("issue"),
             "error": ack.get("error", "")})
        if not repo and str(action.get("repo") or "") and "/" in str(action["repo"]):
            repos.add(str(action["repo"]))
    inbox = outbox / "inbox"
    for name in sorted(repos):
        try:
            issues = gh.api(f"repos/{name}/issues?labels={PROPOSAL_LABEL}&state=all&per_page=100", paginate=True) or []
        except GhError as exc:
            report.setdefault("errors", []).append(f"{name}: {exc}"[:300])
            continue
        report["repos"].append(name)
        for issue in issues:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            try:
                obs = observe(gh, policy, repo=name, issue=issue)
            except GhError as exc:
                report.setdefault("errors", []).append(f"{name}#{issue.get('number')}: {exc}"[:300])
                continue
            if obs is None:
                continue
            target = (inbox / f"{obs['proposal']}.json").resolve()
            if target.parent != inbox.resolve() or not valid_id(obs["proposal"]):
                report.setdefault("errors", []).append(f"{name}#{issue.get('number')}: proposal id is not a plain name")
                continue
            _atomic_write(target, obs)
            report["observed"] += 1
    return report
