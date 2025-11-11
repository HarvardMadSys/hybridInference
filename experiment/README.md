# Offline Routing Experiment

This package provides tools for simulating and evaluating different routing strategies on historical request data.

## Overview

The offline routing experiment allows you to:
- Evaluate routing strategies on historical data
- Compare different strategies (Optimal, Greedy, TwoStep)
- Analyze cost savings and quota utilization
- Tune parameters before deploying to production

## Architecture

```
experiment/
├── data/           # Data models and loaders
│   ├── schema.py   # Request, Provider, Decision models
│   └── loader.py   # CSV data loader
├── cost/           # Cost calculation
│   └── calculator.py
├── quota/          # Quota management
│   └── manager.py
├── strategies/     # Routing strategies
│   ├── base.py     # Base strategy class
│   └── optimal.py  # Optimal strategy (Phase 1)
├── config.py       # Configuration management
└── simulator.py    # Offline simulator
```

## Quick Start

```bash
# Run simulation with default config
python scripts/experiment/run_simulation.py

# Run with custom config
python scripts/experiment/run_simulation.py \
  --config config/experiment.yaml \
  --output results/experiment/my_results.json

# Run with different number of subscriptions
python scripts/experiment/run_simulation.py \
  --num-subscriptions 2 \
  --output results/experiment/2subs.json

# Compare multiple results
python scripts/experiment/compare_results.py results/experiment/*.json
```

### 4. View Results

```bash
cat results/experiment/optimal_results.json
```

## Usage Examples

### Basic Simulation

```bash
python scripts/experiment/run_simulation.py
```

### Override Number of Subscriptions

```bash
python scripts/experiment/run_simulation.py --num-subscriptions 2
```

### What-If Analysis

Compare costs with different subscription counts:

```bash
for num_subs in 1 2 3 4 5; do
  python scripts/experiment/run_simulation.py \
    --num-subscriptions $num_subs \
    --output results/experiment/optimal_${num_subs}subs.json
done
```

## Results

The simulation outputs:

```json
{
  "strategy": "Optimal",
  "costs": {
    "total": 1258.06,
    "subscription": 40.67,
    "api": 1217.39
  },
  "requests": {
    "total": 1404310,
    "subscription": 189710,
    "api": 1214600
  },
  "quota_utilization": 0.627,
  "num_days": 61,
  "runtime_seconds": 6.88
}
```

### Key Metrics

- **Total Cost**: Subscription + API costs
- **Quota Utilization**: How much of the daily quota is used on average
- **Request Distribution**: Percentage routed to subscription vs API
- **Runtime**: Simulation performance

## Strategies

### Phase 1: Optimal (Implemented)

The Optimal strategy has perfect future knowledge and computes the globally optimal routing:

- **Algorithm**: Daily knapsack problem
- **Complexity**: O(n) with NumPy acceleration
- **Use Case**: Establishes theoretical lower bound for cost

**How it works**:
1. For each day, calculate API cost for each request
2. Select top K requests with highest API cost for subscription
3. Route remaining requests to cheapest API

### Phase 2: Greedy (Planned)

Simple baseline strategy:
- Use subscription quota first
- Fall back to cheapest API when quota exhausted

### Phase 3: TwoStep (Planned)

Production-ready online strategy:
- Estimate average subscription cost
- Route request to subscription if API cost > avg subscription cost
- Otherwise use API

## Performance

On BurstGPT_1.csv (1.4M requests):

- **Loading**: ~7 seconds
- **Precomputation**: ~2 seconds
- **Simulation**: ~7 seconds
- **Total**: ~16 seconds

Performance optimizations:
- NumPy vectorization for cost calculation
- `np.argpartition` for O(n) top-K selection
- Batch processing for large datasets

## Configuration

### Provider Configuration

```yaml
models:
  - id: provider-id
    name: "Provider Name"
    type: subscription|api
    pricing:
      # For subscription:
      monthly_fee: "20.0"
      daily_quota: 5000

      # For API:
      prompt: "1.5"      # per 1M tokens
      completion: "2.0"  # per 1M tokens
```

### Simulation Settings

```yaml
simulation:
  num_subscriptions: 1         # Number of subscription accounts
  days_per_month: 30           # For cost prorating
  default_subscription: "id"   # Default subscription provider
  default_api_fallback: "id"   # Default API provider
```

## Development

### Adding a New Strategy

1. Create `experiment/strategies/your_strategy.py`:

```python
from experiment.strategies.base import RoutingStrategy
from experiment.data.schema import Request, RoutingDecision

class YourStrategy(RoutingStrategy):
    def name(self) -> str:
        return "YourStrategy"

    def route(self, request: Request) -> RoutingDecision:
        # Your routing logic here
        pass
```

2. Update `experiment/strategies/__init__.py`
3. Add to `config/experiment.yaml`
4. Update CLI script to support new strategy

### Running Tests

```bash
pytest experiment/
```

## References

- [Design Document](../docs/OFFLINE_ROUTING_DESIGN_CN.md)
- [Online Routing Explained](../docs/ONLINE_ROUTING_EXPLAINED.md)
- [Mathematical Formulation](../docs/ExternalOptimization.md)

## License

See project LICENSE file.
