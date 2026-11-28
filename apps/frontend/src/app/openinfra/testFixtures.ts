// Neutral fixtures for the OpenInfra tests: two nodes, six GPUs, three engines
// (one vLLM) and an offline row left behind under one engine's name.
import type {
  OpenInfraDashboard,
  OpenInfraEngine,
  OpenInfraGpu,
  OpenInfraHistory,
  OpenInfraNode,
} from '@/lib/api/openinfra';

export const NOW = 1_795_800_000_000;

function node(id: string, extra: Partial<OpenInfraNode> = {}): OpenInfraNode {
  return {
    id,
    hostname: id,
    region: 'lab',
    cluster: 'example',
    status: 'online',
    last_seen_at: NOW - 2_000,
    cpu_util_pct: 20,
    load1: 8,
    load5: 7,
    load15: 6,
    mem_used_mb: 512_000,
    mem_total_mb: 1_024_000,
    mem_available_mb: 512_000,
    swap_used_mb: 0,
    procs_running: 4,
    procs_total: 400,
    cpu_count: 128,
    gpu_count: 0,
    avg_utilization_pct: 50,
    ...extra,
  };
}

function gpu(nodeId: string, index: number, extra: Partial<OpenInfraGpu> = {}): OpenInfraGpu {
  return {
    id: `${nodeId}:${index}`,
    node_id: nodeId,
    hostname: nodeId,
    region: 'lab',
    cluster: 'example',
    gpu_index: index,
    uuid: null,
    name: 'NVIDIA Example 80GB',
    driver: '570.00',
    memory_total_mb: 80_000,
    utilization_pct: 50 + index,
    memory_util_pct: 30,
    memory_used_mb: 60_000,
    temperature_c: 60,
    power_w: 400,
    power_limit_w: 700,
    fan_speed_pct: null,
    clocks_sm_mhz: 1800,
    clocks_mem_mhz: 2600,
    ecc_errors: 0,
    pstate: 'P0',
    processes: [],
    status: 'online',
    last_seen_at: NOW - 2_000,
    tensor_active: 0.4,
    dram_active: 0.3,
    pcie_tx_bps: 1e9,
    pcie_rx_bps: 2e9,
    nvlink_tx_bps: 5e10,
    nvlink_rx_bps: 5e10,
    ...extra,
  };
}

const METRICS = {
  num_running_reqs: 12,
  num_queue_reqs: 3,
  num_used_tokens: 200_000,
  token_usage: 0.4,
  gen_throughput: 1_000,
  cache_hit_rate: 0.6,
  prompt_tokens_total: null,
  generation_tokens_total: null,
  ttft_avg_s: 0.5,
  queue_avg_s: 0.1,
  prefill_avg_s: 0.3,
  e2e_latency_avg_s: 8,
  tpot_avg_s: 0.02,
  sm_util_pct: 60,
  max_total_num_tokens: 500_000,
  num_active_tokens: 100_000,
  num_waiting_uncached_tokens: 5_000,
  prefill_tps: 2_000,
  spec_accept_length: null,
  sglang_util_pct: 55,
  max_running_requests: 256,
  weight_gb: 60,
  kv_cache_gb: 20,
  spec_accept_rate: null,
  req_per_min: 30,
  cached_prefill_tps: 3_000,
  chunked_prefill_avg_s: null,
  hicache_host_used: null,
  hicache_host_total: null,
  device_cached_tps: null,
  host_cached_tps: null,
  loadback_tps: null,
  evict_tps: null,
  backup_tps: null,
  prompt_tokens_avg: 8_000,
  uncached_prompt_tokens_avg: 2_000,
  generation_tokens_avg: 400,
  http_error_per_min: 0,
  retracts: null,
};

export function engine(
  id: string,
  name: string,
  nodeId: string,
  gpuIndexes: string,
  extra: Partial<OpenInfraEngine> = {},
): OpenInfraEngine {
  return {
    id,
    node_id: nodeId,
    hostname: nodeId,
    region: 'lab',
    name,
    model_name: `example-org/${name}-instruct`,
    endpoint: `http://${nodeId}:8000`,
    engine: 'sglang',
    status: 'online',
    gpu_indexes: gpuIndexes,
    last_seen_at: NOW - 2_000,
    ...METRICS,
    ...extra,
  };
}

const bucket = (reqs: number) => ({
  reqs,
  cache_tokens: reqs * 1_000,
  prefill_tokens: reqs * 300,
  decode_tokens: reqs * 100,
});

