#!/usr/bin/env bash
# ops/local_deployment_proxy/local_deployment_service.sh — start/stop the local deployment proxy daemon.
#
# Usage:
#   ./local_deployment_proxy/local_deployment_service.sh start   # launch proxy in background
#   ./local_deployment_proxy/local_deployment_service.sh stop    # kill proxy + stop container
#   ./local_deployment_proxy/local_deployment_service.sh status  # check if proxy is running
#
# The proxy itself is a thin Python HTTP server that:
#   • Listens on LISTEN_PORT (default 8001) — always open.
#   • Lazily starts the sglang Docker container on the first request.
#     (Backend binds to BACKEND_PORT=18001 internally, proxy forwards :8001 → :18001.)
#   • Stops the container after IDLE_TIMEOUT seconds of no traffic (default 1440 = 24 min).
#
# Override any env var before calling, e.g.:
#   LISTEN_PORT=9000 IDLE_TIMEOUT=600 ./local_deployment_proxy/local_deployment_service.sh start
#
# SSH reverse tunnel (expose to public LLM routers):
#   SSH_HOST=router.example.com REMOTE_PORT=8001 ./local_deployment_proxy/local_deployment_service.sh start
# This forwards router.example.com:REMOTE_PORT → localhost:LISTEN_PORT.
# Tunnels use autossh when available so they auto-reconnect after a drop or a
# router reboot; without autossh they fall back to plain ssh (no auto-recover)
# and the script prints a warning. Install autossh for durable tunnels.
#
# Multiple hosts (pipe-separated):
#   SSH_HOST="r1.example.com|r2.example.com" REMOTE_PORT=8001 ./local_deployment_proxy/local_deployment_service.sh start
# This opens two tunnels: r1.example.com:8001 → localhost and r2.example.com:8001 → localhost.
#
# Remote bind address (default 0.0.0.0 — bind all interfaces on remote so Docker
# containers on the remote can reach the forwarded port via host.docker.internal).
# Requires sshd_config "GatewayPorts clientspecified" (or "yes") on the remote.
# Override to "localhost" to restore default loopback-only behavior, or pin to a
# specific bridge IP, e.g. REMOTE_BIND=172.17.0.1.
#   REMOTE_BIND=172.17.0.1 SSH_HOST=router.example.com REMOTE_PORT=8001 \
#       ./local_deployment_proxy/local_deployment_service.sh start

set -euo pipefail

LISTEN_PORT="${LISTEN_PORT:-8001}"
REMOTE_PORT="${REMOTE_PORT:-}"
REMOTE_BIND="${REMOTE_BIND:-0.0.0.0}"
SSH_HOST="${SSH_HOST:-}"
PID_FILE="/tmp/local_deployment_proxy_${LISTEN_PORT}.pid"
TUNNEL_PID_FILE="/tmp/local_deployment_proxy_tunnel_${LISTEN_PORT}.pid"
LOG_FILE="/tmp/local_deployment_proxy_${LISTEN_PORT}.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_SCRIPT="${SCRIPT_DIR}/local_deployment_proxy.py"
MODELS_JSON="${SCRIPT_DIR}/models.json"

cmd="${1:-}"

