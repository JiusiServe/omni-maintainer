#!/usr/bin/env python3
"""A stand-in for the gh CLI, so the Phase 0 script can be run end to end in a
test. Every invocation is appended to $GH_LOG as one JSON line: argv, and the
stdin it was given. It answers the handful of reads the script performs."""

import json
import os
import sys

argv = sys.argv[1:]
stdin = ""
if not sys.stdin.isatty():
    try:
        stdin = sys.stdin.read()
    except Exception:
        stdin = ""

with open(os.environ["GH_LOG"], "a") as log:
    log.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")


def has(*needles: str) -> bool:
    return any(any(n in a for n in needles) for a in argv)


if argv[:1] == ["auth"]:
    sys.exit(0)
if has("/apps/omni-maintainer-gate"):
    print(os.environ.get("FAKE_GATE_APP_ID", "918273"))
elif has("/apps/github-actions"):
    print("15368")
elif has("deployment-branch-policies") and "-X" not in argv:
    # the read: gh --jq has already been applied by the real gh, so emit the
    # shape the script's two different --jq filters would produce.
    if any("join" in a for a in argv):
        print(os.environ.get("FAKE_BRANCH_POLICY", "main"))
    else:
        for name in os.environ.get("FAKE_BRANCH_POLICY", "main").split(","):
            print(name)
elif has("/rulesets/") and "-X" not in argv:
    # the whole ruleset, so the validator can be exercised; each knob below
    # breaks exactly one of the properties it checks.
    ci = "tests" if any("omni-maintainer" in a for a in argv) else "suite"
    checks = []
    if os.environ.get("FAKE_CI_BOUND", "1") != "0":
        checks.append({"context": ci, "integration_id": 15368})
    if os.environ.get("FAKE_GATE_CHECK_BOUND", "1") != "0":
        checks.append({"context": "maintainer-gate",
                       "integration_id": int(os.environ.get(
                           "FAKE_RULESET_GATE_ID",
                           os.environ.get("FAKE_GATE_APP_ID", "918273")))})
    rules = [{"type": "deletion"}, {"type": "non_fast_forward"}]
    if os.environ.get("FAKE_PR_RULE", "1") != "0":
        rules.append({"type": "pull_request", "parameters": {}})
    rules.append({"type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": os.environ.get("FAKE_STRICT", "1") != "0",
        "required_status_checks": checks}})
    print(json.dumps({
        "id": int(os.environ.get("FAKE_RULESET_ID", "1")),
        "name": "main protection",
        "enforcement": os.environ.get("FAKE_ENFORCEMENT", "active"),
        "bypass_actors": [{"actor_id": 1}] * int(os.environ.get("FAKE_BYPASS_ACTORS", "0")),
        "conditions": {"ref_name": {
            "include": [os.environ.get("FAKE_REF", "~DEFAULT_BRANCH")],
            "exclude": [e for e in os.environ.get("FAKE_EXCLUDE", "").split(",") if e]}},
        "rules": rules,
    }))
elif has("/rulesets") and "-X" not in argv:
    print(os.environ.get("FAKE_RULESET_ID", ""))
elif has("environments/gate/secrets"):
    print("CLAUDE_CODE_OAUTH_TOKEN GATE_APP_ID GATE_APP_PRIVATE_KEY")
elif argv[:2] == ["label", "list"]:
    print("\n".join(os.environ.get("FAKE_LABELS", "").split(",")))
elif has("/orgs/JiusiServe/installations"):
    print("all")
elif has("actions/workflows/"):
    print("active")
else:
    print("{}")
