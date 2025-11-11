#!/usr/bin/env python3
"""Generate plots for offline routing experiment results.

This script creates publication-quality figures for OSDI submission:
- Cost comparison bar chart
- Quota utilization comparison
- Cost breakdown (subscription vs API)
- Savings analysis
"""

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


def load_results():
    """Load simulation results from JSON files."""
    project_root = Path(__file__).parent.parent.parent
    results_dir = project_root / "experiment" / "results"

    with open(results_dir / "chatgpt_all_api.json") as f:
        all_api = json.load(f)
    with open(results_dir / "chatgpt_greedy.json") as f:
        greedy = json.load(f)
    with open(results_dir / "chatgpt_only_optimal.json") as f:
        optimal = json.load(f)

    return {
        "all_api": all_api,
        "greedy": greedy,
        "optimal": optimal,
    }


def plot_cost_comparison(results, output_dir):
    """Figure 1: Total cost comparison."""
    strategies = list(results.keys())
    costs = [results[s]["costs"]["total"] for s in strategies]

    fig, ax = plt.subplots(figsize=(8, 5))

    colors = ["#e74c3c", "#f39c12", "#27ae60"]  # Red, Orange, Green
    bars = ax.bar(strategies, costs, color=colors, alpha=0.8, edgecolor="black", linewidth=1.5)

    # Add value labels on bars
    for bar, cost in zip(bars, costs, strict=False):
        height = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2.0,
            height,
            f"${cost:.2f}",
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
        )

    ax.set_ylabel("Total Cost ($)", fontsize=12, fontweight="bold")
    ax.set_title(
        "Cost Comparison: ChatGPT Workload (1.19M requests, 61 days)",
        fontsize=13,
        fontweight="bold",
        pad=15,
    )
    ax.grid(axis="y", alpha=0.3, linestyle="--")
    ax.set_ylim(0, max(costs) * 1.15)

    plt.tight_layout()
    plt.savefig(output_dir / "fig1_cost_comparison.png", dpi=300, bbox_inches="tight")
    print("✓ Generated: fig1_cost_comparison.png")
    plt.close()


def plot_cost_breakdown(results, output_dir):
    """Figure 2: Cost breakdown (subscription vs API)."""
    strategies = ["Greedy", "Optimal"]  # Exclude All-API (no subscription)

    subscription_costs = [results[s]["costs"]["subscription"] for s in strategies]
    api_costs = [results[s]["costs"]["api"] for s in strategies]

    fig, ax = plt.subplots(figsize=(8, 5))

    x = np.arange(len(strategies))
    width = 0.5

    ax.bar(
        x,
        subscription_costs,
        width,
        label="Subscription",
        color="#3498db",
        alpha=0.8,
        edgecolor="black",
        linewidth=1.5,
    )
    ax.bar(
        x,
        api_costs,
        width,
        bottom=subscription_costs,
        label="API",
        color="#e74c3c",
        alpha=0.8,
        edgecolor="black",
        linewidth=1.5,
    )

    # Add total cost labels
    for i, strategy in enumerate(strategies):
        total = results[strategy]["costs"]["total"]
        ax.text(i, total, f"${total:.2f}", ha="center", va="bottom", fontsize=11, fontweight="bold")

    ax.set_ylabel("Cost ($)", fontsize=12, fontweight="bold")
    ax.set_title("Cost Breakdown: Subscription vs API", fontsize=13, fontweight="bold", pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels(strategies)
    ax.legend(fontsize=11, loc="upper right")
    ax.grid(axis="y", alpha=0.3, linestyle="--")

    plt.tight_layout()
    plt.savefig(output_dir / "fig2_cost_breakdown.png", dpi=300, bbox_inches="tight")
    print("✓ Generated: fig2_cost_breakdown.png")
    plt.close()


def plot_quota_utilization(results, output_dir):
    """Figure 3: Quota utilization comparison."""
    strategies = ["Greedy", "Optimal"]
    utilization = [results[s]["quota_utilization"] * 100 for s in strategies]

    fig, ax = plt.subplots(figsize=(8, 5))

    colors = ["#f39c12", "#27ae60"]
    bars = ax.bar(
        strategies, utilization, color=colors, alpha=0.8, edgecolor="black", linewidth=1.5
    )

    # Add value labels
    for bar, util in zip(bars, utilization, strict=False):
        height = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2.0,
            height,
            f"{util:.1f}%",
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
        )

    ax.set_ylabel("Quota Utilization (%)", fontsize=12, fontweight="bold")
    ax.set_title("Average Daily Quota Utilization", fontsize=13, fontweight="bold", pad=15)
    ax.set_ylim(0, 100)
    ax.axhline(y=100, color="red", linestyle="--", alpha=0.5, label="Max Quota")
    ax.grid(axis="y", alpha=0.3, linestyle="--")
    ax.legend(fontsize=10)

    plt.tight_layout()
    plt.savefig(output_dir / "fig3_quota_utilization.png", dpi=300, bbox_inches="tight")
    print("✓ Generated: fig3_quota_utilization.png")
    plt.close()


