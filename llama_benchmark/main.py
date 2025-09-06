# llama_benchmark/main.py

from llama_benchmark.benchmark import benchmark_model
from llama_benchmark.config import MODEL_CATALOG
from llama_benchmark.prompt_generator import make_uncached_prompt


def main():
    for model_info in MODEL_CATALOG:
        print(f"--- Benchmarking {model_info['model_id']} ---")
        benchmark_model(model_info, make_uncached_prompt, runs=100, warmup_runs=3, sleep_time=1)

        # print(f"--- Benchmarking {model_info['model_id']} with prefix cached prompt ---")
        # with open("llama_benchmark/res/benchmark_results.jsonl", "a") as f:
        #     f.write(f"--- Benchmarking {model_info['model_id']} with prefix cached prompt ---\n")
        # benchmark_model(model_info, lambda: make_prefix_cached_prompt(500))


if __name__ == "__main__":
    main()
