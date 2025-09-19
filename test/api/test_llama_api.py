import os

from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables from .env file
load_dotenv()

client = OpenAI(api_key=os.environ["LLAMA_API_KEY"], base_url="https://api.llama.com/compat/v1/")
# print(os.environ["LLAMA_API_KEY"])

# Create chat completion request
completion = client.chat.completions.create(
    model="Llama-3.3-8B-Instruct",
    messages=[
        {"role": "developer", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
    ],
)

completion = client.chat.completions.create(
    model="Llama-3.3-70B-Instruct",
    messages=[
        {"role": "developer", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
    ],
)

print("Llama-3.3-70B-Instruct:", completion.choices[0].message.content)

# Test Llama-4-Scout model ID
try:
    completion_scout = client.chat.completions.create(
        model="Llama-4-Scout-17B-16E-Instruct-FP8",
        messages=[
            {"role": "developer", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello Scout!"},
        ],
    )
    print("Llama-4-Scout-17B-16E-Instruct-FP8:", completion_scout.choices[0].message.content)
except Exception as e:
    print(f"Llama-4-Scout-17B-16E-Instruct-FP8 failed: {e}")

# Also try alternative model name format
try:
    completion_scout2 = client.chat.completions.create(
        model="Llama-4-Scout-17B-Instruct",
        messages=[
            {"role": "developer", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello Scout!"},
        ],
    )
    print("Llama-4-Scout-17B-Instruct:", completion_scout2.choices[0].message.content)
except Exception as e:
    print(f"Llama-4-Scout-17B-Instruct failed: {e}")
