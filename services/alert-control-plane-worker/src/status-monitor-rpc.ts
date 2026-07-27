import {
  IngressConflictError,
  IngressUnavailableError,
  type IncidentAcknowledgement,
} from "./ingress";
import { BoundDeploymentRegistryClient } from "./registry-runtime";
import {
  DeploymentLookupError,
  type TrustedDeploymentMetadata,
} from "./registry";
import { routeNameForEnvelope } from "./routing";
import {
  parseRuntimeConfig,
  type RuntimeEnvironment,
} from "./runtime-config";
import type { CanonicalAlertEnvelope, TrustedAlertMetadata } from "./types";
import {
  canonicalEventDigest,
  createCanonicalEnvelope,
  ValidationError,
} from "./validation";

const MAX_STATUS_MONITOR_BODY_BYTES = 64 * 1024;
const STATUS_MONITOR_ENVIRONMENT = "staging";
const STATUS_MONITOR_SERVICE = "status-monitor";
const STATUS_MONITOR_SOURCE = "status-monitor";
const STATUS_MONITOR_PRINCIPAL = "staging-monitor";
/** The only alert types this role may open incidents for. */
const STATUS_MONITOR_ALERT_TYPES: ReadonlySet<string> = new Set([
  "model_unavailable",
  "monitoring_cycle_failure",
]);
const CLOUDFLARE_VERSION_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export interface StatusMonitorRpcEnvironment extends RuntimeEnvironment {
  readonly INCIDENTS: DurableObjectNamespace;
}

export type StatusMonitorRpcErrorCode =
  | "control_plane_dormant"
  | "control_plane_unavailable"
  | "deployment_mismatch"
  | "event_id_conflict"
  | "invalid_event"
  | "invalid_producer_identity"
  | "retired_deployment"
  | "unknown_deployment";

export type StatusMonitorRpcResult =
  | {
      readonly accepted: true;
      readonly acknowledgement: IncidentAcknowledgement;
    }
  | {
      readonly accepted: false;
      readonly errorCode: StatusMonitorRpcErrorCode;
    };

export interface StatusMonitorIncidentSubmitter {
  (
    namespace: DurableObjectNamespace,
    incidentName: string,
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
  ): Promise<IncidentAcknowledgement>;
}

function validDeploymentId(value: unknown): value is string {
  return typeof value === "string" && CLOUDFLARE_VERSION_ID_RE.test(value);
}

function parseEventBody(bodyJson: unknown): unknown {
  if (typeof bodyJson !== "string") {
    throw new ValidationError("status-monitor body must be a string");
  }
  if (
    new TextEncoder().encode(bodyJson).byteLength >
    MAX_STATUS_MONITOR_BODY_BYTES
  ) {
    throw new ValidationError("status-monitor body is too large");
  }
  try {
    return JSON.parse(bodyJson) as unknown;
  } catch {
    throw new ValidationError("status-monitor body must be valid JSON");
  }
}

function trustedMetadata(
  deployment: TrustedDeploymentMetadata,
): TrustedAlertMetadata {
  return {
    environment: "staging",
    source: STATUS_MONITOR_SOURCE,
    principal: STATUS_MONITOR_PRINCIPAL,
    deployment_id: deployment.deploymentId,
    deployment_sha: deployment.deploymentSha,
    artifact_digest: deployment.artifactDigest,
    registry_version: deployment.registryVersion,
  };
}

function validateResolvedDeployment(
  deployment: TrustedDeploymentMetadata,
  deploymentId: string,
): void {
  if (deployment.retiredAt !== null) {
    throw new DeploymentLookupError("retired_deployment");
  }
  if (
    deployment.environment !== STATUS_MONITOR_ENVIRONMENT ||
    deployment.service !== STATUS_MONITOR_SERVICE ||
    deployment.deploymentId !== deploymentId
  ) {
    throw new DeploymentLookupError("deployment_mismatch");
  }
}

function stableFailure(error: unknown): StatusMonitorRpcResult {
  if (error instanceof DeploymentLookupError) {
    return { accepted: false, errorCode: error.code };
  }
  if (error instanceof ValidationError || error instanceof TypeError) {
    return { accepted: false, errorCode: "invalid_event" };
  }
  if (error instanceof IngressConflictError) {
    return { accepted: false, errorCode: "event_id_conflict" };
  }
  if (error instanceof IngressUnavailableError) {
    return { accepted: false, errorCode: "control_plane_unavailable" };
  }
  console.error("status-monitor RPC submission failed");
  return { accepted: false, errorCode: "control_plane_unavailable" };
}

/**
 * Role-specific internal submission path for the status-monitor Worker.
 *
 * The caller supplies only the persisted canonical body and its immutable
 * Cloudflare Worker version ID. Every trusted identity field is fixed here or
 * loaded from the active DeploymentRegistry record.
 */
export async function submitStatusMonitorRpcEvent(
  env: StatusMonitorRpcEnvironment,
  bodyJson: unknown,
  deploymentId: unknown,
  submit: StatusMonitorIncidentSubmitter,
): Promise<StatusMonitorRpcResult> {
  const config = parseRuntimeConfig(env);
  if (config.mode !== "staging-ingress") {
    return { accepted: false, errorCode: "control_plane_dormant" };
  }
  if (!validDeploymentId(deploymentId)) {
    return { accepted: false, errorCode: "invalid_producer_identity" };
  }

  try {
    const event = parseEventBody(bodyJson);
    const registry = new BoundDeploymentRegistryClient(config);
    const deployment = await registry.lookupByDeploymentId({
      environment: STATUS_MONITOR_ENVIRONMENT,
      service: STATUS_MONITOR_SERVICE,
      deploymentId,
    });
    validateResolvedDeployment(deployment, deploymentId);
    const envelope = createCanonicalEnvelope(
      event,
      trustedMetadata(deployment),
    );
    // The shared producer validator accepts every supported alert type, but this
    // entrypoint is role-specific: status-monitor may only open the incident
    // classes it owns (individual models and its own cycle failures). Without
    // this check a `provider_circuit_open` body would reach the same incident
    // object (routing keys on fingerprint, not alert type) and carry free-text
    // fields the status-monitor contracts deliberately exclude.
    if (!STATUS_MONITOR_ALERT_TYPES.has(envelope.event.alert_type)) {
      throw new ValidationError(
        "status-monitor may only submit model_unavailable or monitoring_cycle_failure events",
      );
    }
    const bodyDigest = await canonicalEventDigest(envelope.event);
    const incidentName = await routeNameForEnvelope(
      config.routeKey,
      envelope.trusted,
      envelope.event,
    );
    const acknowledgement = await submit(
      env.INCIDENTS,
      incidentName,
      envelope,
      bodyDigest,
    );
    return { accepted: true, acknowledgement };
  } catch (error) {
    return stableFailure(error);
  }
}
