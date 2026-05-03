'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  AdminUser,
  StatusCounts,
  UserDetail,
  UserSortBy,
  approveUser,
  deleteUser,
  getUserDetail,
  hardDeleteUser,
  listUsers,
  regenerateApiKeyAdmin,
  rejectUser,
  resumeUser,
  updateUser,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { UserDetailPanel } from './UserDetailPanel';

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

export interface UsersTabProps {
  setError: (msg: string | null) => void;
  setToast: (msg: string | null) => void;
  onLoadingChange?: (loading: boolean) => void;
  refreshNonce?: number;
}

export default function UsersTab({
  setError,
  setToast,
  onLoadingChange,
  refreshNonce,
}: UsersTabProps) {
  // Users state
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [counts, setCounts] = useState<StatusCounts>({
    all: 0,
    pending_approval: 0,
    active: 0,
    suspended: 0,
    rejected: 0,
    deleted: 0,
  });
  const [filter, setFilter] = useState('');
  const [searchTerm, setSearchTerm] = useState('');
  const [sortBy, setSortBy] = useState<UserSortBy>('created');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [rejectTarget, setRejectTarget] = useState<AdminUser | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null);
  const [deleteReason, setDeleteReason] = useState('');
  const [hardDeleteTarget, setHardDeleteTarget] = useState<AdminUser | null>(null);
  const [hardDeleteReason, setHardDeleteReason] = useState('');
  const [hardDeleteEmailConfirm, setHardDeleteEmailConfirm] = useState('');
  const [newKey, setNewKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editRole, setEditRole] = useState('');
  const [editQuota, setEditQuota] = useState('');
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await listUsers({
        status: filter || undefined,
        limit: 100,
        offset: 0,
        search: searchTerm || undefined,
        sortBy: sortBy !== 'created' ? sortBy : undefined,
      });
      setUsers(d.users);
      setCounts(d.status_counts);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [filter, searchTerm, sortBy, setError]);

  useEffect(() => {
    load();
  }, [load, refreshNonce]);

  useEffect(() => {
    onLoadingChange?.(loading);
  }, [loading, onLoadingChange]);

  const toggleDetail = async (uid: string) => {
    if (expandedId === uid) {
      setExpandedId(null);
      setDetail(null);
      return;
    }
    setExpandedId(uid);
    setDetailLoading(true);
    setDetail(null);
    try {
      const d = await getUserDetail(uid);
      setDetail(d);
      setEditRole(d.role || 'free');
      setEditQuota(d.quota_daily_usd?.toString() || '100');
    } catch (e) {
      setError(getErrorMessage(e));
      setExpandedId(null);
    } finally {
      setDetailLoading(false);
    }
  };

  const act = async (fn: () => Promise<void>) => {
    try {
      await fn();
    } catch (e) {
      setError(getErrorMessage(e));
    }
  };
  const doApprove = (u: AdminUser) => {
    setBusy(u.id);
    act(async () => {
      await approveUser(u.id);
      setToast(`Approved ${u.email}`);
      await load();
    }).finally(() => setBusy(null));
  };
  const doReject = () => {
    if (!rejectTarget || !rejectReason.trim()) return;
    setBusy(rejectTarget.id);
    act(async () => {
      await rejectUser(rejectTarget.id, rejectReason.trim());
      setToast(`Rejected ${rejectTarget.email}`);
      setRejectTarget(null);
      setRejectReason('');
      await load();
    }).finally(() => setBusy(null));
  };
  const doDelete = () => {
    if (!deleteTarget || !deleteReason.trim()) return;
    setBusy(deleteTarget.id);
    act(async () => {
      await deleteUser(deleteTarget.id, deleteReason.trim());
      setToast(`Deleted ${deleteTarget.email}`);
      setDeleteTarget(null);
      setDeleteReason('');
      setExpandedId(null);
      setDetail(null);
      await load();
    }).finally(() => setBusy(null));
  };
  const doResume = (u: AdminUser) => {
    setBusy(u.id);
    act(async () => {
      await resumeUser(u.id);
      setToast(`Resumed ${u.email}`);
      setExpandedId(null);
      setDetail(null);
      await load();
    }).finally(() => setBusy(null));
  };
  const doHardDelete = () => {
    if (!hardDeleteTarget) return;
    if (hardDeleteEmailConfirm !== hardDeleteTarget.email) return;
    setBusy(hardDeleteTarget.id);
    const email = hardDeleteTarget.email;
    act(async () => {
      await hardDeleteUser(hardDeleteTarget.id, hardDeleteReason.trim() || undefined);
      setToast(`Permanently deleted ${email}`);
      setHardDeleteTarget(null);
      setHardDeleteReason('');
      setHardDeleteEmailConfirm('');
      setExpandedId(null);
      setDetail(null);
      await load();
    }).finally(() => setBusy(null));
  };
  const doRegen = (u: AdminUser) => {
    if (!confirm(`Regenerate key for ${u.email}?`)) return;
    setBusy(u.id);
    act(async () => {
      const r = await regenerateApiKeyAdmin(u.id);
      setNewKey(r.api_key);
      await load();
    }).finally(() => setBusy(null));
  };
  const doSuspend = (uid: string) => {
    if (!confirm('Suspend this user?')) return;
    setBusy(uid);
    act(async () => {
      await updateUser(uid, { status: 'suspended' });
      setToast('Suspended');
      setExpandedId(null);
      await load();
    }).finally(() => setBusy(null));
  };
  const doReactivate = (uid: string) => {
    setBusy(uid);
    act(async () => {
      await updateUser(uid, { status: 'active' });
      setToast('Reactivated');
      setExpandedId(null);
      await load();
    }).finally(() => setBusy(null));
  };
  const doSave = () => {
    if (!expandedId || !detail) return;
    setSaving(true);
    act(async () => {
      const u: Record<string, unknown> = {};
      if (editRole !== (detail.role || 'free')) u.role = editRole;
      if (editQuota !== (detail.quota_daily_usd?.toString() || '100'))
        u.quota_daily_cost_usd = Number(editQuota);
      if (!Object.keys(u).length) return;
      await updateUser(expandedId, u);
      setToast('Saved');
      const d = await getUserDetail(expandedId);
      setDetail(d);
      await load();
    }).finally(() => setSaving(false));
  };

  const filters = [
    { key: '', label: 'All', count: counts.all },
    { key: 'pending_approval', label: 'Pending', count: counts.pending_approval },
    { key: 'active', label: 'Active', count: counts.active },
    { key: 'rejected', label: 'Rejected', count: counts.rejected },
    { key: 'suspended', label: 'Suspended', count: counts.suspended },
    { key: 'deleted', label: 'Deleted', count: counts.deleted },
  ];

  return (
    <>
      {/* Search + Sort */}
      <div className="mt-6 flex items-center gap-3">
        <input
          type="text"
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          placeholder="Search by email or name..."
          className="flex-1 rounded-lg border border-gray-200 bg-white px-4 py-2 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
        />
        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value as UserSortBy)}
          className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-700 focus:border-gray-400 focus:outline-none"
        >
          <option value="created">Created</option>
          <option value="cost_today">Cost today</option>
          <option value="cost_month">Cost this month</option>
          <option value="cost_alltime">Cost all-time</option>
          <option value="last_login">Recent login</option>
        </select>
      </div>

      {/* Filter tabs */}
      <div className="mt-4 flex items-center gap-1 border-b border-gray-200">
        {filters.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`relative px-3 pb-2.5 pt-1 text-[13px] font-medium transition ${
              filter === f.key ? 'text-gray-900' : 'text-gray-500 hover:text-gray-800'
            }`}
          >
            {f.label}
            {f.count !== undefined && (
              <span
                className={`ml-1 tabular-nums text-[11px] ${
                  filter === f.key ? 'text-gray-500' : 'text-gray-400'
                }`}
              >
                {f.count}
              </span>
            )}
            {filter === f.key && (
              <span className="absolute inset-x-0 bottom-0 h-[2px] bg-gray-900 rounded-full" />
            )}
          </button>
        ))}
      </div>

      {/* New key */}
      {newKey && (
        <div className="mt-4 rounded-lg border border-gray-200 bg-white p-4">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-gray-900">New API key generated</span>
            <button onClick={() => setNewKey(null)} className="text-gray-300 hover:text-gray-500">
              &times;
            </button>
          </div>
          <p className="mt-1 text-[12px] text-gray-400">
            Copy it now. It won&apos;t be shown again.
          </p>
          <div className="mt-3 flex items-center gap-2">
            <code className="flex-1 rounded-md bg-gray-50 px-3 py-2 font-mono text-[13px] text-gray-900 break-all select-all border border-gray-100">
              {newKey}
            </code>
            <button
              onClick={() => {
                navigator.clipboard.writeText(newKey);
                setCopied(true);
                setTimeout(() => setCopied(false), 2000);
              }}
              className="shrink-0 rounded-md bg-gray-900 px-3 py-2 text-[12px] font-semibold text-white hover:bg-gray-800 transition"
            >
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
        </div>
      )}

      {/* List */}
      <div className="mt-6">
        {loading ? (
          <div className="flex justify-center py-24">
            <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          </div>
        ) : users.length === 0 ? (
          <div className="py-24 text-center">
            <p className="text-[13px] text-gray-400">
              {filter || searchTerm ? 'No users match this filter.' : 'No users yet.'}
            </p>
          </div>
        ) : (
          <div>
            {users.map((u, i) => {
              const isOpen = expandedId === u.id;
              return (
                <div key={u.id}>
                  {/* Row */}
                  <div
                    onClick={() => toggleDetail(u.id)}
                    className={`group flex cursor-pointer items-center gap-4 py-3.5 transition ${
                      i > 0 ? 'border-t border-gray-100' : ''
                    } ${isOpen ? 'opacity-100' : 'hover:bg-gray-50/50'}`}
                    style={{ paddingLeft: 4, paddingRight: 4 }}
                  >
                    {/* Avatar */}
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gray-900 text-[11px] font-bold text-white">
                      {(u.user_name || u.email).charAt(0).toUpperCase()}
                    </div>

                    {/* Main */}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <span className="truncate text-[14px] font-medium text-gray-900">
                          {u.email}
                        </span>
                        {u.status === 'pending_approval' && (
                          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold text-amber-700">
                            PENDING
                          </span>
                        )}
                        {u.status === 'suspended' && (
                          <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-bold text-gray-500">
                            SUSPENDED
                          </span>
                        )}
                        {u.status === 'rejected' && (
                          <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-bold text-red-500">
                            REJECTED
                          </span>
                        )}
                        {u.status === 'deleted' && (
                          <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-bold text-gray-600">
                            DELETED
                          </span>
                        )}
                        {u.role && u.role !== 'free' && (
                          <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-bold text-blue-700">
                            {u.role}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-2 text-[12px] text-gray-500">
                        <span>{relTime(u.created_at)}</span>
                        {u.has_key && (
                          <span className="font-mono text-gray-400">{u.key_prefix}</span>
                        )}
                        {(() => {
                          const isCostSort =
                            sortBy === 'cost_today' ||
                            sortBy === 'cost_month' ||
                            sortBy === 'cost_alltime';
                          const usageVal =
                            sortBy === 'cost_month'
                              ? Number(u.usage_month_usd ?? 0)
                              : sortBy === 'cost_alltime'
                                ? Number(u.usage_alltime_usd ?? 0)
                                : Number(u.usage_today_usd ?? 0);
                          const suffix =
                            sortBy === 'cost_month'
                              ? '/mo'
                              : sortBy === 'cost_alltime'
                                ? '/all'
                                : '/today';
                          // Cost sorts: always show usage (even without active key).
                          // Other sorts: only show if user has an active key.
                          if (isCostSort || u.has_key) {
                            return (
                              <span className="tabular-nums text-gray-700">
                                ${usageVal.toFixed(2)} {suffix}
                              </span>
                            );
                          }
                          return null;
                        })()}
                      </div>
                    </div>

                    {/* Quick actions */}
                    <div
                      className="flex items-center gap-2 opacity-0 group-hover:opacity-100 transition"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {u.status === 'pending_approval' && (
                        <>
                          <button
                            onClick={() => doApprove(u)}
                            disabled={busy === u.id}
                            className="rounded-md bg-gray-900 px-3 py-1 text-[12px] font-semibold text-white hover:bg-gray-800 transition disabled:opacity-50"
                          >
                            Approve
                          </button>
                          <button
                            onClick={() => {
                              setRejectTarget(u);
                              setRejectReason('');
                            }}
                            className="rounded-md px-3 py-1 text-[12px] font-semibold text-red-500 hover:bg-red-50 transition"
                          >
                            Reject
                          </button>
                        </>
                      )}
                      {u.status === 'active' && u.has_key && (
                        <button
                          onClick={() => doRegen(u)}
                          disabled={busy === u.id}
                          className="rounded-md px-3 py-1 text-[12px] text-gray-400 hover:text-gray-900 hover:bg-gray-100 transition disabled:opacity-50"
                        >
                          Regenerate
                        </button>
                      )}
                    </div>

                    <svg
                      className={`h-4 w-4 shrink-0 text-gray-300 transition-transform ${
                        isOpen ? 'rotate-180' : ''
                      }`}
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={2}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M19.5 8.25l-7.5 7.5-7.5-7.5"
                      />
                    </svg>
                  </div>

                  {/* Rejected note */}
                  {u.status === 'rejected' && u.approval_note && !isOpen && (
                    <p className="pb-3 pl-16 text-[12px] text-red-400 italic">
                      &ldquo;{u.approval_note}&rdquo;
                    </p>
                  )}

                  {/* Detail */}
                  {isOpen && (
                    <div className="ml-12 mb-4 mt-1">
                      {detailLoading ? (
                        <div className="flex py-8">
                          <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                        </div>
                      ) : detail ? (
                        <UserDetailPanel
                          user={u}
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
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Reject modal */}
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
            <h3 className="text-[15px] font-semibold text-gray-900">Reject {rejectTarget.email}</h3>
            <textarea
              className="mt-3 w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] placeholder:text-gray-300 focus:border-gray-400 focus:outline-none"
              rows={3}
              placeholder="Reason (sent to user)..."
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

      {/* Hard-delete (permanent) modal */}
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
