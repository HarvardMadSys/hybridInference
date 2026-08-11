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

  // Previously this returned [] as a "legitimately empty catalog". That was safe
  // while a departed model only had its state dropped, but once the alerter began
  // resolving departed control-plane incidents an empty catalog would recover
  // every open incident at once and wipe the history behind each failure streak.
  it("throws on an empty catalog instead of reporting a successful zero-model cycle", async () => {
    stubFetch(new Response(JSON.stringify({ data: [] }), { status: 200 }));
    await expect(discoverModels(config, "k")).rejects.toThrow(/no usable probe targets/);
  });

  it("throws when no catalog entry yields a usable target", async () => {
    stubFetch(
      new Response(JSON.stringify({ data: [{ name: "no-id" }, { id: 42 }] }), {
        status: 200,
      }),
    );
    await expect(discoverModels(config, "k")).rejects.toThrow(/no usable probe targets/);
  });

  // A blank id would otherwise be probed as a real model and count as a present
  // one, making a degenerate catalog look usable while every genuine model reads
  // as departed.
  it("throws on a catalog of blank ids rather than treating them as targets", async () => {
    stubFetch(
      new Response(JSON.stringify({ data: [{ id: "" }, { id: "   " }] }), {
        status: 200,
      }),
    );
    await expect(discoverModels(config, "k")).rejects.toThrow(/no usable probe targets/);
  });

  it("skips blank ids but keeps the usable rest of the catalog", async () => {
    stubFetch(
      new Response(JSON.stringify({ data: [{ id: " " }, { id: "glm-4.7" }] }), {
        status: 200,
      }),
    );
    expect(await discoverModels(config, "k")).toEqual([{ id: "glm-4.7", kind: "chat" }]);
  });

  // An on-demand model loads lazily on shared GPUs; a synthetic probe is the
  // traffic that defeats that design (it pins the GPUs it probes and pages on
  // the "no vacant GPU" failures of the rest). The catalog flags them and the
  // prober leaves them alone.
  it("skips models the catalog marks on_demand", async () => {
    stubFetch(
      new Response(
        JSON.stringify({
          data: [
            { id: "glm-4.7", output_modalities: ["text"] },
            { id: "llama-3.1-8b", output_modalities: ["text"], on_demand: true },
            { id: "alia-40b-instruct", on_demand: true },
          ],
        }),
        { status: 200 },
      ),
    );
    expect(await discoverModels(config, "k")).toEqual([{ id: "glm-4.7", kind: "chat" }]);
  });

  // Strict boolean: the flag crosses a JSON boundary, and a truthy string such
  // as "false" must not silently drop a model from monitoring.
  it("keeps probing a model whose on_demand flag is not literally true", async () => {
    stubFetch(
      new Response(
        JSON.stringify({
          data: [
            { id: "a", on_demand: "true" },
            { id: "b", on_demand: 1 },
            { id: "c", on_demand: false },
          ],
        }),
        { status: 200 },
      ),
    );
    expect((await discoverModels(config, "k")).map((t) => t.id)).toEqual(["a", "b", "c"]);
  });

  // A catalog with nothing the monitor may probe is a configuration to fail
  // loudly on, not a successful zero-model cycle that wipes history.
  it("throws when every model in the catalog is on-demand", async () => {
    stubFetch(
      new Response(
        JSON.stringify({
          data: [
            { id: "a", on_demand: true },
            { id: "b", on_demand: true },
          ],
        }),
        { status: 200 },
      ),
    );
    await expect(discoverModels(config, "k")).rejects.toThrow(
      /no usable probe targets \(2 on-demand model\(s\) excluded\)/,
    );
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
