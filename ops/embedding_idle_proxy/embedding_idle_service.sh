#!/usr/bin/env bash
# embedding_idle_proxy/embedding_idle_service.sh — start/stop the embedding idle proxy.
#
# Thin wrapper around the shared idle-proxy server (ops/sglang_idle_proxy/
# sglang_idle_proxy.py) configured for embedding models. It runs on its own
# port with its own models.json so it can coexist with the chat proxy.
#
# Usage:
#   ./embedding_idle_proxy/embedding_idle_service.sh start   # launch proxy in background
#   ./embedding_idle_proxy/embedding_idle_service.sh stop    # kill proxy + stop containers
#   ./embedding_idle_proxy/embedding_idle_service.sh status  # check if proxy is running
#
# The proxy:
#   • Listens on LISTEN_PORT (default 8002) — always open.
#   • Lazily starts an sglang container (--is-embedding) per model on first request.
#   • Stops a container after IDLE_TIMEOUT seconds of no traffic (default 1200 = 20 min).
#
# Override any env var before calling, e.g.:
#   LISTEN_PORT=9002 IDLE_TIMEOUT=600 ./embedding_idle_proxy/embedding_idle_service.sh start
#
# SSH reverse tunnel (expose to the public LLM router / production):
#   SSH_HOST=internal.freeinference.org REMOTE_PORT=8002 \
#       ./embedding_idle_proxy/embedding_idle_service.sh start
# This forwards <host>:REMOTE_PORT → localhost:LISTEN_PORT.
#
# Multiple hosts (pipe-separated):
#   SSH_HOST="r1.example.com|r2.example.com" REMOTE_PORT=8002 \
#       ./embedding_idle_proxy/embedding_idle_service.sh start
#
# Remote bind address (default 0.0.0.0 — bind all interfaces on remote so Docker
# containers on the remote can reach the forwarded port via host.docker.internal).
# Requires sshd_config "GatewayPorts clientspecified" (or "yes") on the remote.
#   REMOTE_BIND=172.17.0.1 SSH_HOST=router.example.com REMOTE_PORT=8002 \
#       ./embedding_idle_proxy/embedding_idle_service.sh start

set -euo pipefail

LISTEN_PORT="${LISTEN_PORT:-8002}"
REMOTE_PORT="${REMOTE_PORT:-}"
REMOTE_BIND="${REMOTE_BIND:-0.0.0.0}"
SSH_HOST="${SSH_HOST:-}"
PID_FILE="/tmp/embedding_idle_proxy_${LISTEN_PORT}.pid"
TUNNEL_PID_FILE="/tmp/embedding_idle_proxy_tunnel_${LISTEN_PORT}.pid"
LOG_FILE="/tmp/embedding_idle_proxy_${LISTEN_PORT}.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Reuse the shared idle-proxy server from the sibling sglang_idle_proxy dir.
PROXY_SCRIPT="${SCRIPT_DIR}/../sglang_idle_proxy/sglang_idle_proxy.py"
MODELS_JSON="${SCRIPT_DIR}/models.json"

# Point the shared server at this dir's models.json.
export MODELS_CONFIG="$MODELS_JSON"
export LISTEN_PORT

cmd="${1:-}"

case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Proxy already running (PID $(cat "$PID_FILE"))."
      exit 0
    fi
    echo "Starting embedding idle proxy on :${LISTEN_PORT} …"
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
      IFS='|' read -ra HOSTS <<< "$SSH_HOST"
      TUNNEL_PIDS=()
      TUNNEL_EXIT=0
      for host in "${HOSTS[@]}"; do
        host="${host// /}"
        [[ -z "$host" ]] && continue
        echo "Opening reverse tunnel: ${host}:${REMOTE_BIND}:${REMOTE_PORT} → localhost:${LISTEN_PORT}"
        ssh -N \
          -R "${REMOTE_BIND}:${REMOTE_PORT}:localhost:${LISTEN_PORT}" \
          -o ServerAliveInterval=30 \
          -o ServerAliveCountMax=3 \
          -o ExitOnForwardFailure=yes \
          "$host" &
        TUNNEL_PID=$!
        # Give ssh a moment to fail fast (bad host key, bad auth, port in use).
        sleep 1
        if kill -0 "$TUNNEL_PID" 2>/dev/null; then
          TUNNEL_PIDS+=("$TUNNEL_PID")
          echo "Tunnel established (PID ${TUNNEL_PID}).  ${host}:${REMOTE_BIND}:${REMOTE_PORT} → localhost:${LISTEN_PORT}"
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
