"""The owner runbook, the App manifest and the setup script must agree with
policy.json and with the workflows, or Phase 0 configures the wrong system."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "setup-phase-0.sh"
RUNBOOK = ROOT / "docs" / "phase-0.md"
MANIFEST = ROOT / "docs" / "gate-app-manifest.json"
POLICY = json.loads((ROOT / "src" / "omni_maintainer" / "policy.json").read_text())


def script() -> str:
    return SCRIPT.read_text()


def script_labels() -> set[str]:
    block = re.search(r"^LABELS=\((.*?)^\)", script(), re.S | re.M).group(1)
    return {line.split("|")[0].strip().strip('"') for line in block.strip().splitlines()}


def test_the_script_creates_exactly_the_labels_the_policy_names() -> None:
    assert script_labels() == set(POLICY["labels"].values())


def test_every_required_check_matches_the_policy_for_that_repository() -> None:
    block = re.search(r"declare -A REQUIRED_CI=\((.*?)\)\n", script(), re.S).group(1)
    pairs = dict(re.findall(r"\[(\S+?)\]=(\S+)", block))
    for repo, check in pairs.items():
        expected = POLICY["repos"][f"JiusiServe/{repo}"]["required_checks"]
        assert expected == [check], f"{repo}: script requires {check}, policy requires {expected}"


def test_the_ruleset_binds_both_checks_to_an_integration() -> None:
    """A check required by name alone can be created by any app, so binding is
    the whole point of the ruleset."""
    body = script()
    assert '"integration_id": int(os.environ["ACTIONS_ID"])' in body
    assert '"integration_id": int(os.environ["GATE_ID"])' in body
    assert '"bypass_actors": []' in body, "a bypass actor makes the bar optional"
    assert '"strict_required_status_checks_policy": True' in body, "a branch behind main must be updated"


def test_the_gate_app_slug_matches_the_policy_identity() -> None:
    slug = re.search(r"^GATE_APP_SLUG=(\S+)", script(), re.M).group(1)
    assert slug == POLICY["identities"]["gate_app_slug"]
    assert POLICY["identities"]["reviewer_login"] == f"{slug}[bot]"
    actions = re.search(r"^ACTIONS_APP_SLUG=(\S+)", script(), re.M).group(1)
    assert actions == POLICY["identities"]["ci_app_slug"]


def test_secrets_go_to_the_gate_environment_and_never_to_the_repository() -> None:
    referenced = set(re.findall(r"secrets\.([A-Z_]+)", "".join(
        p.read_text() for p in sorted((ROOT / ".github" / "workflows").glob("*.yml")) )
        + (ROOT / "workflows" / "maintainer-gate.yml").read_text()))
    for name in referenced:
        assert re.search(rf"secret_set {name} ", script()), \
            f"{name} is used by a workflow but the script does not set it in the gate environment"
    assert re.search(r"gh secret set \"\$name\" --env gate", script()), \
        "a repository-level secret is readable by any workflow, including one a PR head carries"
    assert not re.search(r"gh secret set \S+ --repo", script())


def test_the_script_refuses_to_write_the_key_into_an_unrestricted_environment() -> None:
    body = script()
    assert "policy_is_main_only" in body
    guard = body[body.index("set_secrets()"):body.index("# --- labels")]
    assert "refusing to write the gate key" in guard
    assert guard.index("policy_is_main_only") < guard.index("secret_set "), \
        "the branch-policy check must precede every secret write"


def test_the_private_repository_gets_no_environment_and_no_ruleset() -> None:
    body = script()
    public = re.search(r"^PUBLIC_REPOS=\((.*?)\)", body, re.M).group(1).split()
    ruleset = re.search(r"^RULESET_REPOS=\((.*?)\)", body, re.M).group(1).split()
    every = re.search(r"^ALL_REPOS=\((.*?)\)", body, re.M).group(1).split()
    assert "omni-reviewbot" in every, "labels and the ledger still live there"
    assert "omni-reviewbot" not in public
    assert "omni-reviewbot" not in ruleset


def test_the_manifest_asks_for_the_permissions_the_gate_actually_uses() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["name"] == POLICY["identities"]["gate_app_slug"]
    assert manifest["default_permissions"] == {
        "checks": "write", "contents": "write", "issues": "write",
        "pull_requests": "write", "actions": "read", "metadata": "read"}
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["default_events"] == [], "the gate is polled and dispatched, never pushed to"


def test_the_runbook_covers_every_step_the_script_does_not() -> None:
    text = RUNBOOK.read_text()
    for needed in ("claude setup-token", "Generate a private key", "Install App",
                   "gh workflow enable", "--verify", "--dry-run"):
        assert needed in text, f"the runbook never mentions {needed}"
    assert "omni-reviewbot" in text and "human click" in text, \
        "the runbook must say plainly that reviewbot merges stay manual"


def test_the_script_parses_and_its_flags_are_the_documented_ones() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    body = script()
    assert set(re.findall(r"^\s+(--[a-z-]+)\)", body, re.M)) == {"--dry-run", "--verify"}
    assert body.splitlines()[0].startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in body


# --- running the script, against a gh that records what it was asked --------

SECRET_TOKEN = "sk-ant-oat-DO-NOT-LEAK-ME"


def run_script(tmp_path: Path, *args: str, **env_overrides: str):
    """Run the script with a fake gh on PATH; return (result, invocations)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "gh"
    shim.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} {ROOT / 'tests' / 'fake_gh.py'} \"$@\"\n")
    shim.chmod(0o755)
    log = tmp_path / "gh.log"
    log.write_text("")
    pem = tmp_path / "gate.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nPRIVATE-KEY-BODY\n-----END PRIVATE KEY-----\n")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GH_LOG": str(log),
        "GATE_APP_ID": "918273",
        "GATE_APP_PRIVATE_KEY_FILE": str(pem),
        "CLAUDE_CODE_OAUTH_TOKEN": SECRET_TOKEN,
        **env_overrides,
    }
    result = subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, env=env)
    calls = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return result, calls


