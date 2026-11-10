'use client';

import { createContext, Fragment, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import type {
  AdminModelVisibilityItem,
  AdminUser,
  UpdateUserData,
  UserAskQuestionFraction,
  UserAutomationScore,
  UserDetail,
  UserTurnAverages,
} from '@/lib/api/admin';
import { getUserDetail, listModelVisibility } from '@/lib/api/admin';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
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
  // Write handlers. Each throws on failure: a confirmation the table opens
  // stays open with the error, and a direct write shows it where it was made.
  onApprove: (userId: string) => Promise<void>;
  onReject: (userId: string, reason: string) => Promise<void>;
  onUpdate: (userId: string, patch: UpdateUserData) => Promise<void>;
  onSuspend: (userId: string, suspensionMessage?: string | null) => Promise<void>;
  onResume: (userId: string) => Promise<void>;
  onDelete: (userId: string, reason: string) => Promise<void>;
  onHardDelete: (userId: string, reason: string) => Promise<void>;
}

/** Whether the open confirmation is writing; fields inside it lock meanwhile. */
const ConfirmBusy = createContext(false);

/**
 * A free-text field inside a confirmation (a reason, a message). It keeps its
 * own text and reports it through `onChange`, because the confirmation body is
 * fixed when the dialog opens.
 */
function DialogTextField({
  label,
  placeholder,
  rows = 3,
  maxLength,
  onChange,
}: {
  label: string;
  placeholder?: string;
  rows?: number;
  maxLength?: number;
  onChange: (value: string) => void;
}) {
  const busy = useContext(ConfirmBusy);
  const [value, setValue] = useState('');
  return (
    <label className="block text-[12px] font-medium text-gray-700">
      {label}
      <textarea
        value={value}
        onChange={(event) => {
          setValue(event.target.value);
          onChange(event.target.value);
        }}
        disabled={busy}
        rows={rows}
        maxLength={maxLength}
        placeholder={placeholder}
        className="mt-1 w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-[13px] font-normal text-gray-900 placeholder:text-gray-400 focus:border-gray-500 focus:outline-none disabled:opacity-60"
      />
    </label>
  );
}

