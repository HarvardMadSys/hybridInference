#!/usr/bin/env bash
cd "$(dirname "$0")"

# SSH_HOST='internal.freeinference.org|spark2' REMOTE_PORT=8002 ./spark_idle_service.sh start

SSH_HOST='internal.freeinference.org|spark2' REMOTE_PORT=8002 ./spark_idle_service.sh stop
SSH_HOST='internal.freeinference.org|spark2' REMOTE_PORT=8002 ./spark_idle_service.sh start

curl http://127.0.0.1:8002/v1/models \
  -H "Authorization: Bearer freeinference_api"

curl http://127.0.0.1:8002/v1/chat/completions \
  -H "Authorization: Bearer freeinference_api" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-oss-20b",
    "stream": true,
    "messages": [
      {
        "role": "system",
        "content": "You are a precise assistant."
      },
      {
        "role": "user",
        "content": "Hello."
      }
    ]
  }'
