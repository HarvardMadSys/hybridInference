import { readFileSync } from "node:fs";

import { parse } from "smol-toml";

import { deriveEnvironment } from "../src/env.ts";

/**
 * Everything the deploy needs to know about one instance, resolved from the
 * config it is about to ship.
 *
 * The deploy used to carry these as literals — a hardcoded principal, a target
 * nobody stated at all — which is how #1252 moved `GATEWAY_BASE_URL` and left
 * every page reporting the old environment. Derived here, in one place, they
 * cannot disagree with what is deployed or with each other.
 */
export interface DeployParameters {
  /** wrangler's named environment; empty for the top-level (production) one. */
  readonly wranglerEnv: string;
  readonly workerName: string;
  readonly gatewayBaseUrl: string;
  /** The deployment this instance probes, and what its pages will be labelled. */
  readonly targetEnvironment: "production" | "staging";
  /** Derived from the target: partitions incidents and quota between instances. */
  readonly principal: string;
  readonly databaseName: string;
}

interface WranglerInstance {
  name?: string;
  vars?: Record<string, string>;
  d1_databases?: Array<{ database_name?: string }>;
}

interface WranglerConfig extends WranglerInstance {
  env?: Record<string, WranglerInstance>;
}

export function deployParameters(
  configToml: string,
  wranglerEnv: string,
): DeployParameters {
  const config = parse(configToml) as WranglerConfig;
  const instance: WranglerInstance | undefined =
    wranglerEnv === "" ? config : config.env?.[wranglerEnv];
  if (instance === undefined) {
    throw new Error(`wrangler.toml has no environment named "${wranglerEnv}"`);
  }

  const gatewayBaseUrl = instance.vars?.GATEWAY_BASE_URL?.replace(/\/+$/, "");
  if (!gatewayBaseUrl) {
    throw new Error(`environment "${wranglerEnv || "top-level"}" sets no GATEWAY_BASE_URL`);
  }

  const targetEnvironment = deriveEnvironment(gatewayBaseUrl);
  // `local` and `unknown` are what the derivation returns for a host it cannot
  // place. Registering one would attribute real pages to an environment nobody
  // operates, so this fails the deploy rather than the page.
  if (targetEnvironment !== "production" && targetEnvironment !== "staging") {
    throw new Error(`GATEWAY_BASE_URL is not a deployment we alert on: ${gatewayBaseUrl}`);
  }

  const workerName = instance.name;
  const databaseName = instance.d1_databases?.[0]?.database_name;
  if (!workerName || !databaseName) {
    throw new Error(
      `environment "${wranglerEnv || "top-level"}" is missing a Worker name or D1 binding`,
    );
  }

  return {
    wranglerEnv,
    workerName,
    gatewayBaseUrl,
    targetEnvironment,
    principal: `status-monitor-${targetEnvironment}`,
    databaseName,
  };
}

/** Emits `key=value` lines for `$GITHUB_OUTPUT`. */
function main(): void {
  const wranglerEnv = process.argv[2] ?? "";
  const parameters = deployParameters(readFileSync("wrangler.toml", "utf8"), wranglerEnv);
  for (const [key, value] of Object.entries(parameters)) {
    process.stdout.write(`${key}=${value}\n`);
  }
}

if (process.argv[1]?.endsWith("deploy-parameters.ts")) {
  main();
}
