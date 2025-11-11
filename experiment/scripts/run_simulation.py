#!/usr/bin/env python3
"""Run offline routing simulation.

This script loads historical request data, runs the specified routing strategy,
and outputs cost analysis results.

Usage:
    python scripts/experiment/run_simulation.py --config config/experiment.yaml
    python scripts/experiment/run_simulation.py --config config/experiment.yaml --num-subscriptions 2
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from experiment.config import ExperimentConfig
from experiment.cost.calculator import CostCalculator
from experiment.data.loader import DataLoader
from experiment.quota.manager import QuotaManager
from experiment.simulator import OfflineSimulator
from experiment.strategies.optimal import OptimalStrategy


def setup_logging(level: str = "INFO") -> None:
    """Setup logging configuration.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Run offline routing simulation")
    parser.add_argument(
        "--config",
        type=str,
        default="config/experiment.yaml",
        help="Path to experiment configuration file",
    )
    parser.add_argument(
        "--num-subscriptions",
        type=int,
        help="Override number of subscriptions (default: from config)",
    )
    parser.add_argument("--output", type=str, help="Output file for results (JSON)")
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    try:
        # Load configuration
        logger.info(f"Loading configuration from {args.config}")
        config = ExperimentConfig(args.config)

        # Override num_subscriptions if specified
        if args.num_subscriptions:
            config.simulation["num_subscriptions"] = args.num_subscriptions
            logger.info(f"Overriding num_subscriptions to {args.num_subscriptions}")

        # Print configuration
        print("\n" + "=" * 60)
        print("EXPERIMENT CONFIGURATION")
        print("=" * 60)
        print("\nProviders:")
        for pid, provider in config.providers.items():
            if provider.is_subscription():
                print(
                    f"  - {pid}: ${provider.monthly_fee}/month, "
                    f"{provider.daily_quota} quota/day"
                )
            else:
                print(
                    f"  - {pid}: ${provider.input_price_per_1k:.6f}/1K input, "
                    f"${provider.output_price_per_1k:.6f}/1K output"
                )

        print("\nSimulation:")
        print(f"  - Subscriptions: {config.simulation['num_subscriptions']}")
        print(f"  - Default subscription: {config.simulation['default_subscription']}")
        print(f"  - Default API: {config.simulation['default_api_fallback']}")

        print("\nDataset:")
        print(f"  - Path: {config.dataset['path']}")
        if config.dataset.get("filter_model"):
            print(f"  - Filter model: {config.dataset['filter_model']}")

        # Load data
        print("\n" + "=" * 60)
        print("LOADING DATA")
        print("=" * 60 + "\n")

        loader = DataLoader(config.to_dict())

        # Apply model filter if specified in config
        filter_model = config.dataset.get("filter_model")
        requests = loader.load(config.dataset["path"], filter_model=filter_model)

        # Print dataset statistics
        stats = loader.get_statistics(requests)
        print("\nDataset Statistics:")
        print(f"  - Total requests: {stats['total_requests']:,}")
        print(f"  - Number of days: {stats['num_days']}")
        print(f"  - Total tokens: {stats['total_tokens']:,}")
        print(f"  - Avg request tokens: {stats['avg_request_tokens']:.1f}")
        print(f"  - Avg response tokens: {stats['avg_response_tokens']:.1f}")
        print(f"  - Models: {stats['models']}")

        # Create strategy components
        calculator = CostCalculator(config.providers)
        quota_manager = QuotaManager(
            daily_quota=config.get_subscription_provider().daily_quota,
            num_subscriptions=config.simulation["num_subscriptions"],
        )

        # Create Optimal strategy
        print("\n" + "=" * 60)
        print("RUNNING SIMULATION")
        print("=" * 60 + "\n")

        strategy = OptimalStrategy(calculator, quota_manager, config.to_dict())

        # Precompute optimal assignments
        print("Precomputing optimal assignments...")
        strategy.precompute(requests)

        # Run simulation
        print("Running simulation...")
        simulator = OfflineSimulator(requests, strategy, config.to_dict())
        result = simulator.run()

        # Print results
        print("\n" + "=" * 60)
        print("RESULTS")
        print("=" * 60 + "\n")

        print(f"Strategy: {result.strategy_name}")
        print("\nCosts:")
        print(f"  - Total cost:        ${result.total_cost:>10.2f}")
        print(f"  - Subscription cost: ${result.subscription_cost:>10.2f}")
        print(f"  - API cost:          ${result.api_cost:>10.2f}")

        print("\nRequests:")
        print(f"  - Total:        {result.num_requests:>10,}")
        print(
            f"  - Subscription: {result.subscription_requests:>10,} "
            f"({result.subscription_requests/result.num_requests*100:.1f}%)"
        )
        print(
            f"  - API:          {result.api_requests:>10,} "
            f"({result.api_requests/result.num_requests*100:.1f}%)"
        )

        print("\nQuota:")
        print(f"  - Utilization: {result.quota_utilization*100:>10.1f}%")
        print(f"  - Days: {result.num_days}")

        print("\nPerformance:")
        print(f"  - Runtime: {result.runtime_seconds:.2f}s")

        # Save results if output file specified
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, "w") as f:
                json.dump(result.to_dict(), f, indent=2)

            print(f"\nResults saved to {output_path}")

        print("\n" + "=" * 60 + "\n")

    except Exception as e:
        logger.error(f"Simulation failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
