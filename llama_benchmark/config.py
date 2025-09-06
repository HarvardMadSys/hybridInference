# llama_benchmark/config.py

API_KEY = "YOUR_API_KEY_HERE"
API_BASE = "https://api.llama.com/v1"

MODEL_CATALOG = [
    # {
    #     "model_id": "Llama-4-Maverick-17B-128E-Instruct-FP8",
    #     "provider": "Meta",
    #     "input_modalities": ["text", "image"],
    #     "output_modalities": ["text"]
    # },
    # {
    #     "model_id": "Llama-4-Scout-17B-16E-Instruct-FP8",
    #     "provider": "Meta",
    #     "input_modalities": ["text", "image"],
    #     "output_modalities": ["text"]
    # },
    {
        "model_id": "Llama-3.3-70B-Instruct",
        "provider": "Meta",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
    },
    {
        "model_id": "Llama-3.3-8B-Instruct",
        "provider": "Meta",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
    },
    # {
    #     "model_id": "Cerebras-Llama-4-Maverick-17B-128E-Instruct",
    #     "provider": "Cerebras",
    #     "input_modalities": ["text"],
    #     "output_modalities": ["text"]
    # },
    # {
    #     "model_id": "Cerebras-Llama-4-Scout-17B-16E-Instruct",
    #     "provider": "Cerebras",
    #     "input_modalities": ["text"],
    #     "output_modalities": ["text"]
    # },
    # {
    #     "model_id": "Groq-Llama-4-Maverick-17B-128E-Instruct",
    #     "provider": "Groq",
    #     "input_modalities": ["text"],
    #     "output_modalities": ["text"]
    # },
]

HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
