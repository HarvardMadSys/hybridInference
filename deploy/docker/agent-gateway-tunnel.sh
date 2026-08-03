#!/bin/sh
# One encrypted hop to the gateway, and nothing else.
#
# A sandbox runs untrusted repository code on a network declared `internal:
# true` — no route off the host at all. That works because the gateway is
# normally a container on that same network. On a machine that has no gateway,
# it works because this does: one container on both the closed network and a
# routable one, forwarding a single port to a single destination.
#
# It is deliberately not a proxy. There is no CONNECT, no request routing, no
# destination the caller can choose: the address is fixed at startup, so the
# sandbox's reachable world stays exactly one endpoint — the same guarantee it
# has when the gateway is local.
#
# SSH rather than plain TCP because the hop crosses a shared network and the
# traffic is not incidental: every claim carries the dispatcher credential, and
# every job call carries a capability token for that job. Those must not cross
# a lab LAN in the clear, and it lets the gateway keep listening on loopback.
set -eu

KEY_SRC="${TUNNEL_SSH_KEY:-/etc/agent-tunnel/id}"
KNOWN_HOSTS="${TUNNEL_SSH_KNOWN_HOSTS:-/etc/agent-tunnel/known_hosts}"
LISTEN_PORT="${TUNNEL_LISTEN_PORT:-8080}"
REMOTE_HOST="${TUNNEL_REMOTE_HOST:-127.0.0.1}"
REMOTE_PORT="${TUNNEL_REMOTE_PORT:-8080}"
SSH_PORT="${TUNNEL_SSH_PORT:-22}"

die() { printf '[agent-gateway-tunnel] ERROR: %s\n' "$*" >&2; exit 1; }

[ -n "${TUNNEL_SSH_DESTINATION:-}" ] || die \
    "TUNNEL_SSH_DESTINATION is required (user@gateway-host)"
[ -r "$KEY_SRC" ] || die "no readable ssh key at $KEY_SRC (mount one read-only)"

# Host verification is not optional here. Turning it off would hand the
# credentials this tunnel exists to protect to whatever answers on that
# address — the exact attack encrypting the hop is meant to prevent.
[ -s "$KNOWN_HOSTS" ] || die \
    "no known_hosts at $KNOWN_HOSTS. Generate it on the runner host with
     'ssh-keyscan -p $SSH_PORT <gateway-host>', check the fingerprint, and mount
     it. This is not disabled by an option: an unverified hop would leak the
     dispatcher credential to anything that answers."

# ssh refuses a key others can read, and a bind mount keeps the host's mode.
install -m 600 "$KEY_SRC" /tmp/tunnel-key

printf '[agent-gateway-tunnel] %s -> %s:%s via %s\n' \
    "0.0.0.0:${LISTEN_PORT}" "$REMOTE_HOST" "$REMOTE_PORT" "$TUNNEL_SSH_DESTINATION"

# ExitOnForwardFailure: without it ssh stays up having bound nothing, and every
# sandbox gets connection-refused against a container that looks healthy.
# Dying instead lets the restart policy be the supervisor.
exec ssh -N \
    -i /tmp/tunnel-key \
    -p "$SSH_PORT" \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 \
    -o TCPKeepAlive=yes \
    -L "0.0.0.0:${LISTEN_PORT}:${REMOTE_HOST}:${REMOTE_PORT}" \
    "$TUNNEL_SSH_DESTINATION"
