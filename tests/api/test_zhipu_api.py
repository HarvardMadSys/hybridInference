"""Test Zhipu AI GLM models API directly (external)."""

import os

import pytest
from dotenv import load_dotenv
from openai import OpenAI

pytestmark = pytest.mark.external


def _get_zhipu_client():
    load_dotenv()
    api_key = os.environ.get("ZAI_API_KEY")
    if not api_key:
        pytest.skip("ZAI_API_KEY not configured", allow_module_level=False)
    return OpenAI(api_key=api_key, base_url="https://api.z.ai/api/coding/paas/v4/")


@pytest.mark.parametrize("model", ["glm-4.7", "glm-4.7-flash", "glm-5"])
def test_zhipu_basic(model: str):
    """Basic call against active GLM models; requires network and ZAI_API_KEY."""
    client = _get_zhipu_client()
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": "Hello! Please respond with 'Hi there' to confirm you're working.",
            },
        ],
        temperature=0.7,
        max_tokens=64,
    )
    assert completion is not None
    assert completion.choices[0].message.content is not None
