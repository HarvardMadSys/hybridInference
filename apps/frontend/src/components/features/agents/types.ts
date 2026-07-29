// UI types for the cloud agent sandbox (roadmap: issue #1041).
//
// The normalized event kinds mirror the agent_job_events schema decided in the
// roadmap — thinking | message | tool_use | tool_result | diff | usage |
// error | lifecycle — with two display-level simplifications: tool_result is
// folded into its tool_use row, and egress denials (an `error` subtype on the
// wire) get their own kind because the UI renders them as a first-class row.

export type AgentJobState = 'queued' | 'running' | 'needs_review' | 'done' | 'failed' | 'cancelled';

export interface AgentAttempt {
  no: number;
  status: 'live' | 'superseded' | 'finished';
  /** Shown in the audit banner when a lease takeover superseded the attempt. */
  note?: string;
}

export type AgentEvent =
  | { kind: 'lifecycle'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'message'; text: string }
  | {
      kind: 'tool_use';
      tool: 'Read' | 'Bash' | 'Edit';
      detail: string;
      /** Collapsed tool output preview (mono block), e.g. pytest tail. */
      output?: string[];
      /** e.g. "+12 −3" for Edit rows. */
      diffStat?: string;
    }
  | { kind: 'egress_denied'; host: string; attempts: number }
  | { kind: 'usage'; text: string };

export interface DiffLine {
  marker: 'hunk' | 'ctx' | 'add' | 'del';
  text: string;
}

export interface AgentGate {
  label: string;
  state: 'pass' | 'pending' | 'hold';
  detail?: string;
}

export interface AgentJob {
  id: string;
  title: string;
  state: AgentJobState;
  /** Short badge text next to the title, e.g. gate hold or failure reason. */
  stateNote?: string;
  repo: string;
  baseSha: string;
  /** Output branch, always agent/<job-id>. */
  branch: string;
  runtime: string;
  runtimeVersion?: string;
  model: string;
  /** Set when the model is served on our own hardware, e.g. "H200". */
  modelLocal?: string;
  spentUsd: number;
  // False when the deployment has no billing ledger, so the UI can say
  // "unknown" instead of implying the job cost nothing.
  hasLedger?: boolean;
  budgetUsd: number;
  elapsedLabel?: string;
  timeoutLabel: string;
  networkSetup: string;
  networkAgent: string;
  sandbox: string;
  attempts: AgentAttempt[];
  events: AgentEvent[];
  /** Bottom-of-stream live line while running, e.g. "turn 15 · …". */
  liveNote?: string;
  eventCount: number;
  diffFiles: string[];
  diffStat?: { add: number; del: number };
  diffLines: DiffLine[];
  rawLines: string[];
  gates: AgentGate[];
  usage: { tokensIn: string; tokensOut: string; cachePct: number; turns: number };
  egressDenials: number;
  /** e.g. "draft PR #1044" once published. */
  prLabel?: string;
}
