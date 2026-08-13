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

export function isStatusMonitorTarget(value: unknown): value is TrustedEnvironment {
  return (
    typeof value === "string" &&
    (STATUS_MONITOR_TARGETS as readonly string[]).includes(value)
  );
}
