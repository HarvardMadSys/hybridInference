# Llama Benchmark Suite

A clean, efficient benchmark suite for testing LLM endpoints.

## Features

- **Realistic prompts** - Uses actual questions at different lengths (32, 128, 512, 2048 tokens)
- **Concurrency testing** - Tests with 1, 4, 16, 32 concurrent requests
- **Comprehensive metrics** - P50/P95/P99 latency, throughput, success rate
- **Automatic visualization** - Generates charts showing performance characteristics
- **Easy configuration** - Simple setup for testing multiple endpoints

## Quick Start

```bash
# Run the benchmark
./run_benchmark.sh

# Or manually:
python3 advanced_benchmark.py
python3 visualize.py
```

## Configuration

Edit `advanced_benchmark.py` to add your servers:

```python
servers = [
    TestConfig(
        name="local_vllm",
        base_url="http://198.51.100.34:9001/v1",
        api_key="EMPTY",
        model="/models/meta-llama_Llama-4-Scout-17B-16E"
    ),
    TestConfig(
        name="llama_api",
        base_url="https://api.llama.com/compat/v1",
        api_key=os.getenv("LLAMA_API_KEY"),
        model="Llama-4-Scout-17B-16E-Instruct-FP8"
    )
]
```

## Results

The benchmark generates:
- `results.json` - Raw benchmark data
- `benchmark_results.png` - Performance visualizations

## Key Metrics

- **Latency** - Response time at different percentiles
- **Throughput** - Requests per second at different concurrency levels
- **Success Rate** - Percentage of successful requests
- **Token Generation** - Tokens per second

## Why This Approach?

Unlike the GPT solution which:
- Uses repetitive "lorem ipsum" text (unrealistic)
- Has 400+ lines of complex code
- Missing key visualizations

This solution:
- Uses realistic, varied prompts
- Clean, maintainable code (~200 lines)
- Automatic visualization and analysis
- Easy to extend and customize
