#!/usr/bin/env bash
# spark_idle_service.sh — start/stop the vLLM idle proxy daemon on DGX Spark.
#
# Usage:
#   ./spark_idle_proxy/spark_idle_service.sh start
#   ./spark_idle_proxy/spark_idle_service.sh stop
#   ./spark_idle_proxy/spark_idle_service.sh status
#
# SSH reverse tunnel (expose to public LLM routers):
#   SSH_HOST='spark2|internal.freeinference.org' REMOTE_PORT=8002 ./spark_idle_proxy/spark_idle_service.sh start

set -euo pipefail

LISTEN_PORT="${LISTEN_PORT:-8002}"
REMOTE_PORT="${REMOTE_PORT:-}"
REMOTE_BIND="${REMOTE_BIND:-0.0.0.0}"
SSH_HOST="${SSH_HOST:-}"
PID_FILE="/tmp/spark_idle_proxy_${LISTEN_PORT}.pid"
TUNNEL_PID_FILE="/tmp/spark_idle_proxy_tunnel_${LISTEN_PORT}.pid"
LOG_FILE="/tmp/spark_idle_proxy_${LISTEN_PORT}.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_SCRIPT="${SCRIPT_DIR}/spark_idle_proxy.py"
MODELS_JSON="${SCRIPT_DIR}/models.json"

_kill_pid() {
  local pid="$1"
  [[ -z "$pid" ]] && return 0
  kill -0 "$pid" 2>/dev/null || return 0
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -9 "$pid" 2>/dev/null || true
}

_kill_orphan_tunnels() {
  local line pid cmd
  while read -r line; do
    [[ -z "$line" ]] && continue
    pid="${line%% *}"
    cmd="${line#* }"
    if [[ "$cmd" == *"ssh -N"* && "$cmd" == *"-R"*":${LISTEN_PORT}:localhost:${LISTEN_PORT}"* ]]; then
      if kill -0 "$pid" 2>/dev/null; then
        echo "Stopping SSH tunnel (PID ${pid}) …"
        _kill_pid "$pid"
      fi
    fi
  done < <(pgrep -af 'ssh -N' 2>/dev/null || true)
}

_kill_port_listener() {
  local pid
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    if ! kill -0 "$pid" 2>/dev/null; then
      continue
    fi
    echo "Stopping listener on :${LISTEN_PORT} (PID ${pid}) …"
    _kill_pid "$pid"
  done < <(ss -tlnp "sport = :${LISTEN_PORT}" 2>/dev/null \
    | rg -o 'pid=[0-9]+' | sed 's/pid=//' | sort -u || true)
}

cmd="${1:-}"

case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Proxy already running (PID $(cat "$PID_FILE"))."
      exit 0
    fi
    echo "Starting spark idle proxy on :${LISTEN_PORT} …"
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
        sleep 1
        if kill -0 "$TUNNEL_PID" 2>/dev/null; then
          TUNNEL_PIDS+=("$TUNNEL_PID")
          echo "Tunnel established (PID ${TUNNEL_PID}).  ${host}:${REMOTE_BIND}:${REMOTE_PORT} → localhost:${LISTEN_PORT}"
        else
          echo "WARNING: tunnel to ${host} may have failed. Check SSH access." >&2
          TUNNEL_EXIT=1
        fi
      done
      if [[ ${#TUNNEL_PIDS[@]} -gt 0 ]]; then
        printf "%s\n" "${TUNNEL_PIDS[@]}" > "$TUNNEL_PID_FILE"
      fi
      if [[ ${#TUNNEL_PIDS[@]} -eq 0 ]]; then
        exit 1
      fi
    fi
    ;;
  stop)
    _kill_orphan_tunnels
    if [[ -f "$TUNNEL_PID_FILE" ]]; then
      echo "Stopping tunnels …"
      while read -r tpid; do
        [[ -z "$tpid" ]] && continue
        kill -0 "$tpid" 2>/dev/null && _kill_pid "$tpid" || true
      done < "$TUNNEL_PID_FILE"
      rm -f "$TUNNEL_PID_FILE"
    fi
    if [[ -f "$PID_FILE" ]]; then
      PID=$(cat "$PID_FILE")
      if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping proxy (PID ${PID}) …"
        _kill_pid "$PID"
        _kill_port_listener
        if [[ -f "$MODELS_JSON" ]]; then
          for ctr in $(python3 -c "import json; [print(v['container']) for v in json.load(open('$MODELS_JSON')).values()]"); do
            sudo docker rm -f "$ctr" >/dev/null 2>&1 || true
          done
        fi
        echo "Proxy stopped."
      else
        echo "Proxy not running (stale PID file)."
        _kill_port_listener
      fi
      rm -f "$PID_FILE"
    else
      echo "No PID file found — proxy not running."
      _kill_port_listener
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
