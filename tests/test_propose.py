"""``maintainer propose``: the improvement engine's outbox protocol."""

from __future__ import annotations

import json
import re

from omni_maintainer import cli
from omni_maintainer.routine import propose
from omni_maintainer.routine.ghcli import GhResult

MARKER = '<!-- improve:proposal:v1 {"proposal": "prop-1-ab", "state": "open", "tier": 2, "workflow": "pr-review.agent.review_diff"} -->'


class OutboxGh:
    """Serves the reads propose makes; records writes; the issue list grows
    with every dry-run create so a lost ack can be replayed."""

    def __init__(self, issues=None):
        self.writes, self.write_bodies = [], []
        self.issues = list(issues or [])
        self.comments = {}
        self.timeline = {}

    def api(self, path, *, method="GET", fields=None, paginate=False, raw_fields=None):
        if "/issues?labels=improve:proposal" in path:
            return self.issues
        if re.search(r"/labels\?per_page", path):
            return [{"name": n} for n in getattr(self, "labels", ["maintainer:proposed", "improve:proposal"])]
        m = re.search(r"/issues/(\d+)/comments", path)
        if m:
            return self.comments.get(int(m.group(1)), [])
        m = re.search(r"/issues/(\d+)/timeline", path)
        if m:
            return self.timeline.get(int(m.group(1)), [])
        raise AssertionError(f"unexpected read: {path}")

    def read(self, args, stdin=None):
        return GhResult(True, "", "")

    def write(self, args, stdin=None):
        self.writes.append(list(args))
        self.write_bodies.append(stdin or "")
        if args[:2] == ["issue", "create"]:
            return GhResult(True, "https://github.com/o/r/issues/7\n", "")
        return GhResult(True, "", "")


def _action(outbox, kind="open", **extra):
    (outbox / "actions").mkdir(parents=True, exist_ok=True)
    data = {"protocol": "improve-outbox/1", "id": f"1700000000-prop-1-ab-{kind}", "action": kind, "proposal": "prop-1-ab",
            "workflow": "pr-review.agent.review_diff", "tier": 2, "state": "open", "repo": "o/r", "issued_at": 1700000000.0,
            "labels": ["improve:proposal", "improve:pr-review.agent.review_diff", "improve:tier2", "improve:open"],
            "marker": MARKER, "title": "[improve] pr-review.agent.review_diff: seen but not raised",
            "body": f"**Claim.** seen but not raised\n\n{MARKER}\n", **extra}
    (outbox / "actions" / f"{data['id']}.json").write_text(json.dumps(data))
    return data


def _issue(number=7, state="open", labels=("improve:proposal", "improve:pr-review.agent.review_diff", "improve:tier2", "improve:open")):
    return {"number": number, "state": state, "html_url": f"https://github.com/o/r/issues/{number}",
            "body": f"**Claim.** x\n\n{MARKER}\n", "labels": [{"name": l} for l in labels]}


def test_open_creates_the_issue_with_labels_and_acks(tmp_path, policy, monkeypatch):
    monkeypatch.delenv("MAINT_DRY_RUN", raising=False)
    policy = json.loads(json.dumps(policy))
    policy["phase"]["issues_live"] = True
    outbox = tmp_path / "outbox"
    action = _action(outbox)
    gh = OutboxGh()
    report = propose.run(gh, policy, outbox=outbox)
    assert [a["proposal"] for a in report["applied"]] == ["prop-1-ab"] and report["failed"] == []
    # the repository only has the phase-0 labels and improve:proposal: the missing ones are created first
    created = [w for w in gh.writes if w[:2] == ["label", "create"]]
    assert [w[2] for w in created] == ["improve:pr-review.agent.review_diff", "improve:tier2", "improve:open"]
    assert all("-R" in w and "--color" in w for w in created)
    create = next(w for w in gh.writes if w[:2] == ["issue", "create"])
    assert gh.writes.index(create) > gh.writes.index(created[-1]) and "-R" in create and "o/r" in create
    labels = [create[i + 1] for i, a in enumerate(create) if a == "--label"]
    assert labels == ["improve:proposal", "improve:pr-review.agent.review_diff", "improve:tier2", "improve:open", "maintainer:proposed"]
    assert MARKER in gh.write_bodies[gh.writes.index(create)]
    ack = json.loads((outbox / "acks" / f"{action['id']}.json").read_text())
    assert ack["ok"] and ack["action"] == "open" and ack["issue"] == 7 and ack["url"].endswith("/issues/7")
    assert ack["labels_created"] == ["improve:pr-review.agent.review_diff", "improve:tier2", "improve:open"]
    assert ack["proposal"] == "prop-1-ab" and ack["workflow"] == "pr-review.agent.review_diff" and not ack["dry_run"]
    # acked actions are not applied twice; a replayed open finds the existing issue instead of duplicating it
    assert propose.pending_actions(outbox) == []
    gh.issues.append(_issue())
    again = _action(outbox, id="1700000001-prop-1-ab-open")
    (outbox / "actions" / f"{again['id']}.json").write_text(json.dumps({**again, "id": "1700000001-prop-1-ab-open"}))
    report = propose.run(gh, policy, outbox=outbox)
    ack = json.loads((outbox / "acks" / "1700000001-prop-1-ab-open.json").read_text())
    assert ack["ok"] and ack["issue"] == 7 and ack["created"] is False and len([w for w in gh.writes if w[:2] == ["issue", "create"]]) == 1


