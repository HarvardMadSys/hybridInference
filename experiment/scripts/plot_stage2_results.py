#!/usr/bin/env python3
"""Plot Stage 2 (Dual Subscription) results.

Generates visualizations for:
1. Total Cost Comparison (Bar Chart)
2. Cost Breakdown (Stacked Bar: Subscription vs API)
"""

import json
import logging
from pathlib import Path

import matplotlib.pyplot as plt
import seaborn as sns

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def load_results(results_path: str) -> dict:
    """Load results from JSON file."""
    with open(results_path) as f:
        return json.load(f)


def plot_cost_comparison(results: dict, output_dir: Path):
    """Plot total cost comparison."""
    strategies = []
    costs = []

    # Mapping for cleaner names
    name_map = {
        "ilp_optimal": "ILP Optimal",
        "daily_quota_only": "Daily Quota Only",
        "concurrency_only": "Concurrency Only",
        "sequential_daily_first": "Seq (Daily First)",
        "sequential_concurrency_first": "Seq (Conc. First)",
    }

    for key, data in results.items():
        strategies.append(name_map.get(key, key))
        costs.append(data["costs"]["total"])

    plt.figure(figsize=(10, 6))
    sns.set_style("whitegrid")

    # Create bar plot
    ax = sns.barplot(x=strategies, y=costs, palette="viridis")

    # Add labels
    plt.title("Total Cost Comparison - Stage 2 (Dual Subscription)", fontsize=14)
    plt.ylabel("Total Cost ($)", fontsize=12)
    plt.xlabel("Strategy", fontsize=12)
    plt.xticks(rotation=45)

    # Add value labels on top of bars
    for i, v in enumerate(costs):
        ax.text(i, v + 0.5, f"${v:.2f}", ha="center", va="bottom", fontweight="bold")

    plt.tight_layout()
    output_path = output_dir / "stage2_cost_comparison.png"
    plt.savefig(output_path, dpi=300)
    logger.info(f"Saved cost comparison plot to {output_path}")
    plt.close()


def plot_cost_breakdown(results: dict, output_dir: Path):
    """Plot cost breakdown (Subscription vs API)."""
    strategies = []
    sub_costs = []
    api_costs = []

    name_map = {
        "ilp_optimal": "ILP Optimal",
        "daily_quota_only": "Daily Quota Only",
        "concurrency_only": "Concurrency Only",
        "sequential_daily_first": "Seq (Daily First)",
        "sequential_concurrency_first": "Seq (Conc. First)",
    }

    for key, data in results.items():
        strategies.append(name_map.get(key, key))
        sub_costs.append(data["costs"]["subscription"])
        api_costs.append(data["costs"]["api"])

    plt.figure(figsize=(10, 6))
    sns.set_style("whitegrid")

    # Create stacked bar plot
    plt.bar(strategies, sub_costs, label="Subscription Cost")
    plt.bar(strategies, api_costs, bottom=sub_costs, label="API Cost")

    plt.title("Cost Breakdown - Stage 2", fontsize=14)
    plt.ylabel("Cost ($)", fontsize=12)
    plt.xlabel("Strategy", fontsize=12)
    plt.xticks(rotation=45)
    plt.legend()

    plt.tight_layout()
    output_path = output_dir / "stage2_cost_breakdown.png"
    plt.savefig(output_path, dpi=300)
    logger.info(f"Saved cost breakdown plot to {output_path}")
    plt.close()


def main():
    """Generate plots from Stage 2 experiment results."""
    results_path = "experiment/results/stage2/stage2_results.json"
    output_dir = Path("experiment/results/stage2/plots")
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        results = load_results(results_path)
        plot_cost_comparison(results, output_dir)
        plot_cost_breakdown(results, output_dir)
    except FileNotFoundError:
        logger.error(
            f"Results file not found: {results_path}. Please run run_stage2_experiments.py first."
        )
    except Exception as e:
        logger.error(f"Failed to generate plots: {e}")


if __name__ == "__main__":
    main()
