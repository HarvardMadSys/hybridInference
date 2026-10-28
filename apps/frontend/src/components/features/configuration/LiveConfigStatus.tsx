'use client';

import { useCallback, useEffect, useState } from 'react';

import {
  getLiveConfigStatus,
  type LiveConfigStatus as LiveStatus,
  type LiveGroupInstance,
  type LiveGroupStatus,
} from '@/lib/api/config';
import { getErrorMessage } from '@/lib/utils/errors';

import { PILL, TABLE_CELL, TABLE_HEAD_CELL } from '../admin/adminStyles';

/** How often the panel re-reads the status while it is on screen. */
const POLL_MS = 5_000;

type Tone = 'ok' | 'busy' | 'warn' | 'bad' | 'idle';

const TONE_CLASS: Record<Tone, string> = {
  ok: 'bg-emerald-50 text-emerald-700',
  busy: 'bg-sky-50 text-sky-700',
  warn: 'bg-amber-50 text-amber-800',
  bad: 'bg-red-50 text-red-700',
  idle: 'bg-gray-100 text-gray-600',
};

/** One process's standing on a group, in words, with the revision it has reached. */
export function describeInstance(
  instance: LiveGroupInstance,
  revision: number | null,
  epoch: string | null,
): { label: string; tone: Tone } {
  const report = instance.status ?? {};
  if (instance.lifecycle === 'stopped') return { label: 'Stopped', tone: 'idle' };
  if (instance.stale) return { label: 'Unknown — no heartbeat', tone: 'warn' };
  if (!instance.supports) return { label: 'Unsupported build', tone: 'warn' };
  if (report.epoch !== epoch) return { label: 'Not reported yet', tone: 'warn' };
  switch (report.state) {
    case 'applied':
    case 'paused': {
      const current = revision !== null && report.reconciled_revision === revision;
      const name = report.state === 'paused' ? 'Paused (no channel)' : 'Applied';
      return current
        ? { label: name, tone: report.state === 'paused' ? 'idle' : 'ok' }
        : { label: 'Applying', tone: 'busy' };
    }
    case 'applying':
      return { label: 'Applying', tone: 'busy' };
    case 'invalid':
      return { label: 'Invalid — previous values in use', tone: 'bad' };
    case 'failed':
      return { label: 'Failed — previous values in use', tone: 'bad' };
    case 'cleanup_blocked':
      return { label: 'Blocked by cleanup', tone: 'bad' };
    case 'unsupported':
      return { label: 'Unsupported', tone: 'warn' };
    case 'dormant':
      return { label: 'Waiting for restart', tone: 'idle' };
    case 'dormant-frozen':
      return { label: 'Frozen until restart', tone: 'warn' };
    default:
      return { label: 'Not reported yet', tone: 'warn' };
  }
}

function heartbeat(instance: LiveGroupInstance): string {
  const age = instance.heartbeat_age_seconds;
  if (age === null) return '—';
  return age < 1 ? 'just now' : `${Math.round(age)} s ago`;
}

function contractText(group: LiveGroupStatus): string {
  if (!group.contract) {
    return 'Not activated: saved changes apply after a restart, as before.';
  }
  if (!group.contract.active) {
    return 'Deactivated: processes that applied it keep those values until they restart.';
  }
  return 'Active: saved changes apply without a restart.';
}

