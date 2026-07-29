'use client';

import Link from 'next/link';
import { useState } from 'react';
import { restoreAgentJob } from '@/lib/api/agents';
import { groupJobsByConversation } from './conversations';
import { useAgentJobList } from './useAgentJobs';
import type { AgentJobState } from './types';

const STATE_LABEL: Record<AgentJobState, string> = {
  running: 'Running',
  needs_review: 'Needs review',
  done: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
  queued: 'Queued',
};

export function ArchivedTasksView() {
  const { jobs, loading, error, reload } = useAgentJobList(10_000, true);
  const [hiddenThreads, setHiddenThreads] = useState<Set<string>>(new Set());
  const [restoring, setRestoring] = useState<string | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const sections = groupJobsByConversation(
    jobs.filter((job) => !hiddenThreads.has(job.threadId ?? job.id)),
  );

  async function restoreConversation(key: string, jobId: string) {
    setRestoring(key);
    setRestoreError(null);
    try {
      await restoreAgentJob(jobId);
      setHiddenThreads((current) => new Set(current).add(key));
      reload();
    } catch (cause: unknown) {
      setRestoreError(cause instanceof Error ? cause.message : 'Could not restore the task.');
    } finally {
      setRestoring(null);
    }
  }

  return (
    <section className="mx-auto w-full max-w-4xl px-6 py-12 lg:px-12 lg:py-16">
      <h1 className="text-3xl font-semibold tracking-tight text-gray-900">Archived tasks</h1>
      <p className="mt-2 text-base text-gray-500">
        Archived conversations stay available here and can be returned to your task history.
      </p>

      {loading ? <p className="mt-10 text-sm text-gray-500">Loading archived tasks…</p> : null}
      {error ? (
        <p className="mt-10 text-sm text-red-600" role="alert">
          {error}
        </p>
      ) : null}
      {restoreError ? (
        <p className="mt-6 text-sm text-red-600" role="alert">
          {restoreError}
        </p>
      ) : null}
      {!loading && !error && sections.length === 0 ? (
        <div className="mt-10 rounded-2xl border border-dashed border-gray-200 px-6 py-12 text-center">
          <p className="text-sm font-medium text-gray-700">No archived tasks</p>
          <p className="mt-1 text-sm text-gray-500">
            Use the archive button beside a task to move it here.
          </p>
        </div>
      ) : null}

      <div className="mt-10 space-y-8">
        {sections.map((section) => (
          <div key={section.label}>
            <h2 className="text-xs font-medium uppercase tracking-wide text-gray-400">
              {section.label}
            </h2>
            <div className="mt-3 divide-y divide-gray-100 rounded-2xl border border-gray-200 bg-white px-5 shadow-sm">
              {section.conversations.map(({ key, job }) => (
                <div key={key} className="flex items-center gap-4 py-4">
                  <Link href={`/agents/${job.id}`} className="min-w-0 flex-1 rounded-lg">
                    <div className="flex items-center gap-2">
                      <h3 className="truncate text-sm font-medium text-gray-900">{job.title}</h3>
                      <span className="shrink-0 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">
                        {STATE_LABEL[job.state]}
                      </span>
                    </div>
                    <p className="mt-1 truncate text-xs text-gray-500">{job.repo}</p>
                  </Link>
                  <button
                    type="button"
                    disabled={restoring === key}
                    onClick={() => void restoreConversation(key, job.id)}
                    className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
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
                        d="M4 12a8 8 0 1 0 2.34-5.66L4 8.68M4 4v4.68h4.68"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                    {restoring === key ? 'Restoring…' : 'Restore'}
                  </button>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
