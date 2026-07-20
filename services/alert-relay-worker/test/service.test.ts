import { describe, expect, it, vi } from "vitest";

import type { GitHubGateway } from "../src/github";
import { AlertRelayService, RelayNotFoundError } from "../src/service";
import type { SlackWriter } from "../src/slack";
import { StoreConflictError, type RelayStore } from "../src/store";
import type {
  AlertEventV2,
  AlertReceipt,
  AnalysisJob,
  CodexStatus,
  Incident,
  JobCompletion,
  JobQueueMessage,
  SlackMessage,
  TrustedEnvironment,
} from "../src/types";

function event(
  alertId: string,
  status: "firing" | "resolved" = "firing",
  fingerprint = "gateway:provider:openai",
): AlertEventV2 {
  return {
    version: "2",
    alert_id: alertId,
    fingerprint,
    source: "gateway",
    status,
    severity: status === "firing" ? "error" : "info",
    title: status === "firing" ? "Provider failed" : "Provider recovered",
    occurred_at: status === "firing" ? "2026-07-19T12:00:00.000Z" : "2026-07-19T12:10:00.000Z",
    summary: status === "firing" ? "Requests fail" : "Requests recovered",
    context: { provider: "openai" },
    deployment_sha: "a".repeat(40),
  };
}

class MemoryStore implements RelayStore {
  readonly receipts = new Map<string, AlertReceipt>();
  readonly incidents = new Map<string, Incident>();
  readonly jobs = new Map<string, AnalysisJob>();

  private activeKey(environment: TrustedEnvironment, fingerprint: string): string {
    return `${environment}\n${fingerprint}`;
  }

  private active = new Map<string, string>();
  private parentSync = new Map<string, string>();

  async getReceipt(alertId: string): Promise<AlertReceipt | null> {
    return this.receipts.get(alertId) ?? null;
  }

  async getIncident(incidentId: string): Promise<Incident | null> {
    return this.incidents.get(incidentId) ?? null;
  }

  async getActiveIncident(
    environment: TrustedEnvironment,
    fingerprint: string,
  ): Promise<Incident | null> {
    const id = this.active.get(this.activeKey(environment, fingerprint));
    return id ? (this.incidents.get(id) ?? null) : null;
  }

  async getJob(jobId: string): Promise<AnalysisJob | null> {
    return this.jobs.get(jobId) ?? null;
  }

  async getJobForIncident(incidentId: string): Promise<AnalysisJob | null> {
    return [...this.jobs.values()].find((job) => job.incidentId === incidentId) ?? null;
  }

  async createOpening(
    alert: AlertEventV2,
    environment: TrustedEnvironment,
    channelId: string,
    incidentId: string,
    jobId: string,
    now: string,
  ): Promise<Incident> {
    const key = this.activeKey(environment, alert.fingerprint);
    if (this.receipts.has(alert.alert_id) || this.active.has(key)) {
      throw new StoreConflictError("conflict");
    }
    const incident: Incident = {
      id: incidentId,
      environment,
      fingerprint: alert.fingerprint,
      status: "opening",
      alert,
      resolutionAlert: null,
      occurrenceCount: 1,
      firstSeen: alert.occurred_at,
      lastSeen: alert.occurred_at,
      slackChannelId: channelId,
      slackThreadTs: null,
      codexStatus: "investigating",
      analysisRef: null,
      parentDirty: true,
      parentVersion: 1,
      recoveryPending: false,
      recoveryMessageId: null,
    };
    this.incidents.set(incidentId, incident);
    this.active.set(key, incidentId);
    this.jobs.set(jobId, {
      id: jobId,
      incidentId,
      status: "waiting",
      analysisRef: null,
      attempts: 0,
      lastError: null,
      completion: null,
    });
    this.receipts.set(alert.alert_id, {
      alertId: alert.alert_id,
      incidentId,
      action: "opened",
      receivedAt: now,
    });
    return incident;
  }

  async activateIncident(incidentId: string, threadTs: string): Promise<Incident> {
    const incident = this.incidents.get(incidentId)!;
    incident.status = "firing";
    incident.slackThreadTs = threadTs;
    incident.parentDirty = false;
    const job = await this.getJobForIncident(incidentId);
    if (job?.status === "waiting") job.status = "queued";
    return incident;
  }

