'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';

import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_SECONDARY,
  PANEL,
  PILL,
  TABLE_CELL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import { durationText, StatTile } from '@/components/features/admin/batchUsage';
import {
  getModelQueue,
  type ModelQueue,
  type ModelQueueJob,
  type ModelQueueModel,
} from '@/lib/api/batches';
import { getErrorMessage } from '@/lib/utils/errors';

/** How often the page reads the queue again while it is open. */
const REFRESH_MS = 60_000;

/** The order models are listed in: those waiting first, idle ones last. */
const STATE_RANK: Record<string, number> = { queued: 0, loading: 1, serving: 2, idle: 3 };

const STATE_TONE: Record<string, string> = {
  serving: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  loading: 'bg-blue-50 text-blue-700 ring-blue-600/20',
  queued: 'bg-amber-50 text-amber-800 ring-amber-600/20',
  idle: 'bg-gray-100 text-gray-600 ring-gray-500/20',
};

const STATE_HELP: Record<string, string> = {
  serving: 'Its job holds a GPU and serves the model.',
  loading: 'Its job holds a GPU and is loading the model; requests wait until it serves.',
  queued: 'Its job waits in the scheduler’s queue for a GPU; requests wait until it serves.',
  idle: 'No job: the next request for it queues one.',
};

function when(epoch: number | null | undefined): string {
  return epoch
    ? new Date(epoch * 1000).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' })
    : '—';
}

/** "5 min ago" / "in 2 h", against `now` (epoch seconds). */
function relative(epoch: number, now: number): string {
  const delta = epoch - now;
  return delta >= 0 ? `in ${durationText(delta)}` : `${durationText(-delta)} ago`;
}

function plural(count: number, one: string, many: string): string {
  return `${count.toLocaleString('en-US')} ${count === 1 ? one : many}`;
}

function StatePill({ state }: { state: string }) {
  return (
    <span
      className={`${PILL} ring-1 ring-inset ${STATE_TONE[state] ?? STATE_TONE.idle}`}
      title={STATE_HELP[state]}
    >
      {state}
    </span>
  );
}

/** One job in words: its id and state, where it runs or why it waits, and its times. */
function JobLine({ job, now }: { job: ModelQueueJob; now: number }) {
  const running = job.state === 'RUNNING';
  const facts = [
    job.partition,
    running ? job.nodes : job.reason ? `waiting: ${job.reason}` : null,
  ].filter(Boolean);
  let timing: string | null = null;
  if (running && job.started_at) {
    timing = `started ${relative(job.started_at, now)}`;
    if (job.ends_at) timing += ` · walltime ends ${relative(job.ends_at, now)}`;
  } else if (job.submitted_at) {
    timing = `submitted ${relative(job.submitted_at, now)}`;
    if (job.expected_start_at) {
      timing += ` · est. start ${relative(job.expected_start_at, now)}`;
    }
  }
  return (
    <div className="text-[12px]" data-testid={`job-${job.job_id ?? 'unknown'}`}>
      <span className="font-mono text-gray-900">{job.job_id ?? '—'}</span>{' '}
      <span className="text-gray-600">{job.state}</span>
      {facts.length > 0 && <span className="text-gray-500"> · {facts.join(' · ')}</span>}
      {timing && (
        <span
          className="block text-[11px] text-gray-500"
          title={[
            job.submitted_at && `Submitted ${when(job.submitted_at)}`,
            job.started_at && `Started ${when(job.started_at)}`,
            job.expected_start_at && `Scheduler's estimated start ${when(job.expected_start_at)}`,
            job.ends_at && `Walltime ends ${when(job.ends_at)}`,
          ]
            .filter(Boolean)
            .join('\n')}
        >
          {timing}
        </span>
      )}
    </div>
  );
}

function BatchRequests({ model }: { model: ModelQueueModel }) {
  if (model.batch_queued === null || model.batch_queued === undefined) {
    return <span className="text-gray-400">—</span>;
  }
  return (
    <>
      <span className={model.batch_queued > 0 ? 'font-medium text-amber-800' : 'text-gray-700'}>
        {model.batch_queued.toLocaleString('en-US')} waiting
      </span>
      {(model.batch_running ?? 0) > 0 && (
        <span className="block text-[11px] text-gray-500">
          {(model.batch_running ?? 0).toLocaleString('en-US')} running
        </span>
      )}
    </>
  );
}

