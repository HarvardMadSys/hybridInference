import os

import pytest
from dotenv import load_dotenv
from google import genai
from google.genai import types

pytestmark = pytest.mark.external


def test_gemini_simple_generation():
    """Simple Gemini generation call; requires valid key and network."""
    load_dotenv()
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        config=types.GenerateContentConfig(system_instruction="You are a cat. Your name is Neko."),
        contents="Hello there",
    )

    assert response is not None
