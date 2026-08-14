import { readFileSync } from "node:fs";

import { parse } from "smol-toml";
import { describe, expect, it } from "vitest";

import { deriveEnvironment } from "../src/env";
import { deployParameters } from "../scripts/deploy-parameters.ts";

/**
 * The two instances share one Worker and one `wrangler.toml`, but wrangler does
 * not inherit `vars` into a named environment — it replaces them wholesale. So
 * every var the two are meant to agree on exists twice on disk, and the only
 * thing keeping them equal is that someone remembers to edit both.
 *
 * That is the same arrangement that produced the bug this pair of changes
 * exists to fix: two values that had to agree, no mechanism making them. These
 * tests are that mechanism.
 */
const configToml = readFileSync("wrangler.toml", "utf8");
const config = parse(configToml) as {
  name: string;
  vars: Record<string, string>;
  triggers?: { crons?: string[] };
  services: Array<Record<string, string>>;
  d1_databases: Array<Record<string, string>>;
  env: {
    staging: {
      name: string;
      vars: Record<string, string>;
      services: Array<Record<string, string>>;
      d1_databases: Array<Record<string, string>>;
      version_metadata?: { binding: string };
    };
  };
};

const production = config;
const staging = config.env.staging;

/** The one var the two instances are supposed to disagree about. */
const PER_INSTANCE_VARS = new Set(["GATEWAY_BASE_URL"]);

describe("wrangler environments", () => {
  it("gives the two instances the same knobs", () => {
    expect(Object.keys(staging.vars).sort()).toEqual(Object.keys(production.vars).sort());
  });

  it("keeps every shared var identical", () => {
    const shared = (vars: Record<string, string>) =>
      Object.fromEntries(
        Object.entries(vars).filter(([key]) => !PER_INSTANCE_VARS.has(key)),
      );

    // A probe prompt, deadline or failure threshold that drifted between the
    // two would make their measurements quietly incomparable, which is most of
    // the value of running both.
    expect(shared(staging.vars)).toEqual(shared(production.vars));
  });

  it("points each instance at the deployment it is named for", () => {
    expect(deriveEnvironment(production.vars.GATEWAY_BASE_URL)).toBe("production");
    expect(deriveEnvironment(staging.vars.GATEWAY_BASE_URL)).toBe("staging");
  });

  it("gives each instance its own Worker and its own database", () => {
    // Sharing either would merge two deployments' probe history and let one
    // instance's cron overwrite the other's.
    expect(staging.name).not.toBe(production.name);
    expect(staging.d1_databases[0].database_name).not.toBe(
      production.d1_databases[0].database_name,
    );
    expect(staging.d1_databases[0].database_id).not.toBe(
      production.d1_databases[0].database_id,
    );
  });

  it("restates the bindings a named environment does not inherit", () => {
    // wrangler drops these silently rather than failing, and a Worker missing
    // its control-plane binding degrades to legacy delivery instead of erroring.
    expect(staging.version_metadata?.binding).toBe("CF_VERSION_METADATA");
    expect(staging.services).toEqual(production.services);
    expect(staging.d1_databases[0].binding).toBe("DB");
    expect(staging.d1_databases[0].migrations_dir).toBe(
      production.d1_databases[0].migrations_dir,
    );
  });

  it("resolves each instance's deploy parameters from that instance's config", () => {
    // The deploy reads these instead of carrying literals, so pinning them here
    // pins what gets attested: a target that no longer matches the URL, or a
    // principal that no longer matches the target, fails before it can ship.
    expect(deployParameters(configToml, "")).toEqual({
      wranglerEnv: "",
      workerName: "freeinference-monitor",
      gatewayBaseUrl: "https://freeinference.org",
      targetEnvironment: "production",
      principal: "status-monitor-production",
      databaseName: "freeinference-monitor",
    });
    expect(deployParameters(configToml, "staging")).toEqual({
      wranglerEnv: "staging",
      workerName: "freeinference-monitor-staging",
      gatewayBaseUrl: "https://staging.freeinference.org",
      targetEnvironment: "staging",
      principal: "status-monitor-staging",
      databaseName: "freeinference-monitor-staging",
    });
  });

  it("refuses to resolve a gateway it cannot place", () => {
    const withGateway = (url: string) =>
      configToml.replace(
        /^GATEWAY_BASE_URL = ".*"$/m,
        `GATEWAY_BASE_URL = "${url}"`,
      );

    expect(() => deployParameters(withGateway("http://localhost:8787"), "")).toThrow(
      /not a deployment we alert on/,
    );
    expect(() => deployParameters(withGateway("https://example.com"), "")).toThrow(
      /not a deployment we alert on/,
    );
    expect(() => deployParameters(configToml, "nope")).toThrow(/no environment named/);
  });

  it("relies on triggers being inherited rather than restating them", () => {
    // `triggers` IS inherited, so a second copy could only ever drift. If a
    // future wrangler stops inheriting it, this fails rather than leaving the
    // staging instance silently never probing.
    expect(production.triggers?.crons).toEqual(["*/20 * * * *"]);
    expect(staging).not.toHaveProperty("triggers");
  });
});
