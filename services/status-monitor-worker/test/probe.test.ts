import { describe, expect, it } from "vitest";

import { consumeSse, StreamingProbeError } from "../src/probe";

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

  it("throws on an in-band error chunk", async () => {
    const stream = sseStream(
      'data: {"error":{"message":"upstream exploded"}}\n\ndata: [DONE]\n\n',
    );
    await expect(consumeSse(stream, Date.now())).rejects.toBeInstanceOf(StreamingProbeError);
  });
});
