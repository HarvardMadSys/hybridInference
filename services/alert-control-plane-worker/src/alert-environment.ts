import type { TrustedAlertMetadata, TrustedEnvironment } from "./types";

/**
 * The environment an alert is *about* — what a responder needs to see and what
 * an incident's identity is scoped by.
 *
 * Prefer this over reading `trusted.environment` directly anywhere the answer is
 * "which deployment broke": that field is the producer's trust domain, which is
 * only incidentally the same thing (see {@link TrustedAlertMetadata}).
 *
 * The fallback exists because envelopes persisted before `target_environment`
 * are rehydrated by a cast rather than by `parseTrustedMetadata`, so the field
 * is genuinely absent at runtime on those rows despite the type. Falling back to
 * the trust domain reproduces exactly the pre-split behaviour for them, which
 * keeps an in-flight incident's identity stable across the upgrade. New
 * status-monitor records may not omit the field — that is enforced at
 * registration, not here.
 */
export function alertEnvironment(trusted: TrustedAlertMetadata): TrustedEnvironment {
  return trusted.target_environment ?? trusted.environment;
}
