import {
  computeDesiredAlarmAt,
  type IncidentStore,
  type PendingAction,
  refreshDesiredAlarmAt,
} from "./store";

export interface ActionClaim {
  action: PendingAction;
  mode: "execute" | "reconcile";
}

export type ActionExecutionResult =
  | { outcome: "success"; result?: Record<string, unknown> }
  | { outcome: "retry"; error: string; retryAtMs?: number }
  | { outcome: "uncertain"; error: string; reconcileAtMs?: number }
  | { outcome: "manual_reconciliation_required"; error: string }
  | { outcome: "failed"; error: string };

export interface ActionExecutor {
  execute(claim: ActionClaim): Promise<ActionExecutionResult>;
}

export interface OutboxLifecycleHooks {
  onActionCompleted?(
    action: PendingAction,
    result: Record<string, unknown>,
    nowMs: number,
  ): void;
  onActionTerminal?(action: PendingAction, error: string, nowMs: number): void;
}

export interface OutboxRunnerOptions {
  claimLeaseMs?: number;
  retryBaseDelayMs?: number;
  maxAttempts?: number;
  reconcileDelayMs?: number;
  hooks?: OutboxLifecycleHooks;
}

export interface OutboxRunResult {
  ran: boolean;
  actionId: string | null;
  mode: "execute" | "reconcile" | null;
  outcome:
    | ActionExecutionResult["outcome"]
    | "idle"
    | "stale_result"
    | "claim_commit_failed";
}

const DEFAULT_CLAIM_LEASE_MS = 30_000;
const DEFAULT_RETRY_BASE_DELAY_MS = 1_000;
const DEFAULT_MAX_ATTEMPTS = 5;
const DEFAULT_RECONCILE_DELAY_MS = 5_000;

function dueAt(action: PendingAction): number {
  if (action.status === "uncertain") return action.reconcileAtMs ?? Number.MAX_SAFE_INTEGER;
  return action.nextRunAtMs ?? Number.MAX_SAFE_INTEGER;
}

function isTerminal(action: PendingAction): boolean {
  return (
    action.status === "completed" ||
    action.status === "cancelled" ||
    action.status === "failed" ||
    action.status === "manual_reconciliation_required"
  );
}

export class OutboxRunner {
  private readonly claimLeaseMs: number;
  private readonly retryBaseDelayMs: number;
  private readonly maxAttempts: number;
  private readonly reconcileDelayMs: number;
  private readonly hooks: OutboxLifecycleHooks;

  constructor(
    readonly store: IncidentStore,
    private readonly executor: ActionExecutor,
    options: OutboxRunnerOptions = {},
  ) {
    this.claimLeaseMs = options.claimLeaseMs ?? DEFAULT_CLAIM_LEASE_MS;
    this.retryBaseDelayMs = options.retryBaseDelayMs ?? DEFAULT_RETRY_BASE_DELAY_MS;
    this.maxAttempts = options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;
    this.reconcileDelayMs = options.reconcileDelayMs ?? DEFAULT_RECONCILE_DELAY_MS;
    this.hooks = options.hooks ?? {};
  }

  desiredAlarmAt(): number | null {
    return this.store.transaction(() => refreshDesiredAlarmAt(this.store));
  }

  async runOne(nowMs: number): Promise<OutboxRunResult> {
    if (!Number.isFinite(nowMs)) throw new TypeError("nowMs must be finite");
    const claim = this.store.transaction(() => this.claimOne(nowMs));
    if (!claim) {
      return { ran: false, actionId: null, mode: null, outcome: "idle" };
    }

    let executionResult: ActionExecutionResult;
    try {
      executionResult = await this.executor.execute(claim);
    } catch (error) {
      executionResult = {
        outcome: "uncertain",
        error: error instanceof Error ? error.message : String(error),
        reconcileAtMs: nowMs + this.reconcileDelayMs,
      };
    }

    try {
      const committed = this.store.transaction(() =>
        this.commitResult(claim, executionResult, nowMs),
      );
      return {
        ran: true,
        actionId: claim.action.actionId,
        mode: claim.mode,
        outcome: committed ? executionResult.outcome : "stale_result",
      };
    } catch (error) {
      this.markCommitFailureUncertain(claim, error, nowMs);
      throw error;
    }
  }

