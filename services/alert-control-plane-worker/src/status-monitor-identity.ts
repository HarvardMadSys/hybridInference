import type { TrustedEnvironment, TrustedSource } from "./types";

/**
 * Identity vocabulary for the status-monitor producer role, shared by the two
 * places that must never disagree about it: registration, which decides what
 * may be written to the registry, and the RPC, which stamps what was written
 * onto every alert. Each used to hold its own copy of these constants — the
 * arrangement that lets a validator and a stamper drift apart unnoticed.
 */
export const STATUS_MONITOR_SERVICE = "status-monitor";
export const STATUS_MONITOR_SOURCE: TrustedSource = "status-monitor";

/**
 * The trust domain the monitor's own deployment is attested in.
 *
 * This is `staging` because that is what its deploy pipeline *is*: the Worker
 * ships from `dev` on every push, through the `staging` GitHub Environment, with
 * no approval gate. It says nothing about which gateway the monitor probes —
 * that is `targetEnvironment`, and the two are independent for a prober.
 */
export const STATUS_MONITOR_ENVIRONMENT: TrustedEnvironment = "staging";

const PRINCIPAL_BY_TARGET: Readonly<Record<TrustedEnvironment, string>> = {
  production: "status-monitor-production",
  staging: "status-monitor-staging",
};

export const STATUS_MONITOR_TARGETS: readonly TrustedEnvironment[] = [
  "production",
  "staging",
];

/**
 * The principal a monitor deployment runs as, derived from what it watches.
 *
 * Principal is the unit of both incident-route partitioning and quota
 * accounting, so deriving it from the target is what keeps two monitor
 * instances — which share a trust domain, a service name and a control plane —
 * from sharing an incident thread or a quota bucket. Deriving rather than
 * storing it also means the registry cannot hold a record whose principal and
 * target disagree.
 */
export function statusMonitorPrincipalFor(target: TrustedEnvironment): string {
  return PRINCIPAL_BY_TARGET[target];
}

/**
 * What records written before the split were stamped with.
 *
 * Their open incidents are routed by it, and principal is route material, so a
 * derived name would send a recovery to a Durable Object that holds nothing and
 * leave the original incident open forever. The control plane can be deployed
 * while a monitor incident is in flight — the cutover gate lives in the
 * monitor's own deploy, which happens later — so this is not a case the gate
 * can cover.
 */
export const LEGACY_STATUS_MONITOR_PRINCIPAL = "staging-monitor";

/**
 * The principal a monitor deployment runs as, preserving the pre-split name for
 * records that predate the field.
 *
 * A legacy record can only be the single monitor that existed when trust domain
 * and subject were one field, so pinning it to the old principal reproduces its
 * route material exactly — which is the whole compatibility guarantee. New
 * records always carry a target and always derive.
 */
export function statusMonitorPrincipalForRecord(
  targetEnvironment: string | null,
  resolvedTarget: TrustedEnvironment,
): string {
  return targetEnvironment === null
    ? LEGACY_STATUS_MONITOR_PRINCIPAL
    : statusMonitorPrincipalFor(resolvedTarget);
}

export function isStatusMonitorTarget(value: unknown): value is TrustedEnvironment {
  return (
    typeof value === "string" &&
    (STATUS_MONITOR_TARGETS as readonly string[]).includes(value)
  );
}
