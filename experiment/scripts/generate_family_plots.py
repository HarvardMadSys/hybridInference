#!/usr/bin/env python3
"""Generate cross-family comparison plots.

This script creates plots comparing routing performance across model families:
- Cost comparison across families
- Competitive ratio by family
- Savings analysis by family
"""

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


FAMILIES = ["Llama", "Gemini", "GLM", "DeepSeek"]


def load_family_results():
    """Load results for all families."""
    results_dir = project_root / "experiment" / "results"

    family_results = {}
    for family in FAMILIES:
        family_results[family] = {}
        for strategy in ["all-api", "greedy", "optimal"]:
            filepath = results_dir / f"{family.lower()}_{strategy}.json"
            if filepath.exists():
                with open(filepath) as f:
                    family_results[family][strategy] = json.load(f)

    return family_results


def plot_cost_by_family(results, output_dir):
    """Figure 6: Cost comparison across families."""
    fig, ax = plt.subplots(figsize=(12, 6))

    x = np.arange(len(FAMILIES))
    width = 0.25

    all_api_costs = []
    greedy_costs = []
    optimal_costs = []

    for family in FAMILIES:
        all_api_costs.append(results[family].get("all-api", {}).get("costs", {}).get("total", 0))
        greedy_costs.append(results[family].get("greedy", {}).get("costs", {}).get("total", 0))
        optimal_costs.append(results[family].get("optimal", {}).get("costs", {}).get("total", 0))

    ax.bar(
        x - width,
        all_api_costs,
        width,
        label="All-API",
        color="#e74c3c",
        alpha=0.8,
        edgecolor="black",
        linewidth=1.5,
    )
    ax.bar(
        x,
        greedy_costs,
        width,
        label="Greedy",
        color="#f39c12",
        alpha=0.8,
        edgecolor="black",
        linewidth=1.5,
    )
    ax.bar(
        x + width,
        optimal_costs,
        width,
        label="Optimal",
        color="#27ae60",
        alpha=0.8,
        edgecolor="black",
        linewidth=1.5,
    )

    ax.set_ylabel("Total Cost ($)", fontsize=12, fontweight="bold")
    ax.set_title("Cost Comparison Across Model Families", fontsize=13, fontweight="bold", pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels(FAMILIES, fontsize=11)
    ax.legend(fontsize=11, loc="upper left")
    ax.grid(axis="y", alpha=0.3, linestyle="--")

    plt.tight_layout()
    plt.savefig(output_dir / "fig6_cost_by_family.png", dpi=300, bbox_inches="tight")
    print("✓ Generated: fig6_cost_by_family.png")
    plt.close()


def plot_competitive_ratio_by_family(results, output_dir):
    """Figure 7: Competitive ratio across families."""
    fig, ax = plt.subplots(figsize=(10, 6))

    x = np.arange(len(FAMILIES))
    width = 0.35

    greedy_ratios = []

    for family in FAMILIES:
        optimal_cost = results[family].get("optimal", {}).get("costs", {}).get("total", 1)
        greedy_cost = results[family].get("greedy", {}).get("costs", {}).get("total", 0)
        ratio = greedy_cost / optimal_cost if optimal_cost > 0 else 0
        greedy_ratios.append(ratio)

    bars = ax.bar(
        x, greedy_ratios, width, color="#f39c12", alpha=0.8, edgecolor="black", linewidth=1.5
    )

    # Add value labels
    for bar, ratio in zip(bars, greedy_ratios, strict=False):
        height = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2.0,
            height,
            f"{ratio:.3f}x",
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
        )

    ax.axhline(y=1.0, color="green", linestyle="--", linewidth=2, alpha=0.7, label="Optimal (1.0x)")
    ax.set_ylabel("Competitive Ratio", fontsize=12, fontweight="bold")
    ax.set_title(
        "Greedy Competitive Ratio Across Model Families", fontsize=13, fontweight="bold", pad=15
    )
    ax.set_xticks(x)
    ax.set_xticklabels(FAMILIES, fontsize=11)
    ax.set_ylim(0.9, max(greedy_ratios) * 1.1)
    ax.legend(fontsize=10)
    ax.grid(axis="y", alpha=0.3, linestyle="--")

    plt.tight_layout()
    plt.savefig(output_dir / "fig7_competitive_ratio_by_family.png", dpi=300, bbox_inches="tight")
    print("✓ Generated: fig7_competitive_ratio_by_family.png")
    plt.close()


def plot_savings_by_family(results, output_dir):
    """Figure 8: Savings analysis across families."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    families = FAMILIES
    absolute_savings = []
    percentage_savings = []

    for family in families:
        all_api_cost = results[family].get("all-api", {}).get("costs", {}).get("total", 0)
        optimal_cost = results[family].get("optimal", {}).get("costs", {}).get("total", 0)
        savings = all_api_cost - optimal_cost
        savings_pct = (savings / all_api_cost * 100) if all_api_cost > 0 else 0

        absolute_savings.append(savings)
        percentage_savings.append(savings_pct)

    # Absolute savings
    colors = ["#3498db", "#9b59b6", "#e67e22", "#1abc9c"]
    bars1 = ax1.bar(
        families, absolute_savings, color=colors, alpha=0.8, edgecolor="black", linewidth=1.5
    )

    for bar, save in zip(bars1, absolute_savings, strict=False):
        height = bar.get_height()
        ax1.text(
            bar.get_x() + bar.get_width() / 2.0,
            height,
            f"${save:.2f}",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
        )

    ax1.set_ylabel("Cost Savings ($)", fontsize=12, fontweight="bold")
    ax1.set_title("Absolute Savings (Optimal vs All-API)", fontsize=12, fontweight="bold")
    ax1.grid(axis="y", alpha=0.3, linestyle="--")

    # Percentage savings
    bars2 = ax2.bar(
        families, percentage_savings, color=colors, alpha=0.8, edgecolor="black", linewidth=1.5
    )

    for bar, pct in zip(bars2, percentage_savings, strict=False):
        height = bar.get_height()
        ax2.text(
            bar.get_x() + bar.get_width() / 2.0,
            height,
            f"{pct:.1f}%",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
        )

    ax2.set_ylabel("Cost Savings (%)", fontsize=12, fontweight="bold")
    ax2.set_title("Percentage Savings (Optimal vs All-API)", fontsize=12, fontweight="bold")
    ax2.grid(axis="y", alpha=0.3, linestyle="--")

    plt.tight_layout()
    plt.savefig(output_dir / "fig8_savings_by_family.png", dpi=300, bbox_inches="tight")
    print("✓ Generated: fig8_savings_by_family.png")
    plt.close()


def main():
    """Generate all cross-family plots."""
    print("=" * 70)
    print("Generating Cross-Family Comparison Plots")
    print("=" * 70)

    # Load results
    print("\nLoading results...")
    results = load_family_results()
    print(f"✓ Loaded results for {len(results)} families")

    # Create output directory
    output_dir = project_root / "experiment" / "results" / "figures"
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"✓ Output directory: {output_dir}")

    # Generate plots
    print("\nGenerating figures...")
    plot_cost_by_family(results, output_dir)
    plot_competitive_ratio_by_family(results, output_dir)
    plot_savings_by_family(results, output_dir)

    print("\n" + "=" * 70)
    print("Cross-family figures generated successfully!")
    print("=" * 70)
    print(f"\nFigures saved to: {output_dir}")
    print("\nGenerated files:")
    print("  - fig6_cost_by_family.png")
    print("  - fig7_competitive_ratio_by_family.png")
    print("  - fig8_savings_by_family.png")
    print()


if __name__ == "__main__":
    main()
