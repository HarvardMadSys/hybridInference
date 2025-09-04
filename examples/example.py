import asyncio
import sys
import os

# Add project root to Python path when running as script
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from serving.config import get_config


async def main():
    # Load configuration for server base URL (OpenRouter-compatible server)
    _ = get_config()  # Environment side-effects if any
    base_url = os.getenv("SERVER_BASE_URL", "http://localhost:8080")

    payload = {
        "model": "llama-3.3-70b-instruct",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"},
        ],
        "max_tokens": 50,
        "temperature": 0.7,
    }

    resp = requests.post(f"{base_url.rstrip('/')}/v1/chat/completions", json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    print(data["choices"][0]["message"]["content"])


if __name__ == "__main__":
    asyncio.run(main())
