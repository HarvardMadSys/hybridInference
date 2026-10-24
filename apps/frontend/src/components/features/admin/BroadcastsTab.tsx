'use client';

import { Fragment, useCallback, useEffect, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ChangeList, plural } from '@/components/features/admin/confirm/ChangeList';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { useAuth } from '@/components/providers';
import {
  BroadcastDetailResponse,
  BroadcastListItem,
  type BroadcastPreviewRequest,
  type BroadcastPreviewResponse,
  cancelBroadcast,
  createBroadcast,
  getBroadcastDetail,
  listBroadcasts,
  previewBroadcast,
  sendTestBroadcastEmail,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { userAccountLabel } from '@/lib/utils/userLabel';

function relTime(s: string | null): string {
  if (!s) return 'Never';
  const ms = Date.now() - new Date(s).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/** A recipient preview, with the request it was computed for. */
type CountedPreview = BroadcastPreviewResponse & { key: string };

export function BroadcastsTab() {
  const toast = useAccountToast();
  const confirmation = useConfirmAction();
  const { state: authState } = useAuth();
  const adminEmail = authState.user?.email ?? null;
  // Broadcast email state
  const [broadcasts, setBroadcasts] = useState<BroadcastListItem[]>([]);
  const [broadcastLoading, setBroadcastLoading] = useState(false);
  const [broadcastDetail, setBroadcastDetail] = useState<BroadcastDetailResponse | null>(null);
  const [broadcastDetailLoading, setBroadcastDetailLoading] = useState(false);
  const [bcTemplateKey, setBcTemplateKey] = useState<string>('custom');
  const [bcTemplateVars, setBcTemplateVars] = useState<Record<string, string>>({});
  const [bcSubject, setBcSubject] = useState('');
  const [bcBodyMarkdown, setBcBodyMarkdown] = useState('');
  const [bcScheduleMode, setBcScheduleMode] = useState<'now' | 'later'>('now');
  const [bcScheduledAt, setBcScheduledAt] = useState('');
  const [bcPreview, setBcPreview] = useState<CountedPreview | null>(null);
  const [bcPreviewLoading, setBcPreviewLoading] = useState(false);
  const [bcTargetRoles, setBcTargetRoles] = useState<string[]>(['free', 'internal', 'admin']);
  const [bcTargetStatuses, setBcTargetStatuses] = useState<string[]>(['active']);
  // Optional spend gate: only email users who have spent more than this many
  // USD today (UTC). Empty string means no spend filter.
  const [bcMinSpendToday, setBcMinSpendToday] = useState('');

  // Parsed spend threshold sent to the API: null when blank/invalid (no filter).
  const parsedMinSpend = (() => {
    const trimmed = bcMinSpendToday.trim();
    if (trimmed === '') return null;
    const n = Number(trimmed);
    return Number.isFinite(n) && n >= 0 ? n : null;
  })();
  // A non-empty value that fails to parse is invalid. We surface this and block
  // sending rather than silently dropping the filter — a typo'd threshold must
  // never quietly become "email everyone".
  const isSpendInvalid = bcMinSpendToday.trim() !== '' && parsedMinSpend === null;
  // "Schedule for later" without a valid time would be sent immediately.
  const scheduledAtMs = bcScheduleMode === 'later' ? new Date(bcScheduledAt).getTime() : null;
  const isScheduleMissing = scheduledAtMs !== null && Number.isNaN(scheduledAtMs);

  const previewRequest: BroadcastPreviewRequest = {
    template_key: bcTemplateKey === 'custom' ? null : bcTemplateKey,
    template_vars: bcTemplateVars,
    subject: bcSubject,
    body_markdown: bcBodyMarkdown,
    body_text: '',
    target_roles: bcTargetRoles,
    target_statuses: bcTargetStatuses,
    min_spend_today_usd: parsedMinSpend,
  };
  const previewKey = JSON.stringify(previewRequest);
  // A count is shown and confirmed only for the content and filters it was computed for.
  const freshPreview = bcPreview?.key === previewKey ? bcPreview : null;
  const busy = confirmation.busy;

  const loadBroadcasts = useCallback(async () => {
    setBroadcastLoading(true);
    try {
      const res = await listBroadcasts(50, 0);
      setBroadcasts(res.broadcasts);
    } catch {
      // non-fatal
    } finally {
      setBroadcastLoading(false);
    }
  }, []);

  useEffect(() => {
    loadBroadcasts();
  }, [loadBroadcasts]);

  async function countRecipients(
    request: BroadcastPreviewRequest,
    key: string,
  ): Promise<CountedPreview | null> {
    setBcPreviewLoading(true);
    try {
      const res = await previewBroadcast(request);
      const counted = { ...res, key };
      setBcPreview(counted);
      return counted;
    } catch (err) {
      toast.error(getErrorMessage(err));
      return null;
    } finally {
      setBcPreviewLoading(false);
    }
  }

  function audienceRows(request: BroadcastPreviewRequest) {
    return [
      { label: 'Roles', after: request.target_roles.join(', ') || '(none)' },
      {
        label: 'Statuses',
        after: request.target_statuses.map((s) => s.replace('_', ' ')).join(', ') || '(none)',
      },
      ...(request.min_spend_today_usd !== null && request.min_spend_today_usd !== undefined
        ? [{ label: 'Spend today (UTC)', after: `More than $${request.min_spend_today_usd}` }]
        : []),
    ];
  }

  /** Send now or schedule: confirmed with the recipient count and the delivery time. */
  async function reviewSend() {
    if (busy || isSpendInvalid || isScheduleMissing) return;
    const request = previewRequest;
    const scheduledAt = scheduledAtMs === null ? null : new Date(scheduledAtMs).toISOString();
    const preview = freshPreview ?? (await countRecipients(request, previewKey));
    if (!preview) return;
    const count = preview.recipient_count;
    const later = scheduledAt !== null;
    confirmation.request({
      title: later
        ? `Schedule this broadcast for ${plural(count, 'recipient')}?`
        : `Send this broadcast to ${plural(count, 'recipient')} now?`,
      confirmLabel: later ? 'Schedule' : `Send to ${plural(count, 'recipient')}`,
      busyLabel: later ? 'Scheduling…' : 'Sending…',
      body: (
        <>
          <ChangeList
            changes={[
              { label: 'Recipients', after: `${count.toLocaleString()} matching right now` },
              { label: 'Subject', after: preview.rendered_subject || '(empty)' },
              ...audienceRows(request),
              {
                label: 'Delivery',
                after: later ? new Date(scheduledAt).toLocaleString() : 'Immediately',
              },
            ]}
          />
          <p>
            {later
              ? 'The recipient list is fixed when you confirm. Until it is sent, the broadcast can be cancelled from Send History.'
              : 'The emails go out as soon as you confirm and cannot be recalled.'}
          </p>
        </>
      ),
      run: async () => {
        await createBroadcast({ ...request, scheduled_at: scheduledAt });
        toast.success(later ? 'Broadcast scheduled' : 'Broadcast queued');
        await loadBroadcasts();
      },
    });
  }

  function reviewTestSend() {
    if (busy || isSpendInvalid) return;
    const request = previewRequest;
    confirmation.request({
      title: adminEmail
        ? `Send a test email to ${adminEmail}?`
        : 'Send a test email to your account?',
      confirmLabel: 'Send test',
      busyLabel: 'Sending…',
      body: (
        <p className="[overflow-wrap:anywhere]">
          {adminEmail
            ? `Only ${adminEmail} receives it, with [TEST] before the subject. Nobody in the audience is emailed.`
            : 'Your account has no email address, so the server has nowhere to send a test.'}
        </p>
      ),
      run: async () => {
        await sendTestBroadcastEmail(request);
        toast.success('Test email sent to your address');
      },
    });
  }

  function confirmCancel(bc: BroadcastListItem) {
    confirmation.request({
      title: `Cancel the scheduled broadcast “${bc.subject}”?`,
      confirmLabel: 'Cancel broadcast',
      busyLabel: 'Cancelling…',
      cancelLabel: 'Keep scheduled',
      tone: 'danger',
      body: (
        <p>
          {`It will not be sent to its ${plural(bc.recipient_count, 'recipient')}${
            bc.scheduled_at ? ` at ${new Date(bc.scheduled_at).toLocaleString()}` : ''
          }. This cannot be undone.`}
        </p>
      ),
      run: async () => {
        await cancelBroadcast(bc.id);
        toast.success('Broadcast cancelled');
        await loadBroadcasts();
      },
    });
  }

  return (
    <>
      <div className="mt-8 space-y-6">
        {/* Composer */}
        <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
          <h2 className="text-[15px] font-semibold text-gray-900 mb-4">Compose Broadcast</h2>

          {/* Template selector */}
          <div className="mb-4">
            <label className="block text-[12px] font-medium text-gray-600 mb-1">Template</label>
            <select
              value={bcTemplateKey}
              onChange={(e) => {
                setBcTemplateKey(e.target.value);
                setBcTemplateVars({});
                setBcSubject('');
                setBcBodyMarkdown('');
              }}
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
            >
              <option value="custom">Custom</option>
              <option value="maintenance">Maintenance Notice</option>
              <option value="announcement">Announcement</option>
              <option value="quota_change">Quota Change</option>
            </select>
          </div>

          {/* Template variable fields */}
          {bcTemplateKey === 'maintenance' && (
            <div className="mb-4 grid grid-cols-2 gap-3">
              <div>
                <label className="block text-[12px] font-medium text-gray-600 mb-1">Date</label>
                <input
                  className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                  placeholder="e.g. May 1, 2026"
                  value={bcTemplateVars['date'] ?? ''}
                  onChange={(e) => setBcTemplateVars((v) => ({ ...v, date: e.target.value }))}
                />
              </div>
              <div>
                <label className="block text-[12px] font-medium text-gray-600 mb-1">Duration</label>
                <input
                  className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                  placeholder="e.g. 2 hours"
                  value={bcTemplateVars['duration'] ?? ''}
                  onChange={(e) => setBcTemplateVars((v) => ({ ...v, duration: e.target.value }))}
                />
              </div>
            </div>
          )}
          {bcTemplateKey === 'announcement' && (
            <div className="mb-4 space-y-3">
              <div>
                <label className="block text-[12px] font-medium text-gray-600 mb-1">
                  Feature Name
                </label>
                <input
                  className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                  placeholder="e.g. GPT-5 Support"
                  value={bcTemplateVars['feature_name'] ?? ''}
                  onChange={(e) =>
                    setBcTemplateVars((v) => ({ ...v, feature_name: e.target.value }))
                  }
                />
              </div>
              <div>
                <label className="block text-[12px] font-medium text-gray-600 mb-1">
                  Description
                </label>
                <textarea
                  rows={3}
                  className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                  placeholder="Describe the new feature..."
                  value={bcTemplateVars['description'] ?? ''}
                  onChange={(e) =>
                    setBcTemplateVars((v) => ({ ...v, description: e.target.value }))
                  }
                />
              </div>
            </div>
          )}
          {bcTemplateKey === 'quota_change' && (
            <div className="mb-4">
              <label className="block text-[12px] font-medium text-gray-600 mb-1">New Quota</label>
              <input
                className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                placeholder="e.g. $50/day"
                value={bcTemplateVars['new_quota'] ?? ''}
                onChange={(e) => setBcTemplateVars((v) => ({ ...v, new_quota: e.target.value }))}
              />
            </div>
          )}
          {bcTemplateKey === 'custom' && (
            <div className="mb-4 space-y-3">
              <div>
                <label
                  htmlFor="broadcast-subject"
                  className="block text-[12px] font-medium text-gray-600 mb-1"
                >
                  Subject
                </label>
                <input
                  id="broadcast-subject"
                  className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                  placeholder="Email subject"
                  value={bcSubject}
                  onChange={(e) => setBcSubject(e.target.value)}
                />
              </div>
              <div>
                <label
                  htmlFor="broadcast-body"
                  className="block text-[12px] font-medium text-gray-600 mb-1"
                >
                  Body (Markdown)
                </label>
                <textarea
                  id="broadcast-body"
                  rows={6}
                  className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] font-mono focus:outline-none focus:ring-2 focus:ring-gray-900"
                  placeholder={'## Heading\n\nYour **message** here. [link](https://...)'}
                  value={bcBodyMarkdown}
                  onChange={(e) => setBcBodyMarkdown(e.target.value)}
                />
              </div>
              {bcBodyMarkdown && (
                <div>
                  <label className="block text-[12px] font-medium text-gray-600 mb-1">
                    Preview
                  </label>
                  <div className="rounded-md border border-gray-200 px-3 py-2 text-[13px] text-gray-700 [&_a]:text-blue-600 [&_a]:underline [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_p]:my-2 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{bcBodyMarkdown}</ReactMarkdown>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Recipient filters */}
          <div className="mb-4 grid grid-cols-2 gap-6">
            <div>
              <label className="block text-[12px] font-medium text-gray-600 mb-2">Roles</label>
              {['free', 'internal', 'admin'].map((role) => (
                <label
                  key={role}
                  className="flex items-center gap-2 text-[13px] text-gray-700 mb-1"
                >
                  <input
                    type="checkbox"
                    checked={bcTargetRoles.includes(role)}
                    onChange={(e) =>
                      setBcTargetRoles((prev) =>
                        e.target.checked ? [...prev, role] : prev.filter((r) => r !== role),
                      )
                    }
                  />
                  {role}
                </label>
              ))}
            </div>
            <div>
              <label className="block text-[12px] font-medium text-gray-600 mb-2">Statuses</label>
              {['active', 'suspended', 'pending_approval', 'rejected'].map((status) => (
                <label
                  key={status}
                  className="flex items-center gap-2 text-[13px] text-gray-700 mb-1"
                >
                  <input
                    type="checkbox"
                    checked={bcTargetStatuses.includes(status)}
                    onChange={(e) =>
                      setBcTargetStatuses((prev) =>
                        e.target.checked ? [...prev, status] : prev.filter((s) => s !== status),
                      )
                    }
                  />
                  {status.replace('_', ' ')}
                </label>
              ))}
            </div>
          </div>

          {/* Spend filter */}
          <div className="mb-4">
            <label
              htmlFor="broadcast-min-spend"
              className="block text-[12px] font-medium text-gray-600 mb-1"
            >
              Minimum spend today (USD)
            </label>
            <input
              id="broadcast-min-spend"
              type="number"
              min="0"
              step="0.01"
              className={`w-full rounded-md border px-3 py-2 text-[13px] focus:outline-none focus:ring-2 ${
                isSpendInvalid
                  ? 'border-red-300 focus:ring-red-500'
                  : 'border-gray-200 focus:ring-gray-900'
              }`}
              placeholder="e.g. 5 — only users who spent more than $5 today"
              value={bcMinSpendToday}
              onChange={(e) => {
                setBcMinSpendToday(e.target.value);
              }}
            />
            {isSpendInvalid && (
              <p className="mt-1 text-[11px] text-red-500">
                Please enter a valid non-negative number.
              </p>
            )}
            <p className="mt-1 text-[11px] text-gray-400">
              Leave blank to email everyone matching the role/status filters. When set, only users
              whose spend today (UTC) is more than this amount are included.
            </p>
          </div>

          {/* Schedule toggle */}
          <div className="mb-4">
            <label className="block text-[12px] font-medium text-gray-600 mb-2">Send Timing</label>
            <div className="flex items-center gap-4">
              <label className="flex items-center gap-2 text-[13px] text-gray-700">
                <input
                  type="radio"
                  checked={bcScheduleMode === 'now'}
                  onChange={() => setBcScheduleMode('now')}
                />
                Send now
              </label>
              <label className="flex items-center gap-2 text-[13px] text-gray-700">
                <input
                  type="radio"
                  checked={bcScheduleMode === 'later'}
                  onChange={() => setBcScheduleMode('later')}
                />
                Schedule for later
              </label>
            </div>
            {bcScheduleMode === 'later' && (
              <>
                <input
                  type="datetime-local"
                  aria-label="Scheduled send time"
                  className="mt-2 rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                  value={bcScheduledAt}
                  onChange={(e) => setBcScheduledAt(e.target.value)}
                />
                {isScheduleMissing && (
                  <p className="mt-1 text-[11px] text-gray-500">
                    Choose a date and time to schedule the broadcast.
                  </p>
                )}
              </>
            )}
          </div>

          {/* Action buttons */}
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={bcPreviewLoading || isSpendInvalid}
              onClick={() => void countRecipients(previewRequest, previewKey)}
              className="rounded-md border border-gray-300 px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
            >
              {bcPreviewLoading ? 'Loading…' : 'Preview & Count'}
            </button>

            <button
              type="button"
              disabled={busy || isSpendInvalid}
              onClick={reviewTestSend}
              className="rounded-md border border-gray-300 px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
            >
              Send Test to Me
            </button>

            <button
              type="button"
              onClick={() => void reviewSend()}
              disabled={busy || bcPreviewLoading || isSpendInvalid || isScheduleMissing}
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-40"
            >
              {bcScheduleMode === 'later' ? 'Schedule' : 'Send Now'}
            </button>
          </div>

          {/* Preview panel */}
          {freshPreview && (
            <div className="mt-4 rounded-lg border border-blue-100 bg-blue-50 p-4">
              <div className="text-[13px] font-medium text-blue-800 mb-1">
                {freshPreview.recipient_count} recipient
                {freshPreview.recipient_count !== 1 ? 's' : ''} match your filters
              </div>
              <div className="text-[12px] text-blue-700">
                Subject: {freshPreview.rendered_subject}
              </div>
            </div>
          )}
        </div>

        {/* History table */}
        <div className="rounded-xl border border-gray-200 bg-white shadow-sm">
          <div className="px-6 py-4 border-b border-gray-100">
            <h2 className="text-[15px] font-semibold text-gray-900">Send History</h2>
          </div>
          {broadcastLoading ? (
            <div className="px-6 py-8 text-[13px] text-gray-400">Loading…</div>
          ) : broadcasts.length === 0 ? (
            <div className="px-6 py-8 text-[13px] text-gray-400">No broadcasts yet.</div>
          ) : (
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b border-gray-100 text-left text-[11px] font-medium text-gray-500">
                  <th className="px-6 py-3">Subject</th>
                  <th className="px-6 py-3">Status</th>
                  <th className="px-6 py-3">Recipients</th>
                  <th className="px-6 py-3">Sent / Scheduled</th>
                  <th className="px-6 py-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {broadcasts.map((bc) => (
                  <Fragment key={bc.id}>
                    <tr
                      className="border-b border-gray-50 hover:bg-gray-50 cursor-pointer"
                      onClick={async () => {
                        if (broadcastDetail?.broadcast.id === bc.id) {
                          setBroadcastDetail(null);
                          return;
                        }
                        setBroadcastDetailLoading(true);
                        try {
                          const detail = await getBroadcastDetail(bc.id);
                          setBroadcastDetail(detail);
                        } catch {
                          /* ignore */
                        } finally {
                          setBroadcastDetailLoading(false);
                        }
                      }}
                    >
                      <td className="px-6 py-3 max-w-[200px] truncate">{bc.subject}</td>
                      <td className="px-6 py-3">
                        <span
                          className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${
                            bc.status === 'sent'
                              ? 'bg-green-50 text-green-700'
                              : bc.status === 'failed'
                                ? 'bg-red-50 text-red-700'
                                : bc.status === 'sending'
                                  ? 'bg-blue-50 text-blue-700'
                                  : bc.status === 'cancelled'
                                    ? 'bg-gray-100 text-gray-500'
                                    : 'bg-yellow-50 text-yellow-700'
                          }`}
                        >
                          {bc.status}
                        </span>
                      </td>
                      <td className="px-6 py-3">{bc.recipient_count.toLocaleString()}</td>
                      <td className="px-6 py-3 text-gray-500">
                        {bc.sent_at
                          ? relTime(bc.sent_at)
                          : bc.scheduled_at
                            ? new Date(bc.scheduled_at).toLocaleString()
                            : '—'}
                      </td>
                      <td className="px-6 py-3">
                        {bc.status === 'scheduled' && (
                          <button
                            type="button"
                            aria-label={`Cancel scheduled broadcast ${bc.subject}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              confirmCancel(bc);
                            }}
                            className="text-red-500 hover:underline text-[12px]"
                          >
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                    {/* Detail drawer */}
                    {broadcastDetail?.broadcast.id === bc.id && (
                      <tr>
                        <td colSpan={5} className="bg-gray-50 px-6 py-4">
                          {broadcastDetailLoading ? (
                            <span className="text-[12px] text-gray-400">Loading recipients…</span>
                          ) : (
                            <>
                              <div className="text-[12px] font-medium text-gray-600 mb-2">
                                Recipients ({broadcastDetail.total_recipients})
                              </div>
                              <div className="overflow-x-auto">
                                <table className="w-full text-[12px]">
                                  <thead>
                                    <tr className="text-left text-[10px] font-medium text-gray-400">
                                      <th className="pr-4 py-1">Email</th>
                                      <th className="pr-4 py-1">Status</th>
                                      <th className="pr-4 py-1">Error</th>
                                      <th className="pr-4 py-1">Sent At</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {broadcastDetail.recipients.map((r) => (
                                      <tr key={r.user_id} className="border-t border-gray-100">
                                        <td className="pr-4 py-1 text-gray-700">
                                          {userAccountLabel(r)}
                                        </td>
                                        <td className="pr-4 py-1">
                                          <span
                                            className={
                                              r.status === 'sent'
                                                ? 'text-green-600'
                                                : r.status === 'failed'
                                                  ? 'text-red-500'
                                                  : 'text-gray-400'
                                            }
                                          >
                                            {r.status}
                                          </span>
                                        </td>
                                        <td className="pr-4 py-1 text-red-400">{r.error ?? '—'}</td>
                                        <td className="pr-4 py-1 text-gray-400">
                                          {r.sent_at ? relTime(r.sent_at) : '—'}
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            </>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
      {confirmation.dialog}
    </>
  );
}