  async recordRepeat(incidentId: string, alert: AlertEventV2, now: string): Promise<Incident> {
    if (this.receipts.has(alert.alert_id)) throw new StoreConflictError("duplicate");
    const incident = this.incidents.get(incidentId);
    if (!incident || !["opening", "firing"].includes(incident.status)) {
      throw new StoreConflictError("inactive");
    }
    incident.alert = alert;
    incident.occurrenceCount += 1;
    if (incident.lastSeen < alert.occurred_at) incident.lastSeen = alert.occurred_at;
    incident.parentDirty = true;
    incident.parentVersion += 1;
    this.receipts.set(alert.alert_id, {
      alertId: alert.alert_id,
      incidentId,
      action: "repeated",
      receivedAt: now,
    });
    return incident;
  }

  async resolveIncident(
    incidentId: string,
    alert: AlertEventV2,
    recoveryMessageId: string,
    now: string,
  ): Promise<Incident> {
    if (this.receipts.has(alert.alert_id)) throw new StoreConflictError("duplicate");
    const incident = this.incidents.get(incidentId)!;
    this.active.delete(this.activeKey(incident.environment, incident.fingerprint));
    incident.status = "resolved";
    incident.resolutionAlert = alert;
    if (incident.lastSeen < alert.occurred_at) incident.lastSeen = alert.occurred_at;
    incident.codexStatus = "resolved";
    incident.parentDirty = true;
    incident.parentVersion += 1;
    incident.recoveryPending = true;
    incident.recoveryMessageId = recoveryMessageId;
    this.receipts.set(alert.alert_id, {
      alertId: alert.alert_id,
      incidentId,
      action: "resolved",
      receivedAt: now,
    });
    return incident;
  }

  async recordOrphanResolution(alert: AlertEventV2, now: string): Promise<void> {
    if (this.receipts.has(alert.alert_id)) throw new StoreConflictError("duplicate");
    this.receipts.set(alert.alert_id, {
      alertId: alert.alert_id,
      incidentId: null,
      action: "orphan_resolution",
      receivedAt: now,
    });
  }

  async claimParentSync(
    incidentId: string,
    token: string,
    _expiresAt: number,
    _nowEpoch: number,
  ): Promise<boolean> {
    if (this.parentSync.has(incidentId)) return false;
    this.parentSync.set(incidentId, token);
    return true;
  }

  async releaseParentSync(
    incidentId: string,
    token: string,
    parentVersion: number | null,
  ): Promise<void> {
    if (this.parentSync.get(incidentId) !== token) return;
    this.parentSync.delete(incidentId);
    const incident = this.incidents.get(incidentId)!;
    incident.parentDirty = parentVersion === null || incident.parentVersion !== parentVersion;
  }

  async markRecoveryPosted(incidentId: string): Promise<void> {
    this.incidents.get(incidentId)!.recoveryPending = false;
  }

  async setAnalysisRef(jobId: string, analysisRef: string, attempt: number): Promise<boolean> {
    const job = this.jobs.get(jobId)!;
    if (job.status !== "queued" && !(job.status === "dispatching" && job.attempts < attempt)) {
      return false;
    }
    job.status = "dispatching";
    job.analysisRef = analysisRef;
    job.attempts = attempt;
    const incident = this.incidents.get(job.incidentId)!;
    incident.analysisRef = analysisRef;
    incident.parentDirty = true;
    incident.parentVersion += 1;
    return true;
  }

  async markDispatched(jobId: string): Promise<void> {
    this.jobs.get(jobId)!.status = "dispatched";
  }

  async retryDispatch(jobId: string, error: string, final: boolean): Promise<void> {
    const job = this.jobs.get(jobId)!;
    job.status = final ? "failed" : "queued";
    job.lastError = error;
    if (final) {
      const incident = this.incidents.get(job.incidentId)!;
      if (incident.status !== "resolved") incident.codexStatus = "unavailable";
      incident.parentDirty = true;
      incident.parentVersion += 1;
    }
  }

  async beginCompletion(jobId: string, completion: JobCompletion): Promise<boolean> {
    const job = this.jobs.get(jobId)!;
    if (!["queued", "dispatching", "dispatched"].includes(job.status)) return false;
    job.status = "completing";
    job.completion = completion;
    return true;
  }