  private claimOne(nowMs: number): ActionClaim | null {
    this.expireAnalysisDeadlines(nowMs);
    this.recoverExpiredClaimsAndDeadlines(nowMs);
    this.reconcileDependencies(nowMs);

    const candidates = this.store
      .listActions()
      .filter(
        (action) =>
          ((action.status === "pending" &&
            action.nextRunAtMs !== null &&
            action.nextRunAtMs <= nowMs) ||
            (action.status === "uncertain" &&
              action.reconcileAtMs !== null &&
              action.reconcileAtMs <= nowMs)) &&
          action.dependsOnActionId === null,
      )
      .sort(
        (left, right) =>
          dueAt(left) - dueAt(right) ||
          left.createdAtMs - right.createdAtMs ||
          left.actionId.localeCompare(right.actionId),
      );

    const action = candidates[0];
    const scheduler = this.store.getSchedulerState();
    this.store.putSchedulerState({
      ...scheduler,
      lastRunAtMs: nowMs,
      lastError: null,
    });
    if (!action) {
      refreshDesiredAlarmAt(this.store);
      return null;
    }

    const mode = action.status === "uncertain" ? "reconcile" : "execute";
    action.status = "claimed";
    action.claimEpoch += 1;
    action.claimMode = mode;
    if (mode === "execute") action.startedAtMs = nowMs;
    action.leaseExpiresAtMs = nowMs + this.claimLeaseMs;
    action.reconcileAtMs = null;
    if (mode === "execute") action.attempt += 1;
    action.updatedAtMs = nowMs;
    this.store.putAction(action);
    refreshDesiredAlarmAt(this.store);
    return { action, mode };
  }

  private commitResult(
    claim: ActionClaim,
    result: ActionExecutionResult,
    nowMs: number,
  ): boolean {
    const action = this.store.getAction(claim.action.actionId);
    if (!action || !this.matchesClaim(action, claim.action)) {
      refreshDesiredAlarmAt(this.store);
      return false;
    }

    action.claimMode = null;
    action.leaseExpiresAtMs = null;
    action.updatedAtMs = nowMs;

    if (result.outcome === "success") {
      action.status = "completed";
      action.nextRunAtMs = null;
      action.reconcileAtMs = null;
      action.lastError = null;
      action.result = result.result ?? {};
      this.store.putAction(action);
      this.hooks.onActionCompleted?.(action, action.result, nowMs);
      this.unblockDependents(action, nowMs);
    } else if (result.outcome === "retry") {
      if (action.attempt >= this.maxAttempts || nowMs >= action.finalDeadlineAtMs) {
        this.markTerminal(action, "failed", result.error, nowMs);
      } else {
        action.status = "pending";
        action.version += 1;
        action.startedAtMs = null;
        action.nextRunAtMs = Math.min(
          result.retryAtMs ?? nowMs + this.backoffDelay(action.attempt),
          action.finalDeadlineAtMs,
        );
        action.reconcileAtMs = null;
        action.lastError = result.error;
        this.store.putAction(action);
      }
    } else if (result.outcome === "uncertain") {
      const reconcileAtMs = Math.min(
        result.reconcileAtMs ?? nowMs + this.reconcileDelayMs,
        action.finalDeadlineAtMs,
      );
      if (nowMs >= action.finalDeadlineAtMs) {
        this.markTerminal(
          action,
          "manual_reconciliation_required",
          result.error,
          nowMs,
        );
      } else {
        action.status = "uncertain";
        action.version += 1;
        action.nextRunAtMs = null;
        action.reconcileAtMs = reconcileAtMs;
        action.lastError = result.error;
        this.store.putAction(action);
      }
    } else if (result.outcome === "manual_reconciliation_required") {
      this.markTerminal(action, "manual_reconciliation_required", result.error, nowMs);
    } else {
      this.markTerminal(action, "failed", result.error, nowMs);
    }

    refreshDesiredAlarmAt(this.store);
    return true;
  }

  private matchesClaim(current: PendingAction, claimed: PendingAction): boolean {
    return (
      current.status === "claimed" &&
      current.actionId === claimed.actionId &&
      current.version === claimed.version &&
      current.generation === claimed.generation &&
      current.stateVersion === claimed.stateVersion &&
      current.resolutionEpoch === claimed.resolutionEpoch &&
      current.claimEpoch === claimed.claimEpoch
    );
  }

  private recoverExpiredClaimsAndDeadlines(nowMs: number): void {
    for (const action of this.store.listActions()) {
      if (
        action.status === "claimed" &&
        action.leaseExpiresAtMs !== null &&
        action.leaseExpiresAtMs <= nowMs
      ) {
        action.status = "uncertain";
        action.version += 1;
        action.claimMode = null;
        action.leaseExpiresAtMs = null;
        action.nextRunAtMs = null;
        action.reconcileAtMs = nowMs;
        action.lastError = "claim lease expired after external execution may have started";
        action.updatedAtMs = nowMs;
        this.store.putAction(action);
      }

      const current = this.store.getAction(action.actionId);
      if (!current || isTerminal(current) || current.finalDeadlineAtMs > nowMs) continue;
      if (current.status === "uncertain" || current.status === "claimed") {
        this.markTerminal(
          current,
          "manual_reconciliation_required",
          current.lastError ?? "action reached its final reconciliation deadline",
          nowMs,
        );
      } else {
        this.markTerminal(
          current,
          "failed",
          current.lastError ?? "action reached its final retry deadline",
          nowMs,
        );
      }
    }
  }

