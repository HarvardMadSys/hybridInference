import { describe, expect, it } from "vitest";

import { authenticateProducer, authenticateWorkflow, timingSafeTokenEqual } from "../src/auth";
import type { Env } from "../src/types";

describe("bearer authentication", () => {
  const env = {
    ALERT_RELAY_V2_STAGING_TOKEN: "staging-secret",
    ALERT_RELAY_V2_PRODUCTION_TOKEN: "production-secret",
    ALERT_RELAY_V2_LOCAL_TOKEN: "local-secret",
    ALERT_RELAY_V2_WORKFLOW_TOKEN: "workflow-secret",
  } as Env;

  it("maps each producer credential to a trusted environment", async () => {
    await expect(authenticateProducer("Bearer staging-secret", env)).resolves.toBe("staging");
    await expect(authenticateProducer("Bearer production-secret", env)).resolves.toBe(
      "production",
    );
    await expect(authenticateProducer("Bearer local-secret", env)).resolves.toBe("local");
  });

  it("does not accept the workflow token as a producer token or vice versa", async () => {
    await expect(authenticateProducer("Bearer workflow-secret", env)).resolves.toBeNull();
    await expect(authenticateWorkflow("Bearer staging-secret", env)).resolves.toBe(false);
    await expect(authenticateWorkflow("Bearer workflow-secret", env)).resolves.toBe(true);
  });

  it("fails closed for malformed headers and duplicate producer secrets", async () => {
    await expect(authenticateProducer("Basic staging-secret", env)).resolves.toBeNull();
    await expect(
      authenticateProducer("Bearer shared", {
        ...env,
        ALERT_RELAY_V2_STAGING_TOKEN: "shared",
        ALERT_RELAY_V2_PRODUCTION_TOKEN: "shared",
      }),
    ).resolves.toBeNull();
  });

  it("compares differently-sized tokens through fixed-size digests", async () => {
    await expect(timingSafeTokenEqual("a", "a")).resolves.toBe(true);
    await expect(timingSafeTokenEqual("a", "a-much-longer-token")).resolves.toBe(false);
  });
});