def plot_savings_analysis(results, output_dir):
    """Figure 4: Savings vs All-API baseline."""
    all_api_cost = results["all_api"]["costs"]["total"]

    strategies = ["Greedy", "Optimal"]
    savings = [all_api_cost - results[s]["costs"]["total"] for s in strategies]
    savings_pct = [s / all_api_cost * 100 for s in savings]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # Absolute savings
    colors = ["#f39c12", "#27ae60"]
    bars1 = ax1.bar(strategies, savings, color=colors, alpha=0.8, edgecolor="black", linewidth=1.5)

    for bar, save in zip(bars1, savings, strict=False):
        height = bar.get_height()
        ax1.text(
            bar.get_x() + bar.get_width() / 2.0,
            height,
            f"${save:.2f}",
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
        )

    ax1.set_ylabel("Cost Savings ($)", fontsize=12, fontweight="bold")
    ax1.set_title("Absolute Savings vs All-API", fontsize=12, fontweight="bold")
    ax1.grid(axis="y", alpha=0.3, linestyle="--")

    # Percentage savings
    bars2 = ax2.bar(
        strategies, savings_pct, color=colors, alpha=0.8, edgecolor="black", linewidth=1.5
    )

    for bar, pct in zip(bars2, savings_pct, strict=False):
        height = bar.get_height()
        ax2.text(
            bar.get_x() + bar.get_width() / 2.0,
            height,
            f"{pct:.1f}%",
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
        )

    ax2.set_ylabel("Cost Savings (%)", fontsize=12, fontweight="bold")
    ax2.set_title("Percentage Savings vs All-API", fontsize=12, fontweight="bold")
    ax2.set_ylim(0, max(savings_pct) * 1.15)
    ax2.grid(axis="y", alpha=0.3, linestyle="--")

    plt.tight_layout()
    plt.savefig(output_dir / "fig4_savings_analysis.png", dpi=300, bbox_inches="tight")
    print("✓ Generated: fig4_savings_analysis.png")
    plt.close()


def plot_competitive_ratio(results, output_dir):
    """Figure 5: Competitive ratio."""
    optimal_cost = results["optimal"]["costs"]["total"]

    strategies = ["all_api", "greedy", "optimal"]
    ratios = [results[s]["costs"]["total"] / optimal_cost for s in strategies]

    fig, ax = plt.subplots(figsize=(8, 5))

    colors = ["#e74c3c", "#f39c12", "#27ae60"]
    bars = ax.bar(strategies, ratios, color=colors, alpha=0.8, edgecolor="black", linewidth=1.5)

    for bar, ratio in zip(bars, ratios, strict=False):
        height = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2.0,
            height,
            f"{ratio:.2f}x",
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
        )

    ax.axhline(y=1.0, color="green", linestyle="--", linewidth=2, alpha=0.7, label="Optimal (1.0x)")
    ax.set_ylabel("Competitive Ratio", fontsize=12, fontweight="bold")
    ax.set_title("Competitive Ratio vs Optimal", fontsize=13, fontweight="bold", pad=15)
    ax.set_ylim(0, max(ratios) * 1.15)
    ax.grid(axis="y", alpha=0.3, linestyle="--")
    ax.legend(fontsize=10)

    plt.tight_layout()
    plt.savefig(output_dir / "fig5_competitive_ratio.png", dpi=300, bbox_inches="tight")
    print("✓ Generated: fig5_competitive_ratio.png")
    plt.close()


def main():
    """Generate all plots."""
    print("=" * 60)
    print("Generating Plots for Offline Routing Experiment")
    print("=" * 60)

    # Load results
    print("\nLoading results...")
    results = load_results()
    print(f"✓ Loaded results for {len(results)} strategies")

    # Create output directory
    output_dir = project_root / "experiment" / "results" / "figures"
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"✓ Output directory: {output_dir}")

    # Generate plots
    print("\nGenerating figures...")
    plot_cost_comparison(results, output_dir)
    plot_cost_breakdown(results, output_dir)
    plot_quota_utilization(results, output_dir)
    plot_savings_analysis(results, output_dir)
    plot_competitive_ratio(results, output_dir)

    print("\n" + "=" * 60)
    print("All figures generated successfully!")
    print("=" * 60)
    print(f"\nFigures saved to: {output_dir}")
    print("\nGenerated files:")
    print("  - fig1_cost_comparison.png")
    print("  - fig2_cost_breakdown.png")
    print("  - fig3_quota_utilization.png")
    print("  - fig4_savings_analysis.png")
    print("  - fig5_competitive_ratio.png")
    print()


if __name__ == "__main__":
    main()
