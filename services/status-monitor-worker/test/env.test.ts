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