function ModelRow({ model, now }: { model: ModelQueueModel; now: number }) {
  return (
    <tr data-testid={`queue-${model.model}`}>
      <td className={TABLE_CELL}>
        <span className="font-medium text-gray-900">{model.model}</span>
        {model.gateway_models.length > 0 && (
          <span className="block text-[11px] text-gray-500">{model.gateway_models.join(', ')}</span>
        )}
      </td>
      <td className={TABLE_CELL}>
        <StatePill state={model.state} />
      </td>
      <td className={TABLE_CELL}>
        {model.jobs.length === 0 ? (
          <span className="text-gray-400">—</span>
        ) : (
          <div className="space-y-1">
            {model.jobs.map((job, index) => (
              <JobLine key={job.job_id ?? index} job={job} now={now} />
            ))}
          </div>
        )}
        {model.avoided_nodes.length > 0 && (
          <span
            className="mt-1 block text-[11px] text-amber-700"
            title="Nodes its next job keeps off, because a job failed there."
          >
            Avoids {model.avoided_nodes.join(', ')}
          </span>
        )}
      </td>
      <td className={`${TABLE_CELL} whitespace-nowrap`}>
        <BatchRequests model={model} />
      </td>
      <td className={`${TABLE_CELL} whitespace-nowrap`}>
        {model.last_request_at ? (
          <span title={when(model.last_request_at)}>{relative(model.last_request_at, now)}</span>
        ) : (
          <span className="text-gray-400">—</span>
        )}
      </td>
    </tr>
  );
}

function Policy({ queue }: { queue: ModelQueue }) {
  const policy = queue.policy;
  if (!policy) return null;
  const lines = [
    policy.pending_idle_cancel_seconds &&
      `a waiting job is cancelled after ${durationText(policy.pending_idle_cancel_seconds)} without a request`,
    policy.running_idle_stop_seconds &&
      `a running job is stopped after ${durationText(policy.running_idle_stop_seconds)} without one`,
    policy.renew_before_seconds &&
      `a model still in demand gets its next job ${durationText(policy.renew_before_seconds)} before the walltime ends`,
    policy.node_avoid_seconds &&
      `a node where a job failed is avoided for ${durationText(policy.node_avoid_seconds)}`,
  ].filter(Boolean);
  if (lines.length === 0) return null;
  return <p className="text-[12px] text-gray-500">On this host, {lines.join('; ')}.</p>;
}

/**
 * The queue of the on-demand model host the gateway is pointed at
 * (`MODEL_QUEUE_STATUS_URL`): each model's state and scheduler jobs, with the
 * batch requests waiting for it, read again every minute while open.
 */
