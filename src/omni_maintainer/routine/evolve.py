"""evolve-outbox/1: publish the exact evaluated patch as a human-promoted draft PR."""
from __future__ import annotations
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import time

from ..config import dry_run, repo_config
from .ghcli import Gh, GhError, git
from .propose import _atomic_write, valid_id

PROTOCOL = "evolve-outbox/1"
ALLOWED = ("src/infermatrix_copilot/engine/steps/review/*.py", "src/infermatrix_copilot/engine/agent_runtime/*.py",
           "src/infermatrix_copilot/improve/forensics.py", "src/infermatrix_copilot/improve/lints.py",
           "src/infermatrix_copilot/kb_service/intake.py", "src/infermatrix_copilot/engine/steps/pr/debug.py",
           "src/infermatrix_copilot/engine/steps/pr/rebase.py", "src/infermatrix_copilot/engine/steps/issue.py",
           "playbooks/evolution-overrides.json")

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def tree_sha(root: Path) -> str:
    files = []
    for prefix in ("src", "playbooks", "adapters", "skills"):
        for p in sorted((root / prefix).rglob("*")):
            if p.is_symlink(): raise ValueError("source contains a symlink")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                files.append((p.relative_to(root).as_posix(), digest(p.read_bytes())))
    return digest(json.dumps(files, separators=(",", ":")).encode())

def validate(action, policy):
    cid = action.get("candidate", "")
    if action.get("protocol") != PROTOCOL or action.get("action") != "prepare_pr" or not valid_id(cid) or action.get("id") != cid:
        raise ValueError("invalid evolution action identity/protocol")
    repo_config(policy, action["repo"])
    if action.get("branch") != f"evolve/{cid}": raise ValueError("unexpected candidate branch")
    for key in ("base_revision", "source_sha", "patch_sha", "body_sha"):
        if not re.fullmatch(r"[a-f0-9]{40}" if key == "base_revision" else r"[a-f0-9]{64}", action.get(key, "")):
            raise ValueError(f"invalid {key}")
    if digest(action["patch"].encode()) != action["patch_sha"] or digest(action["body"].encode()) != action["body_sha"]:
        raise ValueError("evaluated patch/report hash mismatch")
    if not action.get("evaluation", {}).get("promotable") or action.get("tests", {}).get("rc") != 0:
        raise ValueError("candidate did not pass promotion/testing")
    if f"<!-- evolve:candidate:{cid} -->" not in action["body"]: raise ValueError("missing candidate marker")
    headers = re.findall(r"^diff --git a/(\S+) b/(\S+)$", action["patch"], re.M)
    if not headers: raise ValueError("no reviewable patch")
    for a, b in headers:
        p = PurePosixPath(b)
        if a != b or p.is_absolute() or ".." in p.parts or "\\" in b or not any(fnmatchcase(b, pattern) for pattern in ALLOWED):
            raise ValueError("patch changes a protected or undeclared publication path")
    if any(word in action["patch"] for word in ("GIT binary patch", "old mode ", "new mode ", "rename from ", "deleted file mode ")):
        raise ValueError("binary, mode, rename and delete changes are not publishable")

