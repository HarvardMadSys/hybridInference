from data_loader import load_burst_trace, load_infra_config
from plotter import plot_hourly_request_counts, plot_tradeoff
from simulate import simulate_local_gpu

ALL = 60 * 60 * 24 * 10  # 5269973
#


def main():
    path = "../../data/BurstGPT_1.csv"
    df = load_burst_trace(path)
    config = load_infra_config("../infra_config.yaml")

    model = "GPT-4"
    local_hw = "A100"
    cloud_provider = "OpenAI"

    results = {}
    for num_gpus in [1, 2]:
        # for threshold in [5.0, 10.0, 100.0]:
        for threshold in [100]:
            label = f"Hybrid ({num_gpus} GPU, wait>{threshold}s)"
            latencies, cost, hourly_total_requests, hourly_api_requests = simulate_local_gpu(
                df,
                mode="hybrid",
                local_gpus=num_gpus,
                queue_threshold=threshold,
                sim_duration=ALL,
                model_name=model,
                local_hardware=local_hw,
                cloud_provider=cloud_provider,
                config=config,
            )
            results[label] = (latencies, cost)
            print(
                f"Token num stats for {label} (s):",
                hourly_total_requests,
                "\n",
                hourly_api_requests,
            )
            # plot_pattern(hourly_total_requests)
            # plot_requests_by_hour(hourly_total_requests, hourly_api_requests, model_name=model, local_hw=local_hw, cloud_provider=cloud_provider, label=label)
            plot_hourly_request_counts(
                hourly_total_requests,
                hourly_api_requests,
                model_name=model,
                local_hw=local_hw,
                cloud_provider=cloud_provider,
                label=label,
            )
        latencies, cost, _, _ = simulate_local_gpu(
            df,
            mode="local",
            local_gpus=num_gpus,
            sim_duration=ALL,
            model_name=model,
            local_hardware=local_hw,
            config=config,
        )
        # stats = analyze_latencies(latencies)
        # print(f"Latency stats for {num_gpus} GPUs (s):", stats)
        # print(f"Total Cost for {num_gpus} GPUs ($):", round(cost, 2))
        results[f"Local ({num_gpus} GPUs)"] = (latencies, cost)

    latencies, cost, _, _ = simulate_local_gpu(
        df,
        mode="cloud",
        sim_duration=ALL,
        model_name=model,
        cloud_provider=cloud_provider,
        config=config,
    )
    results["Cloud (API)"] = (latencies, cost)

    plot_tradeoff(results, model_name=model, local_hw=local_hw, cloud_provider=cloud_provider)


if __name__ == "__main__":
    main()
