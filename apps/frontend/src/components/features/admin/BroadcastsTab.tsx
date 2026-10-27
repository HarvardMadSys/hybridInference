'use client';

import { Fragment, useCallback, useEffect, useId, useRef, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { Select } from '@/components/ui/Select';
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
  type Role,
  cancelBroadcast,
  createBroadcast,
  getBroadcastDetail,
  listBroadcasts,
  previewBroadcast,
  sendTestBroadcastEmail,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { userAccountLabel } from '@/lib/utils/userLabel';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
  AdminSpinner,
} from './AdminPrimitives';
import {
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CHECKBOX,
  CONTROL,
  FIELD_HINT,
  FIELD_INVALID,
  FIELD_LABEL,
  FORM_LABEL,
  MARKDOWN_PROSE,
  NOTICE_ACTION,
  PANEL,
  PILL,
  ROW_ACTION,
  ROW_ACTION_DANGER,
  TABLE_HEAD_CELL,
  TEXT_FIELD,
} from './adminStyles';

/** Every account role, lowest first. A new broadcast targets all of them. */
const BROADCAST_ROLES: readonly Role[] = ['free', 'pro', 'internal', 'admin'];

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

/** The `key` of a test send, which names it while it runs. */
const TEST_SEND = 'test send';

/**
 * Email broadcasts. Preview & Count is a direct read, and Send Test to Me sends
 * at once, to the signed-in administrator only. Send Now / Schedule and
 * cancelling a scheduled broadcast each open a confirmation first, and only its
 * Confirm sends or cancels anything.
 */
