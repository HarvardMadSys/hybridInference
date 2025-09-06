# llama_benchmark/prompt_generator.py

import random


def make_uncached_prompt():
    nonce = random.randint(9, 9999999)
    return [
        {"role": "user", "content": f"Task {nonce}: Respond only with a single digit from 0 to 9."}
    ]


def make_prefix_cached_prompt(prefix_tokens=500):
    prefix = "Alice: Hello Bob!\nBob: Hi Alice!\n" * (prefix_tokens // 10)
    return [{"role": "user", "content": prefix + "\nAlice: What do you think about today?"}]


def make_screenshot_prompt():
    return [{"role": "user", "content": ""}]