  async finishCompletion(
    jobId: string,
    status: "completed" | "failed",
    codexStatus: CodexStatus,
  ): Promise<void> {
    const job = this.jobs.get(jobId)!;
    job.status = status;
    const incident = this.incidents.get(job.incidentId)!;
    if (incident.status !== "resolved") incident.codexStatus = codexStatus;
    incident.parentDirty = true;
    incident.parentVersion += 1;
  }

  async resetCompletion(jobId: string, error: string): Promise<void> {
    const job = this.jobs.get(jobId)!;
    job.status = "dispatched";
    job.lastError = error;
    job.completion = null;
  }
}

class FakeSlack implements SlackWriter {
  readonly calls: Array<{ kind: "parent" | "update" | "reply"; thread?: string; message: SlackMessage }> =
    [];

  async postParent(message: SlackMessage): Promise<string> {
    this.calls.push({ kind: "parent", message });
    return `${this.calls.filter((call) => call.kind === "parent").length}.000`;
  }

  async updateParent(threadTs: string, message: SlackMessage): Promise<void> {
    this.calls.push({ kind: "update", thread: threadTs, message });
  }

  async postReply(threadTs: string, message: SlackMessage): Promise<void> {
    this.calls.push({ kind: "reply", thread: threadTs, message });
  }
}

function harness(githubOverrides: Partial<GitHubGateway> = {}) {
  const store = new MemoryStore();
  const slack = new FakeSlack();
  const queued: JobQueueMessage[] = [];
  const github: GitHubGateway = {
    isAncestor: vi.fn(async () => true),
    dispatch: vi.fn(async () => undefined),
    ...githubOverrides,
  };
  let sequence = 0;
  const uuid = () => {
    sequence += 1;
    return `00000000-0000-4000-8000-${sequence.toString().padStart(12, "0")}`;
  };
  const relay = new AlertRelayService(
    store,
    slack,
    { send: async (message) => void queued.push(message) },
    github,
    {
      slackChannelId: "C123",
      model: "glm-5.2",
      modelBaseUrl: "https://freeinference.org/v1",
    },
    () => "2026-07-19T12:30:00.000Z",
    uuid,
  );
  return { relay, store, slack, queued, github };
}

describe("incident lifecycle and idempotency", () => {
  it("keeps one parent, updates repeats, resolves in-thread, and re-arms", async () => {
    const { relay, store, slack } = harness();

    const opened = await relay.submitAlert(event("alert-1"), "staging");
    expect(opened).toMatchObject({ duplicate: false, occurrence_count: 1 });
    expect(slack.calls.filter((call) => call.kind === "parent")).toHaveLength(1);

    const repeated = await relay.submitAlert(event("alert-2"), "staging");
    expect(repeated).toMatchObject({
      incident_id: opened.incident_id,
      occurrence_count: 2,
    });
    expect(slack.calls.filter((call) => call.kind === "parent")).toHaveLength(1);
    expect(slack.calls.filter((call) => call.kind === "reply")).toHaveLength(0);
    expect(slack.calls.at(-1)?.kind).toBe("update");

    const duplicate = await relay.submitAlert(event("alert-2"), "staging");
    expect(duplicate).toMatchObject({ duplicate: true, occurrence_count: 2 });
    expect(store.receipts.size).toBe(2);

    const resolved = await relay.submitAlert(event("alert-3", "resolved"), "staging");
    expect(resolved.incident_id).toBe(opened.incident_id);
    expect(slack.calls.filter((call) => call.kind === "reply")).toHaveLength(1);
    expect(slack.calls.at(-1)?.message.text).toContain("RESOLVED");

    const reopened = await relay.submitAlert(event("alert-4"), "staging");
    expect(reopened.incident_id).not.toBe(opened.incident_id);
    expect(slack.calls.filter((call) => call.kind === "parent")).toHaveLength(2);
  });

  it("records an orphan resolution without creating Slack noise", async () => {
    const { relay, slack } = harness();
    const result = await relay.submitAlert(event("alert-1", "resolved"), "production");
    expect(result).toEqual({
      accepted: true,
      duplicate: false,
      incident_id: null,
      status: "resolved",
      occurrence_count: 0,
    });
    expect(slack.calls).toEqual([]);
  });
});

