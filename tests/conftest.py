from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omni_maintainer.config import load_policy
from omni_maintainer.gate.reads import build_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def policy():
    return load_policy()


@pytest.fixture
def now():
    return datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def pr84(policy):
    """Real public PR JiusiServe/InferMatrixCopilot#84 as captured on 2026-09-03."""
    return build_snapshot(
        "JiusiServe/InferMatrixCopilot",
        pull=load("pr84_pull.json"), files=load("pr84_files.json"), reviews=load("pr84_reviews.json"),
        comments=load("pr84_comments.json"), commits=load("pr84_commits.json"),
        check_runs=load("pr84_checkruns.json"), max_files=int(policy["bar"]["max_files"]), timeline=[],
    )
