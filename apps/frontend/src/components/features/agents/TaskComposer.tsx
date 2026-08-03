'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { createAgentJob, getAgentConfig, listRepoBranches } from '@/lib/api/agents';
import type { AgentConfigApi } from '@/lib/api/agents';
import { ConnectSourceControl } from './ConnectSourceControl';
import { Picker } from './Picker';

// New-task composer (the /agents index state). Runtime × model are first-class
// controls — BYOA × BYOM is the product, not an advanced option.
//
// Everything here is read from the backend rather than written into the markup.
// The previous version showed a repository, a branch, a runtime and a model as
// fixed labels and then submitted different hardcoded values, so the screen
// described a job nobody was running; and it showed those controls whether or
// not any source control was connected, so it looked ready when nothing it
// produced could run.
const TIER_LABELS: Record<string, string> = {
  platform_only: 'PlatformOnly',
  trusted: 'Trusted',
  custom: 'Custom',
  full: 'Open',
};

export function TaskComposer({ initialRepo }: { initialRepo?: string }) {
  const router = useRouter();
  const [task, setTask] = useState('');
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [repo, setRepo] = useState('');
  const [branches, setBranches] = useState<string[]>([]);
  const [branch, setBranch] = useState('');
  const [runtime, setRuntime] = useState('');
  const [model, setModel] = useState('');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getAgentConfig()
      .then((cfg) => {
        if (cancelled) return;
        // The config's model list is the create endpoint's own predicate;
        // /v1/models answers for the browsing user and offered models whose
        // first agent call then 404ed.
        const modelIds = cfg.models ?? [];
        setConfig(cfg);
        setModels(modelIds);
        setRepo(
          initialRepo && cfg.repos.includes(initialRepo) ? initialRepo : (cfg.repos[0] ?? ''),
        );
        setRuntime(cfg.runtimes[0] ?? '');
        setModel(modelIds[0] ?? '');
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : 'could not load config');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [initialRepo]);

  // Branches follow the selected repository, so they reload when it changes.
  useEffect(() => {
    if (!repo || !config?.github_connected) return undefined;
    let cancelled = false;
    listRepoBranches(repo)
      .then((found) => {
        if (cancelled) return;
        setBranches(found.branches);
        setBranch(found.default ?? found.branches[0] ?? '');
      })
      .catch(() => {
        // No branch list is a working state: the job then runs against the
        // repository's own default, which is what omitting a ref means.
        if (!cancelled) {
          setBranches([]);
          setBranch('');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [config?.github_connected, repo]);

  const disconnected = config?.github_connected === false;
  const canRun =
    config?.github_connected === true &&
    task.trim().length > 0 &&
    !submitting &&
    Boolean(repo && runtime && model);

  async function run() {
    if (!canRun) return;
    setSubmitting(true);
    setError(null);
    try {
      // Exactly what the controls show. That this needs saying is the bug it
      // replaced: the old composer displayed one thing and queued another.
      const job = await createAgentJob({
        repo,
        task_prompt: task.trim(),
        runtime,
        model,
        // Sent as a ref; the platform pins it to a commit at creation, because
        // a branch moves and the publisher applies onto a fixed one.
        ...(branch ? { base_ref: branch } : {}),
      });
      router.push(`/agents/${job.id}`);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'could not start the job');
      setSubmitting(false);
    }
  }

  if (loading) {
    return (
      <section className="mx-auto w-full max-w-3xl px-6 pt-28">
        <p className="text-center text-sm text-gray-500">Loading…</p>
      </section>
    );
  }

  return (
    <section className="mx-auto w-full max-w-3xl px-6 pb-20 pt-28">
      <h1 className="text-center text-[28px] font-semibold tracking-[-0.025em] text-gray-950">
        What should the agent do?
      </h1>
      <p className="mt-2 text-center text-sm leading-relaxed text-gray-500">
        Runs in an isolated sandbox. No credentials inside — the output is a draft PR.
      </p>

      {disconnected ? (
        <div className="mt-8">
          <ConnectSourceControl installUrl={config.github_install_url} />
        </div>
      ) : null}

      <div
        aria-disabled={disconnected || undefined}
        className={`rounded-xl border border-gray-200/90 shadow-[0_14px_40px_-28px_rgba(17,24,39,0.5)] transition ${
          disconnected
            ? 'mt-4 bg-gray-50'
            : 'mt-8 bg-white focus-within:border-crimson/30 focus-within:ring-4 focus-within:ring-crimson/[0.05]'
        }`}
      >
        <textarea
          rows={4}
          value={task}
          onChange={(event) => setTask(event.target.value)}
          disabled={disconnected}
          placeholder={
            disconnected
              ? 'Connect GitHub to describe a task'
              : 'Describe a task… e.g. Fix the SSE total-timeout regression on /v1/messages and add a unit test'
          }
          className="w-full resize-none rounded-t-xl border-0 bg-transparent px-5 pb-2 pt-4 text-[15px] leading-relaxed text-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-0 disabled:cursor-not-allowed"
        />

        <div className="flex flex-wrap items-center gap-2 border-t border-gray-100/90 px-3.5 py-2.5">
          {disconnected ? (
            <span className="inline-flex min-w-0 flex-1 items-center gap-2 text-[13px] text-gray-400">
              <svg
                className="h-4 w-4 shrink-0"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                aria-hidden="true"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M8 10V7a4 4 0 0 1 8 0v3m-9 0h10a1 1 0 0 1 1 1v8H6v-8a1 1 0 0 1 1-1Z"
                />
              </svg>
              Connect GitHub to select a repository and run
            </span>
          ) : (
            <>
              <Picker
                label="Repository"
                value={repo}
                options={config?.repos ?? []}
                onChange={setRepo}
              />
              <Picker label="Branch" value={branch} options={branches} onChange={setBranch} />
              <span className="h-4 w-px bg-gray-200" />
              <Picker
                label="Agent"
                value={runtime}
                options={config?.runtimes ?? []}
                onChange={setRuntime}
              />
              <Picker label="Model" value={model} options={models} onChange={setModel} />
            </>
          )}

          <button
            type="button"
            disabled={!canRun}
            onClick={() => void run()}
            className={`ml-auto inline-flex items-center gap-1.5 rounded-lg bg-crimson px-3.5 py-1.5 text-[13px] font-medium text-white shadow-sm transition-colors ${
              canRun ? 'hover:bg-crimson-dark' : 'cursor-not-allowed opacity-60'
            }`}
          >
            {submitting ? 'Starting…' : 'Run'}
            <svg
              className="h-3.5 w-3.5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14m0 0-6-6m6 6-6 6" />
            </svg>
          </button>
        </div>
      </div>

      {error ? (
        <p className="mt-3 text-center text-[13px] text-red-600" role="alert">
          {error}
        </p>
      ) : null}

      {/* Only what this deployment is actually running. Budget was shown here as
          a fixed "$2.00" the composer never sent — it is a backend cap, not a
          choice made on this screen, so it belongs on the job instead. */}
      {config?.github_connected && config.agent_egress_tier ? (
        <p className="mt-3 text-center text-[12px] text-gray-500">
          Network: setup {TIER_LABELS[config.setup_egress_tier ?? ''] ?? config.setup_egress_tier} ·
          agent {TIER_LABELS[config.agent_egress_tier] ?? config.agent_egress_tier}
        </p>
      ) : null}
    </section>
  );
}
