import type { Env, TrustedEnvironment } from "./types";

const encoder = new TextEncoder();

function bearerToken(authorization: string | null): string | null {
  if (!authorization || authorization.length > 4_096) return null;
  const match = /^Bearer ([^\s]+)$/.exec(authorization);
  return match?.[1] ?? null;
}

/** Compare bearer values without exposing token length or an early mismatching byte. */
export async function timingSafeTokenEqual(provided: string, expected: string): Promise<boolean> {
  const [providedHash, expectedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(provided)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  const left = new Uint8Array(providedHash);
  const right = new Uint8Array(expectedHash);
  let difference = 0;
  for (let index = 0; index < left.length; index++) {
    difference |= left[index] ^ right[index];
  }
  return difference === 0;
}

export async function authenticateProducer(
  authorization: string | null,
  env: Env,
): Promise<TrustedEnvironment | null> {
  const provided = bearerToken(authorization);
  if (!provided) return null;
  const candidates: Array<[TrustedEnvironment, string | undefined]> = [
    ["staging", env.ALERT_RELAY_V2_STAGING_TOKEN],
    ["production", env.ALERT_RELAY_V2_PRODUCTION_TOKEN],
    ["local", env.ALERT_RELAY_V2_LOCAL_TOKEN],
  ];
  const matches: TrustedEnvironment[] = [];
  for (const [environment, rawExpected] of candidates) {
    const expected = rawExpected?.trim();
    if (expected && (await timingSafeTokenEqual(provided, expected))) {
      matches.push(environment);
    }
  }
  // Duplicate producer tokens make environment identity ambiguous and fail closed.
  return matches.length === 1 ? matches[0] : null;
}

export async function authenticateWorkflow(
  authorization: string | null,
  env: Env,
): Promise<boolean> {
  const provided = bearerToken(authorization);
  const expected = env.ALERT_RELAY_V2_WORKFLOW_TOKEN?.trim();
  return Boolean(provided && expected && (await timingSafeTokenEqual(provided, expected)));
}