def test_update_and_close_relabel_comment_and_close(tmp_path, policy, monkeypatch):
    monkeypatch.delenv("MAINT_DRY_RUN", raising=False)
    policy = json.loads(json.dumps(policy))
    policy["phase"]["issues_live"] = True
    outbox = tmp_path / "outbox"
    gh = OutboxGh([_issue()])
    upd = _action(outbox, "update", state="supported", comment=f"State `supported`: supported by its experiment.\n\n{MARKER}",
                  labels=["improve:proposal", "improve:pr-review.agent.review_diff", "improve:tier2", "improve:supported"])
    report = propose.run(gh, policy, outbox=outbox)
    assert report["applied"][0]["action"] == "update"
    assert [w[2] for w in gh.writes if w[:2] == ["label", "create"]] == ["improve:supported"]   # a new state label
    edits = [w for w in gh.writes if w[:2] == ["issue", "edit"]]
    assert any("--remove-label" in w and "improve:open" in w for w in edits)
    assert any("--add-label" in w and "improve:supported" in w for w in edits)
    assert [w for w in gh.writes if w[:2] == ["issue", "comment"]] and "supported by its experiment" in gh.write_bodies[-1]
    clo = _action(outbox, "close", state="landed", comment="Closing: landed by https://github.com/o/r/pull/40.",
                  labels=["improve:proposal", "improve:pr-review.agent.review_diff", "improve:tier2", "improve:landed"])
    report = propose.run(gh, policy, outbox=outbox)
    assert report["applied"][0]["action"] == "close"
    close = [w for w in gh.writes if w[:2] == ["issue", "close"]]
    assert close and "--comment" in close[0] and "7" in close[0]
    ack = json.loads((outbox / "acks" / f"{clo['id']}.json").read_text())
    assert ack["ok"] and ack["state"] == "landed"
    # an update for a proposal with no issue fails in its ack, never raises
    orphan = _action(outbox, "update", id="1700000002-prop-9-zz-update", proposal="prop-9-zz")
    (outbox / "actions" / "1700000002-prop-9-zz-update.json").write_text(json.dumps({**orphan, "id": "1700000002-prop-9-zz-update"}))
    report = propose.run(gh, policy, outbox=outbox)
    assert report["failed"] and "no issue carries proposal prop-9-zz" in report["failed"][0]["error"]


