#!/usr/bin/env bash
# Phase 0, steps 4 to 7: environments, secrets, labels, ruleset. See docs/phase-0.md.
#
# Idempotent, and safe to re-run. Three invariants it will not break:
#   - a secret is never written to a `gate` environment that is not restricted
#     to `main`, because such an environment is reachable from a pull-request
#     head and the gate key would stop being evidence of anything;
#   - a secret value never appears in an argument list or in this script's
#     output, dry run included: every value is piped on stdin;
#   - the ruleset binds each required check to the integration that must
#     produce it, never to its name, so a same-named check run created by any
#     other app does not satisfy it.
#
# Needs gh and python3. Nothing else: every filter is gh's own --jq.
set -euo pipefail

OWNER=JiusiServe
PUBLIC_REPOS=(omni-maintainer InferMatrixCopilot)
ALL_REPOS=(omni-maintainer InferMatrixCopilot omni-reviewbot)
RULESET_REPOS=(InferMatrixCopilot)     # omni-maintainer already has its ruleset
VERIFY_RULESET_REPOS=(InferMatrixCopilot omni-maintainer)
GATE_APP_SLUG=omni-maintainer-gate
ACTIONS_APP_SLUG=github-actions
WORKFLOWS=(maintainer-gate.yml maintainer-gate-rb.yml maintainer-merge.yml)
declare -A REQUIRED_CI=([InferMatrixCopilot]=suite [omni-maintainer]=tests)
LABELS=(
  "maintainer:filed|d4c5f9|Filed by the maintainer from production evidence"
  "maintainer:incident|b60205|Production incident; holds deploys until a human closes it"
  "maintainer:canary|fbca04|An open canary window for one deploy attempt"
  "maintainer:rollback|b60205|A prepared revert of a bad deploy"
  "maintainer:blocked|d93f0b|Needs a human decision before anything else happens"
  "maintainer:stale|ededed|Idle; closes on its own unless someone speaks up"
  "maintainer:proposed|c2e0c6|A proposal awaiting a human maintainer-go"
  "maintainer:merge-requested|0e8a16|The maintainer believes this meets the bar"
  "maintainer-go|0052cc|Human grant; only a human label event counts"
  "maintainer:paused|000000|On the ledger issue: stops every routine and the deploy guard"
)

DRY=0; VERIFY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --verify)  VERIFY=1 ;;
    *) echo "usage: $0 [--dry-run] [--verify]" >&2; exit 2 ;;
  esac
done

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then say "  would: $*"; else "$@"; fi; }
die()  { printf 'FAILED: %s\n' "$*" >&2; exit 1; }

# GitHub wants JSON booleans here; `gh api -f` would send the string "false"
# and the request would fail with 422, so every body is real JSON on stdin.
api_json() {
  local method=$1 path=$2 body=$3
  if [ "$DRY" = 1 ]; then say "  would: gh api -X $method $path with $body"; return; fi
  printf '%s' "$body" | gh api -X "$method" "$path" --input - >/dev/null
}

# The value arrives on stdin and is never an argument, so it cannot appear in
# this script's output, in a process listing, or in a shell history.
secret_set() {
  local name=$1 repo=$2
  if [ "$DRY" = 1 ]; then
    cat >/dev/null
    say "  would: gh secret set $name --env gate --repo $OWNER/$repo (value on stdin, not shown)"
    return
  fi
  gh secret set "$name" --env gate --repo "$OWNER/$repo"
}

app_id_of() { gh api "/apps/$1" --jq .id 2>/dev/null || die "no App with slug $1; create it first (docs/phase-0.md step 1)"; }

# --- environments -----------------------------------------------------------
# Restricted to main: only a run on the default branch can mint the gate token.
ensure_environment() {
  local repo=$1
  say "  $repo: gate environment, deployment branch policy = main only"
  api_json PUT "repos/$OWNER/$repo/environments/gate" \
    '{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
  local existing
  existing=$(gh api "repos/$OWNER/$repo/environments/gate/deployment-branch-policies" \
             --jq '.branch_policies[].name' 2>/dev/null || true)
  if ! printf '%s\n' "$existing" | grep -qx main; then
    api_json POST "repos/$OWNER/$repo/environments/gate/deployment-branch-policies" \
      '{"name":"main","type":"branch"}'
  fi
  local extra
  for extra in $(printf '%s\n' "$existing" | grep -vx main | grep -v '^$' || true); do
    say "  WARNING: $repo gate also allows branch '$extra'; remove it, or the gate key is reachable from that branch"
  done
}

