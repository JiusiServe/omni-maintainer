"""Complete, paginated reads of a pull request, or a refusal.

The bar (``bar.py``) is only as good as what it is shown. A single
``gh pr view --json`` call caps collections silently, so a carve-out path,
a hold comment or a requested change past the cap would vanish. Every
collection here is read with ``--paginate`` and the result is rejected when
it is provably incomplete. Any raise here fails the gate closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

Runner = Callable[[str, bool], Any]  # (api path, paginate) -> parsed JSON


class IncompleteRead(RuntimeError):
    """A collection could not be read completely; the gate must refuse."""


@dataclass(frozen=True)
class FileChange:
    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    previous_filename: str = ""


@dataclass(frozen=True)
class Review:
    user_login: str
    user_type: str
    state: str
    submitted_at: datetime | None
    body: str
    commit_id: str


@dataclass(frozen=True)
class Comment:
    user_login: str
    author_association: str
    body: str
    created_at: datetime | None


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str
    conclusion: str | None
    app_slug: str
    head_sha: str
    # Server-stamped when the creating app omits it; the gate App never sets
    # it, so its own check runs are a trustworthy "first seen" clock for a head.
    started_at: datetime | None = None


@dataclass(frozen=True)
class PullSnapshot:
    repo: str
    number: int
    title: str
    head_sha: str
    head_ref: str
    base_ref: str
    draft: bool
    state: str
    mergeable: bool | None
    mergeable_state: str
    author_login: str
    author_type: str
    labels: tuple[str, ...]
    created_at: datetime
    # Author-controlled (commit metadata); informational only. The bar's
    # clocks come from server-stamped events, see ``head_first_seen``.
    last_commit_at: datetime | None
    is_fork: bool
    files: tuple[FileChange, ...]
    commits_count: int
    reviews: tuple[Review, ...]
    issue_comments: tuple[Comment, ...]
    check_runs: tuple[CheckRun, ...]
    additions: int = 0
    deletions: int = 0
    changed_files_reported: int = 0
    merged: bool = False
    # Raw issue timeline events; ``actors.py`` reads label provenance from it.
    timeline: tuple[dict[str, Any], ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

    def head_first_seen(self, *, gate_app_slug: str, check_name: str, now: datetime) -> datetime:
        """When the gate first evaluated this exact head, by its own check runs.

        Commit timestamps can be backdated by whoever pushes; the gate App's
        check runs are stamped by GitHub when the App omits ``started_at``,
        so their earliest ``started_at`` for this head is a clock the PR
        author cannot move. A head with no gate check yet is first seen now.
        """
        stamps = [run.started_at for run in self.check_runs
                  if run.name == check_name and run.app_slug == gate_app_slug
                  and run.head_sha == self.head_sha and run.started_at is not None]
        return min(stamps) if stamps else now

    def paths(self) -> tuple[str, ...]:
        out = []
        for item in self.files:
            out.append(item.filename)
            if item.previous_filename:
                out.append(item.previous_filename)
        return tuple(out)


def parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_snapshot(repo: str, *, pull: dict[str, Any], files: list[dict[str, Any]],
                   reviews: list[dict[str, Any]], comments: list[dict[str, Any]],
                   commits: list[dict[str, Any]], check_runs: dict[str, Any],
                   max_files: int, timeline: list[dict[str, Any]] | None = None) -> PullSnapshot:
    """Assemble a snapshot from raw API documents, refusing incomplete input."""
    if not isinstance(pull, dict) or "number" not in pull:
        raise IncompleteRead("pull request document is malformed")
    head = pull.get("head") or {}
    base = pull.get("base") or {}
    reported_files = int(pull.get("changed_files") or 0)
    if reported_files > max_files:
        raise IncompleteRead(
            f"pull request changes {reported_files} files, above the {max_files} the gate reads"
        )
    if len(files) < reported_files:
        raise IncompleteRead(
            f"file list is incomplete: read {len(files)} of {reported_files}"
        )
    reported_commits = int(pull.get("commits") or 0)
    if len(commits) < reported_commits:
        raise IncompleteRead(
            f"commit list is incomplete: read {len(commits)} of {reported_commits}"
        )
    reported_comments = int(pull.get("comments") or 0)
    if len(comments) < reported_comments:
        raise IncompleteRead(
            f"comment list is incomplete: read {len(comments)} of {reported_comments}"
        )
    runs = check_runs.get("check_runs") if isinstance(check_runs, dict) else None
    if runs is None:
        raise IncompleteRead("check-run document is malformed")
    total_runs = int(check_runs.get("total_count") or len(runs))
    if len(runs) < total_runs:
        raise IncompleteRead(f"check-run list is incomplete: read {len(runs)} of {total_runs}")

    head_repo = (head.get("repo") or {}).get("full_name") or ""
    base_repo = (base.get("repo") or {}).get("full_name") or repo
    last_commit_at = None
    for item in commits:
        stamp = parse_time(((item.get("commit") or {}).get("committer") or {}).get("date"))
        if stamp and (last_commit_at is None or stamp > last_commit_at):
            last_commit_at = stamp
    user = pull.get("user") or {}
    return PullSnapshot(
        repo=repo,
        number=int(pull["number"]),
        title=str(pull.get("title") or ""),
        head_sha=str(head.get("sha") or "").lower(),
        head_ref=str(head.get("ref") or ""),
        base_ref=str(base.get("ref") or ""),
        draft=bool(pull.get("draft")),
        state=str(pull.get("state") or ""),
        mergeable=pull.get("mergeable") if isinstance(pull.get("mergeable"), bool) else None,
        mergeable_state=str(pull.get("mergeable_state") or "unknown"),
        author_login=str(user.get("login") or ""),
        author_type=str(user.get("type") or ""),
        labels=tuple(sorted(str(l.get("name")) for l in (pull.get("labels") or []) if l.get("name"))),
        created_at=parse_time(pull.get("created_at")) or datetime.now(timezone.utc),
        last_commit_at=last_commit_at,
        is_fork=bool(head_repo) and head_repo != base_repo,
        files=tuple(
            FileChange(
                filename=str(f.get("filename") or ""),
                status=str(f.get("status") or ""),
                additions=int(f.get("additions") or 0),
                deletions=int(f.get("deletions") or 0),
                patch=f.get("patch") if isinstance(f.get("patch"), str) else None,
                previous_filename=str(f.get("previous_filename") or ""),
            )
            for f in files
        ),
        commits_count=len(commits),
        reviews=tuple(
            Review(
                user_login=str((r.get("user") or {}).get("login") or ""),
                user_type=str((r.get("user") or {}).get("type") or ""),
                state=str(r.get("state") or ""),
                submitted_at=parse_time(r.get("submitted_at")),
                body=str(r.get("body") or ""),
                commit_id=str(r.get("commit_id") or "").lower(),
            )
            for r in reviews
        ),
        issue_comments=tuple(
            Comment(
                user_login=str((c.get("user") or {}).get("login") or ""),
                author_association=str(c.get("author_association") or "NONE"),
                body=str(c.get("body") or ""),
                created_at=parse_time(c.get("created_at")),
            )
            for c in comments
        ),
        check_runs=tuple(
            CheckRun(
                name=str(run.get("name") or ""),
                status=str(run.get("status") or ""),
                conclusion=run.get("conclusion"),
                app_slug=str((run.get("app") or {}).get("slug") or ""),
                head_sha=str(run.get("head_sha") or "").lower(),
                started_at=parse_time(run.get("started_at")),
            )
            for run in runs
        ),
        additions=int(pull.get("additions") or 0),
        deletions=int(pull.get("deletions") or 0),
        changed_files_reported=reported_files,
        merged=bool(pull.get("merged")),
        timeline=tuple(item for item in (timeline or []) if isinstance(item, dict)),
    )


def load_snapshot(runner: Runner, repo: str, number: int, *, max_files: int,
                  mergeable_retry: Callable[[], None] | None = None) -> PullSnapshot:
    """Read everything the bar needs through ``runner``.

    ``runner(path, paginate)`` returns parsed JSON for ``gh api``. GitHub
    computes ``mergeable`` lazily; when it is still ``null`` the caller's
    ``mergeable_retry`` hook (a sleep, in production) is invoked between
    up to three attempts before the read is refused.
    """
    pull = None
    for attempt in range(3):
        pull = runner(f"repos/{repo}/pulls/{number}", False)
        if isinstance(pull, dict) and isinstance(pull.get("mergeable"), bool):
            break
        if attempt < 2 and mergeable_retry is not None:
            mergeable_retry()
    if not isinstance(pull, dict):
        raise IncompleteRead("pull request document is malformed")
    if not isinstance(pull.get("mergeable"), bool) and not pull.get("merged"):
        raise IncompleteRead("GitHub has not computed mergeability yet; refusing to guess")
    head_sha = str((pull.get("head") or {}).get("sha") or "")
    return build_snapshot(
        repo,
        pull=pull,
        files=_as_list(runner(f"repos/{repo}/pulls/{number}/files?per_page=100", True)),
        reviews=_as_list(runner(f"repos/{repo}/pulls/{number}/reviews?per_page=100", True)),
        comments=_as_list(runner(f"repos/{repo}/issues/{number}/comments?per_page=100", True)),
        commits=_as_list(runner(f"repos/{repo}/pulls/{number}/commits?per_page=100", True)),
        check_runs=_merge_check_pages(runner(f"repos/{repo}/commits/{head_sha}/check-runs?per_page=100", True)),
        max_files=max_files,
        timeline=_as_list(runner(f"repos/{repo}/issues/{number}/timeline?per_page=100", True)),
    )


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    raise IncompleteRead("expected a list from a paginated read")


def _merge_check_pages(value: Any) -> dict[str, Any]:
    """``--paginate`` on the check-runs endpoint yields one object per page."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        runs: list[dict[str, Any]] = []
        total = 0
        for page in value:
            if not isinstance(page, dict):
                raise IncompleteRead("check-run page is malformed")
            total = max(total, int(page.get("total_count") or 0))
            runs.extend(page.get("check_runs") or [])
        return {"total_count": total, "check_runs": runs}
    raise IncompleteRead("check-run document is malformed")
