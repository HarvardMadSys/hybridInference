'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { AlertSnoozeStatus, clearAlertSnooze, getAlertSnooze, snoozeAlerts } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import { BUTTON_PRIMARY, BUTTON_SECONDARY, PANEL } from '@/components/features/admin/adminStyles';

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
  const confirmation = useConfirmAction();
  const busy = confirmation.busy;

  // A reload in flight when this section goes away — a tab switch, or a test
  // environment tearing down — would otherwise land its setState on an
  // unmounted tree and throw where nothing can catch it.
  const mounted = useRef(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await getAlertSnooze();
      if (!mounted.current) return;
      setStatus(next);
    } catch (e) {
      if (!mounted.current) return;
      setError(getErrorMessage(e));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  // Recompute the remaining countdown from the absolute deadline so it stays
  // accurate across timer drift and background-tab throttling, and reload from
  // the server once the snooze elapses.
  useEffect(() => {
    if (!status?.snoozed || !status.snooze_until) return;
    const until = status.snooze_until;
    const id = setInterval(() => {
      const remaining = Math.max(0, Math.round(until - Date.now() / 1000));
      if (remaining <= 0) {
        load();
      } else {
        setStatus((prev) => (prev ? { ...prev, seconds_remaining: remaining } : null));
      }
    }, 10_000);
    return () => clearInterval(id);
  }, [status?.snoozed, status?.snooze_until, load]);

  // Both are discrete actions: each asks once, naming what stops or starts.
  const onSnooze = (seconds: number, label: string) => {
    const extending = Boolean(status?.snoozed);
    confirmation.request({
      title: extending
        ? `Extend the alert snooze to ${label} from now?`
        : `Snooze alert notifications for ${label}?`,
      confirmLabel: extending ? 'Extend snooze' : 'Snooze',
      busyLabel: 'Snoozing…',
      body: (
        <p>
          No backend alert is sent until {formatUntil(Math.round(Date.now() / 1000) + seconds)},
          including alerts for new incidents. Alerting resumes by itself then.
        </p>
      ),
      run: async () => {
        setStatus(await snoozeAlerts(seconds));
        onToast(`Alert notifications snoozed for ${label}`);
      },
    });
  };

  const onResume = () => {
    confirmation.request({
      title: 'Resume alert notifications now?',
      confirmLabel: 'Resume now',
      busyLabel: 'Resuming…',
      body: <p>The snooze ends at once, and the next backend alert is sent as usual.</p>,
      run: async () => {
        setStatus(await clearAlertSnooze());
        onToast('Alert notifications resumed');
      },
    });
  };

  const snoozed = !!status?.snoozed;

  return (
    <AdminSection
      title="Alert Notifications"
      description="Temporarily mute all outgoing backend alert notifications (e.g. during maintenance or a known incident). Alerting resumes automatically when the snooze elapses."
    >
      {error && (
        <div className="mb-3">
          <AdminNotice onDismiss={() => setError(null)}>{error}</AdminNotice>
        </div>
      )}

      {loading ? (
        <AdminLoading size="sm" />
      ) : (
        <div className={`${PANEL} space-y-3 px-4 py-3`}>
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
                className={BUTTON_SECONDARY}
              >
                {d.label}
              </button>
            ))}
            {snoozed && (
              <button type="button" disabled={busy} onClick={onResume} className={BUTTON_PRIMARY}>
                Resume now
              </button>
            )}
          </div>
        </div>
      )}
      {confirmation.dialog}
    </AdminSection>
  );
}