export function ModelQueueTab() {
  const [queue, setQueue] = useState<ModelQueue | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const generation = useRef(0);

  const load = useCallback(async () => {
    const mine = ++generation.current;
    setLoading(true);
    try {
      const next = await getModelQueue();
      if (mine !== generation.current) return;
      setQueue(next);
      setError(null);
    } catch (err) {
      if (mine === generation.current) setError(getErrorMessage(err));
    } finally {
      if (mine === generation.current) {
        setLoading(false);
        setNow(Date.now() / 1000);
      }
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  const refresh = (
    <button
      type="button"
      className={BUTTON_SECONDARY}
      disabled={loading}
      aria-busy={loading || undefined}
      onClick={() => void load()}
    >
      Refresh
    </button>
  );

  if (!queue) {
    return (
      <AdminSection title="Model queue" actions={refresh}>
        {error ? (
          <AdminNotice tone="error">{error}</AdminNotice>
        ) : (
          <AdminLoading label="Loading the model queue" />
        )}
      </AdminSection>
    );
  }

  if (!queue.configured) {
    return (
      <AdminSection title="Model queue" actions={refresh}>
        <AdminEmptyState title="No model queue is configured">
          Set <code>MODEL_QUEUE_STATUS_URL</code> under{' '}
          <Link href="/dashboard/admin/settings" prefetch={false} className="underline">
            Settings → Providers
          </Link>{' '}
          to the queue status address of an on-demand model host, such as an HPC cluster’s on-demand
          proxy, and this page shows its jobs and the batch requests waiting for them.
        </AdminEmptyState>
      </AdminSection>
    );
  }

  const models = [...queue.models].sort(
    (a, b) =>
      (STATE_RANK[a.state] ?? 4) - (STATE_RANK[b.state] ?? 4) ||
      (b.batch_queued ?? 0) - (a.batch_queued ?? 0) ||
      a.model.localeCompare(b.model),
  );
  const active = models.filter((model) => model.state !== 'idle' || model.jobs.length > 0);
  const idle = models.filter((model) => model.state === 'idle' && model.jobs.length === 0);
  const count = (state: string) => queue.models.filter((model) => model.state === state).length;
  const knownWaiting = queue.models.filter((model) => model.batch_queued !== null);
  const waiting = knownWaiting.reduce((sum, model) => sum + (model.batch_queued ?? 0), 0);
  const name = queue.cluster || 'On-demand';

  return (
    <div className="space-y-6">
      <AdminSection
        title={`${name} model queue`}
        description={
          <>
            Models served on demand
            {queue.scheduler ? ` through its ${queue.scheduler} scheduler` : ''}: a request for a
            model without a job queues one, and the model&apos;s requests wait until it serves.
            Batch requests wait without spending an attempt.{' '}
            {queue.refreshed_at ? (
              <span title={when(queue.refreshed_at)}>
                Queue last read {relative(queue.refreshed_at, now)}.
              </span>
            ) : (
              'The host has not read its queue since it started: no job has been asked for.'
            )}
          </>
        }
        actions={refresh}
      >
        {(error || queue.error) && (
          <div className="mb-3">
            <AdminNotice tone="error">{error ?? queue.error}</AdminNotice>
          </div>
        )}
        {!queue.error && (
          <div className="space-y-4">
            <dl className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatTile
                label="Serving"
                value={count('serving').toLocaleString('en-US')}
                detail={plural(queue.models.length, 'model', 'models')}
              />
              <StatTile label="Loading" value={count('loading').toLocaleString('en-US')} />
              <StatTile label="Queued" value={count('queued').toLocaleString('en-US')} />
              <StatTile
                label="Batch requests waiting"
                value={knownWaiting.length ? waiting.toLocaleString('en-US') : '—'}
                detail={knownWaiting.length ? 'For these models' : 'Not known'}
              />
            </dl>
            {active.length === 0 ? (
              <AdminEmptyState title="No job is serving or waiting">
                Every model is idle: the next request for one queues its job.
              </AdminEmptyState>
            ) : (
              <div className={`${PANEL} relative overflow-x-auto`}>
                <table className="min-w-full divide-y divide-gray-200">
                  <thead>
                    <tr>
                      <th className={TABLE_HEAD_CELL}>Model</th>
                      <th className={TABLE_HEAD_CELL}>State</th>
                      <th className={`${TABLE_HEAD_CELL} min-w-[280px]`}>Jobs</th>
                      <th className={TABLE_HEAD_CELL}>Batch requests</th>
                      <th className={TABLE_HEAD_CELL}>Last request</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {active.map((model) => (
                      <ModelRow key={model.model} model={model} now={now} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {idle.length > 0 && (
              <p className="text-[12px] text-gray-500" data-testid="idle-models">
                <span className="font-medium text-gray-600">Idle, no job:</span>{' '}
                {idle.map((model) => model.model).join(', ')}
              </p>
            )}
            <Policy queue={queue} />
          </div>
        )}
      </AdminSection>

      {!queue.error && queue.failed_nodes.length > 0 && (
        <AdminSection
          title="Failed nodes"
          description="Nodes where a job failed (the model server died loading or serving). The models that failed there keep their jobs off them for a while; every model does once two have failed on one."
        >
          <div className={`${PANEL} relative overflow-x-auto`}>
            <table className="min-w-full divide-y divide-gray-200">
              <thead>
                <tr>
                  <th className={TABLE_HEAD_CELL}>Node</th>
                  <th className={TABLE_HEAD_CELL}>Failed jobs</th>
                  <th className={TABLE_HEAD_CELL}>Avoided until</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {queue.failed_nodes.map((node) => (
                  <tr key={node.node} data-testid={`failed-${node.node}`}>
                    <td className={`${TABLE_CELL} font-mono text-[12px]`}>{node.node}</td>
                    <td className={TABLE_CELL}>
                      {node.jobs.join(', ')}
                      {node.avoided_by_all && (
                        <span className="block text-[11px] text-amber-700">
                          Avoided by every model
                        </span>
                      )}
                    </td>
                    <td className={`${TABLE_CELL} whitespace-nowrap`}>
                      {node.avoided_until ? (
                        <span title={when(node.avoided_until)}>
                          {relative(node.avoided_until, now)}
                        </span>
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </AdminSection>
      )}
    </div>
  );
}
