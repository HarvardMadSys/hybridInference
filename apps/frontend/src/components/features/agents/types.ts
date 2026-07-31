// UI types for the cloud agent sandbox (roadmap: issue #1041).
//
// The normalized event kinds mirror the agent_job_events schema decided in the
// roadmap — thinking | message | tool_use | tool_result | diff | usage |
// error | lifecycle. Tool results are folded into their matching activity when
// possible, but remain renderable on their own for older/partial event logs.

export type AgentJobState = 'queued' | 'running' | 'needs_review' | 'done' | 'failed' | 'cancelled';

export interface AgentAttempt {
  no: number;
  status: 'live' | 'superseded' | 'finished';
  /** Shown in the audit banner when a lease takeover superseded the attempt. */
  note?: string;
}

export type AgentEvent =
  | { kind: 'lifecycle'; text: string; attemptNo?: number }
  | { kind: 'thinking'; text: string; attemptNo?: number }
  | { kind: 'message'; text: string; attemptNo?: number }
  | {
      kind: 'tool_use';
      tool: string;
      detail: string;
      id?: string;
      /** Collapsed tool output preview (mono block), e.g. pytest tail. */
      output?: string[];
      outputIsError?: boolean;
      /** e.g. "+12 −3" for Edit rows. */
      diffStat?: string;
      attemptNo?: number;
    }
  | {
      kind: 'tool_result';
      text: string;
      toolUseId?: string;
      isError: boolean;
      attemptNo?: number;
    }
  | { kind: 'terminal'; text: string; isError?: boolean; attemptNo?: number }
  | { kind: 'egress_denied'; host: string; attempts: number; attemptNo?: number }
  | { kind: 'usage'; text: string; attemptNo?: number };

export interface AgentThreadMessage {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  jobId: string;
  createdAt: string | null;
}

export interface DiffLine {
  marker: 'hunk' | 'ctx' | 'add' | 'del';
  text: string;
}

export interface AgentDiffFile {
  path: string;
  add: number;
  del: number;
  lines: DiffLine[];
}

export interface AgentGate {
  label: string;
  state: 'pass' | 'pending' | 'hold';
  detail?: string;
}

export interface AgentJob {
  id: string;
  /** Used to group conversation rows in the recent-tasks sidebar. */
  createdAt?: string | null;
  /** Conversation-level pin time. Every turn in the same thread shares it. */
  pinnedAt?: string | null;
  title: string;
  /** Complete current-turn prompt. Older fixtures may only have `title`. */
  prompt?: string;
  state: AgentJobState;
  /** Short badge text next to the title, e.g. gate hold or failure reason. */
  stateNote?: string;
  repo: string;
  baseRef?: string;
  baseSha: string;
  /** Output branch, agent/<thread-id> for conversations or agent/<job-id> for legacy jobs. */
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
  diffFileDetails?: AgentDiffFile[];
  diffStat?: { add: number; del: number };
  diffLines: DiffLine[];
  rawLines: string[];
  gates: AgentGate[];
  usage: { tokensIn: string; tokensOut: string; cachePct: number; turns: number };
  egressDenials: number;
  /** e.g. "draft PR #1044" once published. */
  prLabel?: string;
  prUrl?: string;
  threadId?: string;
  parentJobId?: string;
  turnNo?: number;
  /** Completed turns before this job. The current prompt/activity are separate. */
  threadMessages?: AgentThreadMessage[];
  /**
   * True when threadMessages already carries this job's own turn (a forked
   * copy has no events, so its durable messages are rendered instead) — the
   * separate current-prompt card would be a duplicate.
   */
  historyIncludesPrompt?: boolean;
}
