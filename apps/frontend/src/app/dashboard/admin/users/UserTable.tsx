'use client';

import { Fragment, useEffect, useMemo, useState } from 'react';
import type {
  AdminModelVisibilityItem,
  AdminUser,
  UserAskQuestionFraction,
  UserAutomationScore,
  UserDetail,
  UserTurnAverages,
} from '@/lib/api/admin';
import { getUserDetail, listModelVisibility, updateUser as apiUpdateUser } from '@/lib/api/admin';
import { userAccountLabel, userAccountLabelKind } from '@/lib/utils/userLabel';
import type { CostHistoryPoint, Density, FilterState, UserRow as UserRowType } from './types';
import { UserRow } from './UserRow';
import { COLUMN_GROUPS, type ColumnId } from './lib/columns';
import { UserDetailPanel } from './UserDetailPanel';

interface UserTableProps {
  users: UserRowType[];
  costHistories: Record<string, CostHistoryPoint[]>;
  turnAverages: Record<string, UserTurnAverages>;
  askQuestionFractions: Record<string, UserAskQuestionFraction>;
  automationScores: Record<string, UserAutomationScore>;
  scoreState: 'idle' | 'loading' | 'loaded';
  scoreSortDir: 'desc' | 'asc' | null;
  onScoreHeader: () => void;
  density: Density;
  /** Optional metric columns the admin has hidden from the Columns menu. */
  hiddenColumns?: ColumnId[];
  filterState: FilterState;
  onSortChange: (sortBy: FilterState['sortBy']) => void;
  // action handlers (all return Promise for async operations)
  onApprove: (userId: string) => Promise<void>;
  onReject: (userId: string, reason: string) => Promise<void>;
  onUpdate: (userId: string, patch: Record<string, unknown>) => Promise<void>;
  onSuspend: (userId: string, suspensionMessage?: string | null) => Promise<void>;
  onResume: (userId: string) => Promise<void>;
  onDelete: (userId: string, reason: string) => Promise<void>;
  onHardDelete: (userId: string, reason: string) => Promise<void>;
}

interface ConfirmAction {
  title: string;
  body: string;
  confirmLabel: string;
  confirmTone: 'danger' | 'neutral';
  // When set, the modal renders a labeled textarea and passes its value to
  // onConfirm (used to capture the suspension message at suspend time).
  messageField?: { label: string; placeholder?: string };
  onConfirm: (message?: string) => void | Promise<void>;
}

