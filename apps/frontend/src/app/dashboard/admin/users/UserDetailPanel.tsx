'use client';

import { useMoney } from '@/lib/hooks/useMoney';
import { useState } from 'react';
import { hasRole } from '@/components/providers/AuthProvider';
import { getUserDetail } from '@/lib/api/admin';
import type { AdminModelVisibilityItem, AdminUser, UserDetail } from '@/lib/api/admin';
import { UserAutomationPanel } from './UserAutomationPanel';
import { UserRecentRequests } from './UserRecentRequests';
import { UserUsageInsights } from './UserUsageInsights';
import {
  BUTTON_DANGER,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CONTROL,
  FIELD_LABEL,
  SECTION,
} from './ui';

export function relTime(s: string | null): string {
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

function shortDate(s: string | null): string {
  if (!s) return '—';
  return new Date(s).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

interface ActivityStatsData {
  avg_turns: number | null;
  avg_user_turns: number | null;
  ask_question_fraction: number | null;
}

function hasActivityStats(d: ActivityStatsData): boolean {
  return d.avg_turns != null || d.avg_user_turns != null || d.ask_question_fraction != null;
}

/** Small "note" glyph shared by the admin-note card and the table row marker. */
export function NoteIcon({ className = '' }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M5.433 13.917l1.262-3.155A4 4 0 017.58 9.42l6.92-6.918a2.121 2.121 0 013 3l-6.92 6.918c-.383.383-.84.685-1.343.886l-3.154 1.262a.5.5 0 01-.65-.65z" />
      <path d="M3.5 5.75c0-.69.56-1.25 1.25-1.25H10A.75.75 0 0010 3H4.75A2.75 2.75 0 002 5.75v9.5A2.75 2.75 0 004.75 18h9.5A2.75 2.75 0 0017 15.25V10a.75.75 0 00-1.5 0v5.25c0 .69-.56 1.25-1.25 1.25h-9.5c-.69 0-1.25-.56-1.25-1.25v-9.5z" />
    </svg>
  );
}

/** One figure in a stat strip: label, value and an optional caption. */
function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="min-w-0 px-4 py-3">
      <div className="text-[11px] font-medium text-gray-500">{label}</div>
      <div className="mt-0.5 truncate text-[18px] font-semibold tabular-nums text-gray-900">
        {value}
      </div>
      {sub && <div className="text-[11px] tabular-nums text-gray-400">{sub}</div>}
    </div>
  );
}

/**
 * All-time turn averages and ask-question share for a user. Computing them
 * scans the user's entire api_logs history, so this stays collapsed behind a
 * button by default and fetches on demand (mirroring the automation-score and
 * usage-insights panels). If the parent's detail already carries the values
 * (e.g. a prior opt-in fetch), they render immediately.
 */
