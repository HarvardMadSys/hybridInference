'use client';

import { useCallback, useEffect, useState } from 'react';

import { AlertSnoozeStatus, clearAlertSnooze, getAlertSnooze, snoozeAlerts } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const DURATIONS: { label: string; seconds: number }[] = [
  { label: '30m', seconds: 30 * 60 },
  { label: '1h', seconds: 60 * 60 },
  { label: '3h', seconds: 3 * 60 * 60 },
  { label: '12h', seconds: 12 * 60 * 60 },
  { label: '24h', seconds: 24 * 60 * 60 },
];

function formatRemaining(seconds: number): string {
  if (seconds <= 0) return '0m';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  if (m > 0) return `${m}m`;
  return `${seconds}s`;
}

function formatUntil(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

interface AlertSnoozeSectionProps {
  onToast: (msg: string) => void;
}

export function AlertSnoozeSection({ onToast }: AlertSnoozeSectionProps) {
  const [status, setStatus] = useState<AlertSnoozeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await getAlertSnooze());
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Tick the remaining countdown down once a minute so the status stays fresh,
  // and reload from the server when the snooze elapses.
  useEffect(() => {
    if (!status?.snoozed) return;
    const id = setInterval(() => {
      setStatus((prev) => {
        if (!prev || !prev.snoozed) return prev;
        const remaining = prev.seconds_remaining - 60;
        if (remaining <= 0) {
          load();
          return prev;
        }
        return { ...prev, seconds_remaining: remaining };
      });
    }, 60_000);
    return () => clearInterval(id);
  }, [status?.snoozed, load]);

  const onSnooze = async (seconds: number, label: string) => {
    setBusy(true);
    try {
      setStatus(await snoozeAlerts(seconds));
      onToast(`Slack alerts snoozed for ${label}`);
    } catch (e) {
      onToast(`Failed to snooze alerts: ${getErrorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const onResume = async () => {
    setBusy(true);
    try {
      setStatus(await clearAlertSnooze());
      onToast('Slack alerts resumed');
    } catch (e) {
      onToast(`Failed to resume alerts: ${getErrorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const snoozed = !!status?.snoozed;

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <div className="mb-3">
        <h2 className="text-[14px] font-semibold text-gray-900">Slack Alerts</h2>
        <p className="mt-1 text-[12px] text-gray-500">
          Temporarily mute all outgoing Slack alerts (e.g. during maintenance or a known incident).
          Alerting resumes automatically when the snooze elapses.
        </p>
      </div>

      {error && (
        <div className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600">
          {error}{' '}
          <button onClick={() => setError(null)} className="ml-2 font-bold">
            &times;
          </button>
        </div>
      )}

      {loading ? (
        <div className="py-8 text-center text-[13px] text-gray-400">Loading...</div>
      ) : (
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <span
              className={`inline-block h-2.5 w-2.5 rounded-full ${
                snoozed ? 'bg-amber-500' : 'bg-emerald-500'
              }`}
            />
            {snoozed && status?.snooze_until ? (
              <span className="text-[13px] text-gray-900">
                Snoozed — {formatRemaining(status.seconds_remaining)} left (until{' '}
                {formatUntil(status.snooze_until)})
              </span>
            ) : (
              <span className="text-[13px] text-gray-900">Active — alerts are being sent</span>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[12px] text-gray-500">
              {snoozed ? 'Extend snooze:' : 'Snooze for:'}
            </span>
            {DURATIONS.map((d) => (
              <button
                key={d.label}
                type="button"
                disabled={busy}
                onClick={() => onSnooze(d.seconds, d.label)}
                className="rounded-md border border-gray-300 bg-white px-3 py-1 text-[12px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-40"
              >
                {d.label}
              </button>
            ))}
            {snoozed && (
              <button
                type="button"
                disabled={busy}
                onClick={onResume}
                className="rounded-md bg-gray-900 px-3 py-1 text-[12px] font-medium text-white transition hover:bg-gray-700 disabled:opacity-40"
              >
                Resume now
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
