#!/usr/bin/env python3
"""Plot oracle experiment results: prediction gap decomposition.

Generates a stacked bar chart showing how much of the total competitive ratio
gap is due to online decision-making vs prediction error.
"""

import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

import matplotlib.pyplot as plt
import numpy as np

# ICML-style configuration
plt.rcParams.update(
    {
        "font.family": "serif",
        "font.size": 10,
        "axes.labelsize": 11,
        "axes.titlesize": 12,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "legend.fontsize": 9,
        "figure.dpi": 150,
    }
)

COLORS = {
    "online": "#4472C4",  # Blue - online decision gap
    "prediction": "#ED7D31",  # Orange - prediction gap
    "optimal": "#70AD47",  # Green - optimal baseline
}


def load_results(results_path: str) -> dict:
    """Load oracle experiment results."""
    with open(results_path) as f:
        return json.load(f)


def plot_gap_decomposition(results: dict, output_dir: Path) -> None:
    """Plot stacked bar chart of gap decomposition.

    Args:
        results: Oracle experiment results
        output_dir: Directory to save plots
    """
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), sharey=True)

    datasets = ["burstgpt", "freeinference"]
    titles = ["BurstGPT (single-model)", "FreeInference (multi-model)"]
    plan_names = ["Base", "Plus", "Pro"]
    plan_labels = ["Base\n(Q=300)", "Plus\n(Q=2K)", "Pro\n(Q=5K)"]

    for idx, (ds_name, title) in enumerate(zip(datasets, titles, strict=False)):
        ax = axes[idx]

        if ds_name not in results:
            ax.set_title(f"{title}\n(no data)")
            continue

        ds = results[ds_name]
        online_gaps = []
        pred_gaps = []

        for plan in plan_names:
            if plan not in ds["plans"]:
                online_gaps.append(0)
                pred_gaps.append(0)
                continue
            gap = ds["plans"][plan]["analysis"]["pd_gap_decomposition"]
            online_gaps.append(max(gap["online_gap"], 0))
            pred_gaps.append(max(gap["prediction_gap_ema"], 0))

        x = np.arange(len(plan_names))
        width = 0.5

        ax.bar(
            x,
            online_gaps,
            width,
            label="Online decision gap",
            color=COLORS["online"],
            edgecolor="white",
            linewidth=0.5,
        )
        ax.bar(
            x,
            pred_gaps,
            width,
            bottom=online_gaps,
            label="Prediction error gap",
            color=COLORS["prediction"],
            edgecolor="white",
            linewidth=0.5,
        )

        # Add value labels on bars
        for i, (og, pg) in enumerate(zip(online_gaps, pred_gaps, strict=False)):
            total = og + pg
            if total > 0.01:
                # Online gap label
                if og > 0.015:
                    ax.text(
                        i,
                        og / 2,
                        f"{og:.3f}",
                        ha="center",
                        va="center",
                        fontsize=8,
                        color="white",
                        fontweight="bold",
                    )
                # Prediction gap label
                if pg > 0.005:
                    ax.text(
                        i,
                        og + pg / 2,
                        f"{pg:.3f}",
                        ha="center",
                        va="center",
                        fontsize=8,
                        color="white",
                        fontweight="bold",
                    )
                # Percentage label on top
                pct = pg / total * 100 if total > 0 else 0
                ax.text(
                    i,
                    total + 0.005,
                    f"{pct:.0f}%",
                    ha="center",
                    va="bottom",
                    fontsize=8,
                    color=COLORS["prediction"],
                    fontweight="bold",
                )

        ax.set_xlabel("Subscription Plan")
        ax.set_xticks(x)
        ax.set_xticklabels(plan_labels)
        ax.set_title(title)

        if idx == 0:
            ax.set_ylabel("Competitive Ratio Gap (CR - 1.0)")
            ax.legend(loc="upper left", framealpha=0.9)

    plt.suptitle(
        "Gap Decomposition: Online Decision vs Prediction Error (PD-EMA)",
        fontsize=12,
        fontweight="bold",
        y=1.02,
    )
    plt.tight_layout()

    output_path = output_dir / "oracle_gap_decomposition.png"
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    print(f"Saved: {output_path}")
    plt.close()


