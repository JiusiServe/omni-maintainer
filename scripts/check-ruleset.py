#!/usr/bin/env python3
"""Judge one ruleset on stdin. A ruleset can exist and still enforce nothing:
it can be disabled, aimed at another branch, missing the pull-request rule,
missing a required check, or let a check pass on a branch behind main. Each of
those is a way for a change to reach main without the bar, so each is checked.
"""

import json
import os
import sys

RULESET = json.load(sys.stdin)
REPO = os.environ["REPO"]
CI = os.environ["CI"]
GATE_ID = int(os.environ["GATE_ID"])
ACTIONS_ID = int(os.environ["ACTIONS_ID"])

failures: list[str] = []


def check(ok: bool, good: str, bad: str) -> None:
    print(f"  ok   {REPO} {good}" if ok else f"  FAIL {REPO} {bad}")
    if not ok:
        failures.append(bad)


rules = {rule.get("type"): rule for rule in RULESET.get("rules", [])}
checks_rule = rules.get("required_status_checks", {}).get("parameters", {})
required = {c.get("context"): c.get("integration_id") for c in checks_rule.get("required_status_checks", [])}
refs = RULESET.get("conditions", {}).get("ref_name", {})

check(RULESET.get("enforcement") == "active",
      "ruleset is active",
      f"ruleset is '{RULESET.get('enforcement')}', so it enforces nothing")
check(len(RULESET.get("bypass_actors") or []) == 0,
      "ruleset has no bypass actors",
      f"ruleset has {len(RULESET.get('bypass_actors') or [])} bypass actors; the bar is then optional")
check("~DEFAULT_BRANCH" in (refs.get("include") or []) or "refs/heads/main" in (refs.get("include") or []),
      "ruleset targets the default branch",
      f"ruleset targets {refs.get('include')}, not the default branch")
check(not (refs.get("exclude") or []),
      "ruleset excludes no ref",
      f"ruleset excludes {refs.get('exclude')}, which is a way around it")
check("pull_request" in rules,
      "ruleset requires a pull request",
      "ruleset does not require a pull request, so main can be pushed to directly")
check("non_fast_forward" in rules and "deletion" in rules,
      "ruleset blocks deletion and force-pushes",
      "ruleset allows deletion or a force-push of main")
check(required.get(CI) == ACTIONS_ID,
      f"requires {CI} from GitHub Actions",
      f"does not require {CI} bound to GitHub Actions (found {required.get(CI)!r})")
check(required.get("maintainer-gate") == GATE_ID,
      "requires maintainer-gate from the gate App",
      f"does not require maintainer-gate bound to the gate App (found {required.get('maintainer-gate')!r})")
check(checks_rule.get("strict_required_status_checks_policy") is True,
      "requires branches to be up to date with main",
      "does not require branches to be up to date, so a check can pass on a stale merge base")

sys.exit(1 if failures else 0)
