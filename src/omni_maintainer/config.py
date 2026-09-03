"""Policy loading and per-repository configuration.

``omni_maintainer/policy.json`` is the single human-gated source of
thresholds, caps, carve-outs, labels and phase flags. It is loaded once per process; nothing in
the package reads environment-specific tuning from anywhere else, so a routine
prompt cannot loosen policy by exporting a variable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The policy ships inside the package so an installed wheel (the gate
# workflows `pip install` a pinned commit) carries exactly the policy that
# commit was reviewed with.
DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policy.json"

TIER_A = "A"
TIER_B = "B"


class PolicyError(RuntimeError):
    """The policy file is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class RepoConfig:
    slug: str
    alias: str
    tier: str
    deploys: bool
    required_checks: tuple[str, ...]
    optional_checks: tuple[str, ...]
    human_only: bool
    ci_workflow_name: str
    deploy_workflow: str
    deploy_workflow_name: str
    pin_file: str

    @property
    def gate_may_merge(self) -> bool:
        """Only Tier A repositories are ever merged by automation.

        ``human_only`` does not remove a repository from the arbiter; it
        makes the bar demand a human-applied ``maintainer-go`` on every PR
        (``bar.py`` rule 6), after which the arbiter may merge.
        """
        return self.tier == TIER_A


def load_policy(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load and validate the policy document.

    ``MAINT_POLICY`` may point at an alternative file for tests and dry runs;
    it cannot be used to skip validation.
    """
    candidate = Path(path or os.environ.get("MAINT_POLICY") or DEFAULT_POLICY_PATH)
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolicyError(f"policy file unreadable: {candidate}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PolicyError(f"policy file is not valid JSON: {candidate}: {exc}") from exc
    _validate(data)
    return data


def _validate(data: dict[str, Any]) -> None:
    if data.get("schema") != 1:
        raise PolicyError("policy schema must be 1")
    for key in ("phase", "identities", "repos", "labels", "caps", "bar",
                "carve_outs", "dashboards", "monitor", "canary", "ledger", "stale"):
        if key not in data:
            raise PolicyError(f"policy is missing section {key!r}")
    if not data["repos"]:
        raise PolicyError("policy lists no repositories")
    for slug, cfg in data["repos"].items():
        if "/" not in slug:
            raise PolicyError(f"repository slug must be owner/name: {slug!r}")
        if cfg.get("tier") not in (TIER_A, TIER_B):
            raise PolicyError(f"{slug}: tier must be A or B")
        if not cfg.get("required_checks"):
            raise PolicyError(f"{slug}: required_checks must not be empty")
    phase = data["phase"]
    if phase.get("revert_mode") not in ("report", "propose"):
        raise PolicyError("phase.revert_mode must be report or propose")
    caps = data["caps"]
    for key in ("merges_per_day", "prs_per_day"):
        if not isinstance(caps.get(key), int) or caps[key] < 0:
            raise PolicyError(f"caps.{key} must be a non-negative integer")
    if not data["identities"].get("reviewer_login"):
        raise PolicyError("identities.reviewer_login must be set")
    if not data["identities"].get("gate_merger_logins"):
        raise PolicyError("identities.gate_merger_logins must not be empty")
    if not data["identities"].get("humans"):
        raise PolicyError("identities.humans must list at least one allowlisted human login")


def repo_config(policy: dict[str, Any], slug: str) -> RepoConfig:
    try:
        cfg = policy["repos"][slug]
    except KeyError as exc:
        raise PolicyError(f"repository not covered by policy: {slug}") from exc
    return RepoConfig(
        slug=slug,
        alias=str(cfg.get("alias") or slug.split("/", 1)[1]),
        tier=str(cfg["tier"]),
        deploys=bool(cfg.get("deploys", False)),
        required_checks=tuple(cfg["required_checks"]),
        optional_checks=tuple(cfg.get("optional_checks") or ()),
        human_only=bool(cfg.get("human_only", False)),
        ci_workflow_name=str(cfg.get("ci_workflow_name") or ""),
        deploy_workflow=str(cfg.get("deploy_workflow") or ""),
        deploy_workflow_name=str(cfg.get("deploy_workflow_name") or ""),
        pin_file=str(cfg.get("pin_file") or ""),
    )


def carve_out_globs(policy: dict[str, Any], slug: str) -> tuple[str, ...]:
    carve = policy["carve_outs"]
    return tuple(carve.get("all") or ()) + tuple(carve.get(slug) or ())


def carve_out_exempt(policy: dict[str, Any], slug: str) -> tuple[str, ...]:
    return tuple((policy["carve_outs"].get("exempt") or {}).get(slug) or ())


def dry_run() -> bool:
    """Every mutating call is printed instead of executed when set."""
    return os.environ.get("MAINT_DRY_RUN", "").strip() not in ("", "0", "false", "no")