def test_observations_carry_human_comments_holds_and_merged_references(tmp_path, policy, monkeypatch):
    monkeypatch.delenv("MAINT_DRY_RUN", raising=False)
    policy = json.loads(json.dumps(policy))
    policy["phase"]["issues_live"] = True
    policy["identities"]["routine_login"] = "routine-bot"
    outbox = tmp_path / "outbox"
    (outbox / "actions").mkdir(parents=True)
    gh = OutboxGh([_issue(), {"number": 8, "state": "open", "html_url": "u", "body": "no marker", "labels": []}])
    gh.comments[7] = [
        {"user": {"login": "tzhouam", "type": "User"}, "author_association": "OWNER", "created_at": "2026-09-30T10:00:00Z",
         "body": "maintainer: hold — let me look first"},
        {"user": {"login": "routine-bot", "type": "User"}, "author_association": "MEMBER", "created_at": "2026-09-30T11:00:00Z",
         "body": "maintainer: hold"},                                       # the routine's own voice never counts
        {"user": {"login": "stranger", "type": "User"}, "author_association": "NONE", "created_at": "2026-09-30T12:00:00Z",
         "body": "maintainer: hold"},                                       # nor an outsider's
        {"user": {"login": "some[bot]", "type": "Bot"}, "author_association": "MEMBER", "created_at": "2026-09-30T13:00:00Z",
         "body": "seen"},
    ]
    gh.timeline[7] = [
        {"event": "cross-referenced", "created_at": "2026-09-30T14:00:00Z",
         "source": {"issue": {"number": 40, "html_url": "https://github.com/o/r/pull/40",
                              "pull_request": {"merged_at": "2026-09-30T15:00:00Z"}}}},
        {"event": "cross-referenced", "created_at": "2026-09-30T14:30:00Z",
         "source": {"issue": {"number": 41, "html_url": "https://github.com/o/r/pull/41", "pull_request": {"merged_at": None}}}},
        {"event": "cross-referenced", "created_at": "2026-09-30T14:40:00Z",
         "source": {"issue": {"number": 42, "html_url": "https://github.com/o/r/issues/42"}}},   # an issue, not a PR
        {"event": "labeled", "label": {"name": "x"}},
    ]
    report = propose.run(gh, policy, outbox=outbox, repo="o/r")
    assert report["observed"] == 1 and report["repos"] == ["o/r"]
    obs = json.loads((outbox / "inbox" / "prop-1-ab.json").read_text())
    assert obs["proposal"] == "prop-1-ab" and obs["workflow"] == "pr-review.agent.review_diff" and obs["issue"] == 7
    assert [c["author"] for c in obs["human_comments"]] == ["tzhouam"] and obs["human_comments"][0]["hold"] is True
    assert [(r["number"], r["merged"]) for r in obs["references"]] == [(40, True), (41, False)]
    assert obs["state"] == "open" and obs["observed_at"] > 0 and gh.writes == []


def test_propose_is_a_dry_run_until_issues_live_and_refuses_credentials(tmp_path, policy, monkeypatch, capsys):
    monkeypatch.delenv("MAINT_DRY_RUN", raising=False)
    outbox = tmp_path / "outbox"
    action = _action(outbox)
    fake = OutboxGh()
    monkeypatch.setattr(cli, "Gh", lambda: fake)
    rc = cli.main(["propose", "--outbox", str(outbox)])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK and out["ok"] and "issues_live is false" in out["note"]
    ack = json.loads((outbox / "acks" / f"{action['id']}.json").read_text())
    assert ack["ok"] and ack["dry_run"] is True                         # the engine keeps the proposal unpublished
    secret = _action(outbox, id="1700000003-prop-1-ab-open", body=f"token ghp_{'A' * 30}\n{MARKER}")
    (outbox / "actions" / "1700000003-prop-1-ab-open.json").write_text(json.dumps({**secret, "id": "1700000003-prop-1-ab-open"}))
    cli.main(["propose", "--outbox", str(outbox)])
    ack = json.loads((outbox / "acks" / "1700000003-prop-1-ab-open.json").read_text())
    assert not ack["ok"] and "refused" in ack["error"] and "ghp_" not in ack["error"]
    unmarked = _action(outbox, id="1700000008-prop-1-ab-open", body='**Claim.** x\n"proposal": "prop-1-ab"\n')
    (outbox / "actions" / "1700000008-prop-1-ab-open.json").write_text(json.dumps({**unmarked, "id": "1700000008-prop-1-ab-open"}))
    other = _action(outbox, id="1700000009-prop-1-ab-open",
                    body='x\n<!-- improve:proposal:v1 {"proposal": "prop-7-zz", "state": "open", "tier": 1, "workflow": "w"} -->')
    (outbox / "actions" / "1700000009-prop-1-ab-open.json").write_text(json.dumps({**other, "id": "1700000009-prop-1-ab-open"}))
    compact = _action(outbox, id="1700000010-prop-1-ab-open", body='x\n<!--improve:proposal:v1 {"proposal":"prop-1-ab","tier":2}-->')
    (outbox / "actions" / "1700000010-prop-1-ab-open.json").write_text(json.dumps({**compact, "id": "1700000010-prop-1-ab-open"}))
    cli.main(["propose", "--outbox", str(outbox)])
    for name in ("1700000008-prop-1-ab-open", "1700000009-prop-1-ab-open"):
        assert "this proposal's marker" in json.loads((outbox / "acks" / f"{name}.json").read_text())["error"]
    assert json.loads((outbox / "acks" / "1700000010-prop-1-ab-open.json").read_text())["ok"]
    (outbox / "actions" / "bad.json").write_text("{not json")
    (outbox / "actions" / "1700000004-x-open.json").write_text(json.dumps({"protocol": "other", "action": "open", "id": "1700000004-x-open"}))
    cli.main(["propose", "--outbox", str(outbox)])
    assert "unreadable" in json.loads((outbox / "acks" / "bad.json").read_text())["error"]
    assert "malformed" in json.loads((outbox / "acks" / "1700000004-x-open.json").read_text())["error"]
    capsys.readouterr()
    assert cli.main(["propose", "--outbox", str(tmp_path / "missing")]) == cli.EXIT_FAIL
    assert propose.parse_marker("nothing") is None and propose.marker_id(MARKER) == "prop-1-ab"


