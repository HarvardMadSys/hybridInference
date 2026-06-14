import { afterEach, describe, expect, it, vi } from "vitest";

import type { Config } from "../src/env";
import { discoverModels } from "../src/models";

const config = {
  gatewayBaseUrl: "https://gw.example",
  probeDeadlineMs: 1000,
} as Config;

function stubFetch(response: Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => response),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("discoverModels", () => {
  it("returns targets and detects embedding kind", async () => {
    stubFetch(
      new Response(
        JSON.stringify({
          data: [
            { id: "glm-4.7", output_modalities: ["text"] },
            { id: "bge-m3", output_modalities: ["embedding"] },
          ],
        }),
        { status: 200 },
      ),
    );
    const targets = await discoverModels(config, "k");
    expect(targets).toEqual([
      { id: "glm-4.7", kind: "chat" },
      { id: "bge-m3", kind: "embedding" },
    ]);
  });

  it("accepts a legitimately empty catalog", async () => {
    stubFetch(new Response(JSON.stringify({ data: [] }), { status: 200 }));
    expect(await discoverModels(config, "k")).toEqual([]);
  });

  it("throws on a malformed catalog (non-array data) instead of returning empty", async () => {
    stubFetch(new Response(JSON.stringify({ models: "oops" }), { status: 200 }));
    await expect(discoverModels(config, "k")).rejects.toThrow(/malformed/);
  });

  it("throws on a non-200 response", async () => {
    stubFetch(new Response("nope", { status: 503 }));
    await expect(discoverModels(config, "k")).rejects.toThrow(/HTTP 503/);
  });
});