function GroupStatus({ group, status }: { group: LiveGroupStatus; status: LiveStatus }) {
  const revision = status.version?.revision ?? null;
  const epoch = status.version?.epoch ?? null;
  const coverage = group.coverage;
  const instances = group.instances.filter((instance) => instance.lifecycle !== 'stopped');
  return (
    <div className="space-y-2">
      <p className="text-[12px] text-gray-700">
        <span className="font-medium text-gray-900">{group.name}</span>{' '}
        <span className="text-gray-500">v{group.schema_version}</span> — {contractText(group)}
      </p>
      {group.contract?.active ? (
        <p className="text-[12px] text-gray-700" role="status">
          {coverage.converged} of {coverage.registered} registered{' '}
          {coverage.registered === 1 ? 'process has' : 'processes have'} applied revision{' '}
          {revision ?? '—'}
          {coverage.unknown > 0 ? `; ${coverage.unknown} unknown` : ''}
          {coverage.failed > 0 ? `; ${coverage.failed} failed` : ''}
          {coverage.unsupported > 0 ? `; ${coverage.unsupported} unsupported` : ''}.
        </p>
      ) : null}
      <p className="text-[11px] text-gray-500">
        Coverage is not verified: a process on an older build does not register here. Compare with
        the deployment&apos;s own process list.
      </p>
      {instances.length > 0 ? (
        <div className="overflow-x-auto rounded border border-gray-100">
          <table className="min-w-full text-left">
            <thead className="bg-gray-50">
              <tr>
                <th scope="col" className={TABLE_HEAD_CELL}>
                  Process
                </th>
                <th scope="col" className={TABLE_HEAD_CELL}>
                  State
                </th>
                <th scope="col" className={TABLE_HEAD_CELL}>
                  Revision
                </th>
                <th scope="col" className={TABLE_HEAD_CELL}>
                  Heartbeat
                </th>
                <th scope="col" className={TABLE_HEAD_CELL}>
                  Notes
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {instances.map((instance) => {
                const report = instance.status ?? {};
                const standing = describeInstance(instance, revision, epoch);
                const notes = [
                  report.error_code ? `error: ${report.error_code}` : null,
                  report.error_keys?.length ? report.error_keys.join(', ') : null,
                  report.cleanup_degraded ? 'cleanup degraded' : null,
                  report.pending_keys?.length
                    ? `differs from saved: ${report.pending_keys.join(', ')}`
                    : null,
                  report.read_stale ? 'last read failed' : null,
                  typeof report.detail?.detector === 'string'
                    ? `detector ${report.detail.detector}`
                    : null,
                ].filter(Boolean);
                return (
                  <tr key={instance.instance_id}>
                    <td className={TABLE_CELL}>
                      <span className="font-mono text-[12px]">{instance.instance_id}</span>
                      {instance.this_process ? (
                        <span className="ml-1 text-[11px] text-gray-500">(this one)</span>
                      ) : null}
                      <div className="text-[11px] text-gray-500">
                        {instance.host ?? '—'} · build {instance.build}
                      </div>
                    </td>
                    <td className={TABLE_CELL}>
                      <span className={`${PILL} ${TONE_CLASS[standing.tone]}`}>
                        {standing.label}
                      </span>
                    </td>
                    <td className={`${TABLE_CELL} tabular-nums`}>
                      {report.reconciled_revision ?? '—'}
                      <span className="text-[11px] text-gray-500">
                        {' '}
                        (checked {report.checked_revision ?? '—'})
                      </span>
                    </td>
                    <td className={TABLE_CELL}>{heartbeat(instance)}</td>
                    <td className={`${TABLE_CELL} text-[12px]`}>{notes.join('; ') || '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-[12px] text-gray-500">No process has registered yet.</p>
      )}
    </div>
  );
}

/**
 * Where the saved configuration stands in every backend process, for the
 * groups that apply without a restart. Shows names, states and revisions only:
 * the endpoint never returns a value.
 */
export function LiveConfigStatus({
  groupName,
  refreshKey,
}: {
  /** The group to show, e.g. `alerts.runtime`. */
  groupName: string;
  /** Changes after each save, so the panel re-reads at once. */
  refreshKey?: string | number | null;
}) {
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const read = useCallback(async () => {
    try {
      setStatus(await getLiveConfigStatus());
      setError(null);
    } catch (e) {
      setError(getErrorMessage(e));
    }
  }, []);

  useEffect(() => {
    void read();
    const timer = setInterval(() => void read(), POLL_MS);
    return () => clearInterval(timer);
  }, [read, refreshKey]);

  const group = status?.groups.find((candidate) => candidate.name === groupName);
  return (
    <section
      aria-label="Applied configuration"
      className="my-3 rounded-lg border border-gray-200 bg-gray-50/60 p-3"
    >
      <h3 className="text-[12px] font-semibold text-gray-900">Applied in each backend process</h3>
      {error ? (
        <p className="mt-1 text-[12px] text-red-700" role="alert">
          Could not read the status: {error}
        </p>
      ) : null}
      {status && !status.database ? (
        <p className="mt-1 text-[12px] text-gray-600">
          This deployment has no database, so settings come from the environment and apply after a
          restart.
        </p>
      ) : null}
      {status?.database ? (
        <div className="mt-2 space-y-2">
          <p className="text-[12px] text-gray-700">
            Saved configuration:{' '}
            <span className="font-mono">
              {status.source} / epoch {status.version?.epoch ?? '—'} / revision{' '}
              {status.version?.revision ?? '—'}
            </span>
          </p>
          {group ? <GroupStatus group={group} status={status} /> : null}
          <p className="text-[11px] text-gray-500">
            Applied means the process uses the saved channel and thresholds; it does not mean a
            message was delivered — use Send test for that. After a channel change, the recovery of
            an incident opened earlier is posted to the new channel.
          </p>
        </div>
      ) : null}
    </section>
  );
}
