"""Issue bodies, markers, and the credential scrub.

Every body this package posts passes ``credential_reason`` first. The five
patterns are copied from omni-reviewbot ``src/omni_reviewbot/pipeline.py``
(``_CREDENTIAL_PATTERNS``); ``tests/test_issues.py`` pins them so a change on
either side is noticed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..routine.ghcli import Gh, GhError

CREDENTIAL_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

FINGERPRINT_MARKER = "omni-maintainer:fingerprint"
_FINGERPRINT_RE = re.compile(r"<!--\s*omni-maintainer:fingerprint:([0-9a-f]{12})\s*-->")
INCIDENT_MARKER = "omni-maintainer:incident:v1"
PROPOSAL_MARKER = "omni-maintainer:proposal:v1"


class UnsafeText(RuntimeError):
    """The text matches a credential pattern and must not be posted."""


def credential_reason(text: str) -> str:
    for pattern in CREDENTIAL_PATTERNS:
        if pattern.search(text or ""):
            # never echo the match: the reason must not become the leak
            return f"text matches a credential pattern ({pattern.pattern.split('[')[0]}…)"
    return ""


def ensure_safe(text: str) -> str:
    reason = credential_reason(text)
    if reason:
        raise UnsafeText(reason)
    return text


def fingerprint_marker(fp: str) -> str:
    return f"<!-- {FINGERPRINT_MARKER}:{fp} -->"


def parse_fingerprint(body: str) -> str | None:
    match = _FINGERPRINT_RE.search(body or "")
    return match.group(1) if match else None


def incident_marker(payload: dict[str, Any]) -> str:
    return f"<!-- {INCIDENT_MARKER} {json.dumps(payload, sort_keys=True)} -->"


@dataclass(frozen=True)
class IssueRef:
    number: int
    url: str
    created: bool


def render_failure_body(*, instance: str, kind: str, fp: str, klass: str, job_ids: list[int],
                        job_links: list[str], head_sha: str, excerpt: str, diagnosis: str) -> str:
    rows = "\n".join(f"| {jid} | {link} |" for jid, link in zip(job_ids, job_links))
    return ensure_safe("\n".join([
        f"**Instance:** `{instance}` · **kind:** `{kind}` · **class:** `{klass}` · **fingerprint:** `{fp}`",
        "",
        "| job | detail |", "|---|---|", rows or "| – | – |",
        "",
        f"**Head at failure:** `{head_sha[:12] if head_sha else '?'}`",
        "",
        "**Error (untrusted, quoted):**", excerpt,
        "",
        "**Diagnosis:**", diagnosis.strip() or "_pending_",
        "",
        f"seen: {len(job_ids)}",
        fingerprint_marker(fp),
    ]))


def find_issue_by_marker(gh: Gh, *, repo: str, marker: str, label: str,
                         state: str = "all") -> dict[str, Any] | None:
    """Scan labeled issues for an exact marker string in the body."""
    try:
        issues = gh.api(f"repos/{repo}/issues?labels={label}&state={state}&per_page=100", paginate=True)
    except GhError:
        return None
    for issue in issues or []:
        if isinstance(issue, dict) and marker in str(issue.get("body") or "") and "pull_request" not in issue:
            return issue
    return None


def create_issue(gh: Gh, *, repo: str, title: str, body: str, labels: list[str]) -> IssueRef:
    ensure_safe(title)
    ensure_safe(body)
    result = gh.write(["issue", "create", "-R", repo, "--title", title, "--body-file", "-",
                       *sum((["--label", label] for label in labels), [])], stdin=body)
    if result.dry_run:
        return IssueRef(0, "(dry-run)", True)
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    number = int(url.rsplit("/", 1)[-1]) if url.rsplit("/", 1)[-1].isdigit() else 0
    return IssueRef(number, url, True)


def comment_issue(gh: Gh, *, repo: str, number: int, body: str) -> None:
    ensure_safe(body)
    gh.write(["issue", "comment", str(number), "-R", repo, "--body-file", "-"], stdin=body)


def add_labels(gh: Gh, *, repo: str, number: int, labels: list[str]) -> None:
    if labels:
        gh.write(["issue", "edit", str(number), "-R", repo,
                  *sum((["--add-label", label] for label in labels), [])])


def remove_labels(gh: Gh, *, repo: str, number: int, labels: list[str]) -> None:
    if labels:
        gh.write(["issue", "edit", str(number), "-R", repo,
                  *sum((["--remove-label", label] for label in labels), [])])


def close_issue(gh: Gh, *, repo: str, number: int, comment: str = "") -> None:
    args = ["issue", "close", str(number), "-R", repo]
    if comment:
        ensure_safe(comment)
        args += ["--comment", comment]
    gh.write(args)
