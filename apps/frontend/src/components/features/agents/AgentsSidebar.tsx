'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAuth } from '@/components/providers';
import { useAgentJobList } from './useAgentJobs';
import type { AgentJob, AgentJobState } from './types';

// Sidebar status dots stay deliberately minimal (Codex-style titles-only
// rows), but unlike Codex our jobs burn budget and can be held by publish
// gates, so "which job is running / held" must be visible without opening it.
const DOT_CLASS: Record<AgentJobState, string> = {
  running: 'bg-blue-500 animate-pulse',
  needs_review: 'bg-amber-400',
  done: 'bg-emerald-500',
  failed: 'bg-red-400',
  cancelled: 'bg-gray-300',
  queued: 'border border-gray-300 bg-transparent',
};

function initialsOf(name: string | null | undefined, email: string | null | undefined): string {
  const source = name || email || '?';
  return source.slice(0, 2).toUpperCase();
}

interface ConversationRow {
  key: string;
  job: AgentJob;
  jobIds: string[];
}

export interface ConversationSection {
  label: 'Today' | 'Previous 7 days' | 'Older';
  conversations: ConversationRow[];
}

/** Collapse run records into Cursor-style conversation rows and date groups. */
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

export function AgentsSidebar() {
  const pathname = usePathname() ?? '';
  const { state } = useAuth();
  const displayName = state.user?.user_name || state.user?.email || '';

  const { jobs, loading, error } = useAgentJobList();

  const sections = groupJobsByConversation(jobs);

  return (
    <aside className="flex w-72 shrink-0 flex-col border-r border-gray-200 bg-gray-50">
      <div className="flex-1 overflow-y-auto px-3 py-3">
        <Link
          href="/agents"
          className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-200/60 ${
            pathname === '/agents' ? 'bg-gray-200/80 text-gray-900' : ''
          }`}
        >
          <svg
            className="h-4 w-4 text-gray-500"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M16.86 4.49a1.5 1.5 0 0 1 2.12 0l.53.53a1.5 1.5 0 0 1 0 2.12L8.53 18.12 4 19.5l1.38-4.53L16.86 4.49Z"
            />
          </svg>
          New task
        </Link>

        {loading ? <p className="mt-2 px-2 text-[13px] text-gray-400">Loading…</p> : null}
        {error ? (
          <p className="mt-2 px-2 text-[13px] text-red-600" role="alert">
            {error}
          </p>
        ) : null}
        {!loading && !error && sections.length === 0 ? (
          <p className="mt-2 px-2 text-[13px] text-gray-400">No jobs yet.</p>
        ) : null}

        {sections.map((section) => (
          <div key={section.label} className="mt-4">
            <div className="px-2 text-[11px] font-medium text-gray-400">{section.label}</div>
            <div className="mt-1 space-y-0.5">
              {section.conversations.map(({ key, job, jobIds }) => {
                const href = `/agents/${job.id}`;
                const isActive = jobIds.some((id) => pathname === `/agents/${id}`);
                return (
                  <Link
                    key={key}
                    href={href}
                    title={`${job.repo} · ${job.title}`}
                    className={`group flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] ${
                      isActive
                        ? 'bg-gray-200/80 font-medium text-gray-900'
                        : 'text-gray-600 hover:bg-gray-200/60'
                    }`}
                  >
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT_CLASS[job.state]}`} />
                    <span className="min-w-0 flex-1 truncate">{job.title}</span>
                  </Link>
                );
              })}
            </div>
          </div>
        ))}

        <Link
          href="/agents/integrations"
          className={`mt-3 flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium hover:bg-gray-200/60 ${
            pathname === '/agents/integrations' ? 'bg-gray-200/80 text-gray-900' : 'text-gray-600'
          }`}
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M8 5v3m8-3v3M6.5 8h11v2.5a5.5 5.5 0 0 1-11 0V8ZM12 16v3"
            />
          </svg>
          Integrations
        </Link>
      </div>

      <div className="flex shrink-0 items-center gap-2.5 border-t border-gray-200 px-4 py-3">
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-crimson/10 text-[11px] font-semibold text-crimson">
          {initialsOf(state.user?.user_name, state.user?.email)}
        </span>
        <span className="truncate text-[13px] font-medium text-gray-800">{displayName}</span>
      </div>
    </aside>
  );
}
