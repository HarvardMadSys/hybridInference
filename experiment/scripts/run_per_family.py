#!/usr/bin/env python3
"""Run experiments for each model family.

This script runs offline routing experiments for different model families:
- Llama family (llama-3.3-70b, llama-4-scout, llama-4-maverick)
- Gemini family (gemini-2.5-flash, gemini-2.5-flash-preview)
- GLM family (glm-4.5, glm-4.6)
- DeepSeek family (deepseek-chat)

For each family, it runs:
1. All-API baseline
2. Greedy strategy
3. Optimal strategy

Then generates comparison plots across families.
"""

import json
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from experiment.config import ExperimentConfig
from experiment.cost.calculator import CostCalculator
from experiment.data.loader import DataLoader
from experiment.data.schema import ProviderConfig, ProviderType
from experiment.quota.manager import QuotaManager
from experiment.simulator import OfflineSimulator
from experiment.strategies.all_api import AllAPIStrategy
from experiment.strategies.greedy import GreedyStrategy
from experiment.strategies.optimal import OptimalStrategy

# Model family definitions
MODEL_FAMILIES = {
    "Llama": {
        "models": ["llama-3.3-70b-instruct", "llama-4-scout", "llama-4-maverick"],
        "representative": "llama-3.3-70b-instruct",  # Most requests
        "pricing": {
            "prompt": "0.6",  # $0.6 per 1M tokens (example)
            "completion": "0.6",
        },
    },
    "Gemini": {
        "models": ["gemini-2.5-flash", "gemini-2.5-flash-preview-09-2025"],
        "representative": "gemini-2.5-flash",
        "pricing": {
            "prompt": "0.075",  # $0.075 per 1M tokens
            "completion": "0.3",
        },
    },
    "GLM": {
        "models": ["glm-4.5", "glm-4.6"],
        "representative": "glm-4.5",
        "pricing": {
            "prompt": "0.5",  # Example pricing
            "completion": "0.5",
        },
    },
    "DeepSeek": {
        "models": ["deepseek-chat"],
        "representative": "deepseek-chat",
        "pricing": {
            "prompt": "0.14",  # $0.14 per 1M tokens
            "completion": "0.28",
        },
    },
}


