import { afterEach, describe, expect, it, vi } from "vitest";

import type { Config } from "../src/env";
import { consumeSse, probeModel, StreamingProbeError } from "../src/probe";

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

  // The chat probe issues two requests: a streaming one (TTFT + token count)
  // and a non-streaming one (end-to-end latency). Dispatch on the `stream` flag
  // so each leg gets a fresh, body-appropriate Response.
  function stubChat(streaming: () => Response, nonStreaming: () => Response) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        const body = JSON.parse(String(init.body));
        return body.stream ? streaming() : nonStreaming();
      }),
    );
  }

  const okStream = () =>
    new Response(
      sseStream(
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n' +
          'data: {"usage":{"completion_tokens":7}}\n\n' +
          "data: [DONE]\n\n",
      ),
      { status: 200 },
    );
  const okJson = () =>
    new Response(JSON.stringify({ choices: [{ message: { content: "hi there" } }] }), {
      status: 200,
    });

  it("is healthy and reports TTFT, latency, and token count", async () => {
    stubChat(okStream, okJson);
    const r = await probeModel(embedConfig, "k", { id: "m", kind: "chat" });
    expect(r.ok).toBe(true);
    expect(r.ttftMs).not.toBeNull();
    expect(r.latencyMs).toBeGreaterThanOrEqual(0);
    expect(r.completionTokens).toBe(7); // from the streaming probe, not request B
  });

  it("fails when the streaming (TTFT) request errors", async () => {
    stubChat(
      () => new Response(JSON.stringify({ error: { message: "stream gw error" } }), { status: 500 }),
      okJson,
    );
    const r = await probeModel(embedConfig, "k", { id: "m", kind: "chat" });
    expect(r.ok).toBe(false);
    expect(r.error).toContain("stream gw error");
  });

  it("fails when the non-streaming (latency) request errors", async () => {
    stubChat(
      okStream,
      () => new Response(JSON.stringify({ error: { message: "latency gw error" } }), { status: 500 }),
    );
    const r = await probeModel(embedConfig, "k", { id: "m", kind: "chat" });
    expect(r.ok).toBe(false);
    expect(r.error).toContain("latency gw error");
  });
});
