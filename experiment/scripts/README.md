# Experiment Scripts

This directory contains scripts for running offline routing experiments and generating analysis.

## Scripts

### 1. `run_simulation.py`

Run offline routing simulation with different strategies.

**Usage:**
```bash
# Run with default config (Optimal strategy)
python experiment/scripts/run_simulation.py

# Run with specific config
python experiment/scripts/run_simulation.py \
  --config config/experiment.yaml \
  --output experiment/results/my_results.json

# Run with different number of subscriptions
python experiment/scripts/run_simulation.py \
  --num-subscriptions 2 \
  --output experiment/results/2subs.json
```

**Strategies:**
- Optimal: Offline optimal with perfect future knowledge
- All-API: Baseline without subscription
- Greedy: Simple online algorithm (use quota first)

### 2. `generate_plots.py`

Generate publication-quality figures from experiment results.

**Usage:**
```bash
python experiment/scripts/generate_plots.py
```

**Output:**
- `experiment/results/figures/fig1_cost_comparison.pdf/png`
- `experiment/results/figures/fig2_cost_breakdown.pdf/png`
- `experiment/results/figures/fig3_quota_utilization.pdf/png`
- `experiment/results/figures/fig4_savings_analysis.pdf/png`
- `experiment/results/figures/fig5_competitive_ratio.pdf/png`

### 3. `compare_results.py`

Compare multiple experiment results.

**Usage:**
```bash
python experiment/scripts/compare_results.py \
  experiment/results/*.json
```

### 4. `analyze_actual_vs_optimal.py`

Analyze actual vs optimal routing decisions.

**Usage:**
```bash
python experiment/scripts/analyze_actual_vs_optimal.py
```

## Quick Start

### Run Complete Experiment

```bash
# 1. Run All-API baseline
python experiment/scripts/run_simulation.py \
  --config config/experiment.yaml \
  --strategy all-api \
  --output experiment/results/chatgpt_all_api.json

# 2. Run Greedy
python experiment/scripts/run_simulation.py \
  --config config/experiment.yaml \
  --strategy greedy \
  --output experiment/results/chatgpt_greedy.json

# 3. Run Optimal
python experiment/scripts/run_simulation.py \
  --config config/experiment.yaml \
  --output experiment/results/chatgpt_optimal.json

# 4. Generate plots
python experiment/scripts/generate_plots.py
```

## Results

Results are saved to `experiment/results/`:
- JSON files with detailed metrics
- `figures/` directory with publication-quality plots

## Configuration

Experiments use config file from `config/`:
- `experiment.yaml` - Main experiment configuration (currently single model: ChatGPT)

To test different models, modify the `filter_model` and `model_mapping` in the config.

## For OSDI Submission

The generated figures are publication-ready:
- PDF format for LaTeX papers
- PNG format for presentations
- 300 DPI resolution
- Clean, professional styling
