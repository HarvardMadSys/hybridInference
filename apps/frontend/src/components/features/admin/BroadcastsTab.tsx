'use client';

import { Fragment, useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  BroadcastDetailResponse,
  BroadcastListItem,
  cancelBroadcast,
  createBroadcast,
  getBroadcastDetail,
  listBroadcasts,
  previewBroadcast,
  sendTestBroadcastEmail,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

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

export function BroadcastsTab() {
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
  const [bcPreview, setBcPreview] = useState<{
    recipient_count: number;
    rendered_subject: string;
    rendered_body_html: string;
  } | null>(null);
  const [bcPreviewLoading, setBcPreviewLoading] = useState(false);
  const [bcSending, setBcSending] = useState(false);
  const [bcConfirm, setBcConfirm] = useState(false);
  const [bcTestLoading, setBcTestLoading] = useState(false);
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
                setBcPreview(null);
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
                <label className="block text-[12px] font-medium text-gray-600 mb-1">Subject</label>
                <input
                  className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                  placeholder="Email subject"
                  value={bcSubject}
                  onChange={(e) => setBcSubject(e.target.value)}
                />
              </div>
              <div>
                <label className="block text-[12px] font-medium text-gray-600 mb-1">
                  Body (Markdown)
                </label>
                <textarea
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
            <label className="block text-[12px] font-medium text-gray-600 mb-1">
              Minimum spend today (USD)
            </label>
            <input
              type="number"
              min="0"
              step="0.01"
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
              placeholder="e.g. 5 — only users who spent more than $5 today"
              value={bcMinSpendToday}
              onChange={(e) => {
                setBcMinSpendToday(e.target.value);
                setBcPreview(null);
              }}
            />
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
              <input
                type="datetime-local"
                className="mt-2 rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                value={bcScheduledAt}
                onChange={(e) => setBcScheduledAt(e.target.value)}
              />
            )}
          </div>

          {/* Action buttons */}
          <div className="flex flex-wrap items-center gap-3">
            <button
              disabled={bcPreviewLoading}
              onClick={async () => {
                setBcPreviewLoading(true);
                try {
                  const res = await previewBroadcast({
                    template_key: bcTemplateKey === 'custom' ? null : bcTemplateKey,
                    template_vars: bcTemplateVars,
                    subject: bcSubject,
                    body_markdown: bcBodyMarkdown,
                    body_text: '',
                    target_roles: bcTargetRoles,
                    target_statuses: bcTargetStatuses,
                    min_spend_today_usd: parsedMinSpend,
                  });
                  setBcPreview(res);
                } catch (err) {
                  toast.error(getErrorMessage(err));
                } finally {
                  setBcPreviewLoading(false);
                }
              }}
              className="rounded-md border border-gray-300 px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
            >
              {bcPreviewLoading ? 'Loading…' : 'Preview & Count'}
            </button>

            <button
              disabled={bcTestLoading}
              onClick={async () => {
                setBcTestLoading(true);
                try {
                  await sendTestBroadcastEmail({
                    template_key: bcTemplateKey === 'custom' ? null : bcTemplateKey,
                    template_vars: bcTemplateVars,
                    subject: bcSubject,
                    body_markdown: bcBodyMarkdown,
                    body_text: '',
                    target_roles: bcTargetRoles,
                    target_statuses: bcTargetStatuses,
                    min_spend_today_usd: parsedMinSpend,
                  });
                  toast.success('Test email sent to your address');
                } catch (err) {
                  toast.error(getErrorMessage(err));
                } finally {
                  setBcTestLoading(false);
                }
              }}
              className="rounded-md border border-gray-300 px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
            >
              {bcTestLoading ? 'Sending…' : 'Send Test to Me'}
            </button>

            <button
              onClick={() => setBcConfirm(true)}
              disabled={bcSending}
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-40"
            >
              {bcScheduleMode === 'later' ? 'Schedule' : 'Send Now'}
            </button>
          </div>

          {/* Preview panel */}
          {bcPreview && (
            <div className="mt-4 rounded-lg border border-blue-100 bg-blue-50 p-4">
              <div className="text-[13px] font-medium text-blue-800 mb-1">
                {bcPreview.recipient_count} recipient
                {bcPreview.recipient_count !== 1 ? 's' : ''} match your filters
              </div>
              <div className="text-[12px] text-blue-700">Subject: {bcPreview.rendered_subject}</div>
            </div>
          )}
        </div>

        {/* Confirmation modal */}
        {bcConfirm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
            <div className="w-full max-w-sm rounded-xl bg-white p-6 shadow-xl">
              <h3 className="text-[15px] font-semibold text-gray-900 mb-2">Confirm Broadcast</h3>
              <p className="text-[13px] text-gray-600 mb-1">
                {bcPreview
                  ? `This will send to ${bcPreview.recipient_count} recipient(s).`
                  : 'Send broadcast email?'}
              </p>
              {bcScheduleMode === 'later' && bcScheduledAt && (
                <p className="text-[12px] text-gray-500 mb-4">
                  Scheduled for: {new Date(bcScheduledAt).toLocaleString()}
                </p>
              )}
              <div className="flex justify-end gap-3 mt-4">
                <button
                  onClick={() => setBcConfirm(false)}
                  className="rounded-md border border-gray-200 px-4 py-2 text-[13px] text-gray-700 hover:bg-gray-50"
                >
                  Cancel
                </button>
                <button
                  disabled={bcSending}
                  onClick={async () => {
                    setBcSending(true);
                    setBcConfirm(false);
                    try {
                      await createBroadcast({
                        template_key: bcTemplateKey === 'custom' ? null : bcTemplateKey,
                        template_vars: bcTemplateVars,
                        subject: bcSubject,
                        body_markdown: bcBodyMarkdown,
                        body_text: '',
                        target_roles: bcTargetRoles,
                        target_statuses: bcTargetStatuses,
                        min_spend_today_usd: parsedMinSpend,
                        scheduled_at:
                          bcScheduleMode === 'later' && bcScheduledAt
                            ? new Date(bcScheduledAt).toISOString()
                            : null,
                      });
                      toast.success(
                        bcScheduleMode === 'later' ? 'Broadcast scheduled' : 'Broadcast queued',
                      );
                      await loadBroadcasts();
                    } catch (err) {
                      toast.error(getErrorMessage(err));
                    } finally {
                      setBcSending(false);
                    }
                  }}
                  className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-40"
                >
                  Confirm
                </button>
              </div>
            </div>
          </div>
        )}

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
                            onClick={async (e) => {
                              e.stopPropagation();
                              if (!confirm('Cancel this scheduled broadcast?')) return;
                              try {
                                await cancelBroadcast(bc.id);
                                toast.success('Broadcast cancelled');
                                await loadBroadcasts();
                              } catch (err) {
                                toast.error(getErrorMessage(err));
                              }
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
                                        <td className="pr-4 py-1 text-gray-700">{r.email}</td>
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
    </>
  );
}