function median(nums: number[]): number {
  if (nums.length === 0) return 0;
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

export function UserTable(props: UserTableProps) {
  const {
    users,
    costHistories,
    turnAverages,
    askQuestionFractions,
    automationScores,
    scoreState,
    scoreSortDir,
    onScoreHeader,
    density,
    hiddenColumns = [],
    filterState,
    onSortChange,
  } = props;
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editRole, setEditRole] = useState('');
  const [editQuota, setEditQuota] = useState('');
  const [editDisabledModels, setEditDisabledModels] = useState<string[]>([]);
  const [editMaxConcurrent, setEditMaxConcurrent] = useState('');
  const [editNote, setEditNote] = useState('');
  const [editSuspensionMsg, setEditSuspensionMsg] = useState('');
  const [availableModels, setAvailableModels] = useState<AdminModelVisibilityItem[]>([]);
  const [saving, setSaving] = useState(false);
  const [savingNote, setSavingNote] = useState(false);
  const [savingSuspensionMsg, setSavingSuspensionMsg] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const pageMedianToday = useMemo(
    () => median(users.map((u) => Number(u.usage_today_usd)).filter((n) => n > 0)),
    [users],
  );

  const showSparkline = density === 'comfortable';
  // Metric columns actually rendered, in table order. The 7-day trend also
  // needs the comfortable density (compact rows don't fetch the history).
  const hidden = new Set(hiddenColumns);
  const visibleGroups = COLUMN_GROUPS.map((g) => ({
    label: g.label,
    ids: g.columns
      .map((c) => c.id)
      .filter((id) => !hidden.has(id) && (id !== 'trend' || showSparkline)),
  })).filter((g) => g.ids.length > 0);
  const visible = new Set<ColumnId>(visibleGroups.flatMap((g) => g.ids));
  const groupStarts = new Set<ColumnId>(visibleGroups.map((g) => g.ids[0]));
  // A left rule where a column group begins.
  const edge = (id: ColumnId) => (groupStarts.has(id) ? 'border-l border-gray-100' : '');
  const colSpan = 2 + visible.size;

  const scoreIndicator =
    scoreState === 'loaded'
      ? scoreSortDir === 'desc'
        ? ' ↓'
        : scoreSortDir === 'asc'
          ? ' ↑'
          : ''
      : '';

  const sortIndicator = (col: FilterState['sortBy']) => (filterState.sortBy === col ? ' ↓' : '');
  const ariaSortFor = (col: FilterState['sortBy']): 'ascending' | 'none' =>
    filterState.sortBy === col ? 'ascending' : 'none';

  // A sortable column header. The server sorts descending only, so the
  // indicator is a single arrow on the active column.
  const sortHeader = ({
    col,
    label,
    title,
    className = '',
  }: {
    col: FilterState['sortBy'];
    label: string;
    title?: string;
    className?: string;
  }) => (
    <th
      className={`whitespace-nowrap px-2.5 py-2 text-right align-bottom ${className}`}
      aria-sort={ariaSortFor(col)}
    >
      <button
        type="button"
        className={`cursor-pointer uppercase tracking-wide transition hover:text-gray-900 ${
          filterState.sortBy === col ? 'text-gray-900' : ''
        }`}
        onClick={() => onSortChange(col)}
        title={title}
      >
        {label}
        {sortIndicator(col)}
      </button>
    </th>
  );

  const toggleDetail = async (userId: string) => {
    if (expandedId === userId) {
      setExpandedId(null);
      setDetail(null);
      return;
    }
    setExpandedId(userId);
    setDetailLoading(true);
    setDetail(null);
    try {
      const [d, visibility] = await Promise.all([getUserDetail(userId), listModelVisibility()]);
      setDetail(d);
      setEditRole(d.role || 'free');
      setEditQuota(d.quota_daily_usd?.toString() ?? '100');
      setEditDisabledModels(d.disabled_models ?? []);
      setEditMaxConcurrent(d.max_concurrent_requests?.toString() ?? '');
      setEditNote(d.admin_note ?? '');
      setEditSuspensionMsg(d.suspension_message ?? '');
      setAvailableModels(visibility.models);
    } catch {
      setExpandedId(null);
    } finally {
      setDetailLoading(false);
    }
  };

  const doSave = async () => {
    if (!expandedId || !detail) return;
    setSaving(true);
    try {
      const patch: Record<string, unknown> = {};
      if (editRole !== (detail.role || 'free')) patch.role = editRole;
      if (editQuota !== (detail.quota_daily_usd?.toString() ?? '100')) {
        patch.quota_daily_cost_usd = Number(editQuota);
      }
      const currentDisabledModels = [...(detail.disabled_models ?? [])].sort();
      const nextDisabledModels = [...editDisabledModels].sort();
      if (JSON.stringify(nextDisabledModels) !== JSON.stringify(currentDisabledModels)) {
        patch.disabled_models = nextDisabledModels;
      }
      const currentMaxConcurrent = detail.max_concurrent_requests?.toString() ?? '';
      if (editMaxConcurrent !== currentMaxConcurrent) {
        patch.max_concurrent_requests = editMaxConcurrent === '' ? null : Number(editMaxConcurrent);
      }
      if (!Object.keys(patch).length) return;
      await apiUpdateUser(expandedId, patch);
      const refreshed = await getUserDetail(expandedId);
      setDetail(refreshed);
      setEditDisabledModels(refreshed.disabled_models ?? []);
      await props.onUpdate(expandedId, patch);
    } finally {
      setSaving(false);
    }
  };

  // Admin note has its own save so it can be edited for users of any status,
  // independent of the active-only role/quota editor below it.
  const doSaveNote = async () => {
    if (!expandedId || !detail) return;
    const next = editNote.trim() ? editNote.trim() : null;
    if (next === (detail.admin_note ?? null)) return;
    setSavingNote(true);
    try {
      await props.onUpdate(expandedId, { admin_note: next });
      const refreshed = await getUserDetail(expandedId);
      setDetail(refreshed);
      setEditNote(refreshed.admin_note ?? '');
    } finally {
      setSavingNote(false);
    }
  };

  // Suspension message has its own save so an admin can edit the user-facing
  // message after suspension, without re-suspending.
  const doSaveSuspensionMsg = async () => {
    if (!expandedId || !detail) return;
    const next = editSuspensionMsg.trim() ? editSuspensionMsg.trim() : null;
    if (next === (detail.suspension_message ?? null)) return;
    setSavingSuspensionMsg(true);
    try {
      await props.onUpdate(expandedId, { suspension_message: next });
      const refreshed = await getUserDetail(expandedId);
      setDetail(refreshed);
      setEditSuspensionMsg(refreshed.suspension_message ?? '');
    } finally {
      setSavingSuspensionMsg(false);
    }
  };

  const doSuspend = (userId: string) => {
    setConfirmMessage('');
    setConfirmAction({
      title: 'Suspend user?',
      body: 'The user will be unable to authenticate or use their API key while suspended.',
      confirmLabel: 'Suspend',
      confirmTone: 'danger',
      messageField: {
        label: 'Message shown to the user at login (optional)',
        placeholder:
          'e.g. Your account was suspended for a policy violation. Contact support to appeal.',
      },
      onConfirm: async (message) => {
        setBusy(userId);
        try {
          await props.onSuspend(userId, message?.trim() ? message.trim() : null);
          setExpandedId(null);
          setDetail(null);
        } finally {
          setBusy(null);
        }
      },
    });
  };

  const doReactivate = async (userId: string) => {
    setBusy(userId);
    try {
      await props.onUpdate(userId, { status: 'active' });
      setExpandedId(null);
      setDetail(null);
    } finally {
      setBusy(null);
    }
  };

  const doResume = async (user: AdminUser) => {
    setBusy(user.id);
    try {
      await props.onResume(user.id);
      setExpandedId(null);
      setDetail(null);
    } finally {
      setBusy(null);
    }
  };

  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null);
  const [deleteReason, setDeleteReason] = useState('');
  const [hardDeleteTarget, setHardDeleteTarget] = useState<AdminUser | null>(null);
  const [hardDeleteReason, setHardDeleteReason] = useState('');
  // What the admin types to confirm a permanent delete: the account's email,
  // or its login name when it has none (the first-run administrator), or as a
  // last resort its id. The backend does not see it; it is a typo guard.
  const [hardDeleteEmailConfirm, setHardDeleteEmailConfirm] = useState('');
  const hardDeleteConfirmTarget = hardDeleteTarget ? userAccountLabel(hardDeleteTarget) : '';
  const hardDeleteConfirmKind = userAccountLabelKind(hardDeleteTarget);
  // Reject modal state — replaces window.prompt (UX consistency + a11y).
  const [rejectTarget, setRejectTarget] = useState<UserRowType | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  // Generic confirm modal — replaces window.confirm for suspend
  // so the same custom-modal pattern is used everywhere in this table.
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  // Value of the optional message textarea in the confirm modal (suspend flow).
  const [confirmMessage, setConfirmMessage] = useState('');

  // Close delete/hard-delete/reject modals if their target row leaves the
  // visible list (filter change, pagination). Without this, the modal stays
  // open with a stale target and the user could submit an action against a
  // row they can no longer see.
  useEffect(() => {
    if (deleteTarget && !users.some((u) => u.id === deleteTarget.id)) {
      setDeleteTarget(null);
      setDeleteReason('');
    }
    if (hardDeleteTarget && !users.some((u) => u.id === hardDeleteTarget.id)) {
      setHardDeleteTarget(null);
      setHardDeleteReason('');
      setHardDeleteEmailConfirm('');
    }
    if (rejectTarget && !users.some((u) => u.id === rejectTarget.id)) {
      setRejectTarget(null);
      setRejectReason('');
    }
  }, [users, deleteTarget, hardDeleteTarget, rejectTarget]);

  const doDelete = async () => {
    if (!deleteTarget || !deleteReason.trim()) return;
    setBusy(deleteTarget.id);
    try {
      await props.onDelete(deleteTarget.id, deleteReason.trim());
      setDeleteTarget(null);
      setDeleteReason('');
      setExpandedId(null);
      setDetail(null);
    } finally {
      setBusy(null);
    }
  };

  const doReject = async () => {
    if (!rejectTarget || !rejectReason.trim()) return;
    setBusy(rejectTarget.id);
    try {
      await props.onReject(rejectTarget.id, rejectReason.trim());
      setRejectTarget(null);
      setRejectReason('');
    } finally {
      setBusy(null);
    }
  };

  const doHardDelete = async () => {
    if (!hardDeleteTarget) return;
    if (!hardDeleteConfirmTarget || hardDeleteEmailConfirm !== hardDeleteConfirmTarget) return;
    setBusy(hardDeleteTarget.id);
    try {
      await props.onHardDelete(hardDeleteTarget.id, hardDeleteReason.trim());
      setHardDeleteTarget(null);
      setHardDeleteReason('');
      setHardDeleteEmailConfirm('');
      setExpandedId(null);
      setDetail(null);
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      {/* `container-type` lets the expanded detail row size itself to the
          visible width (100cqw) instead of the full scrolled table width, so
          its controls never end up off-screen to the right. */}
      <div className="overflow-x-auto [container-type:inline-size]">
        <table className="min-w-full text-sm">
          <thead>
            {/* Column groups make the wide metric block scannable. */}
            <tr className="border-b border-gray-100 bg-gray-50/70 text-[10px] font-semibold uppercase tracking-wider text-gray-400">
              <th colSpan={2} className="sticky left-0 z-10 bg-gray-50 px-3 pt-2 text-left" />
              {visibleGroups.map((g) => (
                <th
                  key={g.label}
                  colSpan={g.ids.length}
                  className="whitespace-nowrap border-l border-gray-100 px-3 pt-2 text-center"
                >
                  {g.label}
                </th>
              ))}
            </tr>
            <tr className="border-b border-gray-200 bg-gray-50/70 text-[11px] font-medium uppercase tracking-wide text-gray-500">
              <th className="sticky left-0 z-10 bg-gray-50 py-2 pl-9 pr-2.5 text-left align-bottom">
                User
              </th>
              <th className="px-2.5 py-2 text-left align-bottom">Status</th>
              {visible.has('today') &&
                sortHeader({ col: 'cost_today', label: 'Today', className: edge('today') })}
              {visible.has('trend') && (
                <th
                  className={`w-16 px-2 py-2 text-left align-bottom font-medium normal-case text-gray-400 ${edge('trend')}`}
                >
                  7d
                </th>
              )}
              {visible.has('month') &&
                sortHeader({ col: 'cost_month', label: 'Month', className: edge('month') })}
              {visible.has('alltime') &&
                sortHeader({ col: 'cost_alltime', label: 'All-time', className: edge('alltime') })}
              {visible.has('requests') &&
                sortHeader({
                  col: 'requests',
                  label: 'Requests',
                  title: 'All-time request count',
                  className: edge('requests'),
                })}
              {visible.has('tokens') &&
                sortHeader({
                  col: 'tokens',
                  label: 'Tokens',
                  title: 'All-time total tokens',
                  className: edge('tokens'),
                })}
              {visible.has('avgTurns') && (
                <th
                  className={`w-[4.5rem] px-2.5 py-2 text-right align-bottom leading-tight xl:w-auto xl:whitespace-nowrap ${edge('avgTurns')}`}
                  title="Average messages per chat request (all-time)"
                >
                  Avg turns
                </th>
              )}
              {visible.has('avgUserTurns') && (
                <th
                  className={`w-[4.5rem] px-2.5 py-2 text-right align-bottom leading-tight xl:w-auto xl:whitespace-nowrap ${edge('avgUserTurns')}`}
                  title="Average user messages per chat request (all-time)"
                >
                  Avg user turns
                </th>
              )}
              {visible.has('askQuestion') && (
                <th
                  className={`w-[4.5rem] px-2.5 py-2 text-right align-bottom leading-tight xl:w-auto xl:whitespace-nowrap ${edge('askQuestion')}`}
                  title="Share of this user's early-conversation requests (first 5 turns) whose available tools offer an ask-the-user question tool"
                >
                  Ask-question
                </th>
              )}
              {visible.has('automation') && (
                <th
                  className={`whitespace-nowrap px-2.5 py-2 pr-3 text-right align-bottom ${edge('automation')}`}
                >
                  <button
                    type="button"
                    onClick={onScoreHeader}
                    className={`inline-flex cursor-pointer items-center gap-1 rounded px-1.5 py-0.5 uppercase tracking-wide transition hover:bg-gray-200/70 hover:text-gray-900 ${
                      scoreState === 'idle' ? 'text-indigo-600' : ''
                    }`}
                    title="Automation score (human vs. script). Click to compute for this page, then click again to sort."
                  >
                    <span>{scoreState === 'idle' ? 'Score ▸' : `Automation${scoreIndicator}`}</span>
                    {scoreState === 'loading' && (
                      <span className="h-3 w-3 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                    )}
                  </button>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {users.length === 0 && (
              <tr>
                <td colSpan={colSpan} className="px-4 py-12 text-center text-[13px] text-gray-400">
                  No users match your filters.
                </td>
              </tr>
            )}
            {users.map((u) => {
              const asAdminUser: AdminUser = {
                id: u.id,
                email: u.email,
                login_name: u.login_name ?? null,
                phone: u.phone,
                user_name: u.user_name,
                role: u.role,
                status: u.status,
                email_verified: u.email_verified,
                approval_note: u.approval_note,
                reviewed_at: u.reviewed_at,
                reviewed_by: u.reviewed_by,
                signup_reason: u.signup_reason,
                admin_note: u.admin_note,
                created_at: u.created_at,
                last_login_at: u.last_login_at,
                has_key: u.has_key,
                key_prefix: u.key_prefix,
                key_status: u.key_status,
                usage_today_usd: Number(u.usage_today_usd),
                usage_month_usd: Number(u.usage_month_usd),
                usage_alltime_usd: Number(u.usage_alltime_usd),
                usage_alltime_requests: u.usage_alltime_requests,
                usage_alltime_tokens: u.usage_alltime_tokens,
              };
              const isExpanded = expandedId === u.id;
              return (
                <Fragment key={u.id}>
                  <UserRow
                    user={u}
                    history={costHistories[u.id]}
                    turns={turnAverages[u.id]}
                    askQuestion={askQuestionFractions[u.id]}
                    automation={automationScores[u.id]}
                    scoreState={scoreState}
                    pageMedianToday={pageMedianToday}
                    density={density}
                    visible={visible}
                    groupStarts={groupStarts}
                    expanded={isExpanded}
                    onToggleExpanded={() => toggleDetail(u.id)}
                    onApprove={() => {
                      setBusy(u.id);
                      props.onApprove(u.id).finally(() => setBusy(null));
                    }}
                    onReject={() => {
                      setRejectTarget(u);
                      setRejectReason('');
                    }}
                  />
                  {isExpanded && (
                    <tr className="border-b border-gray-200">
                      <td colSpan={colSpan} className="bg-gray-50 p-0">
                        <div className="sticky left-0 w-[100cqw] p-4">
                          {detailLoading ? (
                            <div className="flex justify-center py-6">
                              <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                            </div>
                          ) : detail ? (
                            <UserDetailPanel
                              user={asAdminUser}
                              detail={detail}
                              editRole={editRole}
                              editQuota={editQuota}
                              editDisabledModels={editDisabledModels}
                              editMaxConcurrent={editMaxConcurrent}
                              editNote={editNote}
                              editSuspensionMsg={editSuspensionMsg}
                              availableModels={availableModels}
                              saving={saving}
                              savingNote={savingNote}
                              savingSuspensionMsg={savingSuspensionMsg}
                              busy={busy}
                              onChangeRole={setEditRole}
                              onChangeQuota={setEditQuota}
                              onChangeDisabledModels={setEditDisabledModels}
                              onChangeMaxConcurrent={setEditMaxConcurrent}
                              onChangeNote={setEditNote}
                              onChangeSuspensionMsg={setEditSuspensionMsg}
                              onSave={doSave}
                              onSaveNote={doSaveNote}
                              onSaveSuspensionMsg={doSaveSuspensionMsg}
                              onSuspend={doSuspend}
                              onReactivate={doReactivate}
                              onResume={doResume}
                              onRequestDelete={(target) => {
                                setDeleteTarget(target);
                                setDeleteReason('');
                              }}
                              onRequestHardDelete={(target) => {
                                setHardDeleteTarget(target);
                                setHardDeleteReason('');
                                setHardDeleteEmailConfirm('');
                              }}
                            />
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

      {/* Delete modal */}
      {deleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/20 backdrop-blur-[2px]"
            onClick={() => {
              setDeleteTarget(null);
              setDeleteReason('');
            }}
          />
          <div className="relative mx-4 w-full max-w-sm rounded-xl border border-gray-200 bg-white p-5 shadow-2xl">
            <h3 className="text-[15px] font-semibold text-gray-900">
              Delete {userAccountLabel(deleteTarget)}
            </h3>
            <p className="mt-1 text-[12px] text-gray-400">
              This will revoke API keys, purge sessions, and set the account to deleted.
            </p>
            <textarea
              className="mt-3 w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] placeholder:text-gray-300 focus:border-gray-400 focus:outline-none"
              rows={3}
              placeholder="Reason for deletion..."
              value={deleteReason}
              onChange={(e) => setDeleteReason(e.target.value)}
              autoFocus
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => {
                  setDeleteTarget(null);
                  setDeleteReason('');
                }}
                className="rounded-md px-3 py-1.5 text-[13px] text-gray-400 hover:text-gray-900 transition"
              >
                Cancel
              </button>
              <button
                onClick={doDelete}
                disabled={!deleteReason.trim() || busy === deleteTarget.id}
                className="rounded-md bg-red-600 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-red-700 transition disabled:opacity-50"
              >
                {busy === deleteTarget.id ? '...' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Reject modal — replaces window.prompt for rejection reason. */}
      {rejectTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/20 backdrop-blur-[2px]"
            onClick={() => {
              setRejectTarget(null);
              setRejectReason('');
            }}
          />
          <div className="relative mx-4 w-full max-w-sm rounded-xl border border-gray-200 bg-white p-5 shadow-2xl">
            <h3 className="text-[15px] font-semibold text-gray-900">
              Reject {userAccountLabel(rejectTarget)}
            </h3>
            <p className="mt-1 text-[12px] text-gray-400">
              The user will see this reason in their pending-approval state.
            </p>
            <textarea
              className="mt-3 w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] placeholder:text-gray-300 focus:border-gray-400 focus:outline-none"
              rows={3}
              placeholder="Reason for rejection..."
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              autoFocus
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => {
                  setRejectTarget(null);
                  setRejectReason('');
                }}
                className="rounded-md px-3 py-1.5 text-[13px] text-gray-400 hover:text-gray-900 transition"
              >
                Cancel
              </button>
              <button
                onClick={doReject}
                disabled={!rejectReason.trim() || busy === rejectTarget.id}
                className="rounded-md bg-red-600 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-red-700 transition disabled:opacity-50"
              >
                {busy === rejectTarget.id ? '...' : 'Reject'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Generic confirm modal — replaces window.confirm for suspend. */}
      {confirmAction && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/20 backdrop-blur-[2px]"
            onClick={() => setConfirmAction(null)}
          />
          <div className="relative mx-4 w-full max-w-sm rounded-xl border border-gray-200 bg-white p-5 shadow-2xl">
            <h3 className="text-[15px] font-semibold text-gray-900">{confirmAction.title}</h3>
            <p className="mt-2 text-[13px] text-gray-600">{confirmAction.body}</p>
            {confirmAction.messageField && (
              <div className="mt-3">
                <label className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-gray-500">
                  {confirmAction.messageField.label}
                </label>
                <textarea
                  value={confirmMessage}
                  onChange={(e) => setConfirmMessage(e.target.value)}
                  rows={3}
                  maxLength={2000}
                  placeholder={confirmAction.messageField.placeholder}
                  aria-label={confirmAction.messageField.label}
                  className="w-full rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
                />
              </div>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setConfirmAction(null)}
                className="rounded-md px-3 py-1.5 text-[13px] text-gray-400 hover:text-gray-900 transition"
              >
                Cancel
              </button>
              <button
                onClick={async () => {
                  const action = confirmAction;
                  const message = confirmMessage;
                  setConfirmAction(null);
                  await action.onConfirm(message);
                }}
                className={`rounded-md px-3 py-1.5 text-[12px] font-semibold text-white transition ${
                  confirmAction.confirmTone === 'danger'
                    ? 'bg-red-600 hover:bg-red-700'
                    : 'bg-gray-700 hover:bg-gray-800'
                }`}
              >
                {confirmAction.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Hard-delete modal */}
      {hardDeleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/30 backdrop-blur-[2px]"
            onClick={() => {
              setHardDeleteTarget(null);
              setHardDeleteReason('');
              setHardDeleteEmailConfirm('');
            }}
          />
          <div className="relative mx-4 w-full max-w-md rounded-xl border border-red-200 bg-white p-5 shadow-2xl">
            <h3 className="text-[15px] font-semibold text-red-700">
              Permanently delete {hardDeleteConfirmTarget}
            </h3>
            <p className="mt-2 text-[12px] text-gray-600">
              This will <span className="font-semibold text-red-700">permanently wipe</span> the
              user row, all API keys, all api_logs, and prior audit-log entries for this user. This
              action <span className="font-semibold">cannot be undone</span>.
            </p>
            <p className="mt-3 text-[12px] text-gray-500">
              Type the user&apos;s{' '}
              {hardDeleteConfirmKind === 'email'
                ? 'email address'
                : hardDeleteConfirmKind === 'login_name'
                  ? 'username'
                  : hardDeleteConfirmKind === 'phone'
                    ? 'phone number'
                    : 'user ID'}{' '}
              (<span className="font-mono text-gray-700">{hardDeleteConfirmTarget}</span>) to
              confirm:
            </p>
            <input
              type="text"
              className="mt-2 w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] font-mono placeholder:text-gray-300 focus:border-red-400 focus:outline-none"
              aria-label="Confirmation"
              placeholder={
                hardDeleteConfirmKind === 'email'
                  ? 'email@example.com'
                  : hardDeleteConfirmKind === 'login_name'
                    ? 'username'
                    : hardDeleteConfirmKind === 'phone'
                      ? 'phone number'
                      : 'user ID'
              }
              value={hardDeleteEmailConfirm}
              onChange={(e) => setHardDeleteEmailConfirm(e.target.value)}
              autoFocus
            />
            <textarea
              className="mt-3 w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] placeholder:text-gray-300 focus:border-gray-400 focus:outline-none"
              rows={2}
              placeholder="Reason (optional, audit trail)..."
              value={hardDeleteReason}
              onChange={(e) => setHardDeleteReason(e.target.value)}
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => {
                  setHardDeleteTarget(null);
                  setHardDeleteReason('');
                  setHardDeleteEmailConfirm('');
                }}
                className="rounded-md px-3 py-1.5 text-[13px] text-gray-500 hover:text-gray-900 transition"
              >
                Cancel
              </button>
              <button
                onClick={doHardDelete}
                disabled={
                  !hardDeleteConfirmTarget ||
                  hardDeleteEmailConfirm !== hardDeleteConfirmTarget ||
                  busy === hardDeleteTarget.id
                }
                className="rounded-md bg-red-600 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-red-700 transition disabled:opacity-50"
              >
                {busy === hardDeleteTarget.id ? '...' : 'Permanently Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