def test_observation_repos_come_from_the_policy_ids_are_contained_and_lookup_failures_do_not_duplicate(
        tmp_path, policy, monkeypatch):
    from omni_maintainer.routine.ghcli import GhError

    monkeypatch.delenv("MAINT_DRY_RUN", raising=False)
    policy = json.loads(json.dumps(policy))
    policy["phase"]["issues_live"] = True
    outbox = tmp_path / "outbox"
    (outbox / "actions").mkdir(parents=True)
    # no pending action at all: every policy repository is still observed
    evil = {"number": 9, "state": "open", "html_url": "u", "labels": [],
            "body": '<!-- improve:proposal:v1 {"proposal": "../actions/victim", "workflow": "w"} -->'}
    gh = OutboxGh([_issue(), evil])
    report = propose.run(gh, policy, outbox=outbox)
    assert report["repos"] == sorted(policy["repos"]) and report["observed"] == len(policy["repos"])
    assert sorted(p.name for p in (outbox / "inbox").glob("*")) == ["prop-1-ab.json"]     # the traversal id never became a path
    assert not (outbox / "actions" / "victim").exists() and not (tmp_path / "actions").exists()
    assert propose.parse_marker(evil["body"]) is None and not propose.valid_id("/abs") and not propose.valid_id(".hidden")
    # a traversal id in an action is refused in its ack
    bad = _action(outbox, id="1700000005-x-open", proposal="../x")
    (outbox / "actions" / "1700000005-x-open.json").write_text(json.dumps({**bad, "id": "1700000005-x-open"}))
    propose.run(gh, policy, outbox=outbox, repo="o/r")
    assert "plain name" in json.loads((outbox / "acks" / "1700000005-x-open.json").read_text())["error"]
    # MAINT_DRY_RUN with issues_live: the write is skipped, and the ack says dry_run (never "published")
    monkeypatch.setenv("MAINT_DRY_RUN", "1")
    fresh = _action(outbox, id="1700000006-prop-2-cd-open", proposal="prop-2-cd",
                    body='x\n<!-- improve:proposal:v1 {"proposal": "prop-2-cd", "state": "open", "tier": 1, "workflow": "w"} -->')
    (outbox / "actions" / "1700000006-prop-2-cd-open.json").write_text(json.dumps({**fresh, "id": "1700000006-prop-2-cd-open"}))
    propose.run(gh, policy, outbox=outbox, repo="o/r")
    ack = json.loads((outbox / "acks" / "1700000006-prop-2-cd-open.json").read_text())
    assert ack["ok"] and ack["dry_run"] is True
    monkeypatch.delenv("MAINT_DRY_RUN")
    # the existing-issue scan fails: no issue is created, the ack carries the error
    class Flaky(OutboxGh):
        def api(self, path, **kw):
            if "/issues?labels=improve:proposal" in path:
                raise GhError("502 from GitHub")
            return super().api(path, **kw)
    flaky = Flaky()
    again = _action(outbox, id="1700000007-prop-3-ef-open", proposal="prop-3-ef",
                    body='x\n<!-- improve:proposal:v1 {"proposal": "prop-3-ef", "state": "open", "tier": 1, "workflow": "w"} -->')
    (outbox / "actions" / "1700000007-prop-3-ef-open.json").write_text(json.dumps({**again, "id": "1700000007-prop-3-ef-open"}))
    report = propose.run(flaky, policy, outbox=outbox, repo="o/r")
    ack = json.loads((outbox / "acks" / "1700000007-prop-3-ef-open.json").read_text())
    assert not ack["ok"] and "502" in ack["error"] and flaky.writes == [] and report["errors"]