def run_strategy_for_family(family_name: str, family_config: dict, strategy_name: str):
    """Run a specific strategy for a model family.

    Args:
        family_name: Name of model family (e.g., 'Llama')
        family_config: Family configuration dict
        strategy_name: Strategy to run ('all-api', 'greedy', 'optimal')

    Returns:
        Simulation result dict
    """
    print(f"\n{'='*70}")
    print(f"Running {strategy_name.upper()} for {family_name} family")
    print("=" * 70)

    # Load config
    config = ExperimentConfig("config/experiment.yaml")
    config_dict = config.to_dict()

    # Load data
    loader = DataLoader(config_dict)

    # Filter for this family's models
    all_requests = loader.load(config_dict["dataset"]["path"])
    family_requests = [r for r in all_requests if r.model in family_config["models"]]

    print(f"Loaded {len(family_requests):,} requests for {family_name} family")
    print(f"Models: {', '.join(family_config['models'])}")

    if not family_requests:
        print(f"⚠ No requests found for {family_name} family")
        return None

    # Create cost calculator with updated pricing for this family
    # We need to create new ProviderConfig instances with updated pricing
    providers_copy = {}
    for provider_id, provider in config.providers.items():
        if provider.is_api():
            # Create new provider with updated pricing for this family
            providers_copy[provider_id] = ProviderConfig(
                name=provider.name,
                type=ProviderType.API,
                monthly_fee=0.0,
                daily_quota=0,
                input_price_per_1k=float(family_config["pricing"]["prompt"])
                / 1000,  # Convert to per-1K
                output_price_per_1k=float(family_config["pricing"]["completion"]) / 1000,
            )
        else:
            # Keep subscription provider as-is
            providers_copy[provider_id] = provider

    # Update config_dict with new providers
    config_dict["providers"] = providers_copy

    calculator = CostCalculator(providers_copy)

    if strategy_name == "all-api":
        quota_manager = QuotaManager(daily_quota=0, num_subscriptions=0)
        strategy = AllAPIStrategy(calculator, quota_manager, config_dict)
    elif strategy_name == "greedy":
        sub_provider = config.get_subscription_provider()
        quota_manager = QuotaManager(
            daily_quota=sub_provider.daily_quota,
            num_subscriptions=config_dict["simulation"]["num_subscriptions"],
        )
        strategy = GreedyStrategy(calculator, quota_manager, config_dict)
    elif strategy_name == "optimal":
        sub_provider = config.get_subscription_provider()
        quota_manager = QuotaManager(
            daily_quota=sub_provider.daily_quota,
            num_subscriptions=config_dict["simulation"]["num_subscriptions"],
        )
        strategy = OptimalStrategy(calculator, quota_manager, config_dict)
    else:
        raise ValueError(f"Unknown strategy: {strategy_name}")

    # Run simulation
    simulator = OfflineSimulator(family_requests, strategy, config_dict)
    result = simulator.run()

    # Convert to dict
    if hasattr(result, "to_dict"):
        result_dict = result.to_dict()
    elif hasattr(result, "__dict__"):
        result_dict = result.__dict__
    else:
        result_dict = result

    # Save result
    output_path = (
        project_root / "experiment" / "results" / f"{family_name.lower()}_{strategy_name}.json"
    )
    with open(output_path, "w") as f:
        json.dump(result_dict, f, indent=2)

    print(f"\n✓ {strategy_name.upper()} Results:")
    print(f"  Total Cost:        ${result_dict['costs']['total']:.2f}")
    print(f"  API Cost:          ${result_dict['costs']['api']:.2f}")
    print(f"  Subscription Cost: ${result_dict['costs']['subscription']:.2f}")
    if strategy_name != "all-api":
        print(f"  Quota Utilization: {result_dict['quota_utilization']*100:.1f}%")
    print(f"  Saved to: {output_path}")

    return result_dict


def main():
    """Run experiments for all model families."""
    print("=" * 70)
    print("RUNNING EXPERIMENTS FOR ALL MODEL FAMILIES")
    print("=" * 70)

    all_results = {}

    for family_name, family_config in MODEL_FAMILIES.items():
        print(f"\n\n{'#'*70}")
        print(f"# {family_name} Family")
        print(f"{'#'*70}")

        family_results = {}

        # Run all strategies
        for strategy in ["all-api", "greedy", "optimal"]:
            result = run_strategy_for_family(family_name, family_config, strategy)
            if result:
                family_results[strategy] = result

        all_results[family_name] = family_results

    # Print summary
    print("\n\n" + "=" * 70)
    print("SUMMARY: ALL FAMILIES")
    print("=" * 70)

    print(f"\n{'Family':<15} {'All-API':<12} {'Greedy':<12} {'Optimal':<12} {'Savings':<12}")
    print("-" * 70)

    for family_name in MODEL_FAMILIES:
        if all_results.get(family_name):
            results = all_results[family_name]
            all_api_cost = results.get("all-api", {}).get("costs", {}).get("total", 0)
            greedy_cost = results.get("greedy", {}).get("costs", {}).get("total", 0)
            optimal_cost = results.get("optimal", {}).get("costs", {}).get("total", 0)
            savings = all_api_cost - optimal_cost
            savings_pct = (savings / all_api_cost * 100) if all_api_cost > 0 else 0

            print(
                f"{family_name:<15} ${all_api_cost:<11.2f} ${greedy_cost:<11.2f} ${optimal_cost:<11.2f} ${savings:.2f} ({savings_pct:.1f}%)"
            )

    print("\n" + "=" * 70)
    print("Next step: Generate cross-family comparison plots")
    print("  python experiment/scripts/generate_family_plots.py")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
