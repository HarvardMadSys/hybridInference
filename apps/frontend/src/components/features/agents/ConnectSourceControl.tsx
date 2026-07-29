'use client';

import Link from 'next/link';

// Inline onboarding shown above the disabled composer. Keeping the task shape
// visible gives the user context for why GitHub is needed without pretending
// a repository can already be selected.
export function ConnectSourceControl({ installUrl }: { installUrl: string | null }) {
  return (
    <div
      role="status"
      className="flex flex-col gap-3 rounded-xl border border-gray-200 bg-gray-50 px-4 py-3.5 sm:flex-row sm:items-center"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-white text-gray-700 shadow-sm ring-1 ring-gray-200">
        <svg className="h-4 w-4" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
          <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
        </svg>
      </span>
      <div className="min-w-0 flex-1">
        <h2 className="text-sm font-semibold text-gray-900">
          {installUrl ? 'Connect GitHub to start' : 'GitHub setup is not available yet'}
        </h2>
        <p className="mt-0.5 text-[13px] leading-relaxed text-gray-500">
          {installUrl
            ? 'Choose the repositories the agent can work on. Credentials never enter the sandbox.'
            : 'Contact your administrator to enable repository access for cloud agents.'}
        </p>
      </div>
      <Link
        href="/agents/integrations"
        className="inline-flex shrink-0 items-center justify-center gap-2 rounded-lg bg-gray-900 px-3.5 py-2 text-[13px] font-medium text-white hover:bg-gray-800"
      >
        {installUrl ? 'Connect GitHub' : 'View integrations'}
        <svg
          className="h-3.5 w-3.5"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M7 17 17 7m0 0H8m9 0v9" />
        </svg>
      </Link>
    </div>
  );
}
