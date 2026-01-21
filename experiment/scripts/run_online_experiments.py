#!/usr/bin/env python3
"""Run online routing experiments.

This script evaluates online routing strategies against offline optimal baselines.

Experiments:
1. Stage 1 (S_Q + S_A): Daily quota optimization
   - Dataset: BurstGPT (no latency required)
   - Strategies: Greedy, PrimalDual, LA-PD (P10/P20/P50), Offline-Optimal

2. Stage 2 (S_Q + S_C + S_A): Joint optimization
   - Dataset: rednote/freeinference (latency required)
   - Strategies: Greedy, PrimalDual, LA-PD-Unified, Offline-Optimal (ILP)

Usage:
    python experiment/scripts/run_online_experiments.py --stage 1
    python experiment/scripts/run_online_experiments.py --stage 2
    python experiment/scripts/run_online_experiments.py --stage both
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from experiment.config import ExperimentConfig
from experiment.cost import CostCalculator
from experiment.data.loader import DataLoader
from experiment.predictors import EMAOutputPredictor, HistogramOutputPredictor
from experiment.quota import QuotaManager
from experiment.simulator import OfflineSimulator

# Strategies
from experiment.strategies.all_api import AllAPIStrategy
from experiment.strategies.online import (
    GreedyOnlineStrategy,
    LAPDConfig,
    LearningAugmentedPrimalDualStrategy,
    LearningAugmentedUnifiedStrategy,
    PrimalDualOnlineStrategy,
)
from experiment.strategies.stage1_optimal import OptimalStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _validate_model_pricing_coverage(
    requests,
    model_pricing: dict[str, dict[str, float]] | None,
    model_override: str | None,
) -> None:
    """Validate that all request models are priced when model_pricing is enabled.

    Args:
        requests: List of requests.
        model_pricing: Per-model pricing dictionary (per 1M tokens).
        model_override: Optional model override passed to the loader.

    Raises:
        ValueError: If model_pricing is enabled but required models are missing.
    """
    if not model_pricing:
        return

    if model_override:
        if model_override not in model_pricing:
            raise ValueError(
                f"--model '{model_override}' is not found in model_pricing. "
                "Please use a model name that exists in config/experiment.yaml:model_pricing, "
                "or add pricing for it."
            )
        return

    models = {r.model for r in requests if getattr(r, "model", None)}
    missing = sorted(m for m in models if m not in model_pricing)
    if missing:
        raise ValueError(
            "Missing model_pricing entries for dataset models: "
            f"{missing}. Either add pricing in config/experiment.yaml:model_pricing, "
            "or run with --model <target_model> to map all requests to a single priced model."
        )


def _safe_get_request_cost(cost_calculator: CostCalculator, request) -> float:
    """Get request cost with graceful fallback.

    Prefer per-model pricing when available; fall back to cheapest API provider
    pricing when the model is missing in model_pricing.

    Args:
        cost_calculator: Cost calculator.
        request: Request object.

    Returns:
        Cost in dollars.
    """
    try:
        return cost_calculator.calculate_cost_by_model(request)
    except Exception:
        try:
            _, cost = cost_calculator.get_cheapest_api_provider(request)
            return cost
        except Exception:
            return 0.0


def run_stage1_experiments(
    config: ExperimentConfig,
    data_path: str,
    output_dir: Path,
    model_override: str | None = None,
):
    """Run Stage 1 experiments: S_Q + S_A (daily quota optimization).

    Args:
        config: Experiment configuration
        data_path: Path to dataset (BurstGPT)
        output_dir: Output directory for results
        model_override: If set, map all requests to this model for pricing
    """
    logger.info("=" * 60)
    logger.info("Stage 1 Experiments: Daily Quota Optimization (S_Q + S_A)")
    logger.info("=" * 60)

    # Load data
    loader = DataLoader(config.to_dict())
    requests = loader.load(data_path, model_override=model_override)
    stats = loader.get_statistics(requests)

    if model_override:
        logger.info(f"Using model override: {model_override}")
    logger.info(f"Loaded {stats['total_requests']} requests over {stats['num_days']} days")

    # Get configuration
    config_dict = config.to_dict()
    num_subscriptions = config_dict["simulation"]["num_subscriptions"]
    subscription_provider = config.get_subscription_provider()
    daily_quota = subscription_provider.daily_quota * num_subscriptions

    logger.info(f"Daily quota: {daily_quota}")

    _validate_model_pricing_coverage(requests, config_dict.get("model_pricing"), model_override)

    # Calculate cost statistics for L/U estimation
    cost_calculator = CostCalculator(config_dict["providers"], config_dict.get("model_pricing"))
    costs = [_safe_get_request_cost(cost_calculator, r) for r in requests[:10000]]
    costs.sort()
    L = costs[int(len(costs) * 0.05)] if costs else 0.0001
    U = costs[int(len(costs) * 0.95)] if costs else 0.01
    logger.info(f"Estimated cost bounds: L={L:.6f}, U={U:.6f}")

    results = {}

    # 1. All-API Baseline
    logger.info("\n--- Running All-API Baseline ---")
    quota_mgr = QuotaManager(daily_quota)
    strategy = AllAPIStrategy(cost_calculator, quota_mgr, config_dict)
    simulator = OfflineSimulator(requests, strategy, config_dict)
    results["All-API"] = simulator.run().to_dict()

    # 2. Greedy Online
    logger.info("\n--- Running Greedy Online ---")
    quota_mgr = QuotaManager(daily_quota)
    strategy = GreedyOnlineStrategy(
        cost_calculator,
        quota_mgr,
        config_dict,
        daily_quota=daily_quota,
        concurrency_limit=0,  # Stage 1: no S_C
    )
    simulator = OfflineSimulator(requests, strategy, config_dict)
    results["Greedy-Online"] = simulator.run().to_dict()

    # 3. Primal-Dual Online (EMA baseline)
    logger.info("\n--- Running Primal-Dual Online (EMA) ---")
    quota_mgr = QuotaManager(daily_quota)
    strategy = PrimalDualOnlineStrategy(
        cost_calculator,
        quota_mgr,
        config_dict,
        daily_quota=daily_quota,
        sq_min_value=L,
        sq_max_value=U,
        concurrency_limit=0,  # Stage 1: no S_C
    )
    simulator = OfflineSimulator(requests, strategy, config_dict)
    results["PrimalDual-Online"] = simulator.run().to_dict()

    # 3b. PD-Hist (Ablation: PD decision rule with Histogram predictor)
    logger.info("\n--- Running PD-Hist (Ablation) ---")
    quota_mgr = QuotaManager(daily_quota)
    strategy = PrimalDualOnlineStrategy(
        cost_calculator,
        quota_mgr,
        config_dict,
        daily_quota=daily_quota,
        sq_min_value=L,
        sq_max_value=U,
        concurrency_limit=0,  # Stage 1: no S_C
        output_predictor=HistogramOutputPredictor(),  # Use Histogram instead of EMA
    )
    simulator = OfflineSimulator(requests, strategy, config_dict)
    results["PD-Hist"] = simulator.run().to_dict()

    # 4. LA-PD (P10) - Learning-Augmented with conservative quantile
    logger.info("\n--- Running LA-PD (P10) ---")
    quota_mgr = QuotaManager(daily_quota)
    lapd_config_p10 = LAPDConfig(quantile_for_lcb=0.10)
    strategy = LearningAugmentedPrimalDualStrategy(
        cost_calculator,
        quota_mgr,
        config_dict,
        daily_quota=daily_quota,
        sq_min_value=L,
        sq_max_value=U,
        lapd_config=lapd_config_p10,
    )
    simulator = OfflineSimulator(requests, strategy, config_dict)
    result = simulator.run()
    results["LA-PD-P10"] = result.to_dict()
    # Add calibration stats
    if hasattr(strategy, "get_stats"):
        results["LA-PD-P10"]["strategy_stats"] = strategy.get_stats()

    # 5. LA-PD (P20) - Ablation with moderate quantile
    logger.info("\n--- Running LA-PD (P20) ---")
    quota_mgr = QuotaManager(daily_quota)
    lapd_config_p20 = LAPDConfig(quantile_for_lcb=0.20)
    strategy = LearningAugmentedPrimalDualStrategy(
        cost_calculator,
        quota_mgr,
        config_dict,
        daily_quota=daily_quota,
        sq_min_value=L,
        sq_max_value=U,
        lapd_config=lapd_config_p20,
    )
    simulator = OfflineSimulator(requests, strategy, config_dict)
    result = simulator.run()
    results["LA-PD-P20"] = result.to_dict()
    if hasattr(strategy, "get_stats"):
        results["LA-PD-P20"]["strategy_stats"] = strategy.get_stats()

    # 6. LA-PD (P50) - Ablation with aggressive quantile
    logger.info("\n--- Running LA-PD (P50) ---")
    quota_mgr = QuotaManager(daily_quota)
    lapd_config_p50 = LAPDConfig(quantile_for_lcb=0.50)
    strategy = LearningAugmentedPrimalDualStrategy(
        cost_calculator,
        quota_mgr,
        config_dict,
        daily_quota=daily_quota,
        sq_min_value=L,
        sq_max_value=U,
        lapd_config=lapd_config_p50,
    )
    simulator = OfflineSimulator(requests, strategy, config_dict)
    result = simulator.run()
    results["LA-PD-P50"] = result.to_dict()
    if hasattr(strategy, "get_stats"):
        results["LA-PD-P50"]["strategy_stats"] = strategy.get_stats()

    # 7. LA-EMA (Ablation: LA-PD decision rule with EMA predictor)
    logger.info("\n--- Running LA-EMA (Ablation) ---")
    quota_mgr = QuotaManager(daily_quota)
    lapd_config_ema = LAPDConfig(quantile_for_lcb=0.10)
    strategy = LearningAugmentedPrimalDualStrategy(
        cost_calculator,
        quota_mgr,
        config_dict,
        daily_quota=daily_quota,
        sq_min_value=L,
        sq_max_value=U,
        lapd_config=lapd_config_ema,
        output_predictor=EMAOutputPredictor(),  # Use EMA instead of Histogram
    )
    simulator = OfflineSimulator(requests, strategy, config_dict)
    result = simulator.run()
    results["LA-EMA"] = result.to_dict()
    if hasattr(strategy, "get_stats"):
        results["LA-EMA"]["strategy_stats"] = strategy.get_stats()

    # 8. Offline Optimal
    logger.info("\n--- Running Offline Optimal ---")
    quota_mgr = QuotaManager(daily_quota)
    strategy = OptimalStrategy(cost_calculator, quota_mgr, config_dict)
    simulator = OfflineSimulator(requests, strategy, config_dict)
    results["Offline-Optimal"] = simulator.run().to_dict()

    # Calculate competitive ratios and savings
    optimal_cost = results["Offline-Optimal"]["costs"]["total"]
    all_api_cost = results["All-API"]["costs"]["total"]
    optimal_savings = all_api_cost - optimal_cost

    logger.info("\n" + "=" * 60)
    logger.info("Stage 1 Results Summary")
    logger.info("=" * 60)

    for name, res in results.items():
        total_cost = res["costs"]["total"]
        api_cost = res["costs"]["api"]
        ratio = total_cost / optimal_cost if optimal_cost > 0 else float("inf")
        savings = all_api_cost - total_cost
        savings_ratio = savings / optimal_savings if optimal_savings > 0 else 0.0
        logger.info(
            f"{name:20s}: Total=${total_cost:8.2f}, API=${api_cost:8.2f}, "
            f"Ratio={ratio:.4f}, Savings={savings_ratio:.1%}, Quota={res['quota_utilization']:.1%}"
        )

    # Print calibration stats for LA-PD strategies
    logger.info("\n" + "-" * 60)
    logger.info("Prediction Calibration (LA-PD strategies)")
    logger.info("-" * 60)
    for name, res in results.items():
        if "strategy_stats" in res and "predictor_calibration" in res["strategy_stats"]:
            cal = res["strategy_stats"]["predictor_calibration"]
            logger.info(
                f"{name:20s}: q10_cov={cal['q10_coverage']:.1%}, "
                f"q50_cov={cal['q50_coverage']:.1%}, q90_cov={cal['q90_coverage']:.1%}, "
                f"samples={cal['total']}"
            )

    # Save results
    output_file = output_dir / "stage1_online_results.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    logger.info(f"\nResults saved to {output_file}")

    return results


def run_stage2_experiments(
    config: ExperimentConfig,
    data_path: str,
    output_dir: Path,
    model_override: str | None = None,
):
    """Run Stage 2 experiments: S_Q + S_C + S_A (joint optimization).

    Args:
        config: Experiment configuration
        data_path: Path to dataset (rednote/freeinference with latency)
        output_dir: Output directory for results
        model_override: If set, map all requests to this model for pricing
    """
    logger.info("=" * 60)
    logger.info("Stage 2 Experiments: Joint Optimization (S_Q + S_C + S_A)")
    logger.info("=" * 60)

    # Load data
    loader = DataLoader(config.to_dict())
    requests = loader.load(data_path, model_override=model_override)
    stats = loader.get_statistics(requests)

    if model_override:
        logger.info(f"Using model override: {model_override}")
    logger.info(f"Loaded {stats['total_requests']} requests over {stats['num_days']} days")

    # Verify latency data
    has_latency = sum(1 for r in requests if r.latency_ms is not None)
    logger.info(
        f"Requests with latency: {has_latency}/{len(requests)} ({100*has_latency/len(requests):.1f}%)"
    )

    if has_latency < len(requests) * 0.9:
        logger.warning(
            "Less than 90% of requests have latency data. Stage 2 results may be inaccurate."
        )

    # Get configuration
    config_dict = config.to_dict()
    num_subscriptions = config_dict["simulation"]["num_subscriptions"]
    subscription_provider = config.get_subscription_provider()
    daily_quota = subscription_provider.daily_quota * num_subscriptions

    # S_C configuration (from config or defaults)
    subscriptions = config_dict.get("subscriptions", {})
    featherless_config = subscriptions.get("featherless", {})
    concurrency_limit = featherless_config.get("concurrency_limit", 8)

    logger.info(f"Daily quota (S_Q): {daily_quota}")
    logger.info(f"Concurrency limit (S_C): {concurrency_limit}")

    _validate_model_pricing_coverage(requests, config_dict.get("model_pricing"), model_override)

    # Calculate cost statistics
    cost_calculator = CostCalculator(config_dict["providers"], config_dict.get("model_pricing"))
    costs = [_safe_get_request_cost(cost_calculator, r) for r in requests[:10000]]
    costs.sort()
    L = costs[int(len(costs) * 0.05)] if costs else 0.0001
    U = costs[int(len(costs) * 0.95)] if costs else 0.01
    logger.info(f"Estimated cost bounds: L={L:.6f}, U={U:.6f}")

    results = {}

    # 1. All-API Baseline
    logger.info("\n--- Running All-API Baseline ---")
    quota_mgr = QuotaManager(daily_quota)
    strategy = AllAPIStrategy(cost_calculator, quota_mgr, config_dict)
    simulator = OfflineSimulator(requests, strategy, config_dict)
    results["All-API"] = simulator.run().to_dict()

    # 2. Greedy Online (S_Q + S_C)
    logger.info("\n--- Running Greedy Online (Stage 2) ---")
    quota_mgr = QuotaManager(daily_quota)
    strategy = GreedyOnlineStrategy(
        cost_calculator,
        quota_mgr,
        config_dict,
        daily_quota=daily_quota,
        concurrency_limit=concurrency_limit,
    )
    simulator = OfflineSimulator(requests, strategy, config_dict)
    results["Greedy-Online-Stage2"] = simulator.run().to_dict()

    # 3. Primal-Dual Online (S_Q + S_C)
    logger.info("\n--- Running Primal-Dual Online (Stage 2) ---")
    quota_mgr = QuotaManager(daily_quota)
    strategy = PrimalDualOnlineStrategy(
        cost_calculator,
        quota_mgr,
        config_dict,
        daily_quota=daily_quota,
        sq_min_value=L,
        sq_max_value=U,
        concurrency_limit=concurrency_limit,
        queue_capacity=concurrency_limit * 2,  # K = 2C
    )
    simulator = OfflineSimulator(requests, strategy, config_dict)
    results["PrimalDual-Online-Stage2"] = simulator.run().to_dict()

    # 4. LA-PD Unified (S_Q + S_C with learning)
    logger.info("\n--- Running LA-PD Unified (Stage 2) ---")
    quota_mgr = QuotaManager(daily_quota)
    lapd_config = LAPDConfig(quantile_for_lcb=0.10)
    strategy = LearningAugmentedUnifiedStrategy(
        cost_calculator,
        quota_mgr,
        config_dict,
        daily_quota=daily_quota,
        sq_min_value=L,
        sq_max_value=U,
        concurrency_limit=concurrency_limit,
        queue_capacity=concurrency_limit * 2,
        lapd_config=lapd_config,
    )
    simulator = OfflineSimulator(requests, strategy, config_dict)
    result = simulator.run()
    results["LA-PD-Unified-Stage2"] = result.to_dict()
    if hasattr(strategy, "get_stats"):
        results["LA-PD-Unified-Stage2"]["strategy_stats"] = strategy.get_stats()

    # 5. Offline Optimal (ILP) - if available
    try:
        from experiment.strategies.stage2_optimal import ILPOptimalStrategy

        logger.info("\n--- Running Offline Optimal (ILP) ---")
        quota_mgr = QuotaManager(daily_quota)
        strategy = ILPOptimalStrategy(
            cost_calculator,
            quota_mgr,
            config_dict,
            daily_quota=daily_quota,
            concurrency_limit=concurrency_limit,
        )
        simulator = OfflineSimulator(requests, strategy, config_dict)
        results["Offline-Optimal-ILP"] = simulator.run().to_dict()
    except ImportError:
        logger.warning("ILPOptimalStrategy not available, skipping offline optimal")
    except Exception as e:
        logger.warning(f"ILP optimization failed: {e}")

    # Calculate competitive ratios and savings
    optimal_key = "Offline-Optimal-ILP" if "Offline-Optimal-ILP" in results else "All-API"
    optimal_cost = results[optimal_key]["costs"]["total"]
    all_api_cost = results["All-API"]["costs"]["total"]
    optimal_savings = all_api_cost - optimal_cost if optimal_key != "All-API" else 0.0

    logger.info("\n" + "=" * 60)
    logger.info("Stage 2 Results Summary")
    logger.info("=" * 60)

    for name, res in results.items():
        total_cost = res["costs"]["total"]
        api_cost = res["costs"]["api"]
        ratio = total_cost / optimal_cost if optimal_cost > 0 else float("inf")
        savings = all_api_cost - total_cost
        savings_ratio = savings / optimal_savings if optimal_savings > 0 else 0.0
        logger.info(
            f"{name:25s}: Total=${total_cost:8.2f}, API=${api_cost:8.2f}, "
            f"Ratio={ratio:.4f}, Savings={savings_ratio:.1%}"
        )

    # Print calibration stats for LA-PD strategies
    logger.info("\n" + "-" * 60)
    logger.info("Prediction Calibration (LA-PD strategies)")
    logger.info("-" * 60)
    for name, res in results.items():
        if "strategy_stats" in res and "output_calibration" in res["strategy_stats"]:
            cal = res["strategy_stats"]["output_calibration"]
            logger.info(
                f"{name:25s}: q10_cov={cal['q10_coverage']:.1%}, "
                f"q50_cov={cal['q50_coverage']:.1%}, q90_cov={cal['q90_coverage']:.1%}"
            )

    # Save results
    output_file = output_dir / "stage2_online_results.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    logger.info(f"\nResults saved to {output_file}")

    return results


def main():
    """Run online routing experiments with configurable strategies."""
    parser = argparse.ArgumentParser(description="Run online routing experiments")
    parser.add_argument(
        "--stage",
        type=str,
        choices=["1", "2", "both"],
        default="1",
        help="Which stage to run (1, 2, or both)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config/experiment.yaml",
        help="Path to experiment config",
    )
    parser.add_argument(
        "--stage1-data",
        type=str,
        default="data/BurstGPT_1.csv",
        help="Dataset for Stage 1 (BurstGPT)",
    )
    parser.add_argument(
        "--stage2-data",
        type=str,
        default="data/rednote_logs.csv",
        help="Dataset for Stage 2 (with latency)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="experiment/results/online",
        help="Output directory for results",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Target model for pricing. Use this to treat a dataset as single-model workload. "
        "E.g., '--model deepseek-r1' maps all BurstGPT requests to deepseek-r1 pricing.",
    )

    args = parser.parse_args()

    # Load config
    config = ExperimentConfig(args.config)

    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Run experiments
    try:
        if args.stage in ["1", "both"]:
            if Path(args.stage1_data).exists():
                run_stage1_experiments(config, args.stage1_data, output_dir, args.model)
            else:
                logger.error(f"Stage 1 data not found: {args.stage1_data}")

        if args.stage in ["2", "both"]:
            if Path(args.stage2_data).exists():
                run_stage2_experiments(config, args.stage2_data, output_dir, args.model)
            else:
                logger.error(f"Stage 2 data not found: {args.stage2_data}")
    except ValueError as e:
        logger.error(str(e))
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