def plot_cr_comparison(results: dict, output_dir: Path) -> None:
    """Plot CR comparison across all strategies for Pro plan.

    Args:
        results: Oracle experiment results
        output_dir: Directory to save plots
    """
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.5))

    datasets = ["burstgpt", "freeinference"]
    titles = ["BurstGPT (Q=5000)", "FreeInference (Q=5000)"]

    strategy_order = [
        "Optimal",
        "PD-Oracle",
        "PD-EMA",
        "PD-Hist",
        "LA-Oracle",
        "LA-EMA",
        "LA-Hist",
        "Greedy",
    ]
    strategy_colors = {
        "Optimal": "#70AD47",
        "PD-Oracle": "#4472C4",
        "PD-EMA": "#5B9BD5",
        "PD-Hist": "#9DC3E6",
        "LA-Oracle": "#C55A11",
        "LA-EMA": "#ED7D31",
        "LA-Hist": "#F4B183",
        "Greedy": "#A5A5A5",
    }

    for idx, (ds_name, title) in enumerate(zip(datasets, titles, strict=False)):
        ax = axes[idx]

        if ds_name not in results or "Pro" not in results[ds_name]["plans"]:
            ax.set_title(f"{title}\n(no data)")
            continue

        pro = results[ds_name]["plans"]["Pro"]["analysis"]["strategies"]

        crs = []
        colors = []
        labels = []
        for s in strategy_order:
            if s in pro:
                crs.append(pro[s]["cr"])
                colors.append(strategy_colors[s])
                labels.append(s)

        y_pos = np.arange(len(labels))
        bars = ax.barh(y_pos, crs, color=colors, edgecolor="white", linewidth=0.5)

        # Set x-axis to focus on the interesting range
        cr_min = min(crs)
        cr_max = max(crs)
        x_start = max(0.95, cr_min - 0.02)
        x_end = cr_max + 0.06
        ax.set_xlim(x_start, x_end)

        # Add value labels
        for bar, cr in zip(bars, crs, strict=False):
            ax.text(
                bar.get_width() + 0.003,
                bar.get_y() + bar.get_height() / 2,
                f"{cr:.4f}x",
                va="center",
                fontsize=8,
            )

        ax.set_yticks(y_pos)
        ax.set_yticklabels(labels)
        ax.set_xlabel("Competitive Ratio (lower = better)")
        ax.set_title(title)
        ax.axvline(x=1.0, color="black", linestyle="--", linewidth=0.5, alpha=0.5)
        ax.invert_yaxis()

        # Highlight oracle gap
        if "PD-Oracle" in pro and "PD-EMA" in pro:
            oracle_cr = pro["PD-Oracle"]["cr"]
            ema_cr = pro["PD-EMA"]["cr"]
            ax.axvspan(oracle_cr, ema_cr, alpha=0.15, color=COLORS["prediction"])

    plt.suptitle(
        "Competitive Ratio Comparison: Oracle vs Predicted (Pro Plan)",
        fontsize=12,
        fontweight="bold",
        y=1.02,
    )
    plt.tight_layout()

    output_path = output_dir / "oracle_cr_comparison.png"
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    print(f"Saved: {output_path}")
    plt.close()


def main():
    """Generate oracle experiment plots."""
    results_path = project_root / "experiment/results/oracle/oracle_experiment_results.json"
    output_dir = project_root / "experiment/results/oracle"

    if not results_path.exists():
        print(f"Results not found: {results_path}")
        print("Run experiment/scripts/run_oracle_experiment.py first.")
        sys.exit(1)

    results = load_results(str(results_path))
    output_dir.mkdir(parents=True, exist_ok=True)

    plot_gap_decomposition(results, output_dir)
    plot_cr_comparison(results, output_dir)

    print("\nDone! Plots saved to:", output_dir)


if __name__ == "__main__":
    main()
