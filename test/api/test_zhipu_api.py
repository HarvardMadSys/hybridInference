"""Test Zhipu AI GLM models API directly."""

import os

from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables from .env file
load_dotenv()

# Test 1: Using OpenAI-compatible interface
api_key = os.environ.get("ZAI_API_KEY")
if not api_key:
    print("Warning: ZAI_API_KEY not set in .env file")
    print("Please add: ZAI_API_KEY=your-api-key")
    exit(1)

# Zhipu uses OpenAI-compatible endpoint
client = OpenAI(api_key=api_key, base_url="https://open.bigmodel.cn/api/paas/v4/")

print("Testing GLM-4.5 model...")

# Test basic completion
try:
    completion = client.chat.completions.create(
        model="glm-4.5",
        messages=[
            {
                "role": "user",
                "content": "Hello! Please respond with 'Hi there' to confirm you're working.",
            },
        ],
        temperature=0.7,
        max_tokens=20,
    )
    print("✅ GLM-4.5 response:", completion.choices[0].message.content)
    print(f"   Model: {completion.model}")
    print(
        f"   Usage: prompt={completion.usage.prompt_tokens}, "
        f"completion={completion.usage.completion_tokens}, "
        f"total={completion.usage.total_tokens}"
    )
except Exception as e:
    print(f"❌ GLM-4.5 failed: {e}")

# Test streaming
print("\nTesting streaming...")
try:
    stream = client.chat.completions.create(
        model="glm-4.5",
        messages=[
            {"role": "user", "content": "Count from 1 to 5"},
        ],
        stream=True,
        max_tokens=50,
    )

    print("✅ Streaming response: ", end="")
    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="")
    print()
except Exception as e:
    print(f"❌ Streaming failed: {e}")

# Test with system message
print("\nTesting with system message...")
try:
    completion = client.chat.completions.create(
        model="glm-4.5",
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant. Always respond in Chinese.",
            },
            {"role": "user", "content": "What is the capital of France?"},
        ],
        temperature=0.7,
        max_tokens=50,
    )
    print("✅ Response with system message:", completion.choices[0].message.content)
except Exception as e:
    print(f"❌ System message test failed: {e}")

print("\n" + "=" * 50)
print("Note: If tests fail, check:")
print("1. ZAI_API_KEY is valid")
print("2. API endpoint hasn't changed")
print("3. Model names are correct (glm-4.5, glm-4-air, etc.)")
