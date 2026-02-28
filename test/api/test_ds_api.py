import os

import pytest
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

pytestmark = pytest.mark.external

# TEST configuration
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    pytest.skip("DEEPSEEK_API_KEY not configured", allow_module_level=True)


def test_deepseek_api():
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ],
        stream=False,
    )

    assert response.choices[0].message.content is not None