describe("workflow dispatch and callback", () => {
  it("stores the trusted ref, exposes no Slack coordinates, and completes once", async () => {
    const { relay, store, slack, queued, github } = harness();
    await relay.submitAlert(event("alert-1"), "staging");
    const jobId = queued[0].job_id;

    await expect(relay.processQueueJob(jobId, 1)).resolves.toBe("ack");
    await expect(relay.processQueueJob(jobId, 1)).resolves.toBe("ack");
    expect(github.dispatch).toHaveBeenCalledWith(jobId, "dev");
    expect(github.dispatch).toHaveBeenCalledTimes(1);
    expect(store.jobs.get(jobId)?.analysisRef).toBe("a".repeat(40));
    const parentMessages = slack.calls.filter(
      (call) => call.kind === "parent" || call.kind === "update",
    );
    expect(JSON.stringify(parentMessages[0].message)).toContain("dev@pending");
    expect(JSON.stringify(parentMessages.at(-1)?.message)).toContain(`dev@${"a".repeat(40)}`);
    const payload = await relay.getWorkflowJob(jobId);
    expect(payload).toMatchObject({
      job_id: jobId,
      environment: "staging",
      analysis_ref: "a".repeat(40),
      model: "glm-5.2",
    });
    expect(payload).not.toHaveProperty("slack_thread_ts");
    expect(payload).not.toHaveProperty("slack_channel_id");

    const completion: JobCompletion = {
      status: "success",
      analysis: {
        summary: "Provider throttling",
        classification: "upstream_provider",
        confidence: 0.8,
        impact: "Requests fail",
        evidence: ["HTTP 429"],
        likely_cause: "Quota",
        recommended_actions: ["Wait"],
        issue_recommendation: "none",
        draft_pr_recommendation: "none",
      },
    };
    await expect(relay.completeJob(jobId, completion)).resolves.toMatchObject({
      duplicate: false,
      status: "completed",
    });
    const replies = slack.calls.filter((call) => call.kind === "reply");
    expect(replies).toHaveLength(1);
    expect(replies[0].message.text).toContain("Codex analysis");

    await expect(relay.completeJob(jobId, completion)).resolves.toMatchObject({
      duplicate: true,
      status: "completed",
    });
    expect(slack.calls.filter((call) => call.kind === "reply")).toHaveLength(1);
    await expect(relay.getWorkflowJob(jobId)).rejects.toBeInstanceOf(
      RelayNotFoundError,
    );
  });

  it("shows a branch fallback and dispatches each environment workflow definition", async () => {
    const staging = harness({ isAncestor: vi.fn(async () => false) });
    await staging.relay.submitAlert(event("alert-staging"), "staging");
    const stagingJob = staging.queued[0].job_id;
    await staging.relay.processQueueJob(stagingJob, 1);
    expect(staging.store.jobs.get(stagingJob)?.analysisRef).toBe("dev");
    expect(staging.github.dispatch).toHaveBeenCalledWith(stagingJob, "dev");
    expect(JSON.stringify(staging.slack.calls.at(-1)?.message)).toContain(
      "dev@unavailable (analysis ref: dev)",
    );

    const production = harness();
    await production.relay.submitAlert(event("alert-production"), "production");
    const productionJob = production.queued[0].job_id;
    await production.relay.processQueueJob(productionJob, 1);
    expect(production.github.dispatch).toHaveBeenCalledWith(productionJob, "main");
  });

  it("retries queue dispatch and posts a final explicit unavailable reply", async () => {
    const { relay, slack, queued } = harness({
      dispatch: vi.fn(async () => {
        throw new Error("GitHub unavailable");
      }),
    });
    await relay.submitAlert(event("alert-1"), "production");
    const jobId = queued[0].job_id;

    await expect(relay.processQueueJob(jobId, 1)).resolves.toBe("retry");
    await expect(relay.processQueueJob(jobId, 3)).resolves.toBe("ack");
    const replies = slack.calls.filter((call) => call.kind === "reply");
    expect(replies).toHaveLength(1);
    expect(replies[0].message.text).toContain("Codex unavailable");
  });

  it("requires and renders the workflow run URL on failure callback", async () => {
    const { relay, slack, queued } = harness();
    await relay.submitAlert(event("alert-1"), "local");
    const jobId = queued[0].job_id;
    await relay.processQueueJob(jobId, 1);

    await relay.completeJob(jobId, {
      status: "failure",
      error: "checkout failed",
      run_url: "https://github.com/org/repo/actions/runs/9",
    });
    expect(JSON.stringify(slack.calls.at(-2)?.message)).toContain(
      "https://github.com/org/repo/actions/runs/9",
    );
  });
});
