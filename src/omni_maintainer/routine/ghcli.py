"""The only way this package talks to GitHub.

Two verbs, deliberately: ``read`` and ``write``. Reads are always executed.
Writes are executed only when ``MAINT_DRY_RUN`` is unset; otherwise the exact
command is printed and a stub result is returned, which is how every routine
is exercised in shadow before it may post anything.

Every write is also echoed to stderr when executed, so a run log shows each
mutation the routine performed.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from ..config import dry_run


class GhError(RuntimeError):
    """A ``gh`` invocation failed; message carries the trimmed stderr."""


@dataclass(frozen=True)
class GhResult:
    ok: bool
    stdout: str
    stderr: str
    dry_run: bool = False

    def json(self) -> Any:
        if self.dry_run:
            return None
        try:
            return json.loads(self.stdout) if self.stdout.strip() else None
        except json.JSONDecodeError as exc:
            raise GhError(f"gh returned non-JSON output: {exc}") from exc


class Gh:
    """Thin subprocess wrapper around the ``gh`` CLI."""

    def __init__(self, *, binary: str = "gh", timeout: int = 120,
                 env: dict[str, str] | None = None):
        self.binary = binary
        self.timeout = timeout
        self.env = env

    # -- execution ---------------------------------------------------------

    def _run(self, args: list[str], *, stdin: str | None = None) -> GhResult:
        try:
            completed = subprocess.run(
                [self.binary, *args], text=True, capture_output=True,
                timeout=self.timeout, check=False, input=stdin,
                env=self.env if self.env is not None else None,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GhError(f"gh could not run: {exc}") from exc
        if completed.returncode != 0:
            raise GhError(
                f"gh {' '.join(args[:3])} failed (rc={completed.returncode}): "
                f"{(completed.stderr or completed.stdout).strip()[-500:]}"
            )
        return GhResult(True, completed.stdout, completed.stderr)

    def read(self, args: list[str], *, stdin: str | None = None) -> GhResult:
        """A read-only ``gh`` call. Callers must not pass mutating verbs here."""
        if _looks_mutating(args):
            raise GhError(f"refusing to run a mutating command through read(): gh {' '.join(args)}")
        return self._run(args, stdin=stdin)

    def write(self, args: list[str], *, stdin: str | None = None) -> GhResult:
        """A mutating ``gh`` call; printed instead of executed under dry run."""
        rendered = "gh " + " ".join(shlex.quote(a) for a in args)
        if dry_run():
            print(f"[dry-run] {rendered}", file=sys.stderr)
            return GhResult(True, "", "", dry_run=True)
        print(f"[write] {rendered}", file=sys.stderr)
        return self._run(args, stdin=stdin)

    # -- conveniences ------------------------------------------------------

    def api(self, path: str, *, method: str = "GET", fields: dict[str, Any] | None = None,
            paginate: bool = False, raw_fields: dict[str, str] | None = None) -> Any:
        args = ["api", path]
        if method != "GET":
            args += ["-X", method]
        if paginate:
            args += ["--paginate", "--slurp"]
        for key, value in (fields or {}).items():
            args += ["-F", f"{key}={json.dumps(value) if not isinstance(value, str) else value}"]
        for key, value in (raw_fields or {}).items():
            args += ["-f", f"{key}={value}"]
        result = self.write(args) if method != "GET" else self.read(args)
        data = result.json()
        if paginate and isinstance(data, list):
            flat: list[Any] = []
            for page in data:
                if isinstance(page, list):
                    flat.extend(page)
                else:
                    flat.append(page)
            return flat
        return data


_MUTATING_SUBCOMMANDS = {
    ("pr", "merge"), ("pr", "create"), ("pr", "edit"), ("pr", "comment"),
    ("pr", "review"), ("pr", "close"), ("pr", "reopen"), ("pr", "ready"),
    ("issue", "create"), ("issue", "edit"), ("issue", "comment"),
    ("issue", "close"), ("issue", "reopen"), ("issue", "pin"),
    ("label", "create"), ("label", "edit"), ("label", "delete"),
    ("variable", "set"), ("variable", "delete"), ("secret", "set"),
    ("workflow", "run"), ("workflow", "enable"), ("workflow", "disable"),
    ("run", "rerun"), ("run", "cancel"), ("repo", "create"), ("repo", "edit"),
    ("release", "create"), ("ruleset", "create"),
}


def _looks_mutating(args: list[str]) -> bool:
    if not args:
        return False
    if args[0] == "api":
        upper = [a.upper() for a in args]
        for flag in ("-X", "--method"):
            if flag in args:
                method = args[args.index(flag) + 1].upper() if args.index(flag) + 1 < len(args) else "GET"
                if method != "GET":
                    return True
        # -F / -f fields imply POST for gh api unless -X GET was given
        if any(a in ("-F", "-f", "--field", "--raw-field", "--input") for a in args) and "GET" not in upper:
            return True
        return False
    return tuple(args[:2]) in _MUTATING_SUBCOMMANDS


def git(args: list[str], *, cwd: str | None = None, timeout: int = 300) -> str:
    """Run git; pushes are treated as writes and honour dry run."""
    if args[:1] == ["push"]:
        rendered = "git " + " ".join(shlex.quote(a) for a in args)
        if dry_run():
            print(f"[dry-run] {rendered}", file=sys.stderr)
            return ""
        print(f"[write] {rendered}", file=sys.stderr)
    try:
        completed = subprocess.run(
            ["git", *args], text=True, capture_output=True, timeout=timeout,
            check=False, cwd=cwd,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"git could not run: {exc}") from exc
    if completed.returncode != 0:
        raise GhError(f"git {' '.join(args[:2])} failed: {(completed.stderr or completed.stdout).strip()[-500:]}")
    return completed.stdout