export function dashboard(): OpenInfraDashboard {
  const sglang = [
    engine('node-a-chat', 'chat-model', 'node-a', '0,1'),
    engine('node-a-code', 'code-model', 'node-a', '2,3', {
      engine: 'vllm',
      num_queue_reqs: 9,
      model_name: 'example-org/code-model-32b',
    }),
    engine('node-b-embed', 'embed-model', 'node-b', '0'),
    // Left behind when chat-model moved to node-a.
    engine('node-b-chat', 'chat-model', 'node-b', '1', {
      status: 'offline',
      last_seen_at: NOW - 86_400_000,
    }),
  ];
  return {
    generated_at: NOW,
    fleet: 'healthy',
    kpis: {
      nodes: 2,
      nodes_online: 2,
      gpus: 6,
      gpus_online: 6,
      avg_utilization_pct: 52,
      tensor_active: 0.4,
      dram_active: 0.3,
      nvlink_gbps: 600,
      memory_used_mb: 360_000,
      memory_total_mb: 480_000,
      avg_temperature_c: 60,
      power_w: 2400,
      sglang_servers: 4,
      sglang_online: 3,
      running_reqs: 36,
      queue_reqs: 15,
      gen_throughput: 3_000,
      token_usage: 0.4,
      cache_hit_rate: 0.6,
      used_tokens: 600_000,
      max_tokens: 1_500_000,
      active_tokens: 300_000,
      waiting_tokens: 15_000,
      prefill_tps: 6_000,
      cpu_util_pct: 20,
      load1: 8,
      ram_used_mb: 1_024_000,
      ram_total_mb: 2_048_000,
    },
    nodes: [node('node-a', { gpu_count: 4 }), node('node-b', { gpu_count: 2, region: 'lab-2' })],
    gpus: [
      gpu('node-a', 0),
      gpu('node-a', 1),
      gpu('node-a', 2),
      gpu('node-a', 3),
      gpu('node-b', 0),
      gpu('node-b', 1),
    ],
    sglang,
    served: {
      hour: bucket(60),
      day: bucket(1_440),
      servers: [
        { id: 'node-a-chat', name: 'chat-model', hour: bucket(10), day: bucket(240) },
        { id: 'node-a-code', name: 'code-model', hour: bucket(20), day: bucket(480) },
        { id: 'node-b-embed', name: 'embed-model', hour: bucket(30), day: bucket(720) },
      ],
    },
    requests: [
      {
        server_id: 'node-a-chat',
        ts: NOW - 5_000,
        model: 'example-org/chat-model-instruct',
        input_len: 9_000,
        output_len: 300,
        cache_len: 6_000,
        uncached_len: 3_000,
        ttft_s: 0.4,
        queue_s: 0.1,
        prefill_s: 0.3,
        tpot_s: 0.02,
        e2e_s: 6.5,
        decode_tps: 48,
        spec_accept_len: null,
        spec_accept_rate: null,
        batch_size: 24,
      },
      {
        server_id: 'node-a-code',
        ts: NOW - 9_000,
        model: 'example-org/code-model-32b',
        input_len: 20_000,
        output_len: 800,
        cache_len: 15_000,
        uncached_len: 5_000,
        ttft_s: 0.9,
        queue_s: 0.2,
        prefill_s: 0.7,
        tpot_s: 0.03,
        e2e_s: 20,
        decode_tps: 33,
        spec_accept_len: null,
        spec_accept_rate: null,
        batch_size: 12,
      },
    ],
  };
}

export function history(grainMs = 60_000): OpenInfraHistory {
  const times = [NOW - 2 * grainMs, NOW - grainMs, NOW];
  const gpuRows = ['node-a', 'node-b'].flatMap((nodeId) =>
    [0, 1, 2, 3]
      .filter((i) => nodeId === 'node-a' || i < 2)
      .flatMap((i) =>
        times.map((ts, k) => ({
          node_id: nodeId,
          gpu_index: i,
          gpu_id: `${nodeId}:${i}`,
          ts,
          utilization_pct: 40 + 10 * k,
          memory_pct: 70,
          memory_util_pct: 30,
          temperature_c: 60,
          power_w: 400,
          clocks_sm_mhz: 1800,
          tensor_active: 0.5,
          dram_active: 0.3,
          pcie_tx_bps: 1e9,
          pcie_rx_bps: 2e9,
          nvlink_tx_bps: 5e10,
          nvlink_rx_bps: 5e10,
        })),
      ),
  );
  const engines: Array<[string, string, string]> = [
    ['node-a-chat', 'chat-model', 'node-a'],
    ['node-a-code', 'code-model', 'node-a'],
    ['node-b-embed', 'embed-model', 'node-b'],
  ];
  const sglang = engines.flatMap(([id, name, nodeId]) =>
    times.map((ts, k) => ({
      node_id: nodeId,
      server_id: id,
      name,
      ts,
      running: 10 + k,
      queued: 2 * k,
      gen_throughput: 1_000,
      token_usage: 0.5,
      cache_hit_rate: k === 0 ? 0 : 0.6,
      prefill_tps: 2_000,
      cached_prefill_tps: 3_000,
      req_per_min: 30,
      ttft_avg_s: 0.5,
      queue_avg_s: 0.1,
      prefill_avg_s: 0.3,
      used_tokens: 200_000,
      max_tokens: 500_000,
    })),
  );
  const cpu = ['node-a', 'node-b'].flatMap((nodeId) =>
    times.map((ts) => ({
      node_id: nodeId,
      ts,
      cpu_util_pct: 30,
      load1: 10,
      load5: 9,
      mem_used_mb: 600_000,
      mem_total_mb: 1_024_000,
      mem_available_mb: 424_000,
    })),
  );
  return { generated_at: NOW, grain_ms: grainMs, gpu: gpuRows, sglang, cpu };
}
