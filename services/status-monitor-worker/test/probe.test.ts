import { afterEach, describe, expect, it, vi } from "vitest";

import type { Config } from "../src/env";
import { consumeSse, isDiffusionModel, probeModel, StreamingProbeError } from "../src/probe";

const embedConfig = {
  gatewayBaseUrl: "https://gw.example",
  probePrompt: "hi",
  probeMaxTokens: 32,
  maxConcurrency: 3,
  probeHeader: "synthetic",
  retentionDays: 7,
  probeDeadlineMs: 1000,
} as Config;

function sseStream(text: string): ReadableStream<Uint8Array> {
  const bytes = new TextEncoder().encode(text);
  return new ReadableStream({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
  });
}

describe("consumeSse", () => {
  it("measures TTFT and prefers usage token count", async () => {
    const stream = sseStream(
      'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n' +
        'data: {"choices":[{"delta":{"content":" world"}}]}\n\n' +
        'data: {"usage":{"completion_tokens":7}}\n\n' +
        "data: [DONE]\n\n",
    );
    const stats = await consumeSse(stream, Date.now());
    expect(stats.completionTokens).toBe(7);
    expect(stats.ttftMs).not.toBeNull();
  });

  it("handles data lines without a space and counts reasoning as first token", async () => {
    const stream = sseStream(
      'data:{"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\ndata: [DONE]\n\n',
    );
    const stats = await consumeSse(stream, Date.now());
    expect(stats.ttftMs).not.toBeNull();
    expect(stats.completionTokens).toBe(1);
  });

  it("throws on a truncated stream with no completion marker or content", async () => {
    const stream = sseStream(": keep-alive\n\n"); // closes without [DONE] or any delta
    await expect(consumeSse(stream, Date.now())).rejects.toBeInstanceOf(StreamingProbeError);
  });

  it("throws when content arrives but the stream is cut before a terminal marker", async () => {
    const stream = sseStream('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'); // no finish/usage/[DONE]
    await expect(consumeSse(stream, Date.now())).rejects.toBeInstanceOf(StreamingProbeError);
  });

  it("accepts a stream that ends with a finish_reason but no [DONE]", async () => {
    const stream = sseStream(
      'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n',
    );
    const stats = await consumeSse(stream, Date.now());
    expect(stats.completionTokens).toBe(1);
  });

  it("throws on an empty completion (terminal marker but no content)", async () => {
    const stream = sseStream('data: {"usage":{"completion_tokens":0}}\n\ndata: [DONE]\n\n');
    await expect(consumeSse(stream, Date.now())).rejects.toBeInstanceOf(StreamingProbeError);
  });

  it("throws on a malformed JSON data chunk even if [DONE] follows", async () => {
    const stream = sseStream('data: {not valid json\n\ndata: [DONE]\n\n');
    await expect(consumeSse(stream, Date.now())).rejects.toBeInstanceOf(StreamingProbeError);
  });

  it("throws on an in-band error chunk", async () => {
    const stream = sseStream(
      'data: {"error":{"message":"upstream exploded"}}\n\ndata: [DONE]\n\n',
    );
    await expect(consumeSse(stream, Date.now())).rejects.toBeInstanceOf(StreamingProbeError);
  });
});

describe("probeModel (embedding)", () => {
  afterEach(() => vi.unstubAllGlobals());

  function stub(response: Response) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => response),
    );
  }

  it("is healthy for a non-empty embedding", async () => {
    stub(new Response(JSON.stringify({ data: [{ embedding: [0.1, 0.2] }] }), { status: 200 }));
    const r = await probeModel(embedConfig, "k", { id: "bge-m3", kind: "embedding" });
    expect(r.ok).toBe(true);
  });

  it("fails on an empty embedding list (HTTP 200)", async () => {
    stub(new Response(JSON.stringify({ data: [] }), { status: 200 }));
    const r = await probeModel(embedConfig, "k", { id: "bge-m3", kind: "embedding" });
    expect(r.ok).toBe(false);
    expect(r.error).toContain("empty embedding");
  });

  it("fails on an HTTP error with the gateway message", async () => {
    stub(new Response(JSON.stringify({ error: { message: "Embedding service error" } }), { status: 500 }));
    const r = await probeModel(embedConfig, "k", { id: "bge-m3", kind: "embedding" });
    expect(r.ok).toBe(false);
    expect(r.error).toContain("Embedding service error");
  });
});

describe("probeModel (chat)", () => {
  afterEach(() => vi.unstubAllGlobals());

  // The chat probe issues a single streaming request and records how many fetches
  // it made, so the test can assert there is exactly one.
  function stubChat(response: () => Response) {
    const calls = { count: 0, lastBody: undefined as any };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        calls.count += 1;
        calls.lastBody = JSON.parse(String(init.body));
        return response();
      }),
    );
    return calls;
  }

  // Multi-token workload completion carrying the usage token count.
  const workload = () =>
    new Response(
      sseStream(
        'data: {"choices":[{"delta":{"content":"def search"}}]}\n\n' +
          'data: {"choices":[{"delta":{"content":"(xs):"}}]}\n\n' +
          'data: {"usage":{"completion_tokens":7}}\n\n' +
          "data: [DONE]\n\n",
      ),
      { status: 200 },
    );

  it("is healthy and reports TTFT, latency, and token count from one request", async () => {
    const calls = stubChat(workload);
    const r = await probeModel(embedConfig, "k", { id: "m", kind: "chat" });
    expect(r.ok).toBe(true);
    expect(calls.count).toBe(1); // single request, no separate TTFT probe
    expect(calls.lastBody.stream).toBe(true);
    expect(calls.lastBody.max_tokens).toBe(embedConfig.probeMaxTokens);
    expect(r.ttftMs).not.toBeNull();
    expect(r.latencyMs).toBeGreaterThanOrEqual(0);
    expect(r.completionTokens).toBe(7);
  });

  it("fails when the request errors", async () => {
    stubChat(() => new Response(JSON.stringify({ error: { message: "gw error" } }), { status: 500 }));
    const r = await probeModel(embedConfig, "k", { id: "m", kind: "chat" });
    expect(r.ok).toBe(false);
    expect(r.error).toContain("gw error");
  });

  // A diffusion model runs the same streaming probe; it just reports throughput
  // as output-tokens / total-duration (see the isDiffusionModel unit tests) so
  // the branch is exercised end-to-end here — it stays healthy and still records
  // the token count.
  it("probes a diffusion model over the same streaming path", async () => {
    const calls = stubChat(workload);
    const r = await probeModel(embedConfig, "k", { id: "diffusiongemma", kind: "chat" });
    expect(r.ok).toBe(true);
    expect(calls.count).toBe(1);
    expect(r.completionTokens).toBe(7);
  });
});

describe("isDiffusionModel", () => {
  it("matches diffusion model ids case-insensitively, including aliases", () => {
    expect(isDiffusionModel("diffusiongemma")).toBe(true);
    expect(isDiffusionModel("freeinference-diffusiongemma")).toBe(true);
    expect(isDiffusionModel("DiffusionGemma")).toBe(true);
  });

  it("does not match autoregressive models", () => {
    expect(isDiffusionModel("gpt-oss-20b")).toBe(false);
    expect(isDiffusionModel("qwen3.6-35b")).toBe(false);
    expect(isDiffusionModel("bge-m3")).toBe(false);
  });
});