case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Proxy already running (PID $(cat "$PID_FILE"))."
      exit 0
    fi
    echo "Starting local deployment proxy on :${LISTEN_PORT} …"
    echo "  Log: ${LOG_FILE}"
    nohup python3 "$PROXY_SCRIPT" >"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 1
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Proxy started (PID $(cat "$PID_FILE"))."
    else
      echo "ERROR: proxy exited immediately. Check ${LOG_FILE}" >&2
      exit 1
    fi
    # ── SSH reverse tunnel(s) ─────────────────────────────────────────────────
    if [[ -n "$SSH_HOST" && -n "$REMOTE_PORT" ]]; then
      # Prefer autossh so a tunnel that drops (router reboot, network blip)
      # reconnects on its own. Plain `ssh -R` does NOT recover: once the link
      # dies the process exits and ${REMOTE_PORT} on the router is left with no
      # listener until someone reruns this script — which is exactly how port
      # 8001 went dark after a gateway reboot. autossh respawns ssh whenever it
      # exits; with -M 0 it relies on ServerAliveInterval/CountMax (below) plus
      # ExitOnForwardFailure to detect a dead link, and AUTOSSH_GATETIME=0 keeps
      # it retrying even when the very first dial fails (e.g. router still
      # booting).
      if command -v autossh >/dev/null 2>&1; then
        export AUTOSSH_GATETIME=0
        TUNNEL_BIN=(autossh -M 0)
        echo "Using autossh for self-healing reverse tunnel(s)."
      else
        TUNNEL_BIN=(ssh)
        echo "WARNING: autossh not found — falling back to plain ssh. Tunnels will" >&2
        echo "         NOT auto-reconnect after a drop/reboot. Install autossh" >&2
        echo "         (e.g. 'apt-get install autossh') to make them durable." >&2
      fi
      IFS='|' read -ra HOSTS <<< "$SSH_HOST"
      TUNNEL_PIDS=()
      TUNNEL_EXIT=0
      for host in "${HOSTS[@]}"; do
        host="${host// /}"
        [[ -z "$host" ]] && continue
        echo "Opening reverse tunnel: ${host}:${REMOTE_BIND}:${REMOTE_PORT} → localhost:${LISTEN_PORT}"
        "${TUNNEL_BIN[@]}" -N \
          -R "${REMOTE_BIND}:${REMOTE_PORT}:localhost:${LISTEN_PORT}" \
          -o ServerAliveInterval=30 \
          -o ServerAliveCountMax=3 \
          -o ExitOnForwardFailure=yes \
          "$host" &
        TUNNEL_PID=$!
        # Give the tunnel a moment to fail fast (bad host key, bad auth). With
        # autossh the supervisor stays up and keeps retrying, so a live PID here
        # means "supervised", not necessarily "already connected".
        sleep 1
        if kill -0 "$TUNNEL_PID" 2>/dev/null; then
          TUNNEL_PIDS+=("$TUNNEL_PID")
          echo "Tunnel up (PID ${TUNNEL_PID}).  ${host}:${REMOTE_BIND}:${REMOTE_PORT} → localhost:${LISTEN_PORT}"
        else
          echo "WARNING: tunnel to ${host} may have failed. Check SSH access." >&2
          TUNNEL_EXIT=1
        fi
      done
      # Write all tunnel PIDs (joined by newlines).
      if [[ ${#TUNNEL_PIDS[@]} -gt 0 ]]; then
        printf "%s\n" "${TUNNEL_PIDS[@]}" > "$TUNNEL_PID_FILE"
      fi
      [[ $TUNNEL_EXIT -ne 0 ]] && exit 1
    fi
    ;;
  stop)
    # Stop SSH tunnels
    if [[ -f "$TUNNEL_PID_FILE" ]]; then
      echo "Stopping tunnels …"
      while read -r tpid; do
        [[ -z "$tpid" ]] && continue
        kill -0 "$tpid" 2>/dev/null && kill "$tpid" 2>/dev/null || true
      done < "$TUNNEL_PID_FILE"
      rm -f "$TUNNEL_PID_FILE"
    fi
    # Stop proxy
    if [[ -f "$PID_FILE" ]]; then
      PID=$(cat "$PID_FILE")
      if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping proxy (PID ${PID}) …"
        kill "$PID"
        # Stop all model containers from config.
        if [[ -f "$MODELS_JSON" ]]; then
          for ctr in $(python3 -c "import json; [print(v['container']) for v in json.load(open('$MODELS_JSON')).values()]"); do
            sudo docker rm -f "$ctr" >/dev/null 2>&1 || true
          done
        fi
        echo "Proxy stopped."
      else
        echo "Proxy not running (stale PID file)."
      fi
      rm -f "$PID_FILE"
    else
      echo "No PID file found — proxy not running."
      if [[ -f "$MODELS_JSON" ]]; then
        for ctr in $(python3 -c "import json; [print(v['container']) for v in json.load(open('$MODELS_JSON')).values()]"); do
          sudo docker rm -f "$ctr" >/dev/null 2>&1 || true
        done
      fi
    fi
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Proxy running (PID $(cat "$PID_FILE")).  Port :${LISTEN_PORT}"
    else
      echo "Proxy not running."
    fi
    # Show tunnel status for every configured host
    if [[ -n "$SSH_HOST" && -n "$REMOTE_PORT" ]]; then
      IFS='|' read -ra HOSTS <<< "$SSH_HOST"
      # Read tunnel PIDs into an indexed array (preserving order).
      TUNNEL_PIDS=()
      if [[ -f "$TUNNEL_PID_FILE" ]] && [[ -s "$TUNNEL_PID_FILE" ]]; then
        while read -r line; do
          [[ -n "$line" ]] && TUNNEL_PIDS+=("$line")
        done < "$TUNNEL_PID_FILE"
      fi
      idx=0
      for host in "${HOSTS[@]}"; do
        host="${host// /}"
        [[ -z "$host" ]] && continue
        if [[ $idx -lt ${#TUNNEL_PIDS[@]} ]]; then
          tpid="${TUNNEL_PIDS[$idx]}"
          if kill -0 "$tpid" 2>/dev/null; then
            echo "Tunnel running (PID ${tpid}).  ${host}:${REMOTE_BIND}:${REMOTE_PORT} → localhost:${LISTEN_PORT}"
          else
            echo "Tunnel NOT running (stale PID ${tpid}) for ${host}:${REMOTE_BIND}:${REMOTE_PORT} → localhost:${LISTEN_PORT}."
          fi
        else
          echo "Tunnel NOT running (expected ${host}:${REMOTE_BIND}:${REMOTE_PORT} → localhost:${LISTEN_PORT})."
        fi
        ((idx++))
      done
    elif [[ -f "$TUNNEL_PID_FILE" ]]; then
      tpid=$(cat "$TUNNEL_PID_FILE")
      if kill -0 "$tpid" 2>/dev/null; then
        echo "Tunnel running (PID ${tpid})."
      else
        echo "Tunnel NOT running (stale PID file)."
      fi
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
