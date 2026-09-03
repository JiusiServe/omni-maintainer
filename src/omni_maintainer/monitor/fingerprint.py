"""Failure fingerprints and classes.

A fingerprint identifies "the same failure again" across job ids, commit
shas, paths and timestamps, so one issue tracks one cause. Classification
decides who acts: ``code`` and ``provider`` become work for the daily routine,
``infra`` and ``external`` are recorded and watched.
"""

from __future__ import annotations

import hashlib
import re

_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_URL = re.compile(r"https?://\S+")
_ISSUE_REF = re.compile(r"#\d+")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
_HOME_PATH = re.compile(r"/home/ubuntu\S*|/workspace/\S*")
_NUMBER = re.compile(r"\b\d+\b")
_SPACES = re.compile(r"\s+")

CLASSES = ("provider", "external", "infra", "code")


def normalize(error: str) -> str:
    """First meaningful line of ``error`` with volatile tokens removed."""
    text = (error or "").strip()
    first = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    first = first.casefold()
    first = _URL.sub("<url>", first)
    first = _TIMESTAMP.sub("<ts>", first)
    first = _HOME_PATH.sub("<path>", first)
    first = _SHA.sub("<sha>", first)
    first = _ISSUE_REF.sub("#<n>", first)
    first = _NUMBER.sub("<n>", first)
    return _SPACES.sub(" ", first)[:200]


def fingerprint(instance: str, kind: str, error: str) -> str:
    key = f"{instance}|{kind}|{normalize(error)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


_PROVIDER = re.compile(r"infermatrix_copilot|infermatrix copilot|sdk\.v1|strict (readiness|backend|host)|InferMatrix Direct", re.I)
_EXTERNAL = re.compile(
    r"githuberror|rate limit|api\.github\.com|http (?:4\d\d|5\d\d)|"
    r"cursor-agent .*(timed out|exit)|codex .*(timed out|exit)|"
    r"connection reset|temporary failure|remote hung up", re.I)
_INFRA = re.compile(
    r"worktree|no space left|disk|permission denied|no such file|"
    r"lock|sqlite|database is locked|killed", re.I)
_CODE = re.compile(r"omni_reviewbot|reviewrejected|traceback|assertion|keyerror|typeerror|valueerror", re.I)


def classify(kind: str, error: str) -> str:
    """One of ``provider``, ``external``, ``infra``, ``code``.

    Provider first: a traceback that reaches into ``infermatrix_copilot`` is
    the provider's, even though the frames start in ``omni_reviewbot``.
    Gate rejections (``ReviewRejected``) are reviewbot code by definition.
    """
    text = error or ""
    if "reviewrejected" in text.casefold() or "review summary is missing" in text.casefold():
        return "code"
    if _PROVIDER.search(text):
        return "provider"
    if _EXTERNAL.search(text):
        return "external"
    if _INFRA.search(text):
        return "infra"
    if _CODE.search(text):
        return "code"
    if kind in ("review",):
        return "code"
    return "infra"


def excerpt(error: str, *, chars: int) -> str:
    """A fenced, bounded quote of the error for an issue body."""
    text = (error or "").strip().replace("```", "'''")
    if len(text) > chars:
        text = text[:chars].rstrip() + " …"
    return "```text\n" + text + "\n```"
