#!/usr/bin/env python3
"""Generate plots for Stage 1 experiments.

Creates:
- Fig 1: Cost comparison bar chart
- Fig 2: Parameter sensitivity (Q sweep)
- Fig 3: Competitive ratio analysis
"""

import json
from pathlib import Path

import matplotlib.pyplot as plt

# Style settings
plt.style.use("seaborn-v0_8-whitegrid")
plt.rcParams["font.size"] = 12
plt.rcParams["axes.labelsize"] = 14
plt.rcParams["axes.titlesize"] = 16
plt.rcParams["figure.figsize"] = (10, 6)


def plot_fig1_cost_comparison(results: dict, output_dir: Path, dataset_name: str):
    """Plot Fig 1: Cost comparison stacked bar chart (Subscription + API)."""
    fig1 = results["fig1_cost_comparison"]

    strategies = ["All-API", "Greedy", "Optimal"]

    # Extract component costs
    sub_costs = [
        fig1["all_api"]["costs"]["subscription"],
        fig1["greedy"]["costs"]["subscription"],
        fig1["optimal"]["costs"]["subscription"],
    ]
    api_costs = [
        fig1["all_api"]["costs"]["api"],
        fig1["greedy"]["costs"]["api"],
        fig1["optimal"]["costs"]["api"],
    ]
    total_costs = [
        fig1["all_api"]["costs"]["total"],
        fig1["greedy"]["costs"]["total"],
        fig1["optimal"]["costs"]["total"],
    ]
    savings = [
        0,
        fig1["greedy"]["savings_vs_all_api"],
        fig1["optimal"]["savings_vs_all_api"],
    ]

    fig, ax = plt.subplots(figsize=(8, 6))

    # Stacked bars
    # Subscription (bottom)
    # Using different colors for distinction
    ax.bar(
        strategies,
        sub_costs,
        label="Subscription Cost",
        color="#2ecc71",
        edgecolor="black",
        linewidth=1,
    )
    # API (top)
    ax.bar(
        strategies,
        api_costs,
        bottom=sub_costs,
        label="API Cost",
        color="#e74c3c",
        edgecolor="black",
        linewidth=1,
    )

    # Add total labels on top
    for i, (total, saving) in enumerate(zip(total_costs, savings, strict=False)):
        ax.annotate(
            f"${total:.2f}",
            xy=(i, total),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=12,
            fontweight="bold",
        )
        if saving > 0:
            ax.annotate(
                f"(-{saving:.1f}%)",
                xy=(i, total),
                xytext=(0, 18),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=10,
                color="green",
            )

    ax.set_ylabel("Total Cost ($)")
    ax.set_title(f"Stage 1: Cost Comparison ({dataset_name})")
    ax.set_ylim(0, max(total_costs) * 1.25)
    ax.legend(loc="upper right")

    plt.tight_layout()
    plt.savefig(
        output_dir / f"{dataset_name}_fig1_cost_comparison.pdf", dpi=300, bbox_inches="tight"
    )
    plt.savefig(
        output_dir / f"{dataset_name}_fig1_cost_comparison.png", dpi=300, bbox_inches="tight"
    )
    plt.close()
    print(f'Saved Fig 1: {output_dir / f"{dataset_name}_fig1_cost_comparison.pdf"}')


def plot_fig2_parameter_sensitivity(results: dict, output_dir: Path, dataset_name: str):
    """Plot Fig 2: Parameter sensitivity (Q sweep)."""
    fig2 = results["fig2_parameter_sensitivity"]

    q_values = fig2["q_values"]
    all_api = fig2["all_api"]
    greedy = fig2["greedy"]
    optimal = fig2["optimal"]

    fig, ax = plt.subplots(figsize=(10, 6))

    ax.plot(q_values, all_api, "o-", label="All-API", color="#e74c3c", linewidth=2, markersize=8)
    ax.plot(q_values, greedy, "s-", label="Greedy", color="#f39c12", linewidth=2, markersize=8)
    ax.plot(q_values, optimal, "^-", label="Optimal", color="#27ae60", linewidth=2, markersize=8)

    ax.set_xlabel("Daily Quota (Q)")
    ax.set_ylabel("Total Cost ($)")
    ax.set_title(f"Stage 1: Parameter Sensitivity ({dataset_name})")
    ax.legend(loc="upper right", fontsize=12)
    ax.set_xlim(0, max(q_values) * 1.05)
    ax.set_ylim(0, max(all_api) * 1.1)

    # Add grid
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(
        output_dir / f"{dataset_name}_fig2_parameter_sensitivity.pdf", dpi=300, bbox_inches="tight"
    )
    plt.savefig(
        output_dir / f"{dataset_name}_fig2_parameter_sensitivity.png", dpi=300, bbox_inches="tight"
    )
    plt.close()
    print(f'Saved Fig 2: {output_dir / f"{dataset_name}_fig2_parameter_sensitivity.pdf"}')


def plot_fig3_competitive_ratio(results: dict, output_dir: Path, dataset_name: str):
    """Plot Fig 3: Competitive ratio analysis."""
    fig3 = results["fig3_competitive_ratio"]

    q_values = fig3["q_values"]
    ratios = fig3["competitive_ratios"]

    fig, ax = plt.subplots(figsize=(10, 6))

    ax.plot(
        q_values,
        ratios,
        "o-",
        color="#3498db",
        linewidth=2,
        markersize=10,
        label="Greedy / Optimal",
    )
    ax.axhline(y=1.0, color="#27ae60", linestyle="--", linewidth=2, label="Optimal (ratio = 1.0)")

    # Fill area above 1.0
    ax.fill_between(q_values, 1.0, ratios, alpha=0.3, color="#e74c3c", label="Suboptimality gap")

    ax.set_xlabel("Daily Quota (Q)")
    ax.set_ylabel("Competitive Ratio (Greedy / Optimal)")
    ax.set_title(f"Stage 1: Competitive Ratio Analysis ({dataset_name})")
    ax.legend(loc="upper left", fontsize=11)
    ax.set_xlim(0, max(q_values) * 1.05)
    ax.set_ylim(0.9, max(ratios) * 1.1)

    # Add annotations
    ax.annotate(
        f'Avg: {fig3["avg_ratio"]:.2f}x',
        xy=(0.95, 0.95),
        xycoords="axes fraction",
        ha="right",
        va="top",
        fontsize=12,
        bbox={"boxstyle": "round", "facecolor": "wheat", "alpha": 0.5},
    )

    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(
        output_dir / f"{dataset_name}_fig3_competitive_ratio.pdf", dpi=300, bbox_inches="tight"
    )
    plt.savefig(
        output_dir / f"{dataset_name}_fig3_competitive_ratio.png", dpi=300, bbox_inches="tight"
    )
    plt.close()
    print(f'Saved Fig 3: {output_dir / f"{dataset_name}_fig3_competitive_ratio.pdf"}')


def main():
    """Main entry point."""
    # Find results files
    results_dir = Path("experiment/results/stage1")
    output_dir = results_dir / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Process each results file
    for results_file in results_dir.glob("*_results.json"):
        dataset_name = results_file.stem.replace("_results", "")
        print(f"\nProcessing {dataset_name}...")

        with open(results_file) as f:
            results = json.load(f)

        plot_fig1_cost_comparison(results, output_dir, dataset_name)
        plot_fig2_parameter_sensitivity(results, output_dir, dataset_name)
        plot_fig3_competitive_ratio(results, output_dir, dataset_name)

    print(f"\nAll plots saved to {output_dir}")


if __name__ == "__main__":
    main()
