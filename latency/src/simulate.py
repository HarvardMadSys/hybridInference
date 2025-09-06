from collections import defaultdict


def simulate_local_gpu(
    df,
    mode="local",
    local_gpus=1,
    queue_threshold=None,
    sim_duration=240,
    model_name="ChatGPT",
    local_hardware="L4",
    local_provider="AWS",
    cloud_provider="OpenAI",
    config=None,
):
    if config is None:
        raise ValueError("Infra config must be provided.")

    # Track hourly request timestamps
    hourly_total_requests = defaultdict(list)
    hourly_api_requests = defaultdict(list)
    temp_total = defaultdict(int)
    temp_api = defaultdict(int)

    prefill_rate = config["token_rates"][model_name][local_hardware]["prefill"]
    decode_rate = config["token_rates"][model_name][local_hardware]["decode"]
    gpu_cost_per_hr = config["hardware"][local_hardware]["cost_per_hour"]

    cloud_latency = config["api_providers"][cloud_provider]["latency"]
    cloud_cost_input = config["api_providers"][cloud_provider]["cost_input"]
    cloud_cost_output = config["api_providers"][cloud_provider]["cost_output"]

    latencies = []
    total_cost = 0.0

    if mode == "cloud":
        for _, row in df.iterrows():
            ts = row["Timestamp"]
            if ts > sim_duration:
                break
            model = row["Model"]
            input_tokens = row["Request tokens"]
            output_tokens = row["Response tokens"]
            total_cost += (
                input_tokens * cloud_cost_input[model] + output_tokens * cloud_cost_output[model]
            )
            latencies.append(cloud_latency)

    elif mode == "local":
        gpu_available_at = [0.0] * local_gpus
        for _, row in df.iterrows():
            ts = row["Timestamp"]
            if ts > sim_duration:
                break
            model = row["Model"]
            input_tokens = row["Request tokens"]
            output_tokens = row["Response tokens"]

            prefill_time = input_tokens / prefill_rate
            decode_time = output_tokens / decode_rate
            service_time = prefill_time + decode_time

            selected_gpu = min(range(local_gpus), key=lambda g: max(ts, gpu_available_at[g]))
            start_time = max(ts, gpu_available_at[selected_gpu])
            end_time = start_time + service_time
            latencies.append(end_time - ts)
            gpu_available_at[selected_gpu] = end_time

        total_cost = local_gpus * gpu_cost_per_hr * (sim_duration / 3600)

    elif mode == "hybrid":
        gpu_available_at = [0.0] * local_gpus
        for _, row in df.iterrows():
            ts = row["Timestamp"]
            if ts > sim_duration:
                break
            model = row["Model"]

            input_tokens = row["Request tokens"]
            output_tokens = row["Response tokens"]

            day = int(ts) // 86400
            hour = (int(ts) // 3600) % 24
            key = (day, hour)
            temp_total[key] += input_tokens + output_tokens

            prefill_time = input_tokens / prefill_rate
            decode_time = output_tokens / decode_rate
            service_time = prefill_time + decode_time

            next_available_time = min(gpu_available_at)
            wait_time = max(0, next_available_time - ts)

            if wait_time > queue_threshold:
                latencies.append(cloud_latency)
                total_cost += (
                    input_tokens * cloud_cost_input[model]
                    + output_tokens * cloud_cost_output[model]
                )
                temp_api[key] += input_tokens + output_tokens
            else:
                selected_gpu = min(range(local_gpus), key=lambda g: max(ts, gpu_available_at[g]))
                start_time = max(ts, gpu_available_at[selected_gpu])
                end_time = start_time + service_time
                latencies.append(end_time - ts)
                gpu_available_at[selected_gpu] = end_time

        total_cost += local_gpus * gpu_cost_per_hr * (sim_duration / 3600)

        for (day, hour), count in temp_total.items():
            hourly_total_requests[hour].append(count)

        for (day, hour), count in temp_api.items():
            hourly_api_requests[hour].append(count)

        print("GPU available at (end):", gpu_available_at)

    else:
        raise ValueError(f"Unknown mode: {mode}")

    return latencies, total_cost, hourly_total_requests, hourly_api_requests
