import { analysisBranch, resolveAnalysisRef, type GitHubGateway } from "./github";
import {
  renderAnalysisReply,
  renderParent,
  renderRecoveryReply,
  renderUnavailableReply,
} from "./render";
import type { SlackWriter } from "./slack";
import { StoreConflictError, type RelayStore } from "./store";
import type {
  AlertEventV2,
  AnalysisJob,
  CompleteJobResult,
  Incident,
  JobCompletion,
  JobQueueMessage,
  SubmitAlertResult,
  TrustedEnvironment,
} from "./types";

export class RelayNotFoundError extends Error {}
export class RelayConflictError extends Error {}
export class RelayTemporaryError extends Error {}

export interface QueueSender {
  send(message: JobQueueMessage): Promise<unknown>;
}

export interface RelayServiceConfig {
  slackChannelId: string;
  model: string;
  modelBaseUrl: string;
}

/** Wrangler max_retries=2 means the initial delivery plus two retries. */
export const MAX_QUEUE_DELIVERY_ATTEMPTS = 3;

export interface WorkflowJobPayload {
  job_id: string;
  alert: AlertEventV2;
  environment: TrustedEnvironment;
  analysis_ref: string;
  model: string;
  base_url: string;
}

export class AlertRelayService {
  constructor(
    private readonly store: RelayStore,
    private readonly slack: SlackWriter,
    private readonly queue: QueueSender,
    private readonly github: GitHubGateway,
    private readonly config: RelayServiceConfig,
    private readonly now: () => string = () => new Date().toISOString(),
    private readonly uuid: () => string = () => crypto.randomUUID(),
  ) {}

  private result(
    event: AlertEventV2,
    incident: Incident | null,
    duplicate: boolean,
  ): SubmitAlertResult {
    return {
      accepted: true,
      duplicate,
      incident_id: incident?.id ?? null,
      status: event.status,
      occurrence_count: incident?.occurrenceCount ?? 0,
    };
  }

  private async enqueueIfNeeded(incident: Incident): Promise<void> {
    const job = await this.store.getJobForIncident(incident.id);
    if (!job || job.status !== "queued") return;
    try {
      await this.queue.send({ job_id: job.id });
    } catch {
      throw new RelayTemporaryError("analysis queue is temporarily unavailable");
    }
  }

  private async syncIncident(incident: Incident): Promise<Incident> {
    let current = incident;
    if (current.status === "opening") {
      const threadTs = await this.slack.postParent(renderParent(current), current.id);
      current = await this.store.activateIncident(current.id, threadTs, this.now());
    }
    if (!current.slackThreadTs) {
      throw new RelayTemporaryError("incident Slack parent is not ready");
    }
    const threadTs = current.slackThreadTs;
    if (current.recoveryPending) {
      if (!current.recoveryMessageId) {
        throw new RelayTemporaryError("incident recovery message is not ready");
      }
      await this.slack.postReply(
        threadTs,
        renderRecoveryReply(current),
        current.recoveryMessageId,
      );
      await this.store.markRecoveryPosted(current.id, this.now());
      current = (await this.store.getIncident(current.id)) ?? current;
    }
    for (let syncAttempt = 0; current.parentDirty && syncAttempt < 5; syncAttempt++) {
      const syncToken = this.uuid();
      const nowEpoch = Date.now();
      const claimed = await this.store.claimParentSync(
        current.id,
        syncToken,
        nowEpoch + 30_000,
        nowEpoch,
      );
      if (!claimed) {
        current = (await this.store.getIncident(current.id)) ?? current;
        if (current.parentDirty) {
          throw new RelayTemporaryError("incident parent synchronization is already in progress");
        }
        break;
      }
      current = (await this.store.getIncident(current.id)) ?? current;
      const renderedVersion = current.parentVersion;
      try {
        await this.slack.updateParent(threadTs, renderParent(current));
      } catch (error) {
        await this.store.releaseParentSync(current.id, syncToken, null, this.now());
        throw error;
      }
      await this.store.releaseParentSync(
        current.id,
        syncToken,
        renderedVersion,
        this.now(),
      );
      current = (await this.store.getIncident(current.id)) ?? current;
    }
    if (current.parentDirty) {
      throw new RelayTemporaryError("incident parent changed too quickly; retry synchronization");
    }
    await this.enqueueIfNeeded(current);
    return current;
  }