policy_is_main_only() {
  local repo=$1 names
  names=$(gh api "repos/$OWNER/$repo/environments/gate/deployment-branch-policies" \
          --jq '[.branch_policies[].name] | sort | join(",")' 2>/dev/null || echo "")
  [ "$names" = "main" ]
}

set_secrets() {
  local repo=$1
  if ! policy_is_main_only "$repo"; then
    [ "$DRY" = 1 ] && { say "  would refuse: $repo gate is not main-only (it will be, once the step above runs for real)"; return 0; }
    die "$repo gate environment is not restricted to main; refusing to write the gate key into it"
  fi
  say "  $repo: GATE_APP_ID, GATE_APP_PRIVATE_KEY, CLAUDE_CODE_OAUTH_TOKEN (environment secrets)"
  printf '%s' "$GATE_APP_ID"             | secret_set GATE_APP_ID "$repo"
  secret_set GATE_APP_PRIVATE_KEY "$repo" < "$GATE_APP_PRIVATE_KEY_FILE"
  printf '%s' "$CLAUDE_CODE_OAUTH_TOKEN" | secret_set CLAUDE_CODE_OAUTH_TOKEN "$repo"
}

# --- labels -----------------------------------------------------------------
ensure_labels() {
  local repo=$1
  say "  $repo: ${#LABELS[@]} labels"
  local spec name colour desc
  for spec in "${LABELS[@]}"; do
    IFS='|' read -r name colour desc <<< "$spec"
    run gh label create "$name" --color "$colour" --description "$desc" \
        --repo "$OWNER/$repo" --force >/dev/null
  done
}

# --- ruleset ----------------------------------------------------------------
ruleset_payload() {
  GATE_ID=$1 ACTIONS_ID=$2 CI=$3 python3 - <<'PY'
import json, os
print(json.dumps({
  "name": "main protection",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": False,
      "require_code_owner_review": False,
      "require_last_push_approval": False,
      "required_review_thread_resolution": False}},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": True,
      "do_not_enforce_on_create": False,
      "required_status_checks": [
        {"context": os.environ["CI"], "integration_id": int(os.environ["ACTIONS_ID"])},
        {"context": "maintainer-gate", "integration_id": int(os.environ["GATE_ID"])}]}},
  ],
}))
PY
}

ruleset_id() {
  gh api "repos/$OWNER/$1/rulesets" --jq '.[] | select(.name=="main protection") | .id' 2>/dev/null | head -n1
}

ensure_ruleset() {
  local repo=$1 payload existing
  payload=$(ruleset_payload "$2" "$3" "${REQUIRED_CI[$repo]}")
  existing=$(ruleset_id "$repo")
  if [ -n "$existing" ]; then
    say "  $repo: updating ruleset $existing (${REQUIRED_CI[$repo]} + maintainer-gate, no bypass actors)"
    api_json PUT "repos/$OWNER/$repo/rulesets/$existing" "$payload"
  else
    say "  $repo: creating ruleset (${REQUIRED_CI[$repo]} + maintainer-gate, no bypass actors)"
    api_json POST "repos/$OWNER/$repo/rulesets" "$payload"
  fi
}

