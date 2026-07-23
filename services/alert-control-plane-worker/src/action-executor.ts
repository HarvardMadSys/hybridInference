import { NotificationActionExecutor } from "./notification-executor";
import {
  type ActionClaim,
  type ActionExecutionResult,
  type ActionExecutor,
} from "./outbox";
import { QuotaActionExecutor } from "./quota-runtime";
import type { StagingRuntimeConfig } from "./runtime-config";
import { SlackSink } from "./slack";
import type { IncidentStore, PendingActionType } from "./store";

const NOTIFICATION_ACTIONS: ReadonlySet<PendingActionType> = new Set([
  "post_parent",
  "update_parent",
  "post_recovery",
  "post_analysis",
]);

const QUOTA_ACTIONS: ReadonlySet<PendingActionType> = new Set([
  "reserve_quota",
  "release_quota",
]);

export interface RuntimeExecutorOverrides {
  readonly fetch?: typeof fetch;
  readonly now?: () => number;
}

/** Phase C1 deliberately has no GitHub/Codex dispatcher. */
export class AnalysisDisabledExecutor implements ActionExecutor {
  async execute(claim: ActionClaim): Promise<ActionExecutionResult> {
    if (claim.action.type !== "dispatch_analysis") {
      return { outcome: "failed", error: "action_type_not_supported" };
    }
    return { outcome: "failed", error: "analysis_not_enabled" };
  }
}

/** Route each outbox action to its narrow, capability-specific executor. */
export class CompositeActionExecutor implements ActionExecutor {
  constructor(
    private readonly notification: ActionExecutor,
    private readonly quota: ActionExecutor,
    private readonly analysis: ActionExecutor,
  ) {}

  async execute(claim: ActionClaim): Promise<ActionExecutionResult> {
    if (NOTIFICATION_ACTIONS.has(claim.action.type)) {
      return this.notification.execute(claim);
    }
    if (QUOTA_ACTIONS.has(claim.action.type)) {
      return this.quota.execute(claim);
    }
    if (claim.action.type === "dispatch_analysis") {
      return this.analysis.execute(claim);
    }
    return { outcome: "failed", error: "action_type_not_supported" };
  }
}

/** Build the complete, explicitly gated Phase C1 action runtime. */
export function createRuntimeActionExecutor(
  store: IncidentStore,
  config: StagingRuntimeConfig,
  overrides: RuntimeExecutorOverrides = {},
): ActionExecutor {
  const sink = new SlackSink({
    sinkId: config.slack.sinkId,
    botToken: config.slack.botToken,
    channelId: config.slack.channelId,
    fetch: overrides.fetch,
    now: overrides.now,
  });
  return new CompositeActionExecutor(
    new NotificationActionExecutor(store, sink),
    new QuotaActionExecutor(
      config.quota.namespace,
      config.routeKey,
      overrides.now,
    ),
    new AnalysisDisabledExecutor(),
  );
}
