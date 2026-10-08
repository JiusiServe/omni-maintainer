# omni-maintainer

Self-maintenance tooling for [InferMatrixCopilot](https://github.com/JiusiServe/InferMatrixCopilot)
and [omni-reviewbot](https://github.com/JiusiServe/omni-reviewbot): a merge gate
that GitHub enforces, a production monitor with canaries and prepared reverts,
and helpers for the cloud routines that do the daily work.

**Principle:** GitHub is the policy engine; routines only propose. Every
privileged decision (reviewer verdict, gate check, merge, canary record) is
produced by a workflow running under a dedicated GitHub App whose key only
`main`-branch workflow runs can reach. A routine's own token can open branches,
pull requests, issues and labels, and nothing else that matters.

## Two tiers

| Repository | Tier | Who merges |
|---|---|---|
| JiusiServe/InferMatrixCopilot (public) | A | the arbiter workflow, when the bar passes |
| JiusiServe/omni-maintainer (public) | A, human-only | the arbiter, only with a human `maintainer-go` |
| JiusiServe/omni-reviewbot (private) | B | a human; the gate check is advisory |

Tier B exists because rulesets are unavailable on a private repository in a
Free-plan organization, so nothing GitHub-side can stop a direct push there.
The same limitation means omni-reviewbot has no protected environment, so its
gate runs from this repository on a schedule (`maintainer-gate-rb.yml`) with
the App token. The monitor classifies every new commit on its `main`: a merge
is only what GitHub's pull-request record confirms, and any other commit is an
incident unless the deploy run's own record names an allowlisted human as the
pusher.

## The bar (what a PR must satisfy)

Evaluated by `python -m omni_maintainer gate evaluate --repo R --pr N` from
complete, paginated reads; any incomplete read fails closed.

1. not draft, base `main`, mergeable
2. required CI check green on the exact head, **from the GitHub Actions integration**
3. reviewer verdict `APPROVE` on the exact head, authored by the gate App
4. no human "changes requested", no `maintainer:blocked`, no `maintainer: hold` comment from a collaborator
5. at least 24 h since the gate first saw this head (its own server-stamped check run; commit dates are never trusted)
6. carve-out paths need a `maintainer-go` label granted **by an allowlisted human after the gate first saw the head and not revoked since** (replayed from the timeline, not read from label presence)
7. for autonomous merges only: under the daily cap, not paused, no open canary/incident/rollback for deploying repos (the published check lists these as notes; the arbiter enforces them right before it merges)
8. small enough (600 changed lines) unless a human said go

Revert PRs skip the verdict and the veto window only when the gate proves the
reverted tree equals the recorded pre-merge commit **and** `main` still sits at
the reverted merge.

## Canary and rollback

Every push to omni-reviewbot `main` is a deploy. The deploy run itself records
a canary issue; the monitor ticks it hourly against a 14-day baseline of the
private dashboards and closes it, or trips it. A trip opens an incident whose
labels walk a rollback state machine: a revert PR is prepared (human-merged on
omni-reviewbot), its deploy is watched, and production must verify healthy on
two consecutive ticks before the incident closes.

## Commands

```
python -m omni_maintainer preflight                 # pause flag + today's counters, from GitHub
python -m omni_maintainer gate evaluate --repo R --pr N [--publish]
python -m omni_maintainer gate review-queue --repo R
python -m omni_maintainer gate post-verdict --repo R --pr N --head SHA --verdict APPROVE|REVISE --body-file F
python -m omni_maintainer gate arbiter              # Tier A merges under one concurrency group
python -m omni_maintainer monitor tick [--apply] [--credentials-file F] # dashboards, failures, pushes, canaries
python -m omni_maintainer monitor read --instance I [--job N] [--credentials-file F] # authenticated reads
python -m omni_maintainer monitor ack --instance I --updated-at T | --rb-main-sha S   # advance a cursor after its issue exists
python -m omni_maintainer issue upsert --repo R --fingerprint FP --title T --body-file F [--ack-instance I --ack-updated-at T]
python -m omni_maintainer work-queue
python -m omni_maintainer propose --outbox DIR [--repo R]   # the improvement engine's proposal issues
python -m omni_maintainer stale-prs --repo R [--apply]
python -m omni_maintainer ledger [--apply]
python -m omni_maintainer release revert --repo R --workdir D --merge-sha S --pre-merge-sha P --incident-url U --reason T
python -m omni_maintainer release pin-check --candidate SHA --current SHA
```

Set `MAINT_DRY_RUN=1` to print every mutating `gh`/`git push` call instead of
running it.

## Pausing

Add the label `maintainer:paused` to the pinned "Maintainer ledger" issue in
omni-reviewbot. Only a label event by an allowlisted human counts; a routine
removing the label changes nothing. Set the repository variable
`PRODUCTION_DEPLOY_ENABLED=false` on omni-reviewbot to stop deployments.

## Layout

```
src/omni_maintainer/policy.json          thresholds, caps, carve-outs, labels, identities, phase flags (human-gated)
prompts/                    self-contained prompts for the probe, monitor and maintain routines
workflows/                  templates: maintainer-gate.yml (for InferMatrixCopilot), deploy-canary-job.yml
                            (fragment for omni-reviewbot deploy.yml) and rb-canary-guard.py (its script,
                            copied to omni-reviewbot deploy/canary_guard.py)
src/omni_maintainer/gate    reads, actors, verdict, bar, caps, merge
src/omni_maintainer/monitor dashboard, fingerprint, canary, rollback, pushes, issues
src/omni_maintainer/routine preflight, workqueue, release, ledger, ghcli
tests/                      fixtures are real dashboard snapshots and public PR captures
```

## Dashboard service account

The ReviewBot owner provisions a service account through its local auth CLI.
Both bot dashboards share that account and its normal cookie sessions. Set
`REVIEWBOT_DASHBOARD_USERNAME` and `REVIEWBOT_DASHBOARD_PASSWORD` in the monitor
environment; configure the same two GitHub Actions secrets for both canary
workflow steps. The dashboard URLs in policy use HTTPS.

On the production host, keep the environment assignments in
`/home/ubuntu/project/.omni-reviewbot/shared/dashboard-auth/service.env`, owned by the
service user with mode `600` in its mode `700` auth directory. Pass its path
to the helper, which reads assignments as data without executing shell code:

```sh
credentials_file=/home/ubuntu/project/.omni-reviewbot/shared/dashboard-auth/service.env
python -m omni_maintainer monitor tick --credentials-file "$credentials_file"
python -m omni_maintainer monitor read --instance vllm_omni --credentials-file "$credentials_file"
python -m omni_maintainer monitor read --instance vllm_gr --job 123 --credentials-file "$credentials_file"
```

Do not inspect, print, or shell-source the credential file, or pass passwords
on the command line. The client keeps cookies only in
memory, sends the exact HTTPS Origin on login, and shares the `/code_review/`
cookie across both instances. A protected GET returning `401` triggers one
login and one retry; failed login never falls back to anonymous access. During
the first deployment, the older app can still answer the initial GET before
the protected server is promoted. No legacy bearer token is sent or accepted
by this client. HTTP URLs and redirects are rejected before credentials can
be submitted.

## Development

```
python -m pip install -e ".[dev]"
python -m pytest -q
```

Runtime is standard-library only. Every change to this repository is
human-merged by design (the arbiter needs a human `maintainer-go` here).

### Human-reviewed workflow evolution

`omni-maintainer evolve --outbox /srv/copilot/evolution-outbox --source /srv/repos/InferMatrixCopilot`
consumes the separate `evolve-outbox/1` protocol. It checks promotion evidence, test status,
patch/report digests and the exact applied source tree against a trusted local baseline.
The target base must still match the evaluated baseline. It never executes candidate code.

`phase.evolution_prs_live` defaults to `false`; dry runs validate artifacts without pushing.
When the repository policy enables publication and `MAINT_DRY_RUN` is unset, the command
pushes `evolve/<candidate>` and creates a draft PR. A retry reuses an already pushed matching
branch or an existing marked PR. A changed branch or stale base requires re-evaluation.
The regular arbiter cannot autonomously promote these PRs: a verified human go is required.
Merge observations are written to `inbox/`; Copilot independently waits for production
source/config fingerprints before updating its deployment baseline.
