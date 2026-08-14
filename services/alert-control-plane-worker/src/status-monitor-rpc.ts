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
import type {
  AlertEvent,
  CanonicalAlertEnvelope,
  TrustedAlertMetadata,
  TrustedEnvironment,
} from "./types";
import {
  canonicalEventDigest,
  createCanonicalEnvelope,
  ValidationError,
} from "./validation";
import {
  isStatusMonitorTarget,
  statusMonitorPrincipalForRecord,
  STATUS_MONITOR_ENVIRONMENT,
  STATUS_MONITOR_SERVICE,
  STATUS_MONITOR_SOURCE,
} from "./status-monitor-identity";

const MAX_STATUS_MONITOR_BODY_BYTES = 64 * 1024;
/** The only alert types this role may open incidents for. */
const STATUS_MONITOR_ALERT_TYPES: ReadonlySet<string> = new Set([
  "model_unavailable",
  "monitoring_cycle_failure",
]);
const STATUS_MONITOR_CYCLE_FINGERPRINT = "status-monitor:cycle";
const STATUS_MONITOR_MODEL_FINGERPRINT_PREFIX = "status-monitor:model:";

/**
 * Bind each role alert type to its exact fingerprint namespace. Routing keys
 * on fingerprint alone, so the type allowlist by itself is not enough: a
 * monitoring_cycle_failure body carrying a model fingerprint would drive that
 * model's incident object (and vice versa), polluting its lifecycle with
 * events of another type. Exact string equality is sound here because the
 * canonical validator caps model_id at 256 characters, well below the
 * producer's 512-character fingerprint truncation threshold.
 */
function validateRoleFingerprint(event: AlertEvent): void {
  if (event.alert_type === "monitoring_cycle_failure") {
    if (event.fingerprint !== STATUS_MONITOR_CYCLE_FINGERPRINT) {
      throw new ValidationError(
        "monitoring_cycle_failure fingerprint must be the cycle fingerprint",
      );
    }
    return;
  }
  if (
    event.alert_type === "model_unavailable" &&
    event.fingerprint !==
      `${STATUS_MONITOR_MODEL_FINGERPRINT_PREFIX}${event.context.model_id}`
  ) {
    throw new ValidationError(
      "model_unavailable fingerprint must match its model_id",
    );
  }
}
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

/**
 * Resolve what a monitor deployment watches, tolerating records written before
 * the field existed.
 *
 * A pre-split record can only be the single monitor that ran when the trust
 * domain and the subject were the same field, so falling back to `environment`
 * restores exactly what it used to report. That keeps alerts flowing — and
 * incident identity stable — between deploying this control plane and the
 * monitor's next attestation, which is when the label actually changes.
 */
function targetEnvironment(
  deployment: TrustedDeploymentMetadata,
): TrustedEnvironment {
  const target = deployment.targetEnvironment ?? deployment.environment;
  if (!isStatusMonitorTarget(target)) {
    throw new DeploymentLookupError("deployment_mismatch");
  }
  return target;
}

function trustedMetadata(
  deployment: TrustedDeploymentMetadata,
): TrustedAlertMetadata {
  const target = targetEnvironment(deployment);
  return {
    // The producer's trust domain, fixed by which pipeline attested it. Not the
    // environment a responder should read — that is `target_environment`.
    environment: STATUS_MONITOR_ENVIRONMENT,
    target_environment: target,
    source: STATUS_MONITOR_SOURCE,
    // Derived for new records, so one can never carry a principal that
    // disagrees with what it watches; pinned for pre-split ones, whose open
    // incidents are routed by the name they were opened under.
    principal: statusMonitorPrincipalForRecord(deployment.targetEnvironment, target),
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
    validateRoleFingerprint(envelope.event);
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
