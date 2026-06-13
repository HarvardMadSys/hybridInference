import type { Config } from "./env";
import type { TargetModel } from "./models";

/** Outcome of a single probe against one model. */
export interface ProbeResult {
  modelId: string;
  ok: boolean;
  checkedAt: string;
  latencyMs: number | null;
  ttftMs: number | null;
  completionTokens: number | null;
  throughputTps: number | null;
  error: string | null;
}

/** Raised when a streaming response carries an in-band `{"error": ...}` chunk. */
export class StreamingProbeError extends Error {}

function headers(config: Config, apiKey: string): Record<string, string> {
  const h: Record<string, string> = {
    Authorization: `Bearer ${apiKey}`,
    "Content-Type": "application/json",
  };
  // The gateway recognizes "X-Probe: synthetic" to exclude requests from logs,
  // metrics, and cost tracking.
  if (config.probeHeader) {
    h["X-Probe"] = config.probeHeader;
  }
  return h;
}

interface StreamStats {
  ttftMs: number | null;
  completionTokens: number | null;
}

/**
 * Consumes an SSE chat-completion stream, measuring time-to-first-token.
 *
 * Treats `content`, `reasoning_content`, and `tool_calls` deltas as first-token
 * events (matching the gateway's own TTFT tracker), and throws
 * {@link StreamingProbeError} on an in-band error chunk (HTTP 200 + `error`).
 */
export async function consumeSse(
  stream: ReadableStream<Uint8Array>,
  startedAt: number,
): Promise<StreamStats> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let ttftMs: number | null = null;
  let tokens = 0;
  let usageTokens: number | null = null;
  let sawDone = false;
  let sawEvent = false;

  // Returns true when the stream is complete ([DONE]).
  const handleLine = (line: string): boolean => {
    if (!line || line.startsWith(":") || !line.startsWith("data:")) {
      return false;
    }
    const body = line.slice("data:".length).trim();
    if (body === "[DONE]") {
      sawDone = true;
      return true;
    }
    let chunk: any;
    try {
      chunk = JSON.parse(body);
    } catch {
      return false;
    }
    if (chunk.error) {
      const message =
        typeof chunk.error === "object" && chunk.error?.message
          ? String(chunk.error.message)
          : String(chunk.error);
      throw new StreamingProbeError(message || "stream error");
    }
    if (chunk.usage?.completion_tokens != null) {
      usageTokens = chunk.usage.completion_tokens;
      sawEvent = true;
    }
    const choice = chunk.choices?.[0];
    if (choice?.finish_reason) {
      sawEvent = true;
    }
    const delta = choice?.delta ?? {};
    if (delta.content || delta.reasoning_content || delta.tool_calls) {
      sawEvent = true;
      if (ttftMs === null) {
        ttftMs = Date.now() - startedAt;
      }
      tokens += 1;
    }
    return false;
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl: number;
    let stop = false;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).replace(/\r$/, "");
      buffer = buffer.slice(nl + 1);
      if (handleLine(line)) {
        stop = true;
        break;
      }
    }
    if (stop) break;
  }

  // A stream that closes with neither a completion marker nor any meaningful
  // event is a truncated/empty response — fail rather than report it healthy.
  if (!sawDone && !sawEvent) {
    throw new StreamingProbeError("incomplete stream (no completion marker or content)");
  }
  return { ttftMs, completionTokens: usageTokens ?? (tokens || null) };
}

function describeError(err: unknown): string {
  if (err instanceof StreamingProbeError) {
    return `stream error: ${err.message}`.slice(0, 200);
  }
  if (err instanceof Error) {
    return `${err.name}: ${err.message}`.slice(0, 200);
  }
  return String(err).slice(0, 200);
}

async function probeEmbedding(
  config: Config,
  apiKey: string,
  modelId: string,
): Promise<void> {
  const response = await fetch(`${config.gatewayBaseUrl}/v1/embeddings`, {
    method: "POST",
    headers: headers(config, apiKey),
    body: JSON.stringify({ model: modelId, input: config.probePrompt }),
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  await response.arrayBuffer();
}

/** Sends one synthetic request for a model and returns the measured result. */
export async function probeModel(
  config: Config,
  apiKey: string,
  target: TargetModel,
): Promise<ProbeResult> {
  const checkedAt = new Date().toISOString();
  const started = Date.now();
  try {
    if (target.kind === "embedding") {
      await probeEmbedding(config, apiKey, target.id);
      return {
        modelId: target.id,
        ok: true,
        checkedAt,
        latencyMs: Date.now() - started,
        ttftMs: null,
        completionTokens: null,
        throughputTps: null,
        error: null,
      };
    }

    const response = await fetch(`${config.gatewayBaseUrl}/v1/chat/completions`, {
      method: "POST",
      headers: headers(config, apiKey),
      body: JSON.stringify({
        model: target.id,
        messages: [{ role: "user", content: config.probePrompt }],
        max_tokens: config.probeMaxTokens,
        temperature: 0,
        stream: true,
        stream_options: { include_usage: true },
      }),
    });
    if (!response.ok || !response.body) {
      throw new Error(`HTTP ${response.status}`);
    }
    const { ttftMs, completionTokens } = await consumeSse(response.body, started);
    const latencyMs = Date.now() - started;
    const throughputTps =
      completionTokens && latencyMs > 0 ? completionTokens / (latencyMs / 1000) : null;
    return {
      modelId: target.id,
      ok: true,
      checkedAt,
      latencyMs,
      ttftMs,
      completionTokens,
      throughputTps: throughputTps ? Math.round(throughputTps * 100) / 100 : null,
      error: null,
    };
  } catch (err) {
    return {
      modelId: target.id,
      ok: false,
      checkedAt,
      latencyMs: Date.now() - started,
      ttftMs: null,
      completionTokens: null,
      throughputTps: null,
      error: describeError(err),
    };
  }
}