# --- verify -----------------------------------------------------------------
verify() {
  local bad=0 repo s w gate_id actions_id tmp
  gate_id=$(app_id_of "$GATE_APP_SLUG")
  actions_id=$(app_id_of "$ACTIONS_APP_SLUG")
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
  step "Verifying"
  for repo in "${PUBLIC_REPOS[@]}"; do
    if policy_is_main_only "$repo"; then say "  ok   $repo gate environment is main-only"
    else say "  FAIL $repo gate environment is not restricted to main"; bad=1; fi
    local have
    have=$(gh api "repos/$OWNER/$repo/environments/gate/secrets" --jq '[.secrets[].name] | join(" ")' 2>/dev/null || echo "")
    for s in CLAUDE_CODE_OAUTH_TOKEN GATE_APP_ID GATE_APP_PRIVATE_KEY; do
      case " $have " in *" $s "*) say "  ok   $repo has $s" ;; *) say "  FAIL $repo is missing $s"; bad=1 ;; esac
    done
  done
  for repo in "${ALL_REPOS[@]}"; do
    local names spec name missing=""
    names=$(gh label list --repo "$OWNER/$repo" --limit 200 --json name --jq '.[].name' 2>/dev/null || echo "")
    for spec in "${LABELS[@]}"; do
      IFS='|' read -r name _ _ <<< "$spec"
      printf '%s\n' "$names" | grep -qxF "$name" || missing="$missing $name"
    done
    if [ -z "$missing" ]; then say "  ok   $repo has every label"
    else say "  FAIL $repo is missing labels:$missing"; bad=1; fi
  done
  # Which repositories an App is installed on is readable with an App JWT, not
  # with this token, so the org listing is as far as a person can check from
  # here; a token without org-admin cannot even do that, and reports nothing
  # rather than a false failure. Step 7 finishes the job by observation.
  local installed
  installed=$(gh api "/orgs/$OWNER/installations" \
              --jq ".installations[] | select(.app_slug==\"$GATE_APP_SLUG\") | .repository_selection" 2>/dev/null || echo "")
  case "$installed" in
    "")         say "  note the gate App installation is not readable with this token; confirm it on all three repositories by hand" ;;
    all)        say "  ok   the gate App is installed on every repository in $OWNER" ;;
    *)          say "  note the gate App is installed on selected repositories; confirm all three are selected" ;;
  esac
  for repo in "${VERIFY_RULESET_REPOS[@]}"; do
    local rs
    rs=$(ruleset_id "$repo")
    if [ -z "$rs" ]; then say "  FAIL $repo has no 'main protection' ruleset"; bad=1; continue; fi
    gh api "repos/$OWNER/$repo/rulesets/$rs" > "$tmp/ruleset.json" 2>/dev/null || {
      say "  FAIL $repo ruleset $rs is not readable"; bad=1; continue; }
    REPO="$repo" CI="${REQUIRED_CI[$repo]}" GATE_ID="$gate_id" ACTIONS_ID="$actions_id" \
      python3 "$here/check-ruleset.py" < "$tmp/ruleset.json" || bad=1
  done
  for w in "${WORKFLOWS[@]}"; do
    local state
    state=$(gh api "repos/$OWNER/omni-maintainer/actions/workflows/$w" --jq .state 2>/dev/null || echo missing)
    if [ "$state" = "active" ]; then say "  ok   $w is enabled"
    else say "  FAIL $w is $state (enable it: gh workflow enable $w --repo $OWNER/omni-maintainer)"; bad=1; fi
  done
  [ "$bad" = 0 ] || die "Phase 0 is incomplete; fix the FAIL lines above and re-run --verify"
  say ""
  say "Phase 0 configuration is in place. Open a throwaway pull request in InferMatrixCopilot"
  say "and confirm a maintainer-gate check appears on it authored by $GATE_APP_SLUG[bot]."
}

command -v gh >/dev/null || die "gh is not on PATH"
command -v python3 >/dev/null || die "python3 is not on PATH"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated"

if [ "$VERIFY" = 1 ]; then verify; exit 0; fi

[ -n "${GATE_APP_ID:-}" ] || die "GATE_APP_ID is not set"
[ -n "${GATE_APP_PRIVATE_KEY_FILE:-}" ] || die "GATE_APP_PRIVATE_KEY_FILE is not set"
[ -r "${GATE_APP_PRIVATE_KEY_FILE}" ] || die "cannot read ${GATE_APP_PRIVATE_KEY_FILE}"
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || die "CLAUDE_CODE_OAUTH_TOKEN is not set (run: claude setup-token)"

GATE_ID=$(app_id_of "$GATE_APP_SLUG")
ACTIONS_ID=$(app_id_of "$ACTIONS_APP_SLUG")
say "gate App id $GATE_ID, GitHub Actions id $ACTIONS_ID"
[ "$DRY" = 1 ] && say "(dry run: nothing is written)"

step "Environments and secrets (public repositories only)"
for repo in "${PUBLIC_REPOS[@]}"; do ensure_environment "$repo"; set_secrets "$repo"; done
say "  omni-reviewbot: skipped, a private repository in a Free organization has no environment policies"

step "Labels"
for repo in "${ALL_REPOS[@]}"; do ensure_labels "$repo"; done

step "Rulesets"
for repo in "${RULESET_REPOS[@]}"; do ensure_ruleset "$repo" "$GATE_ID" "$ACTIONS_ID"; done
say "  omni-reviewbot: skipped, rulesets need a public repository or a paid plan"

say ""
say "Now merge the carve-out pull requests (docs/phase-0.md step 5), enable the three"
say "workflows (step 6), then run: $0 --verify"
