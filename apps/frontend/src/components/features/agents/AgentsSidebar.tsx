'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useState } from 'react';
import { useAuth } from '@/components/providers';
import { archiveAgentJob } from '@/lib/api/agents';
import { groupJobsByConversation } from './conversations';
import { useAgentJobList } from './useAgentJobs';
import type { AgentJobState } from './types';

export { groupJobsByConversation } from './conversations';

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

export function AgentsSidebar() {
  const pathname = usePathname() ?? '';
  const router = useRouter();
  const { state } = useAuth();
  const displayName = state.user?.user_name || state.user?.email || '';
  const [hiddenThreads, setHiddenThreads] = useState<Set<string>>(new Set());
  const [archiving, setArchiving] = useState<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);

  const { jobs, loading, error, reload } = useAgentJobList();

  const sections = groupJobsByConversation(
    jobs.filter((job) => !hiddenThreads.has(job.threadId ?? job.id)),
  );

  async function archiveConversation(key: string, jobId: string, jobIds: string[]) {
    setArchiving(key);
    setArchiveError(null);
    try {
      await archiveAgentJob(jobId);
      setHiddenThreads((current) => new Set(current).add(key));
      reload();
      if (jobIds.some((id) => pathname === `/agents/${id}`)) router.replace('/agents');
    } catch (cause: unknown) {
      setArchiveError(cause instanceof Error ? cause.message : 'Could not archive the task.');
    } finally {
      setArchiving(null);
    }
  }

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
        {archiveError ? (
          <p className="mt-2 px-2 text-[13px] text-red-600" role="alert">
            {archiveError}
          </p>
        ) : null}
        {!loading && !error && !archiveError && sections.length === 0 ? (
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
                  <div key={key} className="group relative">
                    <Link
                      href={href}
                      title={`${job.repo} · ${job.title}`}
                      className={`flex w-full items-center gap-2 rounded-md py-1.5 pl-2 pr-9 text-left text-[13px] ${
                        isActive
                          ? 'bg-gray-200/80 font-medium text-gray-900'
                          : 'text-gray-600 hover:bg-gray-200/60'
                      }`}
                    >
                      <span
                        className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT_CLASS[job.state]}`}
                      />
                      <span className="min-w-0 flex-1 truncate">{job.title}</span>
                    </Link>
                    <button
                      type="button"
                      aria-label={`Archive ${job.title}`}
                      title="Archive task"
                      disabled={archiving === key}
                      onClick={() => void archiveConversation(key, job.id, jobIds)}
                      className="absolute right-1 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded text-gray-500 opacity-0 transition hover:bg-gray-300/70 hover:text-gray-800 focus:opacity-100 focus:outline-none focus:ring-2 focus:ring-crimson/30 disabled:cursor-wait disabled:opacity-60 group-hover:opacity-100 group-focus-within:opacity-100"
                    >
                      <svg
                        className="h-4 w-4"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth={1.8}
                        aria-hidden="true"
                      >
                        <path
                          d="M4 7.5h16m-14 0V20h12V7.5M5 4h14l1 3.5H4L5 4Zm5 7h4"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
        ))}

        <Link
          href="/agents/archived"
          className={`mt-3 flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium hover:bg-gray-200/60 ${
            pathname === '/agents/archived' ? 'bg-gray-200/80 text-gray-900' : 'text-gray-600'
          }`}
        >
          <svg
            className="h-4 w-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.8}
            aria-hidden="true"
          >
            <path
              d="M4 7.5h16m-14 0V20h12V7.5M5 4h14l1 3.5H4L5 4Zm5 7h4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          Archived
        </Link>

        <Link
          href="/agents/integrations"
          className={`mt-1 flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium hover:bg-gray-200/60 ${
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