const IDENTIFIER_NAME = {
  email: 'email address',
  login_name: 'username',
  phone: 'phone number',
  id: 'user ID',
} as const;

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
  const [availableModels, setAvailableModels] = useState<AdminModelVisibilityItem[]>([]);
  const confirmation = useConfirmAction();
  const toast = useAccountToast();
  // The rows on screen now, for a confirmation opened before a refetch.
  const usersRef = useRef(users);
  useEffect(() => {
    usersRef.current = users;
  }, [users]);

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
    // A write in flight belongs to the open row: it stays open until the write ends.
    if (confirmation.busy) return;
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
      setAvailableModels(visibility.models);
    } catch {
      setExpandedId(null);
    } finally {
      setDetailLoading(false);
    }
  };

  const collapse = () => {
    setExpandedId(null);
    setDetail(null);
  };

  // A confirmation can stay open while the list refetches (filters, paging).
  // Refuse to act on a row the admin can no longer see; nothing is written.
  const ensureListed = (userId: string, label: string) => {
    if (!usersRef.current.some((u) => u.id === userId)) {
      throw new Error(`${label} is no longer in the list. Cancel and find the user again.`);
    }
  };

  const labelOf = (user: AdminUser | UserRowType) => userAccountLabel(user) || user.id;

  // An edit saved from the detail panel: one update, then show what was saved.
  const updateDetail = async (patch: UpdateUserData) => {
    if (!expandedId || !detail) return;
    const userId = expandedId;
    await props.onUpdate(userId, patch);
    try {
      const refreshed = await getUserDetail(userId);
      // Only onto the same user's detail, should another row be open by now.
      setDetail((prev) => (prev && prev.id === userId ? refreshed : prev));
    } catch {
      // Saved, but the refetch failed: show the values that were written.
      setDetail((prev) =>
        prev && prev.id === userId
          ? {
              ...prev,
              ...(patch.role !== undefined ? { role: patch.role } : {}),
              ...(patch.quota_daily_cost_usd !== undefined
                ? { quota_daily_usd: patch.quota_daily_cost_usd }
                : {}),
              ...(patch.disabled_models !== undefined
                ? { disabled_models: patch.disabled_models }
                : {}),
              ...(patch.max_concurrent_requests !== undefined
                ? { max_concurrent_requests: patch.max_concurrent_requests }
                : {}),
              ...(patch.admin_note !== undefined ? { admin_note: patch.admin_note } : {}),
              ...(patch.suspension_message !== undefined
                ? { suspension_message: patch.suspension_message }
                : {}),
            }
          : prev,
      );
    }
  };

  // Approve, Reactivate and Resume act on the click. A failure is toasted, as
  // the handler toasts a success.
  const apply = async (run: () => Promise<void>) => {
    const failure = await confirmation.perform({ run });
    if (failure) toast.error(failure);
  };

  const approve = (user: UserRowType) => void apply(() => props.onApprove(user.id));

  const requestReject = (user: UserRowType) => {
    const label = labelOf(user);
    const reason = { value: '' };
    confirmation.request({
      title: `Reject ${label}?`,
      body: (
        <>
          <p>
            Their registration is rejected. The user will see this reason in their pending-approval
            state.
          </p>
          <DialogTextField
            label="Reason for rejection (required)"
            placeholder="Reason for rejection..."
            onChange={(value) => {
              reason.value = value;
            }}
          />
        </>
      ),
      confirmLabel: 'Reject',
      busyLabel: 'Rejecting…',
      tone: 'danger',
      run: async () => {
        const text = reason.value.trim();
        if (!text) throw new Error('Enter a reason for the rejection.');
        ensureListed(user.id, label);
        await props.onReject(user.id, text);
      },
    });
  };

  const requestSuspend = (user: AdminUser) => {
    const label = labelOf(user);
    const message = { value: '' };
    confirmation.request({
      title: `Suspend ${label}?`,
      body: (
        <>
          <p>The user will be unable to authenticate or use their API key while suspended.</p>
          <DialogTextField
            label="Message shown to the user at login (optional)"
            placeholder="e.g. Your account was suspended for a policy violation. Contact support to appeal."
            maxLength={2000}
            onChange={(value) => {
              message.value = value;
            }}
          />
        </>
      ),
      confirmLabel: 'Suspend',
      busyLabel: 'Suspending…',
      tone: 'danger',
      run: async () => {
        ensureListed(user.id, label);
        await props.onSuspend(user.id, message.value.trim() ? message.value.trim() : null);
        collapse();
      },
    });
  };

  const reactivate = (user: AdminUser) =>
    void apply(async () => {
      await props.onUpdate(user.id, { status: 'active' });
      collapse();
    });

  const resume = (user: AdminUser) =>
    void apply(async () => {
      await props.onResume(user.id);
      collapse();
    });

  const requestDelete = (user: AdminUser) => {
    const label = labelOf(user);
    const reason = { value: '' };
    confirmation.request({
      title: `Delete ${label}?`,
      body: (
        <>
          <p>This will revoke API keys, purge sessions, and set the account to deleted.</p>
          <DialogTextField
            label="Reason for deletion (required)"
            placeholder="Reason for deletion..."
            onChange={(value) => {
              reason.value = value;
            }}
          />
        </>
      ),
      confirmLabel: 'Delete',
      busyLabel: 'Deleting…',
      tone: 'danger',
      run: async () => {
        const text = reason.value.trim();
        if (!text) throw new Error('Enter a reason for the deletion.');
        ensureListed(user.id, label);
        await props.onDelete(user.id, text);
        collapse();
      },
    });
  };

  const requestHardDelete = (user: AdminUser) => {
    // What the admin types to confirm a permanent delete: the account's email,
    // or its login name when it has none (the first-run administrator), or as a
    // last resort its id. The backend does not see it; it is a typo guard.
    const identifier = userAccountLabel(user);
    if (!identifier) return;
    const reason = { value: '' };
    confirmation.request({
      title: `Permanently delete ${identifier}?`,
      body: (
        <>
          <p>
            This will <span className="font-semibold text-red-700">permanently wipe</span> the user
            row, all API keys, all api_logs, and prior audit-log entries for this user. This action{' '}
            <span className="font-semibold">cannot be undone</span>.
          </p>
          <p>
            Type the user&apos;s {IDENTIFIER_NAME[userAccountLabelKind(user)]} below to confirm.
          </p>
          <DialogTextField
            label="Reason (optional, audit trail)"
            placeholder="Reason (optional, audit trail)..."
            rows={2}
            onChange={(value) => {
              reason.value = value;
            }}
          />
        </>
      ),
      confirmLabel: 'Permanently delete',
      busyLabel: 'Deleting…',
      tone: 'danger',
      requireText: identifier,
      run: async () => {
        ensureListed(user.id, identifier);
        await props.onHardDelete(user.id, reason.value.trim());
        collapse();
      },
    });
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
                    busy={confirmation.busy}
                    onApprove={() => approve(u)}
                    onReject={() => requestReject(u)}
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
                              availableModels={availableModels}
                              confirmation={confirmation}
                              onUpdate={updateDetail}
                              onSuspend={requestSuspend}
                              onReactivate={reactivate}
                              onResume={resume}
                              onRequestDelete={requestDelete}
                              onRequestHardDelete={requestHardDelete}
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

      <ConfirmBusy.Provider value={confirmation.busy}>{confirmation.dialog}</ConfirmBusy.Provider>
    </>
  );
}