export function BroadcastsTab() {
  const toast = useAccountToast();
  const confirmation = useConfirmAction();
  const { state: authState } = useAuth();
  const adminEmail = authState.user?.email ?? null;
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
  const [bcPreview, setBcPreview] = useState<CountedPreview | null>(null);
  const [bcPreviewLoading, setBcPreviewLoading] = useState(false);
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
  const [bcTargetRoles, setBcTargetRoles] = useState<string[]>([...BROADCAST_ROLES]);
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
  const testSending = confirmation.savingKey === TEST_SEND;

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

  /**
   * Send now or schedule: confirmed with the recipient count from a preview of
   * exactly this content and audience (counted first when there is none), the
   * subject, the filters and the delivery time.
   */
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
                after: later ? new Date(scheduledAt).toLocaleString('en-US') : 'Immediately',
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

  // A test reaches nobody in the audience, only the signed-in administrator, so
  // it is sent on the click.
  async function sendTest() {
    if (busy || isSpendInvalid) return;
    const request = previewRequest;
    const failure = await confirmation.perform({
      key: TEST_SEND,
      run: async () => {
        await sendTestBroadcastEmail(request);
        toast.success('Test email sent to your address');
      },
    });
    if (failure) toast.error(failure);
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
            bc.scheduled_at ? ` at ${new Date(bc.scheduled_at).toLocaleString('en-US')}` : ''
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

  // The broadcast whose recipients are open. It opens at once (with a loading
  // line) and shows a failure in place; an answer for a broadcast that is no
  // longer open is dropped.
  const [openId, setOpenId] = useState<string | null>(null);
  const openIdRef = useRef<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const loadDetail = async (id: string) => {
    setBroadcastDetailLoading(true);
    setDetailError(null);
    try {
      const detail = await getBroadcastDetail(id);
      if (openIdRef.current === id) setBroadcastDetail(detail);
    } catch (err) {
      if (openIdRef.current === id) setDetailError(getErrorMessage(err));
    } finally {
      if (openIdRef.current === id) setBroadcastDetailLoading(false);
    }
  };

  const toggleDetail = async (id: string) => {
    if (openIdRef.current === id) {
      openIdRef.current = null;
      setOpenId(null);
      setBroadcastDetail(null);
      return;
    }
    openIdRef.current = id;
    setOpenId(id);
    setBroadcastDetail(null);
    await loadDetail(id);
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
            role="group"
            aria-labelledby={fieldId('composer-title')}
            className={`${PANEL} animate-admin-reveal mb-4`}
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
            {/* minmax(0, …) tracks and min-w-0 items at every width, so a wide
                Markdown preview scrolls inside itself instead of widening the
                page. */}
            <div className="grid grid-cols-[minmax(0,1fr)] divide-gray-100 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] xl:divide-x">
              <div className="min-w-0 space-y-4 p-4">
                <h3 className={FIELD_LABEL}>Message</h3>

                {/* Template selector */}
                <div>
                  <label htmlFor={fieldId('template')} className={FORM_LABEL}>
                    Template
                  </label>
                  {/* Switching template clears what was typed, so choosing
                      the current template again must not count as a change
                      (Select reports only a different value). */}
                  <Select
                    id={fieldId('template')}
                    value={bcTemplateKey}
                    onValueChange={(next) => {
                      setBcTemplateKey(next);
                      setBcTemplateVars({});
                      setBcSubject('');
                      setBcBodyMarkdown('');
                      setBcPreview(null);
                    }}
                    options={[
                      { value: 'custom', label: 'Custom' },
                      { value: 'maintenance', label: 'Maintenance Notice' },
                      { value: 'announcement', label: 'Announcement' },
                      { value: 'quota_change', label: 'Quota Change' },
                    ]}
                    className="w-full"
                  />
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
                        <div
                          className={`rounded-md border border-gray-200 bg-gray-50/50 px-3 py-2 ${MARKDOWN_PROSE}`}
                        >
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {bcBodyMarkdown}
                          </ReactMarkdown>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>

              <div className="min-w-0 space-y-4 border-t border-gray-100 p-4 xl:border-t-0">
                <h3 className={FIELD_LABEL}>Audience &amp; delivery</h3>

                {/* Recipient filters */}
                <div className="grid grid-cols-2 gap-6">
                  <fieldset>
                    <legend className={FORM_LABEL}>Roles</legend>
                    <div className="mt-1 space-y-1">
                      {BROADCAST_ROLES.map((role) => (
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
                    <>
                      <input
                        type="datetime-local"
                        aria-label="Scheduled send time"
                        aria-describedby={
                          isScheduleMissing ? fieldId('schedule-missing') : undefined
                        }
                        className={`${CONTROL} mt-2`}
                        value={bcScheduledAt}
                        onChange={(e) => setBcScheduledAt(e.target.value)}
                      />
                      {isScheduleMissing && (
                        <p id={fieldId('schedule-missing')} className={FIELD_HINT}>
                          Choose a date and time to schedule the broadcast.
                        </p>
                      )}
                    </>
                  )}
                </fieldset>
              </div>
            </div>

            {/* Action buttons */}
            <div className="border-t border-gray-100 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                {/* A pure read: counts recipients, sends and stores nothing. */}
                <button
                  type="button"
                  disabled={bcPreviewLoading || isSpendInvalid}
                  onClick={() => void countRecipients(previewRequest, previewKey)}
                  className={BUTTON_SECONDARY}
                >
                  {bcPreviewLoading ? 'Loading…' : 'Preview & Count'}
                </button>

                <button
                  type="button"
                  disabled={busy || isSpendInvalid}
                  onClick={() => void sendTest()}
                  aria-busy={testSending || undefined}
                  title={
                    adminEmail
                      ? `Only ${adminEmail} receives it, with [TEST] before the subject.`
                      : 'Your account has no email address, so the server has nowhere to send a test.'
                  }
                  className={BUTTON_SECONDARY}
                >
                  {testSending ? 'Sending…' : 'Send Test to Me'}
                </button>

                <button
                  type="button"
                  onClick={() => void reviewSend()}
                  disabled={busy || bcPreviewLoading || isSpendInvalid || isScheduleMissing}
                  className={`${BUTTON_PRIMARY} sm:ml-auto`}
                >
                  {bcScheduleMode === 'later' ? 'Schedule' : 'Send Now'}
                </button>
              </div>

              {/* Preview panel: only for the content and filters it counted. */}
              {freshPreview && (
                <div
                  role="status"
                  className="mt-4 rounded-lg border border-blue-100 bg-blue-50 p-4"
                >
                  <div className="mb-1 text-[13px] font-medium text-blue-800">
                    {freshPreview.recipient_count} recipient
                    {freshPreview.recipient_count !== 1 ? 's' : ''} match your filters
                  </div>
                  <div className="text-[12px] text-blue-700">
                    Subject: {freshPreview.rendered_subject}
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
            <div className="relative overflow-x-auto rounded-lg">
              <table className="w-full min-w-[44rem] table-fixed text-[13px]">
                <colgroup>
                  <col />
                  <col className="w-28" />
                  <col className="w-28" />
                  <col className="w-48" />
                  <col className="w-28" />
                </colgroup>
                <thead className="bg-gray-50/80">
                  <tr className="border-b border-gray-200">
                    <th className={TABLE_HEAD_CELL}>Subject</th>
                    <th className={TABLE_HEAD_CELL}>Status</th>
                    <th className={`${TABLE_HEAD_CELL} text-right`}>Recipients</th>
                    <th className={TABLE_HEAD_CELL}>Sent / Scheduled</th>
                    <th className={TABLE_HEAD_CELL}>
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {broadcasts.map((bc) => {
                    const expanded = openId === bc.id;
                    const drawerId = fieldId(`recipients-${bc.id}`);
                    return (
                      <Fragment key={bc.id}>
                        {/* The whole row toggles its recipients on click; the
                            chevron is the same toggle for the keyboard. */}
                        <tr
                          className={`cursor-pointer border-b border-gray-100 transition-colors duration-150 ${
                            expanded ? 'border-b-0 bg-gray-50/60' : 'hover:bg-gray-50/60'
                          }`}
                          onClick={() => void toggleDetail(bc.id)}
                        >
                          <td className="px-4 py-2.5 align-top">
                            <div className="truncate font-medium text-gray-900" title={bc.subject}>
                              {bc.subject}
                            </div>
                            <div className="truncate text-[12px] text-gray-500">
                              Created {relTime(bc.created_at)}
                              {bc.created_by ? ` by ${bc.created_by}` : ''}
                            </div>
                          </td>
                          <td className="px-4 py-2.5 align-top">
                            <span
                              className={`${PILL} capitalize ${
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
                          <td className="px-4 py-2.5 text-right align-top tabular-nums text-gray-700">
                            {bc.recipient_count.toLocaleString()}
                          </td>
                          <td className="whitespace-nowrap px-4 py-2.5 align-top text-[12px] text-gray-500">
                            {bc.sent_at ? (
                              <span title={new Date(bc.sent_at).toLocaleString('en-US')}>
                                Sent {relTime(bc.sent_at)}
                              </span>
                            ) : bc.scheduled_at ? (
                              <span>For {new Date(bc.scheduled_at).toLocaleString('en-US')}</span>
                            ) : (
                              '—'
                            )}
                          </td>
                          <td className="py-2 pr-3 align-top">
                            <div className="flex items-center justify-end gap-0.5">
                              {bc.status === 'scheduled' && (
                                <button
                                  type="button"
                                  aria-label={`Cancel scheduled broadcast ${bc.subject}`}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    confirmCancel(bc);
                                  }}
                                  // A test send in flight holds it, as it holds the sends.
                                  disabled={busy}
                                  className={ROW_ACTION_DANGER}
                                >
                                  Cancel
                                </button>
                              )}
                              <button
                                type="button"
                                aria-expanded={expanded}
                                aria-controls={expanded ? drawerId : undefined}
                                aria-label={`${expanded ? 'Hide' : 'Show'} recipients of ${bc.subject}`}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  void toggleDetail(bc.id);
                                }}
                                className="inline-flex h-7 w-7 items-center justify-center rounded-md text-gray-400 transition-colors duration-150 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
                              >
                                <svg
                                  viewBox="0 0 20 20"
                                  fill="currentColor"
                                  aria-hidden="true"
                                  className={`h-3.5 w-3.5 transition-transform duration-150 motion-reduce:transition-none ${
                                    expanded ? 'rotate-90' : ''
                                  }`}
                                >
                                  <path
                                    fillRule="evenodd"
                                    d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
                                    clipRule="evenodd"
                                  />
                                </svg>
                              </button>
                            </div>
                          </td>
                        </tr>
                        {/* Recipients */}
                        {expanded && (
                          <tr id={drawerId} className="border-b border-gray-100 bg-gray-50/60">
                            <td colSpan={5} className="px-4 pb-4 pt-1">
                              <div className="animate-admin-reveal">
                                {broadcastDetailLoading ? (
                                  <p
                                    role="status"
                                    className="flex items-center gap-2 text-[12px] text-gray-500"
                                  >
                                    <AdminSpinner size="sm" />
                                    Loading recipients…
                                  </p>
                                ) : detailError ? (
                                  <AdminNotice
                                    role="alert"
                                    action={
                                      <button
                                        type="button"
                                        onClick={() => void loadDetail(bc.id)}
                                        className={NOTICE_ACTION}
                                      >
                                        Retry
                                      </button>
                                    }
                                  >
                                    Recipients could not be loaded: {detailError}
                                  </AdminNotice>
                                ) : broadcastDetail ? (
                                  <>
                                    <div className="mb-2 text-[12px] font-medium text-gray-600">
                                      Recipients ({broadcastDetail.total_recipients})
                                    </div>
                                    <div className="relative overflow-x-auto rounded-md border border-gray-200 bg-white">
                                      <table className="w-full text-[12px]">
                                        <thead className="bg-gray-50/80">
                                          <tr className="text-left text-[11px] font-medium uppercase tracking-wide text-gray-500">
                                            <th className="px-3 py-1.5 font-medium">Email</th>
                                            <th className="px-3 py-1.5 font-medium">Status</th>
                                            <th className="px-3 py-1.5 font-medium">Error</th>
                                            <th className="px-3 py-1.5 font-medium">Sent At</th>
                                          </tr>
                                        </thead>
                                        <tbody className="divide-y divide-gray-100">
                                          {broadcastDetail.recipients.map((r) => (
                                            <tr key={r.user_id}>
                                              <td className="px-3 py-1.5 text-gray-700 [overflow-wrap:anywhere]">
                                                {userAccountLabel(r)}
                                              </td>
                                              <td className="px-3 py-1.5">
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
                                              <td className="px-3 py-1.5 text-red-600 [overflow-wrap:anywhere]">
                                                {r.error ?? '—'}
                                              </td>
                                              <td className="whitespace-nowrap px-3 py-1.5 text-gray-400">
                                                {r.sent_at ? relTime(r.sent_at) : '—'}
                                              </td>
                                            </tr>
                                          ))}
                                        </tbody>
                                      </table>
                                    </div>
                                  </>
                                ) : null}
                              </div>
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

      {confirmation.dialog}
    </>
  );
}