def body_of(calls, method: str, path_fragment: str) -> dict | None:
    for call in calls:
        argv = call["argv"]
        if "-X" in argv and argv[argv.index("-X") + 1] == method \
                and any(path_fragment in a for a in argv):
            return json.loads(call["stdin"])
    return None


def test_the_environment_body_uses_json_booleans_not_strings(tmp_path) -> None:
    """gh api -f would send the string "false" and GitHub answers 422."""
    result, calls = run_script(tmp_path)
    assert result.returncode == 0, result.stderr
    body = body_of(calls, "PUT", "environments/gate")
    assert body is not None, "the environment was never created"
    policy = body["deployment_branch_policy"]
    assert policy["protected_branches"] is False
    assert policy["custom_branch_policies"] is True
    for call in calls:
        assert "-f" not in call["argv"], f"-f sends strings: {call['argv']}"


def test_no_secret_value_is_ever_an_argument_or_printed(tmp_path) -> None:
    for args in ([], ["--dry-run"]):
        result, calls = run_script(tmp_path, *args)
        assert result.returncode == 0, result.stderr
        assert SECRET_TOKEN not in result.stdout + result.stderr, \
            f"the token appears in the output of {args or ['(no flags)']}"
        assert "PRIVATE-KEY-BODY" not in result.stdout + result.stderr
        for call in calls:
            joined = " ".join(call["argv"])
            assert SECRET_TOKEN not in joined, f"the token is an argument: {call['argv']}"
            assert "PRIVATE-KEY-BODY" not in joined


