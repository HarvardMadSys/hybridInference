# SSH_HOST='internal.freeinference.org|spark2' REMOTE_PORT=8001 ./sglang_idle_service.sh start

SSH_HOST='internal.freeinference.org|spark2' REMOTE_PORT=8001 ./sglang_idle_service.sh stop
SSH_HOST='internal.freeinference.org|spark2' REMOTE_PORT=8001 ./sglang_idle_service.sh start

curl http://127.0.0.1:8001/v1/embeddings \
  -H "Authorization: Bearer freeinference_api" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "BAAI/bge-m3",
    "input": "What is retrieval-augmented generation?"
  }'


curl http://127.0.0.1:8001/v1/chat/completions \
  -H "Authorization: Bearer freeinference_api" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3.6-35B-A3B-FP8",
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