function ActivityStats({ userId, initial }: { userId: string; initial: ActivityStatsData }) {
  const [data, setData] = useState<ActivityStatsData | null>(
    hasActivityStats(initial) ? initial : null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  const compute = async () => {
    setLoading(true);
    setError(false);
    try {
      const d = await getUserDetail(userId, { includeActivityStats: true });
      setData({
        avg_turns: d.avg_turns,
        avg_user_turns: d.avg_user_turns,
        ask_question_fraction: d.ask_question_fraction,
      });
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  };

  if (!data) {
    return (
      <div className="flex items-center justify-between gap-3 border-t border-gray-100 px-4 py-2.5">
        <div>
          <div className="text-[11px] font-medium text-gray-500">Activity stats</div>
          <p className="text-[11px] text-gray-400">
            All-time avg turns &amp; ask-question share — computed on demand.
          </p>
          {error && <p className="text-[11px] text-red-500">Failed to compute. Try again.</p>}
        </div>
        <button onClick={compute} disabled={loading} className={`${BUTTON_SECONDARY} shrink-0`}>
          {loading && (
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          )}
          {loading ? 'Computing…' : 'Compute activity stats'}
        </button>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 divide-x divide-gray-100 border-t border-gray-100 sm:grid-cols-4">
      <Stat
        label="Avg turns"
        value={data.avg_turns != null ? data.avg_turns.toFixed(1) : '—'}
        sub="per request"
      />
      <Stat
        label="Avg user turns"
        value={data.avg_user_turns != null ? data.avg_user_turns.toFixed(1) : '—'}
        sub="per request"
      />
      <Stat
        label="Ask-question"
        value={
          data.ask_question_fraction != null
            ? `${(data.ask_question_fraction * 100).toFixed(0)}%`
            : '—'
        }
        sub="of opening turns"
      />
    </div>
  );
}

export interface UserDetailPanelProps {
  user: AdminUser;
  detail: UserDetail;
  editRole: string;
  editQuota: string;
  editDisabledModels: string[];
  editMaxConcurrent: string;
  editNote: string;
  editSuspensionMsg: string;
  availableModels: AdminModelVisibilityItem[];
  saving: boolean;
  savingNote: boolean;
  savingSuspensionMsg: boolean;
  busy: string | null;
  onChangeRole: (role: string) => void;
  onChangeQuota: (quota: string) => void;
  onChangeDisabledModels: (modelIds: string[]) => void;
  onChangeMaxConcurrent: (val: string) => void;
  onChangeNote: (val: string) => void;
  onChangeSuspensionMsg: (val: string) => void;
  onSave: () => void;
  onSaveNote: () => void;
  onSaveSuspensionMsg: () => void;
  onSuspend: (userId: string) => void;
  onReactivate: (userId: string) => void;
  onResume: (user: AdminUser) => void;
  onRequestDelete: (user: AdminUser) => void;
  onRequestHardDelete: (user: AdminUser) => void;
}

export function UserDetailPanel(props: UserDetailPanelProps) {
  const money = useMoney();
  const {
    user: u,
    detail,
    editRole,
    editQuota,
    editDisabledModels,
    editMaxConcurrent,
    editNote,
    editSuspensionMsg,
    availableModels,
    saving,
    savingNote,
    savingSuspensionMsg,
    busy,
    onChangeRole,
    onChangeQuota,
    onChangeDisabledModels,
    onChangeMaxConcurrent,
    onChangeNote,
    onChangeSuspensionMsg,
    onSave,
    onSaveNote,
    onSaveSuspensionMsg,
    onSuspend,
    onReactivate,
    onResume,
    onRequestDelete,
    onRequestHardDelete,
  } = props;

  const toggleDisabledModel = (modelId: string) => {
    if (editDisabledModels.includes(modelId)) {
      onChangeDisabledModels(editDisabledModels.filter((value) => value !== modelId));
      return;
    }
    onChangeDisabledModels([...editDisabledModels, modelId].sort());
  };

  const noteDirty = editNote.trim() !== (detail.admin_note ?? '');

  // Account facts the list row has no room for.
  const facts: Array<[string, string]> = [
    ['ID', u.id],
    ['Joined', shortDate(detail.created_at)],
    ['Last login', relTime(detail.last_login_at)],
    ['API key', detail.has_key ? (detail.key_prefix ? `${detail.key_prefix}…` : 'yes') : 'none'],
    ['Email', detail.email ? (detail.email_verified ? 'verified' : 'unverified') : '—'],
  ];

  return (
    <div className="space-y-4">
      {/* Signup reason — surfaced first and highlighted for users awaiting approval */}
      {u.signup_reason && (
        <div
          className={`rounded-lg border p-4 ${
            u.status === 'pending_approval'
              ? 'border-indigo-200 bg-indigo-50'
              : 'border-gray-200 bg-white'
          }`}
        >
          <div className={FIELD_LABEL}>Signup reason</div>
          <p className="mt-1 whitespace-pre-wrap break-words text-[13px] text-gray-700">
            {u.signup_reason}
          </p>
        </div>
      )}

      {/* Overview: account facts, usage figures and the on-demand activity stats */}
      <div className="overflow-hidden rounded-lg border border-gray-200 bg-white">
        <div className="flex flex-wrap gap-x-5 gap-y-1 border-b border-gray-100 bg-gray-50/60 px-4 py-2 text-[12px]">
          {facts.map(([k, v]) => (
            <div key={k} className="flex min-w-0 items-center gap-1.5">
              <span className="text-gray-400">{k}</span>
              <span className="truncate font-medium text-gray-700">{v}</span>
            </div>
          ))}
        </div>
        <div className="grid grid-cols-2 divide-x divide-gray-100 sm:grid-cols-4">
          <Stat
            label="Today"
            value={money.value(detail, 'usage_today', { maximumFractionDigits: 2 })}
            sub={`${detail.usage_today_requests} req`}
          />
          <Stat
            label="This month"
            value={money.value(detail, 'usage_month', { maximumFractionDigits: 2 })}
            sub={`${detail.usage_month_requests} req`}
          />
          <Stat label="Last active" value={relTime(detail.last_request_at)} />
          <Stat
            label="Quota"
            value={
              money.amount(detail, 'quota_daily') != null
                ? `${money.value(detail, 'quota_daily')}/d`
                : '—'
            }
          />
        </div>
        {/* All-time activity stats (avg turns, ask-question share) require
            full-history log scans, so they are gated behind a button to keep
            expanding a row fast. The same figures also appear per-row in the
            table above (from the bulk endpoints), so nothing is lost by default. */}
        <ActivityStats userId={u.id} initial={detail} />
        {detail.models_used.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 border-t border-gray-100 px-4 py-2.5">
            <span className="mr-1 text-[11px] font-medium text-gray-500">Models used</span>
            {detail.models_used.map((m) => (
              <span
                key={m}
                className="rounded-md border border-gray-200 bg-gray-50 px-2 py-0.5 text-[11px] font-medium text-gray-600"
              >
                {m.split('/').pop()}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-5">
        <div className="space-y-4 lg:col-span-3">
          {/* Account settings (active users): role, limits and model access share one Save. */}
          {u.status === 'active' && (
            <div className={`${SECTION} space-y-4`}>
              <div className={FIELD_LABEL}>Account settings</div>
              <div className="flex flex-wrap items-end gap-3">
                <label className="block">
                  <div className="mb-1 text-[11px] font-medium text-gray-500">Role</div>
                  <select
                    value={editRole}
                    onChange={(e) => onChangeRole(e.target.value)}
                    className={CONTROL}
                  >
                    <option value="free">free</option>
                    <option value="pro">pro</option>
                    <option value="internal">internal</option>
                    <option value="admin">admin</option>
                  </select>
                </label>
                {detail.has_key && (
                  <label className="block">
                    <div className="mb-1 text-[11px] font-medium text-gray-500">
                      Daily quota ({money.sourceCurrency(detail)})
                    </div>
                    <input
                      type="number"
                      value={editQuota}
                      onChange={(e) => onChangeQuota(e.target.value)}
                      className={`${CONTROL} w-28`}
                    />
                  </label>
                )}
                <label className="block">
                  <div className="mb-1 text-[11px] font-medium text-gray-500">Max concurrent</div>
                  <input
                    type="number"
                    min={1}
                    value={editMaxConcurrent}
                    onChange={(e) => onChangeMaxConcurrent(e.target.value)}
                    placeholder="Role default"
                    className={`${CONTROL} w-28`}
                  />
                </label>
              </div>

              {availableModels.length > 0 && (
                <div className="space-y-2" data-testid="disabled-models-panel">
                  <div>
                    <div className="text-[11px] font-medium text-gray-500">Model access</div>
                    <p className="mt-0.5 text-[12px] text-gray-400">
                      Disabled models stay hidden for this user even if their role would normally
                      allow them. Models above the user&apos;s role are locked and cannot be enabled
                      here.
                    </p>
                  </div>
                  <div className="grid gap-1.5 sm:grid-cols-2">
                    {availableModels.map((model) => {
                      const roleLocked = !hasRole(editRole, model.effective_required_role);
                      const isDisabled = editDisabledModels.includes(model.model_id);
                      if (roleLocked) {
                        return (
                          <label
                            key={model.model_id}
                            className="flex cursor-not-allowed items-center gap-2 rounded-md border border-gray-200 bg-gray-50 px-3 py-1.5 text-[12px] text-gray-500"
                          >
                            <input
                              type="checkbox"
                              checked={false}
                              disabled
                              readOnly
                              className="cursor-not-allowed"
                              aria-label={`${model.model_id} requires ${model.effective_required_role} role`}
                            />
                            <span className="truncate font-medium text-gray-500">
                              {model.model_id}
                            </span>
                            <span className="ml-auto shrink-0 text-[10px] font-semibold text-gray-400">
                              requires {model.effective_required_role}
                            </span>
                          </label>
                        );
                      }
                      return (
                        <label
                          key={model.model_id}
                          className={`flex cursor-pointer items-center gap-2 rounded-md border px-3 py-1.5 text-[12px] transition ${
                            isDisabled
                              ? 'border-red-200 bg-red-50 text-gray-500'
                              : 'border-gray-200 bg-white text-gray-700 hover:border-gray-300'
                          }`}
                        >
                          <input
                            type="checkbox"
                            checked={!isDisabled}
                            onChange={() => toggleDisabledModel(model.model_id)}
                            aria-label={
                              isDisabled ? `Enable ${model.model_id}` : `Disable ${model.model_id}`
                            }
                          />
                          <span
                            className={`truncate font-medium ${
                              isDisabled ? 'text-gray-400 line-through' : 'text-gray-900'
                            }`}
                          >
                            {model.model_id}
                          </span>
                          {isDisabled && (
                            <span className="ml-auto shrink-0 text-[10px] font-semibold text-red-500">
                              disabled
                            </span>
                          )}
                        </label>
                      );
                    })}
                  </div>
                </div>
              )}

              <div className="flex justify-end border-t border-gray-100 pt-3">
                <button onClick={onSave} disabled={saving} className={BUTTON_PRIMARY}>
                  {saving ? '...' : 'Save'}
                </button>
              </div>
            </div>
          )}

          {/* Suspension message — shown to the user on the login page. */}
          {u.status === 'suspended' && (
            <div className="space-y-2 rounded-lg border border-red-200 bg-red-50/60 p-4">
              <div className="flex items-center justify-between">
                <div className={FIELD_LABEL}>Suspension message</div>
                <span className="text-[10px] text-gray-400">Shown to the user at login</span>
              </div>
              <textarea
                value={editSuspensionMsg}
                onChange={(e) => onChangeSuspensionMsg(e.target.value)}
                rows={2}
                maxLength={2000}
                placeholder="Explain why the account is suspended and what the user can do…"
                aria-label="Suspension message"
                className="w-full rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
              />
              <div className="flex justify-end">
                <button
                  onClick={onSaveSuspensionMsg}
                  disabled={
                    savingSuspensionMsg ||
                    editSuspensionMsg.trim() === (detail.suspension_message ?? '')
                  }
                  className={BUTTON_PRIMARY}
                >
                  {savingSuspensionMsg ? 'Saving…' : 'Save message'}
                </button>
              </div>
            </div>
          )}

          {/* Account lifecycle actions, kept apart from the everyday settings. */}
          {u.status === 'active' && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-gray-200 bg-white px-4 py-3">
              <div>
                <div className="text-[13px] font-medium text-gray-900">Account access</div>
                <p className="text-[12px] text-gray-400">
                  Suspend blocks sign-in and API use; delete revokes keys and sessions.
                </p>
              </div>
              <div className="flex items-center gap-2">
                <button onClick={() => onSuspend(u.id)} className={BUTTON_DANGER}>
                  Suspend
                </button>
                <button onClick={() => onRequestDelete(u)} className={BUTTON_DANGER}>
                  Delete
                </button>
              </div>
            </div>
          )}
          {u.status === 'suspended' && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-gray-200 bg-white px-4 py-3">
              <span className="text-[13px] text-gray-500">This user is suspended.</span>
              <div className="flex items-center gap-2">
                <button onClick={() => onRequestDelete(u)} className={BUTTON_DANGER}>
                  Delete
                </button>
                <button onClick={() => onReactivate(u.id)} className={BUTTON_PRIMARY}>
                  Reactivate
                </button>
              </div>
            </div>
          )}
          {u.status === 'deleted' && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-gray-200 bg-white px-4 py-3">
              <span className="text-[13px] text-gray-400">This user has been deleted.</span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => onRequestHardDelete(u)}
                  disabled={busy === u.id}
                  className={BUTTON_DANGER}
                >
                  Permanently Delete
                </button>
                <button
                  onClick={() => onResume(u)}
                  disabled={busy === u.id}
                  className={BUTTON_PRIMARY}
                >
                  Resume
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Admin note — internal annotation editable for every user status. */}
        <div className="lg:col-span-2">
          <div className={`${SECTION} space-y-3`}>
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-1.5 text-[13px] font-semibold text-gray-900">
                <NoteIcon className="h-3.5 w-3.5 text-amber-500" />
                Admin note
              </div>
              <span className="inline-flex items-center gap-1 text-[11px] text-gray-400">
                <svg className="h-3 w-3" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path
                    fillRule="evenodd"
                    d="M10 1a4.5 4.5 0 00-4.5 4.5V9H5a2 2 0 00-2 2v6a2 2 0 002 2h10a2 2 0 002-2v-6a2 2 0 00-2-2h-.5V5.5A4.5 4.5 0 0010 1zm3 8V5.5a3 3 0 10-6 0V9h6z"
                    clipRule="evenodd"
                  />
                </svg>
                Admins only
              </span>
            </div>
            <textarea
              value={editNote}
              onChange={(e) => onChangeNote(e.target.value)}
              rows={4}
              maxLength={2000}
              placeholder="Add an internal note about this user…"
              aria-label="Admin note"
              className="block w-full resize-y rounded-md border border-transparent bg-gray-50 px-3 py-2 text-[13px] leading-relaxed text-gray-800 placeholder:text-gray-400 transition focus:border-gray-300 focus:bg-white focus:outline-none"
            />
            {/* Save stays in place (disabled until the note changes) so the
                card does not jump; Discard appears only with unsaved edits. */}
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] tabular-nums text-gray-400">
                {noteDirty
                  ? `${editNote.length} / 2000 · unsaved`
                  : detail.admin_note
                    ? ''
                    : 'Context for other admins, e.g. why a quota was raised.'}
              </span>
              <div className="flex shrink-0 items-center gap-2">
                {noteDirty && (
                  <button
                    type="button"
                    onClick={() => onChangeNote(detail.admin_note ?? '')}
                    disabled={savingNote}
                    className="rounded-md px-2.5 py-1.5 text-[12px] font-medium text-gray-500 transition hover:text-gray-900 disabled:opacity-50"
                  >
                    Discard
                  </button>
                )}
                <button
                  onClick={onSaveNote}
                  disabled={savingNote || !noteDirty}
                  className={BUTTON_PRIMARY}
                >
                  {savingNote ? 'Saving…' : 'Save note'}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* These three panels are siblings, so their keys must be unique relative
          to each other — keying them all on `u.id` collides and makes React
          duplicate them on re-render (e.g. after a quota save), stacking extra
          copies of each box. Each key is prefixed so it stays unique here while
          still changing per user, which remounts the panels (resetting their
          computed state) when the admin switches to a different user. */}
      {/* On-demand automation score. */}
      <UserAutomationPanel key={`automation-${u.id}`} userId={u.id} />
      {/* LLM analysis of this user's requests. */}
      <UserUsageInsights key={`insights-${u.id}`} userId={u.id} />
      {/* Recent requests made by this user. */}
      <UserRecentRequests key={`recent-${u.id}`} userId={u.id} />
    </div>
  );
}
