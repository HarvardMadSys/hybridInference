import argparse
import json
from collections import defaultdict
from datetime import datetime

import matplotlib.pyplot as plt


def load_data(log_path):
    by_prompt_type = defaultdict(lambda: {"timestamps": [], "latencies": [], "tcp_rtts": []})

    with open(log_path) as f:
        for line in f:
            entry = json.loads(line)
            if entry["total_seconds"] is None:
                continue

            prompt_type = entry.get("prompt_type", "default")

            by_prompt_type[prompt_type]["timestamps"].append(
                datetime.fromisoformat(entry["timestamp"])
            )
            by_prompt_type[prompt_type]["latencies"].append(entry["total_seconds"])
            by_prompt_type[prompt_type]["tcp_rtts"].append(
                entry.get("ping_rtt")
            )  # will be None if not logged

    return by_prompt_type


def plot_series(timestamps, values, prompt_type, label, ylabel, title, values_1=None, label_1=None):
    if prompt_type != "default":
        label = f"{prompt_type} (queuing)"
        plt.plot(timestamps, values, marker="o", label=label)
        plt.title(title)
        plt.xlabel("Time")
        plt.ylabel(ylabel)
        plt.grid(True)
        plt.tight_layout()
        plt.legend()
        plt.savefig(f"{title.replace(' ', '_').replace(':', '')}.png")
        return
    plt.plot(timestamps, values, marker="o", label=label)
    if values_1 is not None:
        plt.plot(timestamps, values_1, marker="o", label=label_1, markersize=2)
    plt.title(title)
    plt.xlabel("Time")
    plt.ylabel(ylabel)
    plt.grid(True)
    plt.tight_layout()
    plt.legend()
    plt.savefig(f"{title.replace(' ', '_').replace(':', '')}.png")


def analyze(log_path):
    model_name = log_path.split("/")[-1].replace(".jsonl", "")
    data = load_data(log_path)

    for prompt_type, series in data.items():
        print(f"\n--- Prompt Type: {prompt_type} ---")
        timestamps = series["timestamps"]

        plot_series(
            timestamps,
            series["latencies"],
            prompt_type,
            label="latency",
            ylabel="seconds (s)",
            title=f"{model_name}",
            values_1=series["tcp_rtts"],
            label_1="TCP RTT (s)",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--log",
        required=True,
        help="Path to model log file (e.g. logs/Llama-3.3-70B-Instruct.jsonl)",
    )
    args = parser.parse_args()

    analyze(args.log)
