# Phase 0: what only the owner can do

Everything in this repository is inert until the pieces below exist. They need
an organization owner and a machine that can hold a private key, so they are
not automated. Work top to bottom; `scripts/setup-phase-0.sh` does steps 4 to 7
once steps 1 to 3 are done.

The three repositories are `JiusiServe/omni-maintainer` (this one),
`JiusiServe/InferMatrixCopilot` and `JiusiServe/omni-reviewbot`.

## 1. Create the gate App

The gate App is the only identity allowed to say "this pull request passed the
bar". Its key never leaves an environment that only `main`-branch runs can
reach, so a workflow file carried by a pull request head cannot mint its token
and cannot forge a verdict.

Create it at <https://github.com/organizations/JiusiServe/settings/apps/new>:

| Field | Value |
|---|---|
| Name | `omni-maintainer-gate` |
| Homepage | `https://github.com/JiusiServe/omni-maintainer` |
| Webhook | **uncheck Active** |
| Where can this be installed | Only on this account |

Repository permissions, and nothing else:

| Permission | Access | Why |
|---|---|---|
| Checks | Read and write | publishes the `maintainer-gate` check the rulesets require |
| Contents | Read and write | reads trees and commits; pushes rollback branches |
| Issues | Read and write | canary records, incidents, the ledger |
| Pull requests | Read and write | verdict reviews, labels, merges |
| Actions | Read | run and job metadata, the evidence the deploy guard trusts |
| Metadata | Read | mandatory |

Then: **Generate a private key** (a `.pem` downloads, keep it out of any
repository), note the **App ID**, and **Install App** on all three
repositories.

`docs/gate-app-manifest.json` carries the same field values in the shape
GitHub's App-manifest flow expects, if you prefer to create it that way.

## 2. Install the Claude App

Install <https://github.com/apps/claude> on all three repositories. The
reviewer step runs as this App and never receives the gate token.

## 3. Mint a Claude token

Run `claude setup-token` and keep the value. It is the reviewer's subscription
credential, and the only Anthropic credential anywhere in this system.

## 4. Environments, secrets, labels, ruleset

```bash
export GATE_APP_ID=<the App ID from step 1>
export GATE_APP_PRIVATE_KEY_FILE=/path/to/omni-maintainer-gate.private-key.pem
export CLAUDE_CODE_OAUTH_TOKEN=<the token from step 3>
scripts/setup-phase-0.sh
```

It is idempotent, prints every change, and refuses to write a secret into a
repository whose `gate` environment is not restricted to `main`. Pass
`--dry-run` to see the plan first.

What it does, and why each part matters:

- Creates the `gate` environment on the two **public** repositories with a
  deployment-branch policy of `main` only. `omni-reviewbot` is private in a
  Free organization, where environments have no branch policies, so it gets
  none and no gate workflow. Its gate runs from here instead.
- Sets `GATE_APP_ID`, `GATE_APP_PRIVATE_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` as
  **environment** secrets, not repository secrets. A repository secret would
  be readable by any workflow, including one a pull request head brings.
- Creates the maintainer labels in all three repositories.
- Creates the `InferMatrixCopilot` ruleset: pull request required, `suite`
  from the GitHub Actions integration and `maintainer-gate` from the gate App
  both required, branches must be up to date, no bypass actors. It looks the
  App's integration id up by slug, so a same-named check from another app
  cannot satisfy it.

## 5. Merge the three carve-out pull requests

They change `.github/workflows/**` and `deploy/**`, which are permanent
carve-outs: automation may never merge them, by design.

1. `JiusiServe/omni-reviewbot` #33 — the pin file, the push-only deploy
   predicate, the `canary-record` job and the deploy guard.
2. `JiusiServe/InferMatrixCopilot` — `maintainer-gate.yml`, pinned to a
   merged commit of this repository.
3. Anything this repository has open against its own `main`.

## 6. Turn the workflows on

The three workflows here ship disabled, so nothing runs against a
half-configured system:

```bash
for w in maintainer-gate.yml maintainer-gate-rb.yml maintainer-merge.yml; do
  gh workflow enable "$w" --repo JiusiServe/omni-maintainer
done
```

## 7. Check it took

```bash
scripts/setup-phase-0.sh --verify
```

It re-reads the branch policy, the secret names, the labels and the ruleset,
and fails on anything missing. Then open a throwaway pull request in
`InferMatrixCopilot` and confirm a `maintainer-gate` check appears on it
authored by `omni-maintainer-gate[bot]`. That check, from that App, is the
whole enforcement model in one observation.

## What stays manual forever

`omni-reviewbot` is private in a Free organization, so it cannot have a
ruleset and nothing can bind a token holder there. Every `omni-reviewbot`
merge is a human click, including pin bumps and rollbacks. The maintainer
prepares them and says so in the ledger; you merge them. Making the
repository public, or moving the organization to Team, is the only thing that
changes this.
