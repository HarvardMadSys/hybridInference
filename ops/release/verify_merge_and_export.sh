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
  drop-orphaned-trace-fixtures
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
import ast
from pathlib import Path

# Locate by AST, not by scanning for the next "\ndef ". A textual scan does not
# match "async def" or a decorator line, so it deletes past the end of the
# target and swallows whatever follows -- which is how three of dev's async
# alert-control-plane tests were silently removed here, leaving a suite that
# passed because the tests were gone.
p = Path("tests/unit/observability/test_alert_rules.py")
lines = p.read_text().splitlines(keepends=True)
targets = {"test_alerts_yaml_loads_with_pending_prefix_cache_leak",
           "test_alerts_yaml_loads_with_tracked_task_failure_rate"}
drop = set()
for node in ast.parse("".join(lines)).body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in targets:
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        drop.update(range(first, node.end_lineno + 1))
kept = "".join(l for n, l in enumerate(lines, 1) if n not in drop)
p.write_text(kept.replace("\n\n\n\n", "\n\n\n"))
PY
  # Removing a function leaves blank-line spacing ruff will not accept, and
  # `make lint` below checks formatting.
  uv run ruff format -q tests/unit/observability/test_alert_rules.py
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

say "Merged tree: no test disappeared unaccounted for"
# A conflict resolution that deletes too much leaves a suite that passes
# because the cases are gone. Every test name on dev that is absent after the
# merges has to be one a branch deliberately removed.
python3 - "$SCRATCH" <<'PY'
import re, subprocess, sys
from pathlib import Path

work = Path(sys.argv[1])
names = lambda text: set(re.findall(r"^\s*(?:async )?def (test_\w+)", text, re.M))


def at(ref):
    out = set()
    files = subprocess.run(["git", "-C", work, "ls-tree", "-r", "--name-only", ref, "tests/"],
                           capture_output=True, text=True, check=True).stdout.split()
    for f in files:
        if f.endswith(".py"):
            blob = subprocess.run(["git", "-C", work, "show", f"{ref}:{f}"],
                                  capture_output=True, text=True, check=True).stdout
            out |= names(blob)
    return out


now = set()
for f in (work / "tests").rglob("*.py"):
    now |= names(f.read_text())

# What each branch removes on purpose, and why. Anything else is a bug in a
# resolution, not a deletion.
_ALERTS = "#1060 moved the alerts config into the overlay, so there is no root copy left to load"
_LOCAL_ENV = ("#1060/#1085 delete the local-deployment env test -- it asserts against this "
              "deployment's model registry and routing map, both now in the overlay")
_MODELS = "#1085 moved the model registry into the overlay; this one read the root copy"
_RAG = ("#1055 replaces it with four tests that resolve the index inside the overlay "
        "(test_index_path_resolves_to_the_overlay_in_this_checkout and friends)")

EXPECTED = {
    "test_alerts_yaml_loads_with_pending_prefix_cache_leak": _ALERTS,
    "test_alerts_yaml_loads_with_tracked_task_failure_rate": _ALERTS,
    "test_deepseek_v4_flash_has_optional_h200_sglang_route": _LOCAL_ENV,
    "test_minimax_fast_lists_routewise_options_in_comments": _LOCAL_ENV,
    "test_minimax_fast_uses_routewise": _LOCAL_ENV,
    "test_routing_h200_local_deployment_lists_deepseek_v4_flash": _LOCAL_ENV,
    "test_routing_local_deployment_uses_local_deployment_url": _LOCAL_ENV,
    "test_sglang_local_route_uses_local_deployment_url": _LOCAL_ENV,
    "test_spark_route_uses_spark_deployment_url": _LOCAL_ENV,
    "test_all_zai_route_entries_text_only_modalities": _MODELS,
    "test_production_models_yaml_registers_full_inventory": _MODELS,
    "test_production_models_yaml_schema_contract": _MODELS,
    "test_index_path_is_package_relative": _RAG,
}
gone = at("origin/dev") - now
unexpected = sorted(gone - set(EXPECTED))
for n in sorted(gone & set(EXPECTED)):
    print(f"  removed on purpose: {n}  ({EXPECTED[n]})")
if unexpected:
    print("\nThese tests exist on dev and not in the merged tree, and nothing claims to have removed them:")
    for n in unexpected:
        print(f"  {n}")
    print("\nA resolution deleted more than it meant to. The suite would pass anyway.")
    raise SystemExit(1)
PY

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
