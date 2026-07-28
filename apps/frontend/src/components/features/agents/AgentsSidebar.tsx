'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAuth } from '@/components/providers';
import { useAgentJobList } from './useAgentJobs';
import type { AgentJobState } from './types';

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
  const { state } = useAuth();
  const displayName = state.user?.user_name || state.user?.email || '';

  const { jobs, loading, error } = useAgentJobList();

  // Group by the repository each job actually names, rather than assuming one:
  // the shell already scales to multi-repo, and hardcoding a single group made
  // every job look like it belonged to the same one.
  const repos = Object.entries(
    jobs.reduce<Record<string, typeof jobs>>((groups, job) => {
      const key = job.repo || 'unknown';
      (groups[key] ??= []).push(job);
      return groups;
    }, {}),
  ).map(([name, group]) => ({ name: name.split('/').pop() || name, jobs: group }));

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

        <div className="mt-4 px-2 text-[11px] font-medium uppercase tracking-wide text-gray-400">
          Repositories
        </div>

        {loading ? (
          <p className="mt-2 px-2 text-[13px] text-gray-400">Loading…</p>
        ) : null}
        {error ? (
          <p className="mt-2 px-2 text-[13px] text-red-600" role="alert">
            {error}
          </p>
        ) : null}
        {!loading && !error && repos.length === 0 ? (
          <p className="mt-2 px-2 text-[13px] text-gray-400">No jobs yet.</p>
        ) : null}

        {repos.map((repo) => (
          <div key={repo.name} className="mt-1.5">
            <div className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium text-gray-800">
              <svg
                className="h-4 w-4 text-gray-400"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"
                />
              </svg>
              {repo.name}
            </div>

            <div className="mt-0.5 space-y-0.5 pl-2">
              {repo.jobs.map((job) => {
                const href = `/agents/${job.id}`;
                const isActive = pathname === href;
                return (
                  <Link
                    key={job.id}
                    href={href}
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

        <div
          className="mt-3 flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-gray-400"
          title="External repositories arrive with the P1 beta (GitHub App install flow)"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 5v14m-7-7h14" />
          </svg>
          Add repository
          <span className="ml-auto rounded bg-gray-200/70 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">
            P1
          </span>
        </div>
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
