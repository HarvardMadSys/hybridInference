#!/usr/bin/env bash
# ops/lib/systemd_local_api_key.sh — deliver LOCAL_API_KEY to an idle-proxy unit.
#
# Sourced by the three proxy installers (ops/local_deployment_proxy/install.sh,
# ops/h200_idle_proxy/install.sh, ops/spark_idle_proxy/install_service.sh). All
# four local proxy routes — local_deployment (8001), spark (8002) and the two
# H200 nodes (8003/8004) — authenticate against the same LOCAL_API_KEY that the
# gateway signs its requests with, so the key has to reach every one of them: a
# rotation that misses one turns into a silent 100% 401 rate on that route, and
# for the models that carry a remote fallback, into paid traffic nobody ordered.
#
# The units read ${REPO_ROOT}/.env, which covers a box that also hosts the
# gateway. A box that runs only a proxy and its tunnel has no .env, and this is
# where the key reaches it: a mode-0600 drop-in written as Environment=, which
# systemd deliberately ranks *below* an EnvironmentFile= (systemd.exec(5):
# "Settings from these files override settings made with Environment="). So .env
# stays the single source of truth wherever it exists, and this is the fallback
# for the boxes without one — a rotation in .env never has to be chased into
# /etc/systemd/system.
#
# This file only defines functions; it starts and stops nothing. Callers restart
# the unit themselves, since systemd does not re-read a drop-in on its own — which
# is also why an installer re-run must not quietly delete the key: the restart
# would put the deletion straight into the live process.

# Write (or remove) the LOCAL_API_KEY drop-in for one proxy unit.
#
#   write_local_api_key_dropin <systemd_dir> <unit_name> [key]
#
# Three states, and the third argument's *presence* is what separates the last
# two — which is why the key is an argument here instead of being read from the
# environment inside:
#
#   • a non-empty key  → write the drop-in
#   • an empty key     → remove the drop-in (an explicit "clear this")
#   • no key argument  → leave whatever is installed alone
#
# The last one matters because a proxy installer is re-run for reasons that have
# nothing to do with the key: a new tunnel host, a different port, a fresh
# checkout. A caller that collapsed "unset" into "empty" would delete the key on
# every such run, and on exactly the boxes this drop-in exists for — the ones with
# no repo .env — that drops the proxy back to the default hardcoded in its source
# while the gateway keeps signing with the rotated one: a 100% 401 rate on that
# route. Clearing a key is therefore something a caller has to ask for
# (LOCAL_API_KEY= on the command line), and the uninstallers remove the drop-in
# outright.
#
# Returns non-zero on a key systemd could not carry verbatim.
write_local_api_key_dropin() {
  local systemd_dir="$1" unit="$2"
  local dropin_dir="${systemd_dir}/${unit}.d"
  local dropin="${dropin_dir}/local-api-key.conf"

  if [[ "$#" -lt 3 ]]; then
    if [[ -f "$dropin" ]]; then
      echo "Keeping the existing ${unit} LOCAL_API_KEY drop-in (no key passed)."
      echo "  Pass LOCAL_API_KEY='…' to replace it, or LOCAL_API_KEY= to remove it."
    fi
    return 0
  fi

  local key="$3"

  if [[ -z "$key" ]]; then
    if [[ -f "$dropin" ]]; then
      echo "Removing ${unit} LOCAL_API_KEY drop-in (LOCAL_API_KEY is empty) …"
      rm -f "$dropin"
    fi
    return 0
  fi

  # systemd unquotes the value per systemd.syntax(7), so a literal double quote
  # or backslash would not survive into the key. A newline would end the
  # directive and turn the rest of the key into a second one. Refuse rather than
  # mis-set the key, because a mis-set key is the failure this exists to prevent.
  if [[ "$key" == *[\"\\]* || "$key" == *$'\n'* ]]; then
    echo "ERROR: LOCAL_API_KEY contains a double quote, backslash or newline," >&2
    echo "       which systemd would not pass through verbatim. Use a key" >&2
    echo "       without them." >&2
    return 1
  fi

  # Environment= is specifier-expanded (systemd.exec(5): "Specifier expansion is
  # performed"), and systemd.unit(5) requires '%%' for a literal percent. Left
  # alone, a '%h' in the key would be substituted with a path, and a '%' followed
  # by no known specifier would make the assignment invalid and drop it —
  # leaving the proxy on its hardcoded default, 401ing every request.
  local escaped="${key//%/%%}"

  echo "Writing ${unit} LOCAL_API_KEY drop-in …"
  mkdir -p "$dropin_dir"
  # Create it unreadable before the key goes in, not after: chmod-ing a file that
  # already holds the key leaves a world-readable window, however short.
  install -m 0600 /dev/null "$dropin"
  printf '[Service]\nEnvironment="LOCAL_API_KEY=%s"\n' "$escaped" >"$dropin"
}