  private reconcileDependencies(nowMs: number): void {
    for (const action of this.store.listActions()) {
      if (action.status !== "blocked" || action.dependsOnActionId === null) continue;
      const dependency = this.store.getAction(action.dependsOnActionId);
      if (!dependency) {
        this.markTerminal(action, "failed", "action dependency does not exist", nowMs);
      } else if (dependency.status === "completed") {
        action.status = "pending";
        action.version += 1;
        action.dependsOnActionId = null;
        action.updatedAtMs = nowMs;
        this.store.putAction(action);
      } else if (isTerminal(dependency)) {
        this.markTerminal(
          action,
          dependency.status === "manual_reconciliation_required"
            ? "manual_reconciliation_required"
            : "failed",
          `action dependency ended in ${dependency.status}`,
          nowMs,
        );
      }
    }
  }

  private unblockDependents(completed: PendingAction, nowMs: number): void {
    for (const dependent of this.store.listActions()) {
      if (
        dependent.status !== "blocked" ||
        dependent.dependsOnActionId !== completed.actionId
      ) {
        continue;
      }
      dependent.status = "pending";
      dependent.version += 1;
      dependent.dependsOnActionId = null;
      dependent.updatedAtMs = nowMs;
      this.store.putAction(dependent);
    }
  }

  private expireAnalysisDeadlines(nowMs: number): void {
    for (const job of this.store.listAnalysisJobs()) {
      if (
        job.deadlineAtMs === null ||
        job.deadlineAtMs > nowMs ||
        job.status === "succeeded" ||
        job.status === "failed"
      ) {
        continue;
      }
      job.status = "failed";
      job.version += 1;
      job.leaseExpiresAtMs = null;
      job.deadlineAtMs = null;
      job.lastError = "analysis deadline expired";
      job.updatedAtMs = nowMs;
      this.store.putAnalysisJob(job);
      for (const action of this.store.listActions()) {
        if (
          action.type !== "dispatch_analysis" ||
          action.payload.job_id !== job.jobId ||
          (action.status !== "pending" && action.status !== "blocked")
        ) {
          continue;
        }
        action.status = "cancelled";
        action.version += 1;
        action.dependsOnActionId = null;
        action.nextRunAtMs = null;
        action.reconcileAtMs = null;
        action.leaseExpiresAtMs = null;
        action.lastError = "analysis deadline expired before dispatch claim";
        action.updatedAtMs = nowMs;
        this.store.putAction(action);
      }
    }
  }

  private markTerminal(
    action: PendingAction,
    status: "failed" | "manual_reconciliation_required",
    error: string,
    nowMs: number,
  ): void {
    action.status = status;
    action.version += 1;
    action.claimMode = null;
    action.leaseExpiresAtMs = null;
    action.nextRunAtMs = null;
    action.reconcileAtMs = null;
    action.lastError = error;
    action.updatedAtMs = nowMs;
    this.store.putAction(action);
    this.hooks.onActionTerminal?.(action, error, nowMs);
    for (const dependent of this.store.listActions()) {
      if (
        dependent.status !== "blocked" ||
        dependent.dependsOnActionId !== action.actionId
      ) {
        continue;
      }
      dependent.status = status;
      dependent.version += 1;
      dependent.dependsOnActionId = null;
      dependent.nextRunAtMs = null;
      dependent.lastError = `action dependency ended in ${status}`;
      dependent.updatedAtMs = nowMs;
      this.store.putAction(dependent);
      this.hooks.onActionTerminal?.(dependent, dependent.lastError, nowMs);
    }
  }

  private markCommitFailureUncertain(
    claim: ActionClaim,
    error: unknown,
    nowMs: number,
  ): void {
    try {
      this.store.transaction(() => {
        const action = this.store.getAction(claim.action.actionId);
        if (!action || !this.matchesClaim(action, claim.action)) return;
        action.status = "uncertain";
        action.version += 1;
        action.claimMode = null;
        action.leaseExpiresAtMs = null;
        action.nextRunAtMs = null;
        action.reconcileAtMs = Math.min(
          nowMs + this.reconcileDelayMs,
          action.finalDeadlineAtMs,
        );
        action.lastError = `could not commit external result: ${
          error instanceof Error ? error.message : String(error)
        }`;
        action.updatedAtMs = nowMs;
        this.store.putAction(action);
        refreshDesiredAlarmAt(this.store);
      });
    } catch {
      // The persisted claim lease and the pre-armed safety alarm remain the recovery fence.
    }
  }

  private backoffDelay(attempt: number): number {
    return this.retryBaseDelayMs * 2 ** Math.max(0, attempt - 1);
  }

  snapshotDesiredAlarmAt(): number | null {
    return computeDesiredAlarmAt(this.store);
  }
}
