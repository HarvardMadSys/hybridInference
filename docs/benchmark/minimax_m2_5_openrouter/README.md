# MiniMax-M2.5 OpenRouter TTFT Benchmark

**Model:** `MiniMaxAI/MiniMax-M2.5`
**Purpose:** Measure TTFT (Time To First Token) and throughput across OpenRouter providers for MiniMax-M2.5.

## Providers tested

- deepinfra
- fireworks
- together-ai
- featherless
- chutes
- ollama

## Test matrix

| Parameter | Values |
|---|---|
| Input lengths | 1,024 / 4,096 / 16,384 tokens |
| Concurrency | 1 / 4 / 16 / 64 |
| Repeats | 3 per configuration |

## Notes

- **TTFT** is measured from request start to the first SSE chunk carrying non-empty content (role-only chunks are ignored).
- **Chars** counts response characters (not tokens). Throughput is reported as chars/sec.

## Setup

```bash
export OPENROUTER_API_KEY=<your-key>
```

## Run

```bash
# Full pipeline: benchmark + aggregate + plot
python -c "
import sys
sys.path.insert(0, 'docs/benchmark')
from minimax_m2_5_openrouter.openrouter_benchmark import __main__
__main__.main()
"

# Skip plotting
python -c "
import sys
sys.path.insert(0, 'docs/benchmark')
from minimax_m2_5_openrouter.openrouter_benchmark import __main__
__main__.main(['--skip-plot'])
"

# Custom providers / params
python -c "
import sys
sys.path.insert(0, 'docs/benchmark')
from minimax_m2_5_openrouter.openrouter_benchmark import __main__
__main__.main(['--providers', 'deepinfra', 'fireworks', '--input-lens', '1024', '4096', '--concurrencies', '1', '16'])
"
```

## Output

```
results/minimax_m2_5_openrouter/
├── raw.csv          # per-request raw data (chars, not tokens)
├── summary.csv      # aggregated p50/p95 TTFT + chars/sec throughput
└── plots/
    ├── ttft_heatmap.png
    ├── ttft_by_concurrency.png
    └── throughput_bar.png
```

## Standalone steps

```bash
# 1. Run benchmark only
python -c "
import sys; sys.path.insert(0, 'docs/benchmark')
from minimax_m2_5_openrouter.openrouter_benchmark import benchmark
benchmark.main(['--api-key', '\$OPENROUTER_API_KEY'])
"

# 2. Aggregate
python -c "
import sys; sys.path.insert(0, 'docs/benchmark')
from minimax_m2_5_openrouter.openrouter_benchmark import aggregate
aggregate.main(['results/minimax_m2_5_openrouter/raw.csv'])
"

# 3. Plot (requires pandas + matplotlib)
python -c "
import sys; sys.path.insert(0, 'docs/benchmark')
from minimax_m2_5_openrouter.openrouter_benchmark import plot
plot.main(['results/minimax_m2_5_openrouter/summary.csv'])
"
```