  private async replayReceipt(
    event: AlertEventV2,
    incidentId: string | null,
  ): Promise<SubmitAlertResult> {
    const incident = incidentId ? await this.store.getIncident(incidentId) : null;
    const synced = incident ? await this.syncIncident(incident) : null;
    return this.result(event, synced, true);
  }

  async submitAlert(
    event: AlertEventV2,
    environment: TrustedEnvironment,
  ): Promise<SubmitAlertResult> {
    const receipt = await this.store.getReceipt(event.alert_id);
    if (receipt) return this.replayReceipt(event, receipt.incidentId);

    let active = await this.store.getActiveIncident(environment, event.fingerprint);
    if (event.status === "resolved") {
      if (!active) {
        try {
          await this.store.recordOrphanResolution(event, this.now());
        } catch (error) {
          if (error instanceof StoreConflictError) {
            const duplicate = await this.store.getReceipt(event.alert_id);
            return this.replayReceipt(event, duplicate?.incidentId ?? null);
          }
          throw error;
        }
        return this.result(event, null, false);
      }
      if (active.status === "opening") {
        active = await this.syncIncident(active);
      }
      try {
        const resolved = await this.store.resolveIncident(
          active.id,
          event,
          this.uuid(),
          this.now(),
        );
        return this.result(event, await this.syncIncident(resolved), false);
      } catch (error) {
        if (error instanceof StoreConflictError) {
          const duplicate = await this.store.getReceipt(event.alert_id);
          if (duplicate) return this.replayReceipt(event, duplicate.incidentId);
          throw new RelayTemporaryError("incident state changed; retry the alert");
        }
        throw error;
      }
    }

    if (active) {
      if (active.status === "opening") {
        active = await this.syncIncident(active);
      }
      try {
        const repeated = await this.store.recordRepeat(active.id, event, this.now());
        return this.result(event, await this.syncIncident(repeated), false);
      } catch (error) {
        if (error instanceof StoreConflictError) {
          const duplicate = await this.store.getReceipt(event.alert_id);
          if (duplicate) return this.replayReceipt(event, duplicate.incidentId);
          throw new RelayTemporaryError("incident state changed; retry the alert");
        }
        throw error;
      }
    }

    const incidentId = this.uuid();
    const jobId = this.uuid();
    try {
      const opening = await this.store.createOpening(
        event,
        environment,
        this.config.slackChannelId,
        incidentId,
        jobId,
        this.now(),
      );
      return this.result(event, await this.syncIncident(opening), false);
    } catch (error) {
      if (error instanceof StoreConflictError) {
        const duplicate = await this.store.getReceipt(event.alert_id);
        if (duplicate) return this.replayReceipt(event, duplicate.incidentId);
        throw new RelayTemporaryError("incident state changed; retry the alert");
      }
      throw error;
    }
  }

  async getWorkflowJob(jobId: string): Promise<WorkflowJobPayload> {
    const job = await this.store.getJob(jobId);
    if (
      !job ||
      !job.analysisRef ||
      !["dispatching", "dispatched"].includes(job.status)
    ) {
      throw new RelayNotFoundError("job not found");
    }
    const incident = await this.store.getIncident(job.incidentId);
    if (!incident) throw new RelayNotFoundError("job incident not found");
    return {
      job_id: job.id,
      alert: incident.alert,
      environment: incident.environment,
      analysis_ref: job.analysisRef,
      model: this.config.model,
      base_url: this.config.modelBaseUrl,
    };
  }

