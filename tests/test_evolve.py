from __future__ import annotations
import json
from pathlib import Path
import subprocess
import difflib
import pytest

from omni_maintainer.config import load_policy
from omni_maintainer.routine import evolve


@pytest.fixture
def action(tmp_path):
    root = tmp_path / "source"; root.mkdir()
    rel = "src/infermatrix_copilot/improve/forensics.py"
    target = root / rel; target.parent.mkdir(parents=True); target.write_text("baseline\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    base = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    patch = f"diff --git a/{rel} b/{rel}\n" + "".join(difflib.unified_diff(["baseline\n"], ["improved\n"], fromfile="a/" + rel, tofile="b/" + rel))
    target.write_text("improved\n"); source_sha = evolve.tree_sha(root); target.write_text("baseline\n")
    cid = "cand-example"
    body = f"Human-reviewed improvement.\n<!-- evolve:candidate:{cid} -->"
    return root, {"protocol": evolve.PROTOCOL, "action": "prepare_pr", "candidate": cid, "id": cid,
                  "repo": "JiusiServe/InferMatrixCopilot", "branch": f"evolve/{cid}", "base_revision": base,
                  "source_sha": source_sha, "patch": patch, "patch_sha": evolve.digest(patch.encode()),
                  "body": body, "body_sha": evolve.digest(body.encode()), "title": "Improve forensics",
                  "evaluation": {"promotable": True}, "tests": {"rc": 0}}


class GitHub:
    def __init__(self, action): self.action, self.writes = action, []
    def api(self, path):
        if "/git/ref/" in path: return {"object": {"sha": self.action["base_revision"]}}
        return []
    def write(self, args):
        self.writes.append(args)
        return type("Result", (), {"stdout": "https://github.com/JiusiServe/InferMatrixCopilot/pull/1"})()


def test_dry_run_validates_exact_patch_without_pushing(action):
    source, data = action
    gh = GitHub(data)
    ack = evolve.apply_action(gh, load_policy(), data, source=source)
    assert ack["ok"] and ack["dry_run"] and not gh.writes
    assert (source / "src/infermatrix_copilot/improve/forensics.py").read_text() == "baseline\n"


def test_refuses_tampering_and_false_success(action):
    source, data = action
    data["patch"] += "tampered"
    ack = evolve.apply_action(GitHub(data), load_policy(), data, source=source)
    assert not ack["ok"] and "hash mismatch" in ack["error"]
    data["patch_sha"] = evolve.digest(data["patch"].encode()); data["evaluation"]["promotable"] = False
    ack = evolve.apply_action(GitHub(data), load_policy(), data, source=source)
    assert not ack["ok"] and "promotion" in ack["error"]


def test_rejects_source_mismatch_and_stale_base(action):
    source, data = action
    data["source_sha"] = "f" * 64
    ack = evolve.apply_action(GitHub(data), load_policy(), data, source=source)
    assert not ack["ok"] and "differs" in ack["error"]
    gh = GitHub(data)
    gh.api = lambda path: {"object": {"sha": "a" * 40}} if "/git/ref/" in path else []
    ack = evolve.apply_action(gh, load_policy(), data, source=source)
    assert not ack["ok"] and "advanced" in ack["error"]


def test_existing_pr_is_reused_and_ack_observes_merge(action, tmp_path, monkeypatch):
    source, data = action
    subprocess.run(["git", "-C", str(source), "checkout", "-qb", data["branch"]], check=True)
    (source / "src/infermatrix_copilot/improve/forensics.py").write_text("improved\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "arm"], check=True)
    original = evolve.git
    def git(args, **kwargs):
        if args[:3] == ["remote", "set-url", "origin"]: args = [*args[:3], str(source)]
        return original(args, **kwargs)
    monkeypatch.setattr(evolve, "git", git)
    pr = {"html_url": "https://github.com/JiusiServe/InferMatrixCopilot/pull/4", "number": 4,
          "body": data["body"], "state": "closed", "merged_at": "2026-10-04", "merge_commit_sha": "b" * 40}
    gh = GitHub(data)
    gh.api = lambda path: pr if path.endswith("/4") else [pr]
    outbox = tmp_path / "outbox"; (outbox / "actions").mkdir(parents=True)
    (outbox / "actions" / "cand-example.json").write_text(json.dumps(data))
    for _ in range(2):
        report = evolve.run(gh, load_policy(), outbox=outbox, source=source)
        assert not report["failed"] and report["observed"] == 1
    assert not gh.writes
    assert json.loads((outbox / "inbox/cand-example.json").read_text())["merged"]


@pytest.mark.parametrize("bad", ["src/infermatrix_copilot/improve/budget.py", "../escape.py", "/tmp/escape.py"])
def test_publication_never_applies_protected_paths(action, bad):
    _, data = action
    data["patch"] = f"diff --git a/{bad} b/{bad}\n--- a/{bad}\n+++ b/{bad}\n"
    data["patch_sha"] = evolve.digest(data["patch"].encode())
    with pytest.raises(ValueError, match="protected"): evolve.validate(data, load_policy())


def test_retry_after_push_reuses_exact_branch(action, tmp_path, monkeypatch):
    source, data = action
    from omni_maintainer.routine.ghcli import GhError
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(source), str(remote)], check=True)
    policy = load_policy(); policy["phase"]["evolution_prs_live"] = True
    monkeypatch.delenv("MAINT_DRY_RUN", raising=False)
    original = evolve.git
    pushes = []
    def git(args, **kwargs):
        if args[:3] == ["remote", "set-url", "origin"]: args = [*args[:3], str(remote)]
        if args[:1] == ["push"]: pushes.append(args)
        return original(args, **kwargs)
    monkeypatch.setattr(evolve, "git", git)
    gh = GitHub(data)
    def api(path):
        if "/git/ref/" in path: return {"object": {"sha": data["base_revision"]}}
        if "/matching-refs/" in path:
            found = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", "refs/heads/" + data["branch"]], capture_output=True, text=True)
            return [{"ref": "refs/heads/" + data["branch"], "object": {"sha": found.stdout.strip()}}] if found.returncode == 0 else []
        return []
    gh.api = api
    def failed(args): raise GhError("temporary PR request failure")
    gh.write = failed
    first = evolve.apply_action(gh, policy, data, source=source)
    assert not first["ok"] and "temporary" in first["error"]
    before = subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/" + data["branch"]], text=True)
    gh.write = lambda args: type("Result", (), {"stdout": "https://github.com/JiusiServe/InferMatrixCopilot/pull/1"})()
    second = evolve.apply_action(gh, policy, data, source=source)
    after = subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/" + data["branch"]], text=True)
    assert second["ok"] and before == after and len(pushes) == 1
