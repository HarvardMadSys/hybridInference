'use client';

import { Fragment, useEffect, useMemo, useState } from 'react';
import type { AdminUser, UserDetail } from '@/lib/api/admin';
import { getUserDetail, updateUser as apiUpdateUser } from '@/lib/api/admin';
import type { CostHistoryPoint, Density, FilterState, UserRow as UserRowType } from './types';
import { UserRow } from './UserRow';
import { UserDetailPanel } from './UserDetailPanel';

interface UserTableProps {
  users: UserRowType[];
  costHistories: Record<string, CostHistoryPoint[]>;
  density: Density;
  filterState: FilterState;
  onSortChange: (sortBy: FilterState['sortBy']) => void;
  // action handlers (all return Promise for async operations)
  onApprove: (userId: string) => Promise<void>;
  onReject: (userId: string, reason: string) => Promise<void>;
  onUpdate: (userId: string, patch: Record<string, unknown>) => Promise<void>;
  onSuspend: (userId: string) => Promise<void>;
  onResume: (userId: string) => Promise<void>;
  onDelete: (userId: string, reason: string) => Promise<void>;
  onHardDelete: (userId: string, reason: string) => Promise<void>;
  onRegenerateKey: (userId: string) => Promise<void>;
}

function median(nums: number[]): number {
  if (nums.length === 0) return 0;
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

export function UserTable(props: UserTableProps) {
  const { users, costHistories, density, filterState, onSortChange } = props;
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editRole, setEditRole] = useState('');
  const [editQuota, setEditQuota] = useState('');
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const pageMedianToday = useMemo(
    () => median(users.map((u) => Number(u.usage_today_usd)).filter((n) => n > 0)),
    [users],
  );

  const showSparkline = density === 'comfortable';
  const colSpan = showSparkline ? 10 : 9;

  const sortIndicator = (col: FilterState['sortBy']) => (filterState.sortBy === col ? ' ↓' : '');
  const ariaSortFor = (col: FilterState['sortBy']): 'ascending' | 'none' =>
    filterState.sortBy === col ? 'ascending' : 'none';

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
      const d = await getUserDetail(userId);
      setDetail(d);
      setEditRole(d.role || 'free');
      setEditQuota(d.quota_daily_usd?.toString() ?? '100');
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
      if (!Object.keys(patch).length) return;
      await apiUpdateUser(expandedId, patch);
      const refreshed = await getUserDetail(expandedId);
      setDetail(refreshed);
      await props.onUpdate(expandedId, patch);
    } finally {
      setSaving(false);
    }
  };

  const doSuspend = async (userId: string) => {
    if (!confirm('Suspend this user?')) return;
    setBusy(userId);
    try {
      await props.onSuspend(userId);
      setExpandedId(null);
      setDetail(null);
    } finally {
      setBusy(null);
    }
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
  const [hardDeleteEmailConfirm, setHardDeleteEmailConfirm] = useState('');

  // Close delete/hard-delete modals if their target row leaves the visible list
  // (filter change, pagination). Without this, the modal stays open with a
  // stale target and the user could submit an action against a row they can no
  // longer see.
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
  }, [users, deleteTarget, hardDeleteTarget]);

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

  const doHardDelete = async () => {
    if (!hardDeleteTarget) return;
    if (hardDeleteEmailConfirm !== hardDeleteTarget.email) return;
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
      <div className="overflow-x-auto rounded-md border border-gray-200">
        <table className="min-w-full text-sm">
          <thead className="bg-gray-50">
            <tr className="text-left">
              <th className="w-8 px-2 py-2" />
              <th className="px-2 py-2">Email</th>
              <th className="px-2 py-2">Role</th>
              <th className="px-2 py-2" aria-sort={ariaSortFor('cost_today')}>
                <button
                  type="button"
                  className="cursor-pointer"
                  onClick={() => onSortChange('cost_today')}
                >
                  Today{sortIndicator('cost_today')}
                </button>
              </th>
              {showSparkline && <th className="w-16 px-2 py-2">7d</th>}
              <th className="px-2 py-2" aria-sort={ariaSortFor('cost_month')}>
                <button
                  type="button"
                  className="cursor-pointer"
                  onClick={() => onSortChange('cost_month')}
                >
                  Month{sortIndicator('cost_month')}
                </button>
              </th>
              <th className="px-2 py-2" aria-sort={ariaSortFor('cost_alltime')}>
                <button
                  type="button"
                  className="cursor-pointer"
                  onClick={() => onSortChange('cost_alltime')}
                >
                  All-time{sortIndicator('cost_alltime')}
                </button>
              </th>
              <th className="px-2 py-2">Status</th>
              <th className="w-8 px-2 py-2" />
              <th className="px-2 py-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.length === 0 && (
              <tr>
                <td colSpan={colSpan} className="px-4 py-8 text-center text-gray-500">
                  No users match your filters.
                </td>
              </tr>
            )}
            {users.map((u) => {
              const asAdminUser: AdminUser = {
                id: u.id,
                email: u.email,
                user_name: u.user_name,
                role: u.role,
                status: u.status,
                email_verified: u.email_verified,
                approval_note: u.approval_note,
                reviewed_at: u.reviewed_at,
                reviewed_by: u.reviewed_by,
                created_at: u.created_at,
                last_login_at: u.last_login_at,
                has_key: u.has_key,
                key_prefix: u.key_prefix,
                key_status: u.key_status,
                usage_today_usd: Number(u.usage_today_usd),
                usage_month_usd: Number(u.usage_month_usd),
                usage_alltime_usd: Number(u.usage_alltime_usd),
              };
              const isExpanded = expandedId === u.id;
              return (
                <Fragment key={u.id}>
                  <UserRow
                    user={u}
                    history={costHistories[u.id]}
                    pageMedianToday={pageMedianToday}
                    density={density}
                    expanded={isExpanded}
                    onToggleExpanded={() => toggleDetail(u.id)}
                    onApprove={() => {
                      setBusy(u.id);
                      props.onApprove(u.id).finally(() => setBusy(null));
                    }}
                    onReject={() => {
                      const reason = window.prompt('Reject reason?') ?? '';
                      if (reason) {
                        setBusy(u.id);
                        props.onReject(u.id, reason).finally(() => setBusy(null));
                      }
                    }}
                    onRegenerateKey={() => {
                      if (!confirm(`Regenerate key for ${u.email}?`)) return;
                      setBusy(u.id);
                      props.onRegenerateKey(u.id).finally(() => setBusy(null));
                    }}
                  />
                  {isExpanded && (
                    <tr>
                      <td colSpan={colSpan} className="bg-gray-50 p-4">
                        {detailLoading ? (
                          <div className="flex py-4">
                            <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                          </div>
                        ) : detail ? (
                          <UserDetailPanel
                            user={asAdminUser}
                            detail={detail}
                            editRole={editRole}
                            editQuota={editQuota}
                            saving={saving}
                            busy={busy}
                            onChangeRole={setEditRole}
                            onChangeQuota={setEditQuota}
                            onSave={doSave}
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
            <h3 className="text-[15px] font-semibold text-gray-900">Delete {deleteTarget.email}</h3>
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
              Permanently delete {hardDeleteTarget.email}
            </h3>
            <p className="mt-2 text-[12px] text-gray-600">
              This will <span className="font-semibold text-red-700">permanently wipe</span> the
              user row, all API keys, all api_logs, and prior audit-log entries for this user. This
              action <span className="font-semibold">cannot be undone</span>.
            </p>
            <p className="mt-3 text-[12px] text-gray-500">
              Type the user&apos;s email address (
              <span className="font-mono text-gray-700">{hardDeleteTarget.email}</span>) to confirm:
            </p>
            <input
              type="text"
              className="mt-2 w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] font-mono placeholder:text-gray-300 focus:border-red-400 focus:outline-none"
              placeholder="email@example.com"
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
                  hardDeleteEmailConfirm !== hardDeleteTarget.email || busy === hardDeleteTarget.id
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
