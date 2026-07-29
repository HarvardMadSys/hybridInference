'use client';

// Shown in place of the composer when no source control is connected.
//
// The composer used to render a repository chip, a branch chip and pickers
// whether or not anything was connected — so it looked ready while no job it
// produced could run. Saying what is missing is the more useful screen, and it
// is the shape every cloud agent product converges on for the same reason.
export function ConnectSourceControl({ installUrl }: { installUrl: string | null }) {
  return (
    <section className="mx-auto w-full max-w-2xl px-6 pb-16 pt-24">
      <div className="rounded-2xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
        <h1 className="text-lg font-semibold text-gray-900">Connect source control</h1>
        <p className="mt-1.5 text-sm text-gray-500">
          Agent jobs run against a repository the platform is installed on. Nothing can be queued
          until one is connected — the sandbox never holds a credential itself, so the platform
          needs its own installation to check the code out and open the draft PR.
        </p>

        <div className="mt-5 flex flex-wrap items-center gap-2">
          {installUrl ? (
            <a
              href={installUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 rounded-lg bg-gray-900 px-3.5 py-2 text-[13px] font-medium text-white hover:bg-gray-800"
            >
              <svg className="h-4 w-4" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
              </svg>
              Connect GitHub
            </a>
          ) : (
            <p className="text-[13px] text-gray-500">
              No installation URL is configured. An operator sets{' '}
              <code className="rounded bg-gray-100 px-1 py-0.5 text-[12px]">
                AGENT_GITHUB_APP_INSTALL_URL
              </code>{' '}
              and{' '}
              <code className="rounded bg-gray-100 px-1 py-0.5 text-[12px]">
                AGENT_REPO_ALLOWLIST
              </code>
              .
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
