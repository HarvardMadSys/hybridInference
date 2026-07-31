'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import type { AgentRunnerHost } from '@/lib/api/admin';
import {
  forgetAgentRunnerHost,
  getAgentRunnerHosts,
  setActiveAgentRunnerHost,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const ANY_HOST = '__any__';

// A runner polls only between jobs, so silence can mean "busy for an hour" as
// easily as "gone". The wording stays "last polled" everywhere for that reason:
// the page reports what it actually knows rather than guessing at liveness.
const RECENT_POLL_SECONDS = 300;

function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

interface AgentRunnerHostSectionProps {
  onToast: (msg: string) => void;
}

export function AgentRunnerHostSection({ onToast }: AgentRunnerHostSectionProps) {
  const [hosts, setHosts] = useState<AgentRunnerHost[]>([]);
  const [activeHost, setActiveHost] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const mounted = useRef(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await getAgentRunnerHosts();
      if (!mounted.current) return;
      setHosts(next.hosts);
      setActiveHost(next.active_host);
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
    return () => {
      mounted.current = false;
    };
  }, [load]);

  const handleSelect = useCallback(
    async (value: string) => {
      const host = value === ANY_HOST ? null : value;
      setBusy(true);
      try {
        const next = await setActiveAgentRunnerHost(host);
        if (!mounted.current) return;
        setHosts(next.hosts);
        setActiveHost(next.active_host);
        onToast(
          host
            ? `New agent jobs now run on ${host}. Jobs already running elsewhere finish where they are.`
            : 'Agent jobs unpinned — any runner may claim.',
        );
      } catch (e) {
        if (mounted.current) onToast(`Could not switch host: ${getErrorMessage(e)}`);
      } finally {
        if (mounted.current) setBusy(false);
      }
    },
    [onToast],
  );

  const handleForget = useCallback(
    async (host: string) => {
      setBusy(true);
      try {
        const next = await forgetAgentRunnerHost(host);
        if (!mounted.current) return;
        setHosts(next.hosts);
        setActiveHost(next.active_host);
        onToast(`Removed ${host} from the pool.`);
      } catch (e) {
        if (mounted.current) onToast(`Could not remove ${host}: ${getErrorMessage(e)}`);
      } finally {
        if (mounted.current) setBusy(false);
      }
    },
    [onToast],
  );

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <div className="mb-3">
        <h2 className="text-[14px] font-semibold text-gray-900">Cloud Agent Host</h2>
        <p className="mt-1 text-[12px] text-gray-500">
          Which machine runs cloud agent jobs. A host appears here once a runner on it polls for
          work, so the list is the set of machines that are actually configured and reachable.
          Switching takes effect on the next poll; jobs already running elsewhere finish where they
          are rather than being interrupted.
        </p>
      </div>

      {error && (
        <div className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600">
          {error}
          <button type="button" onClick={() => void load()} className="ml-2 font-medium underline">
            Retry
          </button>
        </div>
      )}

      {loading ? (
        <div className="py-8 text-center text-[13px] text-gray-400">Loading...</div>
      ) : (
        <div className="space-y-2">
          <label
            className="flex cursor-pointer items-center gap-3 rounded-md border border-gray-200 px-3 py-2 text-[13px]"
            htmlFor="agent-host-any"
          >
            <input
              id="agent-host-any"
              type="radio"
              name="agent-runner-host"
              className="h-4 w-4"
              disabled={busy}
              checked={activeHost === null}
              onChange={() => void handleSelect(ANY_HOST)}
            />
            <span className="flex-1">
              <span className="font-medium text-gray-900">Any host</span>
              <span className="ml-2 text-[12px] text-gray-500">
                Whichever runner polls first takes the job.
              </span>
            </span>
          </label>

          {hosts.map((host) => {
            const recent = host.seconds_since_seen <= RECENT_POLL_SECONDS;
            const inputId = `agent-host-${host.host}`;
            return (
              <div
                key={host.host}
                className="flex items-center gap-3 rounded-md border border-gray-200 px-3 py-2 text-[13px]"
              >
                <input
                  id={inputId}
                  type="radio"
                  name="agent-runner-host"
                  className="h-4 w-4"
                  disabled={busy}
                  checked={host.active}
                  onChange={() => void handleSelect(host.host)}
                />
                <label htmlFor={inputId} className="flex flex-1 cursor-pointer flex-col">
                  <span className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className={`inline-block h-2 w-2 rounded-full ${
                        recent ? 'bg-green-500' : 'bg-gray-300'
                      }`}
                    />
                    <span className="font-medium text-gray-900">{host.host}</span>
                    {host.active && (
                      <span className="rounded bg-green-50 px-1.5 py-0.5 text-[11px] font-medium text-green-700">
                        active
                      </span>
                    )}
                  </span>
                  <span className="mt-0.5 text-[12px] text-gray-500">
                    Last polled {formatAge(host.seconds_since_seen)}
                    {host.last_worker_id ? ` · ${host.last_worker_id}` : ''}
                  </span>
                </label>
                {!host.active && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void handleForget(host.host)}
                    className="text-[12px] font-medium text-gray-500 underline disabled:opacity-50"
                  >
                    Remove
                  </button>
                )}
              </div>
            );
          })}

          {hosts.length === 0 && (
            <div className="rounded-md border border-dashed border-gray-200 px-4 py-6 text-center text-[12px] text-gray-500">
              No runner has reported a host yet. Set <code>AGENT_RUNNER_HOST</code> on each machine
              and start its runner (<code>ops/deploy/agent_runner.sh up</code>); it appears here
              within seconds.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