def apply_action(gh: Gh, policy: dict, action: dict, *, source: Path) -> dict:
    ack = {"protocol": PROTOCOL, "candidate": action.get("candidate"), "source_sha": action.get("source_sha"),
           "ok": False, "dry_run": dry_run() or not policy["phase"].get("evolution_prs_live", False), "at": time.time()}
    try:
        validate(action, policy)
        repo, branch = action["repo"], action["branch"]
        existing = gh.api(f"repos/{repo}/pulls?state=all&head={repo.split('/')[0]}:{branch}") or []
        matching = [p for p in existing if f"<!-- evolve:candidate:{action['candidate']} -->" in p.get("body", "")]
        base_branch = policy.get("evolution", {}).get("base_branch", "main")
        if not matching:
            head = gh.api(f"repos/{repo}/git/ref/heads/{base_branch}")
            if head.get("object", {}).get("sha") != action["base_revision"]:
                raise ValueError("target base advanced; candidate requires re-evaluation")
        # Always validate patch and source bytes even in dry-run. Never execute candidate code.
        with tempfile.TemporaryDirectory(prefix="evolution-publish-") as temp:
            checkout = Path(temp) / "checkout"
            git(["clone", "--quiet", "--no-hardlinks", "--no-checkout", str(source), str(checkout)])
            git(["checkout", "--quiet", "--detach", action["base_revision"]], cwd=str(checkout))
            patch_path = Path(temp) / "candidate.patch"; patch_path.write_text(action["patch"])
            git(["-c", "core.hooksPath=/dev/null", "apply", "--check", str(patch_path)], cwd=str(checkout))
            git(["-c", "core.hooksPath=/dev/null", "apply", str(patch_path)], cwd=str(checkout))
            if tree_sha(checkout) != action["source_sha"]:
                raise ValueError("applied source differs from evaluated artifact")
            if matching:
                git(["remote", "set-url", "origin", f"https://github.com/{repo}.git"], cwd=str(checkout))
                git(["fetch", "--quiet", "origin", f"refs/heads/{branch}"], cwd=str(checkout))
                git(["reset", "--hard", "FETCH_HEAD"], cwd=str(checkout))
                if tree_sha(checkout) != action["source_sha"] or git(["rev-parse", "HEAD^"], cwd=str(checkout)).strip() != action["base_revision"]:
                    raise ValueError("existing PR branch differs from evaluated artifact")
                ack.update(ok=True, url=matching[0]["html_url"], number=matching[0]["number"], dry_run=False)
                return ack
            if ack["dry_run"]:
                ack.update(ok=True, branch=branch)
                return ack
            # Recover a branch pushed before a failed PR request. Verify its
            # actual bytes and ancestry rather than committing a different head.
            refs = gh.api(f"repos/{repo}/git/matching-refs/heads/{branch}") or []
            found = next((r for r in refs if r.get("ref") == f"refs/heads/{branch}"), None)
            git(["remote", "set-url", "origin", f"https://github.com/{repo}.git"], cwd=str(checkout))
            if found:
                git(["fetch", "--quiet", "origin", f"refs/heads/{branch}"], cwd=str(checkout))
                git(["reset", "--hard", "FETCH_HEAD"], cwd=str(checkout))
                if tree_sha(checkout) != action["source_sha"] or git(["rev-parse", "HEAD^"], cwd=str(checkout)).strip() != action["base_revision"]:
                    raise ValueError("existing branch differs from evaluated artifact or baseline")
            else:
                git(["checkout", "-b", branch], cwd=str(checkout))
                git(["add", "--", *[b for _, b in re.findall(r"^diff --git a/(\S+) b/(\S+)$", action["patch"], re.M)]], cwd=str(checkout))
                git(["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "user.name=omni-maintainer",
                     "-c", "user.email=omni-maintainer@users.noreply.github.com", "commit", "-m", action["title"]], cwd=str(checkout))
                # The engine never supplies a remote URL or executable command.
                git(["push", "origin", f"HEAD:refs/heads/{branch}"], cwd=str(checkout))
            body_file = Path(temp) / "PR.md"; body_file.write_text(action["body"])
            created = gh.write(["pr", "create", "-R", repo, "--draft", "--base", base_branch,
                                "--head", branch, "--title", action["title"], "--body-file", str(body_file)])
            ack.update(ok=True, url=created.stdout.strip(), branch=branch)
    except (ValueError, GhError, KeyError, OSError) as exc:
        ack["error"] = str(exc)
    return ack

def run(gh, policy, *, outbox: Path, source: Path) -> dict:
    report = {"applied": [], "failed": [], "observed": 0}
    outbox.mkdir(parents=True, exist_ok=True)
    # Separate publisher lock, without an extra dependency.
    import fcntl
    with (outbox / ".evolve.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for path in sorted((outbox / "actions").glob("*.json")):
            if not valid_id(path.stem): continue
            try:
                action = json.loads(path.read_text())
                ack_path = outbox / "acks" / path.name
                ack = json.loads(ack_path.read_text()) if ack_path.exists() else {}
                # Failed and dry-run acknowledgements may retry; successful PRs never duplicate.
                if not ack.get("ok") or ack.get("dry_run"):
                    ack = apply_action(gh, policy, action, source=source)
                    _atomic_write(ack_path, ack)
                report["applied" if ack.get("ok") else "failed"].append(ack)
                if ack.get("url") and not ack.get("dry_run"):
                    pr = gh.api(f"repos/{action['repo']}/pulls/{ack.get('number')}" ) if ack.get("number") else \
                        next((p for p in gh.api(f"repos/{action['repo']}/pulls?state=all&head={action['repo'].split('/')[0]}:{action['branch']}") or [] if p["html_url"] == ack["url"]), None)
                    if pr:
                        _atomic_write(outbox / "inbox" / path.name, {"candidate": action["candidate"], "url": ack["url"],
                            "merged": bool(pr.get("merged_at")), "closed": pr.get("state") == "closed",
                            "merge_sha": pr.get("merge_commit_sha"), "at": time.time()})
                        report["observed"] += 1
            except (ValueError, OSError, KeyError, GhError) as exc:
                report["failed"].append({"id": path.stem, "error": str(exc)})
    return report
