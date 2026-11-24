'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { getUpstreamConcurrency } from '@/lib/api/admin';
import type { UpstreamConcurrencyResponse, UpstreamEngineQueue } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

// The gateway learns these limits from live 429s and successes, so a stale view
// is a misleading one. Matches the 10s cadence AlertSnoozeSection polls at —
// fast enough to watch a limit walk down under pressure, slow enough that an
// idle admin tab is not a traffic source of its own.
const REFRESH_INTERVAL_MS = 10_000;

function formatWait(seconds: number): string {
  if (seconds <= 0) return '—';
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  return `${seconds.toFixed(1)} s`;
}

function countClass(value: number): string {
  return value > 0 ? 'font-medium text-amber-600' : 'text-gray-400';
}

// Who keeps the queue: a registration, the queue-wait offload's held requests,
// or nobody any more (listed only until the requests it counted are done).
function queueSource(queue: UpstreamEngineQueue): string {
  if (queue.registered) return `Registered by ${queue.owners.join(', ')}`;
  if (queue.held) return 'Queue-wait offload';
  return 'No longer registered';
}

// The gateway's queue in front of each local engine that has one. Shown only
// when some engine does, so most deployments never see it.
function EngineQueuesTable({ engines }: { engines: UpstreamEngineQueue[] }) {
  return (
    <div className="mt-4" data-testid="engine-queues">
      <h4 className="text-[13px] font-semibold text-gray-900">Local engine queues</h4>
      <p className="mt-1 text-[12px] text-gray-500">
        Requests each local engine was sent that have no first token yet, and the line the gateway
        holds behind them: a request waits here, where its router can still send it elsewhere,
        instead of inside the engine. Counts are since the gateway started.
      </p>
      <div className="mt-2 overflow-x-auto rounded-lg border border-gray-200">
        <table className="min-w-[760px] w-full text-[13px]">
          <thead className="bg-gray-50 text-left text-[12px] uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-3 py-2">Engine</th>
              <th className="px-3 py-2 text-right">Limit</th>
              <th className="px-3 py-2 text-right">Awaiting first token</th>
              <th className="px-3 py-2 text-right">Waiting</th>
              <th className="px-3 py-2 text-right">Sent</th>
              <th className="px-3 py-2 text-right">Left unsent</th>
              <th className="px-3 py-2 text-right">Sent past limit</th>
              <th className="px-3 py-2 text-right">Longest wait</th>
              <th className="px-3 py-2 text-right">Held to the end</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 bg-white">
            {engines.map((queue) => (
              <tr key={queue.engine}>
                <td className="px-3 py-2">
                  <div className="font-mono text-[12px] text-gray-900">{queue.engine}</div>
                  <div className="text-[11px] text-gray-400">{queueSource(queue)}</div>
                </td>
                <td
                  className="px-3 py-2 text-right tabular-nums text-gray-900"
                  data-testid={`engine-limit-${queue.engine}`}
                >
                  {queue.limit}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-gray-700">{queue.pending}</td>
                <td className={`px-3 py-2 text-right tabular-nums ${countClass(queue.waiting)}`}>
                  {queue.waiting}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-gray-700">
                  {queue.sent_at_once + queue.sent_after_wait}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-gray-700">{queue.expired}</td>
                <td className={`px-3 py-2 text-right tabular-nums ${countClass(queue.overflowed)}`}>
                  {queue.overflowed}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-gray-500">
                  {formatWait(queue.wait_seconds_max)}
                </td>
                <td
                  className={`px-3 py-2 text-right tabular-nums ${countClass(
                    queue.released_answered,
                  )}`}
                  title="Requests that held their place for the whole generation: answered whole, or by an adapter that reports no first token."
                >
                  {queue.released_answered}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function UpstreamConcurrencyPanel() {
  const [state, setState] = useState<UpstreamConcurrencyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // A poll in flight when the sub-tab goes away would otherwise land its
  // setState on an unmounted tree.
  const mounted = useRef(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await getUpstreamConcurrency();
      if (!mounted.current) return;
      setState(next);
    } catch (e) {
      if (!mounted.current) return;
      setError(getErrorMessage(e));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    const id = setInterval(() => void load(), REFRESH_INTERVAL_MS);
    return () => {
      mounted.current = false;
      clearInterval(id);
    };
  }, [load]);

  const config = state?.config;
  const buckets = state?.buckets ?? [];
  const engines = state?.engines ?? [];

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-[14px] font-semibold text-gray-900">Upstream concurrency</h3>
          <p className="mt-1 text-[12px] text-gray-500">
            How many requests the gateway will keep open at once against each provider key. The
            limit is learned, not configured: an upstream 429 takes one off, and a run of successful
            responses probes for one more.
          </p>
        </div>
        <button
          type="button"
          disabled={loading}
          onClick={() => void load()}
          className="h-8 rounded-md border border-gray-200 px-3 text-[12px] font-medium text-gray-700 disabled:opacity-50"
        >
          {loading ? 'Loading...' : 'Refresh'}
        </button>
      </div>

      {error && (
        <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-700">
          {error}
          <button type="button" onClick={() => void load()} className="ml-2 font-medium underline">
            Retry
          </button>
        </div>
      )}

      {config && !config.enabled && (
        <div className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-[12px] text-amber-800">
          The limiter is disabled (UPSTREAM_CONCURRENCY_ENABLED=false). Outbound requests are not
          being capped.
        </div>
      )}

      {config && (
        <div
          className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-y border-gray-100 py-2 text-[12px] text-gray-500"
          data-testid="upstream-concurrency-config"
        >
          <span>
            Starts at <span className="tabular-nums text-gray-900">{config.initial_limit}</span>
          </span>
          <span>
            Ceiling <span className="tabular-nums text-gray-900">{config.max_limit}</span>
          </span>
          <span>
            Probes every{' '}
            <span className="tabular-nums text-gray-900">{config.probe_success_interval}</span>{' '}
            successes
          </span>
          <span>
            Waits up to{' '}
            <span className="tabular-nums text-gray-900">{config.acquire_timeout_sec}s</span> for a
            slot
          </span>
        </div>
      )}

      {loading && state === null ? (
        <div className="flex justify-center py-12">
          <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
        </div>
      ) : buckets.length === 0 ? (
        // Buckets are created by traffic, so an idle gateway legitimately has
        // none. Say that, rather than showing an empty table that reads as a
        // failed load.
        <div className="mt-3 rounded-lg border border-dashed border-gray-200 py-8 text-center text-[12px] text-gray-400">
          No remote traffic yet — a provider key gets a limit the first time the gateway calls it.
        </div>
      ) : (
        <div className="mt-3 overflow-x-auto rounded-lg border border-gray-200">
          <table className="min-w-[620px] w-full text-[13px]">
            <thead className="bg-gray-50 text-left text-[12px] uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Provider</th>
                <th className="px-3 py-2">Key</th>
                <th className="px-3 py-2 text-right">Limit</th>
                <th className="px-3 py-2 text-right">In flight</th>
                <th className="px-3 py-2 text-right">Waiting</th>
                <th className="px-3 py-2 text-right">Next probe</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 bg-white">
              {buckets.map((bucket) => (
                <tr key={`${bucket.provider}-${bucket.key_fingerprint}`}>
                  <td className="px-3 py-2 text-gray-900">{bucket.provider}</td>
                  <td className="px-3 py-2 font-mono text-[12px] text-gray-500">
                    {bucket.key_fingerprint}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <span
                      className="text-[18px] font-semibold tabular-nums text-gray-900"
                      data-testid={`upstream-limit-${bucket.key_fingerprint}`}
                    >
                      {bucket.limit}
                    </span>
                    {bucket.probing && (
                      <span
                        className="ml-1.5 align-middle text-[11px] font-medium text-emerald-600"
                        title="The limit was just raised by a probe; a 429 would take it straight back."
                      >
                        probing
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-gray-700">
                    {bucket.in_flight}
                  </td>
                  <td
                    className={`px-3 py-2 text-right tabular-nums ${
                      bucket.waiting > 0 ? 'font-medium text-amber-600' : 'text-gray-400'
                    }`}
                  >
                    {bucket.waiting}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-gray-500">
                    {bucket.successes_since_probe}/{config?.probe_success_interval ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {engines.length > 0 && <EngineQueuesTable engines={engines} />}
    </section>
  );
}
