#!/usr/bin/env python3

import json

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


def load_results(path="/root/hybridInference/llama_benchmark/results.json"):
    with open(path) as f:
        return json.load(f)


def create_visualizations(results):
    df = pd.DataFrame(results)

    # Set style
    sns.set_style("whitegrid")
    fig, axes = plt.subplots(2, 2, figsize=(15, 12))

    # 1. Latency vs Prompt Length
    ax = axes[0, 0]
    for server in df["server"].unique():
        server_data = df[df["server"] == server]
        for conc in [1, 16, 32]:
            conc_data = server_data[server_data["concurrency"] == conc]
            ax.plot(
                conc_data["prompt_length"],
                conc_data["p50"],
                marker="o",
                label=f"{server} (c={conc})",
            )
    ax.set_xlabel("Prompt Length (tokens)")
    ax.set_ylabel("P50 Latency (ms)")
    ax.set_title("Latency vs Prompt Length")
    ax.legend()
    ax.set_xscale("log")

    # 2. Throughput vs Concurrency
    ax = axes[0, 1]
    for server in df["server"].unique():
        server_data = df[df["server"] == server]
        for pl in [32, 512, 2048]:
            pl_data = server_data[server_data["prompt_length"] == pl]
            ax.plot(
                pl_data["concurrency"],
                pl_data["throughput"],
                marker="s",
                label=f"{server} ({pl} tok)",
            )
    ax.set_xlabel("Concurrency")
    ax.set_ylabel("Throughput (req/s)")
    ax.set_title("Throughput Scaling")
    ax.legend()

    # 3. P95/P99 Latency Comparison
    ax = axes[1, 0]
    metrics = ["p50", "p95", "p99"]
    x_pos = range(len(metrics))

    for i, server in enumerate(df["server"].unique()):
        server_data = df[(df["server"] == server) & (df["concurrency"] == 16)]
        avg_metrics = [server_data[m].mean() for m in metrics]
        ax.bar([p + i * 0.3 for p in x_pos], avg_metrics, 0.3, label=server)

    ax.set_xlabel("Percentile")
    ax.set_ylabel("Latency (ms)")
    ax.set_title("Latency Percentiles (c=16)")
    ax.set_xticks([p + 0.15 for p in x_pos])
    ax.set_xticklabels(["P50", "P95", "P99"])
    ax.legend()

    # 4. Success Rate Heatmap
    ax = axes[1, 1]
    pivot = df.pivot_table(
        values="success_rate", index="prompt_length", columns="concurrency", aggfunc="mean"
    )
    sns.heatmap(pivot, annot=True, fmt=".2%", cmap="RdYlGn", ax=ax)
    ax.set_title("Success Rate Heatmap")

    plt.tight_layout()
    plt.savefig("/root/hybridInference/llama_benchmark/benchmark_results.png", dpi=150)
    plt.show()

    # Print summary
    print("\n" + "=" * 60)
    print("BENCHMARK SUMMARY")
    print("=" * 60)

    for server in df["server"].unique():
        server_data = df[df["server"] == server]
        print(f"\n{server}:")
        print(f"  Avg P50 Latency: {server_data['p50'].mean():.0f}ms")
        print(f"  Avg P95 Latency: {server_data['p95'].mean():.0f}ms")
        print(f"  Max Throughput: {server_data['throughput'].max():.2f} req/s")
        print(f"  Avg Success Rate: {server_data['success_rate'].mean():.2%}")


if __name__ == "__main__":
    results = load_results()
    create_visualizations(results)
