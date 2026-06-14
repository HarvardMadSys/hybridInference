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
    // Probe as a typical client so the request resembles real traffic.
    "User-Agent": "claude-code/0.1.0",
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
  let sawTerminal = false; // [DONE], finish_reason, or end-of-stream usage

  // Returns true when the stream is complete ([DONE]).
  const handleLine = (line: string): boolean => {
    if (!line || line.startsWith(":") || !line.startsWith("data:")) {
      return false;
    }
    const body = line.slice("data:".length).trim();
    if (body === "[DONE]") {
      sawTerminal = true;
      return true;
    }
    let chunk: any;
    try {
      chunk = JSON.parse(body);
    } catch {
      // A non-[DONE] data line that isn't valid JSON means the stream is
      // corrupted; a real client couldn't consume it, so fail the probe.
      throw new StreamingProbeError("malformed SSE data chunk");
    }
    if (chunk.error) {
      const message =
        typeof chunk.error === "object" && chunk.error?.message
          ? String(chunk.error.message)
          : String(chunk.error);
      throw new StreamingProbeError(message || "stream error");
    }
    // usage and finish_reason only appear at end-of-stream, so they mark a
    // complete response; a content delta alone does not.
    if (chunk.usage?.completion_tokens != null) {
      usageTokens = chunk.usage.completion_tokens;
      sawTerminal = true;
    }
    const choice = chunk.choices?.[0];
    if (choice?.finish_reason) {
      sawTerminal = true;
    }
    const delta = choice?.delta ?? {};
    // An empty tool_calls array ([]) is truthy but carries no output, so only
    // count a non-empty one as a generated token.
    const hasToolCalls = Array.isArray(delta.tool_calls) && delta.tool_calls.length > 0;
    if (delta.content || delta.reasoning_content || hasToolCalls) {
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

  // A terminal marker proves the stream completed; a content/reasoning/tool
  // delta proves the model actually generated something. Require both, so a
  // truncated stream OR an empty completion (no tokens + synthesized [DONE])
  // is failed rather than reported healthy.
  if (!sawTerminal) {
    throw new StreamingProbeError("incomplete stream (no terminal marker)");
  }
  if (tokens === 0) {
    throw new StreamingProbeError("empty completion (no content generated)");
  }
  return { ttftMs, completionTokens: usageTokens ?? (tokens || null) };
}

/** Builds an Error preserving the gateway's error message from a non-2xx body. */
async function httpError(response: Response): Promise<Error> {
  let detail = "";
  try {
    const body: any = await response.json();
    detail =
      body?.error?.message ||
      (typeof body?.error === "string" ? body.error : "") ||
      (typeof body?.detail === "string" ? body.detail : body?.detail?.error) ||
      "";
  } catch {
    // non-JSON body
  }
  detail = String(detail).slice(0, 160);
  return new Error(detail ? `HTTP ${response.status}: ${detail}` : `HTTP ${response.status}`);
}

function describeError(err: unknown): string {
  if (err instanceof StreamingProbeError) {
    return `stream error: ${err.message}`.slice(0, 200);
  }
  if (err instanceof Error) {
    if (err.name === "TimeoutError" || err.name === "AbortError") {
      return "timeout";
    }
    return err.message.slice(0, 200);
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
    signal: AbortSignal.timeout(config.probeDeadlineMs),
  });
  if (!response.ok) {
    throw await httpError(response);
  }
  // A 2xx with an empty/malformed body means the model produced no embedding,
  // which a real client couldn't use — require a non-empty embedding vector.
  const body: any = await response.json().catch(() => null);
  const vector = body?.data?.[0]?.embedding;
  if (!Array.isArray(vector) || vector.length === 0) {
    throw new Error("empty embedding response");
  }
}

/**
 * Sends one non-streaming chat completion and returns its end-to-end latency.
 *
 * On Cloudflare Workers the clock only advances across I/O, so a streaming read
 * that delivers the whole body in one buffered chunk collapses latency onto
 * TTFT (both timestamps read the same frozen clock). A non-streaming request
 * completes at a single `await response.json()` I/O boundary, yielding an
 * accurate total latency regardless of how the upstream buffers its output.
 * Token count and TTFT come from the streaming probe; this measures latency
 * only.
 */
async function probeLatency(
  config: Config,
  apiKey: string,
  modelId: string,
): Promise<number> {
  const started = Date.now();
  const response = await fetch(`${config.gatewayBaseUrl}/v1/chat/completions`, {
    method: "POST",
    headers: headers(config, apiKey),
    body: JSON.stringify({
      model: modelId,
      messages: [
        { role: "system", content: "You are OpenCode" },
        { role: "user", content: config.probePrompt },
      ],
      max_tokens: config.probeMaxTokens,
      temperature: 1,
      stream: false,
    }),
    signal: AbortSignal.timeout(config.probeDeadlineMs),
  });
  if (!response.ok) {
    throw await httpError(response);
  }
  const body: any = await response.json().catch(() => null);
  const latencyMs = Date.now() - started;
  const content = body?.choices?.[0]?.message?.content;
  if (typeof content !== "string" || content.length === 0) {
    throw new Error("empty completion (no content generated)");
  }
  return latencyMs;
}

/**
 * Decode throughput (tokens/sec) over the post-TTFT window.
 *
 * `ttftMs` comes from the streaming probe (request A); `latencyMs` and
 * `completionTokens` come from the non-streaming probe (request B). Because
 * these are two separate requests, the inputs are not guaranteed monotonic.
 */
function decodeThroughput(
  ttftMs: number | null,
  latencyMs: number,
  completionTokens: number | null,
): number | null {
  if (completionTokens == null || completionTokens <= 1) return null;
  if (ttftMs == null || latencyMs <= ttftMs) return null;
  return (completionTokens - 1) / ((latencyMs - ttftMs) / 1000);
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

    // Request A — streaming: measure TTFT. On Workers, started→first-read spans
    // real I/O, so the first-token time is accurate even though the stream's own
    // end-time may collapse onto it when the whole body arrives in one read.
    const response = await fetch(`${config.gatewayBaseUrl}/v1/chat/completions`, {
      method: "POST",
      headers: headers(config, apiKey),
      body: JSON.stringify({
        model: target.id,
        messages: [
          { role: "system", content: "You are OpenCode" },
          { role: "user", content: config.probePrompt },
        ],
        max_tokens: config.probeMaxTokens,
        temperature: 1,
        stream: true,
        stream_options: { include_usage: true },
      }),
      // Total deadline: aborts even when SSE keepalives keep the stream open.
      signal: AbortSignal.timeout(config.probeDeadlineMs),
    });
    if (!response.ok) {
      throw await httpError(response);
    }
    if (!response.body) {
      throw new Error("no response body");
    }
    // TTFT and token count come from the streaming probe: started→first-read is
    // accurate on Workers, and consumeSse counts tokens with a usage fallback.
    const { ttftMs, completionTokens } = await consumeSse(response.body, started);

    // Request B — non-streaming: measure end-to-end latency at a real I/O
    // boundary, since the streaming read above can't on Workers.
    const latencyMs = await probeLatency(config, apiKey, target.id);

    const throughputTps = decodeThroughput(ttftMs, latencyMs, completionTokens);
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