def test_the_secret_values_do_reach_gh_on_stdin(tmp_path) -> None:
    _, calls = run_script(tmp_path)
    sets = {c["argv"][2]: c for c in calls if c["argv"][:2] == ["secret", "set"]}
    assert set(sets) == {"GATE_APP_ID", "GATE_APP_PRIVATE_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert sets["CLAUDE_CODE_OAUTH_TOKEN"]["stdin"] == SECRET_TOKEN
    assert "PRIVATE-KEY-BODY" in sets["GATE_APP_PRIVATE_KEY"]["stdin"]
    for name, call in sets.items():
        assert "--env" in call["argv"] and call["argv"][call["argv"].index("--env") + 1] == "gate", name


def test_a_dry_run_writes_nothing(tmp_path) -> None:
    result, calls = run_script(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stderr
    for call in calls:
        assert call["argv"][:2] != ["secret", "set"], "a dry run wrote a secret"
        assert "-X" not in call["argv"] or call["argv"][call["argv"].index("-X") + 1] == "GET", \
            f"a dry run made a mutating call: {call['argv']}"
        assert call["argv"][:2] != ["label", "create"], "a dry run created a label"


def test_a_gate_environment_open_to_another_branch_stops_the_run(tmp_path) -> None:
    result, _ = run_script(tmp_path, FAKE_BRANCH_POLICY="main,release")
    assert result.returncode != 0
    assert "refusing to write the gate key" in result.stderr
    assert "also allows branch 'release'" in result.stdout


def test_the_ruleset_body_binds_both_checks_and_has_no_bypass(tmp_path) -> None:
    _, calls = run_script(tmp_path)
    body = body_of(calls, "POST", "/rulesets")
    assert body is not None, "no ruleset was created"
    assert body["bypass_actors"] == []
    checks = next(r for r in body["rules"] if r["type"] == "required_status_checks")["parameters"]
    assert checks["strict_required_status_checks_policy"] is True
    bound = {c["context"]: c["integration_id"] for c in checks["required_status_checks"]}
    assert bound == {"suite": 15368, "maintainer-gate": 918273}


def test_an_existing_ruleset_is_updated_in_place_never_duplicated(tmp_path) -> None:
    _, calls = run_script(tmp_path, FAKE_RULESET_ID="4242")
    assert body_of(calls, "POST", "/rulesets") is None, "a second ruleset was created"
    assert body_of(calls, "PUT", "/rulesets/4242") is not None


def test_verify_never_asks_the_endpoint_that_needs_an_app_jwt(tmp_path) -> None:
    """/repos/{owner}/{repo}/installation is App-authenticated; asking it with
    the owner's token fails and would report a healthy system as broken."""
    labels = ",".join(sorted(POLICY["labels"].values()))
    result, calls = run_script(tmp_path, "--verify", FAKE_RULESET_ID="4242", FAKE_LABELS=labels)
    assert result.returncode == 0, result.stdout + result.stderr
    for call in calls:
        for arg in call["argv"]:
            assert not arg.endswith("/installation"), f"needs an App JWT: {call['argv']}"
    assert "FAIL" not in result.stdout, result.stdout


HEALTHY = {"FAKE_RULESET_ID": "4242",
           "FAKE_LABELS": ",".join(sorted(POLICY["labels"].values()))}


def test_verify_fails_on_a_missing_ruleset_a_missing_label_or_an_open_environment(tmp_path) -> None:
    result, _ = run_script(tmp_path, "--verify",
                           **{**HEALTHY, "FAKE_LABELS": HEALTHY["FAKE_LABELS"].replace("maintainer-go,", "")})
    assert result.returncode != 0 and "missing labels: maintainer-go" in result.stdout

    result, _ = run_script(tmp_path, "--verify", **{**HEALTHY, "FAKE_RULESET_ID": ""})
    assert result.returncode != 0 and "main protection" in result.stdout

    result, _ = run_script(tmp_path, "--verify", **{**HEALTHY, "FAKE_BRANCH_POLICY": "main,release"})
    assert result.returncode != 0 and "not restricted to main" in result.stdout


@pytest.mark.parametrize("knob, value, expected", [
    ("FAKE_ENFORCEMENT", "disabled", "enforces nothing"),
    ("FAKE_ENFORCEMENT", "evaluate", "enforces nothing"),
    ("FAKE_BYPASS_ACTORS", "1", "1 bypass actors"),
    ("FAKE_REF", "refs/heads/release", "not the default branch"),
    ("FAKE_EXCLUDE", "refs/heads/hotfix", "excludes"),
    ("FAKE_PR_RULE", "0", "does not require a pull request"),
    ("FAKE_CI_BOUND", "0", "bound to GitHub Actions"),
    ("FAKE_GATE_CHECK_BOUND", "0", "bound to the gate App"),
    ("FAKE_STRICT", "0", "up to date"),
])
def test_a_ruleset_that_enforces_less_than_it_should_fails_verification(
        tmp_path, knob: str, value: str, expected: str) -> None:
    """A ruleset can exist and still let a change reach main without the bar:
    disabled, aimed elsewhere, or missing one of the rules that make it bite."""
    result, _ = run_script(tmp_path, "--verify", **{**HEALTHY, knob: value})
    assert result.returncode != 0, f"{knob}={value} was accepted"
    assert expected in result.stdout, result.stdout


def test_a_gate_check_from_any_other_app_does_not_satisfy_the_ruleset(tmp_path) -> None:
    result, _ = run_script(tmp_path, "--verify", **{**HEALTHY, "FAKE_RULESET_GATE_ID": "999999"})
    assert result.returncode != 0
    assert "does not require maintainer-gate bound to the gate App" in result.stdout
