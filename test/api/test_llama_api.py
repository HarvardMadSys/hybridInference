import os

from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables from .env file
load_dotenv()

client = OpenAI(api_key=os.environ["LLAMA_API_KEY"], base_url="https://api.llama.com/compat/v1/")
print(os.environ["LLAMA_API_KEY"])

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

print(completion.choices[0].message.content)
