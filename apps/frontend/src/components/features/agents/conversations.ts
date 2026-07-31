import type { AgentJob } from './types';

export interface ConversationRow {
  key: string;
  job: AgentJob;
  jobIds: string[];
}

export interface ConversationSection {
  label: 'Pinned' | 'Today' | 'Previous 7 days' | 'Older';
  conversations: ConversationRow[];
}

export interface ProjectSection {
  /** Full `owner/name`, the key the API filters on. */
  repo: string;
  /** Repo name without the owner — what the folder row shows. */
  label: string;
  conversations: ConversationRow[];
}

/** The repo name a folder row shows; the owner is a tooltip, not a label. */
export function projectLabel(repo: string): string {
  const name = repo.slice(repo.lastIndexOf('/') + 1);
  return name || repo;
}

/** Collapse run records into one row per conversation, pinned then newest first. */
export function toConversationRows(jobs: AgentJob[]): ConversationRow[] {
  const byThread = new Map<string, AgentJob[]>();
  for (const job of jobs) {
    const key = job.threadId ?? job.id;
    const turns = byThread.get(key) ?? [];
    turns.push(job);
    byThread.set(key, turns);
  }

  const rows = [...byThread.entries()].map(([key, turns]) => {
    const ordered = [...turns].sort((left, right) => (left.turnNo ?? 1) - (right.turnNo ?? 1));
    const first = ordered[0];
    const latest = ordered[ordered.length - 1];
    return {
      key,
      job: { ...latest, title: first.title },
      jobIds: ordered.map((job) => job.id),
    };
  });

  rows.sort((left, right) => {
    const leftPinned = Date.parse(left.job.pinnedAt ?? '') || 0;
    const rightPinned = Date.parse(right.job.pinnedAt ?? '') || 0;
    if (leftPinned || rightPinned) return rightPinned - leftPinned;
    const leftTime = Date.parse(left.job.createdAt ?? '') || 0;
    const rightTime = Date.parse(right.job.createdAt ?? '') || 0;
    return rightTime - leftTime || (right.job.turnNo ?? 1) - (left.job.turnNo ?? 1);
  });
  return rows;
}

/** Conversations bucketed by recency — the archived list's ordering. */
export function groupJobsByConversation(
  jobs: AgentJob[],
  now: Date = new Date(),
): ConversationSection[] {
  const rows = toConversationRows(jobs);

  const startToday = new Date(now);
  startToday.setHours(0, 0, 0, 0);
  const weekStart = startToday.getTime() - 6 * 24 * 60 * 60 * 1000;
  const sections = new Map<ConversationSection['label'], ConversationRow[]>([
    ['Pinned', []],
    ['Today', []],
    ['Previous 7 days', []],
    ['Older', []],
  ]);
  for (const row of rows) {
    if (row.job.pinnedAt) {
      sections.get('Pinned')?.push(row);
      continue;
    }
    const time = Date.parse(row.job.createdAt ?? '') || 0;
    const label =
      time >= startToday.getTime() ? 'Today' : time >= weekStart ? 'Previous 7 days' : 'Older';
    sections.get(label)?.push(row);
  }
  return [...sections.entries()]
    .filter(([, conversations]) => conversations.length > 0)
    .map(([label, conversations]) => ({ label, conversations }));
}

/**
 * Conversations bucketed by repo — the sidebar's task tree.
 *
 * A conversation is pinned to one repo for its whole life (follow-ups and
 * forks both inherit it), so every turn of a thread lands in the same folder.
 * Projects are ordered by their most recent activity, and so are the rows
 * inside each, which is why date headers are not repeated per folder.
 */
export function groupJobsByProject(jobs: AgentJob[]): ProjectSection[] {
  const byRepo = new Map<string, ConversationRow[]>();
  for (const row of toConversationRows(jobs)) {
    const rows = byRepo.get(row.job.repo) ?? [];
    rows.push(row);
    byRepo.set(row.job.repo, rows);
  }
  return [...byRepo.entries()].map(([repo, conversations]) => ({
    repo,
    label: projectLabel(repo),
    conversations,
  }));
}
