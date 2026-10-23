'use client';

import { Fragment, useCallback, useEffect, useId, useRef, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
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
import { userAccountLabel } from '@/lib/utils/userLabel';
import { AdminEmptyState, AdminLoading, AdminSection, AdminSpinner } from './AdminPrimitives';
import {
  BUTTON_DANGER,
  BUTTON_GHOST,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CHECKBOX,
  CONTROL,
  FIELD_HINT,
  FIELD_INVALID,
  FIELD_LABEL,
  FORM_LABEL,
  PANEL,
  PILL,
  ROW_ACTION,
  TABLE_CELL,
  TABLE_HEAD_CELL,
  TEXT_FIELD,
} from './adminStyles';

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
  const toast = useAccountToast();
  const baseId = useId();
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
  const [composerOpen, setComposerOpen] = useState(false);
  // The opener disappears while the composer is open: focus moves to its
  // first field on open, and back to the opener when it is hidden.
  const openerRef = useRef<HTMLButtonElement>(null);
  const focusOnToggle = useRef(false);

  useEffect(() => {
    if (!focusOnToggle.current) return;
    focusOnToggle.current = false;
    if (composerOpen) {
      // The subject, or the template picker when a template writes it.
      const first = `${baseId}-${bcTemplateKey === 'custom' ? 'subject' : 'template'}`;
      document.getElementById(first)?.focus();
    } else {
      openerRef.current?.focus();
    }
    // Only the open/close transition moves focus.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composerOpen]);

  const toggleComposer = (open: boolean) => {
    focusOnToggle.current = true;
    setComposerOpen(open);
  };
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
  // A non-empty value that fails to parse is invalid. We surface this and block
  // sending rather than silently dropping the filter — a typo'd threshold must
  // never quietly become "email everyone".
  const isSpendInvalid = bcMinSpendToday.trim() !== '' && parsedMinSpend === null;

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

  const fieldId = (name: string) => `${baseId}-${name}`;

  const toggleDetail = async (id: string) => {
    if (broadcastDetail?.broadcast.id === id) {
      setBroadcastDetail(null);
      return;
    }
    setBroadcastDetailLoading(true);
    try {
      const detail = await getBroadcastDetail(id);
      setBroadcastDetail(detail);
    } catch {
      /* ignore */
    } finally {
      setBroadcastDetailLoading(false);
    }
  };

  const hasDraft =
    bcTemplateKey !== 'custom' ||
    bcSubject !== '' ||
    bcBodyMarkdown !== '' ||
    Object.values(bcTemplateVars).some((v) => v !== '');

  return (
    <>
      <AdminSection
        title="Email broadcasts"
        description="Email sent to the users who match a role, status and spend filter, now or at a scheduled time."
        actions={
          <>
            {broadcastLoading && broadcasts.length > 0 ? <AdminSpinner size="sm" /> : null}
            {!composerOpen && (
              <button
                type="button"
                ref={openerRef}
                onClick={() => toggleComposer(true)}
                aria-controls={fieldId('composer')}
                aria-expanded={false}
                className={BUTTON_PRIMARY}
              >
                {hasDraft ? 'Resume draft' : 'Compose email'}
              </button>
            )}
          </>
        }
      >
        {/* Composer: opened on purpose. Hiding it keeps everything typed. */}
        {composerOpen && (
          <div
            id={fieldId('composer')}
            role="group"
            aria-labelledby={fieldId('composer-title')}
            className={`${PANEL} mb-4`}
          >
            <div className="flex items-center justify-between gap-3 border-b border-gray-100 px-4 py-3">
              <h3
                id={fieldId('composer-title')}
                className="text-[13px] font-semibold text-gray-900"
              >
                New broadcast
              </h3>
              <button
                type="button"
                onClick={() => toggleComposer(false)}
                title="Hide the composer. What you entered is kept."
                className={ROW_ACTION}
              >
                Hide
              </button>
            </div>
            {/* What is sent beside who receives it and when, where there is room. */}
            <div className="grid divide-gray-100 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] xl:divide-x">
              <div className="space-y-4 p-4">
                <h3 className={FIELD_LABEL}>Message</h3>

                {/* Template selector */}
                <div>
                  <label htmlFor={fieldId('template')} className={FORM_LABEL}>
                    Template
                  </label>
                  <select
                    id={fieldId('template')}
                    value={bcTemplateKey}
                    onChange={(e) => {
                      setBcTemplateKey(e.target.value);
                      setBcTemplateVars({});
                      setBcSubject('');
                      setBcBodyMarkdown('');
                      setBcPreview(null);
                    }}
                    className={TEXT_FIELD}
                  >
                    <option value="custom">Custom</option>
                    <option value="maintenance">Maintenance Notice</option>
                    <option value="announcement">Announcement</option>
                    <option value="quota_change">Quota Change</option>
                  </select>
                </div>

                {/* Template variable fields */}
                {bcTemplateKey === 'maintenance' && (
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <div>
                      <label htmlFor={fieldId('date')} className={FORM_LABEL}>
                        Date
                      </label>
                      <input
                        id={fieldId('date')}
                        className={TEXT_FIELD}
                        placeholder="e.g. May 1, 2026"
                        value={bcTemplateVars['date'] ?? ''}
                        onChange={(e) => setBcTemplateVars((v) => ({ ...v, date: e.target.value }))}
                      />
                    </div>
                    <div>
                      <label htmlFor={fieldId('duration')} className={FORM_LABEL}>
                        Duration
                      </label>
                      <input
                        id={fieldId('duration')}
                        className={TEXT_FIELD}
                        placeholder="e.g. 2 hours"
                        value={bcTemplateVars['duration'] ?? ''}
                        onChange={(e) =>
                          setBcTemplateVars((v) => ({ ...v, duration: e.target.value }))
                        }
                      />
                    </div>
                  </div>
                )}
                {bcTemplateKey === 'announcement' && (
                  <div className="space-y-3">
                    <div>
                      <label htmlFor={fieldId('feature-name')} className={FORM_LABEL}>
                        Feature Name
                      </label>
                      <input
                        id={fieldId('feature-name')}
                        className={TEXT_FIELD}
                        placeholder="e.g. GPT-5 Support"
                        value={bcTemplateVars['feature_name'] ?? ''}
                        onChange={(e) =>
                          setBcTemplateVars((v) => ({ ...v, feature_name: e.target.value }))
                        }
                      />
                    </div>
                    <div>
                      <label htmlFor={fieldId('description')} className={FORM_LABEL}>
                        Description
                      </label>
                      <textarea
                        id={fieldId('description')}
                        rows={3}
                        className={TEXT_FIELD}
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
                  <div>
                    <label htmlFor={fieldId('new-quota')} className={FORM_LABEL}>
                      New Quota
                    </label>
                    <input
                      id={fieldId('new-quota')}
                      className={TEXT_FIELD}
                      placeholder="e.g. $50/day"
                      value={bcTemplateVars['new_quota'] ?? ''}
                      onChange={(e) =>
                        setBcTemplateVars((v) => ({ ...v, new_quota: e.target.value }))
                      }
                    />
                  </div>
                )}
                {bcTemplateKey === 'custom' && (
                  <div className="space-y-3">
                    <div>
                      <label htmlFor={fieldId('subject')} className={FORM_LABEL}>
                        Subject
                      </label>
                      <input
                        id={fieldId('subject')}
                        className={TEXT_FIELD}
                        placeholder="Email subject"
                        value={bcSubject}
                        onChange={(e) => setBcSubject(e.target.value)}
                      />
                    </div>
                    <div>
                      <label htmlFor={fieldId('body')} className={FORM_LABEL}>
                        Body (Markdown)
                      </label>
                      <textarea
                        id={fieldId('body')}
                        rows={6}
                        className={`${TEXT_FIELD} font-mono`}
                        placeholder={'## Heading\n\nYour **message** here. [link](https://...)'}
                        value={bcBodyMarkdown}
                        onChange={(e) => setBcBodyMarkdown(e.target.value)}
                      />
                    </div>
                    {bcBodyMarkdown && (
                      <div>
                        <div className={FORM_LABEL}>Preview</div>
                        <div className="rounded-md border border-gray-200 bg-gray-50/50 px-3 py-2 text-[13px] text-gray-700 [&_a]:text-blue-600 [&_a]:underline [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_p]:my-2 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {bcBodyMarkdown}
                          </ReactMarkdown>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>

              <div className="space-y-4 border-t border-gray-100 p-4 xl:border-t-0">
                <h3 className={FIELD_LABEL}>Audience &amp; delivery</h3>

                {/* Recipient filters */}
                <div className="grid grid-cols-2 gap-6">
                  <fieldset>
                    <legend className={FORM_LABEL}>Roles</legend>
                    <div className="mt-1 space-y-1">
                      {['free', 'internal', 'admin'].map((role) => (
                        <label
                          key={role}
                          className="flex items-center gap-2 text-[13px] text-gray-700"
                        >
                          <input
                            type="checkbox"
                            className={CHECKBOX}
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
                  </fieldset>
                  <fieldset>
                    <legend className={FORM_LABEL}>Statuses</legend>
                    <div className="mt-1 space-y-1">
                      {['active', 'suspended', 'pending_approval', 'rejected'].map((status) => (
                        <label
                          key={status}
                          className="flex items-center gap-2 text-[13px] text-gray-700"
                        >
                          <input
                            type="checkbox"
                            className={CHECKBOX}
                            checked={bcTargetStatuses.includes(status)}
                            onChange={(e) =>
                              setBcTargetStatuses((prev) =>
                                e.target.checked
                                  ? [...prev, status]
                                  : prev.filter((s) => s !== status),
                              )
                            }
                          />
                          {status.replace('_', ' ')}
                        </label>
                      ))}
                    </div>
                  </fieldset>
                </div>

                {/* Spend filter */}
                <div>
                  <label htmlFor={fieldId('min-spend')} className={FORM_LABEL}>
                    Minimum spend today (USD)
                  </label>
                  <input
                    id={fieldId('min-spend')}
                    type="number"
                    min="0"
                    step="0.01"
                    className={isSpendInvalid ? `${TEXT_FIELD} ${FIELD_INVALID}` : TEXT_FIELD}
                    placeholder="e.g. 5 — only users who spent more than $5 today"
                    value={bcMinSpendToday}
                    aria-invalid={isSpendInvalid || undefined}
                    aria-describedby={`${isSpendInvalid ? `${fieldId('min-spend-error')} ` : ''}${fieldId('min-spend-hint')}`}
                    onChange={(e) => {
                      setBcMinSpendToday(e.target.value);
                      setBcPreview(null);
                    }}
                  />
                  {isSpendInvalid && (
                    <p id={fieldId('min-spend-error')} className="mt-1 text-[11px] text-red-600">
                      Please enter a valid non-negative number.
                    </p>
                  )}
                  <p id={fieldId('min-spend-hint')} className={FIELD_HINT}>
                    Leave blank to email everyone matching the role/status filters. When set, only
                    users whose spend today (UTC) is more than this amount are included.
                  </p>
                </div>

                {/* Schedule toggle */}
                <fieldset>
                  <legend className={FORM_LABEL}>Send Timing</legend>
                  <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1">
                    <label className="flex items-center gap-2 text-[13px] text-gray-700">
                      <input
                        type="radio"
                        name={fieldId('timing')}
                        className={CHECKBOX}
                        checked={bcScheduleMode === 'now'}
                        onChange={() => setBcScheduleMode('now')}
                      />
                      Send now
                    </label>
                    <label className="flex items-center gap-2 text-[13px] text-gray-700">
                      <input
                        type="radio"
                        name={fieldId('timing')}
                        className={CHECKBOX}
                        checked={bcScheduleMode === 'later'}
                        onChange={() => setBcScheduleMode('later')}
                      />
                      Schedule for later
                    </label>
                  </div>
                  {bcScheduleMode === 'later' && (
                    <input
                      type="datetime-local"
                      aria-label="Scheduled send time"
                      className={`${CONTROL} mt-2`}
                      value={bcScheduledAt}
                      onChange={(e) => setBcScheduledAt(e.target.value)}
                    />
                  )}
                </fieldset>
              </div>
            </div>

            {/* Action buttons */}
            <div className="border-t border-gray-100 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={bcPreviewLoading || isSpendInvalid}
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
                  className={BUTTON_SECONDARY}
                >
                  {bcPreviewLoading ? 'Loading…' : 'Preview & Count'}
                </button>

                <button
                  type="button"
                  disabled={bcTestLoading || isSpendInvalid}
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
                  className={BUTTON_SECONDARY}
                >
                  {bcTestLoading ? 'Sending…' : 'Send Test to Me'}
                </button>

                <button
                  type="button"
                  onClick={() => setBcConfirm(true)}
                  disabled={bcSending || isSpendInvalid}
                  className={`${BUTTON_PRIMARY} sm:ml-auto`}
                >
                  {bcScheduleMode === 'later' ? 'Schedule' : 'Send Now'}
                </button>
              </div>

              {/* Preview panel */}
              {bcPreview && (
                <div
                  role="status"
                  className="mt-4 rounded-lg border border-blue-100 bg-blue-50 p-4"
                >
                  <div className="mb-1 text-[13px] font-medium text-blue-800">
                    {bcPreview.recipient_count} recipient
                    {bcPreview.recipient_count !== 1 ? 's' : ''} match your filters
                  </div>
                  <div className="text-[12px] text-blue-700">
                    Subject: {bcPreview.rendered_subject}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* History */}
        <div className={PANEL}>
          {broadcastLoading && broadcasts.length === 0 ? (
            <AdminLoading size="sm" />
          ) : broadcasts.length === 0 ? (
            <AdminEmptyState variant="plain" title="No broadcasts yet." />
          ) : (
            <div className="overflow-x-auto rounded-lg">
              <table className="w-full min-w-[40rem]">
                <thead className="bg-gray-50/80">
                  <tr className="border-b border-gray-200">
                    <th className={TABLE_HEAD_CELL}>Subject</th>
                    <th className={TABLE_HEAD_CELL}>Status</th>
                    <th className={TABLE_HEAD_CELL}>Recipients</th>
                    <th className={TABLE_HEAD_CELL}>Sent / Scheduled</th>
                    <th className={TABLE_HEAD_CELL}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {broadcasts.map((bc) => {
                    const expanded = broadcastDetail?.broadcast.id === bc.id;
                    return (
                      <Fragment key={bc.id}>
                        <tr
                          className="cursor-pointer border-b border-gray-100 transition-colors hover:bg-gray-50"
                          onClick={() => void toggleDetail(bc.id)}
                        >
                          <td className={`${TABLE_CELL} max-w-[28rem]`}>
                            {/* The row toggles on click; this is the same toggle
                                  for the keyboard. */}
                            <button
                              type="button"
                              aria-expanded={expanded}
                              title={bc.subject}
                              onClick={(e) => {
                                e.stopPropagation();
                                void toggleDetail(bc.id);
                              }}
                              className="block max-w-full truncate rounded text-left font-medium text-gray-900 hover:underline"
                            >
                              {bc.subject}
                            </button>
                          </td>
                          <td className={TABLE_CELL}>
                            <span
                              className={`${PILL} ${
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
                          <td className={`${TABLE_CELL} tabular-nums`}>
                            {bc.recipient_count.toLocaleString()}
                          </td>
                          <td className={`${TABLE_CELL} whitespace-nowrap text-gray-500`}>
                            {bc.sent_at
                              ? relTime(bc.sent_at)
                              : bc.scheduled_at
                                ? new Date(bc.scheduled_at).toLocaleString()
                                : '—'}
                          </td>
                          <td className={TABLE_CELL}>
                            {bc.status === 'scheduled' && (
                              <button
                                type="button"
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
                                className={BUTTON_DANGER}
                              >
                                Cancel
                              </button>
                            )}
                          </td>
                        </tr>
                        {/* Detail drawer */}
                        {expanded && (
                          <tr className="border-b border-gray-100">
                            <td colSpan={5} className="bg-gray-50 px-4 py-4">
                              {broadcastDetailLoading ? (
                                <span className="text-[12px] text-gray-400">
                                  Loading recipients…
                                </span>
                              ) : (
                                <>
                                  <div className="mb-2 text-[12px] font-medium text-gray-600">
                                    Recipients ({broadcastDetail.total_recipients})
                                  </div>
                                  <div className="overflow-x-auto">
                                    <table className="w-full text-[12px]">
                                      <thead>
                                        <tr className="text-left text-[11px] font-medium uppercase tracking-wide text-gray-400">
                                          <th className="py-1 pr-4 font-medium">Email</th>
                                          <th className="py-1 pr-4 font-medium">Status</th>
                                          <th className="py-1 pr-4 font-medium">Error</th>
                                          <th className="py-1 pr-4 font-medium">Sent At</th>
                                        </tr>
                                      </thead>
                                      <tbody>
                                        {broadcastDetail.recipients.map((r) => (
                                          <tr key={r.user_id} className="border-t border-gray-100">
                                            <td className="py-1 pr-4 text-gray-700">
                                              {userAccountLabel(r)}
                                            </td>
                                            <td className="py-1 pr-4">
                                              <span
                                                className={
                                                  r.status === 'sent'
                                                    ? 'text-green-600'
                                                    : r.status === 'failed'
                                                      ? 'text-red-600'
                                                      : 'text-gray-400'
                                                }
                                              >
                                                {r.status}
                                              </span>
                                            </td>
                                            <td className="py-1 pr-4 text-red-600">
                                              {r.error ?? '—'}
                                            </td>
                                            <td className="py-1 pr-4 text-gray-400">
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
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </AdminSection>

      {/* Confirmation modal */}
      {bcConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby={fieldId('confirm-title')}
            className="w-full max-w-sm rounded-xl bg-white p-5 shadow-lg"
          >
            <h3
              id={fieldId('confirm-title')}
              className="mb-2 text-[15px] font-semibold text-gray-900"
            >
              Confirm Broadcast
            </h3>
            <p className="mb-1 text-[13px] text-gray-600">
              {bcPreview
                ? `This will send to ${bcPreview.recipient_count} recipient(s).`
                : 'Send broadcast email?'}
            </p>
            {bcScheduleMode === 'later' && bcScheduledAt && (
              <p className="mb-4 text-[12px] text-gray-500">
                Scheduled for: {new Date(bcScheduledAt).toLocaleString()}
              </p>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                autoFocus
                onClick={() => setBcConfirm(false)}
                className={BUTTON_GHOST}
              >
                Cancel
              </button>
              <button
                type="button"
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
                className={BUTTON_PRIMARY}
              >
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
