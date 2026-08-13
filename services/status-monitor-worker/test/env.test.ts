import { describe, expect, it } from "vitest";

import { type Env, loadConfig } from "../src/env";

function env(overrides: Partial<Env> = {}): Env {
  return { DB: {}, PROBER_API_KEY: "k", GATEWAY_BASE_URL: "https://gw.example", ...overrides } as Env;
}

describe("loadConfig alertFailureThreshold", () => {
  it("defaults to 2 when unset", () => {
    expect(loadConfig(env()).alertFailureThreshold).toBe(2);
  });

  it("parses a positive integer", () => {
    expect(loadConfig(env({ ALERT_FAILURE_THRESHOLD: "3" })).alertFailureThreshold).toBe(3);
    expect(loadConfig(env({ ALERT_FAILURE_THRESHOLD: "1" })).alertFailureThreshold).toBe(1);
  });

  it("falls back to the default for zero, negative, or non-numeric values", () => {
    expect(loadConfig(env({ ALERT_FAILURE_THRESHOLD: "0" })).alertFailureThreshold).toBe(2);
    expect(loadConfig(env({ ALERT_FAILURE_THRESHOLD: "-1" })).alertFailureThreshold).toBe(2);
    expect(loadConfig(env({ ALERT_FAILURE_THRESHOLD: "abc" })).alertFailureThreshold).toBe(2);
  });
});

describe("loadConfig alertStormThreshold", () => {
  it("defaults to 5 and parses a positive override", () => {
    expect(loadConfig(env()).alertStormThreshold).toBe(5);
    expect(loadConfig(env({ ALERT_STORM_THRESHOLD: "10" })).alertStormThreshold).toBe(10);
  });

  it("falls back to the default for invalid values", () => {
    expect(loadConfig(env({ ALERT_STORM_THRESHOLD: "0" })).alertStormThreshold).toBe(5);
    expect(loadConfig(env({ ALERT_STORM_THRESHOLD: "nope" })).alertStormThreshold).toBe(5);
  });
});

describe("loadConfig targetEnvironment", () => {
  it("derives the probed deployment from the configured gateway", () => {
    const target = (url: string) =>
      loadConfig(env({ GATEWAY_BASE_URL: url })).targetEnvironment;

    expect(target("https://freeinference.org")).toBe("production");
    expect(target("https://staging.freeinference.org")).toBe("staging");
    // The port must not defeat the match, and a trailing slash is stripped
    // before the URL is ever parsed.
    expect(target("https://freeinference.org:8443/")).toBe("production");
    expect(target("http://localhost:8787")).toBe("local");
    expect(target("https://gw.example")).toBe("unknown");
  });

  it("cannot disagree with the gateway it is configured to probe", () => {
    // The pairing is derived, not stated. #1252 moved the URL and left every
    // page reading the old environment precisely because those were two
    // independent edits; there is no second value here to forget.
    const config = loadConfig(env({ GATEWAY_BASE_URL: "https://freeinference.org" }));

    expect(config.gatewayBaseUrl).toBe("https://freeinference.org");
    expect(config.targetEnvironment).toBe("production");
  });
});
