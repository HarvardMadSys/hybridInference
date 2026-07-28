'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { createAgentJob, getAgentConfig, listAgentModels } from '@/lib/api/agents';
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

export function TaskComposer() {
  const router = useRouter();
  const [task, setTask] = useState('');
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [repo, setRepo] = useState('');
  const [runtime, setRuntime] = useState('');
  const [model, setModel] = useState('');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getAgentConfig(), listAgentModels().catch(() => [] as string[])])
      .then(([cfg, modelIds]) => {
        if (cancelled) return;
        setConfig(cfg);
        setModels(modelIds);
        setRepo(cfg.repos[0] ?? '');
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
  }, []);

  const canRun = task.trim().length > 0 && !submitting && Boolean(repo && runtime && model);

  async function run() {
    if (!canRun) return;
    setSubmitting(true);
    setError(null);
    try {
      // Exactly what the controls show. That this needs saying is the bug it
      // replaced: the old composer displayed one thing and queued another.
      const job = await createAgentJob({ repo, task_prompt: task.trim(), runtime, model });
      router.push(`/agents/${job.id}`);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'could not start the job');
      setSubmitting(false);
    }
  }

  if (loading) {
    return (
      <section className="mx-auto w-full max-w-2xl px-6 pt-24">
        <p className="text-center text-sm text-gray-500">Loading…</p>
      </section>
    );
  }

  if (config && !config.github_connected) {
    return <ConnectSourceControl installUrl={config.github_install_url} />;
  }

  return (
    <section className="mx-auto w-full max-w-2xl px-6 pb-16 pt-24">
      <h1 className="text-center text-2xl font-bold text-gray-900">What should the agent do?</h1>
      <p className="mt-1.5 text-center text-sm text-gray-500">
        Runs in an isolated sandbox. No credentials inside — the output is a draft PR.
      </p>

      <div className="mt-8 rounded-2xl bg-white shadow-sm ring-1 ring-gray-200 focus-within:ring-2 focus-within:ring-gray-300">
        <textarea
          rows={4}
          value={task}
          onChange={(event) => setTask(event.target.value)}
          placeholder="Describe a task… e.g. Fix the SSE total-timeout regression on /v1/messages and add a unit test"
          className="w-full resize-none rounded-t-2xl border-0 bg-transparent px-5 pt-4 text-[15px] leading-relaxed placeholder:text-gray-400 focus:outline-none focus:ring-0"
        />

        <div className="flex flex-wrap items-center gap-2 border-t border-gray-100 px-3.5 py-2.5">
          <Picker label="Repository" value={repo} options={config?.repos ?? []} onChange={setRepo} />
          <span className="h-4 w-px bg-gray-200" />
          <Picker
            label="Runtime"
            value={runtime}
            options={config?.runtimes ?? []}
            onChange={setRuntime}
          />
          <Picker label="Model" value={model} options={models} onChange={setModel} />

          <button
            type="button"
            disabled={!canRun}
            onClick={() => void run()}
            className={`ml-auto inline-flex items-center gap-1.5 rounded-lg bg-gray-900 px-3.5 py-1.5 text-[13px] font-medium text-white ${
              canRun ? 'hover:bg-gray-800' : 'cursor-not-allowed opacity-60'
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
      {config?.agent_egress_tier ? (
        <p className="mt-3 text-center text-[12px] text-gray-400">
          Network: setup{' '}
          {TIER_LABELS[config.setup_egress_tier ?? ''] ?? config.setup_egress_tier} · agent{' '}
          {TIER_LABELS[config.agent_egress_tier] ?? config.agent_egress_tier}
        </p>
      ) : null}
    </section>
  );
}
