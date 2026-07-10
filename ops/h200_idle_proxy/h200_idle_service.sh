#!/usr/bin/env bash
# ops/h200_idle_proxy/h200_idle_service.sh — start/stop the H200 idle proxy daemon.
#
# Reuses ops/local_deployment_proxy/local_deployment_proxy.py with this package's
# models.json (DeepSeek-V4-Flash FP8, TP=2 on GPUs 0+2 — GPU 1 left free).
#
# Usage:
#   ./ops/h200_idle_proxy/h200_idle_service.sh start
#   ./ops/h200_idle_proxy/h200_idle_service.sh stop
#   ./ops/h200_idle_proxy/h200_idle_service.sh status
#
# Reverse tunnels to staging (spark2) + prod (internal.freeinference.org):
#   SSH_HOST='spark2|jason@internal.freeinference.org' REMOTE_PORT=8003 \
#     ./ops/h200_idle_proxy/h200_idle_service.sh start

set -euo pipefail

LISTEN_PORT="${LISTEN_PORT:-8003}"
REMOTE_PORT="${REMOTE_PORT:-}"
REMOTE_BIND="${REMOTE_BIND:-0.0.0.0}"
SSH_HOST="${SSH_HOST:-}"
PID_FILE="/tmp/h200_idle_proxy_${LISTEN_PORT}.pid"
TUNNEL_PID_FILE="/tmp/h200_idle_proxy_tunnel_${LISTEN_PORT}.pid"
LOG_FILE="/tmp/h200_idle_proxy_${LISTEN_PORT}.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROXY_SCRIPT="${REPO_ROOT}/ops/local_deployment_proxy/local_deployment_proxy.py"
MODELS_JSON="${MODELS_CONFIG:-${SCRIPT_DIR}/models.json}"

export LISTEN_PORT
export MODELS_CONFIG="${MODELS_JSON}"
export IDLE_TIMEOUT="${IDLE_TIMEOUT:-1440}"
export HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-900}"
export HEALTH_INTERVAL="${HEALTH_INTERVAL:-10}"

cmd="${1:-}"

case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Proxy already running (PID $(cat "$PID_FILE"))."
      exit 0
    fi
    if [[ ! -f "$PROXY_SCRIPT" ]]; then
      echo "ERROR: proxy script not found: ${PROXY_SCRIPT}" >&2
      exit 1
    fi
    if [[ ! -f "$MODELS_JSON" ]]; then
      echo "ERROR: models config not found: ${MODELS_JSON}" >&2
      exit 1
    fi
    echo "Starting h200 idle proxy on :${LISTEN_PORT} (MODELS_CONFIG=${MODELS_JSON}) …"
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
    if [[ -n "$SSH_HOST" && -n "$REMOTE_PORT" ]]; then
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
        sleep 1
        if kill -0 "$TUNNEL_PID" 2>/dev/null; then
          TUNNEL_PIDS+=("$TUNNEL_PID")
          echo "Tunnel up (PID ${TUNNEL_PID}).  ${host}:${REMOTE_BIND}:${REMOTE_PORT} → localhost:${LISTEN_PORT}"
        else
          echo "WARNING: tunnel to ${host} may have failed. Check SSH access." >&2
          TUNNEL_EXIT=1
        fi
      done
      if [[ ${#TUNNEL_PIDS[@]} -gt 0 ]]; then
        printf "%s\n" "${TUNNEL_PIDS[@]}" > "$TUNNEL_PID_FILE"
      fi
      [[ $TUNNEL_EXIT -ne 0 ]] && exit 1
    fi
    ;;
  stop)
    if [[ -f "$TUNNEL_PID_FILE" ]]; then
      echo "Stopping tunnels …"
      while read -r tpid; do
        [[ -z "$tpid" ]] && continue
        kill -0 "$tpid" 2>/dev/null && kill "$tpid" 2>/dev/null || true
      done < "$TUNNEL_PID_FILE"
      rm -f "$TUNNEL_PID_FILE"
    fi
    if [[ -f "$PID_FILE" ]]; then
      PID=$(cat "$PID_FILE")
      if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping proxy (PID ${PID}) …"
        kill "$PID"
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
    if [[ -n "$SSH_HOST" && -n "$REMOTE_PORT" ]]; then
      IFS='|' read -ra HOSTS <<< "$SSH_HOST"
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
        ((idx++)) || true
      done
    elif [[ -f "$TUNNEL_PID_FILE" ]]; then
      while read -r tpid; do
        [[ -z "$tpid" ]] && continue
        if kill -0 "$tpid" 2>/dev/null; then
          echo "Tunnel running (PID ${tpid})."
        else
          echo "Tunnel NOT running (stale PID ${tpid})."
        fi
      done < "$TUNNEL_PID_FILE"
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
