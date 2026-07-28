#!/usr/bin/env bash
#
# Reproduce the pre-merge acceptance run.
#
# Every number quoted about "the merged tree" comes from here. Running it is
# how you check the claim instead of taking it; it builds a scratch worktree,
# merges the open branches in the documented order, applies the three conflict
# resolutions by hand — they are not "take both sides", and getting them wrong
# produces convincing failures — and then runs the gates twice: once on the
# merged tree, once inside the export materialised from it.
#
#   ops/release/verify_merge_and_export.sh [scratch-dir]
#
# Read-only with respect to this repository: it writes only into the scratch
# directory, and removes it on success unless KEEP=1.

set -Eeuo pipefail

SCRATCH="${1:-$(mktemp -d -t hi-verify-XXXXXX)}"
EXPORT_DIR="${SCRATCH}.export"
REPO_ROOT="$(git rev-parse --show-toplevel)"
KEEP="${KEEP:-0}"

# Order matters: #1060 lays the mechanism, #1085 is #1060 plus the registry.
BRANCHES=(
  backend-site-identity
  models-config-to-overlay
  openrouter-reference-registry
  frontend-neutral-defaults
  move-rag-index-to-overlay
  brand-residue-sweep
  neutral-db-less-signals
  export-runtime-overrides
  agent-guide-operator-split
  redact-committed-api-key
  scrub-user-emails
  ignore-untracked-private-notes
  generic-test-fixture-path
  public-export-manifest
  no-dangling-repo-paths
)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

cleanup() {
  local status=$?
  if [[ "$KEEP" == "1" ]]; then
    echo "Scratch kept at $SCRATCH (export at $EXPORT_DIR)"
  else
    git -C "$REPO_ROOT" worktree remove --force "$SCRATCH" 2>/dev/null || true
    rm -rf "$EXPORT_DIR"
  fi
  exit "$status"
}
trap cleanup EXIT

say "Building the merged tree in $SCRATCH"
git -C "$REPO_ROOT" fetch -q origin
git -C "$REPO_ROOT" worktree add -q --detach "$SCRATCH" origin/dev
cd "$SCRATCH"

# --- the three resolutions, written out because each has a wrong answer that
# --- looks right ---------------------------------------------------------

resolve_alert_rules() {
  # dev carries the alert-control-plane work; #1060 only moves two cases out.
  # Taking #1060's file whole drops the former and ten tests fail on
  # `alert_slack`.
  git checkout --ours tests/unit/observability/test_alert_rules.py
  python3 - <<'PY'
from pathlib import Path
p = Path("tests/unit/observability/test_alert_rules.py"); s = p.read_text()
for name in ("test_alerts_yaml_loads_with_pending_prefix_cache_leak",
             "test_alerts_yaml_loads_with_tracked_task_failure_rate"):
    if f"def {name}(" not in s:
        continue
    i = s.index(f"def {name}(")
    start = s.rfind("\n\n\n", 0, i); start = start + 1 if start != -1 else i
    nxt = s.find("\ndef ", i); end = nxt + 1 if nxt != -1 else len(s)
    s = s[:start] + s[end:]
p.write_text(s)
PY
}

resolve_route_registration() {
  # #1067 adds two cases *and* the MINIMAL_YAML they use; #1085 removes the two
  # inventory contracts. Stripping markers alone loses the constant and
  # resurrects the contracts.
  python3 - <<'PY'
from pathlib import Path
p = Path("tests/servers/test_contract_route_registration.py")
s = "\n".join(l for l in p.read_text().split("\n")
              if not l.startswith(("<<<<<<<", "=======", ">>>>>>>")))
for name in ("test_production_models_yaml_schema_contract",
             "test_production_models_yaml_registers_full_inventory"):
    if f"def {name}(" not in s:
        continue
    i = s.index(f"def {name}(")
    start = s.rfind("\n\n\n", 0, i); start = start + 1 if start != -1 else i
    nxt = s.find("\ndef ", i); end = nxt + 1 if nxt != -1 else len(s)
    s = s[:start] + s[end:]
if "MINIMAL_YAML = " not in s and "test_omitted_optional_fields" in s:
    const = (
        'MINIMAL_YAML = """\\\nmodels:\n  - id: minimal-model\n'
        '    name: Minimal Model\n    provider: openai_compat\n    route:\n'
        '      - kind: openai_compat\n'
        '        base_url: https://minimal.example.test/v1\n'
        '        api_key: sk-minimal\n"""\n\n\n'
    )
    s = s.replace("def test_omitted_optional_fields", const + "def test_omitted_optional_fields", 1)
p.write_text(s)
PY
}

strip_markers() {
  python3 - "$1" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
p.write_text("\n".join(l for l in p.read_text().split("\n")
                       if not l.startswith(("<<<<<<<", "=======", ">>>>>>>"))))
PY
}

for branch in "${BRANCHES[@]}"; do
  if ! git merge -q --no-edit "origin/murphy/claude/${branch}" >/dev/null 2>&1; then
    echo "  conflict at ${branch}: $(git diff --name-only --diff-filter=U | tr '\n' ' ')"
    for f in $(git diff --name-only --diff-filter=U); do
      case "$f" in
        *test_alert_rules.py)             resolve_alert_rules ;;
        *test_contract_route_registration.py) resolve_route_registration ;;
        *)                                strip_markers "$f" ;;
      esac
      git add "$f"
    done
    git commit -q --no-edit
  fi
done

# Merging two import blocks leaves them unsorted, and removing tests can strand
# an import. Neither is a conflict; both fail `make lint`.
uv run ruff check --fix apps/backend/serving/servers/auth.py >/dev/null 2>&1 || true
uv run ruff format apps/backend/serving/servers/auth.py \
  tests/unit/observability/test_alert_rules.py \
  tests/servers/test_contract_route_registration.py >/dev/null 2>&1 || true
uv run ruff check --fix tests/unit/observability/test_alert_rules.py >/dev/null 2>&1 || true

uv sync -q

say "Merged tree: lint"
make lint

say "Merged tree: tests"
make test

say "Export: materialise and audit"
rm -rf "$EXPORT_DIR"
uv run python ops/release/public_export.py --materialize "$EXPORT_DIR"

say "Export: backend suite"
(cd "$EXPORT_DIR" && PYTHONPATH=apps/backend "$SCRATCH/.venv/bin/python" \
  -m pytest -q -m "not external and not dbtest" -p no:randomly)

say "Export: frontend gates"
(cd "$EXPORT_DIR/apps/frontend" && npm ci --silent && npx tsc --noEmit && npx eslint src && npm test)

say "Acceptance run complete."
