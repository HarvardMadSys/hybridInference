#!/usr/bin/env python3
"""Analyze actual routing decisions vs optimal routing.

This script compares:
1. Actual routing decisions from HybridInference logs
2. Optimal routing computed offline

Use case: Validate if our online routing is close to optimal.
"""

import argparse
import logging
from pathlib import Path

from experiment.config import ExperimentConfig
from experiment.data.loader import DataLoader
from experiment.strategies.optimal import OptimalStrategy
from experiment.cost import CostCalculator
from experiment.quota import QuotaManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def analyze(config_path: str, dataset_path: str):
    """Analyze actual vs optimal routing.

    Args:
        config_path: Path to experiment config
        dataset_path: Path to HybridInference CSV (with Provider and Actual Cost)
    """
    # Load configuration
    config = ExperimentConfig(config_path)

    # Load data
    loader = DataLoader(config.to_dict())
    requests = loader.load(dataset_path)

    # Check if data has actual routing info
    has_actual = any(r.provider is not None for r in requests)
    if not has_actual:
        logger.error("Dataset does not contain actual provider information")
        logger.error("This script requires HybridInference format with Provider column")
        return

    # Calculate actual cost
    actual_cost = sum(r.actual_cost for r in requests if r.actual_cost is not None)
    actual_subscription_count = sum(1 for r in requests if r.provider and 'subscription' in r.provider)
    actual_api_count = len(requests) - actual_subscription_count

    # Compute optimal routing
    calculator = CostCalculator(config.providers)
    quota_manager = QuotaManager(
        daily_quota=config.get_subscription_provider().daily_quota,
        num_subscriptions=config.simulation["num_subscriptions"]
    )

    optimal_strategy = OptimalStrategy(calculator, quota_manager, config.to_dict())
    optimal_strategy.precompute(requests)

    # Calculate optimal cost
    optimal_cost = 0.0
    optimal_subscription_count = 0
    optimal_api_count = 0

    for request in requests:
        decision = optimal_strategy.route(request)
        optimal_cost += decision.cost
        if decision.is_subscription:
            optimal_subscription_count += 1
        else:
            optimal_api_count += 1

    # Add subscription fee
    unique_days = len(set(r.day for r in requests))
    num_days = unique_days
    subscription_fee = config.get_subscription_provider().monthly_fee / 30 * num_days
    optimal_cost += subscription_fee

    # Compare decisions
    different_decisions = 0
    for request in requests:
        actual_provider = request.provider
        optimal_decision = optimal_strategy.route(request)

        # Normalize provider names for comparison
        actual_is_sub = actual_provider and 'subscription' in actual_provider
        optimal_is_sub = optimal_decision.is_subscription

        if actual_is_sub != optimal_is_sub:
            different_decisions += 1

    # Print results
    print("\n" + "=" * 80)
    print("ACTUAL VS OPTIMAL ROUTING ANALYSIS")
    print("=" * 80)

    print(f"\nDataset: {dataset_path}")
    print(f"Total requests: {len(requests)}")
    print(f"Number of days: {num_days}")

    print("\n" + "-" * 80)
    print("ACTUAL ROUTING (from logs)")
    print("-" * 80)
    print(f"Total cost:        ${actual_cost:>12.2f}")
    print(f"Subscription uses: {actual_subscription_count:>12,} ({actual_subscription_count/len(requests)*100:.1f}%)")
    print(f"API uses:          {actual_api_count:>12,} ({actual_api_count/len(requests)*100:.1f}%)")

    print("\n" + "-" * 80)
    print("OPTIMAL ROUTING (computed)")
    print("-" * 80)
    print(f"Total cost:        ${optimal_cost:>12.2f}")
    print(f"Subscription uses: {optimal_subscription_count:>12,} ({optimal_subscription_count/len(requests)*100:.1f}%)")
    print(f"API uses:          {optimal_api_count:>12,} ({optimal_api_count/len(requests)*100:.1f}%)")

    print("\n" + "-" * 80)
    print("COMPARISON")
    print("-" * 80)
    savings = actual_cost - optimal_cost
    savings_pct = (savings / actual_cost * 100) if actual_cost > 0 else 0
    print(f"Potential savings: ${savings:>12.2f} ({savings_pct:.1f}%)")
    print(f"Different decisions: {different_decisions:>10,} ({different_decisions/len(requests)*100:.1f}%)")

    if savings > 0:
        print(f"\n✅ Optimal routing could save ${savings:.2f}")
    elif savings < 0:
        print(f"\n⚠️  Actual routing is better by ${-savings:.2f} (unexpected!)")
    else:
        print(f"\n✅ Actual routing is already optimal!")

    print("\n" + "=" * 80 + "\n")


def main():
    """Analyze actual vs optimal routing decisions.

    Loads historical data with actual routing decisions, computes optimal
    routing, and compares the two to identify potential cost savings.
    """
    parser = argparse.ArgumentParser(
        description="Analyze actual routing vs optimal routing"
    )
    parser.add_argument(
        "--config",
        default="config/experiment.yaml",
        help="Path to experiment config"
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Path to HybridInference CSV (with Provider and Actual Cost columns)"
    )

    args = parser.parse_args()

    analyze(args.config, args.dataset)


if __name__ == "__main__":
    main()
