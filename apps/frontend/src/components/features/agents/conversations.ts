import type { AgentJob } from './types';

export interface ConversationRow {
  key: string;
  job: AgentJob;
  jobIds: string[];
}

export interface ConversationSection {
  label: 'Today' | 'Previous 7 days' | 'Older';
  conversations: ConversationRow[];
}

/** Collapse run records into conversation rows and date groups. */
export function groupJobsByConversation(
  jobs: AgentJob[],
  now: Date = new Date(),
): ConversationSection[] {
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
    const leftTime = Date.parse(left.job.createdAt ?? '') || 0;
    const rightTime = Date.parse(right.job.createdAt ?? '') || 0;
    return rightTime - leftTime || (right.job.turnNo ?? 1) - (left.job.turnNo ?? 1);
  });

  const startToday = new Date(now);
  startToday.setHours(0, 0, 0, 0);
  const weekStart = startToday.getTime() - 6 * 24 * 60 * 60 * 1000;
  const sections = new Map<ConversationSection['label'], ConversationRow[]>([
    ['Today', []],
    ['Previous 7 days', []],
    ['Older', []],
  ]);
  for (const row of rows) {
    const time = Date.parse(row.job.createdAt ?? '') || 0;
    const label =
      time >= startToday.getTime() ? 'Today' : time >= weekStart ? 'Previous 7 days' : 'Older';
    sections.get(label)?.push(row);
  }
  return [...sections.entries()]
    .filter(([, conversations]) => conversations.length > 0)
    .map(([label, conversations]) => ({ label, conversations }));
}
