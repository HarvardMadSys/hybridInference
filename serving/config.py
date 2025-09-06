import os

from dotenv import load_dotenv


def get_config():
    """Load environment variables and return simple config"""
    load_dotenv()

    return {
        "local": {"base_url": os.getenv("LOCAL_BASE_URL", "http://localhost:8001")},
        "openai": {
            "api_key": os.getenv("OPENAI_API_KEY"),
            "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        },
        "llama": {
            "api_key": os.getenv("LLAMA_API_KEY"),
            "base_url": os.getenv("LLAMA_BASE_URL", "https://api.llama.com/compat/v1/"),
        },
        "openrouter": {
            "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            "api_key": os.getenv("OPENROUTER_API_KEY"),
            # Optional attribution headers for OpenRouter, Site URL and title for rankings on openrouter.ai.
            "http_referer": os.getenv("OPENROUTER_HTTP_REFERER", None),
            "x_title": os.getenv("OPENROUTER_X_TITLE", None),
        },
    }
