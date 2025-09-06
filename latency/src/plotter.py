import itertools
import json
import os

import matplotlib.pyplot as plt
import numpy as np


def plot_tradeoff(results, model_name, local_hw, cloud_provider):
    if not os.path.exists("res"):
        os.makedirs("res")

    # Marker settings
    markers = {"p50": "o", "p75": "^", "p99": "s"}
    sizes = {"p50": 10, "p75": 10, "p99": 10}
    color_cycle = itertools.cycle(plt.cm.tab10.colors)

    # Data to export
    export_data = {}

    for label, (latencies, cost) in results.items():
        latencies = np.array(latencies)
        p99 = np.percentile(latencies, 99)
        color = next(color_cycle)

        plt.scatter(
            p99, cost, marker=markers["p99"], s=sizes["p99"], label=label, color=color, alpha=0.7
        )

        export_data[label] = {"p99_latency": float(p99), "cost": float(cost)}

    plt.xlabel("Latency (s)")
    plt.ylabel("Total Cost ($)")
    title = f"Cost vs Latency Tradeoff\nModel={model_name}, GPU={local_hw}, Cloud={cloud_provider}"
    plt.title(title)
    plt.grid(True, linestyle="--", alpha=0.6)
    plt.legend(fontsize=8)
    plt.tight_layout()

    # Sanitize file name
    safe_model = model_name.replace(" ", "_")
    safe_hw = local_hw.replace(" ", "_")
    safe_cloud = cloud_provider.replace(" ", "_")
    base_filename = f"res/tradeoff_{safe_model}_{safe_hw}_cloud-{safe_cloud}"

    # Save plot
    plt.savefig(f"{base_filename}.pdf")
    plt.clf()

    # Save data to JSON
    with open(f"{base_filename}.json", "w") as f:
        json.dump(export_data, f, indent=2)


def plot_pattern(hourly_total_requests):
    if not os.path.exists("res"):
        os.makedirs("res")

    hours = list(range(24))
    total_data = [hourly_total_requests.get(h, []) for h in hours]
    # divide the total_data by 3600 to get tokens per second
    total_data = [np.array(data) / 3600 for data in total_data]
    # print("Total requests per hour:", total_data)

    # Calculate total number of requests and requests per second
    # total_requests = sum(sum(lst) for lst in total_data)
    # requests_per_second = total_requests/5259178

    plt.boxplot(
        total_data,
        positions=hours,
        patch_artist=True,
        boxprops=dict(facecolor="skyblue"),
        medianprops=dict(color="black"),
        showfliers=False,
    )
    sum_all_hours = int(np.sum([item for sublist in total_data for item in sublist]))
    print("Total requests across all hours:", sum_all_hours)
    overall_median = np.median([item for sublist in total_data for item in sublist])
    plt.axhline(y=overall_median, color="red", linestyle="--")

    plt.xticks(hours)
    plt.xlabel("Hour of Day")
    plt.ylabel("Token Rate (tokens/sec)")
    plt.title("Token Rate Distribution")
    plt.grid(True, linestyle="--", alpha=0.5)

    base_name = "res/token_rate_original"
    # Save plot
    plt.savefig(f"{base_name}.pdf")
    plt.clf()


def plot_requests_by_hour(
    hourly_total_requests, hourly_api_requests, model_name, local_hw, cloud_provider, label
):
    if not os.path.exists("res"):
        os.makedirs("res")

    hours = list(range(24))
    total_data = [hourly_total_requests.get(h, []) for h in hours]
    api_data = [hourly_api_requests.get(h, []) for h in hours]
    print("Total requests per hour:", total_data)
    print("API-offloaded requests per hour:", api_data)

    fig, ax = plt.subplots(figsize=(12, 5))

    # X positions for side-by-side boxes: total at h - 0.2, api at h + 0.2
    positions_total = [h - 0.2 for h in hours]
    positions_api = [h + 0.2 for h in hours]

    b1 = ax.boxplot(
        total_data,
        positions=positions_total,
        widths=0.35,
        patch_artist=True,
        boxprops=dict(facecolor="skyblue"),
        medianprops=dict(color="black"),
    )

    b2 = ax.boxplot(
        api_data,
        positions=positions_api,
        widths=0.35,
        patch_artist=True,
        boxprops=dict(facecolor="salmon"),
        medianprops=dict(color="black"),
    )

    ax.set_xticks(hours)
    ax.set_xticklabels([str(h) for h in hours])
    ax.set_xlabel("Hour of Day")
    ax.set_ylabel("Request Number")
    ax.set_title(
        f"Hourly Request Distribution\nModel={model_name}, GPU={local_hw}, Cloud={cloud_provider}, Config={label}"
    )
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend([b1["boxes"][0], b2["boxes"][0]], ["Total", "API-Offloaded"], loc="upper right")

    label = label.replace("GPU, wait>", "").replace(" ", "_").replace("(", "").replace(")", "")
    base_name = f"res/hourly_requests_{model_name.replace(' ', '_')}_{local_hw.replace(' ', '_')}_cloud-{cloud_provider.replace(' ', '_')}_{label}"
    # Save plot
    plt.savefig(f"{base_name}.pdf")
    plt.clf()


def plot_hourly_request_counts(
    hourly_total_requests, hourly_api_requests, model_name, local_hw, cloud_provider, label
):
    if not os.path.exists("res"):
        os.makedirs("res")

    hours = list(range(24))
    count_total = [sum(hourly_total_requests.get(h, [])) / (60 * 60) for h in hours]
    count_api = [sum(hourly_api_requests.get(h, [])) / (60 * 60) for h in hours]

    plt.figure(figsize=(12, 5))
    plt.plot(hours, count_total, marker="o", label="Total Requests", color="tab:blue")
    plt.plot(
        hours,
        count_api,
        marker="s",
        linestyle="--",
        label="API-Offloaded Requests",
        color="tab:red",
    )

    plt.xlabel("Hour of Day")
    plt.ylabel("Tokens per second")
    plt.title(
        f"Token Rate\nModel={model_name}, GPU={local_hw}, Cloud={cloud_provider}, Config={label}"
    )
    plt.xticks(hours)
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.legend(loc="upper left")

    plt.tight_layout()

    label = label.replace("GPUs, wait>", "").replace(" ", "_").replace("(", "").replace(")", "")
    base_name = f"res/token_rate_{model_name.replace(' ', '_')}_{local_hw.replace(' ', '_')}_cloud-{cloud_provider.replace(' ', '_')}_{label}"
    plt.savefig(f"{base_name}.pdf")
    plt.clf()
