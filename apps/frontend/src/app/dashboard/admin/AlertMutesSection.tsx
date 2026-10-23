'use client';

import { Fragment, useCallback, useEffect, useRef, useState } from 'react';

import { AlertTypeMute, listAlertMutes, muteAlertType, unmuteAlertType } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_PRIMARY,
  CONTROL,
  NOTICE_ACTION,
  PILL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';

/** `seconds: null` mutes the type until an admin lifts it. */
const MUTE_OPTIONS: { value: string; label: string; seconds: number | null }[] = [
  { value: '1h', label: '1 hour', seconds: 60 * 60 },
  { value: '24h', label: '24 hours', seconds: 24 * 60 * 60 },
  { value: '7d', label: '7 days', seconds: 7 * 24 * 60 * 60 },
  { value: 'forever', label: 'Until unmuted', seconds: null },
];

// A mute can run for 30 days, but setTimeout fires at once for any delay past
// 2^31-1 ms (~24.8 days), so the check for a lapsed mute waits in bounded steps.
const MAX_LAPSE_CHECK_MS = 6 * 60 * 60 * 1000;

function formatUntil(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function statusText(type: AlertTypeMute): string {
  if (!type.muted) return 'Sending';
  if (type.muted_until === null) return 'Muted until unmuted';
  return `Muted until ${formatUntil(type.muted_until)}`;
}

/** Group rows under their section, keeping the server's catalog order. */
function groupTypes(types: AlertTypeMute[]): [string, AlertTypeMute[]][] {
  const groups = new Map<string, AlertTypeMute[]>();
  for (const type of types) {
    const members = groups.get(type.group) ?? [];
    members.push(type);
    groups.set(type.group, members);
  }
  return [...groups.entries()];
}

interface AlertMutesSectionProps {
  onToast: (msg: string) => void;
}

export function AlertMutesSection({ onToast }: AlertMutesSectionProps) {
  const [types, setTypes] = useState<AlertTypeMute[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<Set<string>>(new Set());

  // Same guard as AlertSnoozeSection: a reload still in flight when the
  // section unmounts must not land its setState on a torn-down tree.
  const mounted = useRef(true);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    setError(null);
    try {
      const response = await listAlertMutes();
      if (!mounted.current) return;
      setTypes(response.types);
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

  // Reload quietly once the soonest timed mute lapses, so a mute that has run
  // out stops reading as muted. Keyed on `types` rather than on the deadline,
  // so a bounded wait that ends before the deadline arms the next one.
  useEffect(() => {
    const deadlines = types
      .filter((type) => type.muted && type.muted_until !== null)
      .map((type) => type.muted_until as number);
    if (deadlines.length === 0) return;
    const untilLapse = Math.min(...deadlines) * 1000 - Date.now();
    const delay = Math.min(Math.max(untilLapse, 0) + 2_000, MAX_LAPSE_CHECK_MS);
    const id = setTimeout(() => void load(true), delay);
    return () => clearTimeout(id);
  }, [types, load]);

  const setRowSaving = (alertType: string, isSaving: boolean) => {
    setSaving((prev) => {
      const next = new Set(prev);
      if (isSaving) next.add(alertType);
      else next.delete(alertType);
      return next;
    });
  };

  const replaceRow = (updated: AlertTypeMute) => {
    setTypes((prev) =>
      prev.map((type) => (type.alert_type === updated.alert_type ? updated : type)),
    );
  };

  const onMute = async (type: AlertTypeMute, optionValue: string) => {
    const option = MUTE_OPTIONS.find((o) => o.value === optionValue);
    if (!option) return;
    setRowSaving(type.alert_type, true);
    try {
      const updated = await muteAlertType(type.alert_type, option.seconds);
      if (!mounted.current) return;
      replaceRow(updated);
      onToast(
        option.seconds === null
          ? `Muted "${type.label}" until unmuted`
          : `Muted "${type.label}" for ${option.label}`,
      );
    } catch (e) {
      onToast(`Failed to mute "${type.label}": ${getErrorMessage(e)}`);
    } finally {
      if (mounted.current) setRowSaving(type.alert_type, false);
    }
  };

  const onUnmute = async (type: AlertTypeMute) => {
    setRowSaving(type.alert_type, true);
    try {
      const updated = await unmuteAlertType(type.alert_type);
      if (!mounted.current) return;
      replaceRow(updated);
      onToast(`Unmuted "${type.label}"`);
    } catch (e) {
      onToast(`Failed to unmute "${type.label}": ${getErrorMessage(e)}`);
    } finally {
      if (mounted.current) setRowSaving(type.alert_type, false);
    }
  };

  const mutedCount = types.filter((type) => type.muted).length;

  return (
    <AdminSection
      title="Alert Types"
      badge={
        mutedCount > 0 && (
          <span className={`${PILL} bg-amber-50 text-amber-700`}>{mutedCount} muted</span>
        )
      }
      description="Mute one kind of backend alert without silencing the rest. A muted type sends nothing, and an incident that opens while it is muted also closes without a recovery message. An incident that was already posted still gets its recovery."
    >
      {error && (
        <div className="mb-3">
          <AdminNotice
            action={
              <button type="button" onClick={() => void load()} className={NOTICE_ACTION}>
                Retry
              </button>
            }
          >
            {error}
          </AdminNotice>
        </div>
      )}

      {loading ? (
        <AdminLoading size="sm" />
      ) : types.length === 0 ? (
        !error && <AdminEmptyState title="No alert types available." />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-gray-200">
          <table className="min-w-full text-[13px]">
            <thead className="bg-gray-50/80">
              <tr className="border-b border-gray-200">
                <th className={TABLE_HEAD_CELL}>Alert</th>
                <th className={TABLE_HEAD_CELL}>Status</th>
                <th className={`${TABLE_HEAD_CELL} text-right`}>Mute</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {groupTypes(types).map(([group, members]) => (
                <Fragment key={group}>
                  <tr className="bg-gray-50">
                    <th
                      colSpan={3}
                      scope="colgroup"
                      className="px-4 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-gray-500"
                    >
                      {group}
                    </th>
                  </tr>
                  {members.map((type) => {
                    const isSaving = saving.has(type.alert_type);
                    return (
                      <tr key={type.alert_type} className="bg-white align-top">
                        <td className="px-4 py-2.5">
                          <div className="font-medium text-gray-900">{type.label}</div>
                          <div className="mt-0.5 text-[12px] text-gray-500">{type.description}</div>
                          <code className="mt-0.5 inline-block text-[11px] text-gray-400">
                            {type.key_pattern}
                          </code>
                        </td>
                        <td className="whitespace-nowrap px-4 py-2.5">
                          <span className="inline-flex items-center gap-2">
                            <span
                              className={`inline-block h-2 w-2 rounded-full ${
                                type.muted ? 'bg-amber-500' : 'bg-emerald-500'
                              }`}
                            />
                            <span className="text-gray-900">{statusText(type)}</span>
                          </span>
                          {type.muted && type.muted_by && (
                            <div className="mt-0.5 text-[11px] text-gray-400">
                              by {type.muted_by}
                            </div>
                          )}
                        </td>
                        <td className="whitespace-nowrap px-4 py-2.5 text-right">
                          <div className="inline-flex items-center gap-2">
                            <select
                              aria-label={`Mute ${type.label}`}
                              value=""
                              disabled={isSaving}
                              onChange={(e) => void onMute(type, e.target.value)}
                              className={CONTROL}
                            >
                              <option value="" disabled>
                                {type.muted ? 'Change…' : 'Mute for…'}
                              </option>
                              {MUTE_OPTIONS.map((option) => (
                                <option key={option.value} value={option.value}>
                                  {option.label}
                                </option>
                              ))}
                            </select>
                            {type.muted && (
                              <button
                                type="button"
                                disabled={isSaving}
                                onClick={() => void onUnmute(type)}
                                className={BUTTON_PRIMARY}
                              >
                                Unmute
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </AdminSection>
  );
}
