"""Test DeepSeek API directly (external)."""

import os

import pytest
from dotenv import load_dotenv
from openai import OpenAI

pytestmark = pytest.mark.external


def test_deepseek_chat_basic():
    """Basic DeepSeek chat call; requires network and DEEPSEEK_API_KEY."""
    load_dotenv()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY not configured")

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ],
        stream=False,
    )
    assert response.choices[0].message.content is not None