  async completeJob(jobId: string, completion: JobCompletion): Promise<CompleteJobResult> {
    let job = await this.store.getJob(jobId);
    if (!job) throw new RelayNotFoundError("job not found");
    if (job.status === "completed" || job.status === "failed") {
      const incident = await this.store.getIncident(job.incidentId);
      if (incident) await this.syncIncident(incident);
      return {
        accepted: true,
        duplicate: true,
        status: job.status,
      };
    }
    const claimed = await this.store.beginCompletion(jobId, completion, this.now());
    job = (await this.store.getJob(jobId)) ?? job;
    if (!claimed && job.status !== "completing") {
      throw new RelayConflictError("job is not ready for completion");
    }
    if (
      !claimed &&
      job.completion &&
      JSON.stringify(job.completion) !== JSON.stringify(completion)
    ) {
      throw new RelayConflictError("job already has a different completion");
    }

    const incident = await this.store.getIncident(job.incidentId);
    if (!incident?.slackThreadTs) throw new RelayConflictError("job incident is not ready");
    try {
      if (completion.status === "success") {
        await this.slack.postReply(
          incident.slackThreadTs,
          renderAnalysisReply(incident, completion.analysis, completion.codex_thread_id),
          job.id,
        );
        await this.store.finishCompletion(job.id, "completed", "analysis_ready", this.now());
      } else {
        await this.slack.postReply(
          incident.slackThreadTs,
          renderUnavailableReply(
            incident,
            `The analysis workflow failed: ${completion.error}`,
            completion.run_url,
          ),
          job.id,
        );
        await this.store.finishCompletion(job.id, "failed", "unavailable", this.now());
      }
    } catch (error) {
      await this.store.resetCompletion(job.id, "Slack callback delivery failed", this.now());
      throw error;
    }
    const updated = await this.store.getIncident(incident.id);
    if (updated) await this.syncIncident(updated);
    return {
      accepted: true,
      duplicate: false,
      status: completion.status === "success" ? "completed" : "failed",
    };
  }

  private async postFinalDispatchFailure(job: AnalysisJob): Promise<void> {
    const incident = await this.store.getIncident(job.incidentId);
    if (!incident?.slackThreadTs) {
      throw new RelayTemporaryError("failed job incident has no Slack parent");
    }
    await this.slack.postReply(
      incident.slackThreadTs,
      renderUnavailableReply(
        incident,
        `The analysis workflow could not be dispatched after ${job.attempts} attempts.`,
      ),
      job.id,
    );
    const updated = await this.store.getIncident(incident.id);
    if (updated) await this.syncIncident(updated);
  }

  async processQueueJob(jobId: string, attempt: number): Promise<"ack" | "retry"> {
    let job = await this.store.getJob(jobId);
    if (!job) return "ack";
    if (job.status === "failed") {
      await this.postFinalDispatchFailure(job);
      return "ack";
    }
    if (["dispatched", "completing", "completed"].includes(job.status)) return "ack";
    const incident = await this.store.getIncident(job.incidentId);
    if (!incident) return "ack";

    try {
      const analysisRef = await resolveAnalysisRef(
        incident.environment,
        incident.alert.deployment_sha,
        this.github,
      );
      const claimed = await this.store.setAnalysisRef(job.id, analysisRef, attempt, this.now());
      if (!claimed) return "ack";
      const updatedIncident = await this.store.getIncident(incident.id);
      if (updatedIncident) await this.syncIncident(updatedIncident);
      await this.github.dispatch(job.id, analysisBranch(incident.environment));
      await this.store.markDispatched(job.id, this.now());
      return "ack";
    } catch (error) {
      const final = attempt >= MAX_QUEUE_DELIVERY_ATTEMPTS;
      const message = error instanceof Error ? error.message : "workflow dispatch failed";
      await this.store.retryDispatch(job.id, message, final, this.now());
      if (!final) return "retry";
      job = (await this.store.getJob(job.id)) ?? job;
      await this.postFinalDispatchFailure(job);
      return "ack";
    }
  }
}
